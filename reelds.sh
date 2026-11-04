#!/bin/sh
# ReelDS encoder launcher for Linux and macOS.
# Detects the OS/CPU, unpacks the matching bundled Python + numpy + Pillow + ffmpeg from
# vendor/ into .runtime/ on first run, then runs pc/reelds.py with your arguments.
#   ./reelds.sh build ~/Videos --out /media/SDCARD
#   ./reelds.sh selftest
#   ./reelds.sh              opens the GUI
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"

case "$(uname -s)" in
    Linux)  os=linux ;;
    Darwin) os=macos ;;
    *) echo "reelds: unsupported OS $(uname -s) (use reelds.cmd on Windows)"; exit 1 ;;
esac
case "$(uname -m)" in
    x86_64|amd64)  arch=x64 ;;
    arm64|aarch64) arch=arm64 ;;
    *) arch="$(uname -m)" ;;
esac
plat="$os-$arch"
VENDOR="$ROOT/vendor/$plat"
RT="$ROOT/.runtime/$plat"

if [ ! -f "$VENDOR/VERSION" ]; then
    # No bundle for this machine (e.g. Linux on ARM): fall back to a system install.
    if command -v python3 >/dev/null 2>&1 && python3 -c "import numpy, PIL" 2>/dev/null; then
        echo "reelds: no bundled runtime for $plat, using system python3 + ffmpeg" >&2
        exec python3 "$ROOT/pc/reelds.py" "$@"
    fi
    echo "reelds: no bundled runtime for $plat. Install python3, numpy, pillow and ffmpeg," >&2
    echo "        then run: python3 pc/reelds.py ..." >&2
    exit 1
fi

PY="$RT/python/bin/python3"
STAMP="$RT/.ok-$(cat "$VENDOR/VERSION")"
if [ ! -f "$STAMP" ]; then
    echo "reelds: first run on $plat - unpacking bundled Python, numpy, Pillow and ffmpeg..." >&2
    rm -rf "$RT"
    mkdir -p "$RT"
    tar -xzf "$VENDOR/python.tar.gz" -C "$RT"
    if [ "$os" = macos ]; then
        # repo downloaded as a zip from a browser: clear Gatekeeper quarantine on the binaries
        xattr -dr com.apple.quarantine "$RT" 2>/dev/null || true
    fi
    "$PY" -m pip install --no-index --find-links "$VENDOR/wheels" --disable-pip-version-check \
        --no-warn-script-location -q numpy pillow imageio-ffmpeg
    if [ "$os" = macos ]; then
        xattr -dr com.apple.quarantine "$RT" 2>/dev/null || true
    fi
    find "$RT" -path "*imageio_ffmpeg/binaries/*" -type f -exec chmod +x {} + 2>/dev/null || true
    touch "$STAMP"
    echo "reelds: ready." >&2
fi

exec "$PY" "$ROOT/pc/reelds.py" "$@"
