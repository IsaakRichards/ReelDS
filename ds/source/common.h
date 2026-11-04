#ifndef REELDS_COMMON_H
#define REELDS_COMMON_H

#include <nds.h>
#include <stdbool.h>
#include <stdio.h>

// Layout -- must match the PC builder (pc/reelds.py)
#define SCREEN_W   256
#define SCREEN_H   192
#define TILE_W     72
#define TILE_H     40
#define HEADER_H   20
#define LABEL_H    13
#define ROW_H      57
#define TILE_STEP  78
#define LEFT_PAD   8
#define DIGIT_W    6
#define VISIBLE_ROWS 3

#define COL_BG          (RGB15(2, 2, 2) | BIT(15))
#define COL_WHITE       (RGB15(31, 31, 31) | BIT(15))
#define COL_RED         (RGB15(28, 1, 2) | BIT(15))
#define COL_TRACK       (RGB15(9, 9, 9) | BIT(15))
#define COL_PLACEHOLDER (RGB15(6, 6, 6) | BIT(15))

#define ALIGN32 __attribute__((aligned(32)))

extern char g_root[48];             // e.g. "sd:/reelds"
extern volatile u32 g_vbl_count;    // incremented every vblank

void fatal(const char *fmt, ...) __attribute__((noreturn));

#endif
