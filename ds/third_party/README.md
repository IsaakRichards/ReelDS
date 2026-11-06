# Third-party code used to start games

ReelDS boots games and homebrew with the same two pieces the NDS Homebrew Menu and
TWiLight Menu++ use. Both are **GPL-2.0-or-later** (see `COPYING.GPL-2.0`); ReelDS as a whole is
GPL-3.0-or-later (see the repository's `LICENSE` and `THIRD_PARTY_NOTICES.md`).

| file | what | source |
|---|---|---|
| `bootstub/bootstub.s` | Hands the CPUs over to the loader when ReelDS exits | [devkitPro/nds-hb-menu](https://github.com/devkitPro/nds-hb-menu/tree/3c1f64c12da157fdd14a2d594db139f50e283261/bootstub) by Dave "WinterMute" Murphy |
| `../data/load.bin` (built in CI) | ARM7 bootloader that loads an `.nds` from the SD card | [devkitPro/nds-bootloader](https://github.com/devkitPro/nds-bootloader/tree/a0f819008a63f28c4994e7cc8a8ace892450bbb5) by Michael "Chishm" Chisholm et al. |

`data/load.bin` and `data/bootstub.bin` are built by the `loader` job in
`.github/workflows/build.yml` (devkitARM container, pinned commit above). To build ReelDS
locally, either download them from a CI run's `loader-bins` artifact into `ds/data/`, or
build them with devkitARM:

```
git clone https://github.com/devkitPro/nds-bootloader && cd nds-bootloader
git checkout a0f819008a63f28c4994e7cc8a8ace892450bbb5 && make LOADBIN=$PWD/../ReelDS/ds/data/load.bin
cd ../ReelDS/ds/third_party/bootstub
arm-none-eabi-gcc -Wl,-Ttext=0 -x assembler-with-cpp -nostartfiles -nostdlib bootstub.s -o bootstub.elf
arm-none-eabi-objcopy -O binary bootstub.elf ../../data/bootstub.bin
```

`pc/savesizes.json` (save sizes by game code) is generated from the ROM list in
TWiLight Menu++ (`universal/include/ROMList.h`, originally from melonDS, **GPL-3.0**).
