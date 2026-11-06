#include <ctype.h>
#include <string.h>

#include "settings.h"

Settings g_settings = { .autoplay = true, .lock_buttons = false, .lock_touch = false,
                        .shuffle_tile = true, .show_games = true, .reelds_path = "" };

static bool truthy(const char *v)
{
    while (*v == ' ' || *v == '\t')
        v++;
    return *v == '1' || tolower((unsigned char)*v) == 'y' || tolower((unsigned char)*v) == 't'
           || (tolower((unsigned char)v[0]) == 'o' && tolower((unsigned char)v[1]) == 'n');
}

void settings_load(void)
{
    char path[64], line[160];
    snprintf(path, sizeof path, "%s/settings.ini", g_root);
    FILE *f = fopen(path, "r");
    if (!f)
        return;             // defaults
    while (fgets(line, sizeof line, f)) {
        char *eq = strchr(line, '=');
        if (line[0] == '#' || !eq)
            continue;
        *eq = 0;
        char *k = line;
        while (*k == ' ' || *k == '\t')
            k++;
        size_t n = strlen(k);
        while (n && (k[n - 1] == ' ' || k[n - 1] == '\t'))
            k[--n] = 0;
        bool v = truthy(eq + 1);
        if (!strcmp(k, "autoplay"))
            g_settings.autoplay = v;
        else if (!strcmp(k, "lock_buttons"))
            g_settings.lock_buttons = v;
        else if (!strcmp(k, "lock_touch"))
            g_settings.lock_touch = v;
        else if (!strcmp(k, "shuffle_tile"))
            g_settings.shuffle_tile = v;
        else if (!strcmp(k, "show_games"))
            g_settings.show_games = v;
        else if (!strcmp(k, "reelds_path")) {
            char *val = eq + 1;
            while (*val == ' ' || *val == '\t')
                val++;
            size_t vn = strcspn(val, "\r\n");
            while (vn && (val[vn - 1] == ' ' || val[vn - 1] == '\t'))
                vn--;
            if (vn < sizeof g_settings.reelds_path) {
                memcpy(g_settings.reelds_path, val, vn);
                g_settings.reelds_path[vn] = 0;
            }
        }
    }
    fclose(f);
}
