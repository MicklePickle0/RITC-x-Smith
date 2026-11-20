#!/usr/bin/env python3
"""
Volatility Case scanner (P4 groundwork). Standard library only.

It reads the market from the RIT REST API about twice a second and shows:
  * your volatility forecast (from news) vs the market maker's implied vol
  * per-option IV, delta, gamma, vega, fair value and $ edge after costs
  * portfolio delta vs the +/-7,000 limit
  * put-call parity / butterfly arbitrage checks
  * a numbered list of SUGGESTED ACTIONS (hedge, open/close straddles, arbs)

Run:
    python vol_scanner.py --key YOUR_API_KEY            # real RIT client (Windows)
    python vol_scanner.py --key TESTKEY --port 9999     # against mock_rit_server.py
    python vol_scanner.py --key KEY --auto-hedge        # bot keeps delta hedged; you pick option trades

Commands (type, then press Enter, while it runs):
    1, 2, 3 ...   execute that suggestion (only if started with --allow-orders)
    s 25          override THIS week's sigma to 25%
    n 28.5        override NEXT week's sigma to 28.5%
    c             clear overrides (go back to parsing news)
    q             quit
"""
import argparse
import http.client
import math
import os
import re
import sys
import threading
import time

from rit_client import RitClient  # shared REST client (keep-alive, rate limits, no POST retries)

# --------------------------------------------------------------------------- #
# Case constants (from the case brief) and strategy knobs (tune these!)
# --------------------------------------------------------------------------- #
TICKS = 300              # 300 s = 20 trading days
TICKS_PER_YEAR = 3600    # 240 days/yr * 15 ticks/day
WEEK = 75
STRIKES = [48, 49, 50, 51, 52]
MULT = 100               # shares per contract
OPT_FEE = 2.00           # $ per contract
RTM_FEE = 0.02           # $ per share
DELTA_LIMIT = 7000
OPT_NET_LIMIT, OPT_GROSS_LIMIT = 1000, 2500
RTM_LIMIT = 50000
MAX_OPT_ORDER, MAX_RTM_ORDER = 100, 10000

HEDGE_BAND = 3000        # re-hedge when |delta| exceeds this
ENTRY_VOL_PTS = 1.5      # open/increase when |edge| >= this many vol points
EXIT_VOL_PTS = 0.5       # flatten when |edge| <= this
STRADDLES_PER_VOLPT = 100
MAX_STRADDLES = 500      # 500 straddles = 1,000 contracts = net limit
ENDGAME_TICK = 270       # stop opening, cut short gamma after this
PARITY_MIN_PROFIT = 5.0  # $ per call/put pair after all fees
GAMMA_BUDGET = 2000      # max delta drift allowed from a 1-standard-deviation one-second RTM move
MIN_HEDGE_BAND = 800     # never let the band shrink below this (avoids hedging every tick)

# --------------------------------------------------------------------------- #
# Black-Scholes, r = 0, no dividends
# --------------------------------------------------------------------------- #
SQRT2PI = math.sqrt(2 * math.pi)


def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def npdf(x):
    return math.exp(-0.5 * x * x) / SQRT2PI


def bs_price(S, K, T, sig, kind):
    if T <= 0 or sig <= 0:
        return max(0.0, S - K) if kind == "C" else max(0.0, K - S)
    sq = sig * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / sq
    d2 = d1 - sq
    if kind == "C":
        return S * ncdf(d1) - K * ncdf(d2)
    return K * ncdf(-d2) - S * ncdf(-d1)


def bs_greeks(S, K, T, sig, kind):
    """delta (per share), gamma (per share per $1), vega ($ per share per 1 vol POINT)."""
    if T <= 0 or sig <= 0:
        itm = (S > K) if kind == "C" else (S < K)
        return ((1.0 if itm else 0.0) if kind == "C" else (-1.0 if itm else 0.0)), 0.0, 0.0
    sq = sig * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / sq
    delta = ncdf(d1) if kind == "C" else ncdf(d1) - 1.0
    gamma = npdf(d1) / (S * sq)
    vega = S * math.sqrt(T) * npdf(d1) / 100.0
    return delta, gamma, vega


def implied_vol(price, S, K, T, kind, lo=1e-4, hi=3.0):
    """Bisection. Returns None if price is outside no-arbitrage bounds."""
    if T <= 0 or price is None:
        return None
    intrinsic = max(0.0, S - K) if kind == "C" else max(0.0, K - S)
    if price <= intrinsic + 1e-6 or price >= (S if kind == "C" else K):
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_price(S, K, T, mid, kind) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- #
# Volatility forecast model
# --------------------------------------------------------------------------- #
RE_THIS = re.compile(r"this week[^0-9]*?(\d+(?:\.\d+)?)\s*%", re.I)
RE_RANGE = re.compile(r"next week[^0-9]*?(\d+(?:\.\d+)?)\s*%?\s*(?:-|–|to|and)\s*(\d+(?:\.\d+)?)\s*%", re.I)


class VolModel:
    """
    Sequence-based (robust to the exact tick a release appears on):
      sigma_now  = latest "this week" value
      sigma_next = midpoint of the latest "next week" range announced AFTER sigma_now,
                   else sigma_now (assume persistence)
    sigma_life  = sqrt of time-weighted variance over the option's remaining life:
                  sigma_now until the next week boundary, sigma_next afterwards.
    """

    def __init__(self):
        self.sigma_now = None
        self.sigma_next = None
        self.next_range = None
        self.override_now = None
        self.override_next = None
        self.log = []

    def on_news(self, item):
        text = f"{item.get('headline', '')} {item.get('body', '')}"
        m = RE_RANGE.search(text)
        if m:
            lo, hi = float(m.group(1)) / 100, float(m.group(2)) / 100
            self.next_range = (lo, hi)
            self.sigma_next = (lo + hi) / 2
            self.log.append(f"t{item.get('tick')}: next week {lo:.0%}-{hi:.0%}")
            return
        m = RE_THIS.search(text)
        if m:
            self.sigma_now = float(m.group(1)) / 100
            self.sigma_next = None
            self.next_range = None
            self.log.append(f"t{item.get('tick')}: this week {self.sigma_now:.0%}")

    def now(self):
        return self.override_now if self.override_now is not None else self.sigma_now

    def nxt(self):
        if self.override_next is not None:
            return self.override_next
        return self.sigma_next if self.sigma_next is not None else self.now()

    def sigma_life(self, tick):
        s_now, s_next = self.now(), self.nxt()
        if s_now is None:
            return None
        remaining = max(1, TICKS - tick)
        to_boundary = min(remaining, WEEK - ((tick - 1) % WEEK) - 1) if tick >= 1 else remaining
        to_boundary = max(0, to_boundary)
        var = (to_boundary * s_now ** 2 + (remaining - to_boundary) * s_next ** 2) / remaining
        return math.sqrt(var)


# --------------------------------------------------------------------------- #
# Scanner
# --------------------------------------------------------------------------- #
class Suggestion:
    def __init__(self, priority, title, reason, orders):
        self.priority, self.title, self.reason, self.orders = priority, title, reason, orders


def clip_orders(ticker, action, qty, max_size):
    out = []
    while qty > 0:
        q = min(qty, max_size)
        out.append((ticker, action, q))
        qty -= q
    return out


class Scanner:
    def __init__(self, api, allow_orders):
        self.api = api
        self.allow_orders = allow_orders
        self.model = VolModel()
        self.last_news_id = 0
        self.state = {}
        self.suggestions = []
        self.messages = []
        self.running = True

    # ---- data ---------------------------------------------------------- #
    def poll(self):
        _, case = self.api.get("/case")
        _, secs = self.api.get("/securities")
        _, news = self.api.get("/news", after=self.last_news_id, limit=50)
        _, trader = self.api.get("/trader")
        for item in sorted(news or [], key=lambda n: n.get("news_id", 0)):
            if item.get("news_id", 0) > self.last_news_id:
                self.model.on_news(item)
                self.last_news_id = item["news_id"]
        return case, {s["ticker"]: s for s in (secs or [])}, trader or {}

    # ---- analytics ----------------------------------------------------- #
    def analyse(self, case, secs, trader):
        tick = case.get("tick", 0)
        T = max(0, TICKS - tick) / TICKS_PER_YEAR
        rtm = secs.get("RTM", {})
        S = (rtm.get("bid", 0) + rtm.get("ask", 0)) / 2 or rtm.get("last", 0)
        sig_life = self.model.sigma_life(tick)
        rows, port_delta, port_gamma, port_vega = [], rtm.get("position", 0), 0.0, 0.0
        for K in STRIKES:
            for kind in "CP":
                t = f"RTM{K}{kind}"
                s = secs.get(t)
                if not s:
                    continue
                bid, ask = s.get("bid", 0), s.get("ask", 0)
                mid = (bid + ask) / 2
                iv = implied_vol(mid, S, K, T, kind)
                d, g, v = bs_greeks(S, K, T, iv or sig_life or 0.2, kind)
                fair = bs_price(S, K, T, sig_life, kind) if sig_life else None
                buy_edge = (fair - ask) * MULT - OPT_FEE if fair is not None else None
                sell_edge = (bid - fair) * MULT - OPT_FEE if fair is not None else None
                pos = s.get("position", 0)
                port_delta += pos * MULT * d
                port_gamma += pos * MULT * g
                port_vega += pos * MULT * v
                rows.append(dict(t=t, K=K, kind=kind, bid=bid, ask=ask, mid=mid, iv=iv, d=d, g=g, v=v,
                                 fair=fair, buy=buy_edge, sell=sell_edge, pos=pos))
        atm = min(STRIKES, key=lambda k: abs(k - S)) if S else 50
        atm_ivs = [r["iv"] for r in rows if r["K"] == atm and r["iv"]]
        atm_iv = sum(atm_ivs) / len(atm_ivs) if atm_ivs else None
        edge = (sig_life - atm_iv) * 100 if (sig_life and atm_iv) else None
        opt_pos = [r["pos"] for r in rows]
        self.state = dict(tick=tick, T=T, S=S, rows=rows, atm=atm, atm_iv=atm_iv, edge=edge,
                          sig_life=sig_life, delta=port_delta, gamma=port_gamma, vega=port_vega,
                          rtm_pos=rtm.get("position", 0), opt_net=sum(opt_pos),
                          opt_gross=sum(map(abs, opt_pos)), nlv=trader.get("nlv"),
                          status=case.get("status"), penalty=trader.get("mock_penalty"))
        self.suggestions = self.suggest()

    # ---- decision logic (the verbal model from the playbook) ------------ #
    def suggest(self):
        st, out = self.state, []
        rows = {r["t"]: r for r in st["rows"]}
        tick, S = st["tick"], st["S"]
        if st["status"] != "ACTIVE" or not S:
            return out

        # Risk scale: how far RTM moves in one second (1 standard deviation), and how much
        # our delta drifts because of it. Gamma (and this drift) explodes near expiry.
        sig_risk = max(x for x in (self.model.now(), st["atm_iv"], 0.2) if x)
        move = S * sig_risk / math.sqrt(TICKS_PER_YEAR)
        drift = abs(st["gamma"]) * move
        band = max(MIN_HEDGE_BAND, min(HEDGE_BAND, DELTA_LIMIT - 3 * drift))
        st["band"], st["drift"] = band, drift

        # 1) Delta hedge: top priority
        dlt = st["delta"]
        if abs(dlt) > band:
            qty = int(round(abs(dlt)))
            action = "SELL" if dlt > 0 else "BUY"
            new_pos = st["rtm_pos"] + (-qty if action == "SELL" else qty)
            if abs(new_pos) > RTM_LIMIT:  # can't hedge fully with RTM: trim options instead
                qty = max(0, qty - (abs(new_pos) - RTM_LIMIT))
            urgent = abs(dlt) > DELTA_LIMIT - 1000
            out.append(Suggestion(0, ("URGENT " if urgent else "") + f"HEDGE: {action} {qty:,} RTM",
                                  f"portfolio delta {dlt:+,.0f} (limit ±{DELTA_LIMIT:,}, penalty $0.10/delta/sec over)",
                                  clip_orders("RTM", action, qty, MAX_RTM_ORDER)))

        # 2) Straddle sizing from vol edge
        edge = st["edge"]
        atm = st["atm"]
        c, p = rows.get(f"RTM{atm}C"), rows.get(f"RTM{atm}P")
        if edge is not None and c and p:
            straddle_vega = (c["v"] + p["v"]) * MULT
            cur_n = st["vega"] / straddle_vega if straddle_vega else 0
            if abs(edge) >= ENTRY_VOL_PTS and tick < ENDGAME_TICK:
                target = max(-MAX_STRADDLES, min(MAX_STRADDLES, STRADDLES_PER_VOLPT * edge))
            elif abs(edge) <= EXIT_VOL_PTS:
                target = 0
            else:
                target = cur_n  # in between: hold (hysteresis)
            if (edge > 0) == (cur_n > 0) and abs(target) < abs(cur_n) and abs(edge) > EXIT_VOL_PTS:
                target = cur_n  # same direction, edge just shrank a bit: hold, don't churn fees
            if tick >= ENDGAME_TICK and cur_n < 0:
                target = 0  # cut short gamma into expiry
            # gamma-based size cap: shrinks automatically as expiry approaches
            straddle_drift = (c["g"] + p["g"]) * MULT * move
            n_cap = GAMMA_BUDGET / straddle_drift if straddle_drift > 0 else MAX_STRADDLES
            st["n_cap"] = n_cap
            target = max(-n_cap, min(n_cap, target))
            diff = target - cur_n
            n = int(min(abs(diff), MAX_OPT_ORDER))
            if n >= 10:
                if (diff > 0 and cur_n >= -1) or (diff < 0 and cur_n <= 1):
                    # increasing exposure in the edge direction: trade ATM straddle
                    action = "BUY" if diff > 0 else "SELL"
                    n = self._fit_limits(n, action)
                    if n > 0:
                        sd = (c["d"] + p["d"]) * MULT * n * (1 if action == "BUY" else -1)
                        exp_pnl = abs(edge) * straddle_vega * n - 2 * (OPT_FEE + 0.01 * MULT) * n
                        orders = [(c["t"], action, n), (p["t"], action, n)]
                        hedge = int(round(-sd))
                        if abs(hedge) >= 100:
                            orders += clip_orders("RTM", "BUY" if hedge > 0 else "SELL", abs(hedge), MAX_RTM_ORDER)
                        out.append(Suggestion(1, f"{action} {n} ATM straddles (K={atm})",
                                              f"vol edge {edge:+.1f} pts (forecast {st['sig_life']:.1%} vs IV "
                                              f"{st['atm_iv']:.1%}); exp. value ≈ ${exp_pnl:,.0f} if IV converges; "
                                              f"target {target:+.0f}, now {cur_n:+.0f}", orders))
                else:
                    # reducing: close existing positions, largest first
                    orders = []
                    budget = 2 * n  # one straddle = 2 contracts
                    for r in sorted(st["rows"], key=lambda r: -abs(r["pos"])):
                        if budget <= 0 or r["pos"] == 0:
                            break
                        if (r["pos"] > 0) == (cur_n > 0):
                            q = min(abs(r["pos"]), budget, MAX_OPT_ORDER)
                            orders.append((r["t"], "SELL" if r["pos"] > 0 else "BUY", q))
                            budget -= q
                    if orders:
                        why = "edge gone" if target == 0 else "edge shrank/flipped"
                        if tick >= ENDGAME_TICK:
                            why = "endgame: cut gamma"
                        out.append(Suggestion(1, f"REDUCE options exposure ({why})",
                                              f"vol edge {edge:+.1f} pts; target {target:+.0f} straddles, now {cur_n:+.0f}",
                                              orders))

        # 3) Put-call parity arbitrage (hold to expiry, riskless)
        rtm_bid, rtm_ask = S - 0.01, S + 0.01
        for K in STRIKES:
            c, p = rows.get(f"RTM{K}C"), rows.get(f"RTM{K}P")
            if not c or not p:
                continue
            fees = 2 * OPT_FEE + MULT * RTM_FEE
            conv = MULT * (c["bid"] - p["ask"] + K - rtm_ask) - fees     # sell C, buy P, buy stock
            rev = MULT * (p["bid"] - c["ask"] + rtm_bid - K) - fees      # buy C, sell P, short stock
            if conv > PARITY_MIN_PROFIT:
                out.append(Suggestion(2, f"PARITY ARB K={K}: sell call, buy put, buy stock",
                                      f"${conv:.2f} per pair locked in at expiry",
                                      [(c["t"], "SELL", 10), (p["t"], "BUY", 10), ("RTM", "BUY", 1000)]))
            if rev > PARITY_MIN_PROFIT:
                out.append(Suggestion(2, f"PARITY ARB K={K}: buy call, sell put, short stock",
                                      f"${rev:.2f} per pair locked in at expiry",
                                      [(c["t"], "BUY", 10), (p["t"], "SELL", 10), ("RTM", "SELL", 1000)]))

        # 4) Butterfly convexity check on calls: C(K-1) - 2C(K) + C(K+1) >= 0
        for K in STRIKES[1:-1]:
            a, b, d = rows.get(f"RTM{K-1}C"), rows.get(f"RTM{K}C"), rows.get(f"RTM{K+1}C")
            if a and b and d:
                cost = (a["ask"] - 2 * b["bid"] + d["ask"]) * MULT + 4 * OPT_FEE
                if cost < -PARITY_MIN_PROFIT:
                    out.append(Suggestion(2, f"BUTTERFLY ARB around K={K}",
                                          f"get paid ${-cost:.2f} for a payoff that is never negative",
                                          [(a["t"], "BUY", 10), (b["t"], "SELL", 20), (d["t"], "BUY", 10)]))

        if tick >= ENDGAME_TICK:
            out.append(Suggestion(3, "ENDGAME: no new positions", "gamma is exploding; keep |delta| small", []))
        return sorted(out, key=lambda s: s.priority)

    def _fit_limits(self, n, action):
        """Shrink n (straddles = 2 contracts each) until options net/gross limits hold."""
        st = self.state
        sign = 1 if action == "BUY" else -1
        while n > 0:
            if abs(st["opt_net"] + sign * 2 * n) <= OPT_NET_LIMIT and st["opt_gross"] + 2 * n <= OPT_GROSS_LIMIT:
                return n
            n -= 1
        return 0

    # ---- execution ----------------------------------------------------- #
    def execute(self, idx):
        if not self.allow_orders:
            self.messages.append("Orders disabled: restart with --allow-orders")
            return
        if idx < 1 or idx > len(self.suggestions):
            self.messages.append(f"No suggestion #{idx}")
            return
        sug = self.suggestions[idx - 1]
        for ticker, action, qty in sug.orders:
            try:
                code, resp = self.api.post("/orders", ticker=ticker, type="MARKET", quantity=qty, action=action)
            except OSError as e:   # not retried (could double-fill); stop and let the user re-check
                self.messages.append(f"ERR {action} {qty} {ticker}: connection problem ({e}); stopped")
                break
            if code == 200:
                self.messages.append(f"OK  {action} {qty} {ticker} @ {resp.get('vwap')}")
            else:
                self.messages.append(f"ERR {action} {qty} {ticker}: {resp}")
        self.messages = self.messages[-6:]

    # ---- display ------------------------------------------------------- #
    def render(self):
        st, m = self.state, self.model
        def pct(x): return f"{x:6.1%}" if x is not None else "   n/a"
        L = []
        week = min(4, (st["tick"] - 1) // WEEK + 1) if st["tick"] >= 1 else 1
        L.append(f"RIT VOL SCANNER  tick {st['tick']:3d}/{TICKS}  week {week}  T={st['T']*240:4.1f} days  "
                 f"status {st['status']}   NLV {st['nlv'] if st['nlv'] is not None else 'n/a'}"
                 + (f"  (mock penalty {st['penalty']})" if st.get("penalty") else ""))
        rng = f" range {m.next_range[0]:.0%}-{m.next_range[1]:.0%}" if m.next_range else ""
        L.append(f"RTM {st['S']:.2f} | σ this wk {pct(m.now())}  σ next wk {pct(m.nxt())}{rng}  "
                 f"→ σ life {pct(st['sig_life'])}  | ATM({st['atm']}) IV {pct(st['atm_iv'])}  "
                 + (f"EDGE {st['edge']:+.1f} vol pts" if st['edge'] is not None else "EDGE n/a"))
        dl = st["delta"]
        band = st.get("band", HEDGE_BAND)
        flag = "!!! OVER LIMIT" if abs(dl) > DELTA_LIMIT else ("! hedge" if abs(dl) > band else "ok")
        L.append(f"DELTA {dl:+9,.0f}  [{flag}] band ±{band:,.0f}  1s drift ~{st.get('drift', 0):,.0f}  "
                 f"size cap {st.get('n_cap', 0):,.0f} straddles")
        L.append(f"gamma {st['gamma']:+8,.0f}/$   vega ${st['vega']:+9,.0f}/pt   "
                 f"RTM pos {st['rtm_pos']:+,}   options net {st['opt_net']:+}/{OPT_NET_LIMIT} gross {st['opt_gross']}/{OPT_GROSS_LIMIT}")
        L.append("")
        L.append(f"{'Ticker':8} {'Pos':>5} {'Bid':>6} {'Ask':>6} {'IV':>7} {'Fair':>7} {'BuyEdge$':>9} {'SellEdge$':>9} "
                 f"{'Delta':>6} {'Vega$':>6}  Signal")
        for r in st["rows"]:
            sig = ""
            if r["buy"] is not None and r["buy"] > 5:
                sig = "CHEAP → buy"
            elif r["sell"] is not None and r["sell"] > 5:
                sig = "RICH → sell"
            L.append(f"{r['t']:8} {r['pos']:>5} {r['bid']:6.2f} {r['ask']:6.2f} {pct(r['iv']):>7} "
                     f"{(r['fair'] or 0):7.3f} {(r['buy'] or 0):9.1f} {(r['sell'] or 0):9.1f} "
                     f"{r['d']:6.2f} {r['v']*MULT:6.2f}  {sig}")
        L.append("")
        L.append("SUGGESTED ACTIONS" + ("" if self.allow_orders else "   (read-only: start with --allow-orders to execute)"))
        if not self.suggestions:
            L.append("  (nothing worth doing: hold)")
        for i, s in enumerate(self.suggestions, 1):
            L.append(f" [{i}] {s.title}")
            L.append(f"      {s.reason}")
            if s.orders:
                L.append("      orders: " + ", ".join(f"{a} {q} {t}" for t, a, q in s.orders[:6])
                         + (" ..." if len(s.orders) > 6 else ""))
        L.append("")
        L.append("news: " + " | ".join(m.log[-4:]))
        for msg in self.messages[-4:]:
            L.append("> " + msg)
        L.append("cmd: number=execute  s <pct>=this-week σ  n <pct>=next-week σ  c=clear  q=quit")
        sys.stdout.write("\033[2J\033[H" + "\n".join(L) + "\n")
        sys.stdout.flush()

    def input_loop(self):
        while self.running:
            try:
                line = input().strip().lower()
            except EOFError:
                return
            try:
                if line == "q":
                    self.running = False
                elif line == "c":
                    self.model.override_now = self.model.override_next = None
                elif line.startswith("s "):
                    self.model.override_now = float(line[2:]) / 100
                elif line.startswith("n "):
                    self.model.override_next = float(line[2:]) / 100
                elif line.isdigit():
                    self.execute(int(line))
            except ValueError:
                self.messages.append(f"could not parse '{line}'")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--key", required=True, help="API key shown in the RIT Client")
    ap.add_argument("--refresh", type=float, default=0.5, help="seconds between refreshes")
    ap.add_argument("--allow-orders", action="store_true", help="let numbered commands send MARKET orders")
    ap.add_argument("--auto-hedge", action="store_true",
                    help="automatically execute delta-hedge suggestions (implies --allow-orders)")
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit (for testing)")
    a = ap.parse_args()
    if a.auto_hedge:
        a.allow_orders = True
    if os.name == "nt":
        os.system("")  # enable ANSI escape codes in Windows terminals
    try:  # piped/redirected output on Windows defaults to cp1252, which can't print σ → ±
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    sc = Scanner(RitClient(a.host, a.port, a.key), a.allow_orders)
    if not a.once:
        threading.Thread(target=sc.input_loop, daemon=True).start()
    while sc.running:
        try:
            sc.analyse(*sc.poll())
            if a.auto_hedge and sc.suggestions and "HEDGE" in sc.suggestions[0].title:
                sc.execute(1)
                sc.analyse(*sc.poll())
            sc.render()
        except (OSError, http.client.HTTPException) as e:
            print(f"connection problem ({e}); is the RIT Client running with the API enabled? retrying...")
            time.sleep(1)
        if a.once:
            break
        time.sleep(a.refresh)


if __name__ == "__main__":
    main()
