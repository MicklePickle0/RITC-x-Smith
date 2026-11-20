"""
Shared RIT Client REST API wrapper (http://localhost:9999/v1). Standard library only, Python 3.8+.

Used by vol_scanner.py and etf_arb.py.

Safety rules (do not loosen):
  * GET:          on a network error (usually a stale keep-alive socket) reconnect and retry ONCE.
  * POST/DELETE:  NEVER retried after a network error. The request may have reached the server,
                  so a retry could send an order twice. Callers re-read positions instead.
  * HTTP 429:     sleep for the JSON `wait` field (or the Retry-After header) and retry. That is
                  safe for every method, because the server rejected the request.
  * HTTP 401:     before any request has succeeded: raises SystemExit with a clear "wrong API key"
                  message. AFTER the key has worked once: the RIT Client also answers 401 a few
                  seconds after every heat ends (seen on the practice server), so it is treated as
                  "case not running": the call fails (None) and callers keep waiting.

Two layers:
  * request()/get()/post()/delete() return (status, data) and raise RitConnectionError (an
    OSError) when the server can't be reached.
  * Typed helpers (case(), securities(), book(), order(), ...) mirror rit.c: they return the
    parsed result, or None on any failure (with the reason in .last_error).
"""
import http.client
import json
import math
import socket
import sys
import threading
import time
from urllib.parse import urlencode

NAN = float("nan")


class RitConnectionError(OSError):
    """The RIT Client could not be reached (not running, API disabled, wrong host/port...)."""


def num(x, default=NAN):
    """JSON number -> float. null, missing or garbage -> default (like json_num in json.c)."""
    try:
        return default if x is None else float(x)
    except (TypeError, ValueError):
        return default


def walk(levels, qty, buying, penalty):
    """Average price to fill `qty` against `levels` [(price, qty), ...] (asks to buy, bids to sell).
    If the book is too thin, the remainder is priced at the last level +/- `penalty` per unit."""
    if qty <= 0 or not levels:
        return NAN
    cost, left = 0.0, qty
    for px, q in levels:
        if left <= 0:
            break
        take = min(left, q)
        cost += take * px
        left -= take
    if left > 0:
        worst = levels[-1][0] + (penalty if buying else -penalty)
        cost += left * worst
    return cost / qty


class RitClient:
    MAX_ATTEMPTS = 5          # total tries per call when rate limited (as in rit.c)
    DEFAULT_WAIT = 0.25       # seconds, if a 429 carries neither `wait` nor Retry-After

    def __init__(self, host="localhost", port=9999, key="", timeout=3.0, verbose=False):
        self.host, self.port, self.key = host, int(port), key
        self.timeout = timeout
        self.verbose = verbose
        self.conn = None
        self._addr = None              # resolved IP of the last successful connect
        self.lock = threading.Lock()   # one socket; vol_scanner calls from two threads
        self.requests = 0
        self.rate_limited = 0
        self.last_error = ""
        self.key_ok = False            # True once any request got 200 with this key

    def close(self):
        """Close the kept-alive socket (a later call simply reconnects)."""
        with self.lock:
            self._close()

    # ------------------------------------------------------------------ transport
    def _close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def _connect(self):
        # Reconnect to the IP that worked last time: on Windows "localhost" tries ::1 first and a
        # refused connect costs ~2 s, which would stall every reconnect if the server is IPv4-only.
        target = self._addr or self.host
        conn = http.client.HTTPConnection(target, self.port, timeout=self.timeout)
        try:
            conn.connect()
        except OSError:
            self._addr = None          # resolve the name again next time
            raise
        ip = conn.sock.getpeername()[0]
        self._addr = f"[{ip}]" if ":" in ip else ip
        try:   # no Nagle delay: every request is tiny and latency-sensitive
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        return conn

    def _send_once(self, method, url):
        """One HTTP exchange -> (status, retry_after_header, body).
        Raises on network errors, with .reused_socket set on the exception."""
        with self.lock:
            reused = self.conn is not None
            try:
                if self.conn is None:
                    self.conn = self._connect()
                self.conn.request(method, url, headers={"Host": f"{self.host}:{self.port}",
                                                        "X-API-Key": self.key, "Accept": "application/json"})
                r = self.conn.getresponse()
                body = r.read()
                if r.will_close:
                    self._close()
                return r.status, r.getheader("Retry-After"), body
            except (OSError, http.client.HTTPException) as e:
                self._close()
                e.reused_socket = reused
                raise

    def _exchange(self, method, url):
        try:
            return self._send_once(method, url)
        except (OSError, http.client.HTTPException) as e:
            if method != "GET":
                # May or may not have reached the server: never resend an order/accept/decline.
                raise RitConnectionError(f"{method} {url}: {e!r} (not retried)") from e
            if not getattr(e, "reused_socket", False):   # fresh connect failed: server is down
                raise RitConnectionError(f"{method} {url}: {e!r}") from e
        try:   # stale keep-alive socket: reconnect and retry a GET once
            return self._send_once(method, url)
        except (OSError, http.client.HTTPException) as e:
            raise RitConnectionError(f"{method} {url}: {e!r}") from e

    # ------------------------------------------------------------------ raw API
    def request(self, method, path, params=None):
        """-> (status, parsed JSON or None). `path` is relative to /v1, e.g. "/securities"."""
        url = "/v1" + path + ("?" + urlencode(params) if params else "")
        status, data = -1, None
        for attempt in range(self.MAX_ATTEMPTS):
            self.requests += 1
            status, retry_after, body = self._exchange(method, url)
            try:
                data = json.loads(body) if body else None
            except ValueError:
                data = None
            if status == 429:   # rate limited: wait as told, then retry
                self.rate_limited += 1
                wait = num(data.get("wait"), -1) if isinstance(data, dict) else -1
                if not wait >= 0:
                    wait = num(retry_after, self.DEFAULT_WAIT)
                if attempt < self.MAX_ATTEMPTS - 1:
                    time.sleep(max(0.0, wait) + 0.005)
                    continue
                self.last_error = f"{method} {url} -> 429 (still rate limited after {self.MAX_ATTEMPTS} tries)"
                return status, data
            if status == 401:
                if not self.key_ok:
                    raise SystemExit("401 Unauthorized: wrong API key? Use the key shown in the RIT Client "
                                     "(API icon in the bottom status bar).")
                # the key worked before: RIT answers 401 between heats -> fail this call, don't exit
                self.last_error = (f"{method} {url} -> 401 (heat over? the key worked before; "
                                   "if this lasts into the next heat, re-check the key)")
                return status, data
            if status == 200:
                self.key_ok = True
            if status != 200:
                msg = data.get("message", "") if isinstance(data, dict) else ""
                self.last_error = f"{method} {url} -> {status} {msg}"
                if self.verbose or status >= 400:
                    print(f"[api] {self.last_error}", file=sys.stderr)
            return status, data
        return status, data

    def get(self, path, **params):
        return self.request("GET", path, params or None)

    def post(self, path, **params):
        return self.request("POST", path, params or None)

    def delete(self, path, **params):
        return self.request("DELETE", path, params or None)

    # ------------------------------------------------------------------ typed helpers (None = failed)
    def _ok(self, method, path, params=None):
        try:
            status, data = self.request(method, path, params)
        except RitConnectionError as e:
            self.last_error = str(e)
            return None
        return data if status == 200 else None

    def case(self):
        """{tick, period, ticks_per_period, status, ...}"""
        d = self._ok("GET", "/case")
        return d if isinstance(d, dict) else None

    def securities(self, ticker=None):
        d = self._ok("GET", "/securities", {"ticker": ticker} if ticker else None)
        return d if isinstance(d, list) else None

    def book(self, ticker, limit=20):
        """-> (bids, asks), each [(price, available_qty), ...] best first; levels with nothing left
        (quantity - quantity_filled <= 0) are skipped. None on failure."""
        d = self._ok("GET", "/securities/book", {"ticker": ticker, "limit": limit})
        if not isinstance(d, dict):
            return None

        def levels(arr):
            out = []
            for e in arr or []:
                q = int(num(e.get("quantity"), 0)) - int(num(e.get("quantity_filled"), 0))
                if q > 0:
                    out.append((num(e.get("price")), q))
                if len(out) >= limit:
                    break
            return out
        return levels(d.get("bids")), levels(d.get("asks"))

    def tenders(self):
        d = self._ok("GET", "/tenders")
        return d if isinstance(d, list) else None

    def accept_tender(self, tender_id, price=None):
        """price=None for fixed-bid tenders; otherwise our bid (sent with 2 decimals)."""
        params = None if price is None or math.isnan(price) else {"price": "%.2f" % price}
        d = self._ok("POST", f"/tenders/{int(tender_id)}", params)
        return isinstance(d, dict) and bool(d.get("success", False))

    def decline_tender(self, tender_id):
        return self._ok("DELETE", f"/tenders/{int(tender_id)}") is not None

    def limits(self):
        d = self._ok("GET", "/limits")
        return d if isinstance(d, list) else None

    def trader(self):
        d = self._ok("GET", "/trader")
        return d if isinstance(d, dict) else None

    def order(self, ticker, action, qty, otype="MARKET", price=None):
        """-> {order_id, quantity_filled, vwap, status, ...} or None. LIMIT price sent with 4 decimals."""
        params = {"ticker": ticker, "type": otype, "quantity": int(qty), "action": action}
        if otype == "LIMIT":
            params["price"] = "%.4f" % price
        d = self._ok("POST", "/orders", params)
        return d if isinstance(d, dict) else None

    def cancel_all(self, ticker=None):
        return self._ok("POST", "/commands/cancel", {"ticker": ticker} if ticker else {"all": 1})
