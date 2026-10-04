#!/usr/bin/env python3
"""
Updates prices/prices.json from one of two sources:

  AHledger (default) — fetches live AH data via the AHledger public API.
  Auctionator        — parses a local prices/Auctionator.lua SavedVariables file.

Usage:
    python update-prices.py [market_id]           # AHledger (default market)
    python update-prices.py --markets             # list AHledger markets

    python update-prices.py auctionator           # parse Auctionator.lua (auto-select realm)
    python update-prices.py auctionator --realms  # list realms in Auctionator.lua
    python update-prices.py auctionator --realm "RealmName-Faction"
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

API_BASE   = "https://api.ahledger.com/v1"
DATA_DIR   = Path(__file__).parent / "data"
PRICES_DIR = Path(__file__).parent / "prices"
PRICES_OUT = PRICES_DIR / "prices.json"
AUCTIONATOR_LUA = PRICES_DIR / "Auctionator.lua"

DEFAULT_MARKET = "forever.normal.horde.us"


def fetch(path: str) -> bytes:
    req = Request(
        f"{API_BASE}{path}",
        headers={"User-Agent": "WowProfessionAssistant/1.0"},
    )
    with urlopen(req, timeout=30) as resp:
        return resp.read()


def list_markets() -> None:
    data = json.loads(fetch("/markets"))
    forever = [m for m in data["markets"] if m["game"] == "forever"]
    other   = [m for m in data["markets"] if m["game"] != "forever"]
    print("WoW: Forever markets:")
    for m in forever:
        print(f"  {m['id']:<40}  {m['label']}")
    if other:
        print("\nOther markets:")
        for m in other:
            print(f"  {m['id']:<40}  {m['label']}")


def build_name_lookup() -> dict[str, str]:
    """
    Load all profession JSON files from data/ and merge their item name
    mappings into a single {item_id_str: name} dict.
    """
    names: dict[str, str] = {}
    for path in DATA_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        for item_id, info in data.get("items", {}).items():
            if info.get("name") and item_id not in names:
                names[item_id] = info["name"]
    return names


def fetch_prices(market: str) -> tuple[dict[str, int], int]:
    """
    Fetch the bulk pricetable for `market` and return
    ({item_id_str: min_buyout_copper}, scan_timestamp).

    Pricetable format (plain text):
      Line 1 header : AHL1|{market}|{unix_ts}|{item_count}
      Data rows     : {item_id}:{minBuyout}:{median}:{quantity}:{auctions}:{median7d}:{median30d}:{median90d}
    """
    raw = fetch(f"/pricetable/{market}").decode()
    lines = raw.strip().splitlines()

    if not lines or not lines[0].startswith("AHL1|"):
        raise ValueError(f"Unexpected pricetable format: {lines[0][:80]!r}")

    header_parts = lines[0].split("|")
    scan_ts    = int(header_parts[2]) if len(header_parts) >= 3 else 0
    item_count = int(header_parts[3]) if len(header_parts) >= 4 else "?"
    print(f"Market : {market}")
    print(f"Items  : {item_count}")

    prices: dict[str, int] = {}
    for line in lines[1:]:
        if not line:
            continue
        parts = line.split(":")
        if len(parts) < 2:
            continue
        item_id    = parts[0]
        min_buyout = int(parts[1])
        if min_buyout > 0:
            prices[item_id] = min_buyout

    return prices, scan_ts


def enrich_with_names(prices: dict[str, int], names: dict[str, str]) -> dict[str, dict]:
    """Combine price and name into a single entry per item."""
    return {
        item_id: {"name": names.get(item_id, ""), "price": price}
        for item_id, price in prices.items()
    }


# ---------------------------------------------------------------------------
# Auctionator source
# ---------------------------------------------------------------------------

def _extract_braces(text: str, start: int) -> str | None:
    """Return the substring from `start` to the matching closing brace,
    skipping string literals so inner braces don't affect the depth count."""
    depth = 0
    i = start
    in_string = False
    string_char = ""
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\" and i + 1 < len(text):
                i += 2
                continue
            if c == string_char:
                in_string = False
        else:
            if c in ('"', "'"):
                in_string = True
                string_char = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    return None


def parse_auctionator_lua(lua_path: Path) -> tuple[dict, list[str]]:
    """
    Parse AUCTIONATOR_PRICE_DATABASE from a SavedVariables Lua file.
    Returns (realm_data, realm_names) where realm_data maps
    realm_str → {item_name: min_price_copper}.
    """
    import json5

    text = lua_path.read_text(encoding="utf-8", errors="replace")

    # Find the assignment and extract the outer table block
    marker = "AUCTIONATOR_PRICE_DATABASE"
    idx = text.find(marker)
    if idx == -1:
        raise ValueError(f"AUCTIONATOR_PRICE_DATABASE not found in {lua_path}")

    brace_start = text.find("{", idx)
    if brace_start == -1:
        raise ValueError("No opening brace found after AUCTIONATOR_PRICE_DATABASE")

    block = _extract_braces(text, brace_start)
    if not block:
        raise ValueError("Could not find matching closing brace")

    raw = json5.loads(block)

    # raw: { "Realm-Faction": { "Item Name": { m=..., h=..., l=..., a=... } } }
    realm_data: dict[str, dict[str, int]] = {}
    for realm, items in raw.items():
        if not isinstance(items, dict):
            continue
        realm_data[realm] = {
            name: int(entry["m"])
            for name, entry in items.items()
            if isinstance(entry, dict) and entry.get("m")
        }

    return realm_data, list(realm_data.keys())


def build_name_to_id() -> dict[str, str]:
    """Build a reverse {item_name_lower: item_id} lookup from profession JSON files."""
    name_to_id: dict[str, str] = {}
    for path in DATA_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        for item_id, info in data.get("items", {}).items():
            name = info.get("name", "")
            if name:
                key = name.lower()
                if key not in name_to_id:
                    name_to_id[key] = item_id
    return name_to_id


def auctionator_prices(realm_data: dict[str, dict[str, int]], realm: str) -> dict[str, int]:
    """
    Resolve Auctionator item names to item IDs using profession JSON data.
    Returns {item_id_str: min_buyout_copper} for matched items.
    """
    name_to_id = build_name_to_id()
    prices: dict[str, int] = {}
    for item_name, price in realm_data[realm].items():
        item_id = name_to_id.get(item_name.lower())
        if item_id:
            prices[item_id] = price
    return prices


def main() -> None:
    args = sys.argv[1:]

    if "--markets" in args:
        list_markets()
        return

    source = args[0].lower() if args else ""

    # --- Auctionator mode ---
    if source == "auctionator":
        if not AUCTIONATOR_LUA.exists():
            print(f"Error: {AUCTIONATOR_LUA} not found.")
            sys.exit(1)

        try:
            realm_data, realm_names = parse_auctionator_lua(AUCTIONATOR_LUA)
        except Exception as e:
            print(f"Parse error: {e}")
            sys.exit(1)

        if "--realms" in args:
            print("Realms found in Auctionator.lua:")
            for r in realm_names:
                print(f"  {r}")
            return

        realm_arg = None
        if "--realm" in args:
            idx = args.index("--realm")
            if idx + 1 < len(args):
                realm_arg = args[idx + 1]
            else:
                print("Error: --realm requires a value.")
                sys.exit(1)

        if realm_arg:
            if realm_arg not in realm_data:
                print(f"Error: realm {realm_arg!r} not found. Available: {realm_names}")
                sys.exit(1)
            realm = realm_arg
        elif len(realm_names) == 1:
            realm = realm_names[0]
        else:
            print(f"Multiple realms found: {realm_names}")
            print("Specify one with: python update-prices.py auctionator --realm \"<realm>\"")
            sys.exit(1)

        prices = auctionator_prices(realm_data, realm)
        names  = build_name_lookup()
        named  = sum(1 for iid in prices if names.get(iid))
        entries = enrich_with_names(prices, names)

        print(f"Realm  : {realm}")
        print(f"Items  : {len(realm_data[realm])} in Lua, {len(entries)} matched to item IDs")

        output = {
            "source":     "auctionator",
            "market":     realm,
            "url":        str(AUCTIONATOR_LUA),
            "scan_at":    None,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "prices":     entries,
        }

        PRICES_OUT.write_text(json.dumps(output, indent=2))
        print(f"Saved {len(entries)} prices ({named} with names) → {PRICES_OUT}")
        return

    # --- AHledger mode ---
    market = args[0] if args else DEFAULT_MARKET

    try:
        prices, scan_ts = fetch_prices(market)
    except URLError as e:
        print(f"Error fetching prices: {e}")
        sys.exit(1)
    except ValueError as e:
        print(f"Parse error: {e}")
        sys.exit(1)

    names   = build_name_lookup()
    named   = sum(1 for iid in prices if names.get(iid))
    entries = enrich_with_names(prices, names)

    scan_at = datetime.fromtimestamp(scan_ts, tz=timezone.utc).isoformat() if scan_ts else None

    output = {
        "source":     "ahledger",
        "market":     market,
        "url":        f"{API_BASE}/pricetable/{market}",
        "scan_at":    scan_at,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "prices":     entries,
    }

    PRICES_OUT.write_text(json.dumps(output, indent=2))
    print(f"Saved {len(entries)} prices ({named} with names) → {PRICES_OUT}")


if __name__ == "__main__":
    main()
