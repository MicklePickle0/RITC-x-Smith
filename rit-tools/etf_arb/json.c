#include "json.h"
#include <stdlib.h>
#include <string.h>

#define POOL_BLOCK 512

typedef struct pool_block { struct pool_block *next; int used; jnode nodes[POOL_BLOCK]; } pool_block;

struct jdoc { pool_block *blocks; jnode *root; };

typedef struct { char *p, *end; jdoc *d; int depth; } parser;

static jnode *new_node(jdoc *d, jtype t) {
    if (!d->blocks || d->blocks->used == POOL_BLOCK) {
        pool_block *b = (pool_block *)calloc(1, sizeof(pool_block));
        if (!b) return NULL;
        b->next = d->blocks;
        d->blocks = b;
    }
    jnode *n = &d->blocks->nodes[d->blocks->used++];
    memset(n, 0, sizeof(*n));
    n->type = t;
    return n;
}

static void skip_ws(parser *ps) {
    while (ps->p < ps->end && (*ps->p == ' ' || *ps->p == '\t' || *ps->p == '\n' || *ps->p == '\r')) ps->p++;
}

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* Decodes the string starting after the opening quote, in place. Returns start or NULL. */
static char *parse_string_raw(parser *ps) {
    char *out = ps->p, *start = ps->p;
    while (ps->p < ps->end) {
        char c = *ps->p++;
        if (c == '"') { *out = '\0'; return start; }
        if (c != '\\') { *out++ = c; continue; }
        if (ps->p >= ps->end) return NULL;
        c = *ps->p++;
        switch (c) {
            case '"': *out++ = '"'; break;
            case '\\': *out++ = '\\'; break;
            case '/': *out++ = '/'; break;
            case 'b': *out++ = '\b'; break;
            case 'f': *out++ = '\f'; break;
            case 'n': *out++ = '\n'; break;
            case 'r': *out++ = '\r'; break;
            case 't': *out++ = '\t'; break;
            case 'u': {
                if (ps->end - ps->p < 4) return NULL;
                int cp = 0;
                for (int i = 0; i < 4; i++) { int h = hexval(ps->p[i]); if (h < 0) return NULL; cp = cp * 16 + h; }
                ps->p += 4;
                /* encode as UTF-8 (surrogate pairs collapsed to '?': not needed for RIT) */
                if (cp < 0x80) *out++ = (char)cp;
                else if (cp < 0x800) { *out++ = (char)(0xC0 | (cp >> 6)); *out++ = (char)(0x80 | (cp & 0x3F)); }
                else if (cp >= 0xD800 && cp <= 0xDFFF) *out++ = '?';
                else { *out++ = (char)(0xE0 | (cp >> 12)); *out++ = (char)(0x80 | ((cp >> 6) & 0x3F)); *out++ = (char)(0x80 | (cp & 0x3F)); }
                break;
            }
            default: return NULL;
        }
    }
    return NULL;
}

static jnode *parse_value(parser *ps);

static jnode *parse_container(parser *ps, int is_obj) {
    jnode *n = new_node(ps->d, is_obj ? J_OBJ : J_ARR), *last = NULL;
    if (!n || ++ps->depth > 64) return NULL;
    ps->p++; /* skip [ or { */
    skip_ws(ps);
    if (ps->p < ps->end && *ps->p == (is_obj ? '}' : ']')) { ps->p++; ps->depth--; return n; }
    for (;;) {
        const char *key = NULL;
        skip_ws(ps);
        if (is_obj) {
            if (ps->p >= ps->end || *ps->p != '"') return NULL;
            ps->p++;
            key = parse_string_raw(ps);
            if (!key) return NULL;
            skip_ws(ps);
            if (ps->p >= ps->end || *ps->p != ':') return NULL;
            ps->p++;
        }
        jnode *v = parse_value(ps);
        if (!v) return NULL;
        v->key = key;
        if (last) last->next = v; else n->child = v;
        last = v;
        n->len++;
        skip_ws(ps);
        if (ps->p >= ps->end) return NULL;
        if (*ps->p == ',') { ps->p++; continue; }
        if (*ps->p == (is_obj ? '}' : ']')) { ps->p++; ps->depth--; return n; }
        return NULL;
    }
}

static jnode *parse_value(parser *ps) {
    skip_ws(ps);
    if (ps->p >= ps->end) return NULL;
    char c = *ps->p;
    if (c == '{') return parse_container(ps, 1);
    if (c == '[') return parse_container(ps, 0);
    if (c == '"') {
        ps->p++;
        jnode *n = new_node(ps->d, J_STR);
        if (!n || !(n->str = parse_string_raw(ps))) return NULL;
        return n;
    }
    if (c == 't' && ps->end - ps->p >= 4 && !strncmp(ps->p, "true", 4)) {
        ps->p += 4; jnode *n = new_node(ps->d, J_BOOL); if (n) n->num = 1; return n;
    }
    if (c == 'f' && ps->end - ps->p >= 5 && !strncmp(ps->p, "false", 5)) {
        ps->p += 5; return new_node(ps->d, J_BOOL);
    }
    if (c == 'n' && ps->end - ps->p >= 4 && !strncmp(ps->p, "null", 4)) {
        ps->p += 4; return new_node(ps->d, J_NULL);
    }
    if (c == '-' || (c >= '0' && c <= '9')) {
        /* strtod needs a terminator: numbers are always followed by , ] } ws or end */
        char tmp[64]; int i = 0;
        while (ps->p < ps->end && i < 63 && strchr("+-0123456789.eE", *ps->p)) tmp[i++] = *ps->p++;
        tmp[i] = '\0';
        char *e;
        double v = strtod(tmp, &e);
        if (e == tmp) return NULL;
        jnode *n = new_node(ps->d, J_NUM);
        if (n) n->num = v;
        return n;
    }
    return NULL;
}

jdoc *json_parse(char *buf, size_t len) {
    jdoc *d = (jdoc *)calloc(1, sizeof(jdoc));
    if (!d) return NULL;
    parser ps = { buf, buf + len, d, 0 };
    d->root = parse_value(&ps);
    if (!d->root) { json_free(d); return NULL; }
    return d;
}

jnode *json_root(jdoc *d) { return d ? d->root : NULL; }

void json_free(jdoc *d) {
    if (!d) return;
    pool_block *b = d->blocks;
    while (b) { pool_block *n = b->next; free(b); b = n; }
    free(d);
}

jnode *json_get(const jnode *obj, const char *key) {
    if (!obj || obj->type != J_OBJ) return NULL;
    for (jnode *c = obj->child; c; c = c->next)
        if (c->key && !strcmp(c->key, key)) return c;
    return NULL;
}

jnode *json_at(const jnode *arr, int i) {
    if (!arr || arr->type != J_ARR) return NULL;
    jnode *c = arr->child;
    while (c && i-- > 0) c = c->next;
    return c;
}

double json_num(const jnode *obj, const char *key, double dflt) {
    jnode *n = json_get(obj, key);
    return (n && (n->type == J_NUM || n->type == J_BOOL)) ? n->num : dflt;
}

const char *json_str(const jnode *obj, const char *key, const char *dflt) {
    jnode *n = json_get(obj, key);
    return (n && n->type == J_STR) ? n->str : dflt;
}

int json_bool(const jnode *obj, const char *key, int dflt) {
    jnode *n = json_get(obj, key);
    return (n && n->type == J_BOOL) ? (int)n->num : dflt;
}
