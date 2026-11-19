// Decibel write bridge (official @decibeltrade/sdk). Called by hedge.py as:
//   node decibel_bridge.mjs <cmd> '<json args>'
// Prints ONE json line on stdout (last line). Secrets are read from env, never printed.
// cmds: whoami | place | cancel_client | leverage      (args.dry=true => simulate only, NEVER submits)
import { DecibelWriteDex, MAINNET_CONFIG, TimeInForce, getMarketAddr } from "@decibeltrade/sdk";
import { Ed25519Account, Ed25519PrivateKey, PrivateKey, PrivateKeyVariants } from "@aptos-labs/ts-sdk";

const [cmd, argJson] = process.argv.slice(2);
const args = JSON.parse(argJson || "{}");
const out = (o) => { process.stdout.write("\n" + JSON.stringify(o) + "\n"); };

function mkAccount() {
  const raw = (process.env.DECIBEL_API_WALLET_PRIVATE_KEY || "").trim();
  if (!raw) throw new Error("DECIBEL_API_WALLET_PRIVATE_KEY not set");
  const fmt = PrivateKey.formatPrivateKey(raw, PrivateKeyVariants.Ed25519);
  return new Ed25519Account({ privateKey: new Ed25519PrivateKey(fmt) });
}

const account = mkAccount();
const config = {
  ...MAINNET_CONFIG,
  gasStationApiKey: process.env.GEOMI_GAS_STATION_KEY || undefined,
};
const write = new DecibelWriteDex(config, account, {
  nodeApiKey: process.env.GEOMI_NODE_KEY || process.env.GEOMI_CLIENT_KEY,
  skipSimulate: false,
});

// DRY RUN: replace the final sign+submit step with a simulation. Nothing is signed or sent.
const sims = [];
if (args.dry) {
  write.signAndSubmit = async (_signer, transaction, meta) => {
    const res = await write.aptos.transaction.simulate.simple({ transaction });
    const r = res?.[0];
    sims.push({
      fn: meta?.functionId,
      success: r?.success,
      vm_status: r?.vm_status,
      gas_used: r?.gas_used,
      max_gas_amount: r?.max_gas_amount,
      gas_unit_price: r?.gas_unit_price,
      fee_payer: transaction.feePayerAddress?.toString?.(),
      events: (r?.events || []).map((e) => e.type.split("::").slice(-2).join("::")).slice(0, 8),
    });
    return { hash: "DRY_RUN_NOT_SUBMITTED", success: !!r?.success, events: r?.events || [], vm_status: r?.vm_status };
  };
}

function redact(s) {
  let t = String(s ?? "");
  for (const k of [
    "DECIBEL_API_WALLET_PRIVATE_KEY",
    "GEOMI_GAS_STATION_KEY",
    "GEOMI_NODE_KEY",
    "GEOMI_CLIENT_KEY",
    "HL_AGENT_PRIVATE_KEY",
    "GITHUB_TOKEN",
  ]) {
    const v = process.env[k];
    if (v && v.length > 4) t = t.split(v).join("***");
  }
  if (process.env.HEDGE_REDACT === "1") t = t.replace(/0x[0-9a-fA-F]{6,}/g, "0x…");
  return t.slice(0, 600);
}

const origErr = console.error;
console.error = (...a) => origErr(...a.map((x) => (typeof x === "string" ? redact(x).slice(0, 400) : x?.message ? redact(x.message) : "[obj]")));

try {
  if (cmd === "whoami") {
    out({
      ok: true,
      api_wallet: process.env.HEDGE_REDACT === "1" ? "redacted" : account.accountAddress.toString(),
      gas_station: !!config.gasStationApiKey,
      market_addr_BTC: getMarketAddr("BTC/USD", config.deployment.perpEngineGlobal).toString(),
    });
  } else if (cmd === "place") {
    const r = await write.placeOrder({
      marketName: args.market,
      price: args.price,            // chain units (px * 10^px_decimals), multiple of tick
      size: args.size,              // chain units (sz * 10^sz_decimals), multiple of lot
      isBuy: args.isBuy,
      timeInForce: args.tif === "ioc" ? TimeInForce.ImmediateOrCancel : args.tif === "gtc" ? TimeInForce.GoodTillCanceled : TimeInForce.PostOnly,
      isReduceOnly: !!args.reduceOnly,
      clientOrderId: args.clientOrderId,
      subaccountAddr: args.subaccount,
    });
    out({ ok: r.success, result: r, sims, dry: !!args.dry });
  } else if (cmd === "cancel_client") {
    if (args.dry) { out({ ok: true, dry: true, note: "cancel not simulated" }); }
    else {
      const r = await write.cancelClientOrder({ clientOrderId: args.clientOrderId, marketName: args.market, subaccountAddr: args.subaccount });
      out({ ok: !!r?.success, hash: r?.hash, vm_status: r?.vm_status });
    }
  } else if (cmd === "leverage") {
    const r = await write.configureUserSettingsForMarket({
      marketAddr: args.marketAddr, subaccountAddr: args.subaccount, isCross: true, userLeverage: args.leverage,
    });
    out({ ok: r?.success ?? false, hash: r?.hash, sims, dry: !!args.dry });
  } else {
    out({ ok: false, error: "unknown cmd" });
  }
} catch (e) {
  out({ ok: false, error: redact(e?.message || e), sims });
}
process.exit(0);
