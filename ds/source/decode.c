// The whole "codec" on the DSi side: a byte-oriented run list of 4x4 blocks.
//
//   op = (mode << 6) | (count - 1), followed by count payloads:
//     mode 0  SKIP   keep previous pixels                (0 bytes / block)
//     mode 1  FILL   one palette index                   (1 byte)
//     mode 2  RAW    16 palette indices, row-major       (16 bytes)
//     mode 3  2-COL  c0, c1, u16 mask (bit = row*4+col)  (4 bytes)
//
// Blocks are visited in raster order. Running out of ops early means "skip the rest".
// Every store is an aligned 32-bit word, and the hot loop lives in ITCM on the DS.
#include "decode.h"

#if defined(__NDS__) && defined(ARM9)
#include <nds/ndstypes.h>
#define HOT ITCM_CODE
#else
#define HOT
#endif

#define STRIDE_W 64 // 256 bytes per row / 4 bytes per word

static const uint32_t nib_mask[16] = {
    0x00000000, 0x000000FF, 0x0000FF00, 0x0000FFFF,
    0x00FF0000, 0x00FF00FF, 0x00FFFF00, 0x00FFFFFF,
    0xFF000000, 0xFF0000FF, 0xFF00FF00, 0xFF00FFFF,
    0xFFFF0000, 0xFFFF00FF, 0xFFFFFF00, 0xFFFFFFFF,
};

static inline uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

HOT static int decode_ops(const uint8_t *src, const uint8_t *end, uint32_t *fb, int total)
{
    int b = 0;
    int bx = 0;
    uint32_t *blk = fb;

    while (b < total && src < end) {
        unsigned op = *src++;
        int n = (int)(op & 63) + 1;
        unsigned mode = op >> 6;
        static const uint8_t pay[4] = { 0, 1, 16, 4 };

        if (b + n > total || (end - src) < n * pay[mode])
            return -1;
        b += n;

        switch (mode) {
        case 0: // SKIP
            while (n--) {
                blk++;
                if (++bx == 64) { bx = 0; blk += STRIDE_W * 3; }
            }
            break;

        case 1: // FILL
            while (n--) {
                uint32_t c = *src++ * 0x01010101u;
                blk[0] = c;
                blk[STRIDE_W] = c;
                blk[STRIDE_W * 2] = c;
                blk[STRIDE_W * 3] = c;
                blk++;
                if (++bx == 64) { bx = 0; blk += STRIDE_W * 3; }
            }
            break;

        case 2: // RAW
            while (n--) {
                blk[0] = rd32(src);
                blk[STRIDE_W] = rd32(src + 4);
                blk[STRIDE_W * 2] = rd32(src + 8);
                blk[STRIDE_W * 3] = rd32(src + 12);
                src += 16;
                blk++;
                if (++bx == 64) { bx = 0; blk += STRIDE_W * 3; }
            }
            break;

        default: // 2-COLOUR
            while (n--) {
                uint32_t c0 = src[0] * 0x01010101u;
                uint32_t c1 = src[1] * 0x01010101u;
                unsigned m = src[2] | (src[3] << 8);
                uint32_t k;
                src += 4;
                k = nib_mask[m & 15];         blk[0] = (c0 & ~k) | (c1 & k);
                k = nib_mask[(m >> 4) & 15];  blk[STRIDE_W] = (c0 & ~k) | (c1 & k);
                k = nib_mask[(m >> 8) & 15];  blk[STRIDE_W * 2] = (c0 & ~k) | (c1 & k);
                k = nib_mask[m >> 12];        blk[STRIDE_W * 3] = (c0 & ~k) | (c1 & k);
                blk++;
                if (++bx == 64) { bx = 0; blk += STRIDE_W * 3; }
            }
            break;
        }
    }
    return 0;
}

int rdv_decode_frame(const uint8_t *data, uint32_t size, uint8_t *fb, int height,
                     const uint16_t **pal)
{
    const uint8_t *p = data + 4;
    *pal = 0;
    if (size < 4)
        return -1;
    if (data[0] & RDV_FRAME_PAL) {
        if (size < 4 + 512)
            return -1;
        *pal = (const uint16_t *)p;
        p += 512;
    }
    return decode_ops(p, data + size, (uint32_t *)fb, 64 * (height / 4));
}
