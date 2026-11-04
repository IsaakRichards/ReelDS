// ReelDS -- a tiny Netflix-style video player for DSi homebrew.
// All transcoding, thumbnailing and text rendering happen on the PC (pc/reelds.py);
// this program only reads, copies and blits.
#include <fat.h>
#include <stdarg.h>
#include <stdlib.h>

#include "browser.h"
#include "catalog.h"
#include "common.h"
#include "player.h"
#include "resume.h"
#include "settings.h"

char g_root[48];
volatile u32 g_vbl_count;

static void on_vblank(void)
{
    g_vbl_count++;
    player_vblank();
}

void fatal(const char *fmt, ...)
{
    setBrightness(3, 0);
    videoSetMode(MODE_0_2D);
    consoleDemoInit();
    printf("\x1b[2J\n  ReelDS\n\n");
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    printf("\n\n  Press START to exit.");
    while (1) {
        swiWaitForVBlank();
        scanKeys();
        if (keysDown() & KEY_START)
            exit(1);
    }
}

static bool find_root(void)
{
    static const char *roots[] = { "sd:/reelds", "fat:/reelds", "/reelds" };
    for (unsigned i = 0; i < sizeof roots / sizeof roots[0]; i++) {
        char p[64];
        snprintf(p, sizeof p, "%s/catalog.bin", roots[i]);
        FILE *f = fopen(p, "rb");
        if (f) {
            fclose(f);
            snprintf(g_root, sizeof g_root, "%s", roots[i]);
            return true;
        }
    }
    return false;
}

int main(void)
{
    if (isDSiMode())
        setCpuClock(true);      // 134 MHz
    irqSet(IRQ_VBLANK, on_vblank);
    irqEnable(IRQ_VBLANK);
    lcdMainOnTop();
    soundEnable();
    keysSetRepeat(18, 5);

    if (!fatInitDefault())
        fatal("Could not mount the SD card.\n");
    if (!find_root())
        fatal("No reelds/catalog.bin found.\n\n"
              "Run on your PC:\n"
              " python reelds.py build\n"
              "   <videos> --out <SD root>\n");
    char p[64];
    snprintf(p, sizeof p, "%s/catalog.bin", g_root);
    if (!catalog_open(p))
        fatal("catalog.bin is damaged or from\nanother version. Rebuild it.\n");

    resume_load();
    settings_load();
    browser_run();
    return 0;
}
