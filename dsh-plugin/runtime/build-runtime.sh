#!/usr/bin/env bash
# Build a self-contained HitMind daemon binary and pack it as the per-platform npm
# runtime package `dsh-hitmind-runtime-<platform>-<arch>`, then (given --publish)
# publish it so `dsh-hitmind`'s optionalDependencies pick it up.
#
# Zero-Python distribution: the plugin (index.js) resolves this package at runtime
# via resolveBundledDaemon() and spawns it directly — no Python interpreter needed.
#
# Usage:
#   ./build-runtime.sh                     # build + pack into ./dist (current platform)
#   ./build-runtime.sh --publish           # additionally `npm publish` (needs npm login / OIDC)
#   HITMIND_RUNTIME_OS=darwin HITMIND_RUNTIME_ARCH=arm64 ./build-runtime.sh
#                                          # override the platform tag (default: detected)
#
# Requirements: a Python 3.11+ with pip, plus `uv` (preferred) or a venv. PyInstaller
# produces a single-file executable (~12 MB; CPython + SQLite runtime bundled) and
# CANNOT cross-compile, so each platform/arch must be built on a matching machine
# (the GitHub Actions matrix in .github/workflows/runtime.yml does exactly this).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"      # hitmind repo root (contains hitmind/ package)

# Version comes from the repo's single source of truth (pyproject.toml), so adapter
# and runtime packages always share one version. Override with HITMIND_RUNTIME_VERSION.
VERSION="${HITMIND_RUNTIME_VERSION:-$(sed -nE 's/^version *= *"([^"]+)".*/\1/p' "$REPO/pyproject.toml" | head -1)}"
[ -n "$VERSION" ] || { echo "cannot determine version from pyproject.toml"; exit 1; }
PUBLISH="${1:-}"

# Platform/arch: env override wins (for CI cross-tagging), else detect from uname.
if [ -n "${HITMIND_RUNTIME_OS:-}" ] && [ -n "${HITMIND_RUNTIME_ARCH:-}" ]; then
  OS="$HITMIND_RUNTIME_OS"; ARCH="$HITMIND_RUNTIME_ARCH"
else
  case "$(uname -s)" in
    Linux*)  OS=linux ;;
    Darwin*) OS=darwin ;;
    MINGW*|MSYS*|CYGWIN*) OS=win32 ;;
    *) echo "unsupported OS: $(uname -s)"; exit 1 ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) ARCH=x64 ;;
    arm64|aarch64) ARCH=arm64 ;;
    *) echo "unsupported arch: $(uname -m)"; exit 1 ;;
  esac
fi

echo "[build-runtime] platform=$OS arch=$ARCH version=$VERSION"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

# --- isolated build env (PyInstaller + hitmind core) ---
# Pin the interpreter so local and CI builds match and stay slim (~12 MB). The
# host's default python3 can be much newer/heavier (e.g. 3.14) and bloat the
# onefile binary; override with HITMIND_BUILD_PYTHON if needed.
PYVER="${HITMIND_BUILD_PYTHON:-3.12}"
if command -v uv >/dev/null 2>&1; then
  uv venv --python "$PYVER" .venv
  . .venv/bin/activate
  uv pip install --python .venv/bin/python --quiet pyinstaller
  uv pip install --python .venv/bin/python --quiet -e "$REPO"
else
  python3 -m venv .venv
  . .venv/bin/activate
  python -m pip install --quiet --upgrade pip pyinstaller
  python -m pip install --quiet -e "$REPO"
fi
python --version

# --- onefile build using the repo's shared entry point ---
pyinstaller --onefile --name hitmind-daemon --collect-submodules hitmind \
  --distpath dist --workpath build --specpath build "$HERE/entry.py" >/dev/null 2>&1
# PyInstaller appends .exe on Windows; account for the OS being built for.
BIN="dist/hitmind-daemon"
[ "$OS" = "win32" ] && BIN="dist/hitmind-daemon.exe"
[ -f "$BIN" ] || { echo "build failed: $BIN missing"; ls -la dist 2>/dev/null; exit 1; }

# macOS arm64 refuses to execute unsigned binaries; ad-hoc sign so the sidecar runs
# after npm extraction (no Apple Developer ID / notarization needed for npm-distributed
# sidecars, since npm files carry no quarantine xattr).
if [ "$OS" = "darwin" ] && command -v codesign >/dev/null 2>&1; then
  codesign --force --sign - "$BIN" && echo "[build-runtime] ad-hoc signed (darwin)"
fi
echo "[build-runtime] built $(du -h "$BIN" | cut -f1) binary"

# --- assemble the runtime package ---
PKG="pkg/dsh-hitmind-runtime-$OS-$ARCH"
mkdir -p "$PKG"
sed -e "s/__PLATFORM__/$OS/g" -e "s/__ARCH__/$ARCH/g" -e "s/__VERSION__/$VERSION/g" \
  "$HERE/package.template.json" > "$PKG/package.json"
if [ "$OS" = "win32" ]; then cp "$BIN" "$PKG/hitmind-daemon.exe"; else cp "$BIN" "$PKG/hitmind-daemon"; chmod +x "$PKG/hitmind-daemon"; fi
mkdir -p "$HERE/dist"
( cd "$PKG" && npm pack --pack-destination "$HERE/dist" >/dev/null )
echo "[build-runtime] packed -> $HERE/dist/dsh-hitmind-runtime-$OS-$ARCH-$VERSION.tgz"

if [ "$PUBLISH" = "--publish" ]; then
  # provenance is off by default so a bare Granular Access Token can publish
  # (provenance needs an OIDC-capable identity and triggers an account-read the
  # token may lack). Set NPM_PROVENANCE=true when publishing via OIDC.
  ( cd "$PKG" && npm publish --access public --provenance="${NPM_PROVENANCE:-false}" )
  echo "[build-runtime] published dsh-hitmind-runtime-$OS-$ARCH@$VERSION"
fi
