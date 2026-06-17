"""
BTC.D PAIRS TRADER — GitHub Actions Autonomous Bot
Runs every 15 minutes. Uses Decibel CLI for order execution.

GitHub Secrets required:
  TELEGRAM_BOT_TOKEN          — from @BotFather
  TELEGRAM_CHAT_ID            — your chat ID
  DECIBEL_PRIVATE_KEY         — API wallet key (ed25519-priv-0x...)
  DECIBEL_SUBACCOUNT          — subaccount address (0x...)
  DECIBEL_NODE_API_KEY        — Geomi Aptos Mainnet key
  DECIBEL_GAS_STATION_API_KEY — Geomi Gas Station key

Optional:
  POSITION_SIZE_USD  (default 100)
  LEVERAGE           (default 3)
  SLIPPAGE           (default 1)
  TRAIL_PCT          (default 0.50)  — trailing stop pullback fraction
  STOP_LOSS_USD      (default -0.30) — hard PnL floor to close position
"""

import os, sys, json, time, subprocess, requests
from datetime import datetime, timezone

# ── Credentials ──────────────────────────────────────────────────────────────
BOT_TOKEN       = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID         = os.environ.get("TELEGRAM_CHAT_ID", "")
DEC_PRIVATE_KEY = os.environ.get("DECIBEL_PRIVATE_KEY", "")
DEC_SUB         = os.environ.get("DECIBEL_SUBACCOUNT", "")
DEC_NODE_KEY    = os.environ.get("DECIBEL_NODE_API_KEY", "")
DEC_GAS_KEY     = os.environ.get("DECIBEL_GAS_STATION_API_KEY", "")

# ── Parameters ───────────────────────────────────────────────────────────────
POSITION_SIZE = float(os.environ.get("POSITION_SIZE_USD", "100"))
LEVERAGE      = int(os.environ.get("LEVERAGE", "3"))
SLIPPAGE      = float(os.environ.get("SLIPPAGE", "1"))
TRAIL_PCT     = float(os.environ.get("TRAIL_PCT", "0.50"))
STOP_LOSS_USD = float(os.environ.get("STOP_LOSS_USD", "-0.30"))
FEE_RATE      = 0.00034  # Decibel Tier 0 taker rate
MAX_LEV       = {"BTC/USD": 40, "ETH/USD": 20}

# ── Data sources ─────────────────────────────────────────────────────────────
KRAKEN   = "https://api.kraken.com/0/public"
COINLORE = "https://api.coinlore.net/api/global/"
KR_KEY   = {"XBTUSD": "XXBTZUSD", "ETHXBT": "XETHXXBT"}
KR_IV    = {"15m": 15, "1h": 60}

# ── State + log files ─────────────────────────────────────────────────────────
STATE_FILE = "trader_state.json"
LOG_FILE   = "trader_log.json"

def load_state():
    defaults = {
        "current_signal":  "NEUTRAL",
        "position_open":   False,
        "entry_btc_size":  0.0,
        "entry_eth_size":  0.0,
        "trade_count":     0,
        "last_run_utc":    "",
        "entry_time_utc":  "",
        "entry_btc_price": 0.0,
        "entry_eth_price": 0.0,
        "entry_equity":    0.0,
        "fee_threshold":      0.0,
        "peak_pnl":           0.0,
        "trail_active":       False,
        "last_lag_alert_utc": "",
        "open_signal":        "",
    }
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
        defaults.update(saved)
    except Exception:
        pass
    return defaults

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)
    print(f"  State: signal={s['current_signal']} open={s['position_open']}")

def load_log():
    try:
        with open(LOG_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def append_log(entry):
    log = load_log()
    log.append(entry)
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)
    print(f"  Logged: {entry['action']} {entry['signal']}")

# ── Helpers ───────────────────────────────────────────────────────────────────
fmt  = lambda n, d=3: f"{n:.{d}f}"
pct  = lambda c: ((c["close"] - c["open"]) / c["open"] * 100) if c["open"] else 0
cdir = lambda c: "up" if c["close"] >= c["open"] else "dn"
ts_s = lambda: datetime.now(timezone.utc).strftime("%H:%M UTC")

def trade_duration_min(entry_time_utc):
    if not entry_time_utc:
        return None
    try:
        entry_t = datetime.fromisoformat(entry_time_utc)
        return round((datetime.now(timezone.utc) - entry_t).total_seconds() / 60, 1)
    except Exception:
        return None

def calc_fee_threshold(btc_price, btc_size, eth_price, eth_size):
    """Round-trip fee: open + close, both legs, taker rate."""
    return round((btc_price * btc_size + eth_price * eth_size) * FEE_RATE * 2, 4)

def conv_type(btc15, eb15):
    """Classify market regime using same 2-of-3 closed-candle logic as signal()."""
    l3b = btc15[-4:-1]; l3e = eb15[-4:-1]
    if len(l3b) < 3 or len(l3e) < 3:
        return None
    btc_up = sum(1 for c in l3b if cdir(c) == "up") >= 2
    eb_up  = sum(1 for c in l3e if cdir(c) == "up") >= 2
    if btc_up and eb_up:         return "BROAD_RALLY"
    if not btc_up and not eb_up: return "BROAD_SELLOFF"
    if btc_up:                   return "BTC_LEADS_UP"
    return "BTC_LEADS_DOWN"

# ── Data fetching ─────────────────────────────────────────────────────────────
def klines(pair, tf, n):
    iv    = KR_IV.get(tf, 15)
    since = int(time.time()) - iv * 60 * (n + 5)
    r     = requests.get(f"{KRAKEN}/OHLC?pair={pair}&interval={iv}&since={since}", timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("error"):
        raise ValueError(f"Kraken: {d['error']}")
    key = KR_KEY.get(pair) or next(k for k in d["result"] if k != "last")
    return [{"ts": int(c[0])*1000, "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]), "vol": float(c[6])}
            for c in d["result"][key][-n:]]

def ticker(pair):
    r = requests.get(f"{KRAKEN}/Ticker?pair={pair}", timeout=10)
    r.raise_for_status()
    d = r.json(); k = list(d["result"].keys())[0]; t = d["result"][k]
    last = float(t["c"][0]); op = float(t["o"])
    return {"last": last, "pct": ((last - op) / op * 100)}

def btcdom():
    try:
        r = requests.get(COINLORE, timeout=10)
        r.raise_for_status()
        d = r.json(); v = d[0] if isinstance(d, list) else d
        return {"btc_d": float(v["btc_d"]), "eth_d": float(v["eth_d"])}
    except Exception as e:
        print(f"  CoinLore failed: {e}")
        return None

# ── Signal engines ────────────────────────────────────────────────────────────
def signal(btc15, eb15):
    l3b = btc15[-4:-1]; l3e = eb15[-4:-1]
    if len(l3b) < 3:
        return {"signal": "NEUTRAL", "strength": 0, "btc_dir": "dn", "eb_dir": "dn",
                "btc_pct": 0, "eb_pct": 0}
    bd = "up" if sum(1 for c in l3b if cdir(c) == "up") >= 2 else "dn"
    ed = "up" if sum(1 for c in l3e if cdir(c) == "up") >= 2 else "dn"
    n  = min(len(btc15), len(eb15), 16)
    dv = sum(1 for i in range(n) if cdir(btc15[-n+i]) != cdir(eb15[-n+i]))
    st = round(dv / n * 5)
    sg = ("LONG_BTC" if bd == "up" and ed == "dn"
          else "LONG_ETH" if bd == "dn" and ed == "up"
          else "NEUTRAL")
    return {"signal": sg, "strength": st, "btc_dir": bd, "eb_dir": ed,
            "btc_pct": pct(btc15[-2]), "eb_pct": pct(eb15[-2])}

def check_lag_signal(btc1h, eb1h, eb15):
    """
    ETH/BTC lag detection: BTC made a strong 1H move but ETH/BTC hasn't followed yet.
    Fires when: BTC 1H >= 0.10% AND ETH/BTC 1H < 0.04% AND ETH/BTC 15M body < 0.10%.
    Returns alert dict or None. Alert only — no trade execution.
    """
    if len(btc1h) < 2 or len(eb1h) < 2 or len(eb15) < 2:
        return None
    btc_1h_pct  = abs(pct(btc1h[-2]))
    eb_1h_pct   = abs(pct(eb1h[-2]))
    eb_15m_body = abs(pct(eb15[-2]))
    if btc_1h_pct >= 0.10 and eb_1h_pct < 0.04 and eb_15m_body < 0.10:
        btc_dir   = "up" if btc1h[-2]["close"] >= btc1h[-2]["open"] else "down"
        lag_ratio = round(btc_1h_pct / max(eb_1h_pct, 0.001), 1)
        return {
            "btc_1h_pct":   btc_1h_pct,
            "eb_1h_pct":    eb_1h_pct,
            "eb_15m_body":  eb_15m_body,
            "lag_ratio":    lag_ratio,
            "btc_dir":      btc_dir,
            "expected_dir": "UP" if btc_dir == "up" else "DOWN",
        }
    return None

# ── Decibel CLI execution ─────────────────────────────────────────────────────
def install_cli():
    print("  Caching @decibeltrade/cli via npx...")
    r = subprocess.run(
        ["npx", "-y", "--package", "@decibeltrade/cli", "decibel-mcp", "--version"],
        capture_output=True, text=True, timeout=120, env=cli_env()
    )
    print(f"  Cache result: {(r.stdout+r.stderr).strip()[:80]}")

def run_cli(action, params):
    rpc_call = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": action, "arguments": params},
    }) + "\n"
    result = subprocess.run(
        ["npx", "-y", "--package", "@decibeltrade/cli", "decibel-mcp"],
        input=rpc_call, capture_output=True, text=True, timeout=60, env=cli_env(),
    )
    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError(f"MCP no output. stderr: {result.stderr[:200]}")
    for line in stdout.split("\n"):
        line = line.strip()
        if not line: continue
        try:
            msg = json.loads(line)
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(f"MCP error: {msg['error']}")
                content = msg.get("result", {}).get("content", [])
                for item in content:
                    if item.get("type") == "text":
                        try: return json.loads(item["text"])
                        except: return {"result": item["text"]}
                return msg.get("result", {})
        except json.JSONDecodeError:
            continue
    raise RuntimeError(f"No valid JSON-RPC response in: {stdout[:200]}")

def cli_env():
    e = os.environ.copy()
    e["DECIBEL_NETWORK"]            = "mainnet"
    e["DECIBEL_PRIVATE_KEY"]        = DEC_PRIVATE_KEY
    e["DECIBEL_SUBACCOUNT_ADDRESS"] = DEC_SUB
    e["DECIBEL_NODE_API_KEY"]       = DEC_NODE_KEY
    if DEC_GAS_KEY: e["DECIBEL_GAS_STATION_API_KEY"] = DEC_GAS_KEY
    return e

def get_open_bot_positions():
    try:
        rpc_call = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_positions", "arguments": {}}
        }) + "\n"
        result = subprocess.run(
            ["npx", "-y", "--package", "@decibeltrade/cli", "decibel-mcp"],
            input=rpc_call, capture_output=True, text=True, timeout=60, env=cli_env(),
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line: continue
            try:
                msg = json.loads(line)
                if msg.get("id") == 1:
                    content = msg.get("result", {}).get("content", [])
                    for item in content:
                        if item.get("type") == "text":
                            data = json.loads(item["text"])
                            positions = data.get("positions", data if isinstance(data, list) else [])
                            btc_open = any(
                                "BTC" in str(p.get("market","")) and abs(float(p.get("size",0))) > 0
                                for p in positions
                            )
                            eth_open = any(
                                "ETH" in str(p.get("market","")) and abs(float(p.get("size",0))) > 0
                                for p in positions
                            )
                            return {"any_open": btc_open or eth_open, "positions": positions}
            except: continue
    except Exception as e:
        print(f"  Position check failed: {e}")
    return {"any_open": False, "positions": []}

def get_balances():
    try:
        r = run_cli("get_balances", {"subaccountAddress": DEC_SUB})
        return {"equity": float(r.get("perpEquityBalance", 0)),
                "avail":  float(r.get("crossWithdrawable", 0)),
                "pnl":    float(r.get("unrealizedPnl", 0))}
    except Exception as e:
        print(f"  Balances failed: {e}")
        return None

def set_lev(symbol, lev):
    eff = min(lev, MAX_LEV.get(symbol, 10))
    try:
        run_cli("set_leverage", {"symbol": symbol, "leverage": eff})
        print(f"  Leverage: {symbol} = {eff}x")
    except Exception as e:
        print(f"  Leverage warning ({symbol}): {e}")

def place_order(symbol, side, size):
    print(f"  Placing {side.upper()} {symbol} sz={size}")
    r = run_cli("place_market_order",
                {"symbol": symbol, "side": side, "size": size,
                 "slippage": SLIPPAGE, "reduceOnly": False})
    print(f"  Result: {json.dumps(r)[:100]}")
    return r

def close_pos(symbol):
    print(f"  Closing {symbol}...")
    r = run_cli("close_position", {"symbol": symbol, "slippage": SLIPPAGE})
    print(f"  Closed: {json.dumps(r)[:100]}")
    return r

def execute_trade(sig, btc_px, eth_px_usd):
    btc_sz = round(POSITION_SIZE / btc_px, 5)
    eth_sz = round(POSITION_SIZE / eth_px_usd, 4)
    print(f"  {sig}: ${POSITION_SIZE}/leg x{LEVERAGE} BTC={btc_sz} ETH={eth_sz}")
    res = {}
    if sig == "LONG_BTC":
        set_lev("BTC/USD", min(LEVERAGE, 40)); res["btc"] = place_order("BTC/USD", "long",  btc_sz)
        time.sleep(0.5)
        set_lev("ETH/USD", min(LEVERAGE, 20)); res["eth"] = place_order("ETH/USD", "short", eth_sz)
    else:
        set_lev("BTC/USD", min(LEVERAGE, 40)); res["btc"] = place_order("BTC/USD", "short", btc_sz)
        time.sleep(0.5)
        set_lev("ETH/USD", min(LEVERAGE, 20)); res["eth"] = place_order("ETH/USD", "long",  eth_sz)
    return {"btc_size": btc_sz, "eth_size": eth_sz, "results": res}

def close_all():
    for sym in ["BTC/USD", "ETH/USD"]:
        try: close_pos(sym)
        except Exception as e: print(f"  Close {sym}: {e}")

# ── Telegram ──────────────────────────────────────────────────────────────────
def tg(text):
    if not BOT_TOKEN or not CHAT_ID: return False
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    ok = r.json().get("ok")
    print("  Telegram OK" if ok else f"  Telegram FAIL: {r.text[:80]}")
    return ok

def msg_open(sig, s, dom, btc_px, eth_px, sizes, acct):
    arrow = "⬆️" if sig == "LONG_BTC" else "⬇️"
    bias  = "Long BTC / Short ETH" if sig == "LONG_BTC" else "Long ETH / Short BTC"
    bars  = "█" * s["strength"] + "░" * (5 - s["strength"])
    dom_l = f"\n\U0001f4ca BTC.D: <b>{fmt(dom['btc_d'],2)}%</b>" if dom else ""
    acc_l = (f"\n\U0001f4b0 Equity: <b>${fmt(acct['equity'],2)}</b>"
             f" · Avail: <b>${fmt(acct['avail'],2)}</b>" if acct else "")
    return (f"{arrow} <b>TRADE OPENED — {bias}</b>\n\n"
            f"⚡ Strength: {bars} {s['strength']}/5\n"
            f"\U0001f56f BTC 15M: <b>{'+' if s['btc_pct']>=0 else ''}{fmt(s['btc_pct'],3)}%</b>"
            f" · ETH/BTC: <b>{'+' if s['eb_pct']>=0 else ''}{fmt(s['eb_pct'],3)}%</b>{dom_l}\n\n"
            f"\U0001f4e6 BTC/USD: {sizes['btc_size']} @ ~${fmt(btc_px,0)}"
            f" · {min(LEVERAGE,40)}x\n"
            f"\U0001f4e6 ETH/USD: {sizes['eth_size']} @ ~${fmt(eth_px,2)}"
            f" · {min(LEVERAGE,20)}x\n"
            f"Size: ${POSITION_SIZE}/leg · ${POSITION_SIZE*2} total{acc_l}\n\n"
            f"<i>{ts_s()} · GitHub Actions</i>")

def msg_close(reason, old, new, acct):
    acc_l = (f"\n\U0001f4b0 Equity: <b>${fmt(acct['equity'],2)}</b>"
             f" · PNL: <b>{'+' if acct['pnl']>=0 else ''}${fmt(acct['pnl'],2)}</b>"
             if acct else "")
    return (f"✕ <b>POSITIONS CLOSED</b>\n\nReason: {reason}\n"
            f"{old} → {new}{acc_l}\n\n<i>{ts_s()} · GitHub Actions</i>")

def msg_lag(lag):
    sign = "+" if lag["btc_dir"] == "up" else "-"
    return (f"⚡ <b>ETH/BTC LAG SIGNAL</b>\n\n"
            f"BTC 1H move:      <b>{sign}{fmt(lag['btc_1h_pct'],3)}%</b>\n"
            f"ETH/BTC 1H move:  <b>{fmt(lag['eb_1h_pct'],4)}%</b>\n"
            f"ETH/BTC 15M body: <b>{fmt(lag['eb_15m_body'],4)}%</b>\n"
            f"Lag ratio:        <b>{lag['lag_ratio']}x</b>\n\n"
            f"Expected direction: <b>{lag['expected_dir']}</b>\n"
            f"<i>Alert only — no trade placed</i>\n\n"
            f"<i>{ts_s()}</i>")

def msg_trail_stop(current_pnl, peak_pnl, acct):
    acc_l = (f"\n\U0001f4b0 Equity: <b>${fmt(acct['equity'],2)}</b>" if acct else "")
    return (f"📈 <b>TRAILING STOP TRIGGERED</b>\n\n"
            f"Locked: <b>{'+' if current_pnl >= 0 else ''}${fmt(current_pnl,2)}</b>\n"
            f"Peak was: <b>${fmt(peak_pnl,2)}</b>{acc_l}\n\n"
            f"<i>{ts_s()} · GitHub Actions</i>")

# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    print(f"\n{'='*60}")
    print(f"BTC.D Pairs Trader -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")
    state = load_state()
    print(f"  Last: {state['current_signal']}  open={state['position_open']}")

    has_dec = bool(DEC_PRIVATE_KEY and DEC_SUB and DEC_NODE_KEY)
    if not has_dec:
        print("  WARNING: Decibel credentials missing -- signal-only mode")

    if has_dec: install_cli()

    print("\n  Fetching data...")
    b15 = klines("XBTUSD", "15m", 120)
    e15 = klines("ETHXBT", "15m", 120)
    b1h = klines("XBTUSD", "1h",  24)
    e1h = klines("ETHXBT", "1h",  24)
    bt  = ticker("XBTUSD")
    et  = ticker("ETHXBT")
    bd  = btcdom()
    btc_px     = bt["last"]
    eth_px_usd = btc_px * et["last"]
    print(f"  BTC: ${fmt(btc_px,0)}  ETH/BTC: {fmt(et['last'],6)}")
    if bd: print(f"  BTC.D: {fmt(bd['btc_d'],2)}%")

    sig  = signal(b15, e15)
    curr = sig["signal"]
    ct   = conv_type(b15, e15)
    print(f"\n  Signal: {curr}  BTC {sig['btc_dir']}  ETH/BTC {sig['eb_dir']}"
          f"  strength {sig['strength']}/5  convergence={ct}")

    append_log({
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "action":           "SIGNAL",
        "signal":           curr,
        "btc_dir":          sig["btc_dir"],
        "eb_dir":           sig["eb_dir"],
        "signal_strength":  sig["strength"],
        "convergence_type": ct,
        "btc_price":        round(btc_px, 2),
        "position_open":    state["position_open"],
    })

    lag = check_lag_signal(b1h, e1h, e15)
    if lag:
        print(f"  LAG: BTC 1H {fmt(lag['btc_1h_pct'],3)}%  "
              f"ETH/BTC 1H {fmt(lag['eb_1h_pct'],4)}%  ratio {lag['lag_ratio']}x")
        last_lag = state.get("last_lag_alert_utc", "")
        lag_elapsed = None
        if last_lag:
            try:
                lag_elapsed = (datetime.now(timezone.utc) -
                               datetime.fromisoformat(last_lag)).total_seconds() / 60
            except Exception:
                pass
        if lag_elapsed is None or lag_elapsed >= 60:
            tg(msg_lag(lag))
            state["last_lag_alert_utc"] = datetime.now(timezone.utc).isoformat()
        else:
            print(f"  LAG alert suppressed — {fmt(lag_elapsed,1)}m since last (cooldown 60m)")

    acct = get_balances() if has_dec else None
    if acct: print(f"  Equity: ${fmt(acct['equity'],2)}  PNL: ${fmt(acct['pnl'],2)}")

    old   = state["current_signal"]
    acted = False

    live = get_open_bot_positions() if has_dec else {"any_open": state["position_open"]}
    print(f"  Live open: {live['any_open']}  State open: {state['position_open']}")

    if curr != old:
        print(f"\n  Signal changed: {old} -> {curr}")

        # BTC_LEADS hold: skip close/reopen only when a position already exists
        has_position = live["any_open"] or state["position_open"]
        if ct == "BTC_LEADS_UP" and curr == "LONG_BTC" and has_position:
            print("  BTC_LEADS_UP detected — holding into anticipated LONG_BTC signal")
            tg(f"⚡ BTC_LEADS_UP — holding position, LONG_BTC anticipated\n"
               f"<i>{ts_s()} · GitHub Actions</i>")
            state["current_signal"] = curr
            acted = True
        elif ct == "BTC_LEADS_DOWN" and curr == "LONG_ETH" and has_position:
            print("  BTC_LEADS_DOWN detected — holding into anticipated LONG_ETH signal")
            tg(f"⚡ BTC_LEADS_DOWN — holding position, LONG_ETH anticipated\n"
               f"<i>{ts_s()} · GitHub Actions</i>")
            state["current_signal"] = curr
            acted = True
        else:
            need_close = live["any_open"] or (state["position_open"] and old != "NEUTRAL")
            if need_close:
                print(f"  Closing (live={live['any_open']} state={state['position_open']})...")
                if has_dec:
                    close_all()
                    time.sleep(2)
                acct = get_balances() if has_dec else None
                if old != "NEUTRAL":
                    open_sig = state.get("open_signal") or old
                    append_log({
                        "timestamp":              datetime.now(timezone.utc).isoformat(),
                        "action":                 "CLOSE",
                        "signal":                 open_sig,
                        "btc_side":               "long"  if open_sig == "LONG_BTC" else "short",
                        "eth_side":               "short" if open_sig == "LONG_BTC" else "long",
                        "btc_size":               state.get("entry_btc_size", 0),
                        "eth_size":               state.get("entry_eth_size", 0),
                        "btc_entry_price":        state.get("entry_btc_price", 0),
                        "eth_entry_price":        state.get("entry_eth_price", 0),
                        "pnl":                    (round(acct["equity"] -
                                                  state.get("entry_equity", acct["equity"]), 2)
                                                  if acct else None),
                        "trade_duration_minutes": trade_duration_min(state.get("entry_time_utc", "")),
                        "signal_strength":        sig["strength"],
                        "exit_reason":            "SIGNAL_FLIP",
                        "peak_pnl":               state.get("peak_pnl", None),
                        "convergence_type":        ct,
                    })
                tg(msg_close(f"Signal flipped to {curr}", old, curr, acct))
                state["position_open"] = False
                state["open_signal"]   = ""

            if curr != "NEUTRAL":
                if has_dec:
                    sizes   = execute_trade(curr, btc_px, eth_px_usd)
                    acct    = get_balances()
                    now_iso = datetime.now(timezone.utc).isoformat()
                    append_log({
                        "timestamp":              now_iso,
                        "action":                 "OPEN",
                        "signal":                 curr,
                        "btc_side":               "long"  if curr == "LONG_BTC" else "short",
                        "eth_side":               "short" if curr == "LONG_BTC" else "long",
                        "btc_size":               sizes["btc_size"],
                        "eth_size":               sizes["eth_size"],
                        "btc_entry_price":        round(btc_px, 2),
                        "eth_entry_price":        round(eth_px_usd, 2),
                        "pnl":                    None,
                        "trade_duration_minutes": None,
                        "signal_strength":        sig["strength"],
                        "convergence_type":        ct,
                    })
                    state["position_open"]   = True
                    state["entry_btc_size"]  = sizes["btc_size"]
                    state["entry_eth_size"]  = sizes["eth_size"]
                    state["entry_time_utc"]  = now_iso
                    state["entry_btc_price"] = round(btc_px, 2)
                    state["entry_eth_price"] = round(eth_px_usd, 2)
                    state["entry_equity"]    = acct["equity"] if acct else 0.0
                    state["trade_count"]    += 1
                    fee_thr = calc_fee_threshold(btc_px, sizes["btc_size"], eth_px_usd, sizes["eth_size"])
                    state["fee_threshold"]   = fee_thr
                    state["peak_pnl"]        = 0.0
                    state["trail_active"]    = False
                    state["open_signal"]     = curr
                    print(f"  Fee threshold: ${fmt(fee_thr,4)}")
                    tg(msg_open(curr, sig, bd, btc_px, eth_px_usd, sizes, acct))
                else:
                    bias = "Long BTC/Short ETH" if curr == "LONG_BTC" else "Long ETH/Short BTC"
                    tg(f"<b>SIGNAL: {bias}</b> (signal-only -- add Decibel keys to trade)\n"
                       f"Strength: {chr(9608)*sig['strength']}{chr(9617)*(5-sig['strength'])}"
                       f" {sig['strength']}/5\n<i>{ts_s()}</i>")
            state["current_signal"] = curr
            acted = True
    else:
        print(f"  Unchanged ({curr}) -- holding.")
        trail_stopped = False

        if state["position_open"] and has_dec and acct and curr != "NEUTRAL":
            current_pnl = acct.get("pnl", 0.0)
            fee_thr     = state.get("fee_threshold", 0.0)
            peak        = state.get("peak_pnl", 0.0)
            active      = state.get("trail_active", False)

            if current_pnl <= STOP_LOSS_USD:
                print(f"  STOP LOSS: PnL ${fmt(current_pnl,3)} <= floor ${fmt(STOP_LOSS_USD,3)}")
                close_all()
                time.sleep(2)
                acct = get_balances()
                open_sig = state.get("open_signal") or old
                append_log({
                    "timestamp":              datetime.now(timezone.utc).isoformat(),
                    "action":                 "CLOSE",
                    "signal":                 open_sig,
                    "btc_side":               "long"  if open_sig == "LONG_BTC" else "short",
                    "eth_side":               "short" if open_sig == "LONG_BTC" else "long",
                    "btc_size":               state.get("entry_btc_size", 0),
                    "eth_size":               state.get("entry_eth_size", 0),
                    "btc_entry_price":        state.get("entry_btc_price", 0),
                    "eth_entry_price":        state.get("entry_eth_price", 0),
                    "pnl":                    (round(acct["equity"] -
                                              state.get("entry_equity", acct["equity"]), 2)
                                              if acct else None),
                    "trade_duration_minutes": trade_duration_min(state.get("entry_time_utc", "")),
                    "signal_strength":        sig["strength"],
                    "exit_reason":            "STOP_LOSS",
                    "peak_pnl":               state.get("peak_pnl", None),
                    "convergence_type":        ct,
                })
                tg(f"\U0001f6d1 <b>STOP LOSS HIT</b>\n\nPnL: <b>${fmt(current_pnl,3)}</b> reached floor "
                   f"<b>${fmt(STOP_LOSS_USD,3)}</b>\n\n<i>{ts_s()} · GitHub Actions</i>")
                state["position_open"] = False
                state["peak_pnl"]      = 0.0
                state["trail_active"]  = False
                state["open_signal"]   = ""
                trail_stopped = True
                acted = True
            elif current_pnl < fee_thr:
                print(f"  Fee zone — PnL: ${fmt(current_pnl,3)}, need ${fmt(fee_thr,3)} to cover fees")
            else:
                if not active:
                    print(f"  Trail activated — PnL ${fmt(current_pnl,3)} cleared fee threshold ${fmt(fee_thr,3)}")
                    state["trail_active"] = True
                    active = True

                if current_pnl > peak:
                    state["peak_pnl"] = current_pnl
                    peak = current_pnl
                    print(f"  New peak PnL: ${fmt(peak,3)}")

                stop_level = peak * (1 - TRAIL_PCT)
                print(f"  Trail: PnL=${fmt(current_pnl,3)}  peak=${fmt(peak,3)}  stop=${fmt(stop_level,3)}")

                if current_pnl < stop_level:
                    print(f"  TRAIL STOP: ${fmt(current_pnl,3)} < stop ${fmt(stop_level,3)}")
                    close_all()
                    time.sleep(2)
                    acct = get_balances()
                    open_sig = state.get("open_signal") or old
                    append_log({
                        "timestamp":              datetime.now(timezone.utc).isoformat(),
                        "action":                 "CLOSE",
                        "signal":                 open_sig,
                        "btc_side":               "long"  if open_sig == "LONG_BTC" else "short",
                        "eth_side":               "short" if open_sig == "LONG_BTC" else "long",
                        "btc_size":               state.get("entry_btc_size", 0),
                        "eth_size":               state.get("entry_eth_size", 0),
                        "btc_entry_price":        state.get("entry_btc_price", 0),
                        "eth_entry_price":        state.get("entry_eth_price", 0),
                        "pnl":                    (round(acct["equity"] -
                                                  state.get("entry_equity", acct["equity"]), 2)
                                                  if acct else None),
                        "trade_duration_minutes": trade_duration_min(state.get("entry_time_utc", "")),
                        "signal_strength":        sig["strength"],
                        "exit_reason":            "TRAIL_STOP",
                        "peak_pnl":               peak,
                        "convergence_type":        ct,
                    })
                    tg(msg_trail_stop(acct["pnl"] if acct else current_pnl, peak, acct))
                    state["position_open"] = False
                    state["peak_pnl"]      = 0.0
                    state["trail_active"]  = False
                    state["open_signal"]   = ""
                    trail_stopped = True
                    acted = True

        if not trail_stopped and curr == "NEUTRAL" and live["any_open"]:
            print("  Orphaned live positions -- closing...")
            if has_dec:
                close_all()
                time.sleep(2)
            acct = get_balances() if has_dec else None
            if state.get("position_open") and state.get("current_signal","NEUTRAL") != "NEUTRAL":
                open_sig = state.get("open_signal") or state["current_signal"]
                append_log({
                    "timestamp":              datetime.now(timezone.utc).isoformat(),
                    "action":                 "CLOSE",
                    "signal":                 open_sig,
                    "btc_side":               "long"  if open_sig == "LONG_BTC" else "short",
                    "eth_side":               "short" if open_sig == "LONG_BTC" else "long",
                    "btc_size":               state.get("entry_btc_size", 0),
                    "eth_size":               state.get("entry_eth_size", 0),
                    "btc_entry_price":        state.get("entry_btc_price", 0),
                    "eth_entry_price":        state.get("entry_eth_price", 0),
                    "pnl":                    (round(acct["equity"] -
                                              state.get("entry_equity", acct["equity"]), 2)
                                              if acct else None),
                    "trade_duration_minutes": trade_duration_min(state.get("entry_time_utc", "")),
                    "signal_strength":        sig["strength"],
                    "exit_reason":            "ORPHAN_CLEANUP",
                    "peak_pnl":               state.get("peak_pnl", None),
                    "convergence_type":        ct,
                })
            tg(msg_close("Orphaned position cleanup (state reset detected)", old, curr, acct))
            state["position_open"] = False
            state["open_signal"]   = ""
            acted = True
        elif not trail_stopped and live["any_open"] and not state["position_open"]:
            print("  Syncing state -- live positions detected but state said closed.")
            state["position_open"]  = True
            state["current_signal"] = curr

    state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    print(f"\n  Done. Acted: {acted}")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as e:
        print(f"\nFATAL: {e}")
        import traceback; traceback.print_exc()
        try: tg(f"<b>Trader Error</b>\n<code>{str(e)[:300]}</code>\n<i>{ts_s()}</i>")
        except: pass
        sys.exit(1)
