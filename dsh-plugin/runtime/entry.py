"""PyInstaller entry point for the self-contained HitMind daemon binary.

Kept in the repo (instead of generated in a temp dir) so local builds and the
GitHub Actions matrix build the exact same entry. ``build-runtime.sh`` bundles
this into a single-file ``hitmind-daemon`` executable per platform.
"""

from hitmind.daemon import main

raise SystemExit(main())
