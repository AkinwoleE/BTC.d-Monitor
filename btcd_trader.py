"""
╔══════════════════════════════════════════════════════════════════════╗
║   BTC.D PAIRS TRADER — GitHub Actions Autonomous Bot                ║
║   Runs every 15 minutes via GitHub Actions cron                     ║
║   Signal: 15M BTC/USD vs ETH/BTC divergence                        ║
║   Execution: Direct Decibel REST API                                ║
╚══════════════════════════════════════════════════════════════════════╝

GitHub Secrets required:
  TELEGRAM_BOT_TOKEN      — from @BotFather
  TELEGRAM_CHAT_ID        — your chat ID
  DECIBEL_BEARER_TOKEN    — from geomi.dev (Aptos Mainnet)
  DECIBEL_ACCOUNT         — your Decibel main account address (0x...)
  DECIBEL_SUBACCOUNT      — your Decibel subaccount address (0x...)

Optional secrets (have defaults):
  POSITION_SIZE_USD       — per leg in USD (default: 100)
  LEVERAGE                — leverage multiplier (default: 3)
  SLIPPAGE                — slippage % (default: 1)
"""

import os
import sys
import json
import time
import requests
from datetime import datetime, timezone


# ════════════════════════════════════════════════════════════════════
# CREDENTIALS — all from GitHub Secrets
# ════════════════════════════════════════════════════════════════════
BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")
DEC_BEARER  = os.environ.get("DECIBEL_BEARER_TOKEN", "")
DEC_ACCOUNT = os.environ.get("DECIBEL_ACCOUNT", "")
DEC_SUB     = os.environ.get("DECIBEL_SUBACCOUNT", "")

# ════════════════════════════════════════════════════════════════════
# STRATEGY PARAMETERS — override via GitHub Secrets if needed
# ════════════════════════════════════════════════════════════════════
POSITION_SIZE = float(os.environ.get("POSITION_SIZE_USD", "100"))
LEVERAGE      = int(os.environ.get("LEVERAGE", "3"))
SLIPPAGE      = float(os.environ.get("SLIPPAGE", "1"))

# Max leverage per market on Decibel
MAX_LEV = {"BTC/USD": 40, "ETH/USD": 20}

# ════════════════════════════════════════════════════════════════════
# API ENDPOINTS
# ════════════════════════════════════════════════════════════════════
KRAKEN      = "https://api.kraken.com/0/public"
COINLORE    = "https://api.coinlore.net/api/global/"
DEC_BASE    = "https://api.mainnet.aptoslabs.com/decibel"
DEC_ORIGIN  = "https://app.decibel.trade"
KR_KEY      = {"XBTUSD": "XXBTZUSD", "ETHXBT": "XETHXXBT"}
KR_IV       = {"15m": 15, "1h": 60}

# ════════════════════════════════════════════════════════════════════
# STATE — persisted via GitHub Actions cache between runs
# ════════════════════════════════════════════════════════════════════
STATE_FILE = "trader_state.json"   # in repo root, committed by workflow

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {
            "current_signal":   "NEUTRAL",   # LONG_BTC | LONG_ETH | NEUTRAL
            "position_open":    False,
            "entry_btc_size":   0.0,
            "entry_eth_size":   0.0,
            "entry_ts":         0,
            "last_signal_ts":   0,
            "trade_count":      0,
        }

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"  State saved: signal={state['current_signal']} position={state['position_open']}")


# ════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════
def fmt(n, d=3):
    return f"{n:.{d}f}"

def pct_move(c: dict) -> float:
    if c["open"] == 0: return 0.0
    return ((c["close"] - c["open"]) / c["open"]) * 100

def candle_dir(c: dict) -> str:
    return "up" if c["close"] >= c["open"] else "dn"

def ts_str():
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


# ════════════════════════════════════════════════════════════════════
# DATA FETCHING
# ════════════════════════════════════════════════════════════════════
def fetch_klines(pair: str, tf: str, n: int) -> list:
    interval = KR_IV.get(tf, 15)
    since    = int(time.time()) - interval * 60 * (n + 5)
    r = requests.get(f"{KRAKEN}/OHLC?pair={pair}&interval={interval}&since={since}", timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("error"): raise ValueError(f"Kraken: {data['error']}")
    key = KR_KEY.get(pair) or next(k for k in data["result"] if k != "last")
    return [
        {"ts": int(c[0])*1000, "open": float(c[1]), "high": float(c[2]),
         "low": float(c[3]), "close": float(c[4]), "vol": float(c[6])}
        for c in data["result"][key][-n:]
    ]

def fetch_ticker(pair: str) -> dict:
    r = requests.get(f"{KRAKEN}/Ticker?pair={pair}", timeout=10)
    r.raise_for_status()
    data = r.json()
    key  = list(data["result"].keys())[0]
    t    = data["result"][key]
    last = float(t["c"][0])
    op   = float(t["o"])
    return {"last": last, "pct": ((last - op) / op) * 100}

def fetch_btc_dominance() -> dict | None:
    try:
        r    = requests.get(COINLORE, timeout=10)
        r.raise_for_status()
        data = r.json()
        d    = data[0] if isinstance(data, list) else data
        return {"btc_d": float(d["btc_d"]), "eth_d": float(d["eth_d"])}
    except Exception as e:
        print(f"  CoinLore failed: {e}")
        return None


# ════════════════════════════════════════════════════════════════════
# SIGNAL ENGINE — pure 15M BTC.D / ETH·BTC divergence
# Exactly matches the HTML bot logic
# ════════════════════════════════════════════════════════════════════
def compute_signal(btc15: list, eb15: list) -> dict:
    # Use last 3 CLOSED candles (exclude index -1 which is still forming)
    l3b = btc15[-4:-1]
    l3e = eb15[-4:-1]

    if len(l3b) < 3:
        return {"signal": "NEUTRAL", "strength": 0, "btc_dir": "dn", "eb_dir": "dn"}

    btc_up_count = sum(1 for c in l3b if candle_dir(c) == "up")
    eb_up_count  = sum(1 for c in l3e if candle_dir(c) == "up")

    btc_dir = "up" if btc_up_count >= 2 else "dn"
    eb_dir  = "up" if eb_up_count  >= 2 else "dn"

    # Divergence strength — % of last 16 candles that diverged
    n   = min(len(btc15), len(eb15), 16)
    div = sum(
        1 for i in range(n)
        if candle_dir(btc15[-n + i]) != candle_dir(eb15[-n + i])
    )
    strength = round((div / n) * 5)

    if btc_dir == "up" and eb_dir == "dn":
        signal = "LONG_BTC"
    elif btc_dir == "dn" and eb_dir == "up":
        signal = "LONG_ETH"
    else:
        signal = "NEUTRAL"

    return {
        "signal":   signal,
        "strength": strength,
        "btc_dir":  btc_dir,
        "eb_dir":   eb_dir,
        "btc_pct":  pct_move(btc15[-2]),   # last closed candle
        "eb_pct":   pct_move(eb15[-2]),
    }


# ════════════════════════════════════════════════════════════════════
# DECIBEL REST API
# ════════════════════════════════════════════════════════════════════
def dec_headers() -> dict:
    return {
        "Authorization": f"Bearer {DEC_BEARER}",
        "Origin":        DEC_ORIGIN,
        "Content-Type":  "application/json",
    }

def dec_get(path: str, params: str = "") -> dict:
    url = f"{DEC_BASE}{path}" + (f"?{params}" if params else "")
    r   = requests.get(url, headers=dec_headers(), timeout=15)
    if not r.ok:
        raise RuntimeError(f"Decibel GET {path} → {r.status_code}: {r.text[:120]}")
    return r.json()

def dec_post(path: str, body: dict) -> dict:
    r = requests.post(
        f"{DEC_BASE}{path}",
        headers=dec_headers(),
        json=body,
        timeout=15,
    )
    # Handle empty response body gracefully
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:200]}
    if not r.ok:
        raise RuntimeError(f"Decibel POST {path} → {r.status_code}: {json.dumps(data)[:150]}")
    return data

def set_leverage(symbol: str, lev: int):
    eff = min(lev, MAX_LEV.get(symbol, 10))
    try:
        # Try both field name formats
        try:
            dec_post("/api/v1/leverage", {"symbol": symbol, "leverage": eff})
        except Exception:
            dec_post("/api/v1/leverage", {"market": symbol, "leverage": eff})
        print(f"  Leverage set: {symbol} = {eff}×")
    except Exception as e:
        print(f"  Leverage warning ({symbol}): {e}")

def place_order(symbol: str, side: str, size: float) -> dict:
    """side: 'long' or 'short'"""
    body = {
        "symbol":      symbol,
        "side":        side,
        "sz":          size,
        "size":        size,
        "slippage":    SLIPPAGE,
        "reduce_only": False,
        "reduceOnly":  False,
    }
    print(f"  Placing {side.upper()} {symbol} size={size}")
    print(f"  Request body: {json.dumps(body)}")
    # Also log raw response for debugging
    r = requests.post(
        f"{DEC_BASE}/api/v1/orders/market",
        headers=dec_headers(),
        json=body,
        timeout=15,
    )
    print(f"  HTTP status: {r.status_code}")
    print(f"  Raw response: {r.text[:200]}")
    try:
        result = r.json()
    except Exception:
        result = {"raw": r.text[:200]}
    if not r.ok:
        raise RuntimeError(f"Order failed {r.status_code}: {r.text[:150]}")
    print(f"  Order result: {json.dumps(result)[:100]}")
    return result

def close_position(symbol: str) -> dict:
    body = {
        "symbol":   symbol,
        "slippage": SLIPPAGE,
    }
    print(f"  Closing {symbol}...")
    result = dec_post("/api/v1/orders/close", body)
    print(f"  Close result: {json.dumps(result)[:100]}")
    return result

def get_account_overview() -> dict | None:
    try:
        data = dec_get("/api/v1/account_overviews", f"account={DEC_ACCOUNT}")
        ov   = data[0] if isinstance(data, list) else data
        return {
            "equity":   float(ov.get("equity") or ov.get("account_value") or 0),
            "avail":    float(ov.get("available_margin") or ov.get("withdrawable") or 0),
            "pnl":      float(ov.get("unrealized_pnl") or 0),
        }
    except Exception as e:
        print(f"  Account overview failed: {e}")
        return None

def get_open_positions() -> list:
    try:
        data = dec_get("/api/v1/account_positions", f"account={DEC_ACCOUNT}")
        all_pos = data if isinstance(data, list) else data.get("positions", [])
        return [p for p in all_pos if abs(float(p.get("size") or p.get("sz") or 0)) > 0]
    except Exception as e:
        print(f"  Positions fetch failed: {e}")
        return []


# ════════════════════════════════════════════════════════════════════
# TRADE EXECUTION
# ════════════════════════════════════════════════════════════════════
def execute_trade(signal: str, btc_price: float, eth_price_usd: float) -> dict:
    """Places both legs of the pairs trade."""
    eff_btc_lev = min(LEVERAGE, MAX_LEV["BTC/USD"])
    eff_eth_lev = min(LEVERAGE, MAX_LEV["ETH/USD"])

    btc_size = round(POSITION_SIZE / btc_price, 5)
    eth_size = round(POSITION_SIZE / eth_price_usd, 4)

    print(f"  Executing {signal}: ${POSITION_SIZE}/leg · {LEVERAGE}×")
    print(f"  BTC size: {btc_size} · ETH size: {eth_size}")

    results = {}
    if signal == "LONG_BTC":
        set_leverage("BTC/USD", eff_btc_lev)
        results["btc"] = place_order("BTC/USD", "long",  btc_size)
        time.sleep(0.5)
        set_leverage("ETH/USD", eff_eth_lev)
        results["eth"] = place_order("ETH/USD", "short", eth_size)
    else:  # LONG_ETH
        set_leverage("BTC/USD", eff_btc_lev)
        results["btc"] = place_order("BTC/USD", "short", btc_size)
        time.sleep(0.5)
        set_leverage("ETH/USD", eff_eth_lev)
        results["eth"] = place_order("ETH/USD", "long",  eth_size)

    return {"btc_size": btc_size, "eth_size": eth_size, "results": results}

def close_all_positions():
    """Closes both BTC/USD and ETH/USD positions."""
    for sym in ["BTC/USD", "ETH/USD"]:
        try:
            close_position(sym)
        except Exception as e:
            print(f"  Close {sym} error: {e}")


# ════════════════════════════════════════════════════════════════════
# TELEGRAM
# ════════════════════════════════════════════════════════════════════
def send_telegram(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("  Telegram: missing credentials")
        return False
    r    = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )
    data = r.json()
    if data.get("ok"):
        print("  ✓ Telegram sent")
        return True
    print(f"  ✗ Telegram failed: {data.get('description')}")
    return False

def build_trade_open_msg(signal: str, sig: dict, btcdom, btc_price: float, eth_price: float, sizes: dict, acct: dict | None) -> str:
    arrow    = "⬆️" if signal == "LONG_BTC" else "⬇️"
    bias     = "Long BTC / Short ETH" if signal == "LONG_BTC" else "Long ETH / Short BTC"
    str_bars = "█" * sig["strength"] + "░" * (5 - sig["strength"])
    dom_line = f"\n📊 BTC.D: <b>{fmt(btcdom['btc_d'], 2)}%</b>" if btcdom else ""
    acct_line = (
        f"\n\n💰 Account equity: <b>${fmt(acct['equity'], 2)}</b>"
        f" · Available: <b>${fmt(acct['avail'], 2)}</b>"
    ) if acct else ""

    return (
        f"{arrow} <b>PAIRS TRADE OPENED</b> — {bias}\n\n"
        f"📈 Signal: <b>{signal.replace('_', ' ')}</b>\n"
        f"⚡ Strength: {str_bars} {sig['strength']}/5\n"
        f"🕯 BTC 15M: <b>{'+' if sig['btc_pct'] >= 0 else ''}{fmt(sig['btc_pct'], 3)}%</b> · "
        f"ETH/BTC 15M: <b>{'+' if sig['eb_pct'] >= 0 else ''}{fmt(sig['eb_pct'], 3)}%</b>"
        f"{dom_line}\n\n"
        f"─────────────────────\n"
        f"📦 <b>Position Details</b>\n"
        f"BTC/USD: {sizes['btc_size']} BTC @ ~${fmt(btc_price, 0)} · {min(LEVERAGE, 40)}×\n"
        f"ETH/USD: {sizes['eth_size']} ETH @ ~${fmt(eth_price, 2)} · {min(LEVERAGE, 20)}×\n"
        f"Size: ${POSITION_SIZE}/leg · ${POSITION_SIZE * 2} total"
        f"{acct_line}\n\n"
        f"<i>{ts_str()} · GitHub Actions runner</i>"
    )

def build_trade_close_msg(reason: str, old_signal: str, new_signal: str, acct: dict | None) -> str:
    acct_line = (
        f"\n💰 Equity: <b>${fmt(acct['equity'], 2)}</b>"
        f" · PNL: <b>{'+' if acct['pnl'] >= 0 else ''}${fmt(acct['pnl'], 2)}</b>"
    ) if acct else ""
    return (
        f"✕ <b>POSITIONS CLOSED</b>\n\n"
        f"Reason: {reason}\n"
        f"Was: <b>{old_signal.replace('_', ' ')}</b>"
        f" → Now: <b>{new_signal.replace('_', ' ')}</b>"
        f"{acct_line}\n\n"
        f"<i>{ts_str()} · GitHub Actions runner</i>"
    )

def build_neutral_msg(sig: dict, btcdom) -> str:
    str_bars = "█" * sig["strength"] + "░" * (5 - sig["strength"])
    dom_line = f" · BTC.D {fmt(btcdom['btc_d'], 2)}%" if btcdom else ""
    return (
        f"⏸ <b>No signal — standing aside</b>\n\n"
        f"BTC 15M: {'+' if sig['btc_pct'] >= 0 else ''}{fmt(sig['btc_pct'], 3)}% · "
        f"ETH/BTC 15M: {'+' if sig['eb_pct'] >= 0 else ''}{fmt(sig['eb_pct'], 3)}%{dom_line}\n"
        f"Strength: {str_bars} {sig['strength']}/5\n\n"
        f"<i>{ts_str()}</i>"
    )


# ════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════
def run():
    now_ts = int(datetime.now(timezone.utc).timestamp())
    print(f"\n{'='*60}")
    print(f"BTC.D Pairs Trader — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")

    # ── Load state ──────────────────────────────────────────────────
    state = load_state()
    print(f"  Last signal: {state['current_signal']} · Position open: {state['position_open']}")

    # ── Validate credentials ────────────────────────────────────────
    has_decibel = bool(DEC_BEARER and DEC_ACCOUNT)
    if not has_decibel:
        print("  WARNING: Decibel credentials missing — signal-only mode")
    if not BOT_TOKEN or not CHAT_ID:
        print("  WARNING: Telegram credentials missing — no notifications")

    # ── Fetch market data ───────────────────────────────────────────
    print("\n  Fetching market data...")
    btc15    = fetch_klines("XBTUSD", "15m", 120)
    eb15     = fetch_klines("ETHXBT", "15m", 120)
    btc_tick = fetch_ticker("XBTUSD")
    eb_tick  = fetch_ticker("ETHXBT")
    btcdom   = fetch_btc_dominance()

    btc_price     = btc_tick["last"]
    eth_price_usd = btc_price * eb_tick["last"]  # derive ETH/USD from BTC × ETH/BTC

    print(f"  BTC: ${fmt(btc_price, 0)} · ETH/BTC: {fmt(eb_tick['last'], 6)}")
    if btcdom:
        print(f"  BTC.D: {fmt(btcdom['btc_d'], 2)}%")

    # ── Compute signal ──────────────────────────────────────────────
    sig    = compute_signal(btc15, eb15)
    signal = sig["signal"]

    print(f"\n  Signal: {signal}")
    print(f"  BTC 15M: {'+' if sig['btc_pct'] >= 0 else ''}{fmt(sig['btc_pct'], 3)}% ({sig['btc_dir']})")
    print(f"  ETH/BTC 15M: {'+' if sig['eb_pct'] >= 0 else ''}{fmt(sig['eb_pct'], 3)}% ({sig['eb_dir']})")
    print(f"  Divergence strength: {sig['strength']}/5")

    # ── Get account state ───────────────────────────────────────────
    acct = get_account_overview() if has_decibel else None
    if acct:
        print(f"  Equity: ${fmt(acct['equity'], 2)} · PNL: ${fmt(acct['pnl'], 2)}")

    # ── Act on signal ───────────────────────────────────────────────
    old_signal = state["current_signal"]
    acted      = False

    if signal != old_signal:
        print(f"\n  Signal changed: {old_signal} → {signal}")

        # Close existing position if one is open
        if state["position_open"] and old_signal != "NEUTRAL":
            print(f"  Closing {old_signal} position...")
            if has_decibel:
                close_all_positions()
                time.sleep(2)
            reason = f"Signal flipped to {signal}"
            acct = get_account_overview() if has_decibel else None
            send_telegram(build_trade_close_msg(reason, old_signal, signal, acct))
            state["position_open"]  = False
            state["entry_btc_size"] = 0.0
            state["entry_eth_size"] = 0.0

        # Open new position if signal is directional
        if signal != "NEUTRAL":
            print(f"\n  Opening {signal} position...")
            if has_decibel:
                sizes  = execute_trade(signal, btc_price, eth_price_usd)
                acct   = get_account_overview()
                state["position_open"]  = True
                state["entry_btc_size"] = sizes["btc_size"]
                state["entry_eth_size"] = sizes["eth_size"]
                state["entry_ts"]       = now_ts
                state["trade_count"]   += 1
                send_telegram(build_trade_open_msg(signal, sig, btcdom, btc_price, eth_price_usd, sizes, acct))
            else:
                # Signal-only mode — send alert without trading
                bias = "Long BTC / Short ETH" if signal == "LONG_BTC" else "Long ETH / Short BTC"
                send_telegram(
                    f"📡 <b>SIGNAL: {bias}</b> (signal-only mode — add Decibel credentials to trade)\n\n"
                    f"Strength: {'█' * sig['strength']}{'░' * (5 - sig['strength'])} {sig['strength']}/5\n"
                    f"<i>{ts_str()}</i>"
                )
        elif signal == "NEUTRAL" and old_signal != "NEUTRAL":
            # Signal went neutral — only notify, don't close (close happens on flip)
            send_telegram(build_neutral_msg(sig, btcdom))

        state["current_signal"] = signal
        state["last_signal_ts"] = now_ts
        acted = True

    else:
        print(f"  Signal unchanged ({signal}) — holding.")
        # Refresh account for logging
        if has_decibel and state["position_open"]:
            acct = get_account_overview()
            if acct:
                print(f"  Live PNL: ${fmt(acct['pnl'], 2)}")

    # ── Save state ──────────────────────────────────────────────────
    save_state(state)
    print(f"\n  Done. Acted: {acted}")
    return 0


# ════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        try:
            send_telegram(
                f"🔴 <b>BTC.D Trader — Fatal Error</b>\n\n"
                f"<code>{str(e)[:300]}</code>\n\n"
                f"<i>{ts_str()}</i>"
            )
        except Exception:
            pass
        sys.exit(1)
