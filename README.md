# TradeShield: algo trading platform where risk limits always hold (PS-06)

```
backend/   FastAPI + asyncio platform (risk engine, order gateway, candles, ledger, recovery, 021 adapter)
frontend/  React + Tailwind + Recharts dashboard
.env       your 021 credentials (you create it from .env.example; never commit it)
```

## Run it

Full step-by-step (Windows / Mac / Linux): see **RUN_GUIDE.md**. Short version:

```bash
cp .env.example .env            # Windows: copy .env.example .env     then put your UCC + password inside
cd backend
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m unittest discover -s tests                     # 72 offline tests, all should pass
python check_021.py                                      # logs in to 021, lists tokens + live prices, places NO orders
uvicorn tradeshield.app:app --port 8000                  # ONE process, no --reload (see "one token" below)

# second terminal
cd frontend && npm install && npm run dev                # open http://localhost:5173
```
Single-server option: `npm run build` in `frontend/`, then the backend serves the UI at http://localhost:8000.

**Modes.** `TS_BROKER=021` (default whenever `O21_USERNAME` is set) trades on the real 021 sandbox with live prices.
`TS_BROKER=mock` uses the built-in fake exchange (`broker/mock.py`, 10x faster clock) - no account or internet needed,
and the only mode that has the failure-injection switches in the "Demo and test tools" panel.

## Reference strategies (exact rules)

All orders are MARKET orders, product INTRADAY, exchange NSE, and ALL of them pass the platform risk engine first.

| # | Strategy | Symbol | Entry | Exit | Size |
|---|---|---|---|---|---|
| 1 | **Time-Based Entry/Exit** (organisers' strategy 1) | RELIANCE | BUY at 09:15 IST (only inside 09:15-09:20, so a late restart does not enter at a random time) | At 15:15 IST: the platform cancels the strategy's open orders, then closes its position with an opposite order | 10 |
| 2 | **1% Breakout from Open** (organisers' strategy 2) | INFY | LTP >= open x 1.01: BUY. LTP <= open x 0.99: SELL. One entry per day. `open` = the open field of the 021 full packet (first price seen today if absent) | Entry price P = average fill price. Long: sell at P x 1.05 (target) or P x 0.95 (stop). Short: buy at P x 0.95 (target) or P x 1.05 (stop). Opposite order, same quantity. No broker-side target/stop. Anything left at 15:15 IST is squared off | 10 |
| 3 | **Trend Breakout** (our own) | TCS | Uses 1m AND 5m candles built from ticks: 5-min EMA(2) vs EMA(4) gives the trend; BUY if trend up and the 1-min close breaks above the previous 5 one-minute highs, SELL if trend down and it breaks below the 5 lows | Exit when the 5-min trend flips | 10 |
| + | Mean Reversion, Tick Momentum, Runaway (deliberately broken, to prove the limits hold) | | see `strategies.py` | | |

Every number above (quantity, times, percentages, interval) is a strategy parameter in `strategies.py`, and quantity/interval and the risk limits are editable from the dashboard.

## Using the real 021 sandbox: how it works (`backend/tradeshield/broker/o21.py`)

* **Auth**: `POST /auth/token` with UCC + password. The API allows ONE live token per account, so TradeShield logs in once and re-logs in
  automatically on a 401. Run one backend process only, and do not log in from Postman while it runs (that revokes the platform's token).
* **Prices**: market websocket, mode `full` (LTP, volume, open) for the symbols in `TS_SYMBOLS`; reconnects with back-off and a fresh key.
  The feed sends the latest value at most every 300 ms, so candles are built from those samples.
* **Idempotency without a client id**: 021 has no `client_order_id`. Before sending, the platform stores its own id in `o21_order_map.json`
  and, once accepted, the 021 `orderId` next to it. If the reply is lost (timeout, 429, 503, a transient 500), the gateway asks the adapter
  "does this id exist?"; the adapter looks in `GET /orders` for an unmapped order with the same token, side, quantity and time and *adopts* it.
  Only when nothing matches is the order resent. (Limit: two identical orders in the same second cannot be told apart; the effect on position is the same.)
* **Fills**: the orders websocket only says "something changed"; the truth is read from `GET /orders` and `GET /orders/{id}/trades` (REST is the
  source of truth, as the API guide advises), every trade becomes one fill with a unique id, applied exactly once. A 2 s poll covers missed socket events.
* **Errors**: 429/502/503/504 and 500 with a generic message are "outcome unknown" (verify, then retry); other 4xx and 500 with a business message
  (e.g. insufficient funds) are final rejections and are recorded with the reason. Retries/relogins show up in the dashboard Activity log.
* **Kill switch**: cancels (waits until 021 confirms the cancel), then closes positions with market orders, re-checking order state until flat or 10 s.
* **Crash recovery**: the order map and database survive a restart; reconciliation asks 021 for orders, trades and positions and repairs differences.

## How the requirements map to code

| Requirement | Where |
|---|---|
| Accounts, strategy subscription | `db.py`, `app.py` (/api/register, /api/subscriptions) |
| 1m and 5m candles from raw ticks (+ custom 15m, 1h, tick candles) | `candles.py` |
| Orders on sandbox, fills tracked, partial fills, rejections | `gateway.py`, `broker/o21.py` (real 021), `broker/mock.py` (offline) |
| Position in sync with account, exact trades, net P&L after charges | `ledger.py`, `gateway.apply_fill`, `recovery.py` |
| Max daily loss / max position / max orders per minute, enforced by platform | `risk.py` (strategies only emit `Signal`s, see `strategies.py`) |
| Kill switch: stop all, cancel orders, close positions within 10 s | `engine.kill_switch` |
| 3+ strategies at once, own P&L/position, long and short same stock | `strategies.py` (6 strategies), per-strategy rows in `positions` table |
| Organiser strategies 1 and 2 + our own (rules above) | `strategies.py`: `TimedEntryExit`, `OpenBreakout`, `TrendBreakout` |
| Dashboard | `frontend/` |
| Crash recovery and reconciliation | `recovery.py` |
| Idempotent order gateway | `gateway.py` (`client_order_id`, ask-before-resend, `fill_id` de-dup) |
| Adaptive Risk Shield | `risk.effective_limits` (50% of loss budget used => limits x0.5, 75% => x0.25) |
| What-If Risk Simulator | `risk.simulate` (read-only preview), `/api/what-if`, `frontend/src/components/WhatIf.jsx` |

## UI features

* **Dark / light mode** (sun/moon button, remembered, follows your system by default). All colours are CSS variables in `frontend/src/index.css`.
* **Web and mobile layouts from one codebase**: on a laptop everything is on one screen; below 1024px the app switches to a phone layout
  with a bottom tab bar (Home, Strategies, Chart, What-If, Activity), strategy cards instead of tables and a scrolling ticker.
* **Candlestick chart** (`CandleChart.jsx`, plain SVG): green = close above open, red = close below open, wicks, volume bars,
  last-price tag and a crosshair (hover or touch). Interval buttons (1m, 5m, 40t ...) plus "Add" for custom intervals like 15m, 1h, 100t.
* **Live ticker** with up/down arrows and a price flash.

## What-If Risk Simulator (how it works)

Pick a strategy, symbol, side and quantity. The platform answers WITHOUT placing anything or changing any state:
1. **Verdict**: would the real risk engine allow it? Every check is listed (kill switch, strategy status, daily loss, position size, orders/min), including ALL failures, not just the first.
2. **Impact**: position before/after (including unfilled pending orders), average price, order value, estimated charges, account net position, day P&L, loss budget used and Adaptive Shield tier before/after.
3. **Stress test**: P&L, loss used and shield tier if the price moves -5%, -2%, -1%, +1%, +2%, +5%, and which move would use up the daily-loss budget.
4. **Kill switch preview**: which positions would be closed and the estimated cost.
Tests prove the verdict always equals what `evaluate()` really does and that simulating changes nothing (`tests/test_platform.py::WhatIfTests`).

## Design decisions to explain in the viva

1. **Strategies are untrusted.** They can only return `Signal`s. Every order goes `Signal -> RiskEngine -> Gateway -> Broker`.
   A crashing strategy is caught and halted (`HALTED_ERROR`); a spamming one is blocked and auto-halted (runaway detector).
2. **Position limits count pending orders** (worst case: everything fills). Without this a runaway strategy could exceed the limit
   by firing many orders before the first fill returns. (Found by a failing test.)
3. **Orders are saved BEFORE they are sent**, with a unique `client_order_id`. On a timeout the gateway first asks the broker
   "do you have this id?" and only resends (same id) if the answer is no.
4. **Each fill is applied exactly once** (`fill_id` is unique), so replays/recovery can never double-count.
5. **Per-strategy sub-ledgers.** The account has one real position per stock; the platform tracks a virtual position per strategy.
   Reconciliation compares the SUM of sub-ledgers with the broker.
6. **Reconciliation fails safe**: if it cannot reach the broker, trading stays paused. Differences it cannot attribute go to an
   "Unattributed" bucket so the account still matches the broker.
7. **Daily-loss breach** halts the strategy AND closes its positions. Kill switch blocks new orders first, then cancels and
   flattens in parallel and repeats until flat or 10 s.
8. **Config-driven**: limits and strategy settings (candle interval, qty) are editable live in the UI, which helps with the live change request.

## Demo script (2 minutes)

(Steps 3 and 5 use the failure-injection switches, which exist in `TS_BROKER=mock` mode only. With the real sandbox the same situations happen on their own: see the Activity log for `O21_RETRY`, `ORDER_ADOPTED`, `O21_RELOGIN`.)

0. Try the sun/moon button, then open the page on your phone (same Wi-Fi: `npm run dev -- --host`, then use the printed network address).
1. Register, subscribe to all four strategies (one is the deliberately broken "Runaway" one).
2. Watch Runaway get blocked by the risk engine (Activity log), then halted. Others keep trading.
3. Open *Demo and test tools*: turn on **Lost acknowledgements** and **Network errors**: orders still appear exactly once.
4. **Send same order twice**: result shows `orders_created: 1`.
5. **Simulate server crash** while orders are open: Activity log shows what was recovered and corrected.
6. Settings on a strategy: set max position to 5, watch orders get blocked with the reason.
7. Open the **What-If** panel: try Buy 500 and watch it turn red with every failing check; compare the stress table.
8. Press **Kill switch**: banner shows elapsed seconds and that positions are flat.

## Known limitations (be upfront about these)

* **Not verified against the live 021 servers by the authors of this code.** The 021 adapter is tested offline (`tests/test_o21.py`, 41 tests)
  against a fake 021 server written from the API guide. Run `python check_021.py` first thing; if a field name or status differs from the guide, `broker/o21.py` is the only file to adjust.
* The FastAPI layer (`app.py`) could not be started where the 021 code was written (no packages could be installed); it only received small changes (logging).
* The charges used for net P&L are the generic model in `config.py` (brokerage, STT, exchange, GST) until the organisers' charges table is plugged in (`config.py`, marked TODO).
* Database is **SQLite** (zero setup); the proposal says PostgreSQL + Redis. All SQL is in `db.py`; rate limiting and live state are in memory. Listed as future scope.
* One broker account is shared by all platform users. 021 orders that were not created by TradeShield and are still open at startup are treated as orphans and cancelled by reconciliation.
* 021 squares off INTRADAY positions itself before close; those RMS orders are not ours, so reconciliation books the difference to an "Unattributed" bucket.
* Strategy 1 and 2 use the wall clock in IST, so they only act during the 09:15-15:15 window. Candle history is not persisted, so strategies warm up again after a restart.
