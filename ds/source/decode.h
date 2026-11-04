// ReelDS .rdv frame decoder. Pure C, no libnds dependency, so it also builds on the PC
// for testing (see test/decode_test.c).
#ifndef REELDS_DECODE_H
#define REELDS_DECODE_H

#include <stdint.h>

#define RDV_FRAME_KEY 1
#define RDV_FRAME_PAL 2

typedef struct {
    char     magic[4];      // "RDV1"
    uint16_t version;
    uint16_t width;         // always 256
    uint16_t height;        // multiple of 4, <= 192
    uint16_t flags;
    uint32_t fps_num;
    uint32_t fps_den;
    uint32_t frame_count;
    uint32_t audio_rate;    // 16384 or 32768
    uint32_t audio_samples; // mono s16le samples in the matching .rda
    uint32_t kf_count;
    uint32_t kf_offset;     // table of {u32 frame, u32 file offset}
    uint32_t reserved[6];
} RdvHeader;               // 64 bytes

typedef struct {
    uint32_t frame;
    uint32_t offset;
} RdvKeyframe;

// Decodes one frame payload into an 8bpp framebuffer that is 256 pixels wide.
// The framebuffer must hold the previous frame (blocks may be "skipped").
// If the frame carries a palette, *pal is set to it (256 BGR555 values, 4-byte aligned
// as long as `data` is); otherwise *pal is set to NULL.
// Returns 0 on success, -1 if the data is corrupt.
int rdv_decode_frame(const uint8_t *data, uint32_t size, uint8_t *fb, int height,
                     const uint16_t **pal);

#endif
