# Turtle System 2 on Hyperliquid

Paper trading is the default. This bot does not place live orders until the
`TURTLE_HL_DRY_RUN` repository variable is set to `false`. Nothing in the
current code change flips that variable.

## What it trades

Turtle System 2, once per completed daily candle, on 28 instruments:
18 crypto names and 10 equity/macro names (`XYZ100`, `SP500`, `GOLD`, `CL`,
`NVDA`, `TSLA`, `AAPL`, `GOOGL`, `MSFT`, `AMZN`).

| Rule | Value |
| --- | --- |
| Entry | 55-day Donchian breakout |
| Exit | 20-day Donchian in the opposite direction |
| Stop | 2N from the latest unit's fill (paper uses the channel price as the fill) |
| Pyramid | one extra unit every 0.5N, up to 4 units, at most one add per candle |
| Unit size | 1% of risk capital per 1N |
| Risk capital | 25% of that leg's account value |
| Notional cap | 3× risk capital, per leg (0.75× that leg's account) |
| Exchange leverage | 3× isolated |
| Excluded from new entries | `CL`, unless `TURTLE_EQUITY_EXCLUDE` says otherwise |

Crypto signals are Binance daily candles (close 00:00 UTC). Orders go to the
main Hyperliquid perp dex. Equity and macro signals are Yahoo Finance session
closes, so a weekend print on the 24/7 perp cannot open a trade. Those orders
go to the `xyz` HIP-3 dex, which has its own collateral. A crypto order cannot
spend xyz margin, and the reverse is also true. Both legs must be funded on
their own.

N is the Turtle 20-day true-range average. A unit's dollar risk at the 2N
stop is 2% of risk capital, which is 0.5% of that leg's account.

## Schedule

The daily candle the bot acts on is yesterday's UTC day. At 00:12 UTC that
bar has closed, and yesterday's US cash session (and the gold/crude
settlement) has been over since 21:00 UTC at the latest.

| Trigger | When | Role |
| --- | --- | --- |
| cron-job.org job **Turtle Dry Run**, id **8401012**, event `turtle-cron-trigger` | **01:12 Africa/Lagos = 00:12 UTC** | Primary. Leave it here. |
| GitHub schedule `10 0 * * *` | 00:10 UTC, often hours late | Backup |
| `workflow_dispatch` | manual | Same bot, same once-per-candle rule |

That cron used to be 00:12 Africa/Lagos (23:12 UTC), which is before the
Binance daily close. The run then re-processed the candle the morning job
had already traded: pyramid adds fired twice on one bar (TRX, TRUMP), and a
position could be stopped by the same candle that opened it (WLFI 12 Sep,
BNB/TRX 30 Oct). It has been moved to 00:12 UTC. Do not move it back.

The 00:10 backup and the 00:12 primary still both see that same closed
candle, and a late GitHub run can arrive hours afterwards. Each instrument
stores `positions[token].last_candle_ts`. The second run logs "already
processed" and does not order. The workflow fast-forwards to latest `main`
before trading, so the later job sees the mark the earlier job pushed.
`workflow_dispatch` of a branch other than `main` does not do that
fast-forward.

Equity bars are not saved until 21:00 UTC on their session date. A partial
Yahoo bar used to freeze in the file (the Nov 16 GOLD bar was one of these).
The next scheduled run refreshes the trailing few sessions and drops any bar
that is still open. That data update is not part of the code pull request.

## Paper and live books

| | Paper (`HL_DRY_RUN` not `false`) | Live (`HL_DRY_RUN=false`) |
| --- | --- | --- |
| State | `turtle_hyperliquid_state.json` | `turtle_hyperliquid_state.live.json` |
| Trades | `turtle_hyperliquid_trades.json` | `turtle_hyperliquid_trades.live.json` |
| Equity | `turtle_hyperliquid_equity.json` | `turtle_hyperliquid_equity.live.json` |

`turtle_hyperliquid_active.json` records which book the last run was. The
dashboard (`turtle.html`) follows it, and the Paper / Live buttons override
it. Mark prices and unrealised PnL come from Hyperliquid `allMids` (main dex
plus `xyz`).

The live files start empty. The first live run does not read the paper book,
so those paper positions are not reported as `CLOSED_EXTERNALLY`.

The first run of a book that has no `last_candle_ts` yet — including the
first paper run after this change, and the first live run — records the
latest completed candle and places no orders. Trading starts on the next
completed candle. That is deliberate: deploying on top of a candle the
previous build already traded must not pyramid or stop it a second time.
Open paper positions are left in place. They are evaluated again on the
following candle.

## Orders

Sizes are rounded down to the coin's `szDecimals`. Prices use Hyperliquid's
perp rule (5 significant figures, at most `6 - szDecimals` decimal places,
integers above 100,000). `Info` and `Exchange` are constructed with
`perp_dexs=["", "xyz"]`, which is what makes `xyz:NVDA` and the other equity
names valid.

A unit is not sent when the size rounds to 0 or when the worse-case IOC
notional (the order price slipped 5% through the signal) is under $10.
Those skips are logged and collected into one Telegram message. A zero
account used to pass the leverage check because `0 <= 0` and then record a
zero-size position. That path is gone. There is no separate
"insufficient margin" skip inside order placement; the size check is the
gate.

In live mode the unit's `entry_px` is the fill's average price, and `fee`
is the USDC fee when the fill query returns it. Stops and the next pyramid
level are measured from that fill. The close records the closing fill and
subtracts entry and exit fees. Paper mode keeps using the channel or
pyramid price, which is what the backtest does.

A pyramid modifies the resting stop in place (same order id, new size and
trigger), so the previous stop stays up until the modify is accepted. If
the modify is rejected, the new stop is placed and only then is the old one
cancelled. The new unit is briefly uncovered; the units already on are not.
A full exit sends the market close first and cancels the stop afterwards,
so a failed close leaves the stop working.

One instrument throwing does not stop the others. State and the trade log
are written after each fill, before the stop call returns, so a crash cannot
forget an order that already filled or replay that candle. A crash anywhere
else sends a Telegram alert and the workflow step fails; the commit step
still runs.

Telegram messages are prefixed with **DRY RUN / PAPER** or **LIVE**.

The workflow installs `hyperliquid-python-sdk==0.24.0`.

## Minimum account

```
unit notional ≈ 0.0025 × account × (price / N)
worse-case notional = unit notional × 0.95
need worse-case notional ≥ $10
account ≥ 4210.53 × (N / price)
```

High-volatility names need more equity, because the unit is smaller. On the
candles in this repo on 16 Nov 2026 the binding name was ZEC (N/price about
12.8%), which needs about $540 before lot rounding. After `szDecimals`
rounding, $750 cleared every name that day. N moves, and integer-lot names
(ADA, DOGE, TRX, WLFI, XRP) can round a borderline size under $10.

Fund **$1,000 in the crypto perp account and $1,000 of collateral on the
xyz dex** before expecting the whole roster to trade. Less than that is
safe: names that cannot clear $10 are skipped and reported, not opened as
dust. The two pools do not share the $1,000.

## Go-live checklist

1. Leave `TURTLE_HL_DRY_RUN` unset or `true` until the steps below are done.
   Setting it to any value other than `false` stays in paper.
2. Confirm cron-job.org job **Turtle Dry Run** (id **8401012**) is
   **01:12 Africa/Lagos (00:12 UTC)** and posts `repository_dispatch` type
   `turtle-cron-trigger`. The GitHub backup stays at `10 0 * * *`.
3. Secrets on the repo: `HYPERLIQUID_API_PRIVATE_KEY` (agent key),
   `HYPERLIQUID_ACCOUNT_ADDRESS` (master account, not the agent address),
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
4. Fund the main perp account and the xyz dex separately. $1,000 on each
   is the practical minimum for the full roster; see the formula above.
   3× isolated is set per market by the bot on the first live entry.
5. Optional repo variable `TURTLE_EQUITY_EXCLUDE`. Empty keeps `CL` out.
   `NONE` excludes nothing. A comma-separated list replaces the default.
6. Do not copy `turtle_hyperliquid_state.json` onto the live filenames.
   Absent live files are the reset. The first live run seeds the latest
   candle, places nothing, and does not emit `CLOSED_EXTERNALLY` for the
   paper book. The next completed candle is the first live decision.
7. Set `TURTLE_HL_DRY_RUN` to `false`. The following messages should start
   with **LIVE**. Until that variable is `false` they start with
   **DRY RUN / PAPER**.
8. Watch the first live candle. A skipped unit names the reason (lot size,
   $10 minimum, or the notional cap). An instrument that errors is skipped;
   the rest of the roster still runs.
9. To abandon the live book: flatten the positions on Hyperliquid, then
   delete `turtle_hyperliquid_state.live.json`,
   `turtle_hyperliquid_trades.live.json`, and
   `turtle_hyperliquid_equity.live.json`. The next run starts flat and
   seeds again. The paper files are untouched.

## Tests

`python -m unittest test_turtle_hyperliquid.py`

Covers lot rounding (against the SDK's `float_to_wire`), the $10 and
zero-capital rejects, one action per candle, the 00:12 UTC session window,
separate paper/live files, fill prices, and stop modify-before-cancel.
It does not call the exchange.
