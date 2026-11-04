// Playback: audio drives the clock, video follows.
//
//  * Audio: raw mono s16 PCM streamed into a 64 KB looping ring buffer on one hardware
//    channel. ARM9 timers 0+1 run at exactly the channel's sample rate (both are derived
//    from the same bus clock divisor), so timer 1 counts samples played -> master clock.
//  * Video: each frame is decoded into a RAM framebuffer, DMA'd into the hidden half of
//    a double-buffered 8bpp bitmap, and the vblank IRQ flips the bitmap base (and swaps
//    the palette on keyframes) so nothing ever tears.
#include <stdlib.h>
#include <string.h>

#include "catalog.h"
#include "decode.h"
#include "gfx.h"
#include "player.h"
#include "settings.h"

#define RING_SAMPLES 32768             // 64 KB: 1 s at 32768 Hz
#define CHUNK        2048
#define READBUF_SIZE (64 * 1024)

#define BAR_X 12
#define BAR_Y 140
#define BAR_W 232
#define BAR_H 3
#define TIME_Y 147
#define OSD_FRAMES (5 * 60)            // bottom screen turns off after 5 s of no input
#define HINT_Y 167                     // controls hint strip on the now-playing panel
#define ESCAPE_FRAMES (4 * 60)         // buttons locked: hold START+SELECT this long to leave

static u8  framebuf[SCREEN_W * SCREEN_H] ALIGN32;
static u8  readbuf[READBUF_SIZE + 64] ALIGN32;
static s16 ring[RING_SAMPLES] ALIGN32;
static u16 pal_shadow[256] ALIGN32;

static volatile int player_active;
static volatile int flip_pending;
static volatile int pal_pending;
static volatile u32 flip_base;

typedef struct {
    FILE *vf, *af;
    RdvHeader h;
    RdvKeyframe *kf;
    u32 fb_bytes;
    int yoff;

    u32 next_frame;         // next frame to read+decode
    u32 next_size;          // size word of the frame at the file position
    int frame_ready;        // framebuf holds a decoded frame not yet shown/dropped
    u32 decoded_frame;
    const u16 *frame_pal;
    u32 shown_frame;
    int back;               // hidden bitmap (0/1)
    int force_present;

    // audio clock
    int snd;
    bool audio_on;
    bool a_eof;
    u32 base_sample;        // absolute sample at which the ring started
    u32 written;            // samples queued since base
    u32 t_hi;
    u16 t_last;
    u16 reload;
    u8 vol;
} Player;

static Player P;
static u8 saved_vol = 127;

// --------------------------------------------------------------------------------------
void player_vblank(void)
{
    if (!player_active || !flip_pending)
        return;
    REG_BG3CNT = (REG_BG3CNT & ~(31 << 8)) | (flip_base << 8);
    if (pal_pending) {
        u32 *d = (u32 *)BG_PALETTE;
        const u32 *s = (const u32 *)pal_shadow;
        for (int i = 0; i < 128; i++)
            d[i] = s[i];
        pal_pending = 0;
    }
    flip_pending = 0;
}

// --------------------------------------------------------------------------------------
// time conversions
static u32 sample_to_frame(u32 s)
{
    return (u32)(((u64)s * P.h.fps_num) / ((u64)P.h.audio_rate * P.h.fps_den));
}
static u32 frame_to_sample(u32 f)
{
    return (u32)(((u64)f * P.h.audio_rate * P.h.fps_den) / P.h.fps_num);
}
static u32 frame_to_ms(u32 f)
{
    return (u32)(((u64)f * 1000 * P.h.fps_den) / P.h.fps_num);
}
static u32 ms_to_frame(u32 ms)
{
    return (u32)(((u64)ms * P.h.fps_num) / ((u64)1000 * P.h.fps_den));
}

// --------------------------------------------------------------------------------------
// audio
static u32 played(void)
{
    u16 lo = TIMER_DATA(1);
    if (lo < P.t_last)
        P.t_hi += 0x10000;
    P.t_last = lo;
    return P.t_hi + lo;
}

static void fill_chunk(void)
{
    s16 *dst = ring + (P.written & (RING_SAMPLES - 1));
    size_t got = 0;
    if (!P.a_eof) {
        got = fread(dst, 2, CHUNK, P.af);
        if (got < CHUNK)
            P.a_eof = true;
    }
    if (got < CHUNK)
        memset(dst + got, 0, (CHUNK - got) * 2);
    DC_FlushRange(dst, CHUNK * 2);
    P.written += CHUNK;
}

static void audio_service(void)
{
    if (!P.audio_on)
        return;
    u32 p = played();
    for (int n = 0; n < 4 && P.written - p <= RING_SAMPLES - CHUNK; n++)
        fill_chunk();
}

static void audio_stop(void)
{
    if (!P.audio_on)
        return;
    soundKill(P.snd);
    TIMER_CR(0) = 0;
    TIMER_CR(1) = 0;
    P.audio_on = false;
}

static void audio_start(u32 sample)
{
    audio_stop();
    P.base_sample = sample;
    P.written = 0;
    P.t_hi = 0;
    P.t_last = 0;
    P.a_eof = sample >= P.h.audio_samples || fseek(P.af, (long)sample * 2, SEEK_SET) != 0;
    while (P.written < RING_SAMPLES)
        fill_chunk();

    TIMER_CR(0) = 0;
    TIMER_CR(1) = 0;
    TIMER_DATA(0) = P.reload;
    TIMER_DATA(1) = 0;
    TIMER_CR(1) = TIMER_ENABLE | TIMER_CASCADE;
    P.snd = soundPlaySample(ring, SoundFormat_16Bit, sizeof ring, (u16)P.h.audio_rate,
                            P.vol, 64, true, 0);
    TIMER_CR(0) = TIMER_ENABLE | TIMER_DIV_1;
    P.audio_on = true;
}

static u32 clock_sample(void)
{
    return P.audio_on ? P.base_sample + played() : P.base_sample;
}

// --------------------------------------------------------------------------------------
// video
static int read_and_decode(void)
{
    u32 size = P.next_size;
    if (size < 4 || size > READBUF_SIZE)
        return -1;
    // read this frame plus the next frame's size word in a single call
    size_t got = fread(readbuf, 1, size + 4, P.vf);
    if (got < size)
        return -1;
    if (got == size + 4)
        memcpy(&P.next_size, readbuf + size, 4);
    else
        P.next_size = 0;
    if (rdv_decode_frame(readbuf, size, framebuf, P.h.height, &P.frame_pal) != 0)
        return -1;
    P.decoded_frame = P.next_frame++;
    P.frame_ready = 1;
    return 0;
}

static void take_palette(void)
{
    if (P.frame_pal) {
        memcpy(pal_shadow, P.frame_pal, 512);
        pal_pending = 1;    // applied together with the next flip
    }
}

static void present(void)
{
    take_palette();
    u8 *dst = (u8 *)BG_GFX + P.back * 0x10000 + P.yoff * SCREEN_W;
    DC_FlushRange(framebuf, P.fb_bytes);
    dmaCopyWords(3, framebuf, dst, P.fb_bytes);
    flip_base = P.back * 4;   // bitmap base in 16 KB units
    flip_pending = 1;
    P.back ^= 1;
    P.shown_frame = P.decoded_frame;
}

static int kf_find(u32 frame)
{
    int lo = 0, hi = (int)P.h.kf_count - 1;
    while (lo < hi) {
        int mid = (lo + hi + 1) / 2;
        if (P.kf[mid].frame <= frame)
            lo = mid;
        else
            hi = mid - 1;
    }
    return lo;
}

static bool seek_to_kf(int idx, bool paused)
{
    audio_stop();
    if (fseek(P.vf, (long)P.kf[idx].offset, SEEK_SET) != 0 || fread(&P.next_size, 4, 1, P.vf) != 1)
        return false;
    P.next_frame = P.kf[idx].frame;
    P.frame_ready = 0;
    P.force_present = 1;
    P.base_sample = frame_to_sample(P.next_frame);
    if (!paused)
        audio_start(P.base_sample);
    return true;
}

static bool seek_rel(int secs, bool paused)
{
    s64 cur = P.shown_frame;
    s64 target = cur + (s64)secs * P.h.fps_num / P.h.fps_den;
    if (target < 0)
        target = 0;
    if (target >= (s64)P.h.frame_count)
        target = P.h.frame_count - 1;
    int idx = kf_find((u32)target);
    if (secs > 0 && P.kf[idx].frame <= cur) {
        if (idx + 1 >= (int)P.h.kf_count)
            return true;            // nothing further to jump to
        idx++;
    }
    return seek_to_kf(idx, paused);
}

// --------------------------------------------------------------------------------------
// bottom screen
static void fmt_time(u32 s, char *out)
{
    u32 h = s / 3600, m = (s / 60) % 60, sec = s % 60;
    if (h)
        sprintf(out, "%lu:%02lu:%02lu", (unsigned long)h, (unsigned long)m, (unsigned long)sec);
    else
        sprintf(out, "%lu:%02lu", (unsigned long)m, (unsigned long)sec);
}

static void draw_osd(u32 frame, bool paused, bool show_vol)
{
    u16 *v = BG_GFX_SUB;
    u32 total = P.h.frame_count ? P.h.frame_count : 1;
    int fill = (int)((u64)frame * BAR_W / total);
    gfx_fill(v, BAR_X, BAR_Y, fill, BAR_H, COL_RED, 0, SCREEN_H);
    gfx_fill(v, BAR_X + fill, BAR_Y, BAR_W - fill, BAR_H, COL_TRACK, 0, SCREEN_H);
    gfx_fill(v, BAR_X, TIME_Y, BAR_W, 12, COL_BG, 0, SCREEN_H);

    char a[16], b[16];
    fmt_time(frame_to_ms(frame) / 1000, a);
    fmt_time(frame_to_ms(P.h.frame_count) / 1000, b);
    gfx_digits(v, BAR_X, TIME_Y, a);
    gfx_digits(v, BAR_X + BAR_W - gfx_digits_width(b), TIME_Y, b);

    int cx = BAR_X + BAR_W / 2;
    if (show_vol) {                     // volume meter
        int w = P.vol * 48 / 127;
        gfx_fill(v, cx - 24, TIME_Y + 4, 48, 3, COL_TRACK, 0, SCREEN_H);
        gfx_fill(v, cx - 24, TIME_Y + 4, w, 3, COL_WHITE, 0, SCREEN_H);
    } else if (paused) {                // pause glyph
        gfx_fill(v, cx - 4, TIME_Y + 1, 3, 9, COL_WHITE, 0, SCREEN_H);
        gfx_fill(v, cx + 1, TIME_Y + 1, 3, 9, COL_WHITE, 0, SCREEN_H);
    }
}

// --------------------------------------------------------------------------------------
static bool open_files(int item)
{
    char path[80];
    const CatItem *it = &cat.items[item];
    snprintf(path, sizeof path, "%s/videos/%s.rdv", g_root, it->id);
    P.vf = fopen(path, "rb");
    snprintf(path, sizeof path, "%s/videos/%s.rda", g_root, it->id);
    P.af = fopen(path, "rb");
    if (!P.vf || !P.af)
        return false;
    setvbuf(P.vf, NULL, _IOFBF, 32 * 1024);
    setvbuf(P.af, NULL, _IOFBF, 8 * 1024);

    RdvHeader *h = &P.h;
    if (fread(h, sizeof *h, 1, P.vf) != 1 || memcmp(h->magic, "RDV1", 4) != 0 || h->width != 256
        || h->height == 0 || h->height > 192 || (h->height & 3) || !h->fps_num || !h->fps_den
        || !h->frame_count || !h->kf_count
        || (h->audio_rate != 16384 && h->audio_rate != 32768))
        return false;

    P.kf = malloc(h->kf_count * sizeof(RdvKeyframe));
    if (!P.kf || fseek(P.vf, (long)h->kf_offset, SEEK_SET) != 0
        || fread(P.kf, sizeof(RdvKeyframe), h->kf_count, P.vf) != h->kf_count)
        return false;
    return true;
}

static void close_files(void)
{
    if (P.vf) fclose(P.vf);
    if (P.af) fclose(P.af);
    free(P.kf);
    P.vf = P.af = NULL;
    P.kf = NULL;
}

static void video_setup(int item)
{
    // top: double-buffered 8bpp bitmap (two 64 KB halves of VRAM bank A)
    videoSetMode(MODE_5_2D);
    vramSetBankA(VRAM_A_MAIN_BG);
    bgInit(3, BgType_Bmp8, BgSize_B8_256x256, 0, 0);
    bgUpdate();
    dmaFillWords(0, BG_GFX, 0x20000);
    dmaFillWords(0, BG_PALETTE, 512);   // index 0 is always black (letterbox)

    // bottom: the pre-rendered "now playing" panel
    int w, h;
    if (img_load_into(cat.items[item].panel_off, g_bb, SCREEN_W * SCREEN_H, &w, &h)
        && w == SCREEN_W && h == SCREEN_H) {
        // the panel shows every control; swap in the hint that matches the lock settings
        if (g_settings.lock_buttons)
            gfx_blit(g_bb, 0, HINT_Y, &cat.hint_locked, 0, SCREEN_H);
        else if (g_settings.lock_touch)
            gfx_blit(g_bb, 0, HINT_Y, &cat.hint_notouch, 0, SCREEN_H);
        gfx_present(BG_GFX_SUB, g_bb);
    } else
        dmaFillHalfWords(COL_BG, BG_GFX_SUB, SCREEN_W * SCREEN_H * 2);
}

u32 player_run(int item, u32 start_ms, bool *finished_out)
{
    *finished_out = false;
    memset(&P, 0, sizeof P);
    P.vol = saved_vol;
    u32 result = start_ms;

    if (!open_files(item)) {
        close_files();
        setBrightness(3, 0);
        return start_ms;
    }
    P.fb_bytes = SCREEN_W * P.h.height;
    P.yoff = (SCREEN_H - P.h.height) / 2;
    P.reload = (u16)(-(int)(2 * (0x1000000 / P.h.audio_rate)));
    P.back = 1;

    video_setup(item);
    setBrightness(3, 0);        // the browser faded us in from black
    flip_pending = pal_pending = 0;
    player_active = 1;

    u32 start_frame = ms_to_frame(start_ms);
    if (start_frame >= P.h.frame_count)
        start_frame = 0;
    bool paused = false;
    bool screen_on = true;
    bool finished = false;
    u32 osd_deadline = g_vbl_count + OSD_FRAMES;
    u32 vol_until = 0;
    u32 last_osd_sec = 0xFFFFFFFF;
    u32 escape_since = 0;
    bool escape_armed = false;

    if (!seek_to_kf(kf_find(start_frame), false))
        goto out;
    P.shown_frame = P.next_frame;

    while (1) {
        scanKeys();
        u32 down = keysDown();
        u32 rep = keysDownRepeat();
        bool redraw = false;

        if (g_settings.lock_touch) {
            down &= ~KEY_TOUCH;
            rep &= ~KEY_TOUCH;
        }
        if (g_settings.lock_buttons) {
            // toddler mode: A is play/pause, nothing else. Grown-ups hold START+SELECT to leave.
            if ((keysHeld() & (KEY_START | KEY_SELECT)) == (KEY_START | KEY_SELECT)) {
                if (!escape_armed) {
                    escape_armed = true;
                    escape_since = g_vbl_count;
                } else if (g_vbl_count - escape_since >= ESCAPE_FRAMES) {
                    break;
                }
            } else {
                escape_armed = false;
            }
            down &= KEY_A | KEY_TOUCH;
            rep = 0;
        }

        if (down) {
            if (!screen_on && (down & ~(KEY_TOUCH | KEY_SELECT))) {
                powerOn(PM_BACKLIGHT_BOTTOM);
                screen_on = true;
            }
            osd_deadline = g_vbl_count + OSD_FRAMES;
        }
        if (down & (KEY_TOUCH | KEY_SELECT)) {
            screen_on = !screen_on;
            if (screen_on)
                powerOn(PM_BACKLIGHT_BOTTOM);
            else
                powerOff(PM_BACKLIGHT_BOTTOM);
        }
        if (down & KEY_B)
            break;
        if (down & (KEY_A | KEY_START)) {
            if (!paused) {
                P.base_sample = clock_sample();
                audio_stop();
                paused = true;
            } else {
                paused = false;
                audio_start(P.base_sample);
            }
            redraw = true;
        }
        if (rep & (KEY_UP | KEY_DOWN)) {
            int v = P.vol + ((rep & KEY_UP) ? 8 : -8);
            P.vol = v < 0 ? 0 : v > 127 ? 127 : v;
            saved_vol = P.vol;
            if (P.audio_on)
                soundSetVolume(P.snd, P.vol);
            vol_until = g_vbl_count + 90;
            redraw = true;
        }
        int jump = (rep & KEY_RIGHT) ? 10 : (rep & KEY_LEFT) ? -10
                 : (rep & KEY_R) ? 60 : (rep & KEY_L) ? -60 : 0;
        if (jump) {
            if (!seek_rel(jump, paused))
                break;
            redraw = true;
        }

        // ---- the actual playback engine ----
        audio_service();
        bool worked = false;
        u32 now = paused ? P.shown_frame : sample_to_frame(clock_sample());

        if (!P.frame_ready && P.next_frame < P.h.frame_count && (!paused || P.force_present)) {
            if (read_and_decode() != 0)
                break;              // corrupt / unreadable file: bail out to the menu
            worked = true;
        }
        if (P.frame_ready && !flip_pending && (P.force_present || P.decoded_frame <= now)) {
            bool late = !P.force_present && P.decoded_frame + 1 < now
                        && P.next_frame < P.h.frame_count;
            if (late)
                take_palette();     // drop it, but keep a keyframe's palette
            else
                present();
            P.frame_ready = 0;
            P.force_present = 0;
            worked = true;
        }
        if (!paused && P.next_frame >= P.h.frame_count && !P.frame_ready && !flip_pending
            && now + 1 >= P.h.frame_count) {
            finished = true;
            break;
        }

        // ---- bottom screen ----
        if (screen_on && !paused && (s32)(g_vbl_count - osd_deadline) > 0) {
            powerOff(PM_BACKLIGHT_BOTTOM);
            screen_on = false;
        }
        u32 sec = frame_to_ms(P.shown_frame) / 1000;
        bool show_vol = (s32)(vol_until - g_vbl_count) > 0;
        if (screen_on && (redraw || sec != last_osd_sec || (vol_until && !show_vol))) {
            if (vol_until && !show_vol)
                vol_until = 0;
            draw_osd(P.shown_frame, paused, show_vol);
            last_osd_sec = sec;
        }

        if (!worked)
            swiWaitForVBlank();
    }

out:
    audio_stop();
    while (flip_pending)
        swiWaitForVBlank();
    player_active = 0;
    powerOn(PM_BACKLIGHT_BOTTOM);

    if (finished) {
        result = 0;
        *finished_out = true;
    } else if (P.h.frame_count) {
        result = frame_to_ms(P.shown_frame);
        // close enough to the end (credits) counts as watched
        if ((u64)P.shown_frame * 100 >= (u64)P.h.frame_count * 97)
            result = 0;
        else if (result < 1000)
            result = 0;
    }
    close_files();
    return result;
}
