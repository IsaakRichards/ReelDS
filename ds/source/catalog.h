#ifndef REELDS_CATALOG_H
#define REELDS_CATALOG_H

#include "common.h"

// On-disk records (little endian, written by pc/reelds.py, catalog version 4)
#define CAT_VERSION 4
#define ENTRY_PAGE  0x8000      // entry flag: opens page (entry & 0x7FFF) instead of playing
#define HOME_PAGE   0

typedef struct {
    char magic[4];              // "RCAT"
    u16 version, n_pages, n_rows, n_items;
    u32 off_pages, off_rows, off_entries, off_items;
    u32 off_digits, off_cw_label;
    u32 n_entries;
    u32 off_hint_notouch;       // now-playing control hints when the touchscreen is locked
    u32 off_hint_locked;        // ... when only A works
    u32 off_shuffle_tile;       // the Shuffle tile and its top-screen image
    u32 off_shuffle_hero;
    u32 off_shuffle_label;
} CatHeader;                    // 60 bytes

typedef struct {
    u32 header_off;             // bottom-screen header strip for this page
    u32 tile_off, hero_off;     // how a show looks from the home page (0 for home)
    u16 first_row, n_rows;
    u16 n_episodes, reserved;
} CatPage;                      // 20 bytes

typedef struct {
    u32 label_off;
    u16 first, count;           // range in the entry table
} CatRow;                       // 8 bytes

#define ITEM_VIDEO    0
#define ITEM_RETAIL   1         // DS game, started through nds-bootstrap
#define ITEM_HOMEBREW 2         // homebrew .nds, booted directly

typedef struct {
    char id[12];                // 8 hex chars, NUL padded -> videos/<id>.rdv / .rda
    u32 thumb_off, hero_off, panel_off;
    u32 frames, fps_num, fps_den, duration;   // duration in seconds
    u32 path_off;               // games: NUL-terminated "sd:/..." path of the .nds
    u32 kind;                   // ITEM_*
    u32 save_size;              // retail games: save file size in bytes
} CatItem;                      // 52 bytes

typedef struct {
    u16 w, h;
    u16 *px;                    // BGR555 with bit 15 set; NULL if not loaded
} Img;

typedef struct {
    FILE *f;
    CatHeader h;
    CatPage *pages;
    CatRow *rows;
    u16 *entries;
    CatItem *items;
    Img digits, cw_label, hint_notouch, hint_locked, shuffle_tile, shuffle_label;
    Img *page_headers;          // lazily loaded, kept
    Img *labels;                // lazily loaded, kept
    Img *thumbs;                // lazily loaded, evicted when too many
    Img *page_tiles;
    int tiles_loaded;
} Catalog;

extern Catalog cat;

bool catalog_open(const char *path);
bool img_load(u32 off, Img *out);
bool img_load_into(u32 off, u16 *dst, int max_px, int *w, int *h);
void img_free(Img *img);

const Img *catalog_label(int row);
const Img *catalog_page_header(int page);
const Img *catalog_entry_tile(u16 entry);
u32 catalog_entry_hero(u16 entry);

int catalog_next_item(int item);    // next episode for autoplay, or -1
bool catalog_is_video(int item);
bool catalog_item_path(int item, char *buf, size_t len);   // games only
u32 item_key(int item);
int item_by_key(u32 key);

#endif
