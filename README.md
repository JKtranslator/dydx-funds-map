# dYdX Treasury SubDAO: funds map

One page showing where the dYdX Treasury SubDAO's funds sit: which chain, which venue (staking,
Morpho, Ondo, buyback wallets) and which asset. Balances are read onchain and the page refreshes
every hour.

Prepared by **KPK** for the **dYdX Treasury SubDAO**.

**Live:** https://jktranslator.github.io/dydx-funds-map/

---

## How it works

Static site on GitHub Pages. No backend.

```
index.html              the page (generated; do not edit by hand)
data/funds.json         aggregated, rounded figures behind the page
scripts/refresh.py      reads the chains, gates the read, writes data/funds.json and index.html
scripts/template.html   the page layout (KPK factsheet style); edit this, not index.html
assets/                 KPK logo, chain and token marks
.github/workflows/      refresh.yml: runs the script hourly and on demand, commits if changed
```

Sources: dYdX Chain and Osmosis REST (balances, delegations, unbonding, unclaimed rewards), the dYdX
indexer, Ethereum and Arbitrum public RPCs (token balances; the Morpho vault at its own share price),
CoinGecko prices with DefiLlama as fallback, and the Binance DYDX-USDT ticker as a cross-check.

### Addresses are not in this repository

The treasury addresses are read from the `FUNDS_MAP_ADDRESSES` repository secret (encrypted, masked in
logs, not available to workflows from forks). The script keeps per-wallet balances in memory, publishes
only totals per position, rounds them, redacts any address from error messages, and refuses to write
if an address-like string reaches the output.

Secret format (one line of JSON):

```json
{"dydx": {"<label>": "<dydx1...>"}, "osmosis": {"<label>": "<osmo1...>"}, "evm": {"<label>": "<0x...>"}}
```

### Gate

If any position moves by more than $250k and 25% since the last published read, the script reads
everything again 30 seconds later and publishes only if the two reads agree within 1%. Otherwise it
exits with code 2 and the page keeps the last good figures. A failed read exits 1 and writes nothing.

### Run locally

```bash
python scripts/refresh.py            # needs FUNDS_MAP_ADDRESSES or ~/.kpk/dydx_funds_map_addresses.json
python scripts/refresh.py --build    # re-render index.html from data/funds.json, no network
```

Stdlib only, Python 3.10+.
