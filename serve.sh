#!/bin/bash
REPO=~/Downloads/BTC.d-Monitor
kill $(lsof -ti:8080) 2>/dev/null
cd "$REPO"
git pull --rebase --quiet 2>/dev/null
# Auto-pull every 60s in background
(while true; do sleep 60; git pull --rebase --quiet 2>/dev/null; done) &
PULL_PID=$!
echo "Dashboard → http://localhost:8080/performance.html"
echo "Auto-pull running (PID $PULL_PID). Ctrl-C to stop."
python3 -m http.server 8080
kill $PULL_PID 2>/dev/null
