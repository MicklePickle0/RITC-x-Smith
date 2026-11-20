# RITCxSmith 2026 Playbook

Competition: **Sat Nov 21**, 11:30 to 13:45 trading. The two cases count 50/50, and your score is your **rank per heat, averaged**, so steady, penalty-free P&L beats one big win.

---

## Cheat sheet (print this)

| | Volatility case | ETF arbitrage case |
|---|---|---|
| Who trades | Both of you, and your P&L is summed | One of you, algorithm only |
| Length | 300 s = 1 month = 20 trading days | 300 s = 1 month |
| Securities | RTM ETF plus 10 European options: calls and puts at strikes 48, 49, 50, 51 and 52 | CAD, USD (currencies), BULL and BEAR (CAD stocks), RITC (USD ETF) |
| Fees | RTM $0.02/share; options $2.00/contract | Market orders $0.02/share; limit-order fills earn a $0.01/share **rebate** |
| Max order | 10,000 RTM shares; 100 option contracts | 10,000 shares (stocks and ETF); 2,500,000 for currencies |
| Limits | RTM 50,000 gross/net; options 2,500 gross / 1,000 net contracts | Check the client or `/v1/limits`. The **ETF counts ×2** toward limits |
| Killer rule | Delta must stay within **±7,000**, or you pay **$0.10 per unit of excess delta per second** | Converters (ETF creation/redemption) are **manual only** and cost $1,500 USD per use (10,000 units) |
| Close-out | RTM closes at the last price; options are cash-settled at max(S−K,0) or max(K−S,0) | Open positions close at the last price, or at the *correct* price if there is a shock divergence |
| Key fact | One contract is 100 shares. Year = 240 days. T(years) = (300 − tick)/3600 | Equilibrium: RITC(USD) × USD/CAD = BULL + BEAR |

---

# P0: What is going on (every term you'll meet)

## Market mechanics

- **Order book.** The list of resting buy orders (**bids**) and sell orders (**asks**/offers) at each price, with the quantity at each level.
- **Best bid / best ask (top of book).** The highest bid and the lowest ask. **Spread** = ask − bid. **Mid** = (bid + ask)/2, the usual estimate of "true" price.
- **Market order.** "Fill me now at whatever price is there." You *take* liquidity, pay the spread and, in the ETF case, pay the $0.02 fee.
- **Limit order.** "Fill me only at price X or better." It rests in the book and *provides* liquidity. In the ETF case a filled limit order earns a $0.01 **rebate** (you get paid). The risk is that it may never fill.
- **Passive vs aggressive.** Passive means resting a limit order. Aggressive means a market order, or a limit order priced through the spread.
- **Walking the book / market impact / slippage.** A big market order eats level 1, then level 2, and so on, so your average fill price (**VWAP**, volume-weighted average price) gets worse. That's **liquidity risk**: the cost of being too big for the book.
- **Long / short.** Long means you own it and profit if the price rises. Short means you sold what you don't own (negative position) and profit if the price falls.
- **Position.** Your signed quantity. **NLV** (net liquidation value) is what your book is worth at current prices.
- **Realized vs unrealized P&L.** Realized means locked in by closing trades. Unrealized means mark-to-market on what you still hold.
- **Gross limit** = Σ|positions|. **Net limit** = |Σ positions| (shorts cancel longs). You cannot exceed them: orders get rejected.
- **Tick.** One unit of case time (1 second here). **Heat.** One run of a case. You play several and get ranked in each.

## Options

- **Call.** The right to *buy* at strike K. Payout at expiry = max(S − K, 0).
- **Put.** The right to *sell* at K. Payout = max(K − S, 0).
- **European.** Can only be exercised at expiry (no early exercise). That makes Black-Scholes exact for pricing here.
- **Cash-settled.** No shares change hands at expiry. You just receive the payout in cash.
- **Strike (K).** The fixed price in the contract. Here K ∈ {48, …, 52} and RTM starts near 50.
- **Moneyness.** ATM (at the money) is K ≈ S. ITM (in the money) is a call with K < S or a put with K > S. OTM (out of the money) is the reverse.
- **Premium.** The option's price. **Contract multiplier 100**: a quote of $1.15 costs $115 per contract (plus a $2 fee).
- **Intrinsic value** = the payout if it expired now. **Time value** = premium − intrinsic, which is what you pay for uncertainty (volatility).
- **Put-call parity** (European, r = 0, no dividends): **C − P = S − K**. If the market violates this by more than costs, there's a riskless arbitrage.
- **Straddle.** Long (or short) a call **and** a put at the same strike. Delta ≈ 0 at the money. It's a pure bet that the stock moves a lot (long) or a little (short).
- **Strangle.** The same idea with an OTM call and an OTM put. **Vertical spread.** Long one strike and short another of the same type.

## Volatility

- **Volatility (σ).** The annualized standard deviation of returns. RTM's process: P_t = P_{t−1}·(1 + r), r ~ N(0, σ_tick). That's a **martingale**: the expected future price = today's price (no drift), so you can't predict direction. Only variance matters.
- **Realized volatility.** How much the price *actually* moves. Your analysts tell you this week's value exactly and give a range for next week.
- **Implied volatility (IV).** The σ that makes Black-Scholes match the market price. It's the market maker's *belief*, read out of prices.
- **Black-Scholes (BS).** The formula price(S, K, T, r, σ). Every input is observable except σ. Price increases with σ for both calls and puts.
- **Variance risk premium.** The usual real-world gap where IV is above realized volatility. **Here it's zero**, so in equilibrium IV = true σ.
- **The core opportunity.** The market maker (MM) *lags*. When σ jumps at the start of a week, the MM's IV is stale and only *gradually learns* the new σ. You know σ before the MM does, so:
  - true σ > IV → options are **cheap** → **buy** them;
  - true σ < IV → options are **expensive** → **sell** (write) them.
- **Vol point.** 1 percentage point of σ (e.g. 20% → 21%).

## Greeks (just partial derivatives of the BS price)

Numbers below are for S = 50 at the start of the case (T = 1/12 year):

| Greek | Meaning | Value per contract at K = 50, σ = 20% |
|---|---|---|
| **Delta Δ** = ∂V/∂S | $ change per $1 move in RTM, i.e. "how many shares this behaves like" | Call ≈ +51 shares, put ≈ −49 shares |
| **Gamma Γ** = ∂Δ/∂S | How fast delta changes as RTM moves | ≈ 14 delta per $1 move. **Explodes near expiry**: ≈ 76 per $1 with 10 s left |
| **Vega** = ∂V/∂σ | $ change per 1 vol point | ≈ $5.76 (ATM), ≈ $4.40–4.70 at 48 and 52 |
| **Theta** = ∂V/∂t | Value lost per unit of time passing | Long options bleed theta. Short options earn it |

- **Portfolio delta** = RTM shares + Σ (contracts × 100 × option delta). This is the number the CRO penalizes.
- **Delta hedging.** Trading RTM so that portfolio delta ≈ 0, which leaves you exposed to volatility (vega and gamma) but not direction.
- **Long gamma.** You own options. As RTM moves, your hedge makes money (buy low, sell high when re-hedging). You profit when realized vol > IV.
- **Short gamma.** You wrote options. Re-hedging loses money (buy high, sell low). You profit when realized vol < IV. It's dangerous near expiry because delta swings wildly.

## ETF case terms

- **ETF (exchange-traded fund).** A security that holds a basket. RITC holds 1 BULL + 1 BEAR.
- **NAV / fair value.** What the basket is worth: here **(BULL + BEAR) / USD** in USD.
- **Base currency / direct quote.** Everything is scored in CAD. The USD price means *CAD per 1 USD* (e.g. 1.00 or 1.35). RITC trades in USD, so its CAD value = RITC × USD.
- **FX exposure.** If you hold USD-priced things and USD/CAD moves, your CAD P&L moves. Hedge by trading the USD currency ticker.
- **Tender offer.** A private offer from an institution: "I'll buy X RITC from you at $P" or "sell you X RITC at $P", valid for a few seconds. In RIT, `action` in the API is from **your** side (BUY = you buy if you accept). Confirm this on the practice server.
- **Fixed-bid vs competitive tender.** With `is_fixed_bid = true` you accept or decline at their price. With `false` you submit your own price (an auction), and the best price wins.
- **Arbitrage.** Profiting from the same thing priced differently in two places.
  - *Tender arb:* buy from the institution below fair value and sell in the market above it (or the reverse).
  - *ETF vs basket arb:* if RITC × USD > BULL + BEAR, sell RITC and buy BULL + BEAR (and the reverse). Because open positions are closed at the **correct** price after a shock, this convergence is close to guaranteed by the end of the heat.
- **Creation / redemption (converters).** Turn 10,000 BULL + 10,000 BEAR into 10,000 RITC (creation), or back (redemption), for $1,500 USD (= $0.15/unit). You use them when the order book is too thin to unwind a big tender. **Manual click only:** the algorithm can't call them, so the person running the bot has to click them in the RIT Client (Assets tab) while the bot runs.
- **Unwinding.** Getting rid of a large tender position by slicing it into many smaller orders so you don't crash the price.

---

# P1: RIT API findings

**Short answer:** the REST API is **language-agnostic**, so **C/C++ works**. Any language that can send HTTP requests can use it. The official tutorial just happens to use Python.

### Facts

- **Base URL:** `http://localhost:9999/v1/`. The port can differ, so check the API icon in the RIT Client's bottom status bar.
- **Auth:** header `X-API-Key: <key>`, or the query param `?key=<key>`. The key is shown in the RIT Client.
- **The REST API talks to the RIT *Client* running on your machine, and that client is Windows-only.** Mac users can only use the browser/Mac app, which goes with the "DMA API". I couldn't find public documentation for the DMA API.
  → **At least one Windows machine (or a Windows VM on a Mac) is required for REST.** The organizers strongly recommend REST over DMA.
- **The API must be enabled in the client**, and the client must be connected to the running case.
- **Orders are rate-limited.** Each security reports `api_orders_per_second`. A rate-limited request returns an error with a `wait` field (seconds to wait). Handle it, don't crash. My code does this.

### Endpoints you'll use

| Endpoint | Use | Vol case | ETF case |
|---|---|---|---|
| `GET /case` | `tick`, `period`, `status` (ACTIVE / STOPPED …) | ✔ time to expiry | ✔ loop control |
| `GET /securities[?ticker=]` | position, bid, ask, last, nlv, limits info for every ticker | ✔ positions → portfolio delta | ✔ positions → exposure |
| `GET /securities/book?ticker=&limit=` | full order book (bids/asks with price, quantity, quantity_filled) | (not needed, no impact) | ✔✔ walk the book for tender costing |
| `GET /news[?after=&limit=]` | `news_id`, `tick`, `headline`, `body` | ✔✔ volatility announcements | (maybe shocks) |
| `GET /tenders` | `tender_id`, `ticker`, `action`, `quantity`, `price`, `is_fixed_bid`, `expires` | | ✔✔ |
| `POST /tenders/{id}[?price=]` | accept (price needed for non-fixed bids) | | ✔✔ |
| `DELETE /tenders/{id}` | decline | | ✔ |
| `POST /orders?ticker=&type=MARKET\|LIMIT&quantity=&action=BUY\|SELL[&price=]` | place an order → returns `order_id` | ✔ | ✔✔ |
| `DELETE /orders/{id}` | cancel one | | ✔ |
| `POST /commands/cancel?all=1` (or `ticker=`, `ids=`) | bulk cancel | | ✔ |
| `GET /limits` | gross/net and limits | ✔ | ✔ |
| `GET/POST /leases`, `/assets` | converters are leased assets. **The brief says algorithms can't use them**, so ignore these | | |

The Swagger page inside the RIT Client is the authoritative list. Open it on Friday and check field names against my code.

### Speed reality check for "high-frequency in C"

Every call is an HTTP round-trip to the local client, plus a server-side rate limit, and the case only updates once per tick (1 s). C won't make the *exchange* faster. What it does give you is a tight loop, keep-alive connections and no garbage collection, which is fine. The real edge is **correct logic** (costing tenders, slicing, hedging), not microseconds.

---

# P2: Volatility case model (verbal algorithm)

## The idea in one sentence

You know the true volatility before the market maker does. When the MM's implied vol differs from what you know, trade options in the direction that profits as the MM catches up, and hedge away direction with RTM.

## Step 1: Your volatility forecast for each option's remaining life

The news gives you σ for the current week (exact) and, mid-week, a range for next week. An option that expires at tick 300 lives through the rest of this week **and** later weeks, so the right σ is the **time-weighted average of variance**:

```
σ_life² = Σ_weeks (ticks remaining in that week × σ_week²) / ticks remaining in total
```

- This week: the announced σ.
- Next week: the midpoint of the announced range (until it's announced, assume = this week's σ).
- Weeks after that: unknown, so assume they equal the most recent known value.

Example: tick 50, σ_w1 = 20%, next week announced as 27–30% (mid 28.5%). Remaining: 25 ticks of week 1 and 225 ticks of weeks 2–4. σ_life = √((25·0.20² + 225·0.285²)/250) ≈ 27.8%. So even though this week is calm, the options are **cheap** if the MM still prices 20–22%.

**Why not just use this week's σ?** Because the MM learns. Its IV will move toward each week's σ as the case goes on, so σ_life is both the hold-to-expiry fair value and where IV is headed.

## Step 2: Each tick, for each of the 10 options

1. T = (300 − tick)/3600 years. (The year is 240 days and the case is 20 days.)
2. IV = solve BS(S, K, T, σ) = mid price (bisection).
3. Fair = BS(S, K, T, σ_life).
4. **Edge per contract** = |Fair − Mid| × 100 − $3 (half the spread, $1, plus the $2 fee).
5. Signal: BUY if Fair > ask by more than the threshold, SELL if Fair < bid by more than the threshold, otherwise HOLD.

## Step 3: Decide the trade

- **Vol edge = σ_life − IV(ATM).** Use the ATM option (strike nearest S) as the headline number.
- **Threshold:** only trade if |vol edge| ≥ ~1.5 vol points, or more if the range for next week is wide (model risk). One vol point ATM ≈ $5.76/contract, while costs are $3 to enter (+$3 to exit early).
- **Instrument:** trade **ATM straddles** (call + put at the strike nearest S). Delta is ≈ 0, so your hedge needs stay small, and it's the most vega per contract.
  - Vol too low (σ_life > IV): **buy straddles**.
  - Vol too high (σ_life < IV): **sell straddles**.
- **Size:** scale with edge. Hard cap: 500 straddles (= 1,000 contracts, the net limit). Build in 100-contract clips (the max order size). A sensible default is ~100 straddles per 1 vol point of edge, capped at 500.
- **Check parity too:** if C − P − (S − K) > ~$0.05, sell the call, buy the put and buy 100 RTM per contract pair (and the reverse). That's riskless, but rare.

## Step 4: Delta hedge (the penalty is brutal)

- Portfolio delta = RTM shares + Σ contracts × 100 × Δ_option.
- **Rule:** if |delta| > **3,000**, trade RTM to bring it back to ~0. Never let it drift toward 7,000.
- Why 3,000 and not 6,999? 500 long straddles have gamma ≈ 14,000 delta per $1, and RTM moves ~$0.15–0.25 per tick, so delta can jump 2,000–4,000 **in one second**. You need the buffer.
- What going over costs: being 5,000 over the limit for 20 seconds = 5,000 × 0.10 × 20 = **$10,000**, which can wipe out the heat.
- Hedging costs $0.03/share ($0.01 half-spread + $0.02 fee). Hedge when you need to, not continuously.

## Step 5: Manage through the month

| When | What happens | What you do |
|---|---|---|
| Tick 1 | "This week σ = X%" | Compute σ_life (assume later weeks = X). Compare it to IV and open the position |
| ~Tick 37 (mid-week) | "Next week between A–B%" | Recompute σ_life with the midpoint. If the sign of the edge flips, **reduce or flip** |
| Ticks 76, 151, 226 | The new week's σ is announced | The MM starts converging. Recompute, resize |
| As the MM converges | Edge shrinks below ~0.5 vol points | Take profit: close (costs another $3/contract), or hold to settlement if the edge is still positive |
| Last ~30 ticks | **Gamma explodes** (×5 or more) | Cut short straddles in particular. Keep |delta| tiny. Do not open new positions |
| End | Options are cash-settled at the last price, RTM is closed | Make sure you're delta-flat going into the close |

**Note:** the brief's text says the range comes mid-week (e.g. t = 37), but its sample news schedule shows one at t = 75. Have your scanner parse news whenever it arrives. Don't hard-code ticks.

## Step 6: Two traders

- The penalty and limits are **per trader**, and P&L is summed. Each of you runs the *same* straddle strategy and hedges **your own** delta. That doubles your size, since each of you has 1,000 contracts.
- Suggested split: whoever is faster on the keyboard drives the options entries and exits. Both of you watch your own delta line on the scanner and hedge it.
- Before each heat, agree: target size, edge threshold, hedge band. Don't improvise.

## Mistakes that lose rankings

1. Forgetting the ×100 multiplier (sizing 100× too big or too small).
2. Using T in calendar days instead of (300 − tick)/3600.
3. Getting the delta sign wrong for puts (puts have *negative* delta).
4. Holding a big short straddle into the last 30 seconds.
5. Trading small edges (< $3/contract is a loss after fees).
6. Ignoring a news flip mid-week.

---

# P3 / P4: Groundwork

The code lives in `rit-tools/`. See `rit-tools/README.md` for how to run it, and for the **practice-server checklist** (things the brief doesn't pin down, which you must verify before Saturday).
- **P3**: `etf_arb/` is a dependency-free C bot: HTTP client, JSON parser, RIT wrapper, tender evaluation by walking the book, sliced unwinding, an ETF-vs-basket arb signal and an FX hedge.
- **P4**: `vol_scanner.py` is a live terminal dashboard. It shows IV, greeks and edge per option, checks parity, tracks portfolio delta and prints a ranked list of suggested actions.
- `mock_rit_server.py` is a fake RIT server for both cases, so you can develop and test without the practice server.
