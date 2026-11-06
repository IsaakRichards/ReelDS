// Starting games from ReelDS.
//
//  * Homebrew is booted directly with the standard NDS bootloader (devkitPro's
//    nds-bootloader, the one the Homebrew Menu and TWiLight Menu++ use).
//  * Retail games are handed to nds-bootstrap (installed with TWiLight Menu++): ReelDS
//    writes sd:/_nds/nds-bootstrap.ini with the game, its save file and QUIT_PATH = ReelDS,
//    then boots nds-bootstrap the same way. Quitting from its in-game menu (L + Down +
//    SELECT) comes back here.
//
// How the hand-over works: the bootloader is ARM7 code that loads an .nds by its FAT
// starting cluster. ReelDS installs it as the "bootstub" (Homebrew Menu's bootstub.s with
// the loader appended) and calls exit(0). libnds' exit path then runs the bootstub, which
// copies the loader to VRAM C, resets the ARM7 into it and parks the ARM9 until the new
// program starts. Both binaries are GPL-2.0+ (see third_party/).
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "bootstub_bin.h"
#include "load_bin.h"

#include "catalog.h"
#include "launch.h"
#include "settings.h"

char g_self_path[128];

typedef struct {
    u32 entrypoint;
    u32 storedFileCluster;
    u32 wantToPatchDldi;
    u32 argStart;
    u32 argSize;
    u32 dldiOffset;
    u32 hasTwlSd;
    u32 isTwlMode;
} BootLdrHeader;

#define BOOTSTUB_PHYS 0x02FF4000u
#define BOOTSTUB_MAX  (32 * 1024)       // room before the DSi system area at 0x02FFC000
#define INI_PATH      "sd:/_nds/nds-bootstrap.ini"
#define INI_MAX       (16 * 1024)

static bool exists(const char *p)
{
    struct stat st;
    return p && *p && stat(p, &st) == 0;
}

// ------------------------------------------------------------------------------------
// boot an .nds from the SD card (does not return on success)
// ------------------------------------------------------------------------------------
static const char *boot_nds(const char *path, int argc, const char **argv)
{
    struct stat st;
    if (stat(path, &st) < 0)
        return "File not found on the SD card";

    u8 *stub = (u8 *)__system_bootstub;
    BootLdrHeader *ldr = (BootLdrHeader *)(stub + bootstub_bin_size);

    // argv goes where the loader says (argStart = its end), two characters per halfword
    const BootLdrHeader *orig = (const BootLdrHeader *)load_bin;
    u32 arg_off = (orig->argStart + 3) & ~3u;
    if (arg_off < load_bin_size)
        arg_off = (load_bin_size + 3) & ~3u;
    u32 arg_chars = 0;
    for (int i = 0; i < argc; i++)
        arg_chars += strlen(argv[i]) + 1;
    u32 total = arg_off + ((arg_chars + 4) & ~3u);
    if (bootstub_bin_size + total > BOOTSTUB_MAX)
        return "Path too long";

    memcpy(stub, bootstub_bin, bootstub_bin_size);
    memcpy(ldr, load_bin, load_bin_size);
    ldr->storedFileCluster = (u32)st.st_ino;    // BlocksDS: st_ino = first FAT cluster
    ldr->wantToPatchDldi = 0;
    ldr->hasTwlSd = 1;
    ldr->isTwlMode = isDSiMode();

    u16 *dst = (u16 *)((u8 *)ldr + arg_off);
    u32 n = 0;
    u16 pending = 0;
    for (int i = 0; i < argc; i++) {
        for (const char *c = argv[i];; c++) {
            u8 ch = (u8)*c;
            if (n & 1)
                *dst++ = pending | (ch << 8);
            else
                pending = ch;
            n++;
            if (!ch)
                break;
        }
    }
    if (n & 1)
        *dst++ = pending;
    ldr->argStart = arg_off;
    ldr->argSize = arg_chars;

    struct __bootstub *bs = (struct __bootstub *)stub;
    bs->arm9reboot = (VoidFn)(uintptr_t)(BOOTSTUB_PHYS + (uintptr_t)bs->arm9reboot);
    bs->arm7reboot = (VoidFn)(uintptr_t)(BOOTSTUB_PHYS + (uintptr_t)bs->arm7reboot);
    bs->bootsize = total;

    soundDisable();
    DC_FlushAll();
    exit(0);                    // libnds hands over to the bootstub installed above
}

// ------------------------------------------------------------------------------------
// retail games: save file + nds-bootstrap.ini
// ------------------------------------------------------------------------------------
static void save_path_for(const char *rom, char *out, size_t len)
{
    const char *slash = strrchr(rom, '/');
    const char *name = slash ? slash + 1 : rom;
    const char *dot = strrchr(name, '.');
    int dir_len = (int)(name - rom);
    int name_len = dot ? (int)(dot - name) : (int)strlen(name);
    snprintf(out, len, "%.*ssaves/%.*s.sav", dir_len, rom, name_len, name);
}

// Same convention as TWiLight Menu++: <rom folder>/saves/<rom name>.sav, created (or grown)
// to the game's save size and filled with 0xFF like blank save memory.
static bool ensure_save(const char *sav, u32 size)
{
    struct stat st;
    u32 have = stat(sav, &st) == 0 ? (u32)st.st_size : 0;
    if (have >= size)
        return true;
    char dir[160];
    snprintf(dir, sizeof dir, "%s", sav);
    char *s = strrchr(dir, '/');
    if (s) {
        *s = 0;
        mkdir(dir, 0777);
    }
    FILE *f = fopen(sav, "ab");
    if (!f)
        return false;
    static u8 blank[4096];
    memset(blank, 0xFF, sizeof blank);
    while (have < size) {
        u32 n = size - have < sizeof blank ? size - have : sizeof blank;
        if (fwrite(blank, 1, n, f) != n) {
            fclose(f);
            return false;
        }
        have += n;
    }
    fclose(f);
    return true;
}

typedef struct {
    const char *key;
    char value[160];
} IniKey;

static bool is_our_key(const char *line, const IniKey *keys, int nkeys)
{
    while (*line == ' ' || *line == '\t')
        line++;
    for (int i = 0; i < nkeys; i++) {
        size_t n = strlen(keys[i].key);
        if (!strncmp(line, keys[i].key, n)) {
            const char *p = line + n;
            while (*p == ' ' || *p == '\t')
                p++;
            if (*p == '=')
                return true;
        }
    }
    return false;
}

// Updates only the keys ReelDS needs, keeping everything TWiLight Menu++ wrote.
static bool write_bootstrap_ini(const IniKey *keys, int nkeys)
{
    static char in[INI_MAX + 1];
    static char out[INI_MAX + 2048];
    size_t in_len = 0, o = 0;
    FILE *f = fopen(INI_PATH, "rb");
    if (f) {
        in_len = fread(in, 1, INI_MAX, f);
        fclose(f);
    }
    in[in_len] = 0;

    bool in_section = false, wrote = false;
    #define EMIT_KEYS()                                                                  \
        do {                                                                             \
            for (int k = 0; k < nkeys; k++)                                              \
                o += snprintf(out + o, sizeof out - o, "%s = \"%s\"\n", keys[k].key,     \
                              keys[k].value);                                            \
            wrote = true;                                                                \
        } while (0)

    char *line = in;
    while (*line && o < sizeof out - 512) {
        char *nl = strchr(line, '\n');
        size_t len = nl ? (size_t)(nl - line + 1) : strlen(line);
        if (line[0] == '[') {
            if (in_section && !wrote)
                EMIT_KEYS();
            in_section = !strncmp(line, "[NDS-BOOTSTRAP]", 15);
        }
        if (!(in_section && is_our_key(line, keys, nkeys))) {
            memcpy(out + o, line, len);
            o += len;
            if (!nl)
                out[o++] = '\n';
        }
        line += len;
    }
    if (!wrote) {
        if (!in_section)
            o += snprintf(out + o, sizeof out - o, "[NDS-BOOTSTRAP]\n");
        EMIT_KEYS();
    }
    #undef EMIT_KEYS

    f = fopen(INI_PATH, "wb");
    if (!f)
        return false;
    bool ok = fwrite(out, 1, o, f) == o;
    fclose(f);
    return ok;
}

static const char *quit_path(void)
{
    if (!strncmp(g_self_path, "sd:/", 4) && exists(g_self_path))
        return g_self_path;
    if (exists(g_settings.reelds_path))
        return g_settings.reelds_path;
    if (exists("sd:/reelds.nds"))
        return "sd:/reelds.nds";
    return "";
}

// ------------------------------------------------------------------------------------
const char *launch_game(int item)
{
    char rom[160];
    if (!catalog_item_path(item, rom, sizeof rom) || !exists(rom))
        return "This game isn't on the SD card any more.\nBuild the card again on your PC.";

    if (cat.items[item].kind == ITEM_HOMEBREW) {
        const char *argv[1] = { rom };
        return boot_nds(rom, 1, argv);
    }

    static const char *bootstraps[] = { "sd:/_nds/nds-bootstrap-release.nds",
                                        "sd:/_nds/nds-bootstrap-nightly.nds" };
    const char *bootstrap = NULL;
    for (unsigned i = 0; i < 2 && !bootstrap; i++)
        if (exists(bootstraps[i]))
            bootstrap = bootstraps[i];
    if (!bootstrap)
        return "nds-bootstrap wasn't found.\nInstall TWiLight Menu++ on this SD card,\n"
               "it includes nds-bootstrap.";

    char sav[176];
    save_path_for(rom, sav, sizeof sav);
    if (cat.items[item].save_size && !ensure_save(sav, cat.items[item].save_size))
        return "Couldn't create the save file.\nIs the SD card locked or full?";

    IniKey keys[] = {
        { "NDS_PATH", "" }, { "SAV_PATH", "" }, { "PRV_PATH", "" }, { "APP_PATH", "" },
        { "HOMEBREW_ARG", "" }, { "RAM_DRIVE_PATH", "" }, { "QUIT_PATH", "" },
        { "DSI_MODE", "1" }, { "BOOST_CPU", "0" }, { "BOOST_VRAM", "0" },
    };
    snprintf(keys[0].value, sizeof keys[0].value, "%s", rom);
    if (cat.items[item].save_size)
        snprintf(keys[1].value, sizeof keys[1].value, "%s", sav);
    snprintf(keys[6].value, sizeof keys[6].value, "%s", quit_path());
    if (!write_bootstrap_ini(keys, sizeof keys / sizeof keys[0]))
        return "Couldn't write nds-bootstrap.ini.\nIs the SD card locked or full?";

    const char *argv[1] = { bootstrap };
    return boot_nds(bootstrap, 1, argv);
}
