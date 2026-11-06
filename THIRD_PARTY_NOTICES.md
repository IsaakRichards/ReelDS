# Third-party notices

ReelDS itself is licensed under the **GNU General Public License v3.0 or later**
(see [`LICENSE`](LICENSE)). It includes or downloads the following third-party software,
each under its own license.

## DSi app (`reelds.nds`)

| Component | License | Source |
|---|---|---|
| libnds (BlocksDS) | Zlib | https://github.com/blocksds/libnds |
| FatFs (SD card file system, inside libnds) | FatFs license (BSD-style, 1-clause) | http://elm-chan.org/fsw/ff/ |
| BlocksDS startup code and linker scripts (`sys/crts`) | MPL-2.0 | https://github.com/blocksds/sdk/tree/master/sys/crts |
| BlocksDS default ARM7 core (`arm7_maxmod.elf`) | Zlib-style | https://github.com/blocksds/sdk/tree/master/sys/arm7/main_core |
| Maxmod (linked into the ARM7 core) | ISC | https://github.com/blocksds/maxmod |
| picolibc (C library) | BSD-3-Clause and similar permissive licenses | https://github.com/picolibc/picolibc |
| NDS bootloader `load.bin` (starts games and homebrew; built in CI, embedded in the app) | GPL-2.0-or-later | https://github.com/devkitPro/nds-bootloader (commit `a0f8190`) |
| Homebrew Menu bootstub (`ds/third_party/bootstub/bootstub.s`) | GPL-2.0-or-later | https://github.com/devkitPro/nds-hb-menu |

Copies of the GPL-2.0 text are in [`ds/third_party/COPYING.GPL-2.0`](ds/third_party/COPYING.GPL-2.0);
see [`ds/third_party/README.md`](ds/third_party/README.md) for build details.

## PC encoder

| Component | License | Source |
|---|---|---|
| CPython (portable builds in `vendor/*/python.tar.gz`) | PSF License | https://github.com/astral-sh/python-build-standalone |
| NumPy (wheels in `vendor/*/wheels/`) | BSD-3-Clause | https://github.com/numpy/numpy |
| Pillow (wheels in `vendor/*/wheels/`) | MIT-CMU (HPND) | https://github.com/python-pillow/Pillow |
| imageio-ffmpeg (wheels in `vendor/*/wheels/`) | BSD-2-Clause | https://github.com/imageio/imageio-ffmpeg |
| FFmpeg (binary inside the imageio-ffmpeg wheels) | GPL (build includes GPL components) | https://ffmpeg.org — build details: https://github.com/imageio/imageio-ffmpeg |
| Tcl/Tk (inside the portable CPython, used by the GUI) | Tcl/Tk license (BSD-style) | https://www.tcl.tk |
| Game save sizes (`pc/savesizes.json`, generated from TWiLight Menu++'s `ROMList.h`, originally from melonDS) | GPL-3.0-or-later | https://github.com/DS-Homebrew/TWiLightMenu · https://github.com/melonDS-emu/melonDS |

## Used at run time, not included

Retail DS games are started through **nds-bootstrap** (GPL-3.0), which must already be on the
SD card (it comes with TWiLight Menu++). ReelDS only writes its settings file and starts it.

The exact versions bundled are listed in [`vendor/README.md`](vendor/README.md). The
`vendor/` folder is filled by `tools/fetch_vendor.py`; nothing in it is modified.

## Build tools (not distributed)

The GitHub Actions workflows use the BlocksDS Docker image to build `reelds.nds`; they are
not part of what ReelDS distributes.
