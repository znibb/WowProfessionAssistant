#!/usr/bin/env python3
"""
Scrapes WoW profession recipes from Wowhead and stores them as JSON.

Usage:
    python scraper.py [profession_name] [full]

    profession_name : One of the keys in PROFESSIONS below.
                      Omit to scrape all professions.
    full            : Pass "full" as the second argument to also fetch individual
                      spell pages for recipes whose cooldown cannot be determined
                      from the profession page alone. Omit for a faster scrape
                      that leaves those cooldowns as null.
"""

import sys
import re
import json
import time
from pathlib import Path
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

SCRAPE_DELAY = 3 # seconds

# ---------------------------------------------------------------------------
# Profession registry
# ---------------------------------------------------------------------------
PROFESSIONS = {
    "alchemy":        "https://www.wowhead.com/forever/skill=171/alchemy",
    "blacksmithing":  "https://www.wowhead.com/forever/skill=164/blacksmithing",
    "cooking":        "https://www.wowhead.com/forever/skill=185/cooking",
    "enchanting":     "https://www.wowhead.com/forever/skill=333/enchanting",
    "engineering":    "https://www.wowhead.com/forever/skill=202/engineering",
    "first-aid":      "https://www.wowhead.com/forever/skill=129/first-aid",
    "leatherworking": "https://www.wowhead.com/forever/skill=165/leatherworking",
    "tailoring":      "https://www.wowhead.com/forever/skill=197/tailoring",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DATA_DIR = Path(__file__).parent / "data"
REAGENT_CACHE_PATH = DATA_DIR / "reagent-cache.json"

# Wowhead zone ID for Orgrimmar (confirmed from vendor location data)
ORGRIMMAR_ZONE_ID = 1637

# Shared Playwright browser instance (created once per run)
_browser = None
_playwright = None


def get_browser():
    global _browser, _playwright
    if _browser is None:
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
    return _browser


def shutdown_browser():
    global _browser, _playwright
    if _browser:
        _browser.close()
        _browser = None
    if _playwright:
        _playwright.stop()
        _playwright = None

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def extract_balanced(text: str, start: int, open_char: str, close_char: str) -> str | None:
    """
    Return the substring of `text` from `start` to the closing bracket that
    balances the first `open_char` encountered at `start`.  String literals
    (single- and double-quoted) are skipped so inner brackets inside strings
    don't affect the depth counter.
    """
    depth = 0
    i = start
    in_string = False
    string_char = ""

    while i < len(text):
        c = text[i]

        if in_string:
            if c == "\\" and i + 1 < len(text):
                i += 2          # skip escaped character
                continue
            elif c == string_char:
                in_string = False
        else:
            if c in ('"', "'"):
                in_string = True
                string_char = c
            elif c == open_char:
                depth += 1
            elif c == close_char:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]

        i += 1

    return None


def js_to_json(js: str) -> str:
    """
    Minimal conversion of a JavaScript object/array literal to JSON.
    Handles:
      - single-quoted strings → double-quoted
      - unquoted object keys → quoted
      - trailing commas before } or ]
    """
    # Single-quoted strings → double-quoted (simplified; doesn't handle all edge cases)
    result = re.sub(r"'((?:[^'\\]|\\.)*)'", lambda m: '"' + m.group(1).replace('"', '\\"') + '"', js)
    # Unquoted keys
    result = re.sub(r'(?<=[{,\s])(\w+)\s*:', r'"\1":', result)
    # Trailing commas
    result = re.sub(r',\s*([}\]])', r'\1', result)
    return result


def parse_js(js: str):
    """Try to parse a JS literal as Python data using json5 with json fallback."""
    try:
        import json5
        return json5.loads(js)
    except Exception:
        pass
    try:
        return json.loads(js)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(js_to_json(js))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_recipe_data(html: str) -> list:
    """
    Find the spell Listview on the page and return its raw `data` array.
    Wowhead embeds this as:
        new Listview({template: 'spell', ..., data: [{...}, ...]})
    """
    listview_re = re.compile(r"new\s+Listview\s*\(", re.DOTALL)

    for match in listview_re.finditer(html):
        # The argument to Listview() is a JS object — extract it
        arg_start = html.find("{", match.end())
        if arg_start == -1:
            continue
        arg_block = extract_balanced(html, arg_start, "{", "}")
        if not arg_block:
            continue

        # Only process the spell/recipe Listview
        if "'spell'" not in arg_block and '"spell"' not in arg_block:
            continue

        # Find `data:[` inside this block
        data_match = re.search(r"\bdata\s*:\s*\[", arg_block)
        if not data_match:
            continue

        array_start = arg_block.rfind("[", 0, data_match.end())
        array_text = extract_balanced(arg_block, array_start, "[", "]")
        if not array_text:
            continue

        parsed = parse_js(array_text)
        if isinstance(parsed, list) and parsed:
            return parsed

    return []


def extract_item_data(html: str) -> dict:
    """
    Extract item names and prices from all WH.Gatherer.addData(3, ...) calls.
    Returns a dict keyed by string item ID.
    """
    items: dict = {}
    # Second arg is a numeric locale code (e.g. 16), not a string
    pattern = re.compile(r"WH\.Gatherer\.addData\s*\(\s*3\s*,\s*\d+\s*,\s*\{")

    for match in pattern.finditer(html):
        obj_start = html.rfind("{", match.start(), match.end())
        obj_text = extract_balanced(html, obj_start, "{", "}")
        if not obj_text:
            continue

        parsed = parse_js(obj_text)
        if not isinstance(parsed, dict):
            continue

        for item_id, info in parsed.items():
            if not isinstance(info, dict):
                continue
            name = info.get("name_enus") or info.get("name", "")
            equip = info.get("jsonequip") or {}
            items[str(item_id)] = {
                "name":       name,
                "sell_price": int(equip.get("sellprice") or 0),
                "buy_price":  int(equip.get("buyprice") or 0),
            }

    return items


# ---------------------------------------------------------------------------
# Orgrimmar vendor check
# ---------------------------------------------------------------------------

def has_orgrimmar_vendor(html: str) -> bool:
    """
    Return True if the item's sold-by list contains at least one vendor
    whose location includes Orgrimmar (zone ID ORGRIMMAR_ZONE_ID).
    """
    listview_re = re.compile(r"new\s+Listview\s*\(", re.DOTALL)
    for match in listview_re.finditer(html):
        arg_start = html.find("{", match.end())
        if arg_start == -1:
            continue
        arg_block = extract_balanced(html, arg_start, "{", "}")
        if not arg_block:
            continue
        if "'sold-by'" not in arg_block and '"sold-by"' not in arg_block:
            continue

        data_match = re.search(r"\bdata\s*:\s*\[", arg_block)
        if not data_match:
            continue
        array_start = arg_block.rfind("[", 0, data_match.end())
        array_text = extract_balanced(arg_block, array_start, "[", "]")
        if not array_text:
            continue

        vendors = parse_js(array_text)
        if not isinstance(vendors, list):
            continue

        for vendor in vendors:
            if isinstance(vendor, dict) and ORGRIMMAR_ZONE_ID in vendor.get("location", []):
                return True

    return False


# ---------------------------------------------------------------------------
# Cooldown helpers
# ---------------------------------------------------------------------------

_COOLDOWN_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(days?|hours?|min(?:utes?)?|sec(?:onds?)?)\s+cooldown",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    "day": 86400, "days": 86400,
    "hour": 3600, "hours": 3600,
    "min": 60, "minute": 60, "minutes": 60,
    "sec": 1, "second": 1, "seconds": 1,
}


def parse_cooldown_str(s: str) -> int | None:
    """Parse a cooldown string like '3.958 days cooldown' into seconds. Returns None if blank."""
    if not s:
        return None
    m = _COOLDOWN_RE.search(s)
    if not m:
        return None
    value = float(m.group(1))
    unit  = m.group(2).lower().rstrip("s") + ("s" if m.group(2).lower().endswith("s") else "")
    # Normalise to base key
    unit_key = m.group(2).lower()
    factor = _UNIT_SECONDS.get(unit_key) or _UNIT_SECONDS.get(unit_key.rstrip("s"))
    if factor is None:
        return None
    return int(value * factor)


def cooldown_from_envchange(raw: dict) -> tuple[int | None, bool]:
    """
    Extract cooldown from a raw recipe's envChange block.
    Returns (cooldown_seconds, is_known).
      - is_known=True  → answer is definitive (no spell-page fetch needed)
      - is_known=False → envChange has no after/before data; must fetch spell page
    """
    env   = raw.get("envChange") or {}
    after = env.get("after")
    if after is not None:
        return parse_cooldown_str(after.get("cooldown", "")), True
    return None, False


def cooldown_from_spell_page(html: str) -> int | None:
    """
    Parse a spell page for its cooldown by finding the recipe Listview entry
    and reading its envChange.after.cooldown (same structure as profession page).
    Falls back to scanning all Listview data arrays.
    """
    listview_re = re.compile(r"new\s+Listview\s*\(", re.DOTALL)
    for match in listview_re.finditer(html):
        arg_start = html.find("{", match.end())
        if arg_start == -1:
            continue
        arg_block = extract_balanced(html, arg_start, "{", "}")
        if not arg_block:
            continue
        data_match = re.search(r"\bdata\s*:\s*\[", arg_block)
        if not data_match:
            continue
        array_start = arg_block.rfind("[", 0, data_match.end())
        array_text  = extract_balanced(arg_block, array_start, "[", "]")
        if not array_text:
            continue
        entries = parse_js(array_text)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            after = (entry.get("envChange") or {}).get("after")
            if after is not None:
                cd = parse_cooldown_str(after.get("cooldown", ""))
                if cd:
                    return cd
    return None


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_recipe(raw: dict, items: dict) -> dict | None:
    """Convert a raw Wowhead recipe entry to our storage format."""
    recipe_id = raw.get("id")
    name = raw.get("name") or raw.get("displayName", "")
    colors = raw.get("colors")      # [orange_min, yellow_min, green_min, gray_min]
    creates = raw.get("creates")    # [itemID, min_qty, max_qty]
    reagents = raw.get("reagents", [])

    if not (recipe_id and name and colors and creates):
        return None
    if len(colors) < 4 or len(creates) < 3:
        return None

    cooldown_seconds, is_known = cooldown_from_envchange(raw)

    return {
        "id":   recipe_id,
        "name": name,
        "creates": {
            "item_id": creates[0],
            "min_qty": creates[1],
            "max_qty": creates[2],
        },
        "skill_ranges": {
            "orange": colors[0],
            "yellow": colors[1],
            "green":  colors[2],
            "gray":   colors[3],
        },
        "learned_at":       raw.get("learnedat", colors[0]),
        "source":           raw.get("source", []),
        "cooldown_seconds": cooldown_seconds,
        "_needs_cooldown_fetch": not is_known,
        "reagents": [
            {
                "item_id":  r[0],
                "name":     items.get(str(r[0]), {}).get("name", ""),
                "quantity": r[1],
            }
            for r in reagents
            if isinstance(r, (list, tuple)) and len(r) >= 2
        ],
    }


# ---------------------------------------------------------------------------
# Reagent vendor cache  (keyed by string item_id → bool)
# ---------------------------------------------------------------------------

_reagent_cache: dict[str, bool] = {}


def _save_reagent_cache() -> None:
    REAGENT_CACHE_PATH.write_text(json.dumps(_reagent_cache, indent=2))


# ---------------------------------------------------------------------------
# Main scraping logic
# ---------------------------------------------------------------------------

def fetch_html(url: str) -> str:
    """Fetch page HTML using a real Chromium browser to pass bot/WAF checks."""
    browser = get_browser()
    ctx = browser.new_context(user_agent=USER_AGENT)
    page = ctx.new_page()
    page.add_init_script(
        'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
    )
    page.goto(url, wait_until="networkidle", timeout=60_000)
    html = page.content()
    ctx.close()
    return html


def scrape_profession(name: str, url: str, full: bool = False) -> dict:
    print(f"[{name}] Fetching {url} ...")
    html = fetch_html(url)

    raw_recipes = extract_recipe_data(html)
    print(f"[{name}] {len(raw_recipes)} raw recipes found")

    items = extract_item_data(html)
    print(f"[{name}] {len(items)} items found")

    # Identify reagent item IDs that are candidates for vendor purchasing
    # (buy_price > 0) and check whether they are sold in Orgrimmar.
    reagent_vendor_ids = {
        r[0]
        for raw in raw_recipes
        for r in raw.get("reagents", [])
        if isinstance(r, (list, tuple))
        and len(r) >= 2
        and items.get(str(r[0]), {}).get("buy_price", 0) > 0
    }
    vendor_ids = sorted(reagent_vendor_ids)
    uncached = [iid for iid in vendor_ids if str(iid) not in _reagent_cache]
    cached_count = len(vendor_ids) - len(uncached)
    print(f"[{name}] Checking {len(vendor_ids)} vendor-candidate reagents for Orgrimmar availability "
          f"({cached_count} cached, {len(uncached)} to fetch) ...")
    for iid in vendor_ids:
        if str(iid) in _reagent_cache:
            items[str(iid)]["orgrimmar_vendor"] = _reagent_cache[str(iid)]
    for idx, item_id in enumerate(uncached, 1):
        item_name = items.get(str(item_id), {}).get("name", "")
        print(f"\033[2K\r[{name}]   item {idx}/{len(uncached)} — {item_name} (id={item_id}) ...", end="", flush=True)
        item_url = f"https://www.wowhead.com/forever/item={item_id}"
        try:
            item_html = fetch_html(item_url)
            result = has_orgrimmar_vendor(item_html)
            items[str(item_id)]["orgrimmar_vendor"] = result
            _reagent_cache[str(item_id)] = result
            _save_reagent_cache()
        except Exception as exc:
            print(f"\033[2K\r[{name}]   WARNING: could not check item {item_id}: {exc}")
            items[str(item_id)]["orgrimmar_vendor"] = False
        time.sleep(1)
    if uncached:
        print(f"\033[2K\r[{name}]   {len(uncached)}/{len(uncached)} vendor items fetched.")
    else:
        print(f"[{name}]   All {len(vendor_ids)} vendor items served from cache.")

    recipes = [r for raw in raw_recipes if (r := normalize_recipe(raw, items))]
    print(f"[{name}] {len(recipes)} recipes normalized")

    # Fetch spell pages for recipes whose cooldown could not be determined
    # from the profession page's envChange data.
    needs_fetch = [r for r in recipes if r.get("_needs_cooldown_fetch")]
    total_spells = len(needs_fetch)
    if full and needs_fetch:
        print(f"[{name}] Fetching spell pages for {total_spells} recipes with unknown cooldown ...")
        for idx, recipe in enumerate(needs_fetch, 1):
            print(f"\033[2K\r[{name}]   spell {idx}/{total_spells} ({recipe['name']}) ...", end="", flush=True)
            spell_url = f"https://www.wowhead.com/forever/spell={recipe['id']}"
            try:
                spell_html = fetch_html(spell_url)
                recipe["cooldown_seconds"] = cooldown_from_spell_page(spell_html)
            except Exception as exc:
                print(f"\033[2K\r[{name}]   WARNING: could not fetch spell {recipe['id']}: {exc}")
            time.sleep(1)
        print(f"\033[2K\r[{name}]   {total_spells}/{total_spells} spell pages fetched.")
    else:
        print(f"[{name}] Skipping spell page fetch for {total_spells} recipes with unknown cooldown (run with 'full' to check).")

    for recipe in recipes:
        recipe.pop("_needs_cooldown_fetch", None)

    skill_match = re.search(r"skill=(\d+)", url)
    skill_id = int(skill_match.group(1)) if skill_match else 0

    return {
        "profession": name,
        "skill_id":   skill_id,
        "url":        url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "recipes":    recipes,
        "items":      items,
    }


def main() -> None:
    global _reagent_cache
    DATA_DIR.mkdir(exist_ok=True)

    # Purge the reagent cache at the start of every run so stale vendor data
    # doesn't carry over, but rebuild it fresh as professions are scraped.
    _reagent_cache = {}
    if REAGENT_CACHE_PATH.exists():
        REAGENT_CACHE_PATH.unlink()
    print("Reagent cache cleared.")

    args = sys.argv[1:]
    full = "full" in [a.lower() for a in args]
    profession_args = [a for a in args if a.lower() != "full"]

    if profession_args:
        profession = profession_args[0].lower()
        if profession not in PROFESSIONS:
            print(f"Unknown profession: {profession!r}")
            print(f"Available: {', '.join(sorted(PROFESSIONS))}")
            sys.exit(1)
        targets = {profession: PROFESSIONS[profession]}
    else:
        targets = PROFESSIONS

    if full:
        print("Full mode: spell pages will be fetched for unknown cooldowns.")

    for i, (name, url) in enumerate(targets.items()):
        if i > 0:
            time.sleep(SCRAPE_DELAY)       # polite delay between requests
        try:
            data = scrape_profession(name, url, full=full)
            out = DATA_DIR / f"{name}.json"
            data["recipes"].sort(key=lambda r: r["name"])
            out.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            print(f"[{name}] Saved → {out}\n")
        except Exception as exc:
            print(f"[{name}] ERROR: {exc}\n")

    shutdown_browser()


if __name__ == "__main__":
    main()
