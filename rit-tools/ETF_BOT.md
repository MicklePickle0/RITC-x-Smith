# How the ETF bot works (`etf_arb.py`)

This is the logic of the ETF arbitrage bot, step by step, with the exact formulas and worked numbers. Every worked example below is also a test in `test_etf_arb.py` (the test name is given), so if you change a rule the matching test fails and tells you which number moved.

For finance terms (bid/ask, short, ETF, basket…) see `PLAYBOOK.md` P0. For how to run things, see `README.md`.

---

## 1. The idea in one minute

- **RITC** is an ETF made of **1 BULL + 1 BEAR**. RITC is priced in **USD**; BULL and BEAR in **CAD**; the **USD** ticker is the exchange rate (CAD per 1 USD).
- So it *should* hold that **RITC × USD = BULL + BEAR**, i.e. fair RITC (USD) = (BULL + BEAR) / USD. The case shocks prices away from this, and they drift back.
- We earn money in two ways:
  1. **Tenders.** An institution offers us a big block of RITC (20k–60k) at a fixed price. If that price beats what it costs us to *hedge* the block in the market, we accept and hedge immediately.
  2. **Stat arb.** When RITC × USD and BULL + BEAR disagree by more than the fees, sell the expensive side and buy the cheap side. At the end of the heat, open positions close at the *correct* price, so a hedged pair converges.
- The main danger is **holding an unhedged block**. Prices drift about $0.08/unit per tick, so 50k units unhedged for 20 ticks once cost $47k on a mock heat. The bot's top priority after a tender is hedging *fast*.

### Words used below

| Term | Meaning |
|---|---|
| touch | the best bid (highest buyer) and best ask (lowest seller) |
| walk the book | fill a big order level by level and take the average price |
| marketable limit | a LIMIT order priced through the touch, so it fills now, but never worse than its price |
| basket | 1 BULL + 1 BEAR, which is what one RITC is made of |
| **E** (exposure) | `RITC + (BULL + BEAR) / 2`, in "ETF units". +20k means we're long the equivalent of 20k RITC. **E = 0 means hedged.** |
| pair | a hedged arb position: short RITC + long BULL + long BEAR (or the mirror) |
| gross / net | RIT's position limits: gross = \|BULL\| + \|BEAR\| + **2**\|RITC\|, net = BULL + BEAR + **2**·RITC (the ETF counts twice) |

Why E uses (BULL + BEAR) / 2: buying 1 BULL and 1 BEAR is like buying 1 RITC, so 2 shares of basket = 1 unit of exposure. A short RITC with a long basket has E = −1 + 2/2 = 0.

---

## 2. One loop (every ~50 ms)

```
            ┌──────────────────────────────────────────────────────────────┐
            │ GET /case ── not reachable? ── print "waiting…", retry 0.5 s │
            │     └─ not ACTIVE? ── forget tenders, retry 0.25 s           │
            └──────────────────────────────┬───────────────────────────────┘
                                           ▼
 0. SNAPSHOT  cancel all open orders → read positions + BULL/BEAR/RITC books
              (anything missing → skip this loop, trade nothing)
              first loop of a new tick → status line + CSV row
                                           ▼
 1. TENDERS   price each new RITC tender → ACCEPT or decline (once per id)
                                           ▼
 2. UNWIND    |E| ≥ 100 ?  → one hedge slice on the cheaper venue ──┐
 3. LEGS      BULL ≠ BEAR? → trade one leg ─────────────────────────┤ traded?
                                           ▼ no                     │ yes → end loop
 4. STAT ARB  close a pair early, or open a new one                 │ (positions are
 5. FX        |USD exposure| ≥ 25,000 → trade USD                   │  stale now)
                                           ▼  ◄─────────────────────┘
                                 sleep the rest of 50 ms
```

Two rules make this safe:
- **RIT's positions are the only truth.** The bot never keeps its own running position count; every loop re-reads them. Orders that didn't fill, or failed, are simply re-decided next loop.
- **One kind of action per loop.** If the unwind or leg balance traded, the positions in hand are out of date, so arb and FX wait for the next loop. Nothing gets doubled.

The cancel at the start matters: unwind orders are marketable limits, so if part of one doesn't fill, the rest sits in the book. Cancelling every loop means we never have stale orders resting at old prices.

---

## 3. Step by step

### Step 0: snapshot (`Bot.snapshot`)
Reads `GET /securities` (positions, USD bid/ask) and `GET /securities/book` for BULL, BEAR and RITC (20 levels). Available quantity per level = `quantity − quantity_filled`. Computes mids and `fair_usd = (BULL_mid + BEAR_mid) / USD_mid`.
If a ticker or a book side is missing, or the USD quote is bad, it returns nothing and **the loop trades nothing** (`TestSnapshot`).

### Step 1: tenders (`Bot.handle_tenders`)

**Question:** if we take this block, what's the best price we can get rid of it at, after fees?

There are two ways out, and we use the better one:

| | You **BUY** Q (you'll hold +Q, must sell) | You **SELL** Q (you'll hold −Q, must buy back) |
|---|---|---|
| via ETF | `walk(RITC bids, Q) − FEE` | `walk(RITC asks, Q) + FEE` |
| via basket | `(walk(BULL bids) + walk(BEAR bids) − 2·FEE) / USD_ask` | `(walk(BULL asks) + walk(BEAR asks) + 2·FEE) / USD_bid` |
| best | the **higher** (more proceeds) | the **lower** (cheaper) |

`walk` is the average fill price over the book for the whole Q. If the visible book is too thin, the rest is priced at the last level ± `THIN_BOOK_PENALTY` ($0.05), which is deliberately pessimistic.

Then:
- **Fixed tender:** the price is given.
- **Competitive tender** (no price, we bid): we name `best − 0.03 − 0.05` if buying, or `best + 0.03 + 0.05` if selling (`TENDER_SAFETY + TENDER_BID_MARGIN`).
- `per_unit = best − price − TENDER_SAFETY` (buying) or `price − best − TENDER_SAFETY` (selling). `profit = per_unit × Q`.

**Accept only if all of these hold**, otherwise decline:
1. `tick < 290` (no new risk at the very end),
2. `per_unit > 0` and `profit ≥ $300`,
3. after the trade, gross and net still fit within **90%** of the limits,
4. after the trade, `|E| ≤ 80,000`,
5. the tender actually has a price (or is competitive).

Each tender id is decided **exactly once**, even if the accept/decline call fails, so it's never sent twice.

**Worked example** (`test_buy_fixed_accept`). You BUY 10,000 @ 24.90. RITC bids: 5,000 @ 25.00, 5,000 @ 24.99, … BULL bids 10.00, 9.99, …; BEAR bids 15.00, 14.99, …; USD = 1.
- via ETF: walk = (5000·25.00 + 5000·24.99)/10000 = 24.995, minus 0.02 = **24.975**
- via basket: (9.995 + 14.995 − 0.04) / 1 = 24.95
- best = 24.975 (ETF). per_unit = 24.975 − 24.90 − 0.03 = **0.045**, profit = **$450 ≥ $300 → ACCEPT**.
- At 24.93 instead: per_unit 0.015, profit $150 → **decline** (`test_buy_fixed_too_thin_decline`).

Other cases tested: SELL side (`test_sell_fixed_accept`), our own bid on a competitive tender (`test_non_fixed_names_own_price`: bids 24.895), basket exit through USD = 1.25 (`test_basket_exit_with_usd_conversion`), the exposure cap (`test_max_exposure_declines`), the gross limit (`test_gross_limit_declines`) and the tick-290 cutoff.

What it looks like (real run on the mock):
```
[t105] TENDER #5 you BUY 30000 RITC @ 27.19 (our bid) | fair 27.350 | exit via ETF 27.267 | edge 0.050/u = $1500
       ACCEPT
[t 30] TENDER #2 you SELL 20000 RITC @ 25.85 | fair 25.924 | exit via basket 26.008 | edge -0.188/u = $-3753
       decline
```

### Step 2: unwind (`Bot.unwind`, first half)

**Goal:** drive E to 0, fast.

1. If `|E| < 100`, do nothing. If E > 0 we **sell**, if E < 0 we **buy**.
2. **Pick the venue** by its price at the touch, in USD per unit after fees:
   - ETF: `RITC_bid − FEE` (selling) / `RITC_ask + FEE` (buying)
   - basket: `(BULL_bid + BEAR_bid − 2·FEE) / USD_ask` (selling) / `(BULL_ask + BEAR_ask + 2·FEE) / USD_bid` (buying)
   - Take whichever gives more when selling, or costs less when buying.
3. **Size the slice:** `q = min(|E|, 10,000, budget left this tick, depth within $0.04 of the touch)`. For the basket, the depth is that of the *thinner* leg. The budget is 60,000 units per tick (books refill each tick).
4. If the basket wouldn't fit the limits, **fall back to the ETF**, sized by the ETF's own depth.
5. Send **marketable LIMIT** orders priced `UNWIND_BAND` ($0.04) through the touch. They fill whatever is within 4 cents and never walk further; any unfilled rest is cancelled next loop. For the basket, that's q BULL **and** q BEAR (each leg moves E by q/2, so together they move E by q).

**Worked example** (`test_sell_via_etf_with_band`). Long 20,000 RITC → E = +20,000 → sell. ETF gives 25.00 − 0.02 = 24.98 vs basket 24.96, so the ETF wins. Depth from 25.00 down to 24.96 = 5 levels × 5,000 = 25,000. q = min(20,000, 10,000, 60,000, 25,000) = **10,000**. Order: **SELL 10,000 RITC LIMIT @ 24.96**. The next loop sends the other 10,000.

**Why the $0.04 band:** an early version only hit the top level of the book, and after a 50k tender it sat unhedged for ~20 ticks (−$47k). Walking up to 4 cents costs a little impact but hedges in 1–2 ticks (that heat became +$9.9k). Hedging speed matters more than saving impact.

A real 30k tender hedged within one tick, switching venue as prices moved:
```
[t105] unwind E=30000 via ETF (etf 27.300 vs basket 27.287)
  -> SELL   10000 RITC LIMIT @ 27.2800  (unwind)
[t105] unwind E=20000 via basket (etf 27.270 vs basket 27.287)
  -> SELL   10000 BULL LIMIT @ 10.1600  (unwind leg)
  -> SELL   10000 BEAR LIMIT @ 16.9700  (unwind leg)
[t105] unwind E=10000 via ETF (etf 27.270 vs basket 27.257)
  -> SELL   10000 RITC LIMIT @ 27.2500  (unwind)
[t106] ... pos RITC 12000 BULL -12000 BEAR -12000 ...      ← E = 12000 − 12000 = 0, hedged
```

### Step 3: leg balance (`Bot.unwind`, second half)
Only if nothing traded yet this loop. If `|BULL − BEAR| ≥ 100`, the basket isn't a clean hedge (it's a bet on BULL vs BEAR). The bot trades **one** leg with a MARKET order (up to 10,000), choosing the direction that *also* shrinks E:
- E > 0 (too long): **sell the bigger leg**.
- E ≤ 0 (too short): **buy the smaller leg**.

Example (`test_leg_balance_buys_small_leg_when_short`): BULL +5,000, BEAR 0, RITC −2,500 → E = 0, imbalance 5,000 → **BUY 5,000 BEAR**.
(An older version traded both legs, which pushed E the wrong way.)

### Step 4: stat arb (`Bot.stat_arb`)
Profit per unit in **CAD** of each direction, using prices we can actually trade at, after all three fees:
```
sell_etf = RITC_bid·USD_bid − (BULL_ask + BEAR_ask) − FEE·USD_mid − 2·FEE     (RITC rich)
buy_etf  = (BULL_bid + BEAR_bid) − RITC_ask·USD_ask − FEE·USD_mid − 2·FEE     (RITC cheap)
```
`FEE·USD_mid` is the RITC fee (charged in USD) converted to CAD.

1. **Find the existing pair:** short RITC with long BULL and BEAR, or the mirror. Its size is the smallest of the three positions.
2. **Exit early** if the pair is ≥ 100 and the *reverse* trade now pays more than `ARB_EXIT` ($0.02). Close 2,000 units (3 MARKET orders) and stop for this loop.
3. **Converter hint:** if the pair is ≥ 10,000 and gross is above 80% of the limit, every 10 ticks it prints which manual conversion (Assets tab, $1,500) would free limits. Converting 10k units frees 40k of gross.
4. **Enter** a new pair if `tick < 290`, the pair is below 30,000, the best edge is ≥ `ARB_ENTRY` ($0.06), `q = min(2,000, top-of-book size on all 3 legs) ≥ 100`, and it fits the limits.

Example (`test_enter_sell_etf`): RITC bid 25.20, BULL ask 10.02, BEAR ask 15.02, USD 1 → sell_etf = 25.20 − 25.04 − 0.02 − 0.04 = **0.10 ≥ 0.06** → **SELL 2,000 RITC, BUY 2,000 BULL, BUY 2,000 BEAR**.
The exit threshold is lower than the entry, so a pair isn't opened and closed on the same small wiggle.

### Step 5: FX hedge (`Bot.fx_hedge`)
Trading RITC creates USD exposure: we hold USD cash, or USD-priced shares. `exp_usd = USD position + RITC position × RITC_mid`. If `|exp_usd| ≥ 25,000`, it trades USD the opposite way (MARKET, ≤ 2.5M per order).
This formula is right **whether or not** RIT books RITC cash into the USD position. Example (`test_ritc_value_offsets_usd_cash`): long 2,000 RITC (worth 50,040 USD) and −50,000 USD cash → exposure 40 → nothing to do.

---

## 4. Safeguards

| What can go wrong | What the bot does | Test |
|---|---|---|
| Network error on an **order / accept / decline** | **Never resent** (it might have gone through). The loop re-reads positions and decides again. | `test_dropped_post_is_not_resent`, `test_stale_socket_post_not_retried` |
| Stale keep-alive socket on a **read** | reconnects and retries the GET once | `test_stale_socket_get_retried_once` |
| HTTP 429 rate limit | waits the `wait` seconds (or `Retry-After`), retries, up to 5 tries; counts it in `rl` | `test_429_*` |
| Wrong API key | stops with "401 Unauthorized: wrong API key?" | `test_401_exits_with_clear_message` |
| RIT Client closed or restarting | "waiting for RIT client…", resumes by itself | `test_server_down_returns_none` |
| `/limits` not answering at start | uses 250k/150k fallback, retries every tick until it gets the real ones | `test_limits_fallback_then_retry` |
| Same tender seen twice | decided once per id; the list is cleared between heats (or when the tick goes backwards) | `test_each_tender_decided_once`, `test_decided_set_resets_on_new_heat` |
| Bug / odd data in one loop | prints the traceback, keeps trading | `test_run_survives_unexpected_error` |
| Leftover resting orders | cancel-all at the start of every loop and on Ctrl+C | `test_live_loop_cancels_first` |
| Near the limits | every plan is checked against 90% of gross and net, with the ETF counted ×2 | `test_gross_limit_declines`, `test_basket_over_limits_falls_back_to_etf_depth` |
| End of heat | no new tenders or arbs from tick 290; unwind keeps running | `test_no_new_risk_after_cutoff`, `test_cutoff_blocks_entry` |

---

## 5. Knobs: what to change and when

All are UPPER_CASE at the top of `etf_arb.py`. Restart the bot after editing. **Tune on the practice server, not the mock**: the mock's price impact is unrealistic.

| Knob | Default | Raise it → | Change it when you see… |
|---|---|---|---|
| `TENDER_SAFETY` | 0.03 | fewer, safer tenders | accepted tenders losing money once hedged (compare the tender's `edge` line with NLV a few ticks later) |
| `TENDER_MIN_PROFIT` | 300 | skip small tenders | lots of tiny accepts that don't move NLV |
| `TENDER_BID_MARGIN` | 0.05 | more profit per competitive win, fewer wins | competitive tenders never won (lower it) or always won easily (raise it) |
| `THIN_BOOK_PENALTY` | 0.05 | more pessimistic on big blocks | big tenders hedging worse than estimated |
| `UNWIND_BAND` | 0.04 | faster hedge, more impact | `|E|` taking more than ~2 ticks to reach 0 (raise it), or hedge fills far from the touch (lower it) |
| `UNWIND_PER_TICK` | 60,000 | faster hedge | books clearly refilling more than this per tick |
| `MAX_EXPOSURE` | 80,000 | accept bigger tenders | decline lines saying `NO LIMIT ROOM` on tenders you wanted |
| `ARB_ENTRY` / `ARB_EXIT` | 0.06 / 0.02 | fewer, fatter arbs | arbs churning fees (raise) or `dev` often > 0.1 with no trades (lower) |
| `ARB_SLICE` / `ARB_MAX_UNITS` | 2,000 / 30,000 | bigger arb positions | gross staying low while `dev` is large |
| `FX_BAND` | 25,000 | fewer FX trades | many small FX hedges |
| `LOOP_MS` | 50 | fewer requests | `rl` (rate-limit count) climbing fast |

---

## 6. Reading the output

```
[t106] RITC 27.18 fair 27.187 dev -0.007 CAD | pos RITC 12000 BULL -12000 BEAR -12000 USD -321144 | gross 48000/300000 | NLV 4,707 | req 2765 rl 0
  tick  RITC mid  fair (USD)  RITC×USD − basket   positions (from RIT)                    used / limit       P&L (CAD)  API calls, 429s
```
- `TENDER #… | exit via … | edge …/u = $…` then `ACCEPT` / `decline`: a tender decision with all its numbers.
- `unwind E=… via ETF|basket (etf … vs basket …)`: a hedge slice and why that venue was chosen.
- `  -> SELL 10000 RITC LIMIT @ 27.2800 (unwind)`: an order (in `--dry-run` these are printed but **not sent**).
- `ARB ENTER` / `ARB EXIT` / `FX hedge` / `CONVERTER HINT`: the other steps.
- `!! …`: something failed (order, accept or decline). `[api] … -> 400 …`: RIT rejected a request; the message says why.

### CSV log (`--log ticks.csv`)
One row per tick: `time, tick, bull_mid, bear_mid, ritc_mid, usd_mid, fair_usd, dev_cad, pos_ritc, pos_bull, pos_bear, pos_usd, E, gross, nlv, requests, rate_limited`. Rows are appended, so one file can hold several heats (the tick restarts at 1). Things to check after a practice heat:
- **Hedge speed:** after each tender, how many rows until `|E| < 1000`? It should be 1–2.
- **Where the money came from:** plot `nlv` against tick; jumps right after tenders are tender P&L, slow climbs while `dev` closes are arb.
- **Rate limits:** if `rate_limited` grows steadily, raise `LOOP_MS`.

---

## 7. Testing

```bash
cd rit-tools
python -m unittest test_etf_arb -v      # 61 tests, ~35 s (one full mock heat included)
```
Set `SKIP_SLOW=1` to skip the full heat (~3 s total then). In PowerShell: `$env:SKIP_SLOW=1`.

| Group | What it proves |
|---|---|
| `TestHelpers`, `TestSnapshot` | book walking, band depth, limit maths, bad data skips the loop |
| `TestTenders` | every accept/decline rule, with hand-computed prices |
| `TestUnwind` | venue choice, slice sizing, band pricing, per-tick budget, basket→ETF fallback, leg balance direction, "traded → skip arb/FX" |
| `TestStatArb`, `TestFxHedge` | entry/exit edges, size caps, cutoff, converter hint, FX formula and slicing |
| `TestLoop` | dry-run sends **nothing**, cancel-first, one CSV row per tick, survives a bad loop |
| `TestClient` | the network safety rules against a real local server: 429, 401, dropped and stale connections, query formats |
| `TestFullHeatOnMock` | a whole 300-tick heat: no errors, tenders accepted, every exposure spike hedged within 8 ticks, flat at the close |

What the tests **can't** tell you: real market impact, how fast real books refill, and the open questions below. That's what the practice server is for. For multi-seed runs against the standalone mock, see CLAUDE.md "Testing".

---

## 8. Assumptions to check on the practice server

| Assumption | If it's wrong |
|---|---|
| Tender `action` is from **our** side (BUY = we receive RITC) | every tender is priced backwards. Check with one small accept. |
| RITC trades change the USD position | no change needed: the FX formula works either way, but confirm |
| Marketable LIMIT pays the $0.02 fee (not the $0.01 rebate) | if it earns the rebate, our costs are lower than estimated: fine, just conservative |
| `/limits` has one block with `gross_limit`/`net_limit` | the bot reads the first block; if there are several, pick the right one in `read_limits` |
| `api_orders_per_second` ≈ 10 per ticker | if lower, raise `LOOP_MS` |

## 9. Differences from the C bot (`etf_arb/etf_arb.c`)

Same steps, formulas, knobs and defaults. Differences:
- **Fallback sizing:** when the basket hedge doesn't fit the limits and the unwind falls back to the ETF, Python sizes the slice by the **ETF's** depth. C kept the basket's depth (`test_basket_over_limits_falls_back_to_etf_depth`).
- **Extras:**
  - NLV on the status line
  - the `--log` CSV
  - `/limits` retried until it answers
  - tender list cleared if the tick goes backwards
  - cancel-all on Ctrl+C
  - one bad loop doesn't stop the bot
  - the client remembers the working IP, so Windows reconnects don't pay a 2 s `localhost` → IPv6 penalty
