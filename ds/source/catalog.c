#include <stdlib.h>
#include <string.h>

#include "catalog.h"

_Static_assert(sizeof(CatHeader) == 60, "CatHeader layout");
_Static_assert(sizeof(CatPage) == 20, "CatPage layout");
_Static_assert(sizeof(CatRow) == 8, "CatRow layout");
_Static_assert(sizeof(CatItem) == 52, "CatItem layout");

#define MAX_TILES_LOADED 400    // ~2.3 MB of tiles before we drop the cache

Catalog cat;

static bool read_at(u32 off, void *dst, size_t len)
{
    return fseek(cat.f, (long)off, SEEK_SET) == 0 && fread(dst, 1, len, cat.f) == len;
}

bool img_load(u32 off, Img *out)
{
    u16 wh[2];
    out->px = NULL;
    if (!off || !read_at(off, wh, 4) || wh[0] == 0 || wh[0] > 256 || wh[1] == 0 || wh[1] > 192)
        return false;
    size_t bytes = (size_t)wh[0] * wh[1] * 2;
    u16 *px = malloc(bytes);
    if (!px)
        return false;
    if (fread(px, 1, bytes, cat.f) != bytes) {
        free(px);
        return false;
    }
    out->w = wh[0];
    out->h = wh[1];
    out->px = px;
    return true;
}

bool img_load_into(u32 off, u16 *dst, int max_px, int *w, int *h)
{
    u16 wh[2];
    if (!off || !read_at(off, wh, 4) || wh[0] * wh[1] > max_px)
        return false;
    size_t bytes = (size_t)wh[0] * wh[1] * 2;
    if (fread(dst, 1, bytes, cat.f) != bytes)
        return false;
    *w = wh[0];
    *h = wh[1];
    return true;
}

void img_free(Img *img)
{
    free(img->px);
    img->px = NULL;
}

bool catalog_open(const char *path)
{
    cat.f = fopen(path, "rb");
    if (!cat.f)
        return false;
    setvbuf(cat.f, NULL, _IOFBF, 8 * 1024);

    CatHeader *h = &cat.h;
    if (fread(h, sizeof *h, 1, cat.f) != 1 || memcmp(h->magic, "RCAT", 4) != 0
        || h->version != CAT_VERSION || h->n_pages == 0 || h->n_items == 0)
        return false;

    cat.pages = calloc(h->n_pages, sizeof(CatPage));
    cat.rows = calloc(h->n_rows, sizeof(CatRow));
    cat.entries = calloc(h->n_entries, sizeof(u16));
    cat.items = calloc(h->n_items, sizeof(CatItem));
    cat.page_headers = calloc(h->n_pages, sizeof(Img));
    cat.page_tiles = calloc(h->n_pages, sizeof(Img));
    cat.labels = calloc(h->n_rows, sizeof(Img));
    cat.thumbs = calloc(h->n_items, sizeof(Img));
    if (!cat.pages || !cat.rows || !cat.entries || !cat.items || !cat.page_headers
        || !cat.page_tiles || !cat.labels || !cat.thumbs)
        return false;

    if (!read_at(h->off_pages, cat.pages, h->n_pages * sizeof(CatPage))
        || !read_at(h->off_rows, cat.rows, h->n_rows * sizeof(CatRow))
        || !read_at(h->off_entries, cat.entries, h->n_entries * sizeof(u16))
        || !read_at(h->off_items, cat.items, h->n_items * sizeof(CatItem)))
        return false;

    // never trust an index read from disk
    for (int p = 0; p < h->n_pages; p++)
        if (cat.pages[p].first_row + cat.pages[p].n_rows > h->n_rows)
            return false;
    for (int r = 0; r < h->n_rows; r++)
        if (cat.rows[r].first + cat.rows[r].count > h->n_entries)
            return false;
    for (u32 e = 0; e < h->n_entries; e++) {
        u16 v = cat.entries[e];
        if ((v & ENTRY_PAGE) ? (v & 0x7FFF) >= h->n_pages : v >= h->n_items)
            return false;
    }
    for (int i = 0; i < h->n_items; i++)
        cat.items[i].id[11] = 0;

    img_load(h->off_digits, &cat.digits);
    img_load(h->off_cw_label, &cat.cw_label);
    img_load(h->off_hint_notouch, &cat.hint_notouch);
    img_load(h->off_hint_locked, &cat.hint_locked);
    img_load(h->off_shuffle_tile, &cat.shuffle_tile);
    img_load(h->off_shuffle_label, &cat.shuffle_label);
    return true;
}

const Img *catalog_label(int row)
{
    Img *l = &cat.labels[row];
    if (!l->px)
        img_load(cat.rows[row].label_off, l);
    return l;
}

const Img *catalog_page_header(int page)
{
    Img *hd = &cat.page_headers[page];
    if (!hd->px)
        img_load(cat.pages[page].header_off, hd);
    return hd;
}

static void maybe_evict(void)
{
    if (cat.tiles_loaded < MAX_TILES_LOADED)
        return;
    // Simple and predictable: drop every tile, visible ones reload on demand.
    for (int i = 0; i < cat.h.n_items; i++)
        img_free(&cat.thumbs[i]);
    for (int p = 0; p < cat.h.n_pages; p++)
        img_free(&cat.page_tiles[p]);
    cat.tiles_loaded = 0;
}

const Img *catalog_entry_tile(u16 entry)
{
    bool page = entry & ENTRY_PAGE;
    int idx = entry & 0x7FFF;
    Img *t = page ? &cat.page_tiles[idx] : &cat.thumbs[idx];
    if (t->px)
        return t;
    maybe_evict();
    if (!img_load(page ? cat.pages[idx].tile_off : cat.items[idx].thumb_off, t))
        return NULL;
    cat.tiles_loaded++;
    return t;
}

u32 catalog_entry_hero(u16 entry)
{
    return (entry & ENTRY_PAGE) ? cat.pages[entry & 0x7FFF].hero_off : cat.items[entry].hero_off;
}

// Autoplay order: the next video after `item` in the row it lives in (show tiles are
// skipped). Inside a show, the end of a season continues with the next season's row.
int catalog_next_item(int item)
{
    for (int p = 0; p < cat.h.n_pages; p++) {
        const CatPage *pg = &cat.pages[p];
        for (int r = 0; r < pg->n_rows; r++) {
            const CatRow *row = &cat.rows[pg->first_row + r];
            for (int k = 0; k < row->count; k++) {
                if (cat.entries[row->first + k] != item)
                    continue;
                for (int j = k + 1; j < row->count; j++) {
                    u16 e = cat.entries[row->first + j];
                    if (!(e & ENTRY_PAGE) && catalog_is_video(e))
                        return e;
                }
                if (p == HOME_PAGE)
                    return -1;          // films / folders: stop at the end of the row
                for (int rr = r + 1; rr < pg->n_rows; rr++) {
                    const CatRow *nr = &cat.rows[pg->first_row + rr];
                    for (int j = 0; j < nr->count; j++) {
                        u16 e = cat.entries[nr->first + j];
                        if (!(e & ENTRY_PAGE) && catalog_is_video(e))
                            return e;
                    }
                }
                return -1;
            }
        }
    }
    return -1;
}

bool catalog_is_video(int item)
{
    return cat.items[item].kind == ITEM_VIDEO;
}

bool catalog_item_path(int item, char *buf, size_t len)
{
    if (!cat.items[item].path_off || len < 8 || fseek(cat.f, (long)cat.items[item].path_off, SEEK_SET))
        return false;
    size_t n = fread(buf, 1, len - 1, cat.f);
    buf[n] = 0;
    return memchr(buf, 0, n) != NULL || n < len - 1;
}

u32 item_key(int item)
{
    return (u32)strtoul(cat.items[item].id, NULL, 16);
}

int item_by_key(u32 key)
{
    for (int i = 0; i < cat.h.n_items; i++)
        if (item_key(i) == key)
            return i;
    return -1;
}
