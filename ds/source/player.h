#ifndef REELDS_PLAYER_H
#define REELDS_PLAYER_H

#include "common.h"

// Plays catalog item `item` starting near `start_ms`.
// Returns the position (ms) to resume from, or 0 if the title was (nearly) finished.
// *finished is true only when playback reached the end on its own (autoplay may continue).
u32 player_run(int item, u32 start_ms, bool *finished);

// Called from the vblank interrupt.
void player_vblank(void);

#endif
