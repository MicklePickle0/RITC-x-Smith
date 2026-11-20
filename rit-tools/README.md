# rit-tools: RITCxSmith 2026 groundwork

| File | What it is | Language |
|---|---|---|
| `etf_arb.py` | **P3** trading bot for the Algorithmic ETF Arbitrage case (**primary**) | Python 3.8+, standard library only |
| `vol_scanner.py` | **P4** live scanner + decision helper for the Volatility case | Python 3.8+, standard library only |
| `rit_client.py` | Shared RIT REST client used by both of the above | Python 3.8+, standard library only |
| `ETF_BOT.md` | **How the ETF bot works**: every step, formula, worked example, knob and safeguard | doc |
| `test_etf_arb.py` | Tests for the ETF bot and the client (`python -m unittest test_etf_arb -v`) | Python, standard library only |
| `etf_arb/` | The original C version of the ETF bot (alternative, same logic) | C11, no dependencies |
| `mock_rit_server.py` | Fake RIT Client API for both cases, for offline testing | Python, standard library only |

Nothing needs `pip install`.

---

## 0. Competition-day setup (important)

The REST API is served **by the RIT Client running on your own machine** at `http://localhost:9999/v1`. That client is **Windows-only**.
- Run the RIT Client on a Windows laptop, or a Windows VM on a Mac (Parallels/UTM/VMware). Run these tools **on that same machine** (or point `--host` at it).
- In the RIT Client, the API icon in the bottom status bar shows the **port** and the **API key**. Make sure the API is enabled.
- The browser/Mac app uses the separate "DMA API", which these tools don't support. The organizers recommend REST.

## 1. Try everything offline first

```bash
# terminal 1: fake volatility case at 1 tick/second
python3 mock_rit_server.py --case vol
# terminal 2
python3 vol_scanner.py --key TESTKEY --allow-orders
```
```bash
# terminal 1: fake ETF case, 5x speed, restarts after each heat
python3 mock_rit_server.py --case etf --tick-seconds 0.2 --loop
# terminal 2
python etf_arb.py --key TESTKEY --log ticks.csv
```
On Windows use `python`; on macOS/Linux `python3`.
The mock is a **plumbing test**: it checks that requests, parsing, rate-limit handling and the logic flow all work. Its market model is crude (for example, its price-impact model is unrealistic), so **tune parameters on the real practice server, not on the mock.**

---

## 2. Volatility scanner (`vol_scanner.py`)

```bash
python vol_scanner.py --key YOUR_KEY                 # read-only dashboard
python vol_scanner.py --key YOUR_KEY --allow-orders  # type 1/2/3 + Enter to execute a suggestion
python vol_scanner.py --key YOUR_KEY --auto-hedge    # bot keeps delta hedged automatically
```

**What it shows** (refreshes every 0.5 s):
- **Header:** tick, days left, σ this week and next week (parsed from news), **σ life** (time-weighted forecast over the option's remaining life), ATM implied vol, and **EDGE** = σ life − IV in vol points.
- **Risk line:** portfolio delta vs ±7,000, the **dynamic hedge band** (it tightens as gamma grows), the expected 1-second delta drift, and a **size cap** (max straddles you can hold safely right now).
- **Option table:** IV, fair value at σ life, **$ edge per contract after the spread and the $2 fee** for buying and for selling, delta, vega.
- **SUGGESTED ACTIONS**, ranked:
  1. `HEDGE`: buy/sell RTM when delta is outside the band (marked URGENT near the limit)
  2. `BUY/SELL n ATM straddles`: sized from the edge, capped by limits and gamma, with the RTM hedge included
  3. `REDUCE`: when the edge is gone or has flipped, or in the endgame
  4. `PARITY ARB` / `BUTTERFLY ARB`: riskless mispricings across options (rare)

**Commands:** `1`..`9` execute a suggestion · `s 25` override this week's σ to 25% · `n 28.5` override next week's · `c` clear overrides · `q` quit.
Use the overrides if the news wording differs from what the parser expects. Check this on the practice server.

**Suggested team workflow:** each of you runs the scanner on your own login with `--auto-hedge`. Delta penalties are per trader. You decide on the straddle entries and exits; the bot handles hedging.

**Knobs** (top of the file): `ENTRY_VOL_PTS`, `EXIT_VOL_PTS`, `STRADDLES_PER_VOLPT`, `MAX_STRADDLES`, `HEDGE_BAND`, `GAMMA_BUDGET`, `ENDGAME_TICK`.
On the mock, adding the gamma-aware band cut delta penalties from roughly $2–8k to $0.1–1k per heat.

---

## 3. ETF arbitrage bot (`etf_arb.py`)

**Read [`ETF_BOT.md`](ETF_BOT.md) for the full logic**, with worked numbers, a knob-tuning table and how to read the output. Summary below.

### Run
```bash
python etf_arb.py --key YOUR_KEY --dry-run           # prints every decision, sends NO orders/accepts/declines: do this first
python etf_arb.py --key YOUR_KEY --log ticks.csv     # live, one CSV row per tick
python etf_arb.py --key YOUR_KEY --host 127.0.0.1 --port 9999
```
No build step. **All knobs are UPPER_CASE constants at the top of `etf_arb.py`** (same names and defaults as the C `#define`s). Edit them between heats and restart.

**Status line** (once per tick): RITC mid, fair value, deviation in CAD, positions, gross vs limit, NLV, `req` (API requests so far) and `rl` (rate-limit waits so far).

**CSV log** (`--log FILE`, appended, flushed every row): `time, tick, bull_mid, bear_mid, ritc_mid, usd_mid, fair_usd, dev_cad, pos_ritc, pos_bull, pos_bear, pos_usd, E, gross, nlv, requests, rate_limited`. Load it with pandas/Excel after a heat to see how fast E went back to 0 after each tender and which knobs mattered.

### Files
- `rit_client.py`: shared REST client with keep-alive and TCP_NODELAY. **It never retries a POST/DELETE after a network error**, so an order or tender accept can't be sent twice. It retries a GET once if a kept-alive socket went stale. On HTTP 429 it waits the `wait` seconds RIT returns (or `Retry-After`) and retries. A 401 stops the program with a "wrong API key" message, unless the key has already worked: RIT also answers 401 after every heat ends, so then the bot just waits for the next heat.
- `etf_arb.py`: the strategy, ported from `etf_arb/etf_arb.c`, plus fixes for the real server (see "Waiting for fills" below). Every function and knob is commented.
- `test_etf_arb.py`: 80 tests covering every decision rule (with hand-computed numbers), the fill wait and limit checks, the client's safety rules against a real local server, and one full heat on the mock with fills shown 0.2 s late. Run `python -m unittest test_etf_arb -v` after **any** change to the bot. Set `SKIP_SLOW=1` to skip the 30 s full heat.

### Logic (each loop, ~50 ms)
1. Cancel stale resting orders, then read positions and the BULL/BEAR/RITC books. RIT's positions are always treated as the truth.
2. **Tenders:** walk the order book for the full quantity to price two exits: the ETF itself, or the BULL+BEAR basket converted at USD. Accept if `edge/unit > 0` after `TENDER_SAFETY`, `profit ≥ TENDER_MIN_PROFIT`, it fits the limits (ETF counted ×2), and `|E| ≤ MAX_EXPOSURE`. For competitive (non-fixed) tenders it bids its own price. Each tender id is decided once.
3. **Unwind:** drive the ETF-equivalent exposure **E = RITC + (BULL+BEAR)/2** to 0 on whichever venue is cheaper right now. It uses marketable limit orders priced `UNWIND_BAND` through the touch, so it hedges fast without walking the book blindly. A hedged pair (short RITC / long basket) is fine to hold, because close-out happens at the *correct* price.
4. **Leg balance:** keep BULL = BEAR by trading one leg, in the direction that also shrinks E.
5. **Stat arb:** if `RITC×USD` and `BULL+BEAR` diverge by more than `ARB_ENTRY` after fees, trade the pair. It closes early if the reverse trade pays `ARB_EXIT`.
6. **FX hedge:** keep `USD position + RITC position × price` within `FX_BAND`.
7. **Converter hints:** converters are manual-only, so the bot prints when creating or redeeming 10,000 units would free up limits. You click it in the Assets tab.

Steps 4–6 are skipped in any loop where the unwind traded, because positions are stale until the next read.

**Waiting for fills.** On the practice server a fill shows up in `GET /securities` about 100–250 ms *after* the order returns. Trading on the old positions made the first live heat re-send the same hedge over and over: it swung between ETF and basket, churned gross to 295k/300k and lost ~$230k. So after any trade the bot does nothing until the positions show the fills the order responses reported (`SETTLE_TIMEOUT_MS`, default 1 s, then it warns and carries on; `SETTLE_MIN_MS` when a size is unknown). To keep hedging fast anyway, the unwind sends everything within `UNWIND_BAND` in one loop, as orders of at most `UNWIND_SLICE`. Unwind and leg balance never push gross/net past the headroom unless the trade reduces them, and a rejected order pauses trading for `REJECT_BACKOFF_MS`. Reproduce it offline with `mock_rit_server.py --position-lag 0.2`.

The biggest risk it manages is **unhedged exposure after a big tender**. On a mock heat, slicing only the top of the book left the bot short 50k for ~20 ticks and lost $47k. Hedging within a $0.04 band turned that heat into +$9.9k.

**Robustness:** a stopped RIT Client gives `waiting for RIT client...` and the bot resumes by itself; if `/limits` was unavailable at start it keeps retrying and switches to the real limits. An unexpected error in one loop is printed with a traceback and the bot keeps going. Ctrl+C cancels open orders before exiting.

**Windows tip:** a refused connection to `localhost` costs ~2 s per address on Windows (it tries IPv6 `::1` first). The client remembers the address that worked, but if startup feels slow, pass `--host 127.0.0.1`.

### Alternative: the C bot (`etf_arb/`)
Same strategy and knobs (as `#define`s), for if Python is unavailable. It has no CSV log. **It does not have the fill wait, limit checks on hedging, or reject backoff, so it churns on the real server (see above). Don't run it live until those are ported.** Another difference: when the unwind falls back from the basket to the ETF because the basket won't fit the limits, the C version still sizes the slice by the basket's depth; the Python version uses the ETF's depth.

| Platform | Command |
|---|---|
| macOS / Linux | `make` |
| Windows, MinGW-w64 (MSYS2) | `mingw32-make WINDOWS=1` |
| Windows, Visual Studio (Developer Prompt) | `cl /O2 etf_arb.c rit.c http.c json.c ws2_32.lib` |

It compiles cleanly with `-Wall -Wextra` on gcc and clang; the Windows build is untested. Run with `./etf_arb --key YOUR_KEY [--dry-run]`. Files: `http.c` (HTTP/1.1, keep-alive, never retries POSTs), `json.c` (parser; `make test_json`), `rit.c` (API wrapper), `etf_arb.c` (strategy).

---

## 4. Check these on the practice server (ports 16620 vol / 16630 ETF)

Checked on 2026-11-20 (ETF case): tender `action` is from your side; trading RITC does move the USD position; `api_orders_per_second` is 50; `/v1/limits` has `LIMIT-STOCK` (300k gross / 200k net, ETF ×2) and `LIMIT-CASH` (10M), and the bot uses `LIMIT-STOCK`; the stated `limit_order_rebate` is **$0.03**; positions lag fills by ~100–250 ms. Still to check:

1. **Fee vs rebate on marketable limits**: does a limit order that fills immediately earn the $0.03 rebate or pay the $0.02 fee? If it earns the rebate, always use marketable limits.
2. **Order responses**: does `POST /orders` report `quantity_filled` straight away? If not, the bot waits `SETTLE_MIN_MS` after each trade instead.
3. **News wording** in the vol case. Watch the scanner's `news:` line parse it. If it doesn't, use the `s`/`n` overrides.
4. Field names against the **Swagger page** in the RIT Client (`/v1/...`).

## 5. Next steps (roadmap)

**P3 (ETF bot)**
- Fill tracking: use the `vwap`/`quantity_filled` returned by `POST /orders` to log realized P&L per tender.
- Smarter unwinding: slice size based on observed book refill per tick, and passive limit orders (to earn rebates) when there's no urgency.
- A tender risk premium that grows with `Q` and current `|E|`, instead of a flat `TENDER_SAFETY`.
- Analyse the `--log` CSVs from practice heats to see which knobs mattered.

**P4 (scanner)**
- Per-strike σ (check whether the MM uses one σ for all strikes; if not, trade the most mispriced strike).
- A model of the MM's learning speed (fit IV(t) vs true σ on practice heats) to time entries and exits.
- An optional `--auto` mode that executes straddle suggestions too, which is allowed in this case. Use it only after validating on the practice server.
