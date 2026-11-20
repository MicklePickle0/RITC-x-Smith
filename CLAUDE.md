# CLAUDE.md: RITCxSmith 2026 toolkit

## Who / what / when
- Two CS + stats students (little finance background) competing in **RITCxSmith 2026** (Rotman International Trading Competition x Smith School of Business), **Saturday Nov 21, 2026**. Trading runs 11:30–13:45.
- Two cases, weighted 50/50. Score = **rank per heat, averaged**, so consistency and avoiding penalties beat big swings.
- `PLAYBOOK.md` explains all finance terms and the strategy. Read it before changing strategy logic.

## Current task: port the C ETF bot to Python
We decided Python is the better choice: the bottleneck is HTTP round-trips to the local RIT Client plus server rate limits, and the market only updates once per second. The organizers' base template is Python too, and Python is faster to retune between heats.

**Deliverables**
1. `rit-tools/rit_client.py`: a shared REST client, extracted from the `RitClient` class in `rit-tools/vol_scanner.py`. Then make `vol_scanner.py` import it, and confirm the scanner still works.
2. `rit-tools/etf_arb.py`: a **behaviour-for-behaviour port** of `rit-tools/etf_arb/etf_arb.c` (with `rit.c` → `rit_client.py`). Keep the same knobs, the same defaults and the same safeguards.
3. CSV logging, one row per tick: tick, mids, fair, dev, positions, E, gross, NLV (from `GET /v1/trader`), rate-limit count. Put it behind `--log FILE`.
4. Update `rit-tools/README.md` so the Python bot is the primary ETF bot. Keep the C bot documented as an alternative.

Do **not** delete the C code.

## Constraints
- **Standard library only** (`http.client`, `json`, `argparse`, `csv`, `math`, `time`). No pip installs: the competition machine may have no internet. Python 3.8+.
- It must run on **Windows** (the RIT Client is Windows-only, and the API is served by that client at `http://localhost:9999/v1`). No POSIX-only calls. Use `os.system("")` to enable ANSI colours if you add any.
- **Never retry a POST/DELETE after a network error.** It could send an order twice. Retry GETs once on a stale keep-alive socket. On HTTP 429, sleep for the JSON `wait` field (or the `Retry-After` header) and retry; that's safe because the server rejected the request.
- RIT positions (from `GET /v1/securities`) are the source of truth every loop. Don't keep shadow position state.
- All strategy knobs go as UPPER_CASE constants at the top of `etf_arb.py`, with the same names as the C `#define`s.

## The ETF case (from the official brief)
- 300 s heat = 1 month. **Trading must be done by the algorithm, not manually**, although the person running it may edit code between heats. Only one team member participates.
- Tickers: `CAD`, `USD` (currencies; USD is quoted as CAD per 1 USD), `BULL` and `BEAR` (CAD stocks, starting at $10 and $15), `RITC` (USD ETF, starts at $25).
- Equilibrium: **RITC_usd × USD = BULL + BEAR**, so fair RITC in USD = (BULL_mid + BEAR_mid) / USD_mid. Prices get periodic shocks away from this.
- Fees: $0.02/share on market orders filled; **$0.01/share rebate** on filled limit orders. Max order 10,000 shares (stocks and ETF), 2,500,000 for currencies.
- Gross/net limits apply, and **the ETF counts ×2**. Read them from `GET /v1/limits`. The fallback in the C bot is 250,000/150,000.
- Private **tender offers** for RITC (`GET /v1/tenders`) with a short expiry. Accept with `POST /v1/tenders/{id}` (add `?price=` for non-fixed/competitive bids), decline with `DELETE /v1/tenders/{id}`.
- **Converters** (10,000 BULL + 10,000 BEAR ⇄ 10,000 RITC, $1,500 USD per use) are **manual only** (Assets tab). The API can't use them, so the bot only prints hints.
- Close-out: non-zero positions close at the last price, **or at the correct price if there is a shock divergence**. That means a hedged ETF-vs-basket pair converges at the end and is fine to hold.

## RIT REST API (verified from the official REST API PDF and the `ritc` Python library docs)
- Base URL `http://localhost:9999/v1`. The port is shown in the RIT Client's bottom status bar. Auth: header `X-API-Key: <key>`.
- `GET /case` → `tick`, `period`, `ticks_per_period`, `status` (`ACTIVE`, …)
- `GET /securities[?ticker=]` → list of `ticker`, `position`, `bid`, `ask`, `last`, `api_orders_per_second`, …
- `GET /securities/book?ticker=&limit=` → `{bids:[...], asks:[...]}`. Each level has `price`, `quantity`, `quantity_filled`. Available = quantity − quantity_filled.
- `GET /tenders` → list of `tender_id`, `ticker`, `action`, `quantity`, `price` (null for non-fixed), `is_fixed_bid`, `expires`, `caption`
- `POST /orders?ticker=&type=MARKET|LIMIT&quantity=&action=BUY|SELL[&price=]` → `order_id`, `quantity_filled`, `vwap`, `status`
- `POST /commands/cancel?all=1` (or `ticker=`, `ids=`) → `cancelled_order_ids`
- `GET /limits` → list of `name`, `gross`, `net`, `gross_limit`, `net_limit`
- `GET /trader` → `nlv`
- Error JSON: `code`, `message`, `wait` (seconds, on rate limit).

## Strategy to port (each loop, `LOOP_MS = 50`)
0. If `status != ACTIVE`: sleep and clear the set of already-decided tenders. Otherwise: cancel all open orders, then snapshot positions and the BULL/BEAR/RITC books (depth 20). Take USD bid/ask from `/securities`.
1. **Tenders** (each undecided RITC tender), with Q = quantity:
   - **You BUY** (you'll hold +Q): `exit_etf = walk(RITC bids, Q) − FEE`; `exit_basket = (walk(BULL bids, Q) + walk(BEAR bids, Q) − 2·FEE) / USD_ask`; `best = max`.
   - **You SELL** (you'll hold −Q): `exit_etf = walk(RITC asks, Q) + FEE`; `exit_basket = (walk(BULL asks) + walk(BEAR asks) + 2·FEE) / USD_bid`; `best = min`.
   - `walk()` = average fill price over book levels. If the book is too thin, the rest is priced at the last level ± `THIN_BOOK_PENALTY`.
   - Non-fixed tender: our price = `best ∓ (TENDER_SAFETY + TENDER_BID_MARGIN)` (minus when we buy, plus when we sell).
   - `per_unit = best − price − TENDER_SAFETY` (buy) or `price − best − TENDER_SAFETY` (sell). `profit = per_unit · Q`.
   - Accept only if: `tick < NO_NEW_RISK_TICK`, `per_unit > 0`, `profit ≥ TENDER_MIN_PROFIT`, the post-trade position fits `LIMIT_HEADROOM` × the limits (gross = |BULL| + |BEAR| + 2|RITC|, net = BULL + BEAR + 2·RITC), and the new `|E| ≤ MAX_EXPOSURE`. Otherwise decline. Never decide the same tender id twice.
2. **Unwind:** `E = RITC + (BULL + BEAR)/2`, the ETF-equivalent exposure. If `|E| ≥ UNWIND_MIN` and the per-tick budget (`UNWIND_PER_TICK`) remains:
   - Compare at the touch: ETF (`RITC_bid − FEE` to sell / `RITC_ask + FEE` to buy) vs basket (`(BULL + BEAR ∓ 2·FEE) / USD_ask or USD_bid`). Pick the better one. Fall back to the ETF if the basket doesn't fit the limits.
   - `q = min(|E|, UNWIND_SLICE, budget, depth within UNWIND_BAND of the touch)`. For the basket, use the min depth of the two legs.
   - Send **marketable LIMIT** orders priced `UNWIND_BAND` through the touch. Basket = q of BULL **and** q of BEAR.
   - If anything traded this loop, **skip steps 3–5** (positions are stale).
3. **Leg balance** (only if nothing traded yet): if `|BULL − BEAR| ≥ UNWIND_MIN`, trade **one** leg (MARKET, up to `UNWIND_SLICE`), choosing the direction that also shrinks E. E > 0 → sell the bigger leg. E ≤ 0 → buy the smaller leg.
4. **Stat arb** (`ARB_ENABLE`), in CAD per unit:
   - `sell_etf = RITC_bid·USD_bid − (BULL_ask + BEAR_ask) − FEE·USD_mid − 2·FEE`
   - `buy_etf = (BULL_bid + BEAR_bid) − RITC_ask·USD_ask − FEE·USD_mid − 2·FEE`
   - Existing pair = short RITC with long BULL and BEAR (dir −1), or the mirror (dir +1). Size = min of the three |positions|.
   - If pair ≥ UNWIND_MIN and the reverse edge > `ARB_EXIT`: close an `ARB_SLICE` of the pair (3 market orders) and return.
   - Print a converter hint every 10 ticks if pair ≥ 10,000 and gross > 80% of the limit.
   - Enter a new pair if `tick < NO_NEW_RISK_TICK`, `pair < ARB_MAX_UNITS`, `max(edge) ≥ ARB_ENTRY`, `q = min(ARB_SLICE, top-of-book qty on all three legs) ≥ UNWIND_MIN`, and it fits the limits.
5. **FX hedge** (`FX_HEDGE_ENABLE`): `exp_usd = USD_position + RITC_position × RITC_mid`. If `|exp_usd| ≥ FX_BAND`, trade USD the opposite way (MARKET, ≤ 2,500,000 per order). This formula is right whether or not RIT books RITC cash into the USD position.
6. Print a status line once per tick. Log every decision (tender evaluation with numbers, every order with its reason).

**Knob defaults** (copy exactly): `FEE 0.02, REBATE 0.01, MAX_ORDER 10000, MAX_FX_ORDER 2500000, TENDER_SAFETY 0.03, TENDER_MIN_PROFIT 300, TENDER_BID_MARGIN 0.05, THIN_BOOK_PENALTY 0.05, UNWIND_MIN 100, UNWIND_SLICE 10000, UNWIND_PER_TICK 60000, UNWIND_BAND 0.04, MAX_EXPOSURE 80000, ARB_ENABLE True, ARB_ENTRY 0.06, ARB_EXIT 0.02, ARB_SLICE 2000, ARB_MAX_UNITS 30000, FX_HEDGE_ENABLE True, FX_BAND 25000, LIMIT_HEADROOM 0.90, FALLBACK_GROSS 250000, FALLBACK_NET 150000, NO_NEW_RISK_TICK 290, LOOP_MS 50, BOOK_DEPTH 20`.
**Retuned in the Python bot after practice heats (2026-11-20):** `MAX_EXPOSURE 100000` (tenders are 71k–99k there; note E = net/2, so the 90% net headroom already caps |E| at 90k and the real tender cap is `LIMIT_HEADROOM`), `ARB_MAX_UNITS 10000` (a 30k pair used the gross room tenders needed, and lost money). Python-only: `SETTLE_TIMEOUT_MS 1000, SETTLE_MIN_MS 250, REJECT_BACKOFF_MS 500`.

**CLI:** `python etf_arb.py --key KEY [--host localhost] [--port 9999] [--dry-run] [--log ticks.csv]`. Dry-run prints every decision but sends **no** orders, accepts and declines.

## Lessons already learned (don't regress)
- An early version unwound only top-of-book slices. After a 50k tender it sat unhedged for ~20 ticks and lost **$47k** on one mock heat. Hedging within `UNWIND_BAND` fixed it (+$9.9k). Speed of hedging > price-impact savings.
- Leg balancing used to trade both legs. Now it trades one leg, in the direction that reduces E.
- The mock's permanent-impact model penalizes basket hedging unrealistically. **Don't tune knobs on the mock**; tune on the practice server.
- **Positions lag fills on the real server (2026-11-20 practice heat, lost ~$230k).** `GET /securities` shows a fill ~100–250 ms after the order returns. The original port (and the C bot) re-read stale positions every 50 ms and re-sent the same hedge, overshot, swung between ETF and basket, and churned gross up to 295k/300k by tick 77. Leg balance then resent one rejected order 3,022 times for the rest of the heat. Fixes in the Python bot, all deliberate departures from the C bot:
  - After any trade, wait until `/securities` shows the fills the order responses reported (`SETTLE_TIMEOUT_MS`, `SETTLE_MIN_MS` for unknown sizes). This is not shadow state: RIT is still the truth, the bot only waits for it to catch up. Nothing else is traded in a loop that accepted a tender.
  - Unwind sends all depth within `UNWIND_BAND` (up to budget) in one loop, as orders of ≤ `UNWIND_SLICE`, so the fill wait doesn't slow hedging.
  - Unwind and leg balance check limits: allowed if within headroom or if they don't make gross/|net| worse; leg balance falls back to the other leg. A rejected order pauses trading `REJECT_BACKOFF_MS`.
  - `--log` failing to open warns and continues (the first live launch died on a `PermissionError`).
  - Test with `mock_rit_server.py --position-lag 0.2`; the full-heat test uses it. On it the old bot made money on 1/6 seeds, the fixed bot on 6/6 (and 6/6 without lag).
  - **The C bot still has this bug.** Don't use it on the real server until it's ported.

## Testing
```bash
cd rit-tools
python -m unittest test_etf_arb -v                                     # run first after ANY bot/client change (80 tests, ~35 s)
python mock_rit_server.py --case etf --tick-seconds 0.2 --seed 8      # 60-second heat, prints final NLV
python mock_rit_server.py --case etf --tick-seconds 0.2 --position-lag 0.2  # fills show late, like the real server
python etf_arb.py --key TESTKEY --dry-run                              # decisions only
python etf_arb.py --key TESTKEY --log ticks.csv                        # live against the mock
python mock_rit_server.py --case etf --tick-seconds 0.2 --rate-limit 2 # stress the 429 handling
```
- Run several seeds (4, 8, 15, 16, 23, 42), each on its own `--port`. The C bot made money on 5 of 6 of these. The mock is time-driven and not fully deterministic, so compare **behaviour** (tender decisions, |E| quickly driven to ~0, no API errors, rate-limit waits handled), not exact P&L.
- Wrong key must give a clear 401 message. A stopped RIT Client must give "waiting…" retries, not a crash.
- Also run `python vol_scanner.py --key TESTKEY --once` against `--case vol` to confirm the shared-client refactor didn't break it.

**Definition of done:** runs every seed above without exceptions; behaviour matches the C bot; dry-run sends zero orders; CSV log is written; README updated; `vol_scanner.py` still works.

## Practice server facts (checked 2026-11-20, ETF case, team07)
- Tender `action` is from **our** side: accepting "BUY 76,000" made RITC +76,000.
- Trading RITC **does** change the USD position (−76,000 × 25.90 after that tender).
- `api_orders_per_second` = 50 for every ticker. ~120 requests/tick gave 0 rate limits.
- `/limits` = `LIMIT-STOCK` (gross 300,000 / net 200,000, fine 0.5) and `LIMIT-CASH` (10M). 4,000 RITC showed gross 8,000, so the ETF does count ×2 (ignore the `units: 0.5` in `/securities`). The bot picks `LIMIT-STOCK` by name.
- `/securities` says `trading_fee` 0.02 and `limit_order_rebate` **0.03** (not 0.01) for BULL, BEAR, RITC; USD/CAD 0.
- Field names match the code. Positions lag fills ~100–250 ms (see Lessons).
- **The API answers 401 a few seconds after every heat ends** (seen twice), and the same key works again in the next heat. So `rit_client` exits on 401 only if the key never worked; after a success, a 401 just fails that call and the bot prints "waiting for RIT client…" until the next heat.
- Tenders were 71,000–99,000 RITC (14 seen over three heats), about one every 30 ticks. Most are bigger than `MAX_EXPOSURE` (80k), and above 90k they also break the net headroom (2 × 90k = 180k).
- Fixed bot, live heat from tick 67 (2026-11-20): E stayed at 0, no rejected orders, no fill timeouts, 0 rate limits, gross ≤ 126k. All 6 tenders declined (5 for limit room; #1026 SELL 84k had +$16k estimated edge but 84k > `MAX_EXPOSURE`). Only stat arb traded: 46 entries, 22 exits, all MARKET. NLV −$12.8k at tick 299, before close-out.

## Still unverified
1. Does a marketable limit order earn the rebate or pay the fee? (Still open: the clean heat sent no limit orders, because no tender was accepted, so nothing needed unwinding.)
2. Field names against the Swagger page inside the RIT Client.
3. Does `POST /orders` report `quantity_filled` immediately? Probably yes: 204 market orders and no fill timeouts.

## Repo map
```
PLAYBOOK.md                  finance glossary (P0), API notes (P1), vol-case model (P2)
rit-tools/README.md          how to run everything + practice-server checklist
rit-tools/vol_scanner.py     volatility case scanner / decision helper (Python, stdlib)
rit-tools/mock_rit_server.py fake RIT API for both cases (--case vol|etf)
rit-tools/etf_arb/           C version of the ETF bot (reference implementation for the port)
```
