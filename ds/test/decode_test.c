// Host-side check that source/decode.c decodes .rdv files exactly like the encoder expects.
//   cc -O2 -I../source decode_test.c ../source/decode.c -o decode_test
//   ./decode_test video.rdv out.rgb      (raw 256xH rgb24 frames, H from header)
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "decode.h"

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s in.rdv out.rgb\n", argv[0]);
        return 2;
    }
    FILE *f = fopen(argv[1], "rb");
    FILE *o = fopen(argv[2], "wb");
    if (!f || !o) { perror("open"); return 1; }

    RdvHeader h;
    if (fread(&h, sizeof h, 1, f) != 1 || memcmp(h.magic, "RDV1", 4)) {
        fprintf(stderr, "bad header\n");
        return 1;
    }
    static uint8_t fb[256 * 192] __attribute__((aligned(4)));
    static uint8_t buf[256 * 1024] __attribute__((aligned(4)));
    static uint8_t rgb[256 * 192 * 3];
    uint16_t pal[256] = {0};

    for (uint32_t i = 0; i < h.frame_count; i++) {
        uint32_t size;
        if (fread(&size, 4, 1, f) != 1 || size > sizeof buf || fread(buf, 1, size, f) != size) {
            fprintf(stderr, "short read at frame %u\n", i);
            return 1;
        }
        const uint16_t *p;
        if (rdv_decode_frame(buf, size, fb, h.height, &p)) {
            fprintf(stderr, "corrupt frame %u\n", i);
            return 1;
        }
        if (p)
            memcpy(pal, p, 512);
        for (int k = 0; k < 256 * h.height; k++) {
            uint16_t c = pal[fb[k]];
            int r = c & 31, g = (c >> 5) & 31, b = (c >> 10) & 31;
            rgb[k * 3] = (r << 3) | (r >> 2);
            rgb[k * 3 + 1] = (g << 3) | (g >> 2);
            rgb[k * 3 + 2] = (b << 3) | (b >> 2);
        }
        fwrite(rgb, 3, 256 * h.height, o);
    }
    printf("decoded %u frames (256x%u)\n", h.frame_count, h.height);
    return 0;
}
