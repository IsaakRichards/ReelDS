# ReelDS -- build with BlocksDS (https://blocksds.skylyrac.net)
#   make            -> reelds.nds
#
# Uses BlocksDS's stock ARM9-only makefile (the default ARM7 core handles sound,
# touch, power and sleep, which is all we need).

BLOCKSDS        ?= /opt/blocksds/core

NAME            := reelds
GAME_TITLE      := ReelDS
GAME_SUBTITLE   := Pocket video player
GAME_AUTHOR     := ReelDS
GAME_ICON       := icon.bmp

SOURCEDIRS      := source
INCLUDEDIRS     := source
GFXDIRS         :=
BINDIRS         :=
AUDIODIRS       :=
NITROFSDIR      :=

include $(BLOCKSDS)/sys/default_makefiles/rom_arm9/Makefile
