#include <string.h>

#include "gfx.h"

u16 g_bb[SCREEN_W * SCREEN_H] ALIGN32;
u16 g_hero[SCREEN_W * SCREEN_H] ALIGN32;

void gfx_fill(u16 *fb, int x, int y, int w, int h, u16 c, int cy0, int cy1)
{
    int x0 = x < 0 ? 0 : x;
    int x1 = x + w > SCREEN_W ? SCREEN_W : x + w;
    int y0 = y < cy0 ? cy0 : y;
    int y1 = y + h > cy1 ? cy1 : y + h;
    for (int yy = y0; yy < y1; yy++) {
        u16 *p = fb + yy * SCREEN_W;
        for (int xx = x0; xx < x1; xx++)
            p[xx] = c;
    }
}

void gfx_frame(u16 *fb, int x, int y, int w, int h, int t, u16 c, int cy0, int cy1)
{
    gfx_fill(fb, x, y, w, t, c, cy0, cy1);
    gfx_fill(fb, x, y + h - t, w, t, c, cy0, cy1);
    gfx_fill(fb, x, y + t, t, h - 2 * t, c, cy0, cy1);
    gfx_fill(fb, x + w - t, y + t, t, h - 2 * t, c, cy0, cy1);
}

static void blit_rect(u16 *fb, int x, int y, const u16 *src, int stride, int w, int h,
                      int cy0, int cy1)
{
    int sx = 0, sy = 0;
    if (x < 0) { sx = -x; w += x; x = 0; }
    if (y < cy0) { sy = cy0 - y; h -= sy; y = cy0; }
    if (x + w > SCREEN_W) w = SCREEN_W - x;
    if (y + h > cy1) h = cy1 - y;
    if (w <= 0 || h <= 0)
        return;
    for (int r = 0; r < h; r++) {
        const u16 *s = src + (sy + r) * stride + sx;
        u16 *d = fb + (y + r) * SCREEN_W + x;
        for (int i = 0; i < w; i++)   // halfword copies: VRAM rejects byte writes
            d[i] = s[i];
    }
}

void gfx_blit(u16 *fb, int x, int y, const Img *img, int cy0, int cy1)
{
    if (img && img->px)
        blit_rect(fb, x, y, img->px, img->w, img->w, img->h, cy0, cy1);
}

static int glyph(char c)
{
    if (c >= '0' && c <= '9')
        return c - '0';
    return c == ':' ? 10 : -1;
}

int gfx_digits_width(const char *s)
{
    return (int)strlen(s) * DIGIT_W;
}

int gfx_digits(u16 *fb, int x, int y, const char *s)
{
    const Img *d = &cat.digits;
    for (; *s; s++, x += DIGIT_W) {
        int g = glyph(*s);
        if (g >= 0 && d->px)
            blit_rect(fb, x, y, d->px + g * DIGIT_W, d->w, DIGIT_W, d->h, 0, SCREEN_H);
    }
    return x;
}

void gfx_present(u16 *vram, const u16 *src)
{
    DC_FlushRange(src, SCREEN_W * SCREEN_H * 2);
    dmaCopyWords(3, src, vram, SCREEN_W * SCREEN_H * 2);
}

void gfx_init_browser_modes(void)
{
    videoSetMode(MODE_5_2D);
    vramSetBankA(VRAM_A_MAIN_BG);
    bgInit(3, BgType_Bmp16, BgSize_B16_256x256, 0, 0);

    videoSetModeSub(MODE_5_2D);
    vramSetBankC(VRAM_C_SUB_BG);
    bgInitSub(3, BgType_Bmp16, BgSize_B16_256x256, 0, 0);
    bgUpdate();
}
