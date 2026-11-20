#include "rit.h"
#include "json.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

void rit_init(rit_t *r, const char *host, int port, const char *key) {
    memset(r, 0, sizeof(*r));
    http_conn_init(&r->conn, host, port);
    snprintf(r->key, sizeof(r->key), "%s", key);
}

/* Request with rate-limit handling. On success returns the parsed doc (caller frees). */
static jdoc *call(rit_t *r, const char *method, const char *path, int *status_out) {
    for (int attempt = 0; attempt < 5; attempt++) {
        r->requests++;
        http_resp h = http_request(&r->conn, method, path, r->key);
        if (status_out) *status_out = h.status;
        if (h.status == -1) return NULL;
        jdoc *d = h.body_len ? json_parse(h.body, h.body_len) : NULL;
        if (h.status == 429) {                   /* rate limited: wait as told, then retry */
            r->rate_limited++;
            double wait = h.retry_after;
            if (d) { double w = json_num(json_root(d), "wait", -1); if (w >= 0) wait = w; }
            json_free(d);
            sleep_ms((int)(wait * 1000) + 5);
            continue;
        }
        if (h.status == 401) {
            fprintf(stderr, "401 Unauthorized: wrong API key? (shown in the RIT Client)\n");
            json_free(d);
            return NULL;
        }
        if (h.status != 200) {
            if (r->verbose || h.status >= 400) {
                jnode *root = json_root(d);
                fprintf(stderr, "[api] %s %s -> %d %s\n", method, path, h.status,
                        root ? json_str(root, "message", "") : "");
            }
            json_free(d);
            return NULL;
        }
        return d;
    }
    return NULL;
}

int rit_get_case(rit_t *r, rit_case_t *out) {
    jdoc *d = call(r, "GET", "/v1/case", NULL);
    if (!d) return -1;
    jnode *o = json_root(d);
    out->tick = (int)json_num(o, "tick", 0);
    out->period = (int)json_num(o, "period", 0);
    out->ticks_per_period = (int)json_num(o, "ticks_per_period", 300);
    snprintf(out->status, sizeof(out->status), "%s", json_str(o, "status", "?"));
    json_free(d);
    return 0;
}

static int read_levels(jnode *arr, rit_level *lv, int max) {
    int n = 0;
    for (jnode *e = arr ? arr->child : NULL; e && n < max; e = e->next) {
        long q = (long)json_num(e, "quantity", 0) - (long)json_num(e, "quantity_filled", 0);
        if (q <= 0) continue;
        lv[n].price = json_num(e, "price", NAN);
        lv[n].qty = q;
        n++;
    }
    return n;
}

int rit_get_book(rit_t *r, const char *ticker, rit_book *out, int limit) {
    char path[128];
    snprintf(path, sizeof(path), "/v1/securities/book?ticker=%s&limit=%d", ticker, limit);
    jdoc *d = call(r, "GET", path, NULL);
    if (!d) return -1;
    jnode *o = json_root(d);
    out->nb = read_levels(json_get(o, "bids"), out->bids, RIT_MAX_LEVELS);
    out->na = read_levels(json_get(o, "asks"), out->asks, RIT_MAX_LEVELS);
    json_free(d);
    return 0;
}

int rit_get_securities(rit_t *r, rit_security *out, int max, int *n) {
    jdoc *d = call(r, "GET", "/v1/securities", NULL);
    *n = 0;
    if (!d) return -1;
    for (jnode *e = json_root(d)->child; e && *n < max; e = e->next) {
        rit_security *s = &out[(*n)++];
        snprintf(s->ticker, sizeof(s->ticker), "%s", json_str(e, "ticker", ""));
        s->position = (long)llround(json_num(e, "position", 0));
        s->bid = json_num(e, "bid", NAN);
        s->ask = json_num(e, "ask", NAN);
        s->last = json_num(e, "last", NAN);
        s->api_orders_per_second = (int)json_num(e, "api_orders_per_second", 10);
    }
    json_free(d);
    return 0;
}

int rit_get_tenders(rit_t *r, rit_tender *out, int max, int *n) {
    jdoc *d = call(r, "GET", "/v1/tenders", NULL);
    *n = 0;
    if (!d) return -1;
    for (jnode *e = json_root(d)->child; e && *n < max; e = e->next) {
        rit_tender *t = &out[(*n)++];
        t->id = (int)json_num(e, "tender_id", -1);
        snprintf(t->ticker, sizeof(t->ticker), "%s", json_str(e, "ticker", ""));
        snprintf(t->action, sizeof(t->action), "%s", json_str(e, "action", ""));
        snprintf(t->caption, sizeof(t->caption), "%s", json_str(e, "caption", ""));
        t->quantity = (long)json_num(e, "quantity", 0);
        t->price = json_num(e, "price", NAN);   /* null -> NAN */
        t->is_fixed_bid = json_bool(e, "is_fixed_bid", 1);
        t->expires = (int)json_num(e, "expires", 0);
    }
    json_free(d);
    return 0;
}

int rit_accept_tender(rit_t *r, int id, double price) {
    char path[128];
    if (isnan(price)) snprintf(path, sizeof(path), "/v1/tenders/%d", id);
    else snprintf(path, sizeof(path), "/v1/tenders/%d?price=%.2f", id, price);
    jdoc *d = call(r, "POST", path, NULL);
    if (!d) return -1;
    int ok = json_bool(json_root(d), "success", 0);
    json_free(d);
    return ok ? 0 : -2;
}

int rit_decline_tender(rit_t *r, int id) {
    char path[64];
    snprintf(path, sizeof(path), "/v1/tenders/%d", id);
    jdoc *d = call(r, "DELETE", path, NULL);
    if (!d) return -1;
    json_free(d);
    return 0;
}

int rit_get_limits(rit_t *r, rit_limits *out) {
    jdoc *d = call(r, "GET", "/v1/limits", NULL);
    memset(out, 0, sizeof(*out));
    if (!d) return -1;
    jnode *e = json_at(json_root(d), 0);    /* first limit block; ETF case has one */
    if (e) {
        out->gross = (long)json_num(e, "gross", 0);
        out->net = (long)json_num(e, "net", 0);
        out->gross_limit = (long)json_num(e, "gross_limit", 0);
        out->net_limit = (long)json_num(e, "net_limit", 0);
    }
    json_free(d);
    return 0;
}

int rit_order(rit_t *r, const char *ticker, const char *action, long qty, const char *type, double price,
              int *order_id, long *filled, double *vwap) {
    char path[256];
    if (!strcmp(type, "LIMIT"))
        snprintf(path, sizeof(path), "/v1/orders?ticker=%s&type=LIMIT&quantity=%ld&action=%s&price=%.4f",
                 ticker, qty, action, price);
    else
        snprintf(path, sizeof(path), "/v1/orders?ticker=%s&type=MARKET&quantity=%ld&action=%s",
                 ticker, qty, action);
    jdoc *d = call(r, "POST", path, NULL);
    if (!d) return -1;
    jnode *o = json_root(d);
    if (order_id) *order_id = (int)json_num(o, "order_id", -1);
    if (filled) *filled = (long)json_num(o, "quantity_filled", 0);
    if (vwap) *vwap = json_num(o, "vwap", NAN);
    json_free(d);
    return 0;
}

int rit_cancel_all(rit_t *r, const char *ticker) {
    char path[96];
    if (ticker) snprintf(path, sizeof(path), "/v1/commands/cancel?ticker=%s", ticker);
    else snprintf(path, sizeof(path), "/v1/commands/cancel?all=1");
    jdoc *d = call(r, "POST", path, NULL);
    if (!d) return -1;
    json_free(d);
    return 0;
}

const rit_security *rit_find(const rit_security *s, int n, const char *ticker) {
    for (int i = 0; i < n; i++) if (!strcmp(s[i].ticker, ticker)) return &s[i];
    return NULL;
}

double rit_walk(const rit_level *lv, int n, long qty, int buying, double penalty) {
    if (qty <= 0) return NAN;
    if (n <= 0) return NAN;
    double cost = 0;
    long left = qty;
    for (int i = 0; i < n && left > 0; i++) {
        long take = left < lv[i].qty ? left : lv[i].qty;
        cost += take * lv[i].price;
        left -= take;
    }
    if (left > 0) {   /* book too thin: assume the rest fills worse than the last visible level */
        double worst = lv[n - 1].price + (buying ? penalty : -penalty);
        cost += left * worst;
    }
    return cost / qty;
}
