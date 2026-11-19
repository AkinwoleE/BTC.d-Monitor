"""Which positions and orders belong to the funding hedge.

The RSI divergence bot, the BTC.D pairs bot, and Turtle share the Decibel
subaccount and the Hyperliquid master with this hedge. They must not alert
on, adopt, or close a hedge position they can positively attribute. A bot
that already has its own open position is left alone.

Positive attribution is one of:
- a row in hedge-data state.json (not the static fallback_book)
- an order or fill whose client id starts with `hedge-`
- the opposite leg on the other venue, same coin, opposite sign, matching size

fallback_book in config.json is only a hint. A size match against it alone
does not hide the position; the other bot keeps its original alert.
"""
import json
import os
import urllib.request

ORDER_PREFIX = "hedge-"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "runtime", "state.json")
CONFIG_PATH = os.path.join(HERE, "config.json")
HL_INFO = "https://api.hyperliquid.xyz/info"
APTOS_NODE = "https://fullnode.mainnet.aptoslabs.com/v1"
DEC_PKG = "0x50ead22afd6ffd9769e3b3d6e0e64a2a350d68e8b102c4e72e33d0b8cfdfdb06"
BTC_MARKET = "0x5e0e16f34adfb4b316f8d532d68acbfa206826feaaa418d3938046bdc2044861"
_SIZE_CACHE = {}


def order_client_id(order):
    if not isinstance(order, dict):
        return ""
    for key in ("client_order_id", "clientOrderId", "cloid"):
        val = order.get(key)
        if val:
            return str(val)
    return ""


def order_is_hedge(order):
    return order_client_id(order).startswith(ORDER_PREFIX)


def order_coin(order):
    if not isinstance(order, dict):
        return None
    for key in ("coin", "market_name", "symbol", "market"):
        val = order.get(key)
        if not val or str(val).startswith("0x"):
            continue
        return str(val).upper().split("/")[0].split("-")[0]
    return None


def _rows_from_state(data, account_dec, account_hl):
    rows = []
    for coin, pos in (data.get("positions") or {}).items():
        if not isinstance(pos, dict):
            continue
        name = str(coin).upper()
        dec = pos.get("dec_sz")
        hl = pos.get("hl_sz")
        if isinstance(dec, (int, float)) and dec != 0:
            rows.append({"venue": "decibel", "coin": name, "size": float(dec), "account": account_dec})
        if isinstance(hl, (int, float)) and hl != 0:
            rows.append({"venue": "hyperliquid", "coin": name, "size": float(hl), "account": account_hl})
    return rows


def load_book(state_path=None, config_path=None):
    """Return (rows, source). A present state file wins, even when it is empty."""
    state_path = state_path or STATE_PATH
    config_path = config_path or CONFIG_PATH
    cfg = {}
    if config_path and os.path.exists(config_path):
        with open(config_path) as handle:
            cfg = json.load(handle)
    account_dec = str(cfg.get("decibel_subaccount") or "").lower()
    account_hl = str(cfg.get("hl_master") or "").lower()
    if state_path and os.path.exists(state_path):
        with open(state_path) as handle:
            data = json.load(handle)
        return _rows_from_state(data, account_dec, account_hl), "state"
    rows = []
    for row in cfg.get("fallback_book") or []:
        venue = str(row.get("venue") or "").lower()
        account = row.get("account") or (account_dec if venue == "decibel" else account_hl)
        rows.append({
            "venue": venue,
            "coin": str(row.get("coin") or "").upper(),
            "size": float(row["size"]),
            "account": str(account or "").lower(),
        })
    return rows, "config"


def _size_match(size, tracked):
    tol = max(1e-5, abs(float(tracked)) * 0.02)
    return abs(float(size) - float(tracked)) <= tol


def _book_size_match(venue, coin, size, book, account):
    if size is None:
        return False
    coin_u = str(coin or "").upper()
    venue_l = str(venue or "").lower()
    account_l = str(account or "").lower()
    for row in book or []:
        if row.get("venue") != venue_l or row.get("coin") != coin_u:
            continue
        row_account = str(row.get("account") or "").lower()
        if row_account and account_l and row_account != account_l:
            continue
        try:
            if _size_match(size, row["size"]):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _tagged(records, coin):
    coin_u = str(coin or "").upper()
    for record in records or []:
        if not order_is_hedge(record):
            continue
        named = order_coin(record)
        if named is not None and named != coin_u:
            continue
        return True
    return False


def opposite_matches(size, opposite_size):
    """True when the other venue holds the offsetting leg of about the same size."""
    if size is None or opposite_size is None:
        return False
    try:
        leg = float(size)
        other = float(opposite_size)
    except (TypeError, ValueError):
        return False
    if leg == 0 or other == 0 or leg * other >= 0:
        return False
    return _size_match(abs(leg), abs(other))


def ignore_for_other_bot(own_open, venue, coin, size, book, orders=None, account=None,
                         book_source="config", opposite_size=None, fills=None):
    """True only when this position is positively the hedge and the caller did not open it.

    book_source "config" is the static fallback. Those rows never hide a position
    by themselves. Pass book_source "state" for hedge-data state.json.
    """
    if own_open:
        return False
    if _tagged(list(orders or []) + list(fills or []), coin):
        return True
    if opposite_matches(size, opposite_size):
        return True
    if book_source == "state" and _book_size_match(venue, coin, size, book, account):
        return True
    return False


def _config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH) as handle:
        return json.load(handle)


def hl_signed_size(coin, master=None, timeout=10):
    """Signed Hyperliquid position, or None if the read fails. 0 when flat."""
    coin_u = str(coin or "").upper()
    if not master:
        master = _config().get("hl_master") or ""
    key = ("hl", master.lower(), coin_u)
    if key in _SIZE_CACHE:
        return _SIZE_CACHE[key]
    size = None
    try:
        payload = json.dumps({"type": "clearinghouseState", "user": master}).encode()
        req = urllib.request.Request(HL_INFO, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            state = json.loads(resp.read().decode())
        size = 0.0
        for ap in state.get("assetPositions") or []:
            pos = (ap or {}).get("position") or {}
            if str(pos.get("coin") or "").upper() == coin_u:
                size = float(pos.get("szi") or 0)
                break
    except Exception:
        size = None
    _SIZE_CACHE[key] = size
    return size


def dec_signed_size(coin, subaccount=None, timeout=10):
    """Signed Decibel BTC position from a public chain view, or None on failure.

    Only BTC has a known market address here. Other coins return None.
    """
    if str(coin or "").upper() != "BTC":
        return None
    if not subaccount:
        subaccount = _config().get("decibel_subaccount") or ""
    if not subaccount:
        return None
    key = ("dec", subaccount.lower(), "BTC")
    if key in _SIZE_CACHE:
        return _SIZE_CACHE[key]
    size = None
    try:
        def view(fn):
            payload = json.dumps({
                "function": f"{DEC_PKG}::perp_engine::{fn}",
                "type_arguments": [],
                "arguments": [subaccount, BTC_MARKET],
            }).encode()
            req = urllib.request.Request(
                f"{APTOS_NODE}/view", data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        if not view("has_position")[0]:
            size = 0.0
        else:
            raw = int(view("get_position_size")[0]) / 1e8
            size = raw if view("get_position_is_long")[0] else -raw
    except Exception:
        size = None
    _SIZE_CACHE[key] = size
    return size
