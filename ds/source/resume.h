#ifndef REELDS_RESUME_H
#define REELDS_RESUME_H

#include "common.h"

// Remembers where you stopped each title (keyed by its 8-hex-digit id), most recent first.
void resume_load(void);
u32  resume_get(u32 key);              // ms, 0 = not started
void resume_set(u32 key, u32 ms);      // ms == 0 forgets the title; saves to SD
int  resume_recent(u32 *keys, int max);

#endif
