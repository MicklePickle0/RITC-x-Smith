/* Thin C wrapper around the RIT Client REST API (http://localhost:9999/v1). */
#ifndef RIT_API_H
#define RIT_API_H
#include "http.h"

#define RIT_MAX_LEVELS 20
#define RIT_MAX_SECS   16
#define RIT_MAX_TENDERS 16

typedef struct { http_conn conn; char key[64]; int verbose; long requests, rate_limited; } rit_t;

typedef struct { int tick, period, ticks_per_period; char status[16]; } rit_case_t;

typedef struct { double price; long qty; } rit_level;
typedef struct { rit_level bids[RIT_MAX_LEVELS]; int nb; rit_level asks[RIT_MAX_LEVELS]; int na; } rit_book;

typedef struct {
    char ticker[16];
    long position;
    double bid, ask, last;
    int api_orders_per_second;
} rit_security;

typedef struct {
    int id;
    char ticker[16];
    char action[8];     /* "BUY" or "SELL", from YOUR side (verify on practice server!) */
    long quantity;
    double price;       /* NAN for non-fixed (auction) tenders */
    int is_fixed_bid;
    int expires;        /* tick */
    char caption[160];
} rit_tender;

typedef struct { long gross, net, gross_limit, net_limit; } rit_limits;

void rit_init(rit_t *r, const char *host, int port, const char *key);

/* All return 0 on success, <0 on error. */
int rit_get_case(rit_t *r, rit_case_t *out);
int rit_get_book(rit_t *r, const char *ticker, rit_book *out, int limit);
int rit_get_securities(rit_t *r, rit_security *out, int max, int *n);
int rit_get_tenders(rit_t *r, rit_tender *out, int max, int *n);
int rit_accept_tender(rit_t *r, int id, double price /* NAN if fixed */);
int rit_decline_tender(rit_t *r, int id);
int rit_get_limits(rit_t *r, rit_limits *out);
/* type: "MARKET" or "LIMIT" (price ignored for MARKET). Writes order id, filled qty and vwap if non-NULL. */
int rit_order(rit_t *r, const char *ticker, const char *action, long qty, const char *type, double price,
              int *order_id, long *filled, double *vwap);
int rit_cancel_all(rit_t *r, const char *ticker /* NULL = all */);

/* helpers */
const rit_security *rit_find(const rit_security *s, int n, const char *ticker);
/* Average price to fill `qty` against levels (asks to buy, bids to sell).
 * If the book is too thin, the remainder is priced at the last level +/- `penalty` per unit. */
double rit_walk(const rit_level *lv, int n, long qty, int buying, double penalty);

#endif
