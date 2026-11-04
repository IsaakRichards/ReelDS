#ifndef REELDS_GFX_H
#define REELDS_GFX_H

#include "catalog.h"

// 256x192 BGR555 staging buffers in main RAM
extern u16 g_bb[SCREEN_W * SCREEN_H];
extern u16 g_hero[SCREEN_W * SCREEN_H];

// All drawing clips to x in [0,256) and y in [cy0,cy1). Safe to use on VRAM (16-bit writes only).
void gfx_fill(u16 *fb, int x, int y, int w, int h, u16 c, int cy0, int cy1);
void gfx_frame(u16 *fb, int x, int y, int w, int h, int t, u16 c, int cy0, int cy1);
void gfx_blit(u16 *fb, int x, int y, const Img *img, int cy0, int cy1);
int  gfx_digits(u16 *fb, int x, int y, const char *s);
int  gfx_digits_width(const char *s);
void gfx_present(u16 *vram, const u16 *src);   // flush + DMA a full 256x192 screen

void gfx_init_browser_modes(void);

#endif
