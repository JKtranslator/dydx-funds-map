"""Hourly refresh for the dYdX Treasury SubDAO funds map.

Reads balances (read-only) from dYdX Chain, Osmosis, Ethereum and Arbitrum, prices from CoinGecko
(DefiLlama as fallback), and writes two files:

    data/funds.json   aggregated, rounded figures only
    index.html        the page, rendered from data/funds.json and scripts/template.html

The treasury addresses are NOT in this repository. They come from the FUNDS_MAP_ADDRESSES environment
variable (a GitHub Actions secret in CI) or, when run locally, from ~/.kpk/dydx_funds_map_addresses.json.
Nothing this script writes or prints contains an address: per-wallet detail stays in memory, and every
error message is redacted before it is printed.

    python scripts/refresh.py            # read the chains, gate, write data/funds.json and index.html
    python scripts/refresh.py --build    # re-render index.html from data/funds.json only (no network)

Exit codes: 0 written; 1 a read failed (nothing written); 2 the gate held a suspicious read back.
Stdlib only.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "funds.json"
LOCAL_ADDR_FILE = Path.home() / ".kpk" / "dydx_funds_map_addresses.json"

DYDX_REST = ["https://dydx-rest.publicnode.com", "https://dydx-api.lavenderfive.com",
             "https://dydx.api.kjnodes.com", "https://api.dydx.nodestake.org"]
OSMO_REST = ["https://osmosis-rest.publicnode.com", "https://lcd.osmosis.zone"]
INDEXER = "https://indexer.dydx.trade/v4"
RPC = {"ethereum": ["https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com", "https://eth.drpc.org"],
       "arbitrum": ["https://arbitrum-one-rpc.publicnode.com", "https://arb1.arbitrum.io/rpc",
                    "https://arbitrum.drpc.org"]}
BINANCE_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]   # the mirror works from US CI

USDC_IBC = "ibc/8E27BA2D5493AF5636760E354E46004562C46AB7EC0CC4C1CA14E9E20E2545B5"   # USDC on dYdX Chain
# Public token contracts (not treasury addresses): symbol -> (chain, contract, decimals, kind)
EVM_TOKENS = {
    "USDY": ("ethereum", "0x96f6ef951840721adbf46ac996b59e0235cb985c", 18, "erc20"),
    "MORPHO_KPK_USDC": ("ethereum", "0x1a1985f50352b58090eb36425afdfacbac7806f4", 18, "erc4626"),
    "USDC_ETH": ("ethereum", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6, "erc20"),
    "USDT_ETH": ("ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7", 6, "erc20"),
    "USDC_ARB": ("arbitrum", "0xaf88d065e77c8cc2239327c5edb3a432268e5831", 6, "erc20"),
    "USDT_ARB": ("arbitrum", "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", 6, "erc20"),
}
CG_IDS = {"DYDX": "dydx-chain", "USDY": "ondo-us-dollar-yield", "USDC": "usd-coin", "USDT": "tether",
          "ETH": "ethereum", "OSMO": "osmosis"}
UA = {"User-Agent": "kpk-dydx-funds-map/1.0", "Accept": "application/json"}

# Gate: a position that moves by more than this since the last published read is read a second time,
# and published only if both reads agree. Catches a lagging node that answers with empty balances.
GATE_ABS, GATE_REL, AGREE = 250_000, 0.25, 0.01

_SECRETS: list[str] = []
_ADDR_RE = re.compile(r"\b(?:dydx1|osmo1)[0-9a-z]{20,}\b|\b0x[0-9a-fA-F]{40}\b")


def redact(text: str) -> str:
    for s in _SECRETS:
        text = text.replace(s, "<address>").replace(s.lower(), "<address>")
    return _ADDR_RE.sub("<address>", text)


class ReadError(RuntimeError):
    pass


# ---- addresses ---------------------------------------------------------------------------------
def load_addresses() -> dict:
    raw = os.environ.get("FUNDS_MAP_ADDRESSES")
    if not raw and LOCAL_ADDR_FILE.exists():
        raw = LOCAL_ADDR_FILE.read_text()
    if not raw:
        raise ReadError("no addresses: set FUNDS_MAP_ADDRESSES or create ~/.kpk/dydx_funds_map_addresses.json")
    a = json.loads(raw)
    for group in a.values():
        _SECRETS.extend(group.values())
    return a


# ---- transport ---------------------------------------------------------------------------------
def get(url: str, headers: dict | None = None, timeout: int = 25):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def rest(pool: list[str], path: str, what: str):
    """GET from the first node that answers. `what` names the call in errors; the path holds addresses."""
    err = None
    for attempt in range(3):
        for base in pool:
            try:
                return get(base + path)
            except Exception as e:
                err = e
        time.sleep(3 * (attempt + 1))
    raise ReadError(f"all nodes failed for {what}: {type(err).__name__}")


def rpc(chain: str, method: str, params: list, what: str):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    err = None
    for attempt in range(2):
        for url in RPC[chain]:
            try:
                req = urllib.request.Request(url, data=body, headers={**UA, "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    out = json.loads(r.read())
                if "result" in out:
                    return out["result"]
                err = "rpc error"
            except Exception as e:
                err = type(e).__name__
        time.sleep(3)
    raise ReadError(f"{chain} rpc failed for {what}: {err}")


def call(chain: str, to: str, data: str, what: str) -> int:
    res = rpc(chain, "eth_call", [{"to": to, "data": data}, "latest"], what)
    return int(res, 16) if res and res != "0x" else 0


def pad(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


# ---- reads -------------------------------------------------------------------------------------
def prices() -> dict:
    ids = ",".join(CG_IDS.values())
    try:
        p = get(f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&precision=6")
        out = {sym: float(p[cg]["usd"]) for sym, cg in CG_IDS.items()}
        src = "CoinGecko"
    except Exception:
        keys = ",".join(f"coingecko:{v}" for v in CG_IDS.values())
        p = get(f"https://coins.llama.fi/prices/current/{keys}")["coins"]
        out = {sym: float(p[f"coingecko:{cg}"]["price"]) for sym, cg in CG_IDS.items()}
        src = "DefiLlama"
    bn = None
    for host in BINANCE_HOSTS:
        try:
            bn = float(get(f"{host}/api/v3/ticker/price?symbol=DYDXUSDT")["price"])
            break
        except Exception:
            continue
    return {"usd": out, "source": src, "binance_dydxusdt": bn}


def read_dydx(addrs: dict) -> dict:
    vals = rest(DYDX_REST, "/cosmos/staking/v1beta1/validators?pagination.limit=300", "validators")["validators"]
    moniker = {v["operator_address"]: v["description"]["moniker"].strip() for v in vals}
    status = {v["operator_address"]: v["status"] for v in vals}
    out = {"usdc": 0.0, "dydx_liquid": 0.0, "delegations": {}, "unbonding": 0.0,
           "rew_usdc": 0.0, "rew_dydx": 0.0, "trading_equity": 0.0}
    for a in addrs.values():
        bals = rest(DYDX_REST, f"/cosmos/bank/v1beta1/balances/{a}?pagination.limit=200", "dYdX balances")["balances"]
        b = {x["denom"]: int(x["amount"]) for x in bals}
        out["usdc"] += b.get(USDC_IBC, 0) / 1e6
        out["dydx_liquid"] += b.get("adydx", 0) / 1e18
        dels = rest(DYDX_REST, f"/cosmos/staking/v1beta1/delegations/{a}?pagination.limit=200",
                    "dYdX delegations")["delegation_responses"]
        for d in dels:
            amt = int(d["balance"]["amount"]) / 1e18
            if amt <= 0:
                continue
            v = d["delegation"]["validator_address"]
            row = out["delegations"].setdefault(v, {"name": moniker.get(v, "Unknown validator"),
                                                    "active": status.get(v) == "BOND_STATUS_BONDED", "dydx": 0.0})
            row["dydx"] += amt
        ub = rest(DYDX_REST, f"/cosmos/staking/v1beta1/delegators/{a}/unbonding_delegations",
                  "dYdX unbonding")["unbonding_responses"]
        out["unbonding"] += sum(int(e["balance"]) for u in ub for e in u["entries"]) / 1e18
        rw = rest(DYDX_REST, f"/cosmos/distribution/v1beta1/delegators/{a}/rewards", "dYdX rewards")
        for c in rw.get("total", []):
            if c["denom"] == USDC_IBC:
                out["rew_usdc"] += float(c["amount"]) / 1e6
            elif c["denom"] == "adydx":
                out["rew_dydx"] += float(c["amount"]) / 1e18
        try:
            sub = get(f"{INDEXER}/addresses/{a}")
            out["trading_equity"] += sum(float(s.get("equity") or 0) for s in sub.get("subaccounts", []))
        except Exception:
            pass   # 404 when the address has never used the exchange
    out["height"] = int(rest(DYDX_REST, "/cosmos/base/tendermint/v1beta1/blocks/latest",
                             "latest block")["block"]["header"]["height"])
    return out


def read_osmosis(addrs: dict) -> float:
    osmo = 0.0
    for a in addrs.values():
        bals = rest(OSMO_REST, f"/cosmos/bank/v1beta1/balances/{a}?pagination.limit=200", "Osmosis balances")["balances"]
        osmo += sum(int(x["amount"]) for x in bals if x["denom"] == "uosmo") / 1e6
    return osmo


def read_evm(addrs: dict) -> dict:
    out = {k: 0.0 for k in EVM_TOKENS}
    out["ETH_GAS"] = 0.0
    for a in addrs.values():
        for chain in ("ethereum", "arbitrum"):
            out["ETH_GAS"] += int(rpc(chain, "eth_getBalance", [a, "latest"], "gas balance"), 16) / 1e18
        for sym, (chain, t, dec, kind) in EVM_TOKENS.items():
            bal = call(chain, t, "0x70a08231" + pad(a), f"{sym} balance")
            if not bal:
                continue
            if kind == "erc4626":   # value the vault shares at the vault's own share price, in USDC
                out[sym] += call(chain, t, "0x07a2d13a" + hex(bal)[2:].rjust(64, "0"), f"{sym} convertToAssets") / 1e6
            else:
                out[sym] += bal / 10 ** dec
    return out


def read_all(addrs: dict) -> dict:
    return {"prices": prices(), "dydx": read_dydx(addrs["dydx"]), "osmo": read_osmosis(addrs.get("osmosis", {})),
            "evm": read_evm(addrs["evm"]), "taken_utc": dt.datetime.now(dt.timezone.utc)}


# ---- aggregate (this is all that leaves memory) -------------------------------------------------
def r_usd(v: float) -> float:
    return round(v, -3) if abs(v) >= 10_000 else round(v, -1)


def aggregate(raw: dict) -> dict:
    px, d, e = raw["prices"]["usd"], raw["dydx"], raw["evm"]
    staked = sum(x["dydx"] for x in d["delegations"].values())
    positions = [   # id, chain, venue, venue sub, {asset: usd}
        ("staking", "dYdX Chain", "Staking Programme", None,
         {"DYDX": (staked + d["unbonding"]) * px["DYDX"]}),
        ("rewards", "dYdX Chain", "Staking Programme", None,
         {"USDC": d["rew_usdc"] * px["USDC"], "DYDX": d["rew_dydx"] * px["DYDX"]}),
        ("buyback_usdc", "dYdX Chain", "Buyback wallet", "USDC on dYdX Chain", {"USDC": d["usdc"] * px["USDC"]}),
        ("trading", "dYdX Chain", "dYdX exchange", "Trading account equity", {"USDC": d["trading_equity"]}),
        ("morpho", "Ethereum", "Morpho", "KPK USDC Prime Vault", {"USDC": e["MORPHO_KPK_USDC"] * px["USDC"]}),
        ("usdy", "Ethereum", "Ondo Finance", "US Dollar Yield (USDY)", {"USDY": e["USDY"] * px["USDY"]}),
        ("eth_wallet", "Ethereum", "Staking Vault wallet", "Stablecoins held in the wallet",
         {"USDC": e["USDC_ETH"] * px["USDC"], "USDT": e["USDT_ETH"] * px["USDT"]}),
        ("arb_vault", "Arbitrum", "Buyback Vault", "Stablecoins on Arbitrum",
         {"USDT": e["USDT_ARB"] * px["USDT"], "USDC": e["USDC_ARB"] * px["USDC"]}),
    ]
    small = d["dydx_liquid"] * px["DYDX"] + e["ETH_GAS"] * px["ETH"] + raw["osmo"] * px["OSMO"]
    pos = [{"id": i, "chain": c, "venue": v, "venue_sub": s,
            "assets": {k: r_usd(x) for k, x in a.items() if x >= 1}}
           for i, c, v, s, a in positions]
    pos = [p for p in pos if sum(p["assets"].values()) > 0]
    vals = sorted(({"name": re.sub(r"[^\w .,&()\-]", "", x["name"]).strip() or "Unnamed validator",
                    "dydx": round(x["dydx"], -4), "active": x["active"]}
                   for x in d["delegations"].values() if round(x["dydx"], -4) > 0), key=lambda r: -r["dydx"])
    taken = raw["taken_utc"]
    return {
        "updated_utc": taken.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "dydx_height": d["height"],
        "prices": {**{k: round(v, 6) for k, v in px.items()}, "source": raw["prices"]["source"],
                   "binance_dydxusdt": raw["prices"]["binance_dydxusdt"]},
        "positions": pos,
        "small_balances_usd": r_usd(small),
        "staking": {"delegated_dydx": round(staked, -4), "unbonding_dydx": round(d["unbonding"], -4),
                    "rewards_usdc": r_usd(d["rew_usdc"]), "validators": vals},
    }


def pos_values(f: dict) -> dict:
    return {p["id"]: sum(p["assets"].values()) for p in f["positions"]}


def gate(new: dict, prev: dict | None) -> list[str]:
    if not prev:
        return []
    a, b = pos_values(new), pos_values(prev)
    flags = []
    for k in set(a) | set(b):
        x, y = a.get(k, 0), b.get(k, 0)
        if abs(x - y) > GATE_ABS and abs(x - y) > GATE_REL * max(y, 1):
            flags.append(f"{k}: {y:,.0f} -> {x:,.0f}")
    return flags


def agree(f1: dict, f2: dict) -> bool:
    a, b = pos_values(f1), pos_values(f2)
    return set(a) == set(b) and all(abs(a[k] - b[k]) <= AGREE * max(a[k], b[k], 1) + 1000 for k in a)


# ---- render ------------------------------------------------------------------------------------
COL = {"DYDX": "var(--c-dydx)", "USDC": "var(--c-stable)", "USDT": "var(--c-stable)", "USDY": "var(--c-rwa)"}
ASSET_SUB = {"DYDX": "Native token", "USDC": "Stablecoin", "USDY": "Tokenised US Treasuries", "USDT": "Stablecoin"}
CHAIN_MARK = {"dYdX Chain": "dydx", "Ethereum": "ethereum", "Arbitrum": "arbitrum"}
CHAIN_ORDER = ["dYdX Chain", "Ethereum", "Arbitrum"]
ASSET_ORDER = ["DYDX", "USDC", "USDY", "USDT"]
STABLE_NOTE = {
    "usdy": ("usdy", "Ondo USDY", "Tokenised short-term US Treasuries and bank deposits"),
    "buyback_usdc": ("usdc", "USDC, Buyback wallet", "Source of the DYDX buyback budget"),
    "morpho": ("usdc", "KPK USDC Prime Vault (Morpho)", "USDC lending vault curated by KPK"),
    "arb_vault": ("usdt", "Stablecoins, Buyback Vault", "Buyback budget held on Arbitrum"),
    "eth_wallet": ("usdc", "Stablecoins, Staking Vault wallet", "Held in the wallet, not deployed"),
    "trading": ("usdc", "dYdX trading account", "Equity on the dYdX exchange"),
    "rewards": ("usdc", "Unclaimed staking rewards", "USDC and DYDX accrued, not yet withdrawn"),
}


def m(v: float) -> str:
    if v >= 1e6:
        return f"${v / 1e6:.2f}m"
    if v >= 1e3:
        return f"${v / 1e3:.0f}k"
    return f"${v:,.0f}"


def render(f: dict) -> str:
    P = f["prices"]
    small = f["small_balances_usd"]
    total = sum(sum(p["assets"].values()) for p in f["positions"]) + small

    def pct(v: float, dgt: int = 1) -> str:
        return f"{100 * v / total:.{dgt}f}%"

    # venues: merge positions that share a venue (staking + rewards)
    venues: dict[str, dict] = {}
    for p in f["positions"]:
        v = venues.setdefault(p["venue"], {"chain": p["chain"], "sub": p["venue_sub"], "assets": {}})
        for a, x in p["assets"].items():
            v["assets"][a] = v["assets"].get(a, 0) + x
    chains = {c: sum(sum(v["assets"].values()) for v in venues.values() if v["chain"] == c) for c in CHAIN_ORDER}
    assets = {a: sum(v["assets"].get(a, 0) for v in venues.values()) for a in ASSET_ORDER}
    st = f["staking"]
    vals = [v for v in st["validators"] if v["dydx"] > 0]
    n_val, n_active = len(vals), sum(v["active"] for v in vals)
    if "Staking Programme" in venues:
        venues["Staking Programme"]["sub"] = f"{n_val} validators"
    staked_usd = (st["delegated_dydx"] + st["unbonding_dydx"]) * P["DYDX"]
    dydx_usd = assets["DYDX"]
    non_dydx = total - dydx_usd

    # ---- sankey (drawn from venues >= 0.05% of the total; the rest stays in the figures) ----
    floor = 0.0005 * total
    shown = {k: v for k, v in venues.items() if sum(v["assets"].values()) >= floor}
    # within a chain, order venues by their main asset (same order as the asset column) so bands do not cross
    def main_asset(k):
        return ASSET_ORDER.index(max(shown[k]["assets"], key=shown[k]["assets"].get))
    venue_ord = sorted(shown, key=lambda k: (CHAIN_ORDER.index(shown[k]["chain"]), main_asset(k),
                                             -sum(shown[k]["assets"].values())))
    nodes = [(f"c:{c}", 0, c, {"dYdX Chain": "Cosmos appchain", "Ethereum": "Staking Vault wallet",
                               "Arbitrum": "Buyback Vault wallet"}[c], chains[c], CHAIN_MARK[c])
             for c in CHAIN_ORDER if chains[c] >= floor]
    nodes += [(f"v:{k}", 1, k, shown[k]["sub"] or "", sum(shown[k]["assets"].values()), None) for k in venue_ord]
    nodes += [(f"a:{a}", 2, a, ASSET_SUB[a], assets[a], a.lower()) for a in ASSET_ORDER if assets[a] >= floor]
    node_ids = {n[0] for n in nodes}
    links = []
    for k in venue_ord:
        for a in ASSET_ORDER:
            x = shown[k]["assets"].get(a, 0)
            if x > 0 and f"a:{a}" in node_ids:
                links.append((f"c:{shown[k]['chain']}", f"v:{k}", x, a))
    for k in venue_ord:
        for a in ASSET_ORDER:
            x = shown[k]["assets"].get(a, 0)
            if x > 0 and f"a:{a}" in node_ids:
                links.append((f"v:{k}", f"a:{a}", x, a))

    SW, TOP, BOT, NW, GAP, MINH, SLOT = 1000, 30, 14, 10, 12, 3, 58
    XS = {0: 175, 1: 505, 2: 800}
    scale = 230 / max(n[4] for n in nodes)
    cols = {c: [n for n in nodes if n[1] == c] for c in XS}
    slots = {c: [max(SLOT, max(MINH, n[4] * scale) + 16) for n in col] for c, col in cols.items()}
    SH = TOP + BOT + max(sum(v) + GAP * (len(v) - 1) for v in slots.values())
    pos = {}
    for c, col in cols.items():
        tot = sum(slots[c]) + GAP * (len(col) - 1)
        y = TOP + (SH - TOP - BOT - tot) / 2
        for n, sl in zip(col, slots[c]):
            h = max(MINH, n[4] * scale)
            top = y + (sl - h) / 2
            pos[n[0]] = {"x": XS[c], "y": top, "h": h, "out": top, "in": top}
            y += sl + GAP
    paths = []
    for s, t, v, a in links:
        h = max(1.2, v * scale)
        ps, pt = pos[s], pos[t]
        y0, y1 = ps["out"], pt["in"]
        ps["out"] += h
        pt["in"] += h
        x0, x1 = ps["x"] + NW, pt["x"]
        xm = (x0 + x1) / 2
        paths.append(f'<path class="lk" style="fill:{COL[a]}" d="M{x0:.1f},{y0:.1f} C{xm:.1f},{y0:.1f} {xm:.1f},{y1:.1f} '
                     f'{x1:.1f},{y1:.1f} L{x1:.1f},{y1 + h:.1f} C{xm:.1f},{y1 + h:.1f} {xm:.1f},{y0 + h:.1f} '
                     f'{x0:.1f},{y0 + h:.1f} Z"><title>{html.escape(s[2:])} to {html.escape(t[2:])}: {a} {m(v)}</title></path>')
    labels = []
    for nid, c, lab, sub, v, mk in nodes:
        p = pos[nid]
        cy = p["y"] + p["h"] / 2
        labels.append(f'<rect class="nd" x="{p["x"]}" y="{p["y"]:.1f}" width="{NW}" height="{p["h"]:.1f}"/>')
        tx, anchor = (p["x"] - 14, "end") if c == 0 else (p["x"] + NW + 14, "start")
        tx_name = tx
        if mk:
            ix = (tx - 20) if anchor == "end" else tx
            src = "assets/dydx.svg" if mk == "dydx" else ("assets/usdy_icon.png" if mk == "usdy" else f"assets/{mk}.svg")
            labels.append(f'<image href="{src}" x="{ix}" y="{cy - 27:.1f}" width="20" height="20"/>')
            tx_name = tx - 28 if anchor == "end" else tx + 28
        halo = " halo" if c == 1 else ""
        labels.append(f'<text class="nl{halo}" x="{tx_name}" y="{cy - 12:.1f}" text-anchor="{anchor}">{html.escape(lab)}</text>')
        labels.append(f'<text class="nv{halo}" x="{tx}" y="{cy + 8:.1f}" text-anchor="{anchor}"><tspan class="nvb">{m(v)}</tspan>'
                      f'  {pct(v)}</text>')
        labels.append(f'<text class="ns{halo}" x="{tx}" y="{cy + 25:.1f}" text-anchor="{anchor}">{html.escape(sub)}</text>')
    heads = [(XS[0] + NW, "end", "Chain"), (XS[1], "start", "Where it is deployed"), (XS[2], "start", "Asset")]
    colheads = "".join(f'<text class="ch" x="{x}" y="10" text-anchor="{a}">{t}</text>' for x, a, t in heads)
    sankey = (f'<svg class="sankey" viewBox="0 0 {SW} {SH}" role="img" aria-label="Flow of treasury funds from chain to '
              f'venue to asset">{colheads}{"".join(paths)}{"".join(labels)}</svg>')

    # ---- validators ----
    vmax = vals[0]["dydx"] if vals else 1
    vrows = "".join(
        f'<div class="vr"><span class="vn">{html.escape(v["name"])}{"" if v["active"] else " (inactive)"}</span>'
        f'<span class="vb"><i style="width:{100 * v["dydx"] / vmax:.1f}%"></i></span>'
        f'<span class="vv num">{v["dydx"] / 1e6:.2f}m</span></div>' for v in vals)
    staked = st["delegated_dydx"]
    val_status = (f"{n_val}, all in the active set" if n_active == n_val
                  else f"{n_val}, of which {n_active} in the active set")
    unbonding = (f"{st['unbonding_dydx'] / 1e6:.2f}m DYDX in the 21-day unbonding period, included above"
                 if st["unbonding_dydx"] >= 10_000 else "None in progress; unbonding takes 21 days on dYdX Chain")

    # ---- stablecoin table ----
    srows, stot = "", 0.0
    order = ["usdy", "buyback_usdc", "morpho", "arb_vault", "eth_wallet", "trading", "rewards"]
    byid = {p["id"]: p for p in f["positions"]}
    for k in order:
        if k not in byid:
            continue
        v = sum(byid[k]["assets"].values())
        mk, name, note = STABLE_NOTE[k]
        stot += v
        srows += (f'<tr><td><span class="cell"><img class="mk" src="assets/{"usdy_icon.png" if mk == "usdy" else mk + ".svg"}" alt="">'
                  f'{html.escape(name)}</span><span class="note">{html.escape(note)}</span></td>'
                  f'<td>{html.escape(byid[k]["chain"])}</td><td class="r num">{m(v)}</td><td class="r num">{pct(v)}</td></tr>')
    srows += (f'<tr class="tot"><td>Total, RWA and stablecoins</td><td></td><td class="r num">{m(stot)}</td>'
              f'<td class="r num">{pct(stot)}</td></tr>')

    by_chain = ", ".join(f"{c} {pct(chains[c], 0 if chains[c] / total >= 0.1 else 1)}" for c in CHAIN_ORDER if chains[c] > 0)
    taken = dt.datetime.fromisoformat(f["updated_utc"].replace("Z", "+00:00"))
    top = vals[0] if vals else {"name": "n/a", "dydx": 0}
    R = {
        "{{TS}}": f["updated_utc"], "{{DATE_ISO}}": taken.strftime("%Y-%m-%d"), "{{TIME}}": taken.strftime("%H:%M"),
        "{{DATE_LONG}}": f"{taken.day} {taken:%B %Y}",
        "{{TOTAL}}": m(total), "{{DYDX_SHARE}}": f"{100 * dydx_usd / total:.0f}%", "{{NON_DYDX}}": m(non_dydx),
        "{{NON_DYDX_SHARE}}": f"{100 * non_dydx / total:.0f}%",
        "{{STAKED_M}}": f"{staked / 1e6:.2f}m", "{{STAKED_USD}}": m(staked * P["DYDX"]), "{{N_VAL}}": str(n_val),
        "{{VAL_STATUS}}": val_status, "{{UNBONDING}}": unbonding,
        "{{TOP_VAL}}": html.escape(top["name"]), "{{TOP_SHARE}}": f"{100 * top['dydx'] / max(staked, 1):.1f}%",
        "{{SANKEY}}": sankey, "{{VROWS}}": vrows, "{{SROWS}}": srows, "{{BY_CHAIN}}": by_chain,
        "{{PX_DYDX}}": f"${P['DYDX']:.4f}",
        "{{PX_DYDX_BN}}": f"${P['binance_dydxusdt']:.4f}" if P.get("binance_dydxusdt") else "unavailable",
        "{{PX_USDY}}": f"${P['USDY']:.4f}", "{{HEIGHT}}": f"{int(f['dydx_height']):,}",
        "{{DUST}}": m(small), "{{SENS}}": m(0.10 * dydx_usd),
    }
    out = (ROOT / "scripts" / "template.html").read_text(encoding="utf-8")
    for k, v in R.items():
        out = out.replace(k, v)
    left = re.findall(r"\{\{[A-Z_]+\}\}", out)
    if left:
        raise ReadError(f"unfilled placeholders: {left}")
    if _ADDR_RE.search(out):
        raise ReadError("refusing to write: an address-like string reached the page")
    return out


def write(f: dict) -> None:
    page = render(f)
    body = json.dumps(f, indent=1)
    if any(s.lower() in body.lower() or s.lower() in page.lower() for s in _SECRETS):
        raise ReadError("refusing to write: a treasury address reached the output")
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(body + "\n", encoding="utf-8")
    (ROOT / "index.html").write_text(page, encoding="utf-8")


def main() -> int:
    if "--build" in sys.argv:
        (ROOT / "index.html").write_text(render(json.loads(DATA.read_text())), encoding="utf-8")
        print("index.html rebuilt from data/funds.json")
        return 0
    addrs = load_addresses()
    prev = json.loads(DATA.read_text()) if DATA.exists() else None
    new = aggregate(read_all(addrs))
    flags = gate(new, prev)
    if flags:
        print("large move since the last read, reading again: " + "; ".join(flags))
        time.sleep(30)
        again = aggregate(read_all(addrs))
        if not agree(new, again):
            print("STOP: the two reads disagree; keeping the published figures")
            return 2
        new = again
    write(new)
    tot = sum(pos_values(new).values()) + new["small_balances_usd"]
    print(f"written: total {m(tot)}, {len(new['positions'])} positions, "
          f"{len(new['staking']['validators'])} validators, block {new['dydx_height']:,}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:   # never let a raw message (which may hold a URL path) reach the log
        print("ERROR: " + redact(f"{type(e).__name__}: {e}"))
        sys.exit(1)
