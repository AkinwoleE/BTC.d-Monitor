# hedge-data

Hourly funding snapshots and hedge state written by `.github/workflows/hedge_monitor.yml`.

`funding_snapshots.csv` is public market data (funding, open interest, mids).
`state.json` includes the UTC-day equity baseline used by the daily loss stop, plus open hedge sizes.
`decisions.jsonl` records entries, exits, and repairs.

This branch is in the same repository. If the repository is public, those files are public. Make the repository private before enabling live trading.
