# Third-party notices

ReelDS itself is licensed under the **GNU General Public License v3.0 or later**
(see [`LICENSE`](LICENSE)). It includes or downloads the following third-party software,
each under its own license.

## DSi app (`reelds.nds`)

| Component | License | Source |
|---|---|---|
| [BlocksDS](https://github.com/blocksds/sdk) runtime (libnds, crt0, ARM7 core) linked into the app | zlib / MIT-style (see the BlocksDS `licenses/` folder) | https://github.com/blocksds/sdk |

## PC encoder

| Component | License | Source |
|---|---|---|
| CPython (portable builds in `vendor/*/python.tar.gz`) | PSF License | https://github.com/astral-sh/python-build-standalone |
| NumPy (wheels in `vendor/*/wheels/`) | BSD-3-Clause | https://github.com/numpy/numpy |
| Pillow (wheels in `vendor/*/wheels/`) | MIT-CMU (HPND) | https://github.com/python-pillow/Pillow |
| imageio-ffmpeg (wheels in `vendor/*/wheels/`) | BSD-2-Clause | https://github.com/imageio/imageio-ffmpeg |
| FFmpeg (binary inside the imageio-ffmpeg wheels) | GPL (build includes GPL components) | https://ffmpeg.org — build details: https://github.com/imageio/imageio-ffmpeg |
| Tcl/Tk (inside the portable CPython, used by the GUI) | Tcl/Tk license (BSD-style) | https://www.tcl.tk |

The exact versions bundled are listed in [`vendor/README.md`](vendor/README.md). The
`vendor/` folder is filled by `tools/fetch_vendor.py`; nothing in it is modified.

## Build tools (not distributed)

The GitHub Actions workflows use the BlocksDS Docker image to build `reelds.nds`; they are
not part of what ReelDS distributes.
