# BTC RSI divergence

The monitor detects. The trader orders. They share `divergences.json` and
nothing else. Trading is on for the primary account. This file does not
turn it off, and it does not change a dry-run default.

## What it trades

BTC/USD perpetuals on Decibel, one position at a time, from RSI-14
divergences on 2h candles.

| Rule | Value |
| --- | --- |
| Patterns | 3-pivot (price makes two higher highs or two lower lows while RSI does the opposite) and 2-pt (one leg, first pivot RSI ≥ 65 or ≤ 35) |
| Pivot | local high/low with 3 strictly lower/higher bars on each side |
| Spacing | 4 to 60 bars between consecutive pivots |
| Timeframe that is traded | 2h only (`DIV_TRADE_TF`, default `2h` in the workflow) |
| 1h | still detected and shown on `divergence.html`; the trader does not act on it |
| Entry | market order once the signal bar has closed. The backtest fills at that bar's close, which is the open of the next 2h bar |
| Take-profit | +3.4% of the entry price |
| Stop | −2% of the entry price |
| Time exit | 48h if neither TP nor SL has filled |
| Same-direction signal while open | skip |
| Opposite signal while open | close and flip |
| New entries | blocked Saturday and Sunday UTC. A position already open is still managed |
| Freshness | a 2h signal older than 4h is not traded. A 1h signal older than 2h is not traded |
| Leverage | 20x cross, unless `DIV_LEVERAGE` / `YAR_LEVERAGE` says otherwise |

The 2-pt pattern is tracked so a live sample can accumulate. The 3-year
backtest that the live rules follow pools both.

## Data

2h detection, the series the trader acts on, uses Binance BTCUSDT native
2h klines from `data-api.binance.vision` (the same host divergence100
uses; `api.binance.com` returns 451 from GitHub Actions). Each run merges
that with the committed archive `data/divergence100/BTCUSDT.json`. Live
rows overwrite the archive on the same open time. The still-forming bar
is dropped.

If that series does not reach the latest closed 2h bar, detection falls
back to Kraken 1h candles resampled into UTC-even 2h bars. The log says
so, and Telegram is sent at most once every 6 hours. Kraken is also still
the 1h feed and the 1h candles used to extend each episode's 48h forward
path. A Kraken failure does not block a good Binance 2h series.

RSI is Wilder. On a short rolling window the seed at the left edge moves
RSI by several points, so the same pivot can qualify weeks later with a
different RSI. Live detection ignores pivots in the first 150 bars
(`RSI_WARMUP_BARS`). `find_divergences()` called without that argument is
what the backtest uses, and it is unchanged: the first 14 bars already
have no RSI, and bar 150 of a history that starts in August 2023 does not
touch a 2026 signal.

An episode that is already older than the freshness window when it is
first seen is not appended. Episodes the current Binance series does not
produce, and whose first pivot is past that series' warm-up, are tagged
`phantom`. They stay in the table on the dashboard with a badge. The
headline stats and the 24h chart skip them. The trade log is not rewritten:
those orders really happened.

## Sizing

Notional is equity × risk% ÷ stop-distance%.

With the defaults that is equity × 2.5% ÷ 2% = 1.25 × equity. A stop then
loses 2.5% of equity, not ~$20. On a $160 account the order is about $200
notional, about $10 of margin at 20x, and about $4 at the stop. The
take-profit in dollars is 3.4% of that notional. The prices sent to
Decibel are still entry × 1.034 and entry × 0.98.

`DIV_POSITION_USD`, when the repository variable is actually set, forces
that dollar notional and the risk percent is ignored. The same is true of
`YAR_POSITION_USD` on Yar's step. An empty GitHub variable is unset.

`DIV_TP_USD` and `DIV_SL_USD` do not set a dollar target. They set the
percent, as they always did: fraction = usd ÷ position, and an unset
position is 1,000. Unset TP/SL variables are $34 and $20 on that $1,000
base, which is +3.4% and −2%. Setting them to 34 and 20 while position is
unset does not change the percents. Setting them to anything else does.

The order size is `round(notional / price, 5)`. If that is below Decibel's
minimum size the trade is skipped and Telegram'd. It is not rounded up.

A manual trade (`manual_trade.yml`) passes an explicit size and does not
use the risk percent.

## Both accounts

| | Primary | Yar |
| --- | --- | --- |
| Workflow step | `divergence_trader.py` | same script, `TRADER_ACCOUNT=yar` |
| On by default | yes (`DIV_TRADING_ENABLED` defaults to `true`) | no (`YAR_TRADING_ENABLED` defaults to `false`) |
| State | `divergence_trader_state.json` | `divergence_trader_state_yar.json` |
| Trades | `divergence_trades.json` | `divergence_trades_yar.json` |
| Equity | `divergence_equity.json` | `divergence_equity_yar.json` |
| Signals | `divergences.json` | the same file, read only |
| Risk variable | `DIV_RISK_PCT` | `YAR_RISK_PCT`, passed into the process as `DIV_RISK_PCT` |
| Fixed-size override | `DIV_POSITION_USD` | `YAR_POSITION_USD` |
| Keys | `DECIBEL_PRIVATE_KEY`, `DECIBEL_SUBACCOUNT` | `YAR_KEY`, `YAR_SUBACCOUNT` |

Yar's step is `continue-on-error: true`. A failure there must not skip the
commit step and must not touch the primary state. The commit step adds
each data file on its own, because one `git add` with a missing path used
to stage nothing.

## Schedule

| Trigger | When | Role |
| --- | --- | --- |
| cron-job.org job **BTC.d Trader**, event `divergence-check` | every 15 min at :00, :15, :30, :45 **Africa/Lagos** | Primary. `repository_dispatch` on this repo |
| GitHub schedule `*/15 * * * *` | every 15 min UTC, often late | Backup |
| `workflow_dispatch` | manual | Same monitor, then both trader steps |

On 2026-11-18 the cron-job.org job was changed from every 30 minutes to
every 15 minutes. Lagos is UTC+1, so those minute marks are the same
minutes in UTC. Do not move it back to 30 minutes: fills 1–3 hours after
the signal bar were about $15 worse than the backtest, which enters when
the signal bar closes.

`cancel-in-progress` stays false. A run killed between the order and the
state write would leave a live position the next run does not know.

## What each run records

The trader writes `last_run_ts` into its state file. A gap over 1 hour
sends Telegram. An entry more than 30 minutes after the signal bar closed
still trades (the 4h freshness gate is unchanged) and the open message
says it is late.

Each open logs the signal time and price, the delay, the decision price,
the fill, slippage versus that decision price (positive means the fill was
worse), and the fee `get_trade_history` reports (`fee_amount`). The close
logs the exit fill and fee the same way. The price the TP/SL is anchored
to is still the order response's fill, not a later history VWAP.

With no position in state and none on the exchange, an equity change of
$1 or more sends Telegram. A read of about $0 is the known empty-snapshot
glitch and does not move the baseline. An open position, or the cycle that
just closed one, clears the baseline so the trade's own PnL is not reported
as unexplained.

## Changelog

### 2026-11-18

Live trading since October was about −$58 per account. The Binance 2h
backtest of the same rules (`test_divergence.py`, the reviewer's script)
is +$466 over 140 trades from August 2023 to mid-November 2026, about
+$3.33 per trade across 3.3 years. From August 2026 it is 9 trades and
+$17 on a $1,000 notional: roughly flat next to the live loss. The rules
below were not the thing that changed. What was traded, when it was
filled, and how big it was did.

Checked against `divergences.json`, the trade logs, the equity files, and
`data/divergence100/BTCUSDT.json`:

| Gap | What the repo shows | What changed |
| --- | --- | --- |
| Kraken vs Binance | Since August, 26 of 40 2h episodes in `divergences.json` are not in a full Binance 2h pass. Three of those were traded (14 Aug, 29 Oct, 30 Oct). Together they are about −$21 per account. The 29 Oct stop is most of it. Every Binance 2h signal since August was already in the file, so switching feeds does not drop a backtest trade | 2h detection uses Binance, with a logged Kraken fallback. Kraken-only episodes are tagged `phantom` and leave the dashboard stats. They are not deleted: the fills were real |
| Late entries | Four Binance signals were filled 55 minutes to about 3 hours after the bar closed (11 Sep, 23 Sep, 8 Oct, 12 Oct). Against the backtest's bar-open fill those four were about $15 worse. The rest of the 2h entries were within a minute | Telegram when an entry is more than 30 minutes late, and when the trader's own `last_run_ts` is more than an hour old. The cron-job.org job is now every 15 minutes (see Schedule) |
| 6 Oct equity drop | Primary equity sat at $228.35 with no position (previous close 25 Sep, next open 8 Oct), printed $237.92 at 15:05 UTC, then $207.12 at 20:13 UTC. Net −$21.23. Yar stayed at $245.97 the whole day | A flat account that moves by $1 or more is alerted. Near-zero reads are still treated as the empty-snapshot glitch |
| Fixed $1,000 at 20x | Every fill was about $1,000 notional. A 2% stop is ~$20, about 15% of a ~$141 account, so one stop dominated the account | Risk sizing, default 2.5% of equity at the stop. TP and SL prices stay +3.4% and −2%. See the variable table |
| Stale warm-up signals | 24 of those 40 2h episodes were committed about 25 days after `confirmed_ts`, which is the left edge of a ~700-hour Kraken window. Eleven pivots are stored with more than one RSI. The trader's 4h gate already refused them; the dashboard counted them | Pivots in the first 150 bars are ignored on the live pass. Episodes that are already old when first seen are not recorded |

`test_divergence.py` reruns the reviewer's backtest on the committed
Binance candles and checks the signal list and the trade list against the
hashes from before this change. The warm-up argument only removes episodes
that use a pivot inside those 150 bars. A 1,000-bar Binance tail, which is
what a live fetch returns, matches the full history for every signal whose
pivots sit past the warm-up.

### Variables

Check Settings → Secrets and variables → Actions → Variables. Nothing in
this change writes those variables. Secrets are not touched.

| Variable | Action |
| --- | --- |
| `DIV_POSITION_USD` | **Clear it if it exists**, any value including 1000. While it is set, the primary account stays on that fixed notional and `DIV_RISK_PCT` is ignored |
| `YAR_POSITION_USD` | **Clear it if it exists**, same reason, Yar's account |
| `DIV_TP_USD`, `DIV_SL_USD` | Leave unset for +3.4% / −2%. If they are set to anything other than 34 and 20, clear them unless that other percent is intentional. 34 and 20 with position unset is the same as unset |
| `YAR_TP_USD`, `YAR_SL_USD` | Same, for Yar |
| `DIV_RISK_PCT` | Optional. A number, not `2.5%`. `2.5` means 2.5% of equity is lost if the stop fills. Unset → 2.5 |
| `YAR_RISK_PCT` | Optional, same meaning. The Yar step maps it onto `DIV_RISK_PCT`. Do not create a `DIV_RISK_PCT` to control Yar |
| `DIV_TRADING_ENABLED` | Leave it. Unset means on |
| `YAR_TRADING_ENABLED` | Leave it. Unset means off |
| `DIV_DRY_RUN`, `YAR_DRY_RUN` | Leave them. This change does not force dry-run on or off |
| `DIV_LEVERAGE`, `YAR_LEVERAGE`, `DIV_MAX_HOURS`, `YAR_MAX_HOURS`, `DIV_TRADE_TF`, `YAR_TRADE_TF`, `DIV_BLOCK_DAYS`, `YAR_BLOCK_DAYS` | Unchanged. Defaults in the workflow are still 2h, Sat/Sun, and the script defaults of 20x and 48h |

### Rollout

The open position is not resized and its TP/SL prices are not moved. The
next entry uses risk sizing only after `DIV_POSITION_USD` /
`YAR_POSITION_USD` are cleared. Until then that account keeps the fixed
size it has today.

The first monitor run after merge tags existing Kraken-only 2h episodes
`phantom` and commits `divergences.json` the way it already commits every
cycle. This pull request does not rewrite that file. New episodes are only
appended when they are still fresh, and only from Binance unless the
fallback line is in the log.

Yar stays off until `YAR_TRADING_ENABLED` is set true. The primary account
stays on.
