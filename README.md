# BTC.d Monitor

Delta-neutral funding hedge for Decibel (Aptos) and Hyperliquid, plus the older divergence and turtle bots documented in `docs/DIVERGENCE.md` and `docs/TURTLE.md`.

## Funding hedge

The hedge is short the perp whose funding is higher and long the other, in the same base size, so the book is delta-neutral and earns the funding spread. Decibel is the maker leg (post-only). Hyperliquid follows the size that actually filled, then uses a taker fallback. A scheduled run does not send orders unless live mode is on.

Code lives in `bot/`. `bot/hedge.py` is the order CLI. `bot/strategy.py` is the hourly decision engine. `bot/decibel_bridge.mjs` is the official Decibel TypeScript SDK, with Gas Station sponsorship.

### This repository is public

Actions logs, the `hedge-data` branch, and the **Hedge alerts** issue are public for as long as the repository is public.

Logs are one summary line. They do not include keys, addresses, or balances. The alert issue includes the pair, size, direction, and liquidation distance, not equity and not keys. `hedge-data` stores funding snapshots (public market data) and `state.json`, which includes the daily equity baseline and open sizes because the loss stop needs them.

Make the repository private before setting `LIVE_TRADING=true`.

### Secrets

The order script reads these environment names. The repository secrets use different names. The workflows map them. Nothing in this table is a guess at a secret's value. `gh secret list` is not available to this automation (HTTP 403); the names are the ones referenced by the existing workflows and by `docs/TURTLE.md` / `btcd_trader.py`.

| Script variable | Repository secret | Why this mapping |
| --- | --- | --- |
| `GEOMI_CLIENT_KEY` | `DECIBEL_NODE_API_KEY` | No secret is named `GEOMI_CLIENT_KEY`. `btcd_trader.py` calls `DECIBEL_NODE_API_KEY` the Geomi Aptos Mainnet key. The hedge sends it as the Decibel REST bearer. |
| `GEOMI_NODE_KEY` (optional) | `DECIBEL_NODE_API_KEY` | Same key. The bridge uses it as `nodeApiKey` and falls back to `GEOMI_CLIENT_KEY`. There is no separate node-key secret in the workflows. |
| `GEOMI_GAS_STATION_KEY` | `DECIBEL_GAS_STATION_API_KEY` | Documented as the Geomi Gas Station key. |
| `DECIBEL_API_WALLET_PRIVATE_KEY` | `DECIBEL_PRIVATE_KEY` | Documented as the Decibel API wallet (ed25519). |
| `HL_AGENT_PRIVATE_KEY` | `HYPERLIQUID_API_PRIVATE_KEY` | `docs/TURTLE.md` calls this the Hyperliquid agent key. |

Addresses are not secrets. They are in `bot/config.json`:

- Hyperliquid master `0x8bDaF4d7e8A35364A14fA93461bBACb4454ff018`
- Decibel subaccount `0xe91f3109b1558db1c77c09fa19e1f6765c2872235446b6302b11ca363231911f`

`HYPERLIQUID_ACCOUNT_ADDRESS` and `DECIBEL_SUBACCOUNT` exist for the other bots. This hedge does not trade those values. Each monitor run compares them with `bot/config.json` and, if they differ, comments on the alert issue without printing either address.

### Config defaults

`bot/config.json`:

| Limit | Default |
| --- | --- |
| Per-pair notional | $300 (new entries; the already-open 0.005 BTC hedge is not force-shrunk to this) |
| Total notional | $600 |
| Leverage | 3x, using live venue equity |
| Concurrent pairs | 3 |
| New entries per hour | 1 |
| Liquidation distance floor | 25% (close that pair) |
| Daily loss stop | $25 from the first equity print of the UTC day (flatten) |
| Entry | 7-day net above $1 after round-trip maker fees and an adverse cross-venue gap |
| Exit | forward 7-day net below $0 after close fees, or 4 consecutive hours of negative carry |
| History | at least 6 hourly snapshots, averaged over up to 24, including the current hour |
| Decibel and Hyperliquid open interest | at least $10,000 each |
| Size vs Decibel open interest | at most 10% |
| BTC size cap in `hedge.py` | 0.01 BTC |
| Maker fee assumed | 0.015% per leg |

The caps were chosen for about $141 on Decibel and $331 on Hyperliquid. Leverage uses the balances read that hour, not those two figures.

### What the hourly run does

`.github/workflows/hedge_monitor.yml` runs at minute 0 of every hour, and on `workflow_dispatch`. Concurrency group `hedge-bot` does not cancel an in-progress run, so a resting order is not killed by the next hour.

The run scans perps listed on both venues, appends an hourly funding snapshot, reads live positions, and then:

1. Repairs a naked leg (Decibel post-only first when Decibel must change; Hyperliquid follows the filled size, with a taker fallback). A gap below the venue minimum is closed instead of grown.
2. Closes a pair whose liquidation distance is under 25%, whose book breaks 3x or $600, or whose UTC-day equity is down $25.
3. Closes a hedge whose funding spread has decayed or stayed negative for 4 hours, once 6 snapshots exist.
4. Opens at most one new hedge when the spread is attractive, the kill switch is off, and the caps allow it.

Live positions are the source of truth. The open 0.005 BTC short-Decibel / long-Hyperliquid hedge is adopted the first time it is seen. Entry spread is remembered in `state.json`. Snapshots and state are committed to the `hedge-data` branch (`[skip ci]`) and uploaded as the `hedge-snapshot` artifact.

### Paper and live

Paper is the default. `LIVE_TRADING` unset, empty, or anything other than `true` does not send orders. Risk actions are comments only.

To turn live trading on:

1. Make this repository private.
2. Settings → Secrets and variables → Actions → Variables.
3. Set `LIVE_TRADING` to `true`.
4. Leave `BOT_ENABLED` unset or set it to `true`. Do not commit a file named `bot/TRADING_PAUSED`.
5. Run **Hedge monitor** with `workflow_dispatch`, or wait for the next hour.

Set `LIVE_TRADING` to `false` or delete the variable to go back to paper. The next run stops sending orders. It does not flatten by itself unless a risk or exit rule says so.

`BOT_ENABLED=false`, or a committed `bot/TRADING_PAUSED` file, blocks new entries only. Live mode still repairs a naked leg and still runs the risk and funding exits. A manual live open is blocked too. A manual close is not.

### Open and close by hand

Actions → **Hedge open** or **Hedge close**. Both are `workflow_dispatch` only.

- `dry_run` defaults to true (simulate, including the Decibel bridge simulation; nothing is signed for Hyperliquid).
- Set `dry_run` to false to send real orders. That does not require `LIVE_TRADING`. It does require the kill switch to be off for an open.
- `pair` is the base symbol (`BTC`, `ETH`). `size` defaults to `0.005`. Direction is `short-dec-long-hl` or `long-dec-short-hl`.
- `hedge.py` still refuses a BTC size above 0.01, a size below the Decibel minimum, leverage above 3x, or a Hyperliquid order under $10. This workflow does not pass `--force`.

The same concurrency group as the monitor is used, so a manual run waits instead of overlapping an hourly run.

### Alerts

The workflow keeps a single open issue titled **Hedge alerts** and tries to pin it (the Actions token often cannot pin; pin it once by hand). A comment is posted when something changes. A quiet hour does not comment, so you are not mailed every hour.

| Comment | Meaning |
| --- | --- |
| `ENTER` | Live mode sent an open, or paper mode would have. |
| `SIGNAL` | The spread is attractive and the bot did not take it (paused, at the cap, or a risk action is in the same run). Repeated hours stay quiet. |
| `EXIT` / `REPAIR` | Close, flatten, or hedge a naked leg. In paper mode this is the action that was not sent. |
| `CLEAR` | A risk condition from an earlier comment is gone. |
| `ERROR` | The run failed (missing key, venue error, order rejected). |

Liquidation distance under 25% is repeated at most every 6 hours until it clears. A live entry, exit, or repair is always commented.

### Local checks

```bash
python -m unittest bot/test_strategy.py
python bot/strategy.py public-check
```

`public-check` reads the public Hyperliquid account and the Decibel BTC position from chain. It does not need the Geomi key and it does not send orders. `hedge.py status` without `HEDGE_REDACT=1` prints balances; do not run that in Actions.

### Isolation from the RSI bot

The RSI divergence trader, the BTC.D pairs trader, and Turtle already use these accounts. They do not manage the hedge.

- Decibel orders and fills use the client id prefix `hedge-` (`hedge-open-…`, `hedge-close-…`).
- Filled legs are listed in `state.json` on the `hedge-data` branch (`dec_sz` / `hl_sz` per coin).
- Another bot ignores a position only when that bot has no position of its own there and one of these is true: the state file records the leg, the order or fill carries the `hedge-` prefix, or the other venue has the opposite leg at about the same size. `fallback_book` in `bot/config.json` does not hide a position by itself. A size that only matches that static list still gets the original alert and stand-down.
- The divergence and Turtle workflows load `hedge-data` `state.json` before they run. Hedge alerts go to the **Hedge alerts** issue. Divergence and Turtle keep Telegram.

A BTC position the RSI bot itself opened stays in its state file and is still managed. The hedge does not write that file.
