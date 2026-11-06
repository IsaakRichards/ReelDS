#ifndef REELDS_LAUNCH_H
#define REELDS_LAUNCH_H

#include "common.h"

// Path ReelDS was started from (argv[0]), if the launcher passed one.
extern char g_self_path[128];

// Starts catalog item `item` (a DS game or homebrew). On success this never returns;
// on failure it returns a short, human readable reason.
const char *launch_game(int item);

#endif
