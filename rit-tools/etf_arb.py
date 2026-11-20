#!/usr/bin/env python3
"""
Algorithmic ETF Arbitrage Case bot (Python port of etf_arb/etf_arb.c). Standard library only.
Full explanation with worked examples: ETF_BOT.md. Tests: test_etf_arb.py.

THE IDEA
  RITC is an ETF that holds 1 BULL + 1 BEAR. RITC trades in USD; BULL and BEAR trade in CAD;
  the USD ticker is the exchange rate (CAD per 1 USD). So in equilibrium:
      RITC_usd x USD = BULL + BEAR          fair RITC in USD = (BULL + BEAR) / USD
  We make money three ways:
    1. TENDERS  - institutions offer to buy/sell a big block of RITC from/to us at a set price.
                  Take the ones priced better than what it costs to hedge them.
    2. STAT ARB - when RITC x USD drifts away from BULL + BEAR, sell the rich side, buy the cheap
                  side. Hedged pairs are closed at the *correct* price at the end, so they converge.
    3. REBATES  - not targeted yet: all our orders are marketable (whether a marketable LIMIT
                  earns the $0.01 rebate or pays the $0.02 fee is unverified).
  The danger is holding an unhedged block after a tender: prices drift ~$0.08/unit per tick.

EVERY LOOP (~50 ms)
  0. cancel stale orders, read positions + order books (positions from RIT are the truth)
  1. TENDERS:   cost the tender by walking the book (ETF vs basket exit), accept/decline
  2. UNWIND:    shrink directional exposure E = RITC + (BULL+BEAR)/2 with marketable limits
                priced UNWIND_BAND through the touch, on whichever venue (ETF itself, or the
                BULL+BEAR basket) is cheaper right now
  3. LEGS:      keep BULL and BEAR equal so a basket hedge really is a basket
  4. STAT ARB:  if RITC*USD and BULL+BEAR diverge by more than costs, trade the pair
  5. FX:        keep USD exposure (USD position + RITC value) near zero
  If step 2 or 3 traded, steps 4-5 wait for the next loop (positions are stale until re-read).

CONVENTIONS
  * Positions: + = long (we own it), - = short (we owe it). Quantities are shares / units.
  * RITC prices are USD; BULL/BEAR prices are CAD; USD price = CAD per USD.
  * One "unit" of exposure = one RITC, or one BULL + one BEAR (they move together like one RITC).
  * Converters (create/redeem) are manual only (Assets tab), so the bot just PRINTS hints.

Run:
    python etf_arb.py --key YOUR_API_KEY [--host localhost] [--port 9999] [--dry-run] [--log ticks.csv]
"""
import argparse
import csv
import math
import os
import sys
import time
import traceback

from rit_client import NAN, RitClient, num, walk

# ======================= STRATEGY KNOBS (tune on the practice server) ======================= #
# Same names and defaults as the #defines in etf_arb/etf_arb.c.

# --- case facts (from the brief; don't tune) ---
FEE = 0.02                # $/share on market orders (stocks CAD, RITC USD)
REBATE = 0.01             # $/share earned on filled limit orders (not used in any formula yet)
MAX_ORDER = 10000         # max shares per order (stocks and ETF)
MAX_FX_ORDER = 2500000    # max USD per currency order

# --- tenders: when to say yes ---
TENDER_SAFETY = 0.03      # USD/unit required on top of estimated exit cost.
                          #   up = accept fewer, safer tenders; down = accept more, thinner ones
TENDER_MIN_PROFIT = 300.0 # USD total, else decline (filters tiny wins not worth the risk)
TENDER_BID_MARGIN = 0.05  # USD/unit we ask for in competitive (non-fixed) tenders.
                          #   up = more profit per win but win fewer auctions
THIN_BOOK_PENALTY = 0.05  # $/unit beyond visible depth when walking the book (pessimism for
                          #   the part of the hedge the visible book can't absorb)

# --- unwind: how fast to hedge ---
UNWIND_MIN = 100          # ignore exposure below this many units (also min arb size)
UNWIND_SLICE = 10000      # max units per slice (= max order size)
UNWIND_PER_TICK = 60000   # max units unwound per tick (liquidity refills each tick)
UNWIND_BAND = 0.04        # $ we are willing to walk past the touch to hedge quickly.
                          # Bigger = faster hedge, more impact. Unhedged drift costs
                          # ~$0.08/unit per tick of waiting, so don't make this tiny.
MAX_EXPOSURE = 80000      # never accept a tender that leaves |E| above this

# --- stat arb ---
ARB_ENABLE = True
ARB_ENTRY = 0.06          # CAD/unit profit after fees needed to open a pair
ARB_EXIT = 0.02           # CAD/unit profit needed to close a pair early (< ARB_ENTRY, so a
                          #   pair isn't opened and closed on the same small wiggle)
ARB_SLICE = 2000          # units per arb trade (3 market orders: RITC, BULL, BEAR)
ARB_MAX_UNITS = 30000     # stop opening new pairs above this many units

# --- FX ---
FX_HEDGE_ENABLE = True
FX_BAND = 25000.0         # USD of exposure tolerated before hedging

# --- risk limits and timing ---
LIMIT_HEADROOM = 0.90     # never plan to use more than 90% of gross/net limits
FALLBACK_GROSS = 250000   # used only if /limits isn't available
FALLBACK_NET = 150000
NO_NEW_RISK_TICK = 290    # after this: no new tenders or arbs, only unwind (heat = 300 ticks)
LOOP_MS = 50              # target loop period; the market itself updates once per second
BOOK_DEPTH = 20           # order-book levels fetched per ticker
# ============================================================================================ #

# CSV columns written by --log, one row per tick (at the first loop of that tick):
#   time        unix seconds            tick        case tick (1..300)
#   *_mid       (bid+ask)/2 per ticker  fair_usd    (BULL_mid + BEAR_mid) / USD_mid
#   dev_cad     RITC_mid x USD_mid - (BULL_mid + BEAR_mid): + = ETF rich, - = ETF cheap
#   pos_*       positions from RIT      E           RITC + (BULL+BEAR)/2 (0 = hedged)
#   gross       |BULL|+|BEAR|+2|RITC|   nlv         net liquidation value from GET /trader (CAD)
#   requests    API calls so far        rate_limited  429 responses so far
LOG_FIELDS = ["time", "tick", "bull_mid", "bear_mid", "ritc_mid", "usd_mid", "fair_usd", "dev_cad",
              "pos_ritc", "pos_bull", "pos_bear", "pos_usd", "E", "gross", "nlv", "requests", "rate_limited"]


def gross_of(bull, bear, ritc):
    """Gross position as RIT counts it: sum of absolute positions, the ETF counted twice."""
    return abs(bull) + abs(bear) + 2 * abs(ritc)


def net_of(bull, bear, ritc):
    """Net position as RIT counts it: longs minus shorts, the ETF counted twice."""
    return bull + bear + 2 * ritc


def depth_within(levels, band, buying):
    """Quantity available from the touch out to `band` worse.
    e.g. bids 25.00, 24.99, ... with band 0.04 -> sum of the levels priced 25.00 down to 24.96."""
    if not levels:
        return 0
    lim = levels[0][0] + band if buying else levels[0][0] - band
    q = 0
    for px, qty in levels:
        if (px > lim + 1e-9) if buying else (px < lim - 1e-9):   # 1e-9: float-rounding slack
            break
        q += qty
    return q


class Market:
    """One snapshot of books and positions. Rebuilt every loop, never carried over.
    Fields (set by Bot.snapshot):
      p_bull, p_bear, p_ritc   int positions        p_usd     float USD position
      bull_bids, bull_asks...  [(price, qty), ...] best first, for BULL / BEAR / RITC
      bull_bid, bull_ask ...   best prices (the "touch") for BULL / BEAR / RITC, and usd_bid/usd_ask
      *_mid                    (bid + ask) / 2       fair_usd  (bull_mid + bear_mid) / usd_mid
    """
    pass


class TickLog:
    """CSV, one row per tick, flushed every row so a crash loses nothing.
    Appends to an existing file (header written only when the file is new/empty)."""

    def __init__(self, path):
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.f = open(path, "a", newline="")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(LOG_FIELDS)
            self.f.flush()

    def row(self, values):
        self.w.writerow(values)
        self.f.flush()

    def close(self):
        self.f.close()


class Bot:
    """The strategy. State kept between loops is deliberately tiny; positions are NOT kept:
      gross_limit, net_limit   from GET /limits (fallback until it answers)
      decided                  tender ids already accepted/declined this heat (never decide twice)
      last_tick, unwound_this_tick   per-tick unwind budget bookkeeping
      last_status_tick         to print the status line / CSV row once per tick
    """

    def __init__(self, api, dry_run, log=None):
        self.api = api                 # RitClient (or a fake with the same methods, in tests)
        self.dry_run = dry_run         # True: print every decision but send nothing
        self.log = log                 # TickLog or None
        self.gross_limit, self.net_limit = FALLBACK_GROSS, FALLBACK_NET
        self.limits_from_api = False
        self.decided = set()           # tender ids already accepted/declined this heat
        self.last_tick = -1
        self.unwound_this_tick = 0
        self.last_status_tick = -1

    # ------------------------------------------------------------------ helpers
    def read_limits(self):
        """Load gross/net limits from GET /limits. Returns True once real limits are known."""
        lim = self.api.limits()
        e = lim[0] if lim else None    # first limit block; ETF case has one
        if e and num(e.get("gross_limit"), 0) > 0:
            self.gross_limit = int(num(e.get("gross_limit"), 0))
            self.net_limit = int(num(e.get("net_limit"), 0))
            self.limits_from_api = True
        return self.limits_from_api

    def fits_limits(self, bull, bear, ritc):
        """Would these (planned) positions stay within LIMIT_HEADROOM of both limits?"""
        return (gross_of(bull, bear, ritc) <= int(LIMIT_HEADROOM * self.gross_limit) and
                abs(net_of(bull, bear, ritc)) <= int(LIMIT_HEADROOM * self.net_limit))

    def send(self, ticker, action, qty, limit_px=None, why=""):
        """Print and (unless dry-run) send ONE order. limit_px=None -> MARKET, else LIMIT at that price.
        Returns False only if a live order failed. A failed order is never resent here: the next
        loop re-reads positions and decides again."""
        qty = int(qty)
        if qty <= 0:
            return True
        if limit_px is None:
            print(f"  -> {action:<4} {qty:7d} {ticker:<4} MARKET          ({why})")
        else:
            print(f"  -> {action:<4} {qty:7d} {ticker:<4} LIMIT @ {limit_px:<8.4f} ({why})")
        if self.dry_run:
            return True
        r = self.api.order(ticker, action, qty, "MARKET" if limit_px is None else "LIMIT", limit_px)
        if r is None:
            print(f"     !! order failed: {self.api.last_error}")
            return False
        return True

    def slice_send(self, ticker, action, qty, px=None, why=""):
        """Send qty as several orders no bigger than the max order size; stop at the first failure."""
        maxq = MAX_FX_ORDER if ticker == "USD" else MAX_ORDER
        qty = int(qty)
        while qty > 0:
            q = min(qty, maxq)
            if not self.send(ticker, action, q, px, why):
                break
            qty -= q

    # ------------------------------------------------------------------ market snapshot
    def snapshot(self):
        """Read positions (GET /securities) and the three books (GET /securities/book).
        Returns a Market, or None if anything is missing -> the loop is skipped, nothing traded."""
        secs = self.api.securities()
        if secs is None:
            return None
        by = {s.get("ticker"): s for s in secs}
        if any(t not in by for t in ("BULL", "BEAR", "RITC", "USD")):
            print("missing tickers (wrong case?)", file=sys.stderr)
            return None
        m = Market()
        pos = lambda t: int(round(num(by[t].get("position"), 0)))
        m.p_bull, m.p_bear, m.p_ritc = pos("BULL"), pos("BEAR"), pos("RITC")
        m.p_usd = float(pos("USD"))
        m.usd_bid, m.usd_ask = num(by["USD"].get("bid")), num(by["USD"].get("ask"))   # USD: no book needed
        books = {}
        for t in ("BULL", "BEAR", "RITC"):
            bk = self.api.book(t, BOOK_DEPTH)
            if bk is None or not bk[0] or not bk[1]:   # need both sides of every book
                return None
            books[t] = bk
        m.bull_bids, m.bull_asks = books["BULL"]
        m.bear_bids, m.bear_asks = books["BEAR"]
        m.ritc_bids, m.ritc_asks = books["RITC"]
        m.bull_bid, m.bull_ask = m.bull_bids[0][0], m.bull_asks[0][0]
        m.bear_bid, m.bear_ask = m.bear_bids[0][0], m.bear_asks[0][0]
        m.ritc_bid, m.ritc_ask = m.ritc_bids[0][0], m.ritc_asks[0][0]
        m.bull_mid = 0.5 * (m.bull_bid + m.bull_ask)
        m.bear_mid = 0.5 * (m.bear_bid + m.bear_ask)
        m.ritc_mid = 0.5 * (m.ritc_bid + m.ritc_ask)
        m.usd_mid = 0.5 * (m.usd_bid + m.usd_ask)
        if not (m.usd_mid > 0):        # also catches NaN
            return None
        m.fair_usd = (m.bull_mid + m.bear_mid) / m.usd_mid
        return m

    # ------------------------------------------------------------------ 1. tenders
    def handle_tenders(self, m, tick):
        """Decide every new RITC tender: accept if it pays more than hedging it costs, else decline.

        Cost of hedging = average price of walking the book for the FULL quantity, on the better of
        two exits: trade RITC itself, or the BULL+BEAR basket converted to USD. Worked example
        (ETF_BOT.md, test_buy_fixed_accept): you BUY 10,000 @ 24.90, RITC bids 5000@25.00 +
        5000@24.99 -> exit 24.995 - 0.02 fee = 24.975; edge = 24.975 - 24.90 - 0.03 = 0.045/u = $450.
        """
        tenders = self.api.tenders()
        if tenders is None:
            return
        for t in tenders:
            tid = int(num(t.get("tender_id"), -1))
            if tid in self.decided or t.get("ticker") != "RITC":
                continue
            Q = int(num(t.get("quantity"), 0))
            action = t.get("action", "")
            we_buy = action == "BUY"   # assumed to be from OUR side (verify on the practice server)
            fixed = t.get("is_fixed_bid")
            fixed = True if fixed is None else bool(fixed)
            # --- what is the block worth to us, per unit, in USD? (best way out after fees) ---
            if we_buy:   # we receive Q RITC; we'll need to SELL Q (or short the basket)
                exit_etf = walk(m.ritc_bids, Q, False, THIN_BOOK_PENALTY) - FEE
                exit_basket = (walk(m.bull_bids, Q, False, THIN_BOOK_PENALTY) +
                               walk(m.bear_bids, Q, False, THIN_BOOK_PENALTY) - 2 * FEE) / m.usd_ask
                best = exit_etf if exit_etf > exit_basket else exit_basket     # higher proceeds
                venue = "ETF" if exit_etf > exit_basket else "basket"
            else:        # we deliver Q RITC (go short); we'll need to BUY Q (or buy the basket)
                exit_etf = walk(m.ritc_asks, Q, True, THIN_BOOK_PENALTY) + FEE
                exit_basket = (walk(m.bull_asks, Q, True, THIN_BOOK_PENALTY) +
                               walk(m.bear_asks, Q, True, THIN_BOOK_PENALTY) + 2 * FEE) / m.usd_bid
                best = exit_etf if exit_etf < exit_basket else exit_basket     # lower cost
                venue = "ETF" if exit_etf < exit_basket else "basket"
            # --- the price we'd trade at ---
            price = num(t.get("price"))            # NaN if the tender has no price
            if not fixed:   # competitive: name our own price with a margin
                price = (best - TENDER_SAFETY - TENDER_BID_MARGIN if we_buy
                         else best + TENDER_SAFETY + TENDER_BID_MARGIN)
            # --- profit after the safety cushion, and do we have room? ---
            per_unit = best - price - TENDER_SAFETY if we_buy else price - best - TENDER_SAFETY
            profit = per_unit * Q
            new_ritc = m.p_ritc + (Q if we_buy else -Q)
            new_E = new_ritc + 0.5 * (m.p_bull + m.p_bear)
            room = self.fits_limits(m.p_bull, m.p_bear, new_ritc) and abs(new_E) <= MAX_EXPOSURE

            print(f"[t{tick:3d}] TENDER #{tid} you {action} {Q} RITC @ {price:.2f}{'' if fixed else ' (our bid)'}"
                  f" | fair {m.fair_usd:.3f} | exit via {venue} {best:.3f} | edge {per_unit:.3f}/u = ${profit:.0f}"
                  f"{'' if room else ' | NO LIMIT ROOM'}")

            # --- decide: all five conditions, or decline ---
            if (tick < NO_NEW_RISK_TICK and room and per_unit > 0 and profit >= TENDER_MIN_PROFIT
                    and not math.isnan(price)):
                print("       ACCEPT")
                if not self.dry_run and not self.api.accept_tender(tid, None if fixed else price):
                    print(f"       !! accept failed: {self.api.last_error}")
            else:
                print("       decline")
                if not self.dry_run and not self.api.decline_tender(tid):
                    print(f"       !! decline failed: {self.api.last_error}")
            self.decided.add(tid)      # never decide (or resend) the same tender twice

    # ------------------------------------------------------------------ 2+3. unwind & leg balance
    def unwind(self, m, tick):
        """Step 2: push exposure E back to 0. Step 3: keep BULL = BEAR.
        Returns True if it traded (positions in `m` are then stale)."""
        traded = False
        if tick != self.last_tick:                   # new tick: refill the unwind budget
            self.last_tick = tick
            self.unwound_this_tick = 0
        E = m.p_ritc + 0.5 * (m.p_bull + m.p_bear)   # ETF-equivalent exposure (+ long, - short)
        budget = UNWIND_PER_TICK - self.unwound_this_tick

        if abs(E) >= UNWIND_MIN and budget > 0:
            sell = E > 0                             # long -> sell; short -> buy
            # proceeds (if selling) or cost (if buying) per ETF unit, in USD, at the touch
            via_etf = m.ritc_bid - FEE if sell else m.ritc_ask + FEE
            via_basket = ((m.bull_bid + m.bear_bid - 2 * FEE) / m.usd_ask if sell
                          else (m.bull_ask + m.bear_ask + 2 * FEE) / m.usd_bid)
            use_etf = via_etf >= via_basket if sell else via_etf <= via_basket
            # how much each venue can absorb within UNWIND_BAND of its touch
            d_etf = depth_within(m.ritc_bids if sell else m.ritc_asks, UNWIND_BAND, not sell)
            d_bull = depth_within(m.bull_bids if sell else m.bull_asks, UNWIND_BAND, not sell)
            d_bear = depth_within(m.bear_bids if sell else m.bear_asks, UNWIND_BAND, not sell)

            def size(top):
                """Slice size: never more than the exposure, a slice, the budget, or the depth."""
                return min(int(abs(E)), UNWIND_SLICE, budget, top)

            q = size(d_etf if use_etf else min(d_bull, d_bear))   # basket: limited by thinner leg
            if not use_etf:   # basket legs move E by q/2 each, so trade q per leg to move E by q
                sgn = -q if sell else q
                if not self.fits_limits(m.p_bull + sgn, m.p_bear + sgn, m.p_ritc):
                    use_etf = True
                    q = size(d_etf)   # (C version kept the basket's depth here)
            if q > 0:
                act = "SELL" if sell else "BUY"
                why = (f"unwind E={E:.0f} via {'ETF' if use_etf else 'basket'} "
                       f"(etf {via_etf:.3f} vs basket {via_basket:.3f})")
                print(f"[t{tick:3d}] {why}")
                # marketable LIMIT priced UNWIND_BAND through the touch: fills what's there,
                # never walks further; any unfilled rest is cancelled next loop
                s = -UNWIND_BAND if sell else UNWIND_BAND
                if use_etf:
                    self.send("RITC", act, q, (m.ritc_bid if sell else m.ritc_ask) + s, "unwind")
                else:
                    self.send("BULL", act, q, (m.bull_bid if sell else m.bull_ask) + s, "unwind leg")
                    self.send("BEAR", act, q, (m.bear_bid if sell else m.bear_ask) + s, "unwind leg")
                self.unwound_this_tick += q
                traded = True

        # 3. keep the basket legs equal (an unequal basket is a bet on BULL vs BEAR, not a hedge)
        imb = m.p_bull - m.p_bear
        if not traded and abs(imb) >= UNWIND_MIN:
            q = min(abs(imb), UNWIND_SLICE)
            # Trade ONE leg, picking the direction that also shrinks E:
            #   E > 0 (too long)   -> sell the leg that is too big
            #   E <= 0 (too short) -> buy the leg that is too small
            if E > 0:
                leg = "BULL" if imb > 0 else "BEAR"
            else:
                leg = "BEAR" if imb > 0 else "BULL"
            self.send(leg, "SELL" if E > 0 else "BUY", q, None, "leg balance")
            traded = True
        return traded

    # ------------------------------------------------------------------ 4. stat arb
    def stat_arb(self, m, tick):
        """Trade RITC against the basket when they disagree by more than ARB_ENTRY after fees;
        close pairs early when the reverse trade pays ARB_EXIT. Each trade = 3 MARKET orders."""
        if not ARB_ENABLE:
            return
        # profit per unit in CAD of (a) selling RITC + buying basket, (b) buying RITC + selling basket
        # (FEE * usd_mid = the RITC fee, charged in USD, converted to CAD; 2 * FEE = the two stock fees)
        sell_etf = m.ritc_bid * m.usd_bid - (m.bull_ask + m.bear_ask) - FEE * m.usd_mid - 2 * FEE
        buy_etf = (m.bull_bid + m.bear_bid) - m.ritc_ask * m.usd_ask - FEE * m.usd_mid - 2 * FEE

        # existing pair: short ETF / long basket (dir -1) or long ETF / short basket (dir +1)
        pair, direction = 0, 0
        if m.p_ritc < 0 and m.p_bull > 0 and m.p_bear > 0:
            direction, pair = -1, -m.p_ritc
        if m.p_ritc > 0 and m.p_bull < 0 and m.p_bear < 0:
            direction, pair = +1, m.p_ritc
        if direction:
            pair = min(pair, abs(m.p_bull), abs(m.p_bear))   # pair size = smallest of the 3 legs

        # close a pair early when the reverse trade is profitable
        if pair >= UNWIND_MIN:
            exit_edge = buy_etf if direction < 0 else sell_etf
            if exit_edge > ARB_EXIT:
                q = min(pair, ARB_SLICE)
                print(f"[t{tick:3d}] ARB EXIT pair {pair} (edge {exit_edge:.3f} CAD/u)")
                self.send("RITC", "BUY" if direction < 0 else "SELL", q, None, "arb exit")
                self.send("BULL", "SELL" if direction < 0 else "BUY", q, None, "arb exit")
                self.send("BEAR", "SELL" if direction < 0 else "BUY", q, None, "arb exit")
                return
            # big pair eating the limits: converting 10k units frees 40k of gross (manual click)
            if (pair >= 10000 and gross_of(m.p_bull, m.p_bear, m.p_ritc) > 0.8 * self.gross_limit
                    and tick % 10 == 0):
                kind = "ETF-CREATION (basket -> RITC)" if direction < 0 else "ETF-REDEMPTION (RITC -> basket)"
                print(f"[t{tick:3d}] CONVERTER HINT (manual, Assets tab): {kind} 10,000 units "
                      f"to free limits ($1,500 USD)")

        # open a new pair?
        if tick >= NO_NEW_RISK_TICK or pair >= ARB_MAX_UNITS:
            return
        edge = max(sell_etf, buy_etf)
        if edge < ARB_ENTRY:
            return
        sell = sell_etf > buy_etf      # True: RITC is rich -> sell RITC, buy basket
        q = min(ARB_SLICE,             # only what's at the top of all three books (MARKET orders)
                m.ritc_bids[0][1] if sell else m.ritc_asks[0][1],
                m.bull_asks[0][1] if sell else m.bull_bids[0][1],
                m.bear_asks[0][1] if sell else m.bear_bids[0][1])
        if q < UNWIND_MIN:
            return
        nr = m.p_ritc + (-q if sell else q)
        nb = m.p_bull + (q if sell else -q)
        ne = m.p_bear + (q if sell else -q)
        if not self.fits_limits(nb, ne, nr):
            return
        print(f"[t{tick:3d}] ARB ENTER {'SELL' if sell else 'BUY'} RITC vs basket: edge {edge:.3f} CAD/u, {q} units")
        self.send("RITC", "SELL" if sell else "BUY", q, None, "arb")
        self.send("BULL", "BUY" if sell else "SELL", q, None, "arb")
        self.send("BEAR", "BUY" if sell else "SELL", q, None, "arb")

    # ------------------------------------------------------------------ 5. FX hedge
    def fx_hedge(self, m, tick):
        """Keep our USD exposure inside +/- FX_BAND by trading the USD ticker (MARKET, sliced)."""
        if not FX_HEDGE_ENABLE:
            return
        # Works whether or not RIT books RITC cash flows into the USD position:
        # exposure = USD held + USD value of RITC held.
        exp_usd = m.p_usd + m.p_ritc * m.ritc_mid
        if abs(exp_usd) < FX_BAND:
            return
        print(f"[t{tick:3d}] FX hedge: USD exposure {exp_usd:.0f}")
        self.slice_send("USD", "SELL" if exp_usd > 0 else "BUY", int(abs(exp_usd)), None, "fx hedge")

    # ------------------------------------------------------------------ status line + CSV
    def status(self, m, tick):
        """Once per tick: print the status line, write the CSV row, retry /limits if still unknown."""
        if not self.limits_from_api:   # RIT Client may have come up after we started
            self.read_limits()
        tr = self.api.trader()
        nlv = num(tr.get("nlv")) if tr else NAN
        dev = m.ritc_mid * m.usd_mid - (m.bull_mid + m.bear_mid)   # CAD; + = ETF rich
        E = m.p_ritc + 0.5 * (m.p_bull + m.p_bear)
        gross = gross_of(m.p_bull, m.p_bear, m.p_ritc)
        print(f"[t{tick:3d}] RITC {m.ritc_mid:.2f} fair {m.fair_usd:.3f} dev {dev:+.3f} CAD | pos RITC {m.p_ritc} "
              f"BULL {m.p_bull} BEAR {m.p_bear} USD {m.p_usd:.0f} | gross {gross}/{self.gross_limit} | "
              f"NLV {nlv:,.0f} | req {self.api.requests} rl {self.api.rate_limited}")
        if self.log:
            self.log.row([f"{time.time():.3f}", tick, f"{m.bull_mid:.4f}", f"{m.bear_mid:.4f}",
                          f"{m.ritc_mid:.4f}", f"{m.usd_mid:.5f}", f"{m.fair_usd:.4f}", f"{dev:.4f}",
                          m.p_ritc, m.p_bull, m.p_bear, f"{m.p_usd:.0f}", f"{E:.1f}", gross,
                          "" if math.isnan(nlv) else f"{nlv:.2f}", self.api.requests, self.api.rate_limited])

    # ------------------------------------------------------------------ main loop
    def run(self):
        """Forever: wait for an ACTIVE case, then run step() every LOOP_MS. Ctrl+C escapes."""
        idle = 0                       # loops spent waiting (prints a message every 20)
        while True:
            t0 = time.monotonic()
            c = self.api.case()
            if c is None:              # RIT Client not reachable: keep trying
                idle += 1
                if idle % 20 == 1:
                    print(f"waiting for RIT client... ({self.api.last_error})")
                time.sleep(0.5)
                continue
            status = c.get("status", "?")
            if status != "ACTIVE":     # between heats: forget tenders (ids restart next heat)
                idle += 1
                if idle % 20 == 1:
                    print(f"case status {status}, waiting...")
                self.decided.clear()
                time.sleep(0.25)
                continue
            idle = 0
            tick = int(num(c.get("tick"), 0))
            try:
                self.step(tick)
            except Exception:   # never let one bad loop kill the bot mid-heat; shout about it
                print("!! unexpected error in loop (continuing):", file=sys.stderr)
                traceback.print_exc()
            spent = time.monotonic() - t0
            time.sleep(max(0.0, LOOP_MS / 1000.0 - spent))

    def step(self, tick):
        """One loop: steps 0-5 from the module docstring."""
        if not self.dry_run:
            self.api.cancel_all()      # stale resting slices from last loop
        m = self.snapshot()
        if m is None:
            return
        if tick < self.last_status_tick:   # new heat without us seeing a STOPPED status (we were
            self.decided.clear()           # disconnected): tender ids may restart from 1
        if tick != self.last_status_tick:
            self.last_status_tick = tick
            self.status(m, tick)
        self.handle_tenders(m, tick)
        if not self.unwind(m, tick):   # only one kind of action per loop: the next
            self.stat_arb(m, tick)     # loop re-reads positions, so nothing doubles
            self.fx_hedge(m, tick)


def main():
    """Parse the command line, read limits, run the bot until Ctrl+C."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", required=True, help="API key shown in the RIT Client")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--dry-run", action="store_true", help="print every decision, send NO orders/accepts/declines")
    ap.add_argument("--log", metavar="FILE", help="append one CSV row per tick to FILE")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)   # see decisions immediately, even when piped
    except AttributeError:
        pass
    if os.name == "nt":
        os.system("")   # enable ANSI handling in Windows consoles

    api = RitClient(a.host, a.port, a.key)
    log = TickLog(a.log) if a.log else None
    bot = Bot(api, a.dry_run, log)
    if not bot.read_limits():
        print("warning: /limits unavailable, using fallback limits (will retry each tick)")
    print(f"ETF arb bot: {a.host}:{a.port}  limits gross {bot.gross_limit} net {bot.net_limit}  "
          + ("[DRY RUN: no orders]" if a.dry_run else "") + (f"  logging to {a.log}" if a.log else ""))
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\nstopping")
        if not a.dry_run:
            api.cancel_all()           # don't leave resting slices behind
    finally:
        if log:
            log.close()


if __name__ == "__main__":
    main()
