#include <string.h>

#include "resume.h"

#define MAX_ENTRIES 64

typedef struct {
    u32 key, ms, seq;
} Entry;

static Entry ents[MAX_ENTRIES];
static int n_ents;
static u32 next_seq = 1;

static void path(char *out, size_t len)
{
    snprintf(out, len, "%s/resume.bin", g_root);
}

void resume_load(void)
{
    char p[64];
    path(p, sizeof p);
    FILE *f = fopen(p, "rb");
    n_ents = 0;
    if (!f)
        return;
    u32 hdr[2];
    if (fread(hdr, 4, 2, f) == 2 && hdr[0] == 0x314D5352 /* "RSM1" */ && hdr[1] <= MAX_ENTRIES)
        n_ents = (int)fread(ents, sizeof(Entry), hdr[1], f);
    fclose(f);
    for (int i = 0; i < n_ents; i++)
        if (ents[i].seq >= next_seq)
            next_seq = ents[i].seq + 1;
}

static void save(void)
{
    char p[64];
    path(p, sizeof p);
    FILE *f = fopen(p, "wb");
    if (!f)
        return;
    u32 hdr[2] = { 0x314D5352, (u32)n_ents };
    fwrite(hdr, 4, 2, f);
    fwrite(ents, sizeof(Entry), n_ents, f);
    fclose(f);
}

u32 resume_get(u32 key)
{
    for (int i = 0; i < n_ents; i++)
        if (ents[i].key == key)
            return ents[i].ms;
    return 0;
}

void resume_set(u32 key, u32 ms)
{
    int i;
    for (i = 0; i < n_ents && ents[i].key != key; i++)
        ;
    if (ms == 0) {
        if (i < n_ents) {
            ents[i] = ents[--n_ents];
            save();
        }
        return;
    }
    if (i == n_ents) {
        if (n_ents < MAX_ENTRIES) {
            n_ents++;
        } else {            // evict the least recently watched
            i = 0;
            for (int j = 1; j < n_ents; j++)
                if (ents[j].seq < ents[i].seq)
                    i = j;
        }
    }
    ents[i].key = key;
    ents[i].ms = ms;
    ents[i].seq = next_seq++;
    save();
}

int resume_recent(u32 *keys, int max)
{
    Entry tmp[MAX_ENTRIES];
    memcpy(tmp, ents, sizeof(Entry) * n_ents);
    for (int i = 1; i < n_ents; i++) {      // insertion sort, newest first
        Entry e = tmp[i];
        int j = i - 1;
        while (j >= 0 && tmp[j].seq < e.seq) {
            tmp[j + 1] = tmp[j];
            j--;
        }
        tmp[j + 1] = e;
    }
    int n = n_ents < max ? n_ents : max;
    for (int i = 0; i < n; i++)
        keys[i] = tmp[i].key;
    return n;
}
