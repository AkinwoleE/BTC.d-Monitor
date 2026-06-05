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
"""

import os, sys, json, time, subprocess, requests
from datetime import datetime, timezone

# ── Credentials ─────────────────────────────────────────────────────
BOT_TOKEN       = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID         = os.environ.get("TELEGRAM_CHAT_ID", "")
DEC_PRIVATE_KEY = os.environ.get("DECIBEL_PRIVATE_KEY", "")
DEC_SUB         = os.environ.get("DECIBEL_SUBACCOUNT", "")
DEC_NODE_KEY    = os.environ.get("DECIBEL_NODE_API_KEY", "")
DEC_GAS_KEY     = os.environ.get("DECIBEL_GAS_STATION_API_KEY", "")

# ── Parameters ───────────────────────────────────────────────────────
POSITION_SIZE = float(os.environ.get("POSITION_SIZE_USD", "100"))
LEVERAGE      = int(os.environ.get("LEVERAGE", "3"))
SLIPPAGE      = float(os.environ.get("SLIPPAGE", "1"))
MAX_LEV       = {"BTC/USD": 40, "ETH/USD": 20}

# ── Data sources ─────────────────────────────────────────────────────
KRAKEN   = "https://api.kraken.com/0/public"
COINLORE = "https://api.coinlore.net/api/global/"
KR_KEY   = {"XBTUSD": "XXBTZUSD", "ETHXBT": "XETHXXBT"}
KR_IV    = {"15m": 15, "1h": 60}

# ── State ────────────────────────────────────────────────────────────
STATE_FILE = "trader_state.json"

def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except: return {"current_signal":"NEUTRAL","position_open":False,
                    "entry_btc_size":0.0,"entry_eth_size":0.0,"trade_count":0}

def save_state(s):
    with open(STATE_FILE,"w") as f: json.dump(s,f,indent=2)
    print(f"  State: signal={s['current_signal']} open={s['position_open']}")

# ── Helpers ──────────────────────────────────────────────────────────
fmt   = lambda n,d=3: f"{n:.{d}f}"
pct   = lambda c: ((c["close"]-c["open"])/c["open"]*100) if c["open"] else 0
cdir  = lambda c: "up" if c["close"]>=c["open"] else "dn"
ts_s  = lambda: datetime.now(timezone.utc).strftime("%H:%M UTC")

# ── Data fetching ────────────────────────────────────────────────────
def klines(pair, tf, n):
    iv    = KR_IV.get(tf,15)
    since = int(time.time())-iv*60*(n+5)
    r     = requests.get(f"{KRAKEN}/OHLC?pair={pair}&interval={iv}&since={since}",timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("error"): raise ValueError(f"Kraken: {d['error']}")
    key = KR_KEY.get(pair) or next(k for k in d["result"] if k!="last")
    return [{"ts":int(c[0])*1000,"open":float(c[1]),"high":float(c[2]),
             "low":float(c[3]),"close":float(c[4]),"vol":float(c[6])}
            for c in d["result"][key][-n:]]

def ticker(pair):
    r = requests.get(f"{KRAKEN}/Ticker?pair={pair}",timeout=10); r.raise_for_status()
    d = r.json(); k = list(d["result"].keys())[0]; t = d["result"][k]
    last=float(t["c"][0]); op=float(t["o"])
    return {"last":last,"pct":((last-op)/op*100)}

def btcdom():
    try:
        r=requests.get(COINLORE,timeout=10); r.raise_for_status()
        d=r.json(); v=d[0] if isinstance(d,list) else d
        return {"btc_d":float(v["btc_d"]),"eth_d":float(v["eth_d"])}
    except Exception as e:
        print(f"  CoinLore failed: {e}"); return None

# ── Signal engine ────────────────────────────────────────────────────
def signal(btc15, eb15):
    l3b=btc15[-4:-1]; l3e=eb15[-4:-1]
    if len(l3b)<3:
        return {"signal":"NEUTRAL","strength":0,"btc_dir":"dn","eb_dir":"dn","btc_pct":0,"eb_pct":0}
    bd = "up" if sum(1 for c in l3b if cdir(c)=="up")>=2 else "dn"
    ed = "up" if sum(1 for c in l3e if cdir(c)=="up")>=2 else "dn"
    n  = min(len(btc15),len(eb15),16)
    dv = sum(1 for i in range(n) if cdir(btc15[-n+i])!=cdir(eb15[-n+i]))
    st = round(dv/n*5)
    sg = "LONG_BTC" if bd=="up" and ed=="dn" else "LONG_ETH" if bd=="dn" and ed=="up" else "NEUTRAL"
    return {"signal":sg,"strength":st,"btc_dir":bd,"eb_dir":ed,"btc_pct":pct(btc15[-2]),"eb_pct":pct(eb15[-2])}

# ── Decibel CLI execution ────────────────────────────────────────────
def install_cli():
    print("  Installing @decibeltrade/cli and @decibeltrade/sdk...")
    r = subprocess.run(
        ["npm", "install", "--ignore-scripts",
         "@decibeltrade/cli", "@decibeltrade/sdk"],
        capture_output=True, text=True, timeout=120
    )
    print("  Done." if r.returncode==0 else f"  Warning: {r.stderr[:200]}")

    # Patch: the CLI bundles its own nested @decibeltrade/sdk that has
    # admin.js as ESM but missing its own sub-imports. Fix by replacing
    # the CLI's nested SDK dist with the top-level SDK dist which works.
    import shutil
    src = "node_modules/@decibeltrade/sdk/dist"
    dst = "node_modules/@decibeltrade/cli/node_modules/@decibeltrade/sdk/dist"
    if os.path.exists(src) and os.path.exists(dst):
        print(f"  Patching CLI nested SDK dist...")
        for fname in os.listdir(src):
            src_f = os.path.join(src, fname)
            dst_f = os.path.join(dst, fname)
            if os.path.isfile(src_f):
                shutil.copy2(src_f, dst_f)
        print("  Patch applied.")
    else:
        print(f"  Patch skipped — src={os.path.exists(src)} dst={os.path.exists(dst)}")

    # Confirm CLI is findable
    result = subprocess.run(
        ["node", "-e",
         "try{const m=require.resolve('@decibeltrade/cli');console.log('FOUND:'+m);}catch(e){console.log('NOT_FOUND:'+e.message);}"],
        capture_output=True, text=True, timeout=10
    )
    print(f"  CLI: {result.stdout.strip()[:100]}")

def cli_env():
    e = os.environ.copy()
    e["DECIBEL_NETWORK"]            = "mainnet"
    e["DECIBEL_PRIVATE_KEY"]        = DEC_PRIVATE_KEY
    e["DECIBEL_SUBACCOUNT_ADDRESS"] = DEC_SUB
    e["DECIBEL_NODE_API_KEY"]       = DEC_NODE_KEY
    if DEC_GAS_KEY: e["DECIBEL_GAS_STATION_API_KEY"] = DEC_GAS_KEY
    return e

def run_cli(action, params):
    """Run a Decibel action via the official CLI using ESM imports."""
    # CLI is installed at repo root — use absolute path directly
    cli_path = "/home/runner/work/BTC.d-Monitor/BTC.d-Monitor/node_modules/@decibeltrade/cli/dist/index.js"

    script = f"""
const {{ DecibelClient }} = await import('{cli_path}');

const client = new DecibelClient({{
  network: process.env.DECIBEL_NETWORK,
  privateKey: process.env.DECIBEL_PRIVATE_KEY,
  subaccountAddress: process.env.DECIBEL_SUBACCOUNT_ADDRESS,
  nodeApiKey: process.env.DECIBEL_NODE_API_KEY,
  gasStationApiKey: process.env.DECIBEL_GAS_STATION_API_KEY,
}});
await client.connect();
const p = {json.dumps(params)};
let r;
if      ('{action}'==='place_market_order') r = await client.exchange.placeMarketOrder(p);
else if ('{action}'==='close_position')     r = await client.exchange.closePosition(p);
else if ('{action}'==='set_leverage')       r = await client.exchange.setLeverage(p);
else if ('{action}'==='get_balances')       r = await client.info.getBalances(p);
console.log(JSON.stringify(r));
"""
    script_path = "/tmp/decibel_run.mjs"
    with open(script_path, "w") as f:
        f.write(script)

    result = subprocess.run(
        ["node", script_path],
        env=cli_env(),
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"CLI: {result.stderr[:300]}")
    out = result.stdout.strip()
    if not out: raise RuntimeError("CLI empty response")
    return json.loads(out)

def get_balances():
    try:
        r = run_cli("get_balances", {"subaccountAddress": DEC_SUB})
        return {"equity":float(r.get("perpEquityBalance",0)),
                "avail": float(r.get("crossWithdrawable",0)),
                "pnl":   float(r.get("unrealizedPnl",0))}
    except Exception as e:
        print(f"  Balances failed: {e}"); return None

def set_lev(symbol, lev):
    eff = min(lev, MAX_LEV.get(symbol,10))
    try:
        run_cli("set_leverage", {"symbol":symbol,"leverage":eff})
        print(f"  Leverage: {symbol} = {eff}×")
    except Exception as e:
        print(f"  Leverage warning ({symbol}): {e}")

def place_order(symbol, side, size):
    print(f"  Placing {side.upper()} {symbol} sz={size}")
    r = run_cli("place_market_order",
                {"symbol":symbol,"side":side,"size":size,
                 "slippage":SLIPPAGE,"reduceOnly":False})
    print(f"  Result: {json.dumps(r)[:100]}")
    return r

def close_pos(symbol):
    print(f"  Closing {symbol}...")
    r = run_cli("close_position", {"symbol":symbol,"slippage":SLIPPAGE})
    print(f"  Closed: {json.dumps(r)[:100]}")
    return r

def execute_trade(sig, btc_px, eth_px_usd):
    btc_sz = round(POSITION_SIZE/btc_px, 5)
    eth_sz = round(POSITION_SIZE/eth_px_usd, 4)
    print(f"  {sig}: ${POSITION_SIZE}/leg · {LEVERAGE}× · BTC {btc_sz} · ETH {eth_sz}")
    res = {}
    if sig == "LONG_BTC":
        set_lev("BTC/USD", min(LEVERAGE,40)); res["btc"] = place_order("BTC/USD","long",btc_sz)
        time.sleep(0.5)
        set_lev("ETH/USD", min(LEVERAGE,20)); res["eth"] = place_order("ETH/USD","short",eth_sz)
    else:
        set_lev("BTC/USD", min(LEVERAGE,40)); res["btc"] = place_order("BTC/USD","short",btc_sz)
        time.sleep(0.5)
        set_lev("ETH/USD", min(LEVERAGE,20)); res["eth"] = place_order("ETH/USD","long",eth_sz)
    return {"btc_size":btc_sz,"eth_size":eth_sz,"results":res}

def close_all():
    for sym in ["BTC/USD","ETH/USD"]:
        try: close_pos(sym)
        except Exception as e: print(f"  Close {sym}: {e}")

# ── Telegram ─────────────────────────────────────────────────────────
def tg(text):
    if not BOT_TOKEN or not CHAT_ID: return False
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id":CHAT_ID,"text":text,"parse_mode":"HTML"},timeout=10)
    ok = r.json().get("ok"); print("  ✓ Telegram" if ok else f"  ✗ TG: {r.text[:80]}"); return ok

def msg_open(sig, s, dom, btc_px, eth_px, sizes, acct):
    arrow = "⬆️" if sig=="LONG_BTC" else "⬇️"
    bias  = "Long BTC / Short ETH" if sig=="LONG_BTC" else "Long ETH / Short BTC"
    bars  = "█"*s["strength"]+"░"*(5-s["strength"])
    dom_l = f"\n📊 BTC.D: <b>{fmt(dom['btc_d'],2)}%</b>" if dom else ""
    acc_l = f"\n💰 Equity: <b>${fmt(acct['equity'],2)}</b> · Avail: <b>${fmt(acct['avail'],2)}</b>" if acct else ""
    return (f"{arrow} <b>TRADE OPENED — {bias}</b>\n\n"
            f"⚡ Strength: {bars} {s['strength']}/5\n"
            f"🕯 BTC 15M: <b>{'+' if s['btc_pct']>=0 else ''}{fmt(s['btc_pct'],3)}%</b> · "
            f"ETH/BTC: <b>{'+' if s['eb_pct']>=0 else ''}{fmt(s['eb_pct'],3)}%</b>{dom_l}\n\n"
            f"📦 BTC/USD: {sizes['btc_size']} @ ~${fmt(btc_px,0)} · {min(LEVERAGE,40)}×\n"
            f"📦 ETH/USD: {sizes['eth_size']} @ ~${fmt(eth_px,2)} · {min(LEVERAGE,20)}×\n"
            f"Size: ${POSITION_SIZE}/leg · ${POSITION_SIZE*2} total{acc_l}\n\n"
            f"<i>{ts_s()} · GitHub Actions</i>")

def msg_close(reason, old, new, acct):
    acc_l = f"\n💰 Equity: <b>${fmt(acct['equity'],2)}</b> · PNL: <b>{'+' if acct['pnl']>=0 else ''}${fmt(acct['pnl'],2)}</b>" if acct else ""
    return (f"✕ <b>POSITIONS CLOSED</b>\n\nReason: {reason}\n"
            f"{old} → {new}{acc_l}\n\n<i>{ts_s()} · GitHub Actions</i>")

# ── Main ─────────────────────────────────────────────────────────────
def run():
    print(f"\n{'='*60}\nBTC.D Pairs Trader — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n{'='*60}")
    state = load_state()
    print(f"  Last: {state['current_signal']} · open={state['position_open']}")

    has_dec = bool(DEC_PRIVATE_KEY and DEC_SUB and DEC_NODE_KEY)
    if not has_dec: print("  WARNING: Decibel credentials missing — signal-only mode")

    # Install CLI if trading is enabled
    if has_dec: install_cli()

    print("\n  Fetching data...")
    b15 = klines("XBTUSD","15m",120)
    e15 = klines("ETHXBT","15m",120)
    bt  = ticker("XBTUSD")
    et  = ticker("ETHXBT")
    bd  = btcdom()
    btc_px     = bt["last"]
    eth_px_usd = btc_px * et["last"]
    print(f"  BTC: ${fmt(btc_px,0)} · ETH/BTC: {fmt(et['last'],6)}")
    if bd: print(f"  BTC.D: {fmt(bd['btc_d'],2)}%")

    sig  = signal(b15, e15)
    curr = sig["signal"]
    print(f"\n  Signal: {curr} · BTC {sig['btc_dir']} · ETH/BTC {sig['eb_dir']} · strength {sig['strength']}/5")

    acct = get_balances() if has_dec else None
    if acct: print(f"  Equity: ${fmt(acct['equity'],2)} · PNL: ${fmt(acct['pnl'],2)}")

    old = state["current_signal"]
    acted = False

    if curr != old:
        print(f"\n  Signal changed: {old} → {curr}")
        if state["position_open"] and old != "NEUTRAL":
            print(f"  Closing {old} position...")
            if has_dec: close_all(); time.sleep(2)
            acct = get_balances() if has_dec else None
            tg(msg_close(f"Signal flipped to {curr}", old, curr, acct))
            state["position_open"] = False

        if curr != "NEUTRAL":
            if has_dec:
                sizes = execute_trade(curr, btc_px, eth_px_usd)
                acct  = get_balances()
                state["position_open"]  = True
                state["entry_btc_size"] = sizes["btc_size"]
                state["entry_eth_size"] = sizes["eth_size"]
                state["trade_count"]   += 1
                tg(msg_open(curr, sig, bd, btc_px, eth_px_usd, sizes, acct))
            else:
                bias = "Long BTC/Short ETH" if curr=="LONG_BTC" else "Long ETH/Short BTC"
                tg(f"📡 <b>SIGNAL: {bias}</b> (signal-only — add Decibel keys to trade)\n"
                   f"Strength: {'█'*sig['strength']}{'░'*(5-sig['strength'])} {sig['strength']}/5\n"
                   f"<i>{ts_s()}</i>")
        state["current_signal"] = curr
        acted = True
    else:
        print(f"  Unchanged ({curr}) — holding.")

    save_state(state)
    print(f"\n  Done. Acted: {acted}")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as e:
        print(f"\nFATAL: {e}")
        import traceback; traceback.print_exc()
        try: tg(f"🔴 <b>Trader Error</b>\n<code>{str(e)[:300]}</code>\n<i>{ts_s()}</i>")
        except: pass
        sys.exit(1)
