#ifndef REELDS_SETTINGS_H
#define REELDS_SETTINGS_H

#include "common.h"

// reelds/settings.ini on the SD card, written by the PC app. Read once at startup.
typedef struct {
    bool autoplay;          // play the next episode when one ends
    bool lock_buttons;      // during playback only A works; hold START+SELECT 4 s to leave
    bool lock_touch;        // during playback the touchscreen is ignored
    bool shuffle_tile;      // show the Shuffle tile (random videos / random episodes of a show)
    bool show_games;        // show the Games row(s)
    char reelds_path[96];   // where reelds.nds is (a game's Quit comes back to it)
} Settings;

extern Settings g_settings;

void settings_load(void);

#endif
