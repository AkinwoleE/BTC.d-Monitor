"""Strip secrets and, when HEDGE_REDACT=1, long hex addresses from text that might be logged."""
import os
import re

SECRET_ENVS = (
    "GEOMI_CLIENT_KEY",
    "GEOMI_NODE_KEY",
    "GEOMI_GAS_STATION_KEY",
    "DECIBEL_API_WALLET_PRIVATE_KEY",
    "HL_AGENT_PRIVATE_KEY",
    "DECIBEL_PRIVATE_KEY",
    "DECIBEL_NODE_API_KEY",
    "DECIBEL_GAS_STATION_API_KEY",
    "HYPERLIQUID_API_PRIVATE_KEY",
    "DECIBEL_SUBACCOUNT",
    "HYPERLIQUID_ACCOUNT_ADDRESS",
    "GITHUB_TOKEN",
)


def sanitize(msg):
    text = str(msg)
    for name in SECRET_ENVS:
        val = os.environ.get(name) or ""
        if len(val) >= 6 and val in text:
            text = text.replace(val, "***")
    if os.environ.get("HEDGE_REDACT") == "1":
        text = re.sub(r"0x[0-9a-fA-F]{6,}", "0x…", text)
    return text
