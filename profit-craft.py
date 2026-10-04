#!/usr/bin/env python3
"""
Shows recipes that can be crafted and sold to a vendor for profit.

Only considers recipes with exactly one player-sourced material
(buy_price == 0), so break-even calculations are deterministic:
there is one unknown variable whose maximum purchase price is reported.

If that player-sourced material is itself a craftable intermediate of the
same profession (e.g. a Bolt of cloth), it is resolved one level down to
its raw material so the break-even price reflects what to pay on the AH.

When prices.json is present, actual AH prices are used to calculate
estimated profit per craft. The table is sorted by profit (highest first).
Without prices.json the table is sorted by break-even budget instead.

Usage:
    python profit-craft.py <profession_name> [skill_level]
    python profit-craft.py all [skill_level]

    profession_name : Profession to analyse.
    all             : Check all scraped professions and show only recipes with
                      a confirmed positive profit (requires prices.json).
    skill_level     : Maximum learned_at skill required (default: 300).
"""

import sys
import json
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.markup import escape
from rich import box

DATA_DIR   = Path(__file__).parent / "data"
PRICES_PATH = Path(__file__).parent / "prices" / "prices.json"
console = Console(width=110)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def copper_to_str(copper: int) -> str:
    """Format a copper amount as a compact gold/silver/copper string."""
    if copper <= 0:
        return "[dim]—[/dim]"
    g = copper // 10000
    s = (copper % 10000) // 100
    c = copper % 100
    parts = []
    if g:
        parts.append(f"[#FFD700]{g}g[/#FFD700]")
    if s:
        parts.append(f"[white]{s}s[/white]")
    if c:
        parts.append(f"[#B87333]{c}c[/#B87333]")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def load_prices() -> tuple[dict, dict | None]:
    """
    Load prices.json if available.
    Returns (price_lookup, metadata) where price_lookup is {item_id: copper}
    and metadata contains source/timing info. Both are None if file is absent.
    """
    if not PRICES_PATH.exists():
        return {}, None

    data = json.loads(PRICES_PATH.read_text())
    meta = {
        "source":     data.get("source", ""),
        "market":     data.get("market", ""),
        "scan_at":    data.get("scan_at", ""),
        "fetched_at": data.get("fetched_at", ""),
    }
    prices = {
        item_id: entry["price"]
        for item_id, entry in data.get("prices", {}).items()
    }
    return prices, meta


def analyze(data: dict, max_skill: int, prices: dict) -> list[dict]:
    """
    Return a list of profitable recipe analyses.

    A recipe is included when:
      - learned_at <= max_skill.
      - No inherent cooldown.
      - The created item has a non-zero vendor sell price.
      - Exactly one reagent has buy_price == 0 (player-sourced).
      - The break-even budget (vendor income minus vendor mat cost) is > 0.

    When prices contains AH data, estimated_profit is populated and the
    results are sorted by that; otherwise sorted by break-even budget.
    """
    items = data["items"]

    # Build lookup: created item_id → recipe, for resolving craftable intermediates
    item_to_recipe: dict[int, dict] = {}
    for r in data["recipes"]:
        creates_id = r.get("creates", {}).get("item_id")
        if creates_id is not None:
            item_to_recipe[creates_id] = r

    results = []

    for recipe in data["recipes"]:
        if recipe.get("learned_at", 0) > max_skill:
            continue
        if recipe.get("cooldown_seconds") is not None:
            continue
        creates = recipe["creates"]
        item_id = str(creates["item_id"])
        min_qty  = creates["min_qty"]

        item = items.get(item_id)
        if not item:
            continue

        vendor_income = item["sell_price"] * min_qty
        if vendor_income == 0:
            continue

        vendor_mats = []
        player_mats = []

        for r in recipe["reagents"]:
            mat_id    = str(r["item_id"])
            mat_item  = items.get(mat_id, {})
            qty       = r["quantity"]
            mat_name  = r.get("name") or mat_item.get("name", f"#{mat_id}")
            buy_price = int(mat_item.get("buy_price") or 0)
            orgrimmar_vendor = mat_item.get("orgrimmar_vendor", False)

            if buy_price > 0 and orgrimmar_vendor:
                vendor_mats.append({
                    "name":       mat_name,
                    "quantity":   qty,
                    "buy_price":  buy_price,
                    "total_cost": buy_price * qty,
                })
            else:
                player_mats.append({
                    "item_id":  mat_id,
                    "name":     mat_name,
                    "quantity": qty,
                })

        # Only deterministic case: exactly one player-sourced material
        if len(player_mats) != 1:
            continue

        # Resolve craftable intermediates (e.g. Bolt of Cloth → raw cloth).
        player_mat = player_mats[0]
        intermediate = item_to_recipe.get(int(player_mat["item_id"]))
        if intermediate:
            int_reagents = intermediate["reagents"]
            int_vendor = [
                r for r in int_reagents
                if items.get(str(r["item_id"]), {}).get("buy_price", 0) > 0
                and items.get(str(r["item_id"]), {}).get("orgrimmar_vendor", False)
            ]
            int_player = [
                r for r in int_reagents
                if not (
                    items.get(str(r["item_id"]), {}).get("buy_price", 0) > 0
                    and items.get(str(r["item_id"]), {}).get("orgrimmar_vendor", False)
                )
            ]
            if len(int_player) == 1:
                raw = int_player[0]
                raw_item = items.get(str(raw["item_id"]), {})
                for vm in int_vendor:
                    vm_item = items.get(str(vm["item_id"]), {})
                    vendor_mats.append({
                        "name":       vm.get("name") or vm_item.get("name", f"#{vm['item_id']}"),
                        "quantity":   vm["quantity"] * player_mat["quantity"],
                        "buy_price":  vm_item.get("buy_price", 0),
                        "total_cost": vm_item.get("buy_price", 0) * vm["quantity"] * player_mat["quantity"],
                    })
                player_mat = {
                    "item_id":  str(raw["item_id"]),
                    "name":     raw.get("name") or raw_item.get("name", f"#{raw['item_id']}"),
                    "quantity": raw["quantity"] * player_mat["quantity"],
                }

        vendor_mat_cost  = sum(m["total_cost"] for m in vendor_mats)
        breakeven_budget = vendor_income - vendor_mat_cost

        if breakeven_budget <= 0:
            continue

        breakeven_per_unit = breakeven_budget / player_mat["quantity"]

        # Calculate estimated profit using AH price if available
        ah_price = prices.get(player_mat["item_id"])
        if ah_price is not None:
            player_mat_cost  = ah_price * player_mat["quantity"]
            estimated_profit = vendor_income - vendor_mat_cost - player_mat_cost
        else:
            estimated_profit = None

        results.append({
            "recipe_name":        recipe["name"],
            "vendor_income":      vendor_income,
            "vendor_mats":        vendor_mats,
            "vendor_mat_cost":    vendor_mat_cost,
            "player_mat":         player_mat,
            "ah_price":           ah_price,
            "estimated_profit":   estimated_profit,
            "breakeven_budget":   breakeven_budget,
            "breakeven_per_unit": int(breakeven_per_unit),
        })

    has_prices = any(r["estimated_profit"] is not None for r in results)
    if has_prices:
        results.sort(key=lambda r: r["estimated_profit"] if r["estimated_profit"] is not None else float("-inf"), reverse=True)
    else:
        results.sort(key=lambda r: r["breakeven_budget"], reverse=True)

    return results


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def display(results: list[dict], profession: str, prices_meta: dict | None) -> None:
    if not results:
        console.print(
            f"[yellow]No profitable one-player-material recipes found for "
            f"[bold]{profession}[/bold].[/yellow]"
        )
        return

    has_prices = any(r["estimated_profit"] is not None for r in results)

    table = Table(
        title=f"Profitable Vendor Crafts — {profession.title()}",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold cyan",
    )

    table.add_column("Recipe",      style="bold white", no_wrap=True)
    table.add_column("Sell",        justify="right",    no_wrap=True)
    table.add_column("Fixed",       justify="right",    no_wrap=True)
    table.add_column("Player Mat",  style="yellow",     no_wrap=True)
    table.add_column("Max",         justify="right",    no_wrap=True, style="bold yellow")
    if has_prices:
        table.add_column("AH",      justify="right",    no_wrap=True)
        table.add_column("Profit",  justify="right",    no_wrap=True, style="bold green")

    for r in results:
        if r["vendor_mats"]:
            mat_lines = "\n".join(
                f"{m['quantity']}× {m['name']} ({copper_to_str(m['buy_price'])} ea)"
                for m in r["vendor_mats"]
            )
            mat_cost_str = copper_to_str(r["vendor_mat_cost"])
        else:
            mat_lines    = "—"
            mat_cost_str = "—"

        pm = r["player_mat"]
        player_mat_str = f"{pm['quantity']}× {pm['name']}"

        row = [
            r["recipe_name"],
            copper_to_str(r["vendor_income"]),
            mat_cost_str,
            player_mat_str,
            copper_to_str(r["breakeven_per_unit"]),
        ]

        if has_prices:
            ah_price = r["ah_price"]
            profit   = r["estimated_profit"]
            row.append(copper_to_str(ah_price) if ah_price is not None else "[dim]?[/dim]")
            if profit is None:
                row.append("[dim]?[/dim]")
            elif profit > 0:
                row.append(copper_to_str(profit))
            else:
                row.append(f"[red]{copper_to_str(abs(profit))} loss[/red]")

        table.add_row(*row)

    console.print(table)

    if has_prices:
        footer = (
            f"\n[dim]{len(results)} recipes shown. "
            "Sorted by estimated profit. "
            "\n'Sell' = item vendor price."
            "\n'Fixed' = cost of vendor-sourced materials."
            "\n'Max' = break-even price per unit of the player-sourced material. "
            "\n'AH' = current AH min buyout. "
            "\n'Profit' = vendor income − all material costs at AH price.[/dim]"
        )
    else:
        footer = (
            f"\n[dim]{len(results)} recipes shown. "
            "'Break-even / unit' = max price per unit of the player-sourced material "
            "at which the craft remains profitable when sold to a vendor.[/dim]"
        )
    console.print(footer)

    if prices_meta:
        console.print(
            f"\n[dim]Prices: {escape(prices_meta['source'])} | Market: {escape(prices_meta['market'])} | "
            f"Scan: {escape(prices_meta['scan_at'] or 'unknown')} | "
            f"Fetched: {escape(prices_meta['fetched_at'] or 'unknown')}[/dim]",
            highlight=False,
        )
    else:
        console.print(
            "[yellow]No prices.json found — run [bold]python update-prices.py[/bold] "
            "to fetch AH prices for profit calculations.[/yellow]"
        )


# ---------------------------------------------------------------------------
# All-professions display
# ---------------------------------------------------------------------------

def display_all(rows: list[dict], prices_meta: dict) -> None:
    """Display profitable recipes across all professions in a single table."""
    if not rows:
        console.print("[yellow]No recipes with positive estimated profit found across any profession.[/yellow]")
        return

    table = Table(
        title="Profitable Vendor Crafts — All Professions",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold cyan",
    )

    table.add_column("Profession",  style="cyan",       no_wrap=True)
    table.add_column("Recipe",      style="bold white", no_wrap=True)
    table.add_column("Sell",        justify="right",    no_wrap=True)
    table.add_column("Fixed",       justify="right",    no_wrap=True)
    table.add_column("Player Mat",  style="yellow",     no_wrap=True)
    table.add_column("Max",         justify="right",    no_wrap=True, style="bold yellow")
    table.add_column("AH",          justify="right",    no_wrap=True)
    table.add_column("Profit",      justify="right",    no_wrap=True, style="bold green")

    for r in rows:
        pm = r["player_mat"]
        if r["vendor_mats"]:
            mat_cost_str = copper_to_str(r["vendor_mat_cost"])
        else:
            mat_cost_str = "—"

        table.add_row(
            r["profession"],
            r["recipe_name"],
            copper_to_str(r["vendor_income"]),
            mat_cost_str,
            f"{pm['quantity']}× {pm['name']}",
            copper_to_str(r["breakeven_per_unit"]),
            copper_to_str(r["ah_price"]) if r["ah_price"] else "[dim]?[/dim]",
            copper_to_str(r["estimated_profit"]),
        )

    console.print(table)
    console.print(
        f"\n[dim]{len(rows)} recipes shown across all professions. Sorted by estimated profit."
        "\nOnly recipes with confirmed positive profit displayed.[/dim]"
    )
    console.print(
        f"[dim]Prices: {escape(prices_meta['source'])} | Market: {escape(prices_meta['market'])}"
        f"\nScan: {escape(prices_meta['scan_at'] or 'unknown')}"
        f"\nFetched: {escape(prices_meta['fetched_at'] or 'unknown')}[/dim]",
        highlight=False,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        console.print("[red]Usage: python profit-craft.py <profession_name|all> [skill_level][/red]")
        sys.exit(1)

    profession = sys.argv[1].lower()

    max_skill = 300
    if len(sys.argv) >= 3:
        try:
            max_skill = int(sys.argv[2])
        except ValueError:
            console.print("[red]skill_level must be an integer.[/red]")
            sys.exit(1)

    prices, prices_meta = load_prices()

    if profession == "all":
        if not prices_meta:
            console.print(
                "[yellow]'all' mode requires AH prices — run "
                "[bold]python update-prices.py[/bold] first.[/yellow]"
            )
            sys.exit(1)

        all_rows = []
        for path in sorted(DATA_DIR.glob("*.json")):
            if "recipes" not in json.loads(path.read_text()):
                continue
            prof_name = path.stem
            data = json.loads(path.read_text())
            results = analyze(data, max_skill, prices)
            for r in results:
                if r["estimated_profit"] is not None and r["estimated_profit"] > 0:
                    all_rows.append({**r, "profession": prof_name})

        all_rows.sort(key=lambda r: r["estimated_profit"], reverse=True)
        display_all(all_rows, prices_meta)
        return

    data_path = DATA_DIR / f"{profession}.json"

    if not data_path.exists():
        console.print(
            f"[red]No data found for [bold]{profession}[/bold]. "
            f"Run [bold]scraper.py {profession}[/bold] first.[/red]"
        )
        sys.exit(1)

    data    = json.loads(data_path.read_text())
    results = analyze(data, max_skill, prices)
    display(results, profession, prices_meta)


if __name__ == "__main__":
    main()
