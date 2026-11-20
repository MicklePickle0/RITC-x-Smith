/*
 * Algorithmic ETF Arbitrage Case: groundwork trading bot in plain C.
 *
 *   ETF:  RITC (USD)  =  1 BULL (CAD) + 1 BEAR (CAD), converted at USD (CAD per 1 USD)
 *   fair RITC in USD  =  (BULL + BEAR) / USD
 *
 * Every loop:
 *   0. cancel stale orders, read positions + order books (positions from RIT are the truth)
 *   1. TENDERS:   cost the tender by walking the book (ETF vs basket exit), accept/decline
 *   2. UNWIND:    shrink directional exposure E = RITC + (BULL+BEAR)/2 in top-of-book slices,
 *                 on whichever venue (ETF itself, or the BULL+BEAR basket) is cheaper right now
 *   3. LEGS:      keep BULL and BEAR equal so a basket hedge really is a basket
 *   4. STAT ARB:  if RITC*USD and BULL+BEAR diverge by more than costs, trade the pair
 *                 (open positions are closed at the *correct* price at the end, so it converges)
 *   5. FX:        keep USD exposure (USD position + RITC value) near zero
 *
 * Converters (create/redeem) are manual only (Assets tab), so the bot just PRINTS hints.
 *
 * Build:  make            (macOS/Linux/MinGW)     or see README for MSVC
 * Run:    ./etf_arb --key YOUR_API_KEY [--host localhost] [--port 9999] [--dry-run]
 */
#include "rit.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ======================= STRATEGY KNOBS (tune on the practice server) ======================= */
#define FEE                 0.02    /* $/share on market orders (stocks CAD, RITC USD) */
#define REBATE              0.01    /* $/share earned on filled limit orders */
#define MAX_ORDER           10000   /* max shares per order (stocks and ETF) */
#define MAX_FX_ORDER        2500000

#define TENDER_SAFETY       0.03    /* USD/unit required on top of estimated exit cost */
#define TENDER_MIN_PROFIT   300.0   /* USD total, else decline */
#define TENDER_BID_MARGIN   0.05    /* USD/unit we ask for in competitive (non-fixed) tenders */
#define THIN_BOOK_PENALTY   0.05    /* $/unit beyond visible depth when walking the book */

#define UNWIND_MIN          100     /* ignore exposure below this many units */
#define UNWIND_SLICE        10000   /* max units per slice (= max order size) */
#define UNWIND_PER_TICK     60000   /* max units unwound per tick (liquidity refills each tick) */
#define UNWIND_BAND         0.04    /* $ we are willing to walk past the touch to hedge quickly.
                                       Bigger = faster hedge, more impact. Unhedged drift costs
                                       ~$0.08/unit per tick of waiting, so don't make this tiny. */
#define MAX_EXPOSURE        80000   /* never accept a tender that leaves |E| above this */

#define ARB_ENABLE          1
#define ARB_ENTRY           0.06    /* CAD/unit profit after fees needed to open a pair */
#define ARB_EXIT            0.02    /* CAD/unit profit needed to close a pair early */
#define ARB_SLICE           2000
#define ARB_MAX_UNITS       30000

#define FX_HEDGE_ENABLE     1
#define FX_BAND             25000.0 /* USD of exposure tolerated before hedging */

#define LIMIT_HEADROOM      0.90    /* never plan to use more than 90% of gross/net limits */
#define FALLBACK_GROSS      250000  /* used only if /limits isn't available */
#define FALLBACK_NET        150000
#define NO_NEW_RISK_TICK    290     /* after this: no new tenders or arbs, only unwind */
#define LOOP_MS             50
#define BOOK_DEPTH          20
/* ============================================================================================ */

typedef struct {
    rit_t api;
    int dry_run;
    long gross_limit, net_limit;
    int decided[256]; int ndecided;
    int last_tick; long unwound_this_tick;
    int last_status_tick;
} bot_t;

typedef struct {
    rit_book bull, bear, ritc;
    double bull_bid, bull_ask, bear_bid, bear_ask, ritc_bid, ritc_ask, usd_bid, usd_ask;
    double bull_mid, bear_mid, ritc_mid, usd_mid, fair_usd;
    long p_bull, p_bear, p_ritc;
    double p_usd;
} mkt_t;

/* ----------------------------------------------------------------------------------------- */
static int already_decided(bot_t *b, int id) {
    for (int i = 0; i < b->ndecided; i++) if (b->decided[i] == id) return 1;
    return 0;
}
static void mark_decided(bot_t *b, int id) {
    b->decided[b->ndecided % 256] = id;
    if (b->ndecided < 256) b->ndecided++;
}

static long gross_of(long bull, long bear, long ritc) { return labs(bull) + labs(bear) + 2 * labs(ritc); }
static long net_of(long bull, long bear, long ritc) { return bull + bear + 2 * ritc; }

static int fits_limits(bot_t *b, long bull, long bear, long ritc) {
    return gross_of(bull, bear, ritc) <= (long)(LIMIT_HEADROOM * b->gross_limit) &&
           labs(net_of(bull, bear, ritc)) <= (long)(LIMIT_HEADROOM * b->net_limit);
}

static int send(bot_t *b, const char *ticker, const char *action, long qty, double limit_px, const char *why) {
    if (qty <= 0) return 0;
    const char *type = isnan(limit_px) ? "MARKET" : "LIMIT";
    if (isnan(limit_px)) printf("  -> %-4s %7ld %-4s MARKET          (%s)\n", action, qty, ticker, why);
    else printf("  -> %-4s %7ld %-4s LIMIT @ %-8.4f (%s)\n", action, qty, ticker, limit_px, why);
    if (b->dry_run) return 0;
    long filled = 0; double vwap = NAN; int oid = -1;
    int rc = rit_order(&b->api, ticker, action, qty, type, limit_px, &oid, &filled, &vwap);
    if (rc != 0) printf("     !! order failed\n");
    return rc;
}

static void slice_send(bot_t *b, const char *ticker, const char *action, long qty, double px, const char *why) {
    long maxq = strcmp(ticker, "USD") ? MAX_ORDER : MAX_FX_ORDER;
    while (qty > 0) {
        long q = qty < maxq ? qty : maxq;
        if (send(b, ticker, action, q, px, why) != 0) break;
        qty -= q;
    }
}

/* ------------------------------- market snapshot ----------------------------------------- */
static int snapshot(bot_t *b, mkt_t *m) {
    rit_security secs[RIT_MAX_SECS]; int n = 0;
    if (rit_get_securities(&b->api, secs, RIT_MAX_SECS, &n) != 0) return -1;
    const rit_security *bull = rit_find(secs, n, "BULL"), *bear = rit_find(secs, n, "BEAR"),
                       *ritc = rit_find(secs, n, "RITC"), *usd = rit_find(secs, n, "USD");
    if (!bull || !bear || !ritc || !usd) { fprintf(stderr, "missing tickers (wrong case?)\n"); return -1; }
    m->p_bull = bull->position; m->p_bear = bear->position; m->p_ritc = ritc->position;
    m->p_usd = (double)usd->position;
    m->usd_bid = usd->bid; m->usd_ask = usd->ask;
    if (rit_get_book(&b->api, "BULL", &m->bull, BOOK_DEPTH) || rit_get_book(&b->api, "BEAR", &m->bear, BOOK_DEPTH) ||
        rit_get_book(&b->api, "RITC", &m->ritc, BOOK_DEPTH))
        return -1;
    if (!m->bull.nb || !m->bull.na || !m->bear.nb || !m->bear.na || !m->ritc.nb || !m->ritc.na) return -1;
    m->bull_bid = m->bull.bids[0].price; m->bull_ask = m->bull.asks[0].price;
    m->bear_bid = m->bear.bids[0].price; m->bear_ask = m->bear.asks[0].price;
    m->ritc_bid = m->ritc.bids[0].price; m->ritc_ask = m->ritc.asks[0].price;
    m->bull_mid = 0.5 * (m->bull_bid + m->bull_ask);
    m->bear_mid = 0.5 * (m->bear_bid + m->bear_ask);
    m->ritc_mid = 0.5 * (m->ritc_bid + m->ritc_ask);
    m->usd_mid = 0.5 * (m->usd_bid + m->usd_ask);
    if (!(m->usd_mid > 0)) return -1;
    m->fair_usd = (m->bull_mid + m->bear_mid) / m->usd_mid;
    return 0;
}

/* ------------------------------- 1. tenders ---------------------------------------------- */
static void handle_tenders(bot_t *b, mkt_t *m, int tick) {
    rit_tender t[RIT_MAX_TENDERS]; int n = 0;
    if (rit_get_tenders(&b->api, t, RIT_MAX_TENDERS, &n) != 0) return;
    for (int i = 0; i < n; i++) {
        if (already_decided(b, t[i].id) || strcmp(t[i].ticker, "RITC")) continue;
        long Q = t[i].quantity;
        int we_buy = !strcmp(t[i].action, "BUY");
        double exit_etf, exit_basket, best;
        const char *venue;
        if (we_buy) {   /* we receive Q RITC; we'll need to SELL Q (or short the basket) */
            exit_etf = rit_walk(m->ritc.bids, m->ritc.nb, Q, 0, THIN_BOOK_PENALTY) - FEE;
            exit_basket = (rit_walk(m->bull.bids, m->bull.nb, Q, 0, THIN_BOOK_PENALTY) +
                           rit_walk(m->bear.bids, m->bear.nb, Q, 0, THIN_BOOK_PENALTY) - 2 * FEE) / m->usd_ask;
            best = exit_etf > exit_basket ? exit_etf : exit_basket;
            venue = exit_etf > exit_basket ? "ETF" : "basket";
        } else {        /* we deliver Q RITC (go short); we'll need to BUY Q (or buy the basket) */
            exit_etf = rit_walk(m->ritc.asks, m->ritc.na, Q, 1, THIN_BOOK_PENALTY) + FEE;
            exit_basket = (rit_walk(m->bull.asks, m->bull.na, Q, 1, THIN_BOOK_PENALTY) +
                           rit_walk(m->bear.asks, m->bear.na, Q, 1, THIN_BOOK_PENALTY) + 2 * FEE) / m->usd_bid;
            best = exit_etf < exit_basket ? exit_etf : exit_basket;
            venue = exit_etf < exit_basket ? "ETF" : "basket";
        }
        double price = t[i].price;
        if (!t[i].is_fixed_bid) /* competitive: name our own price with a margin */
            price = we_buy ? best - TENDER_SAFETY - TENDER_BID_MARGIN : best + TENDER_SAFETY + TENDER_BID_MARGIN;
        double per_unit = we_buy ? best - price - TENDER_SAFETY : price - best - TENDER_SAFETY;
        double profit = per_unit * Q;
        long new_ritc = m->p_ritc + (we_buy ? Q : -Q);
        double new_E = new_ritc + 0.5 * (double)(m->p_bull + m->p_bear);
        int room = fits_limits(b, m->p_bull, m->p_bear, new_ritc) && fabs(new_E) <= MAX_EXPOSURE;

        printf("[t%3d] TENDER #%d you %s %ld RITC @ %.2f%s | fair %.3f | exit via %s %.3f | edge %.3f/u = $%.0f%s\n",
               tick, t[i].id, t[i].action, Q, price, t[i].is_fixed_bid ? "" : " (our bid)", m->fair_usd,
               venue, best, per_unit, profit, room ? "" : " | NO LIMIT ROOM");

        if (tick < NO_NEW_RISK_TICK && room && per_unit > 0 && profit >= TENDER_MIN_PROFIT && !isnan(price)) {
            printf("       ACCEPT\n");
            if (!b->dry_run) rit_accept_tender(&b->api, t[i].id, t[i].is_fixed_bid ? NAN : price);
        } else {
            printf("       decline\n");
            if (!b->dry_run) rit_decline_tender(&b->api, t[i].id);
        }
        mark_decided(b, t[i].id);
    }
}

/* ------------------------------- 2+3. unwind & leg balance ------------------------------- */
/* returns 1 if it traded (positions in `m` are then stale) */
/* quantity available from the touch out to `band` worse */
static long depth_within(const rit_level *lv, int n, double band, int buying) {
    if (n <= 0) return 0;
    double lim = buying ? lv[0].price + band : lv[0].price - band;
    long q = 0;
    for (int i = 0; i < n; i++) {
        if (buying ? lv[i].price > lim + 1e-9 : lv[i].price < lim - 1e-9) break;
        q += lv[i].qty;
    }
    return q;
}

static int unwind(bot_t *b, mkt_t *m, int tick) {
    int traded = 0;
    if (tick != b->last_tick) { b->last_tick = tick; b->unwound_this_tick = 0; }
    double E = m->p_ritc + 0.5 * (double)(m->p_bull + m->p_bear);   /* ETF-equivalent exposure */
    long budget = UNWIND_PER_TICK - b->unwound_this_tick;

    if (fabs(E) >= UNWIND_MIN && budget > 0) {
        int sell = E > 0;
        /* proceeds (if selling) or cost (if buying) per ETF unit, in USD, at the touch */
        double via_etf = sell ? m->ritc_bid - FEE : m->ritc_ask + FEE;
        double via_basket = sell ? (m->bull_bid + m->bear_bid - 2 * FEE) / m->usd_ask
                                 : (m->bull_ask + m->bear_ask + 2 * FEE) / m->usd_bid;
        int use_etf = sell ? via_etf >= via_basket : via_etf <= via_basket;
        long d_etf = sell ? depth_within(m->ritc.bids, m->ritc.nb, UNWIND_BAND, 0)
                          : depth_within(m->ritc.asks, m->ritc.na, UNWIND_BAND, 1);
        long d_bull = sell ? depth_within(m->bull.bids, m->bull.nb, UNWIND_BAND, 0)
                           : depth_within(m->bull.asks, m->bull.na, UNWIND_BAND, 1);
        long d_bear = sell ? depth_within(m->bear.bids, m->bear.nb, UNWIND_BAND, 0)
                           : depth_within(m->bear.asks, m->bear.na, UNWIND_BAND, 1);
        long top = use_etf ? d_etf : (d_bull < d_bear ? d_bull : d_bear);
        long q = (long)fabs(E);
        if (q > UNWIND_SLICE) q = UNWIND_SLICE;
        if (q > budget) q = budget;
        if (q > top) q = top;
        if (!use_etf) { /* basket legs move E by q/2 each, so trade q per leg to move E by q */
            long nb = m->p_bull + (sell ? -q : q), nr = m->p_bear + (sell ? -q : q);
            if (!fits_limits(b, nb, nr, m->p_ritc)) use_etf = 1;
        }
        if (q > 0) {
            const char *act = sell ? "SELL" : "BUY";
            char why[96];
            snprintf(why, sizeof(why), "unwind E=%.0f via %s (etf %.3f vs basket %.3f)", E, use_etf ? "ETF" : "basket",
                     via_etf, via_basket);
            printf("[t%3d] %s\n", tick, why);
            /* marketable LIMIT priced UNWIND_BAND through the touch: fills what's there,
               never walks further; any unfilled rest is cancelled next loop */
            double s = sell ? -UNWIND_BAND : UNWIND_BAND;
            if (use_etf) {
                send(b, "RITC", act, q, (sell ? m->ritc_bid : m->ritc_ask) + s, "unwind");
            } else {
                send(b, "BULL", act, q, (sell ? m->bull_bid : m->bull_ask) + s, "unwind leg");
                send(b, "BEAR", act, q, (sell ? m->bear_bid : m->bear_ask) + s, "unwind leg");
            }
            b->unwound_this_tick += q;
            traded = 1;
        }
    }

    /* 3. keep the basket legs equal */
    long imb = m->p_bull - m->p_bear;
    if (!traded && labs(imb) >= UNWIND_MIN) {
        long q = labs(imb);
        if (q > UNWIND_SLICE) q = UNWIND_SLICE;
        /* Trade ONE leg, picking the direction that also shrinks E:
           E > 0 (too long)  -> sell the leg that is too big
           E <= 0 (too short) -> buy the leg that is too small */
        const char *leg = (E > 0) ? (imb > 0 ? "BULL" : "BEAR") : (imb > 0 ? "BEAR" : "BULL");
        send(b, leg, E > 0 ? "SELL" : "BUY", q, NAN, "leg balance");
        traded = 1;
    }
    return traded;
}

/* ------------------------------- 4. stat arb --------------------------------------------- */
static void stat_arb(bot_t *b, mkt_t *m, int tick) {
    if (!ARB_ENABLE) return;
    /* profit per unit in CAD of (a) selling RITC + buying basket, (b) buying RITC + selling basket */
    double sell_etf = m->ritc_bid * m->usd_bid - (m->bull_ask + m->bear_ask) - FEE * m->usd_mid - 2 * FEE;
    double buy_etf = (m->bull_bid + m->bear_bid) - m->ritc_ask * m->usd_ask - FEE * m->usd_mid - 2 * FEE;

    /* existing pair: short ETF / long basket (dir -1) or long ETF / short basket (dir +1) */
    long pair = 0; int dir = 0;
    if (m->p_ritc < 0 && m->p_bull > 0 && m->p_bear > 0) { dir = -1; pair = -m->p_ritc; }
    if (m->p_ritc > 0 && m->p_bull < 0 && m->p_bear < 0) { dir = +1; pair = m->p_ritc; }
    if (dir) {
        long legs = labs(m->p_bull) < labs(m->p_bear) ? labs(m->p_bull) : labs(m->p_bear);
        if (legs < pair) pair = legs;
    }

    /* close a pair early when the reverse trade is profitable */
    if (pair >= UNWIND_MIN) {
        double exit_edge = dir < 0 ? buy_etf : sell_etf;
        if (exit_edge > ARB_EXIT) {
            long q = pair < ARB_SLICE ? pair : ARB_SLICE;
            printf("[t%3d] ARB EXIT pair %ld (edge %.3f CAD/u)\n", tick, pair, exit_edge);
            send(b, "RITC", dir < 0 ? "BUY" : "SELL", q, NAN, "arb exit");
            send(b, "BULL", dir < 0 ? "SELL" : "BUY", q, NAN, "arb exit");
            send(b, "BEAR", dir < 0 ? "SELL" : "BUY", q, NAN, "arb exit");
            return;
        }
        if (pair >= 10000 && gross_of(m->p_bull, m->p_bear, m->p_ritc) > 0.8 * b->gross_limit && tick % 10 == 0)
            printf("[t%3d] CONVERTER HINT (manual, Assets tab): %s 10,000 units to free limits ($1,500 USD)\n",
                   tick, dir < 0 ? "ETF-CREATION (basket -> RITC)" : "ETF-REDEMPTION (RITC -> basket)");
    }

    if (tick >= NO_NEW_RISK_TICK || pair >= ARB_MAX_UNITS) return;
    double edge = sell_etf > buy_etf ? sell_etf : buy_etf;
    if (edge < ARB_ENTRY) return;
    int sell = sell_etf > buy_etf;
    long top = sell ? m->ritc.bids[0].qty : m->ritc.asks[0].qty;
    long t2 = sell ? m->bull.asks[0].qty : m->bull.bids[0].qty, t3 = sell ? m->bear.asks[0].qty : m->bear.bids[0].qty;
    long q = ARB_SLICE;
    if (top < q) q = top;
    if (t2 < q) q = t2;
    if (t3 < q) q = t3;
    if (q < UNWIND_MIN) return;
    long nr = m->p_ritc + (sell ? -q : q), nb = m->p_bull + (sell ? q : -q), ne = m->p_bear + (sell ? q : -q);
    if (!fits_limits(b, nb, ne, nr)) return;
    printf("[t%3d] ARB ENTER %s RITC vs basket: edge %.3f CAD/u, %ld units\n", tick, sell ? "SELL" : "BUY", edge, q);
    send(b, "RITC", sell ? "SELL" : "BUY", q, NAN, "arb");
    send(b, "BULL", sell ? "BUY" : "SELL", q, NAN, "arb");
    send(b, "BEAR", sell ? "BUY" : "SELL", q, NAN, "arb");
}

/* ------------------------------- 5. FX hedge --------------------------------------------- */
static void fx_hedge(bot_t *b, mkt_t *m, int tick) {
    if (!FX_HEDGE_ENABLE) return;
    /* Works whether or not RIT books RITC cash flows into the USD position:
       exposure = USD held + USD value of RITC held. */
    double exp_usd = m->p_usd + m->p_ritc * m->ritc_mid;
    if (fabs(exp_usd) < FX_BAND) return;
    long q = (long)fabs(exp_usd);
    printf("[t%3d] FX hedge: USD exposure %.0f\n", tick, exp_usd);
    slice_send(b, "USD", exp_usd > 0 ? "SELL" : "BUY", q, NAN, "fx hedge");
}

/* ----------------------------------------------------------------------------------------- */
int main(int argc, char **argv) {
    const char *host = "localhost", *key = NULL;
    int port = 9999, dry = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--key") && i + 1 < argc) key = argv[++i];
        else if (!strcmp(argv[i], "--host") && i + 1 < argc) host = argv[++i];
        else if (!strcmp(argv[i], "--port") && i + 1 < argc) port = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--dry-run")) dry = 1;
        else { fprintf(stderr, "usage: %s --key API_KEY [--host H] [--port P] [--dry-run]\n", argv[0]); return 2; }
    }
    if (!key) { fprintf(stderr, "--key is required (the API key shown in the RIT Client)\n"); return 2; }
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (http_global_init() != 0) { fprintf(stderr, "network init failed\n"); return 1; }

    static bot_t b;
    memset(&b, 0, sizeof(b));
    rit_init(&b.api, host, port, key);
    b.dry_run = dry;
    b.last_tick = -1;

    rit_limits lim;
    if (rit_get_limits(&b.api, &lim) == 0 && lim.gross_limit > 0) {
        b.gross_limit = lim.gross_limit; b.net_limit = lim.net_limit;
    } else {
        b.gross_limit = FALLBACK_GROSS; b.net_limit = FALLBACK_NET;
        printf("warning: /limits unavailable, using fallback limits\n");
    }
    printf("ETF arb bot: %s:%d  limits gross %ld net %ld  %s\n", host, port, b.gross_limit, b.net_limit,
           dry ? "[DRY RUN: no orders]" : "");

    int idle = 0;
    for (;;) {
        double t0 = now_ms();
        rit_case_t c;
        if (rit_get_case(&b.api, &c) != 0) { if (++idle % 20 == 1) printf("waiting for RIT client...\n"); sleep_ms(500); continue; }
        if (strcmp(c.status, "ACTIVE")) {
            if (++idle % 20 == 1) printf("case status %s, waiting...\n", c.status);
            b.ndecided = 0;
            sleep_ms(250);
            continue;
        }
        idle = 0;
        if (!dry) rit_cancel_all(&b.api, NULL);   /* stale resting slices from last loop */
        mkt_t m;
        if (snapshot(&b, &m) != 0) { sleep_ms(LOOP_MS); continue; }

        if (c.tick != b.last_status_tick) {
            b.last_status_tick = c.tick;
            double dev = m.ritc_mid * m.usd_mid - (m.bull_mid + m.bear_mid);
            printf("[t%3d] RITC %.2f fair %.3f dev %+.3f CAD | pos RITC %ld BULL %ld BEAR %ld USD %.0f | gross %ld/%ld | req %ld rl %ld\n",
                   c.tick, m.ritc_mid, m.fair_usd, dev, m.p_ritc, m.p_bull, m.p_bear, m.p_usd,
                   gross_of(m.p_bull, m.p_bear, m.p_ritc), b.gross_limit, b.api.requests, b.api.rate_limited);
        }
        handle_tenders(&b, &m, c.tick);
        if (!unwind(&b, &m, c.tick)) {      /* only one kind of action per loop: the next */
            stat_arb(&b, &m, c.tick);       /* loop re-reads positions, so nothing doubles */
            fx_hedge(&b, &m, c.tick);
        }

        int spent = (int)(now_ms() - t0);
        sleep_ms(LOOP_MS - spent);
    }
    return 0;
}
