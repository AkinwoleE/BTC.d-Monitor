#!/usr/bin/env python3
"""
Delta-neutral hedge: Decibel (maker leader, via official TS SDK bridge) + Hyperliquid (follower, official python SDK).

  ./hedge.py status
  ./hedge.py plan  [--size 0.005] [--direction short-dec-long-hl|long-dec-short-hl]
  ./hedge.py open  [--size 0.005] [--direction ...]            # DRY RUN by default
  ./hedge.py close                                               # DRY RUN by default
  add  --live --yes  to send REAL orders (never used by the dry run).

Secrets are read from env only (DECIBEL_API_WALLET_PRIVATE_KEY, HL_AGENT_PRIVATE_KEY, GEOMI_CLIENT_KEY,
GEOMI_GAS_STATION_KEY) and never printed/logged.

BTC size/price increments below are the certified defaults. set_market() swaps in another
overlapping perp. Addresses are overridden by bot/config.json when that file is present.

hold_hl, skip_funding_check, and ignore_pause are not CLI flags. strategy.py sets
them on the argparse.Namespace it passes into open/close/sync.
"""
import argparse, json, math, os, subprocess, sys, time, datetime
import requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from redact import sanitize
from hedge_book import ORDER_PREFIX, hedge_cloid_hex

# ------------------------------------------------------------------ config
DEC_SUB      = "0xe91f3109b1558db1c77c09fa19e1f6765c2872235446b6302b11ca363231911f"
HL_MASTER    = "0x8bDaF4d7e8A35364A14fA93461bBACb4454ff018"
DEC_PKG      = "0x50ead22afd6ffd9769e3b3d6e0e64a2a350d68e8b102c4e72e33d0b8cfdfdb06"
DEC_MARKET   = "BTC/USD"
DEC_MARKET_ADDR = "0x5e0e16f34adfb4b316f8d532d68acbfa206826feaaa418d3938046bdc2044861"
DEC_API      = "https://api.mainnet.aptoslabs.com/decibel/api/v1"
APTOS_NODE   = "https://api.mainnet.aptoslabs.com/v1"
HL_COIN      = "BTC"
SZ_DEC_DEC, PX_DEC_DEC = 8, 6            # Decibel BTC: sz_decimals 8, px_decimals 6
DEC_TICK_USD, DEC_LOT, DEC_MIN = 0.1, 1000 / 1e8, 2000 / 1e8
HL_SZ_DECIMALS = 5
HL_MIN_NOTIONAL = 10.0
MAKER_FEE = 0.00015                       # both venues, base tier
TAKER_FEE = 0.00045
MAX_SIZE_BTC = 0.01                       # hard safety cap unless --force
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BRIDGE_DIR, "hedge.log")

def _load_addresses():
    """Non-secret addresses live in config.json. Defaults match that file."""
    global DEC_SUB, HL_MASTER
    path = os.path.join(BRIDGE_DIR, "config.json")
    if not os.path.exists(path):
        return
    with open(path) as f:
        cfg = json.load(f)
    if cfg.get("decibel_subaccount"):
        DEC_SUB = cfg["decibel_subaccount"]
    if cfg.get("hl_master"):
        HL_MASTER = cfg["hl_master"]

_load_addresses()

def log(msg):
    line = f"{datetime.datetime.now().strftime('%F %T')} {sanitize(msg)}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f: f.write(line + "\n")
    except OSError:
        pass

def entries_paused():
    flag = os.environ.get("BOT_ENABLED", "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return True
    return os.path.exists(os.path.join(BRIDGE_DIR, "TRADING_PAUSED"))

def set_market(coin, dec_name, dec_addr, sz_decimals, px_decimals, tick, lot, min_size, hl_sz_decimals):
    """Point the order path at one overlapping perp. BTC defaults stay in place until this is called."""
    global DEC_MARKET, DEC_MARKET_ADDR, HL_COIN, SZ_DEC_DEC, PX_DEC_DEC
    global DEC_TICK_USD, DEC_LOT, DEC_MIN, HL_SZ_DECIMALS
    if min(tick, lot, min_size) <= 0:
        raise ValueError(f"refusing {coin}: tick/lot/min must be positive")
    if lot > 1_000_000 or min_size > 1_000_000:
        raise ValueError(f"refusing {coin}: lot/min look like raw chain units")
    DEC_MARKET = dec_name
    DEC_MARKET_ADDR = dec_addr
    HL_COIN = coin
    SZ_DEC_DEC = int(sz_decimals)
    PX_DEC_DEC = int(px_decimals)
    DEC_TICK_USD = float(tick)
    DEC_LOT = float(lot)
    DEC_MIN = float(min_size)
    HL_SZ_DECIMALS = int(hl_sz_decimals)

def _bearer():
    k = os.environ.get("GEOMI_CLIENT_KEY")
    if not k: sys.exit("GEOMI_CLIENT_KEY not set")
    return {"Authorization": f"Bearer {k}"}

# ------------------------------------------------------------------ Decibel (reads: REST + chain views)
def dget(path, **params):
    r = requests.get(f"{DEC_API}/{path}", params=params, headers=_bearer(), timeout=20)
    r.raise_for_status()
    return r.json()

def chain_view(fn, args):
    r = requests.post(f"{APTOS_NODE}/view", json={"function": f"{DEC_PKG}::perp_engine::{fn}", "type_arguments": [], "arguments": args}, timeout=20)
    r.raise_for_status()
    return r.json()

def dec_position():
    """signed BTC size on the Decibel subaccount (negative = short), straight from chain views."""
    has = chain_view("has_position", [DEC_SUB, DEC_MARKET_ADDR])[0]
    if not has: return 0.0
    sz = int(chain_view("get_position_size", [DEC_SUB, DEC_MARKET_ADDR])[0]) / 10**SZ_DEC_DEC
    is_long = chain_view("get_position_is_long", [DEC_SUB, DEC_MARKET_ADDR])[0]
    return sz if is_long else -sz

def dec_book():
    ob = dget("orderbook", market=DEC_MARKET_ADDR)
    bids = [(float(p), float(s)) for p, s in ob["bids"]]; asks = [(float(p), float(s)) for p, s in ob["asks"]]
    return max(bids)[0], min(asks)[0]

def dec_price_row():
    for x in dget("prices", market=DEC_MARKET_ADDR):
        if x["market"] == DEC_MARKET_ADDR: return x
    raise RuntimeError("no Decibel price row")

def dec_funding_hr():
    x = dec_price_row()
    return x["funding_rate_bps"] / 1e4 * (1 if x["is_funding_positive"] else -1)   # per hour, fraction

def dec_overview():
    return dget("account_overviews", account=DEC_SUB)

def dec_open_orders():
    return dget("open_orders", account=DEC_SUB).get("items", [])

def bridge(cmd, args):
    p = subprocess.run(["node", "decibel_bridge.mjs", cmd, json.dumps(args)], cwd=BRIDGE_DIR, capture_output=True, text=True, timeout=120)
    lines = [l for l in p.stdout.strip().splitlines() if l.strip().startswith("{")]
    if not lines: return {"ok": False, "error": "bridge produced no json", "stderr": sanitize(p.stderr[-300:])}
    return json.loads(lines[-1])

def ceil_tick(px, tick): return round(math.ceil(px / tick - 1e-9) * tick, 8)
def floor_tick(px, tick): return round(math.floor(px / tick + 1e-9) * tick, 8)

def dec_place(is_buy, size_btc, px_usd, reduce_only, client_id, dry, tif="post_only"):
    return bridge("place", {"dry": dry, "market": DEC_MARKET, "price": int(round(px_usd * 10**PX_DEC_DEC)),
                            "size": int(round(size_btc * 10**SZ_DEC_DEC)), "isBuy": is_buy, "tif": tif,
                            "reduceOnly": reduce_only, "clientOrderId": client_id, "subaccount": DEC_SUB})

# ------------------------------------------------------------------ Hyperliquid
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.signing import order_request_to_order_wire

_info = None
def hl_info():
    global _info
    if _info is None: _info = Info(constants.MAINNET_API_URL, skip_ws=True)
    return _info

_ex = None
def hl_exchange():
    """Exchange (signing) object. Only constructed for LIVE order/leverage actions."""
    global _ex
    if _ex is None:
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        k = os.environ["HL_AGENT_PRIVATE_KEY"].strip()
        k = k if k.startswith("0x") else "0x" + k
        _ex = Exchange(Account.from_key(k), constants.MAINNET_API_URL, account_address=HL_MASTER)
    return _ex

def hl_agent_address():
    from eth_account import Account
    k = os.environ["HL_AGENT_PRIVATE_KEY"].strip(); k = k if k.startswith("0x") else "0x" + k
    return Account.from_key(k).address

def hl_state():
    return hl_info().user_state(HL_MASTER)

def hl_spot_usdc():
    for b in hl_info().spot_user_state(HL_MASTER)["balances"]:
        if b["coin"] == "USDC": return float(b["total"]) - float(b["hold"])
    return 0.0

def hl_position():
    for ap in hl_state().get("assetPositions", []):
        if ap["position"]["coin"] == HL_COIN: return float(ap["position"]["szi"])
    return 0.0

def hl_book():
    l = hl_info().l2_snapshot(HL_COIN)["levels"]
    return float(l[0][0]["px"]), float(l[1][0]["px"])

def hl_funding():
    meta, ctxs = hl_info().meta_and_asset_ctxs()
    for u, c in zip(meta["universe"], ctxs):
        if u["name"] == HL_COIN: return float(c["funding"]), float(c["markPx"]), float(c["oraclePx"])
def hl_funding_avg24():
    h = hl_info().funding_history(HL_COIN, int((time.time() - 86400) * 1000))
    return sum(float(x["fundingRate"]) for x in h) / max(len(h), 1)

def hl_px(px, up):
    """HL perp prices: <=5 significant figures and <= (6 - szDecimals) decimals. Round up/down to a valid price."""
    dec = min(6 - HL_SZ_DECIMALS, max(0, 4 - int(math.floor(math.log10(abs(px))))))
    step = 10 ** -dec
    v = (math.ceil(px / step - 1e-9) if up else math.floor(px / step + 1e-9)) * step
    return round(v, dec)

def hl_sz(sz): return math.floor(sz * 10**HL_SZ_DECIMALS + 1e-9) / 10**HL_SZ_DECIMALS

def hl_order_wire(is_buy, sz, px, tif, reduce_only):
    asset = hl_info().name_to_asset(HL_COIN)
    return order_request_to_order_wire({"coin": HL_COIN, "is_buy": is_buy, "sz": sz, "limit_px": px, "order_type": {"limit": {"tif": tif}}, "reduce_only": reduce_only}, asset)

def hl_order(is_buy, sz, px, tif, reduce_only, client_id=None):
    cloid = None
    if client_id:
        try:
            from hyperliquid.utils.types import Cloid
            cloid = Cloid.from_str(hedge_cloid_hex(client_id))
        except Exception as exc:
            log(f"HL cloid skipped: {exc}")
    if cloid is None:
        r = hl_exchange().order(HL_COIN, is_buy, sz, px, {"limit": {"tif": tif}}, reduce_only)
    else:
        r = hl_exchange().order(HL_COIN, is_buy, sz, px, {"limit": {"tif": tif}}, reduce_only, cloid=cloid)
    st = r["response"]["data"]["statuses"][0] if r.get("status") == "ok" else {"error": str(r)[:200]}
    return st

def hl_cancel(oid): return hl_exchange().cancel(HL_COIN, oid)

def hl_open_orders():
    return [o for o in hl_info().open_orders(HL_MASTER) if o["coin"] == HL_COIN]

# ------------------------------------------------------------------ plan
def liq_prices(direction, size, entry_dec, entry_hl, dec_equity, hl_equity, mm=0.0125):
    out = {}
    if direction == "short-dec-long-hl":
        out["decibel_short_liq"] = (dec_equity + size * entry_dec) / (size * (1 + mm))
        p = (size * entry_hl - hl_equity) / (size * (1 - mm)); out["hl_long_liq"] = p if p > 0 else None
    else:
        p = (size * entry_dec - dec_equity) / (size * (1 - mm)); out["decibel_long_liq"] = p if p > 0 else None
        out["hl_short_liq"] = (hl_equity + size * entry_hl) / (size * (1 + mm))
    return out

def make_plan(size, direction):
    db, da = dec_book(); hb, ha = hl_book()
    fd = dec_funding_hr(); fh, hmark, hor = hl_funding(); fh24 = hl_funding_avg24()
    ov = dec_overview(); dec_eq = ov["perp_equity_balance"]; hl_eq = hl_spot_usdc()   # unified account: spot USDC is the perp collateral
    short_dec = direction == "short-dec-long-hl"
    entry_dec = ceil_tick(da, DEC_TICK_USD) if short_dec else floor_tick(db, DEC_TICK_USD)   # join own side of book (maker)
    entry_hl = hb if short_dec else ha                                                         # follower maker price
    notional = size * entry_dec
    # funding: short pays/receives -rate*...; positive rate => longs pay shorts
    dec_f_hr = (fd if short_dec else -fd); hl_f_hr = (-fh if short_dec else fh); hl_f_hr24 = (-fh24 if short_dec else fh24)
    net_hr_live, net_hr_24 = dec_f_hr + hl_f_hr, dec_f_hr + hl_f_hr24         # + = we earn
    fees_mm = 4 * MAKER_FEE * notional
    fees_dec_m_hl_t = 2 * MAKER_FEE * notional + MAKER_FEE * notional + TAKER_FEE * notional
    plan = dict(direction=direction, size_btc=size, notional_usd=round(notional, 2),
        decibel=dict(side="SELL" if short_dec else "BUY", px=entry_dec, best_bid=db, best_ask=da, equity=dec_eq, leverage=round(notional / dec_eq, 2),
                     funding_pct_hr=fd * 100, max_lev_market=40),
        hyperliquid=dict(side="BUY" if short_dec else "SELL", px=entry_hl, best_bid=hb, best_ask=ha, equity=round(hl_eq, 2), leverage=round(notional / hl_eq, 2),
                         funding_pct_hr=fh * 100, funding_24h_avg_pct_hr=fh24 * 100, mark=hmark, oracle=hor),
        cross_venue_basis_pct=(( (da + db) / 2) / ((ha + hb) / 2) - 1) * 100,
        fees_usd=dict(all_maker_round_trip=round(fees_mm, 4), decibel_maker_hl_taker_fallback_round_trip=round(fees_dec_m_hl_t, 4)),
        funding_usd_per_week=dict(live_rates=round(net_hr_live * notional * 168, 3), using_hl_24h_avg=round(net_hr_24 * notional * 168, 3)),
        liq=liq_prices(direction, size, entry_dec, entry_hl, dec_eq, hl_eq))
    plan["funding_usd_7d_net_of_all_maker_fees"] = dict(live=round(plan["funding_usd_per_week"]["live_rates"] - fees_mm, 3),
                                                       hl24h=round(plan["funding_usd_per_week"]["using_hl_24h_avg"] - fees_mm, 3))
    return plan

def validate(plan, force):
    errs, warns = [], []
    s = plan["size_btc"]
    if s > MAX_SIZE_BTC and not force: errs.append(f"size {s} > safety cap {MAX_SIZE_BTC} BTC (use --force)")
    if s < DEC_MIN - 1e-12: errs.append(f"size below Decibel min {DEC_MIN}")
    if abs(round(s / DEC_LOT) * DEC_LOT - s) > 1e-12: errs.append("size not a multiple of Decibel lot 0.00001")
    if abs(hl_sz(s) - s) > 1e-12: errs.append(f"size has too many HL decimals ({HL_SZ_DECIMALS})")
    if s * plan["hyperliquid"]["px"] < HL_MIN_NOTIONAL: errs.append("HL notional < $10 minimum")
    if plan["decibel"]["leverage"] > 3.0: errs.append(f"Decibel leverage {plan['decibel']['leverage']}x > 3x target")
    if plan["decibel"]["equity"] < s * plan["decibel"]["px"] / 3: errs.append("insufficient Decibel collateral for 3x")
    if plan["hyperliquid"]["equity"] < s * plan["hyperliquid"]["px"] / 3: errs.append("insufficient HL collateral for 3x")
    if abs(plan["cross_venue_basis_pct"]) > 0.15: warns.append(f"cross-venue basis {plan['cross_venue_basis_pct']:+.3f}% is large")
    if plan["funding_usd_7d_net_of_all_maker_fees"]["live"] < 0 and plan["funding_usd_7d_net_of_all_maker_fees"]["hl24h"] < 0:
        msg = "expected 7d funding does NOT cover round-trip fees at current rates"
        (warns if force else errs).append(msg + ("" if force else " (use --force to override)"))
    elif plan["funding_usd_7d_net_of_all_maker_fees"]["live"] < 0 or plan["funding_usd_7d_net_of_all_maker_fees"]["hl24h"] < 0:
        warns.append("funding vs fees is net-negative under one of the two HL funding estimates")
    return errs, warns

# ------------------------------------------------------------------ status
def cmd_status(a):
    ov = dec_overview(); dp = dec_position(); st = hl_state(); hp = hl_position()
    if os.environ.get("HEDGE_REDACT") == "1" and not getattr(a, "show_balances", False):
        net = dp + hp
        print(f"status coin={HL_COIN} dec={dp:+.5f} hl={hp:+.5f} net={net:+.5f} redacted=1")
        return
    print("== DECIBEL subaccount", DEC_SUB[:10] + "…")
    for k in ["perp_equity_balance", "total_margin", "usdc_cross_withdrawable_balance", "cross_available_to_trade", "maintenance_margin",
              "cross_margin_ratio", "unrealized_pnl", "unrealized_funding_cost", "margin_deficit"]:
        print(f"  {k:34s} {ov.get(k)}")
    print(f"  BTC position (signed)              {dp}")
    for p in dget("account_positions", account=DEC_SUB):
        if not p.get("is_deleted"):
            print(f"  REST pos: size={p['size']} entry={p['entry_price']} est_liq={p['estimated_liquidation_price']} unrealized_funding={p['unrealized_funding']} lev={p['user_leverage']}")
    print("  open orders:", [(o['order_id'][:8], o['order_direction'], o['price'], o['remaining_size'], o['status']) for o in dec_open_orders()])
    print("== HYPERLIQUID master", HL_MASTER[:10] + "…")
    print(f"  perp accountValue {st['marginSummary']['accountValue']}  withdrawable {st['withdrawable']}  spot USDC {hl_spot_usdc():.2f}")
    for ap in st.get("assetPositions", []):
        p = ap["position"]
        print(f"  {p['coin']} szi={p['szi']} entry={p['entryPx']} liq={p['liquidationPx']} uPnL={p['unrealizedPnl']} marginUsed={p['marginUsed']} cumFunding(sinceOpen)={p['cumFunding']['sinceOpen']} (allTime)={p['cumFunding']['allTime']}")
    print("  open orders:", [(o['oid'], o['side'], o['limitPx'], o['sz']) for o in hl_open_orders()])
    net = dp + hp
    print(f"== NET BTC delta (dec+hl) = {net:+.5f} BTC (~${net * hl_book()[0]:+.2f})", "" if abs(net) < 1e-4 else "  <-- UNHEDGED")
    liq = [p for p in dget("account_positions", account=DEC_SUB) if not p.get("is_deleted")]
    if liq:
        mark = dec_price_row()["mark_px"]; el = liq[0]["estimated_liquidation_price"]
        if el: print(f"   Decibel liq distance: {abs(el - mark) / mark * 100:.1f}%", "(WARN <20%: add margin)" if abs(el - mark) / mark < 0.20 else "")

def cmd_plan(a):
    plan = make_plan(a.size, a.direction); errs, warns = validate(plan, a.force)
    print(json.dumps(plan, indent=2)); print("ERRORS:", errs); print("WARNINGS:", warns)
    return plan, errs

# ------------------------------------------------------------------ engine
def _hl_target(mode, ap, a0, b0):
    """HL size that keeps the book delta-neutral, or walks both legs to flat on close.

    Open/sync: target is the opposite of the Decibel position already filled (-ap), so HL
    never runs ahead of the Decibel fill. Close of a hedge (a0 + b0 ~= 0) is the same
    number. Close when both legs have the same sign walks HL toward 0 in step with Decibel
    instead of flipping it.
    """
    if mode != "close":
        return -ap
    if abs(a0) < 1e-12:
        return 0.0
    remain = ap / a0
    if remain < 0:
        remain = 0.0
    if remain > 1:
        remain = 1.0
    return b0 * remain

def run_hedge(mode, a, direction):
    """mode 'open' | 'close' | 'sync'. Decibel leg leads (post-only); HL follows the filled size.

    sync moves only the Hyperliquid leg to -dec_position (repair a missing HL hedge).
    """
    live = a.live and a.yes
    dry = not live
    a0, b0 = dec_position(), hl_position()
    hold_hl = bool(getattr(a, "hold_hl", False))
    if mode == "open":
        size = a.size; short_dec = direction == "short-dec-long-hl"
        if (abs(a0) > 1e-9 or abs(b0) > 1e-9) and not a.allow_existing: sys.exit(f"existing positions (dec {a0}, hl {b0}); use --allow-existing")
        dec_is_buy = not short_dec; dec_reduce = False
    elif mode == "sync":
        size = 0.0
        dec_is_buy = False
        dec_reduce = True
        short_dec = a0 < 0
    else:
        if abs(a0) < 1e-9 and abs(b0) < 1e-9: sys.exit("nothing to close")
        size = abs(a0)                       # Decibel leg leads; HL mirrors to flat-hedge
        dec_is_buy = a0 < 0; dec_reduce = True
        if size < DEC_LOT / 2:
            size = 0.0                       # Decibel already flat; close the HL leg only
    if size > 0:
        size = round(math.floor(size / DEC_LOT + 1e-9) * DEC_LOT, 8)
    hl_is_buy = (not dec_is_buy) if size > 0 else (b0 < 0)
    hl_reduce = (mode == "close")
    db, da = dec_book(); hb, ha = hl_book()
    dpx = floor_tick(db, DEC_TICK_USD) if dec_is_buy else ceil_tick(da, DEC_TICK_USD)
    cid = f"{ORDER_PREFIX}{mode}-{int(time.time())}"
    log(f"{mode.upper()} {'(LIVE)' if live else '(DRY-RUN)'}: Decibel {'BUY' if dec_is_buy else 'SELL'} {size} {HL_COIN} post-only @ {dpx} ro={dec_reduce} cid={cid}; "
        f"then HL {'BUY' if hl_is_buy else 'SELL'} follows (ALO {hb if hl_is_buy else ha}; taker fallback after {a.hedge_timeout}s, cap {a.max_slip_bps} bps)")

    wire_sz = size
    if mode == "sync":
        wire_sz = abs(-a0 - b0)
    elif mode == "close" and size == 0:
        wire_sz = abs(b0)
    if dry:
        if size > 0:
            res = dec_place(dec_is_buy, size, dpx, dec_reduce, cid, True)
            log(f"[dry] Decibel simulate -> ok={res.get('ok')} sims={json.dumps(res.get('sims'))[:600]} err={res.get('error')}")
            if a.set_leverage and mode == "open":
                lv = bridge("leverage", {"dry": True, "marketAddr": DEC_MARKET_ADDR, "subaccount": DEC_SUB, "leverage": a.leverage})
                log(f"[dry] Decibel leverage->{a.leverage}x simulate -> ok={lv.get('ok')} sims={json.dumps(lv.get('sims'))[:400]} err={lv.get('error')}")
        else:
            log("[dry] no Decibel order (leg already at its target)")
        # HL: build the wires we WOULD send (no signing, no network write)
        maker_px = hl_px(hb if hl_is_buy else ha, up=not hl_is_buy)
        slip = a.max_slip_bps / 1e4
        taker_px = hl_px((ha * (1 + slip)) if hl_is_buy else (hb * (1 - slip)), up=hl_is_buy)
        hsz = hl_sz(wire_sz)
        for name, px, tif in [("ALO maker", maker_px, "Alo"), ("IOC taker fallback", taker_px, "Ioc")]:
            w = hl_order_wire(hl_is_buy, hsz, px, tif, hl_reduce)
            ok_cross = (px < ha) if (tif == "Alo" and hl_is_buy) else (px > hb) if (tif == "Alo") else True
            log(f"[dry] HL {name}: wire={json.dumps(w)} notional=${hsz * px:.2f} (min $10: {'ok' if hsz * px >= 10 else 'FAIL'}) non-crossing={ok_cross}")
        aad = hl_info().post("/info", {"type": "activeAssetData", "user": HL_MASTER, "coin": HL_COIN})
        if os.environ.get("HEDGE_REDACT") == "1":
            log(f"[dry] HL activeAssetData: leverage={aad.get('leverage')}")
        else:
            log(f"[dry] HL activeAssetData: leverage={aad['leverage']} maxTradeSzs={aad['maxTradeSzs']} availableToTrade={aad['availableToTrade']}")
        if a.set_leverage and mode == "open": log(f"[dry] HL would set leverage {a.leverage}x cross (signed update_leverage)")
        log("[dry] no order sent, nothing signed.")
        return

    # ---------------- LIVE
    if a.set_leverage and mode == "open":
        log(f"set Decibel leverage {a.leverage}x: {bridge('leverage', {'dry': False, 'marketAddr': DEC_MARKET_ADDR, 'subaccount': DEC_SUB, 'leverage': a.leverage})}")
        log(f"set HL leverage {a.leverage}x: {hl_exchange().update_leverage(a.leverage, HL_COIN, True)}")
    t0 = time.time(); hl_ts = None; dec_cancelled = False
    if size > 0:
        r = dec_place(dec_is_buy, size, dpx, dec_reduce, cid, False)
        log(f"Decibel order submitted: ok={r.get('ok')} err={r.get('error')}")
        if not r.get("ok"): sys.exit("Decibel placement failed; nothing to hedge")
    else:
        dec_cancelled = True
        t_cancel = time.time()
        log("no Decibel order; Hyperliquid follows the current Decibel size")
    while True:
        time.sleep(a.poll)
        ap, bp = dec_position(), hl_position()
        resting = hl_open_orders()
        pending = sum((1 if o["side"] == "B" else -1) * float(o["sz"]) for o in resting)
        dec_open = [o for o in dec_open_orders() if o.get("client_order_id") == cid] if size > 0 else []
        if hold_hl and dec_open and abs(ap) + 1e-12 < abs(b0):
            unhedged = 0.0
            target = bp + pending
        else:
            target = _hl_target(mode, ap, a0, b0)
            unhedged = target - bp - pending
        log(f"poll: dec={ap:+.5f} hl={bp:+.5f} hl_resting={pending:+.5f} unhedged={unhedged:+.5f} dec_order_open={bool(dec_open)}")
        if abs(unhedged) * hb >= HL_MIN_NOTIONAL * 1.1 and not resting:
            hb, ha = hl_book(); is_buy = unhedged > 0
            px = hl_px(hb if is_buy else ha, up=not is_buy); sz = hl_sz(abs(unhedged))
            reduce_only = hl_reduce or (is_buy and bp < -1e-12) or ((not is_buy) and bp > 1e-12)
            st = hl_order(is_buy, sz, px, "Alo", reduce_only, cid); hl_ts = time.time(); log(f"HL ALO {'BUY' if is_buy else 'SELL'} {sz} @ {px}: {st}")
        elif resting and hl_ts and time.time() - hl_ts > a.hedge_timeout:
            for o in resting: hl_cancel(o["oid"])
            hb, ha = hl_book(); gap = target - bp; is_buy = gap > 0; slip = a.max_slip_bps / 1e4
            rem = hl_sz(abs(gap)); px = hl_px((ha * (1 + slip)) if is_buy else (hb * (1 - slip)), up=is_buy)
            reduce_only = hl_reduce or (is_buy and bp < -1e-12) or ((not is_buy) and bp > 1e-12)
            st = hl_order(is_buy, rem, px, "Ioc", reduce_only, cid); hl_ts = None
            log(f"HL TAKER fallback {'BUY' if is_buy else 'SELL'} {rem} @ cap {px}: {st}")
        if size > 0 and not dec_cancelled and (time.time() - t0 > a.maker_timeout):
            log("maker timeout: cancelling Decibel order"); log(str(bridge("cancel_client", {"dry": False, "market": DEC_MARKET, "clientOrderId": cid, "subaccount": DEC_SUB})))
            dec_cancelled = True; t_cancel = time.time()
        if dec_cancelled and not dec_open and abs(unhedged) * hb < HL_MIN_NOTIONAL * 1.1 and not resting: break
        if dec_cancelled and time.time() - t_cancel > 90: log("giving up waiting; check positions"); break
        if time.time() - t0 > a.maker_timeout + 180: break
    log(f"DONE. final dec={dec_position():+.5f} hl={hl_position():+.5f}")

def _funding_block(err):
    return "funding" in err.lower()

def cmd_open(a):
    if entries_paused() and a.live and a.yes and not getattr(a, "ignore_pause", False):
        log("BLOCKED: kill switch (BOT_ENABLED=false or bot/TRADING_PAUSED); no live entry")
        sys.exit(2)
    plan = make_plan(a.size, a.direction); errs, warns = validate(plan, a.force)
    if getattr(a, "skip_funding_check", False):
        errs = [e for e in errs if not _funding_block(e)]
    log("PLAN " + json.dumps({k: plan[k] for k in ("direction", "size_btc", "notional_usd", "fees_usd", "funding_usd_per_week", "liq")}))
    for w in warns: log("WARN " + w)
    if errs:
        for e in errs: log("BLOCKED: " + e)
        if a.live and a.yes: sys.exit(1)
    if a.live and not a.yes: sys.exit("--live requires --yes")
    run_hedge("open", a, a.direction)

def cmd_close(a): run_hedge("close", a, None)

def resolve_cli_market(coin):
    """Keep certified BTC increments. Any other coin loads human specs from the Decibel markets API."""
    base = coin.upper().split("/")[0].split("-")[0].strip()
    if base == "BTC":
        return "BTC"
    rows = dget("markets")
    match = None
    for m in rows:
        if m.get("asset_type") == "spot":
            continue
        name = str(m.get("market_name", ""))
        if name.split("/")[0].split("-")[0].upper() == base:
            match = m
            break
    if not match:
        sys.exit(f"no Decibel perp named {base}")
    hl_name, hl_sz = None, None
    for u in hl_info().meta()["universe"]:
        if u["name"].upper() == base:
            hl_name, hl_sz = u["name"], int(u["szDecimals"])
            break
    if hl_name is None:
        sys.exit(f"no Hyperliquid perp named {base}")
    set_market(hl_name, match["market_name"], match["market_addr"], int(match["sz_decimals"]), int(match["px_decimals"]),
               float(match["tick_size"]), float(match["lot_size"]), float(match["min_size"]), hl_sz)
    return hl_name

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["status", "plan", "open", "close"])
    ap.add_argument("--coin", default="BTC", help="perp base symbol, e.g. BTC or ETH")
    ap.add_argument("--size", type=float, default=0.005)
    ap.add_argument("--direction", choices=["short-dec-long-hl", "long-dec-short-hl"], default="short-dec-long-hl")
    ap.add_argument("--live", action="store_true", help="send real orders (default: dry run)")
    ap.add_argument("--yes", action="store_true", help="required together with --live")
    ap.add_argument("--dry-run", action="store_true", help="(default) simulate only")
    ap.add_argument("--maker-timeout", type=int, default=120, help="seconds the Decibel post-only order may rest")
    ap.add_argument("--hedge-timeout", type=int, default=20, help="seconds an HL maker hedge may rest before IOC taker fallback")
    ap.add_argument("--max-slip-bps", type=float, default=30)
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--set-leverage", action="store_true"); ap.add_argument("--leverage", type=int, default=3)
    ap.add_argument("--force", action="store_true"); ap.add_argument("--allow-existing", action="store_true")
    a = ap.parse_args()
    if a.dry_run: a.live = False
    dry_flag = int(not (a.live and a.yes))
    try:
        a.coin = resolve_cli_market(a.coin)
        {"status": cmd_status, "plan": cmd_plan, "open": cmd_open, "close": cmd_close}[a.cmd](a)
    except SystemExit as e:
        if e.code not in (0, None):
            print(f"HEDGE_RESULT cmd={a.cmd} coin={a.coin} dry={dry_flag} ok=0")
            raise
    except Exception:
        import traceback
        print(sanitize(traceback.format_exc())[-2000:])
        print(f"HEDGE_RESULT cmd={a.cmd} coin={a.coin} dry={dry_flag} ok=0")
        sys.exit(1)
    print(f"HEDGE_RESULT cmd={a.cmd} coin={a.coin} dry={dry_flag} ok=1")

if __name__ == "__main__":
    main()
