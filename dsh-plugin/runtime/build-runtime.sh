#!/usr/bin/env bash
# Build a self-contained HitMind daemon binary and pack it as the per-platform npm
# runtime package `dsh-hitmind-runtime-<platform>-<arch>`, then (given --publish)
# publish it so `dsh-hitmind`'s optionalDependencies pick it up.
#
# Zero-Python distribution: the plugin (index.js) resolves this package at runtime
# via resolveBundledDaemon() and spawns it directly — no Python interpreter needed.
#
# Usage:
#   ./build-runtime.sh                 # build + pack into ./dist (current platform)
#   ./build-runtime.sh --publish       # additionally `npm publish` (needs npm login)
#
# Requirements: a Python 3.10+ with pip, plus `uv` (preferred) or a venv. PyInstaller
# produces a single-file executable (~12 MB; CPython + SQLite runtime bundled).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"      # hitmind repo root (contains hitmind/ package)
VERSION="0.1.0"
PUBLISH="${1:-}"

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

echo "[build-runtime] platform=$OS arch=$ARCH version=$VERSION"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

# --- isolated build env (PyInstaller + hitmind core) ---
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --quiet --upgrade pip pyinstaller
python -m pip install --quiet -e "$REPO"

# --- entry point + onefile build ---
cat > entry.py <<'PY'
from hitmind.daemon import main
raise SystemExit(main())
PY
pyinstaller --onefile --name hitmind-daemon --collect-submodules hitmind \
  --distpath dist --workpath build --specpath build entry.py >/dev/null 2>&1
BIN="dist/hitmind-daemon"
[ -f "$BIN" ] || { echo "build failed: $BIN missing"; exit 1; }
echo "[build-runtime] built $(du -h "$BIN" | cut -f1) binary"

# --- assemble the runtime package ---
PKG="pkg/dsh-hitmind-runtime-$OS-$ARCH"
mkdir -p "$PKG"
sed -e "s/__PLATFORM__/$OS/g" -e "s/__ARCH__/$ARCH/g" "$HERE/package.template.json" > "$PKG/package.json"
if [ "$OS" = "win32" ]; then cp "$BIN" "$PKG/hitmind-daemon.exe"; else cp "$BIN" "$PKG/hitmind-daemon"; chmod +x "$PKG/hitmind-daemon"; fi
mkdir -p "$HERE/dist"
( cd "$PKG" && npm pack --pack-destination "$HERE/dist" >/dev/null )
echo "[build-runtime] packed -> $HERE/dist/dsh-hitmind-runtime-$OS-$ARCH-$VERSION.tgz"

if [ "$PUBLISH" = "--publish" ]; then
  ( cd "$PKG" && npm publish --access public )
  echo "[build-runtime] published dsh-hitmind-runtime-$OS-$ARCH@$VERSION"
fi
