// Top screen: the selected title's (or show's) hero image, pre-rendered on the PC.
// Bottom screen: rows of tiles for the current page.
//   * Home page: one row per folder, "Continue Watching" first. A show is a single tile.
//   * Show page: one row per season. B goes back to where you were.
//   * A "Can't decide?" row on top holds the Shuffle tile: random videos (home) or random
//     episodes (show page).
// D-pad / touch to move, A or a second tap to play / open.
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "browser.h"
#include "catalog.h"
#include "gfx.h"
#include "player.h"
#include "resume.h"
#include "settings.h"

#define MAX_ROWS   128
#define MAX_DEPTH  8
#define HERO_DELAY 8        // frames the selection must rest before the hero is loaded
#define ENTRY_SHUFFLE 0xFFFF    // UI-only entry: the Shuffle tile (checked before ENTRY_PAGE)
#define ENTRY_NONE    0xFFFE

typedef struct {
    const Img *label;
    int count;
    const u16 *entries;     // catalog entries: item index, or ENTRY_PAGE | page index
    int sel;                // selected column
    int first;              // first fully visible column
    int sx;                 // current horizontal scroll in px (animated)
} UiRow;

typedef struct {
    int page;
    u16 entry;              // what was selected when we left this page
} Crumb;

static UiRow rows[MAX_ROWS];
static int n_rows;
static int sel_row;
static int top_row;
static int sy;              // current vertical scroll in px (animated)
static int cur_page;
static Crumb stack[MAX_DEPTH];
static int depth;
static u16 cw_entries[64];
static u32 hero_loaded = 0xFFFFFFFF;
static const u16 shuffle_entry[1] = { ENTRY_SHUFFLE };

// Shuffle plays from a shuffled deck, so nothing repeats until everything has played.
static u16 *deck;
static int deck_n, deck_pos, deck_page = -1;

static u16 cur_entry(void)
{
    UiRow *r = &rows[sel_row];
    return r->entries[r->sel];
}

static void scroll_to_selection(bool snap)
{
    UiRow *r = &rows[sel_row];
    if (r->sel < r->first)
        r->first = r->sel;
    if (r->sel > r->first + 2)
        r->first = r->sel - 2;
    top_row = sel_row;
    if (top_row > n_rows - VISIBLE_ROWS)
        top_row = n_rows - VISIBLE_ROWS;
    if (top_row < 0)
        top_row = 0;
    if (snap) {
        sy = top_row * ROW_H;
        r->sx = r->first * TILE_STEP;
    }
}

// Selects `entry` if it is on this page. CW row only as a last resort.
static bool select_entry(u16 entry)
{
    for (int pass = 0; pass < 2; pass++) {
        for (int r = 0; r < n_rows; r++) {
            if ((rows[r].entries == cw_entries || rows[r].entries == shuffle_entry) != (pass == 1))
                continue;
            for (int c = 0; c < rows[r].count; c++) {
                if (rows[r].entries[c] == entry) {
                    sel_row = r;
                    rows[r].sel = c;
                    return true;
                }
            }
        }
    }
    return false;
}

static void build_rows(int page)
{
    cur_page = page;
    n_rows = 0;

    if (page == HOME_PAGE) {
        u32 keys[64];
        int n = resume_recent(keys, 64), cw = 0;
        for (int i = 0; i < n; i++) {
            int it = item_by_key(keys[i]);
            if (it >= 0)
                cw_entries[cw++] = (u16)it;
        }
        if (cw)
            rows[n_rows++] = (UiRow){ &cat.cw_label, cw, cw_entries, 0, 0, 0 };
    }
    const CatPage *p = &cat.pages[page];
    for (int r = 0; r < p->n_rows && n_rows < MAX_ROWS; r++) {
        const CatRow *cr = &cat.rows[p->first_row + r];
        if (cr->count)
            rows[n_rows++] = (UiRow){ catalog_label(p->first_row + r), cr->count,
                                      &cat.entries[cr->first], 0, 0, 0 };
    }

    // "Can't decide?" row with the Shuffle tile, at the very top of the page
    if (g_settings.shuffle_tile && cat.shuffle_tile.px && n_rows && n_rows < MAX_ROWS) {
        memmove(&rows[1], &rows[0], n_rows * sizeof(UiRow));
        rows[0] = (UiRow){ &cat.shuffle_label, 1, shuffle_entry, 0, 0, 0 };
        n_rows++;
    }
    sel_row = 0;
}

static void deck_build(int page)
{
    free(deck);
    deck = malloc(cat.h.n_items * sizeof(u16));
    deck_n = deck_pos = 0;
    deck_page = page;
    if (!deck)
        return;
    if (page == HOME_PAGE) {            // everything, including every show's episodes
        for (int i = 0; i < cat.h.n_items; i++)
            deck[deck_n++] = (u16)i;
    } else {                            // just this show
        const CatPage *p = &cat.pages[page];
        for (int r = 0; r < p->n_rows; r++) {
            const CatRow *cr = &cat.rows[p->first_row + r];
            for (int k = 0; k < cr->count; k++)
                if (!(cat.entries[cr->first + k] & ENTRY_PAGE))
                    deck[deck_n++] = cat.entries[cr->first + k];
        }
    }
    for (int i = deck_n - 1; i > 0; i--) {     // Fisher-Yates
        int j = rand() % (i + 1);
        u16 t = deck[i];
        deck[i] = deck[j];
        deck[j] = t;
    }
}

// Next random video for this page; never the one that just played (if there's a choice).
static int deck_next(int just_played)
{
    if (deck_page != cur_page || deck_pos >= deck_n) {
        deck_build(cur_page);
        if (deck_n > 1 && deck[0] == just_played) {
            deck[0] = deck[deck_n - 1];
            deck[deck_n - 1] = (u16)just_played;
        }
    }
    return deck_n ? deck[deck_pos++] : -1;
}

// Inside a show, start on the episode you watched most recently (else the first episode).
static void select_last_watched(void)
{
    if (n_rows > 1 && rows[0].entries == shuffle_entry)
        sel_row = 1;
    u32 keys[64];
    int n = resume_recent(keys, 64);
    for (int i = 0; i < n; i++) {
        int it = item_by_key(keys[i]);
        if (it >= 0 && select_entry((u16)it))
            return;
    }
}

static int approach(int cur, int target)
{
    int d = target - cur;
    if (!d)
        return cur;
    int s = d / 3;
    if (!s)
        s = d > 0 ? 1 : -1;
    return cur + s;
}

static bool animate(void)
{
    bool moving = false;
    int ty = top_row * ROW_H;
    if (sy != ty) {
        sy = approach(sy, ty);
        moving = true;
    }
    for (int i = 0; i < n_rows; i++) {
        int tx = rows[i].first * TILE_STEP;
        if (rows[i].sx != tx) {
            rows[i].sx = approach(rows[i].sx, tx);
            moving = true;
        }
    }
    return moving;
}

static void draw(void)
{
    u16 *fb = g_bb;
    gfx_fill(fb, 0, 0, SCREEN_W, SCREEN_H, COL_BG, 0, SCREEN_H);
    gfx_blit(fb, 0, 0, catalog_page_header(cur_page), 0, HEADER_H);

    for (int r = 0; r < n_rows; r++) {
        int y = HEADER_H + r * ROW_H - sy;
        if (y + ROW_H <= HEADER_H || y >= SCREEN_H)
            continue;
        UiRow *u = &rows[r];
        gfx_blit(fb, LEFT_PAD, y, u->label, HEADER_H, SCREEN_H);
        int ty = y + LABEL_H;
        for (int c = 0; c < u->count; c++) {
            int x = LEFT_PAD + c * TILE_STEP - u->sx;
            if (x + TILE_W <= 0)
                continue;
            if (x >= SCREEN_W)
                break;
            u16 e = u->entries[c];
            const Img *t = e == ENTRY_SHUFFLE ? &cat.shuffle_tile : catalog_entry_tile(e);
            if (t)
                gfx_blit(fb, x, ty, t, HEADER_H, SCREEN_H);
            else
                gfx_fill(fb, x, ty, TILE_W, TILE_H, COL_PLACEHOLDER, HEADER_H, SCREEN_H);

            if (!(e & ENTRY_PAGE)) {        // resume progress on episodes / films (not Shuffle/shows)
                u32 ms = resume_get(item_key(e));
                u32 dur = cat.items[e].duration * 1000;
                if (ms && dur) {
                    int w = (int)((u64)ms * (TILE_W - 8) / dur);
                    if (w > TILE_W - 8)
                        w = TILE_W - 8;
                    gfx_fill(fb, x + 4, ty + TILE_H - 4, TILE_W - 8, 2, COL_TRACK, HEADER_H, SCREEN_H);
                    gfx_fill(fb, x + 4, ty + TILE_H - 4, w, 2, COL_RED, HEADER_H, SCREEN_H);
                }
            }
            if (r == sel_row && c == u->sel)
                gfx_frame(fb, x - 2, ty - 2, TILE_W + 4, TILE_H + 4, 2, COL_WHITE, HEADER_H, SCREEN_H);
        }
    }
    swiWaitForVBlank();     // copy right behind the beam: no tearing
    gfx_present(BG_GFX_SUB, g_bb);
}

static void load_hero(u16 entry)
{
    int w, h;
    u32 off = entry == ENTRY_SHUFFLE ? cat.h.off_shuffle_hero : catalog_entry_hero(entry);
    if (img_load_into(off, g_hero, SCREEN_W * SCREEN_H, &w, &h)
        && w == SCREEN_W && h == SCREEN_H) {
        swiWaitForVBlank();
        gfx_present(BG_GFX, g_hero);
    } else {
        dmaFillHalfWords(COL_BG, BG_GFX, SCREEN_W * SCREEN_H * 2);
    }
    hero_loaded = entry;
}

static void fade(bool out)
{
    for (int i = 0; i <= 16; i += 4) {
        setBrightness(3, out ? -i : -(16 - i));
        swiWaitForVBlank();
    }
}

static bool hit_tile(int px, int py, int *out_row, int *out_col)
{
    if (py < HEADER_H)
        return false;
    for (int r = 0; r < n_rows; r++) {
        int ty = HEADER_H + r * ROW_H - sy + LABEL_H;
        if (py < ty || py >= ty + TILE_H)
            continue;
        for (int c = 0; c < rows[r].count; c++) {
            int x = LEFT_PAD + c * TILE_STEP - rows[r].sx;
            if (px >= x && px < x + TILE_W) {
                *out_row = r;
                *out_col = c;
                return true;
            }
        }
    }
    return false;
}

// Switch page with a quick fade; `keep` is the entry to select (if present).
static void show_page(int page, u16 keep, bool last_watched)
{
    fade(true);
    build_rows(page);
    if (!select_entry(keep) && last_watched)
        select_last_watched();
    scroll_to_selection(true);
    for (int r = 0; r < n_rows; r++)
        if (r != sel_row)
            rows[r].sx = 0;
    load_hero(cur_entry());
    draw();
    fade(false);
}

static void open_page(int page)
{
    if (depth < MAX_DEPTH)
        stack[depth++] = (Crumb){ cur_page, cur_entry() };
    show_page(page, ENTRY_NONE, true);
}

static void go_back(void)
{
    if (!depth)
        return;
    Crumb c = stack[--depth];
    show_page(c.page, c.entry, false);
}

// Plays the selected video, or random ones if the Shuffle tile is selected. With autoplay
// on, the next episode (or the next random pick) follows when one ends by itself.
static void play_selected(void)
{
    u16 sel = cur_entry();
    bool shuffle = sel == ENTRY_SHUFFLE;
    int item = shuffle ? deck_next(-1) : sel;
    if (item < 0)
        return;
    fade(true);
    for (;;) {
        u32 key = item_key(item);
        bool finished;
        u32 pos = player_run(item, resume_get(key), &finished);
        resume_set(key, pos);
        fade(true);
        int next = -1;
        if (finished && g_settings.autoplay)
            next = shuffle ? deck_next(item) : catalog_next_item(item);
        if (next < 0)
            break;
        item = next;
    }

    gfx_init_browser_modes();
    // stay on this page, on the Shuffle tile or the last thing played
    build_rows(cur_page);
    select_entry(shuffle ? ENTRY_SHUFFLE : (u16)item);
    scroll_to_selection(true);
    load_hero(cur_entry());
    draw();
    fade(false);
}

void browser_run(void)
{
    srand((unsigned)time(NULL) ^ (g_vbl_count << 16));
    gfx_init_browser_modes();
    build_rows(HOME_PAGE);
    scroll_to_selection(true);
    load_hero(cur_entry());
    draw();

    int hero_timer = 0;
    bool dirty = false;

    while (1) {
        scanKeys();
        u32 down = keysDown();
        u32 rep = keysDownRepeat();
        int old_row = sel_row, old_col = rows[sel_row].sel;

        if (down & KEY_SELECT)
            return;
        if (down & KEY_B) {
            go_back();
            hero_timer = 0;
            continue;
        }
        if (rep & KEY_UP && sel_row > 0)
            sel_row--;
        if (rep & KEY_DOWN && sel_row < n_rows - 1)
            sel_row++;
        if (rep & KEY_LEFT && rows[sel_row].sel > 0)
            rows[sel_row].sel--;
        if (rep & KEY_RIGHT && rows[sel_row].sel < rows[sel_row].count - 1)
            rows[sel_row].sel++;
        if (rep & KEY_L)    // jump 3 tiles
            rows[sel_row].sel = rows[sel_row].sel > 3 ? rows[sel_row].sel - 3 : 0;
        if (rep & KEY_R) {
            int s = rows[sel_row].sel + 3;
            rows[sel_row].sel = s < rows[sel_row].count ? s : rows[sel_row].count - 1;
        }

        bool activate = (down & (KEY_A | KEY_START)) != 0;
        if (down & KEY_TOUCH) {
            touchPosition t;
            touchRead(&t);
            int r, c;
            if (hit_tile(t.px, t.py, &r, &c)) {
                if (r == sel_row && c == rows[r].sel)
                    activate = true;
                sel_row = r;
                rows[r].sel = c;
            }
        }

        if (sel_row != old_row || rows[sel_row].sel != old_col) {
            scroll_to_selection(false);
            hero_timer = HERO_DELAY;
            dirty = true;
        }
        if (activate) {
            u16 e = cur_entry();
            if (e != ENTRY_SHUFFLE && (e & ENTRY_PAGE))
                open_page(e & 0x7FFF);
            else
                play_selected();
            hero_timer = 0;
            continue;
        }

        if (animate())
            dirty = true;
        if (dirty) {
            draw();             // waits for vblank itself
            dirty = false;
        } else {
            swiWaitForVBlank();
        }
        if (hero_timer && --hero_timer == 0 && hero_loaded != cur_entry())
            load_hero(cur_entry());
    }
}
