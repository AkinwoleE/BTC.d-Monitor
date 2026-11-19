#!/usr/bin/env bash
# Publish funding snapshots and strategy state to the hedge-data branch.
# The commit message contains [skip ci] so the push does not start another run.
# Credentials come from the Actions checkout. This script does not print the remote URL.
set -euo pipefail

src="${1:-bot/runtime}"
if [ ! -f "$src/funding_snapshots.csv" ] && [ ! -f "$src/state.json" ]; then
  echo "hedge-data: nothing to publish"
  exit 0
fi

out="$(mktemp -d)"
cp "$src/funding_snapshots.csv" "$out/" 2>/dev/null || true
cp "$src/state.json" "$out/" 2>/dev/null || true
cp "$src/decisions.jsonl" "$out/" 2>/dev/null || true
cat > "$out/README.md" << 'EOF'
# hedge-data

Hourly funding snapshots and hedge state written by `.github/workflows/hedge_monitor.yml`.

`funding_snapshots.csv` is public market data (funding, open interest, mids).
`state.json` includes the UTC-day equity baseline used by the daily loss stop, plus open hedge sizes.
`decisions.jsonl` records entries, exits, and repairs.

This branch is in the same repository. If the repository is public, those files are public. Make the repository private before enabling live trading.
EOF

git config user.name "hedge-bot"
git config user.email "actions@users.noreply.github.com"

if git fetch --depth=50 origin +hedge-data:refs/remotes/origin/hedge-data; then
  git checkout -B hedge-data origin/hedge-data
else
  git checkout --orphan hedge-data
  git rm -rf --cached . >/dev/null 2>&1 || true
fi

cp "$out/funding_snapshots.csv" . 2>/dev/null || true
cp "$out/state.json" . 2>/dev/null || true
cp "$out/decisions.jsonl" . 2>/dev/null || true
cp "$out/README.md" README.md

git add -- funding_snapshots.csv state.json decisions.jsonl README.md
if git diff --cached --quiet; then
  echo "hedge-data: no changes"
  exit 0
fi
git commit -m "hedge: funding snapshot [skip ci]"
git push origin hedge-data
echo "hedge-data: published"
