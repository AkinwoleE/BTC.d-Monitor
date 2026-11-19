"""Which positions and orders belong to the funding hedge.

The RSI divergence bot, the BTC.D pairs bot, and Turtle share the Decibel
subaccount and the Hyperliquid master with this hedge. They must not alert
on, adopt, or close a hedge position. A bot that already has its own open
position is left alone: this module never hides a trade that bot recorded.

Filled positions are matched to the hedge book (hedge-data state.json when
that file is present, otherwise the fallback book in config.json). Resting
Decibel orders use the client id prefix `hedge-`.
"""
import json
import os

ORDER_PREFIX = "hedge-"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "runtime", "state.json")
CONFIG_PATH = os.path.join(HERE, "config.json")


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


def position_is_hedge(venue, coin, size, book, orders=None, account=None):
    """True when this live leg is the hedge's, from an order tag or the book."""
    coin_u = str(coin or "").upper()
    venue_l = str(venue or "").lower()
    account_l = str(account or "").lower()
    for order in orders or []:
        if not order_is_hedge(order):
            continue
        named = order_coin(order)
        if named is not None and named != coin_u:
            continue
        return True
    if size is None:
        return False
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


def ignore_for_other_bot(own_open, venue, coin, size, book, orders=None, account=None):
    """Other bots call this before alerting or managing a live position.

    own_open means that bot's state already records a position. Those trades
    stay on the existing path.
    """
    if own_open:
        return False
    return position_is_hedge(venue, coin, size, book, orders, account)
