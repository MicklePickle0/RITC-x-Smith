#!/usr/bin/env python3
"""
Tests for etf_arb.py and rit_client.py. Standard library only.

    cd rit-tools
    python -m unittest test_etf_arb -v          # everything (~40 s, includes one full mock heat)
    set SKIP_SLOW=1   (PowerShell: $env:SKIP_SLOW=1)   to skip the full-heat test

Three layers:
  1. Strategy unit tests: a FakeApi feeds the bot a hand-built market and records every order,
     accept and decline. Every expected number below is worked out by hand in the comments, so a
     failing test tells you which rule changed. (ETF_BOT.md uses the same worked examples.)
  2. Client tests: a real local HTTP server scripted to return 429s, 401s, dropped connections and
     stale keep-alive sockets, to prove the safety rules (POST/DELETE never resent, etc.).
  3. One full 300-tick heat against mock_rit_server.py, checking behaviour end to end.
"""
import contextlib
import csv
import io
import json
import math
import os
import random
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import etf_arb
from etf_arb import Bot, TickLog, depth_within
from rit_client import RitClient, walk


# =============================================================================================== #
# Fakes
# =============================================================================================== #
def ladder(bid, ask, n=8, step=0.01, qty=5000):
    """A book with n levels each side, `step` apart, `qty` shares per level."""
    bids = [(round(bid - i * step, 4), qty) for i in range(n)]
    asks = [(round(ask + i * step, 4), qty) for i in range(n)]
    return bids, asks


class FakeApi:
    """Stands in for RitClient. Default market:
         BULL 10.00 / 10.02   BEAR 15.00 / 15.02   RITC 25.00 / 25.04 (USD)   USD 1.0000 / 1.0000
       8 levels per side, 1 cent apart, 5,000 shares each. Limits gross 300,000 / net 200,000."""

    def __init__(self):
        self.requests = self.rate_limited = 0
        self.last_error = ""
        self.calls = []
        self.tick, self.status = 10, "ACTIVE"
        self.pos = {"BULL": 0, "BEAR": 0, "RITC": 0, "USD": 0}
        self.usd = (1.0, 1.0)
        self.books = {"BULL": ladder(10.00, 10.02), "BEAR": ladder(15.00, 15.02), "RITC": ladder(25.00, 25.04)}
        self.tender_list = []
        self.limit_list = [{"name": "LIMIT-STOCK", "gross_limit": 300000, "net_limit": 200000}]
        self.nlv = 1234.5

    # reads
    def case(self):
        return {"tick": self.tick, "status": self.status}

    def securities(self, ticker=None):
        out = [{"ticker": t, "position": p, "bid": 1, "ask": 1} for t, p in self.pos.items()]
        out.append({"ticker": "CAD", "position": 0, "bid": 1, "ask": 1})
        for s in out:
            if s["ticker"] == "USD":
                s["bid"], s["ask"] = self.usd
        return out

    def book(self, ticker, limit=20):
        return self.books[ticker]

    def tenders(self):
        return list(self.tender_list)

    def limits(self):
        return self.limit_list

    def trader(self):
        return {"nlv": self.nlv}

    # writes (recorded). Positions do NOT change: tests move self.pos by hand to play the server.
    report_fills = False   # True: order responses carry quantity_filled = qty (like the real server)
    reject = False         # True: every order fails, like a limit rejection

    def order(self, ticker, action, qty, otype="MARKET", price=None):
        self.calls.append(("order", ticker, action, qty, otype, price))
        if self.reject:
            self.last_error = "POST /v1/orders -> 500 exceeds gross trading limits"
            return None
        r = {"order_id": len(self.calls)}
        if self.report_fills:
            r["quantity_filled"] = qty
        return r

    def accept_tender(self, tid, price=None):
        self.calls.append(("accept", tid, price))
        return True

    def decline_tender(self, tid):
        self.calls.append(("decline", tid))
        return True

    def cancel_all(self, ticker=None):
        self.calls.append(("cancel_all",))
        return {"cancelled_order_ids": []}

    @property
    def orders(self):
        return [c[1:] for c in self.calls if c[0] == "order"]


def tender(tid=1, action="BUY", qty=10000, price=24.90, fixed=True, ticker="RITC"):
    return {"tender_id": tid, "ticker": ticker, "action": action, "quantity": qty,
            "price": price if fixed else None, "is_fixed_bid": fixed, "expires": 99, "caption": ""}


class BotCase(unittest.TestCase):
    """Builds a Bot on a FakeApi and silences its printing (kept in self.out)."""

    def setUp(self):
        self.api = FakeApi()
        self.bot = Bot(self.api, dry_run=False)
        self.bot.read_limits()
        self.out = io.StringIO()
        self._quiet = contextlib.redirect_stdout(self.out)
        self._quiet.__enter__()

    def tearDown(self):
        self._quiet.__exit__(None, None, None)

    def market(self):
        m = self.bot.snapshot()
        self.assertIsNotNone(m)
        return m

    def assertOrder(self, got, ticker, action, qty, otype, price=None):
        self.assertEqual(got[:4], (ticker, action, qty, otype), got)
        if price is None:
            self.assertIsNone(got[4])
        else:
            self.assertAlmostEqual(got[4], price, places=6)


# =============================================================================================== #
# 1. Strategy unit tests
# =============================================================================================== #
class TestHelpers(unittest.TestCase):
    def test_walk_average_price(self):
        # buy 150: 100 @ 10 + 50 @ 11 = 1550 -> 10.3333
        self.assertAlmostEqual(walk([(10, 100), (11, 100)], 150, True, 0.05), 1550 / 150)

    def test_walk_thin_book_penalty(self):
        # only 100 visible; the other 100 priced at last level +0.05 (buying) / -0.05 (selling)
        self.assertAlmostEqual(walk([(10, 100)], 200, True, 0.05), (1000 + 100 * 10.05) / 200)
        self.assertAlmostEqual(walk([(10, 100)], 200, False, 0.05), (1000 + 100 * 9.95) / 200)

    def test_walk_degenerate(self):
        self.assertTrue(math.isnan(walk([], 100, True, 0.05)))
        self.assertTrue(math.isnan(walk([(10, 100)], 0, True, 0.05)))

    def test_depth_within_band(self):
        bids, asks = ladder(25.00, 25.04)
        # selling into bids: 25.00 .. 24.96 are within 0.04 -> 5 levels x 5000
        self.assertEqual(depth_within(bids, 0.04, buying=False), 25000)
        # buying from asks: 25.04 .. 25.08
        self.assertEqual(depth_within(asks, 0.04, buying=True), 25000)
        self.assertEqual(depth_within([], 0.04, True), 0)

    def test_gross_net_count_etf_twice(self):
        self.assertEqual(etf_arb.gross_of(100, -50, -10), 100 + 50 + 20)
        self.assertEqual(etf_arb.net_of(100, -50, -10), 100 - 50 - 20)


class TestSnapshot(BotCase):
    def test_reads_positions_and_mids(self):
        self.api.pos.update(BULL=5, BEAR=-7, RITC=3, USD=1000)
        m = self.market()
        self.assertEqual((m.p_bull, m.p_bear, m.p_ritc, m.p_usd), (5, -7, 3, 1000.0))
        self.assertAlmostEqual(m.bull_mid, 10.01)
        self.assertAlmostEqual(m.fair_usd, (10.01 + 15.01) / 1.0)

    def test_empty_book_side_skips_loop(self):
        self.api.books["RITC"] = (self.api.books["RITC"][0], [])
        self.assertIsNone(self.bot.snapshot())

    def test_missing_ticker_skips_loop(self):
        del self.api.pos["RITC"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(self.bot.snapshot())
        self.assertIn("missing tickers", err.getvalue())

    def test_bad_usd_quote_skips_loop(self):
        self.api.usd = (None, None)
        self.assertIsNone(self.bot.snapshot())

    def test_limits_fallback_then_retry(self):
        self.api.limit_list = []
        bot = Bot(self.api, dry_run=False)
        self.assertFalse(bot.read_limits())
        self.assertEqual((bot.gross_limit, bot.net_limit), (etf_arb.FALLBACK_GROSS, etf_arb.FALLBACK_NET))
        self.api.limit_list = [{"gross_limit": 300000, "net_limit": 200000}]
        bot.step(10)                      # status line retries /limits
        self.assertEqual(bot.gross_limit, 300000)


class TestTenders(BotCase):
    def decide(self, *tenders, tick=10):
        self.api.tender_list = list(tenders)
        self.bot.handle_tenders(self.market(), tick)
        return [c for c in self.api.calls if c[0] in ("accept", "decline")]

    def test_buy_fixed_accept(self):
        # We receive 10,000 RITC and must sell them.
        #   ETF exit:    walk bids 5000 @ 25.00 + 5000 @ 24.99 = 24.995, minus fee 0.02 -> 24.975
        #   basket exit: (9.995 + 14.995 - 0.04) / 1.0 = 24.95          -> best = ETF 24.975
        #   per unit = 24.975 - 24.90 - 0.03 safety = 0.045 -> $450 >= $300
        self.assertEqual(self.decide(tender(price=24.90)), [("accept", 1, None)])
        self.assertIn("exit via ETF 24.975 | edge 0.045/u = $450", self.out.getvalue())

    def test_buy_fixed_too_thin_decline(self):
        # same, price 24.93 -> per unit 0.015 -> $150 < $300
        self.assertEqual(self.decide(tender(price=24.93)), [("decline", 1)])

    def test_sell_fixed_accept(self):
        # We deliver 10,000 RITC (go short) and must buy them back.
        #   ETF:    asks 25.04/25.05 -> 25.045 + 0.02 = 25.065
        #   basket: (10.025 + 15.025 + 0.04) / 1.0 = 25.09   -> best = min = ETF 25.065
        #   per unit = 25.15 - 25.065 - 0.03 = 0.055 -> $550
        self.assertEqual(self.decide(tender(action="SELL", price=25.15)), [("accept", 1, None)])

    def test_non_fixed_names_own_price(self):
        # competitive BUY: our bid = best - safety - margin = 24.975 - 0.03 - 0.05 = 24.895
        # per unit = 24.975 - 24.895 - 0.03 = 0.05 -> $500 -> accept, sending our price
        got = self.decide(tender(fixed=False))
        self.assertEqual(got[0][:2], ("accept", 1))
        self.assertAlmostEqual(got[0][2], 24.895)
        self.assertIn("(our bid)", self.out.getvalue())

    def test_basket_exit_with_usd_conversion(self):
        # USD = 1.25 CAD. RITC bids 19.80 -> ETF exit 19.795 - 0.02 = 19.775
        # basket exit (9.995 + 14.995 - 0.04) / 1.25 = 19.96 -> best = basket
        # per unit = 19.96 - 19.80 - 0.03 = 0.13 -> $1300
        self.api.usd = (1.25, 1.25)
        self.api.books["RITC"] = ladder(19.80, 19.84)
        self.assertEqual(self.decide(tender(price=19.80)), [("accept", 1, None)])
        self.assertIn("exit via basket 19.960", self.out.getvalue())

    def test_max_exposure_declines(self):
        # with MAX_EXPOSURE 50,000: Q = 60,000 leaves E = 60,000 -> decline (even at a great price)
        from unittest import mock as um
        with um.patch.object(etf_arb, "MAX_EXPOSURE", 50000):
            self.assertEqual(self.decide(tender(qty=60000, price=20.00)), [("decline", 1)])
        self.assertIn("NO LIMIT ROOM", self.out.getvalue())

    def test_90k_tender_fits(self):
        # practice-server sizes: 90,000 -> net 2 x 90k = 180k = 90% of 200k, gross 180k: accept
        self.assertEqual(self.decide(tender(qty=90000, price=20.00)), [("accept", 1, None)])

    def test_net_headroom_declines_above_90k(self):
        # 95,000 -> net 190k > 180k (90% of 200k). E = net/2, so this is the real tender cap.
        self.assertEqual(self.decide(tender(qty=95000, price=20.00)), [("decline", 1)])
        self.assertIn("NO LIMIT ROOM", self.out.getvalue())

    def test_gross_limit_declines(self):
        # holding +100k BULL, +100k BEAR (E = +100k). Selling 70k RITC brings E to +30k (fine)
        # but gross = 100k + 100k + 2 x 70k = 340k > 90% x 300k = 270k
        self.api.pos.update(BULL=100000, BEAR=100000)
        self.assertEqual(self.decide(tender(action="SELL", qty=70000, price=30.00)), [("decline", 1)])

    def test_no_new_risk_after_cutoff(self):
        self.assertEqual(self.decide(tender(price=24.00), tick=etf_arb.NO_NEW_RISK_TICK), [("decline", 1)])

    def test_fixed_without_price_declines(self):
        t = tender(price=24.90)
        t["price"] = None
        self.assertEqual(self.decide(t), [("decline", 1)])

    def test_non_ritc_ignored(self):
        self.assertEqual(self.decide(tender(ticker="BULL")), [])

    def test_each_tender_decided_once(self):
        self.decide(tender(price=24.90))
        self.decide(tender(price=24.90))
        self.assertEqual(len([c for c in self.api.calls if c[0] == "accept"]), 1)

    def test_decided_set_resets_on_new_heat(self):
        self.decide(tender(price=24.90), tick=200)
        self.bot.last_status_tick = 200
        self.api.calls.clear()
        self.api.tender_list = [tender(price=24.90)]      # new heat, id 1 again
        self.bot.step(3)                                   # tick went backwards
        self.assertIn(("accept", 1, None), self.api.calls)


class TestUnwind(BotCase):
    def test_sell_via_etf_with_band(self):
        # Long 20,000 RITC -> E = +20,000 -> sell.
        #   ETF proceeds 25.00 - 0.02 = 24.98 vs basket (10.00 + 15.00 - 0.04)/1 = 24.96 -> ETF
        #   q = min(|E| 20000, budget 60000, depth within band 25000) = 20000, all in this loop,
        #   as orders of at most UNWIND_SLICE 10000; marketable LIMIT 0.04 through the bid: 24.96
        self.api.pos["RITC"] = 20000
        self.assertTrue(self.bot.unwind(self.market(), 10))
        self.assertEqual(len(self.api.orders), 2)
        for o in self.api.orders:
            self.assertOrder(o, "RITC", "SELL", 10000, "LIMIT", 24.96)

    def test_hedge_capped_by_band_depth(self):
        # 40k long, only 25k within 0.04 of the bid -> 10k + 10k + 5k now, the rest next time
        self.api.pos["RITC"] = 40000
        self.bot.unwind(self.market(), 10)
        self.assertEqual([o[2] for o in self.api.orders], [10000, 10000, 5000])

    def test_buy_via_etf(self):
        # Short 20,000 RITC: ETF cost 25.04 + 0.02 = 25.06 vs basket 25.04 + 0.04 = 25.08 -> ETF @ 25.08
        self.api.pos["RITC"] = -20000
        self.bot.unwind(self.market(), 10)
        self.assertOrder(self.api.orders[0], "RITC", "BUY", 10000, "LIMIT", 25.08)

    def test_sell_via_basket_when_cheaper(self):
        # RITC bid 24.90 -> ETF proceeds 24.88 < basket 24.96 -> sell 20,000 of EACH leg
        # (2 orders of 10k per leg)
        self.api.pos["RITC"] = 20000
        self.api.books["RITC"] = ladder(24.90, 24.94)
        self.bot.unwind(self.market(), 10)
        self.assertEqual(len(self.api.orders), 4)
        for o in self.api.orders[:2]:
            self.assertOrder(o, "BULL", "SELL", 10000, "LIMIT", 9.96)
        for o in self.api.orders[2:]:
            self.assertOrder(o, "BEAR", "SELL", 10000, "LIMIT", 14.96)

    def test_basket_over_limits_falls_back_to_etf_depth(self):
        # E = 110k - 50k = +60k, basket cheaper, but selling 10k more of each leg makes
        # gross 60k + 60k + 220k = 340k > 270k -> fall back to the ETF, sized by the ETF's own
        # band depth (5 levels x 1000 = 5000). The C bot would have sent 10,000 here.
        self.api.pos.update(RITC=110000, BULL=-50000, BEAR=-50000)
        self.api.books["RITC"] = ladder(24.90, 24.94, qty=1000)
        self.bot.unwind(self.market(), 10)
        self.assertEqual(len(self.api.orders), 1)
        self.assertOrder(self.api.orders[0], "RITC", "SELL", 5000, "LIMIT", 24.86)

    def test_per_tick_budget(self):
        # 60,000 per tick (25k + 25k + 10k, the fake book never empties), then nothing until
        # the tick changes
        self.api.pos["RITC"] = 200000
        for _ in range(5):
            self.bot.unwind(self.market(), 10)
        self.assertEqual(sum(o[2] for o in self.api.orders), 60000)
        self.bot.unwind(self.market(), 11)
        self.assertEqual(sum(o[2] for o in self.api.orders), 85000)

    def test_small_exposure_ignored(self):
        self.api.pos["RITC"] = 99
        self.assertFalse(self.bot.unwind(self.market(), 10))
        self.assertEqual(self.api.orders, [])

    def test_leg_balance_buys_small_leg_when_short(self):
        # BULL +5000, BEAR 0, RITC -2500 -> E = 0 (no unwind), legs off by 5000.
        # E <= 0 -> BUY the smaller leg (BEAR), MARKET
        self.api.pos.update(BULL=5000, RITC=-2500)
        self.assertTrue(self.bot.unwind(self.market(), 10))
        self.assertEqual(self.api.orders, [("BEAR", "BUY", 5000, "MARKET", None)])

    def test_leg_balance_sells_big_leg_when_long(self):
        # BULL +3000, BEAR 0, RITC -1450 -> E = +50 (below UNWIND_MIN) and E > 0 -> SELL BULL
        self.api.pos.update(BULL=3000, RITC=-1450)
        self.bot.unwind(self.market(), 10)
        self.assertEqual(self.api.orders, [("BULL", "SELL", 3000, "MARKET", None)])

    def test_unwind_trade_skips_arb_and_fx(self):
        # E = +20k AND a big USD exposure: only the unwind order goes out this loop
        self.api.pos.update(RITC=20000, USD=-900000)
        self.api.books["RITC"] = ladder(25.20, 25.24)   # also a juicy arb, ignored this loop
        self.bot.step(10)
        self.assertEqual({o[0] for o in self.api.orders}, {"RITC"})


class TestStatArb(BotCase):
    def test_no_edge_no_trade(self):
        # default market: sell_etf = 25.00 - 25.04 - 0.02 - 0.04 = -0.10; buy_etf also -0.10
        self.bot.stat_arb(self.market(), 10)
        self.assertEqual(self.api.orders, [])

    def test_enter_sell_etf(self):
        # RITC rich: bid 25.20 -> sell_etf = 25.20 x 1 - (10.02 + 15.02) - 0.02 - 0.04 = 0.10 >= 0.06
        # q = min(ARB_SLICE 2000, top-of-book 5000 x3) = 2000: SELL RITC, BUY BULL, BUY BEAR (MARKET)
        self.api.books["RITC"] = ladder(25.20, 25.24)
        self.bot.stat_arb(self.market(), 10)
        self.assertEqual(self.api.orders, [("RITC", "SELL", 2000, "MARKET", None),
                                           ("BULL", "BUY", 2000, "MARKET", None),
                                           ("BEAR", "BUY", 2000, "MARKET", None)])

    def test_enter_buy_etf(self):
        # RITC cheap: ask 24.80 -> buy_etf = 25.00 - 24.80 - 0.06 = 0.14
        self.api.books["RITC"] = ladder(24.76, 24.80)
        self.bot.stat_arb(self.market(), 10)
        self.assertEqual([o[:2] for o in self.api.orders], [("RITC", "BUY"), ("BULL", "SELL"), ("BEAR", "SELL")])

    def test_exit_pair_early(self):
        # holding short-ETF pair of 4000; RITC now cheap: buy_etf = 25.00 - 24.90 - 0.06 = 0.04 > 0.02
        self.api.pos.update(RITC=-4000, BULL=4000, BEAR=4000)
        self.api.books["RITC"] = ladder(24.86, 24.90)
        self.bot.stat_arb(self.market(), 10)
        self.assertEqual(self.api.orders, [("RITC", "BUY", 2000, "MARKET", None),
                                           ("BULL", "SELL", 2000, "MARKET", None),
                                           ("BEAR", "SELL", 2000, "MARKET", None)])

    def test_max_units_blocks_entry(self):
        n = etf_arb.ARB_MAX_UNITS   # pair already at the cap -> no new entry
        self.api.pos.update(RITC=-n, BULL=n, BEAR=n)
        self.api.books["RITC"] = ladder(25.20, 25.24)
        self.bot.stat_arb(self.market(), 10)
        self.assertEqual(self.api.orders, [])

    def test_cutoff_blocks_entry(self):
        self.api.books["RITC"] = ladder(25.20, 25.24)
        self.bot.stat_arb(self.market(), etf_arb.NO_NEW_RISK_TICK)
        self.assertEqual(self.api.orders, [])

    def test_thin_top_of_book_blocks_entry(self):
        self.api.books["RITC"] = ladder(25.20, 25.24, qty=50)   # 50 < UNWIND_MIN
        self.bot.stat_arb(self.market(), 10)
        self.assertEqual(self.api.orders, [])

    def test_converter_hint(self):
        # pair 70k, gross 70k + 70k + 140k = 280k > 80% x 300k, tick divisible by 10
        self.api.pos.update(RITC=-70000, BULL=70000, BEAR=70000)
        self.bot.stat_arb(self.market(), 120)
        self.assertIn("CONVERTER HINT", self.out.getvalue())
        self.assertIn("ETF-CREATION", self.out.getvalue())


class TestFxHedge(BotCase):
    def test_hedges_usd_cash(self):
        self.api.pos["USD"] = 30000
        self.bot.fx_hedge(self.market(), 10)
        self.assertEqual(self.api.orders, [("USD", "SELL", 30000, "MARKET", None)])

    def test_ritc_value_offsets_usd_cash(self):
        # long 2000 RITC worth 2000 x 25.02 = 50,040 USD, short 50,000 USD cash -> exposure 40 -> nothing
        self.api.pos.update(RITC=2000, USD=-50000)
        self.bot.fx_hedge(self.market(), 10)
        self.assertEqual(self.api.orders, [])

    def test_big_hedge_is_sliced(self):
        self.api.pos["USD"] = -6000000
        self.bot.fx_hedge(self.market(), 10)
        self.assertEqual([o[2] for o in self.api.orders], [2500000, 2500000, 1000000])
        self.assertTrue(all(o[1] == "BUY" for o in self.api.orders))


class TestLoop(BotCase):
    def test_dry_run_sends_nothing(self):
        bot = Bot(self.api, dry_run=True)
        bot.read_limits()
        self.api.pos.update(RITC=20000, BULL=5000, USD=900000)
        self.api.tender_list = [tender(price=24.00), tender(tid=2, price=26.00)]
        for tick in (10, 11, 12):
            bot.step(tick)
        self.assertEqual(self.api.calls, [])
        self.assertIn("ACCEPT", self.out.getvalue())
        self.assertIn("decline", self.out.getvalue())
        self.assertIn("  -> SELL", self.out.getvalue())

    def test_live_loop_cancels_first(self):
        self.bot.step(10)
        self.assertEqual(self.api.calls[0], ("cancel_all",))

    def test_csv_row_per_tick(self):
        path = os.path.join(tempfile.mkdtemp(), "ticks.csv")
        log = TickLog(path)
        bot = Bot(self.api, dry_run=True, log=log)
        bot.read_limits()
        self.api.pos.update(RITC=-1000, BULL=600, BEAR=600)
        for tick in (5, 5, 5, 6):                 # several loops per tick -> one row per tick
            bot.step(tick)
        log.close()
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["tick"] for r in rows], ["5", "6"])
        self.assertEqual(list(rows[0].keys()), etf_arb.LOG_FIELDS)
        self.assertEqual(float(rows[0]["E"]), -400.0)          # -1000 + (600 + 600)/2
        self.assertEqual(int(rows[0]["gross"]), 600 + 600 + 2000)
        self.assertEqual(float(rows[0]["nlv"]), 1234.5)

    def test_run_survives_unexpected_error(self):
        # a bug in one loop is printed and the loop keeps going
        calls = {"n": 0}

        def boom(tick):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyError("oops")
            raise KeyboardInterrupt
        self.bot.step = boom
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(KeyboardInterrupt):
            self.bot.run()
        self.assertEqual(calls["n"], 2)
        self.assertIn("KeyError", err.getvalue())


class TestSettle(BotCase):
    """The practice-server bug: fills show up in GET /securities ~100-250 ms after the order
    returns. The bot must not trade again on positions that don't show its last trades yet."""

    # Long 30k RITC, 25k within the band: loop 1 sells 25k (10k + 10k + 5k).

    def setUp(self):
        super().setUp()
        self.now = 100.0
        self.bot.clock = lambda: self.now
        self.api.report_fills = True
        self.api.pos["RITC"] = 30000

    def sold(self):
        return sum(o[2] for o in self.api.orders)

    def test_waits_until_fill_is_visible(self):
        # Next loops: RIT still shows 30k -> send NOTHING (the old bot re-sent the same sells
        # here, overshot, and swung back and forth until it hit the gross limit).
        self.bot.step(10)
        self.assertEqual(self.sold(), 25000)
        for _ in range(3):
            self.now += 0.05
            self.bot.step(10)
        self.assertEqual(self.sold(), 25000)
        self.api.pos["RITC"] = 5000           # the fills show up -> next loop sells the rest
        self.now += 0.05
        self.bot.step(10)
        self.assertEqual(self.sold(), 30000)
        self.assertOrder(self.api.orders[-1], "RITC", "SELL", 5000, "LIMIT", 24.96)

    def test_partly_visible_still_waits(self):
        self.bot.step(10)
        self.api.pos["RITC"] = 15000          # 15k of the 25k shows so far
        self.now += 0.05
        self.bot.step(10)
        self.assertEqual(self.sold(), 25000)

    def test_gives_up_after_timeout(self):
        self.bot.step(10)
        self.now += etf_arb.SETTLE_TIMEOUT_MS / 1000 - 0.01
        self.bot.step(10)
        self.assertEqual(self.sold(), 25000)
        self.now += 0.02
        self.bot.step(10)
        self.assertGreater(self.sold(), 25000)
        self.assertIn("fills not visible", self.out.getvalue())

    def test_unknown_fill_size_waits_minimum(self):
        # order response without quantity_filled: wait SETTLE_MIN_MS, then trust what RIT shows
        self.api.report_fills = False
        self.bot.step(10)
        self.now += etf_arb.SETTLE_MIN_MS / 1000 - 0.01
        self.bot.step(10)
        self.assertEqual(self.sold(), 25000)
        self.now += 0.02
        self.bot.step(10)
        self.assertGreater(self.sold(), 25000)

    def test_overfill_counts_as_visible(self):
        # a resting remainder filled too: moved more than reported is fine
        self.bot.step(10)
        self.api.pos["RITC"] = 0              # reported 25k sold, RIT shows 30k sold
        self.now += 0.05
        self.api.pos["USD"] = 0
        self.bot.step(10)
        self.assertIsNone(self.bot.pending)

    def test_tender_accept_stops_the_loop(self):
        # accepting a tender makes RITC stale: no arb/unwind in the same loop, then wait for it
        self.api.pos["RITC"] = 0
        self.api.tender_list = [tender(price=24.90)]
        self.api.books["RITC"] = ladder(25.00, 25.04, qty=20000)
        self.bot.step(10)
        self.assertEqual([c[0] for c in self.api.calls], ["cancel_all", "accept"])
        self.now += 0.05
        self.bot.step(10)                     # RITC still 0 on RIT -> wait
        self.assertEqual(self.api.orders, [])
        self.api.pos["RITC"] = 10000          # tender shows up -> hedge it
        self.now += 0.05
        self.bot.step(10)
        self.assertEqual(self.api.orders[0][:2], ("RITC", "SELL"))

    def test_rejected_order_backs_off(self):
        # the live run resent one rejected order 3,022 times; now it pauses REJECT_BACKOFF_MS
        self.api.reject = True
        self.bot.step(10)
        self.assertEqual(len(self.api.orders), 1)          # first slice fails -> rest not sent
        self.now += etf_arb.SETTLE_MIN_MS / 1000 + 0.01    # fill wait over, backoff not yet
        self.bot.step(10)
        self.assertEqual(len(self.api.orders), 1)
        self.now = 100.0 + etf_arb.REJECT_BACKOFF_MS / 1000 + 0.01
        self.bot.step(10)
        self.assertEqual(len(self.api.orders), 2)

    def test_tenders_still_decided_during_backoff(self):
        self.api.reject = True
        self.bot.step(10)
        self.now += etf_arb.SETTLE_MIN_MS / 1000 + 0.01
        self.api.tender_list = [tender(price=26.00)]       # bad price -> decline
        self.bot.step(10)
        self.assertIn(("decline", 1), self.api.calls)

    def test_new_heat_clears_wait(self):
        self.bot.step(250)
        self.now += 0.05
        self.bot.step(1)                      # tick went backwards: new heat, old fills irrelevant
        self.assertEqual(self.sold(), 50000)


class TestLimitsWhenHedging(BotCase):
    def test_leg_balance_uses_other_leg_near_limit(self):
        # Final state of the bad practice heat: gross 295,176 / 300,000, legs off by 7,616.
        # Preferred: E <= 0 -> BUY 7,616 BEAR, which the server rejected 3,022 times.
        # Now: that breaks the limit, so SELL 7,616 BULL instead (cuts gross).
        self.api.pos.update(RITC=-73794, BULL=77602, BEAR=69986)
        self.assertTrue(self.bot.unwind(self.market(), 100))
        self.assertEqual(self.api.orders, [("BULL", "SELL", 7616, "MARKET", None)])

    def test_unwind_etf_blocked_when_it_adds_gross(self):
        # E = +60k from a long basket at the limit; basket sell is cheaper but adds gross? No:
        # selling long legs CUTS gross, so the basket is used. Make the ETF the only route and
        # check it is blocked when shorting RITC would push gross over the limit.
        self.api.pos.update(RITC=0, BULL=130000, BEAR=130000)   # E = +130k, gross 260k
        self.api.books["BULL"] = ladder(9.80, 9.82)             # basket much worse -> ETF wins
        m = self.market()
        self.bot.unwind(m, 100)
        # ETF sell 10k -> gross 280k > 270k and it adds gross -> blocked; nothing sent
        self.assertEqual(self.api.orders, [])
        self.assertIn("blocked by limits", self.out.getvalue())

    def test_unwind_allowed_over_headroom_when_it_cuts_gross(self):
        # already over 90% (gross 280k): selling 10k of a 140k long RITC cuts gross -> allowed
        self.api.pos.update(RITC=140000)
        self.bot.unwind(self.market(), 100)
        self.assertEqual(self.api.orders[0][:3], ("RITC", "SELL", 10000))

    def test_read_limits_picks_limit_stock(self):
        # the practice server also returns LIMIT-CASH (10M); it may come first
        self.api.limit_list = [{"name": "LIMIT-CASH", "gross_limit": 10000000, "net_limit": 10000000},
                               {"name": "LIMIT-STOCK", "gross_limit": 300000, "net_limit": 200000}]
        bot = Bot(self.api, dry_run=True)
        self.assertTrue(bot.read_limits())
        self.assertEqual((bot.gross_limit, bot.net_limit), (300000, 200000))


class TestLogFailures(unittest.TestCase):
    def test_bad_log_path_runs_without_log(self):
        # the first live practice launch died here: PermissionError on the CSV
        import sys
        from unittest import mock as um
        argv = ["etf_arb.py", "--key", "K", "--host", "127.0.0.1", "--port", "9", "--log",
                os.path.join(tempfile.mkdtemp(), "no_such_dir", "x.csv")]
        out = io.StringIO()
        with um.patch.object(sys, "argv", argv), um.patch.object(Bot, "run", lambda self: None), \
                contextlib.redirect_stdout(out):
            etf_arb.main()
        self.assertIn("running WITHOUT a CSV log", out.getvalue())

    def test_write_error_stops_logging_not_bot(self):
        path = os.path.join(tempfile.mkdtemp(), "t.csv")
        log = TickLog(path)
        log.f.close()                          # any later write raises ValueError/OSError

        class Broken:
            def write(self, *_):
                raise OSError("disk full")

            def flush(self):
                pass

            def close(self):
                pass
        log.f = Broken()
        log.w = csv.writer(log.f)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            log.row([1, 2, 3])
            log.row([4, 5, 6])                 # no second error: logging is off
        self.assertEqual(err.getvalue().count("CSV log write failed"), 1)
        log.close()


# =============================================================================================== #
# 2. Client safety tests (real sockets)
# =============================================================================================== #
class Scripted(BaseHTTPRequestHandler):
    """Local test server. `script[(method, path)]` is a list of actions, consumed in order
    (the last one repeats):
        ("json", status, body[, headers])   normal reply
        ("stale", status, body)             reply, then silently close the keep-alive socket
        ("drop",)                           read the request, close without replying
    Every request that reaches the handler is appended to `hits`."""
    protocol_version = "HTTP/1.1"
    script, hits = {}, []

    def log_message(self, *a):
        pass

    def _go(self, method):
        path = urlparse(self.path).path
        Scripted.hits.append((method, self.path, self.headers.get("X-API-Key")))
        acts = Scripted.script.get((method, path), [("json", 200, {})])
        act = acts.pop(0) if len(acts) > 1 else acts[0]
        if act[0] == "drop":
            self.close_connection = True
            return
        body = json.dumps(act[2]).encode()
        self.send_response(act[1])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (act[3] if len(act) > 3 else {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        if act[0] == "stale":
            self.close_connection = True

    def do_GET(self):
        self._go("GET")

    def do_POST(self):
        self._go("POST")

    def do_DELETE(self):
        self._go("DELETE")


class TestClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Scripted.script, Scripted.hits = {}, []
        self.api = RitClient("127.0.0.1", self.port, "KEY123")
        self.err = io.StringIO()
        self._q = contextlib.redirect_stderr(self.err)
        self._q.__enter__()

    def tearDown(self):
        self.api.close()
        self._q.__exit__(None, None, None)

    def hits(self, method):
        return [h for h in Scripted.hits if h[0] == method]

    def test_sends_api_key(self):
        Scripted.script[("GET", "/v1/case")] = [("json", 200, {"tick": 3, "status": "ACTIVE"})]
        self.assertEqual(self.api.case()["tick"], 3)
        self.assertEqual(Scripted.hits[0][2], "KEY123")

    def test_429_waits_then_retries(self):
        Scripted.script[("GET", "/v1/case")] = [("json", 429, {"wait": 0.05}), ("json", 200, {"tick": 1})]
        t = time.time()
        self.assertEqual(self.api.case(), {"tick": 1})
        self.assertGreaterEqual(time.time() - t, 0.05)
        self.assertEqual((len(Scripted.hits), self.api.rate_limited), (2, 1))

    def test_429_retry_after_header(self):
        Scripted.script[("GET", "/v1/case")] = [("json", 429, {}, {"Retry-After": "0.05"}),
                                                ("json", 200, {"tick": 1})]
        self.assertEqual(self.api.case(), {"tick": 1})

    def test_429_on_post_is_retried(self):
        # safe: the server REJECTED the order, so sending it again can't double it
        Scripted.script[("POST", "/v1/orders")] = [("json", 429, {"wait": 0.01}), ("json", 200, {"order_id": 7})]
        self.assertEqual(self.api.order("RITC", "BUY", 100)["order_id"], 7)
        self.assertEqual(len(self.hits("POST")), 2)

    def test_429_gives_up_after_five(self):
        Scripted.script[("GET", "/v1/case")] = [("json", 429, {"wait": 0.0})]
        self.assertIsNone(self.api.case())
        self.assertEqual(len(Scripted.hits), RitClient.MAX_ATTEMPTS)

    def test_401_exits_with_clear_message(self):
        Scripted.script[("GET", "/v1/case")] = [("json", 401, {"message": "API key is invalid"})]
        with self.assertRaises(SystemExit) as cm:
            self.api.case()
        self.assertIn("API key", str(cm.exception))

    def test_401_after_key_worked_means_heat_over(self):
        # practice server: 401 a few seconds after every heat ends, same key works next heat.
        # Once the key has worked, a 401 must NOT exit the bot: fail the call, keep waiting.
        Scripted.script[("GET", "/v1/case")] = [("json", 200, {"tick": 300, "status": "STOPPED"}),
                                                ("json", 401, {"message": "API key is invalid"}),
                                                ("json", 200, {"tick": 1, "status": "ACTIVE"})]
        self.assertEqual(self.api.case()["status"], "STOPPED")
        self.assertIsNone(self.api.case())
        self.assertIn("heat over", self.api.last_error)
        self.assertEqual(self.api.case()["status"], "ACTIVE")

    def test_dropped_post_is_not_resent(self):
        # the server got the order but the reply was lost: it must NOT be sent again
        Scripted.script[("POST", "/v1/orders")] = [("drop",)]
        self.assertIsNone(self.api.order("RITC", "BUY", 100))
        self.assertEqual(len(self.hits("POST")), 1)
        self.assertIn("not retried", self.api.last_error)

    def test_dropped_delete_is_not_resent(self):
        Scripted.script[("DELETE", "/v1/tenders/5")] = [("drop",)]
        self.assertFalse(self.api.decline_tender(5))
        self.assertEqual(len(self.hits("DELETE")), 1)

    def test_stale_socket_get_retried_once(self):
        Scripted.script[("GET", "/v1/case")] = [("stale", 200, {"tick": 1}), ("json", 200, {"tick": 2})]
        self.assertEqual(self.api.case(), {"tick": 1})
        time.sleep(0.1)                       # server has now closed the kept-alive socket
        self.assertEqual(self.api.case(), {"tick": 2})

    def test_stale_socket_post_not_retried(self):
        Scripted.script[("GET", "/v1/case")] = [("stale", 200, {"tick": 1})]
        self.api.case()
        time.sleep(0.1)
        self.assertIsNone(self.api.order("RITC", "BUY", 100))
        self.assertEqual(self.hits("POST"), [])     # lost, never doubled; the bot re-reads next loop

    def test_server_down_returns_none(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        api = RitClient("127.0.0.1", port, "K")
        self.assertIsNone(api.case())
        self.assertIn("Refused", api.last_error)

    def test_book_uses_available_quantity(self):
        Scripted.script[("GET", "/v1/securities/book")] = [("json", 200, {
            "bids": [{"price": 10.0, "quantity": 500, "quantity_filled": 200},
                     {"price": 9.99, "quantity": 300, "quantity_filled": 300}],   # fully filled: skipped
            "asks": [{"price": 10.02, "quantity": 100, "quantity_filled": 0}]})]
        self.assertEqual(self.api.book("BULL", 20), ([(10.0, 300)], [(10.02, 100)]))

    def test_order_and_tender_query_strings(self):
        Scripted.script[("POST", "/v1/tenders/9")] = [("json", 200, {"success": True})]
        self.api.order("BULL", "SELL", 300, "LIMIT", 9.96)
        self.api.order("BULL", "BUY", 300)
        self.assertTrue(self.api.accept_tender(9, 24.5))
        self.assertTrue(self.api.accept_tender(9))
        paths = [h[1] for h in self.hits("POST")]
        self.assertEqual(paths[0], "/v1/orders?ticker=BULL&type=LIMIT&quantity=300&action=SELL&price=9.9600")
        self.assertEqual(paths[1], "/v1/orders?ticker=BULL&type=MARKET&quantity=300&action=BUY")
        self.assertEqual(paths[2:], ["/v1/tenders/9?price=24.50", "/v1/tenders/9"])

    def test_accept_reports_failure(self):
        Scripted.script[("POST", "/v1/tenders/9")] = [("json", 200, {"success": False})]
        self.assertFalse(self.api.accept_tender(9))


# =============================================================================================== #
# 3. One full heat against the mock server (end to end)
# =============================================================================================== #
@unittest.skipIf(os.environ.get("SKIP_SLOW"), "SKIP_SLOW set")
class TestFullHeatOnMock(unittest.TestCase):
    def test_full_heat(self):
        import mock_rit_server as mock
        random.seed(8)
        # 300 ticks x 0.1 s = 30 s heat. Fills show in /securities 0.2 s late, as on the practice
        # server (without the fill wait the bot churned to the gross limit here).
        mock.M = mock.Market("etf", 0.1, 10, position_lag=0.2)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), mock.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        path = os.path.join(tempfile.mkdtemp(), "heat.csv")
        api = RitClient("127.0.0.1", srv.server_address[1], mock.API_KEY)
        log = TickLog(path)
        bot = Bot(api, dry_run=False, log=log)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertTrue(bot.read_limits())
                while True:                          # Bot.run() without the infinite loop
                    c = api.case()
                    if c["status"] != "ACTIVE":
                        break
                    bot.step(c["tick"])
                    time.sleep(etf_arb.LOOP_MS / 1000)
        finally:
            log.close()
            srv.shutdown()
            srv.server_close()
            api.close()
        text, errors = out.getvalue(), err.getvalue()
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        nlv = mock.M.nlv()
        print(f"\n    full heat: final NLV {nlv:,.0f} CAD, {text.count('ACCEPT')} tenders accepted, "
              f"{len(rows)} CSV rows, {api.requests} requests, {api.rate_limited} rate-limit waits")

        self.assertNotIn("Traceback", errors)
        self.assertNotIn("!!", text)
        self.assertGreater(text.count("ACCEPT"), 0)
        self.assertGreater(len(rows), 250)
        # every exposure spike from a tender is hedged back under 1,000 within a few ticks
        E = [(int(r["tick"]), abs(float(r["E"]))) for r in rows]
        for i, (tick, e) in enumerate(E):
            if e >= 5000:
                later = [t for t, x in E[i:] if x < 1000]
                self.assertTrue(later and later[0] - tick <= 8, f"|E|={e:.0f} at tick {tick} not hedged fast")
        self.assertLess(max(e for _, e in E[-5:]), 1000)   # flat into the close


if __name__ == "__main__":
    unittest.main(verbosity=2)
