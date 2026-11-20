#!/usr/bin/env python3
"""
Mock RIT Client REST API, for developing and testing offline.

It imitates the endpoints of the real RIT Client (http://localhost:9999/v1/...)
for the two RITCxSmith cases. The market model is a simple stand-in, NOT the
real case. Use it to check that your code runs, parses and handles errors.
Then tune strategy on the real practice server.

    python3 mock_rit_server.py --case vol            # Volatility Trading Case
    python3 mock_rit_server.py --case etf            # Algorithmic ETF Arbitrage Case
    python3 mock_rit_server.py --case etf --tick-seconds 0.2 --loop

Standard library only. API key defaults to TESTKEY (header X-API-Key).
"""
import argparse
import json
import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

TICKS = 300


# --------------------------------------------------------------------------- #
# Black-Scholes (r = 0) for the vol case
# --------------------------------------------------------------------------- #
def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(S, K, T, sig, kind):
    if T <= 0 or sig <= 0:
        return max(0.0, S - K) if kind == "C" else max(0.0, K - S)
    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if kind == "C":
        return S * ncdf(d1) - K * ncdf(d2)
    return K * ncdf(-d2) - S * ncdf(-d1)


# --------------------------------------------------------------------------- #
# Case state
# --------------------------------------------------------------------------- #
class Market:
    def __init__(self, case, tick_seconds, rate_limit):
        self.case = case
        self.tick_seconds = tick_seconds
        self.rate_limit = rate_limit
        self.lock = threading.RLock()
        self.reset()

    # ---- common ---------------------------------------------------------- #
    def reset(self):
        self.t0 = time.time()
        self.tick = 0
        self.status = "ACTIVE"
        self.positions = {}
        self.cash = 0.0  # CAD
        self.next_order_id = 1
        self.open_orders = {}
        self.news = []
        self.next_news_id = 1
        self.tenders = {}
        self.next_tender_id = 1
        self.order_times = {}
        self.penalty = 0.0
        self.fees = 0.0
        if self.case == "vol":
            self._init_vol()
        else:
            self._init_etf()
        self.advance()

    def add_news(self, headline, body, ticker=""):
        self.news.insert(0, {"news_id": self.next_news_id, "period": 1, "tick": self.tick,
                             "ticker": ticker, "headline": headline, "body": body})
        self.next_news_id += 1

    def advance(self):
        """Catch the simulation up to wall-clock time."""
        target = min(TICKS, 1 + int((time.time() - self.t0) / self.tick_seconds))
        while self.tick < target:
            self.tick += 1
            if self.case == "vol":
                self._step_vol()
            else:
                self._step_etf()
        if self.tick >= TICKS and (time.time() - self.t0) / self.tick_seconds >= TICKS:
            if self.status == "ACTIVE":
                self.status = "STOPPED"
                self._close_out()

    # ---- volatility case ------------------------------------------------- #
    def _init_vol(self):
        self.strikes = [48, 49, 50, 51, 52]
        self.S = 50.0
        self.week_sig = [random.choice([0.15, 0.18, 0.20, 0.22, 0.25, 0.28, 0.30, 0.35, 0.40])
                         for _ in range(4)]
        self.iv = self.week_sig[0] + random.choice([-1, 1]) * random.uniform(0.03, 0.08)
        self.tickers = ["RTM"] + [f"RTM{k}{c}" for k in self.strikes for c in "CP"]

    def _sig_now(self):
        return self.week_sig[min(3, (self.tick - 1) // 75)]

    def _step_vol(self):
        t = self.tick
        week = min(3, (t - 1) // 75)
        if t > 1:
            self.S *= 1.0 + random.gauss(0.0, self._sig_now() / 60.0)  # 3600 ticks/yr
        self.iv += 0.04 * (self._sig_now() - self.iv)                  # MM learns slowly
        if (t - 1) % 75 == 0:
            self.add_news(f"Week {week + 1} volatility",
                          f"The realized volatility of RTM for this week will be "
                          f"{round(self.week_sig[week] * 100)}%")
        if (t - 1) % 75 == 36 and week < 3:
            nxt = round(self.week_sig[week + 1] * 100)
            lo = nxt - random.randint(0, 3)
            self.add_news(f"Week {week + 2} volatility forecast",
                          f"The realized volatility of RTM for next week will be between "
                          f"{lo}% and {lo + 3}%")
        self._track_delta_penalty()

    def _vol_quote(self, ticker):
        if ticker == "RTM":
            bid = round(self.S - 0.01, 2)
            return bid, round(bid + 0.02, 2)
        K = int(ticker[3:5]); kind = ticker[5]
        T = max(0.0, (TICKS - self.tick) / 3600.0)
        mid = bs(self.S, K, T, self.iv, kind)
        bid = max(0.0, round(mid - 0.01, 2))
        return bid, round(bid + 0.02, 2)

    def _vol_delta(self):
        T = max(1e-9, (TICKS - self.tick) / 3600.0)
        d = self.positions.get("RTM", 0)
        for k in self.strikes:
            d1 = (math.log(self.S / k) + 0.5 * self.iv ** 2 * T) / (self.iv * math.sqrt(T))
            d += self.positions.get(f"RTM{k}C", 0) * 100 * ncdf(d1)
            d += self.positions.get(f"RTM{k}P", 0) * 100 * (ncdf(d1) - 1)
        return d

    def _track_delta_penalty(self):
        over = abs(self._vol_delta()) - 7000
        if over > 0:
            self.penalty += over * 0.10

    # ---- ETF case -------------------------------------------------------- #
    def _init_etf(self):
        self.mid = {"BULL": 10.0, "BEAR": 15.0, "USD": 1.0}
        self.dev = 0.0  # RITC premium/discount vs fair
        self.depth = {"BULL": 6000, "BEAR": 6000, "RITC": 4000}
        self.tickers = ["CAD", "USD", "BULL", "BEAR", "RITC"]
        self.mid["RITC"] = (self.mid["BULL"] + self.mid["BEAR"]) / self.mid["USD"]

    def _step_etf(self):
        if self.tick > 1:
            self.mid["BULL"] *= 1 + random.gauss(0, 0.0025)
            self.mid["BEAR"] *= 1 + random.gauss(0, 0.0035)
            self.mid["USD"] *= 1 + random.gauss(0, 0.0004)
        self.dev *= 0.93
        if random.random() < 0.03:
            self.dev += random.choice([-1, 1]) * random.uniform(0.004, 0.012)
        fair = (self.mid["BULL"] + self.mid["BEAR"]) / self.mid["USD"]
        self.mid["RITC"] = fair * (1 + self.dev)
        # resting limit orders fill when the market trades through them
        for oid, o in list(self.open_orders.items()):
            bid, ask = self._etf_bbo(o["ticker"])
            if (o["action"] == "BUY" and ask <= o["price"]) or \
               (o["action"] == "SELL" and bid >= o["price"]):
                self._fill(o["ticker"], o["action"], o["quantity"] - o["quantity_filled"],
                           o["price"], rebate=True)
                o["quantity_filled"] = o["quantity"]; o["status"] = "TRANSACTED"
                del self.open_orders[oid]
        # tenders
        for tid, td in list(self.tenders.items()):
            if self.tick > td["expires"]:
                del self.tenders[tid]
        if self.tick % 25 == 5 and self.tick < 280:
            action = random.choice(["BUY", "SELL"])
            qty = random.choice([20000, 30000, 40000, 50000, 60000])
            edge = random.uniform(-0.006, 0.012)  # >0 means good for you
            m = self.mid["RITC"]
            price = round(m * (1 - edge) if action == "BUY" else m * (1 + edge), 2)
            fixed = random.random() < 0.8
            self.tenders[self.next_tender_id] = {
                "tender_id": self.next_tender_id, "period": 1, "tick": self.tick,
                "expires": self.tick + 5, "caption": f"Institution offers: you {action} {qty} RITC",
                "quantity": qty, "action": action, "is_fixed_bid": fixed,
                "price": price if fixed else None, "ticker": "RITC"}
            self.next_tender_id += 1

    def _etf_bbo(self, ticker):
        m = self.mid[ticker]
        if ticker == "USD":
            return round(m - 0.0001, 4), round(m + 0.0001, 4)
        half = 0.01 if ticker != "RITC" else 0.02
        return round(m - half, 2), round(m + half, 2)

    def _etf_book(self, ticker, limit=10):
        bid, ask = self._etf_bbo(ticker)
        step = 0.0001 if ticker == "USD" else 0.01
        d = self.depth.get(ticker, 2_000_000)
        bids, asks = [], []
        rnd = random.Random(self.tick * 7 + hash(ticker) % 1000)
        for i in range(limit):
            q1 = int(d * rnd.uniform(0.5, 1.5)); q2 = int(d * rnd.uniform(0.5, 1.5))
            bids.append({"price": round(bid - i * step, 4), "quantity": q1, "quantity_filled": 0,
                         "ticker": ticker, "type": "LIMIT", "action": "BUY", "status": "OPEN"})
            asks.append({"price": round(ask + i * step, 4), "quantity": q2, "quantity_filled": 0,
                         "ticker": ticker, "type": "LIMIT", "action": "SELL", "status": "OPEN"})
        return bids, asks

    # ---- trading --------------------------------------------------------- #
    def bbo(self, ticker):
        return self._vol_quote(ticker) if self.case == "vol" else self._etf_bbo(ticker)

    def _fx(self, ticker):
        return self.mid["USD"] if (self.case == "etf" and ticker == "RITC") else 1.0

    def _fill(self, ticker, action, qty, price, rebate=False):
        sign = 1 if action == "BUY" else -1
        self.positions[ticker] = self.positions.get(ticker, 0) + sign * qty
        mult = 100 if (self.case == "vol" and ticker != "RTM") else 1
        if self.case == "etf" and ticker == "RITC":
            # USD-priced: cash flow lands in the USD currency position
            self.positions["USD"] = self.positions.get("USD", 0) - sign * qty * price
        elif self.case == "etf" and ticker == "USD":
            self.cash -= sign * qty * price
        else:
            self.cash -= sign * qty * price * mult
        if self.case == "vol":
            fee = qty * (2.0 if ticker != "RTM" else 0.02)
        elif ticker == "USD":
            fee = 0.0
        else:
            fee = -0.01 * qty if rebate else 0.02 * qty
        self.cash -= fee
        self.fees += fee

    def limits_check(self, ticker, action, qty):
        pos = dict(self.positions)
        pos[ticker] = pos.get(ticker, 0) + (qty if action == "BUY" else -qty)
        if self.case == "vol":
            opt = [pos.get(t, 0) for t in self.tickers if t != "RTM"]
            if abs(pos.get("RTM", 0)) > 50000: return "RTM limit exceeded"
            if sum(abs(x) for x in opt) > 2500: return "Options gross limit exceeded"
            if abs(sum(opt)) > 1000: return "Options net limit exceeded"
        else:
            g = abs(pos.get("BULL", 0)) + abs(pos.get("BEAR", 0)) + 2 * abs(pos.get("RITC", 0))
            n = pos.get("BULL", 0) + pos.get("BEAR", 0) + 2 * pos.get("RITC", 0)
            if g > 300000: return "Gross limit exceeded"
            if abs(n) > 200000: return "Net limit exceeded"
        return None

    def place(self, ticker, otype, qty, action, price):
        if ticker not in self.tickers or ticker == "CAD":
            return 400, {"code": "INVALID", "message": f"Unknown ticker {ticker}"}
        maxq = 2_500_000 if ticker in ("USD", "CAD") else (100 if (self.case == "vol" and ticker != "RTM") else 10000)
        if qty <= 0 or qty > maxq:
            return 400, {"code": "INVALID_QUANTITY", "message": f"Quantity must be 1..{maxq}"}
        err = None if ticker == "USD" else self.limits_check(ticker, action, qty)
        if err:
            return 400, {"code": "LIMIT", "message": err}
        oid = self.next_order_id; self.next_order_id += 1
        order = {"order_id": oid, "period": 1, "tick": self.tick, "trader_id": "mock",
                 "ticker": ticker, "type": otype, "quantity": qty, "action": action,
                 "price": price, "quantity_filled": 0, "vwap": None, "status": "OPEN"}
        bid, ask = self.bbo(ticker)
        if otype == "MARKET" or (action == "BUY" and price >= ask) or (action == "SELL" and price <= bid):
            if self.case == "etf" and ticker != "USD":
                bids, asks = self._etf_book(ticker)
                levels = asks if action == "BUY" else bids
                left, cost = qty, 0.0
                for lv in levels:
                    take = min(left, lv["quantity"]); cost += take * lv["price"]; left -= take
                    if left == 0: break
                filled = qty - left
                vwap = cost / filled
                self._fill(ticker, action, filled, vwap)
                # permanent price impact
                impact = (filled / 100000.0) * 0.01
                self.mid[ticker] *= (1 + impact) if action == "BUY" else (1 - impact)
            else:
                filled, vwap = qty, (ask if action == "BUY" else bid)
                self._fill(ticker, action, filled, vwap)
            order.update(quantity_filled=filled, vwap=round(vwap, 4), status="TRANSACTED")
        else:
            self.open_orders[oid] = order
        return 200, order

    def _close_out(self):
        for t, q in list(self.positions.items()):
            if q == 0 or t in ("USD", "CAD"): continue
            if self.case == "vol" and t != "RTM":
                K = int(t[3:5]); payoff = max(0, self.S - K) if t[5] == "C" else max(0, K - self.S)
                self.cash += q * payoff * 100
            elif self.case == "etf" and t == "RITC":
                fair = (self.mid["BULL"] + self.mid["BEAR"]) / self.mid["USD"]
                self.positions["USD"] = self.positions.get("USD", 0) + q * fair
            else:
                price = self.S if self.case == "vol" else self.mid[t]
                self.cash += q * price
            self.positions[t] = 0
        if self.case == "etf":
            self.cash += self.positions.get("USD", 0) * self.mid["USD"]
            self.positions["USD"] = 0

    def nlv(self):
        total = self.cash
        for t, q in self.positions.items():
            if q == 0 or t == "CAD": continue
            if self.case == "vol":
                bid, ask = self.bbo(t)
                total += q * (bid + ask) / 2 * (1 if t == "RTM" else 100)
            elif t == "USD":
                total += q * self.mid["USD"]
            else:
                total += q * self.mid[t] * self._fx(t)
        return total - (self.penalty if self.case == "vol" else 0)

    def securities(self):
        out = []
        for t in self.tickers:
            if t == "CAD":
                bid = ask = 1.0
            else:
                bid, ask = self.bbo(t)
            out.append({"ticker": t, "type": ("CURRENCY" if t in ("CAD", "USD") else
                                              ("OPTION" if self.case == "vol" and t != "RTM" else
                                               ("ETF" if t == "RITC" else "STOCK"))),
                        "position": self.positions.get(t, 0), "bid": bid, "ask": ask,
                        "last": round((bid + ask) / 2, 4), "bid_size": 10000, "ask_size": 10000,
                        "currency": ("USD" if t == "RITC" else "CAD"),
                        "unit_multiplier": 100 if (self.case == "vol" and t != "RTM") else 1,
                        "max_trade_size": 100 if (self.case == "vol" and t != "RTM") else 10000,
                        "api_orders_per_second": self.rate_limit, "is_tradeable": t != "CAD",
                        "nlv": 0, "realized": 0, "unrealized": 0, "vwap": 0})
        return out


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
M = None
API_KEY = "TESTKEY"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, like the real client

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj, extra_headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _auth(self, q):
        key = self.headers.get("X-API-Key") or self.headers.get("X-API-key") or q.get("key", [None])[0]
        if key != API_KEY:
            self._send(401, {"code": "UNAUTHORIZED", "message": "API key is invalid"})
            return False
        return True

    def _route(self, method):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path.rstrip("/")
        g = lambda k, d=None: q.get(k, [d])[0]
        if "Content-Length" in self.headers:
            self.rfile.read(int(self.headers["Content-Length"]))
        if not self._auth(q):
            return
        with M.lock:
            M.advance()
            if method == "GET" and p == "/v1/case":
                return self._send(200, {"name": f"Mock {M.case.upper()} case", "period": 1,
                                        "tick": M.tick, "ticks_per_period": TICKS,
                                        "total_periods": 1, "status": M.status,
                                        "is_enforce_trading_limits": True})
            if method == "GET" and p == "/v1/trader":
                return self._send(200, {"trader_id": "mock", "first_name": "Mock",
                                        "last_name": "Trader", "nlv": round(M.nlv(), 2),
                                        "mock_penalty": round(M.penalty, 2),
                                        "mock_fees": round(M.fees, 2)})
            if method == "GET" and p == "/v1/limits":
                if M.case == "vol":
                    pos = M.positions
                    opt = [pos.get(t, 0) for t in M.tickers if t != "RTM"]
                    return self._send(200, [
                        {"name": "RTM", "gross": abs(pos.get("RTM", 0)), "net": pos.get("RTM", 0),
                         "gross_limit": 50000, "net_limit": 50000, "gross_fine": 0, "net_fine": 0},
                        {"name": "OPTIONS", "gross": sum(map(abs, opt)), "net": sum(opt),
                         "gross_limit": 2500, "net_limit": 1000, "gross_fine": 0, "net_fine": 0}])
                pos = M.positions
                g_ = abs(pos.get("BULL", 0)) + abs(pos.get("BEAR", 0)) + 2 * abs(pos.get("RITC", 0))
                n_ = pos.get("BULL", 0) + pos.get("BEAR", 0) + 2 * pos.get("RITC", 0)
                return self._send(200, [{"name": "LIMIT-STOCK", "gross": g_, "net": n_,
                                         "gross_limit": 300000, "net_limit": 200000,
                                         "gross_fine": 0, "net_fine": 0}])
            if method == "GET" and p == "/v1/securities":
                s = M.securities(); t = g("ticker")
                return self._send(200, [x for x in s if not t or x["ticker"] == t])
            if method == "GET" and p == "/v1/securities/book":
                t = g("ticker"); lim = int(g("limit", "10"))
                if t not in M.tickers:
                    return self._send(400, {"code": "INVALID", "message": "Unknown ticker"})
                if M.case == "etf":
                    bids, asks = M._etf_book(t, lim)
                else:
                    b, a = M.bbo(t)
                    bids = [{"price": b, "quantity": 100000, "quantity_filled": 0}]
                    asks = [{"price": a, "quantity": 100000, "quantity_filled": 0}]
                return self._send(200, {"bids": bids, "asks": asks})
            if method == "GET" and p == "/v1/news":
                after = int(g("after", g("since", "0")))
                lim = int(g("limit", "20"))
                return self._send(200, [n for n in M.news if n["news_id"] > after][:lim])
            if method == "GET" and p == "/v1/tenders":
                return self._send(200, list(M.tenders.values()))
            if p.startswith("/v1/tenders/"):
                tid = int(p.split("/")[-1])
                td = M.tenders.get(tid)
                if not td:
                    return self._send(400, {"code": "INVALID", "message": "Tender not found or expired"})
                if method == "DELETE":
                    del M.tenders[tid]
                    return self._send(200, {"success": True})
                if method == "POST":
                    price = td["price"]
                    if not td["is_fixed_bid"]:
                        if g("price") is None:
                            return self._send(400, {"code": "INVALID", "message": "price required"})
                        price = float(g("price"))
                        m = M.mid["RITC"]
                        ok = price <= m * 0.998 if td["action"] == "BUY" else price >= m * 1.002
                        del M.tenders[tid]
                        if not ok:
                            return self._send(200, {"success": True, "mock_note": "bid lost"})
                    else:
                        del M.tenders[tid]
                    M._fill("RITC", td["action"], td["quantity"], price)
                    return self._send(200, {"success": True})
            if method == "POST" and p == "/v1/orders":
                t = g("ticker")
                now = time.time()
                times = [x for x in M.order_times.get(t, []) if now - x < 1.0]
                if len(times) >= M.rate_limit:
                    wait = round(1.0 - (now - times[0]), 3)
                    return self._send(429, {"code": "TOO_MANY_REQUESTS",
                                            "message": "Rate limit exceeded", "wait": wait},
                                      {"Retry-After": str(wait)})
                times.append(now); M.order_times[t] = times
                if M.status != "ACTIVE":
                    return self._send(400, {"code": "CASE_STOPPED", "message": "Case is not active"})
                try:
                    qty = int(float(g("quantity", "0")))
                    price = float(g("price")) if g("price") else None
                except ValueError:
                    return self._send(400, {"code": "INVALID", "message": "bad number"})
                otype = (g("type") or "").upper(); action = (g("action") or "").upper()
                if otype not in ("MARKET", "LIMIT") or action not in ("BUY", "SELL") or \
                        (otype == "LIMIT" and price is None):
                    return self._send(400, {"code": "INVALID", "message": "bad order params"})
                code, obj = M.place(t, otype, qty, action, price)
                return self._send(code, obj)
            if method == "GET" and p == "/v1/orders":
                return self._send(200, list(M.open_orders.values()))
            if method == "DELETE" and p.startswith("/v1/orders/"):
                oid = int(p.split("/")[-1])
                ok = M.open_orders.pop(oid, None) is not None
                return self._send(200 if ok else 400, {"success": ok})
            if method == "POST" and p == "/v1/commands/cancel":
                t = g("ticker"); ids = g("ids")
                if g("all") == "1":
                    sel = list(M.open_orders)
                elif ids:
                    sel = [int(x) for x in ids.split(",") if int(x) in M.open_orders]
                elif t:
                    sel = [k for k, o in M.open_orders.items() if o["ticker"] == t]
                else:
                    sel = []
                for k in sel:
                    del M.open_orders[k]
                return self._send(200, {"cancelled_order_ids": sel})
        self._send(404, {"code": "NOT_FOUND", "message": f"{method} {p} not mocked"})

    def do_GET(self): self._route("GET")
    def do_POST(self): self._route("POST")
    def do_DELETE(self): self._route("DELETE")


def main():
    global M, API_KEY
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", choices=["vol", "etf"], required=True)
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--key", default="TESTKEY")
    ap.add_argument("--tick-seconds", type=float, default=1.0, help="seconds per tick (1.0 = real speed)")
    ap.add_argument("--rate-limit", type=int, default=10, help="orders per second per ticker")
    ap.add_argument("--loop", action="store_true", help="restart a new heat 5 s after each one ends")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()
    if a.seed is not None:
        random.seed(a.seed)
    API_KEY = a.key
    M = Market(a.case, a.tick_seconds, a.rate_limit)
    if M.case == "vol":
        print("Mock vol case. Weekly sigmas (secret!):", [round(s, 2) for s in M.week_sig])
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Mock RIT ({a.case}) on http://localhost:{a.port}/v1  key={a.key}  Ctrl+C to stop")
    reported = False
    try:
        while True:
            time.sleep(0.5)
            with M.lock:
                M.advance()
                if M.status == "STOPPED" and not reported:
                    print(f"Heat over. Final NLV {M.nlv():,.2f} CAD | fees {M.fees:,.2f}"
                          + (f" | delta penalty {M.penalty:,.2f}" if M.case == 'vol' else ""), flush=True)
                    reported = True
                    if a.loop:
                        time.sleep(5)
                        M.reset()
                        reported = False
                        print("New heat started.", flush=True)
    except KeyboardInterrupt:
        pass
    srv.shutdown()


if __name__ == "__main__":
    main()
