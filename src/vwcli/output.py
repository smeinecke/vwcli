from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.table import Table

__all__ = ["Console", "Table", "item_group_label", "vault_name", "render_search_results"]


def item_group_label(item: dict[str, Any], folder_map: dict[str, str], col_map: dict[str, str]) -> str:
    """Return a folder name or a sorted list of collection names for an item."""
    folder = folder_map.get(item.get("folderId") or "", "")
    if folder:
        return folder
    col_ids = item.get("collectionIds") or []
    names = sorted(n for cid in col_ids if (n := col_map.get(cid or "", "")))
    return ", ".join(names) if names else "No Folder"


def vault_name(item: dict[str, Any], org_map: dict[str, str]) -> str:
    """Return the organization/vault name for an item."""
    org_id = str(item.get("organizationId") or "")
    if org_id:
        return org_map.get(org_id) or "Unknown"
    return "Personal"


def render_search_results(
    items: list[dict[str, Any]],
    *,
    output_json: bool,
    org_map: dict[str, str],
    folder_map: dict[str, str],
    col_map: dict[str, str],
    console: Console | None = None,
) -> None:
    """Render search results as JSON, a Rich table, or tab-separated plain text."""
    if output_json:
        print(json.dumps(items, indent=2))
        return

    if sys.stdout.isatty():
        table = Table(title=f"Matches: {len(items)}", show_lines=False, highlight=True)
        table.add_column("ID", style="dim")
        table.add_column("Vault", no_wrap=True)
        table.add_column("Folder / Collection", overflow="fold")
        table.add_column("Name", style="bold", no_wrap=True)
        table.add_column("Username", no_wrap=True, style="green")
        table.add_column("Password", no_wrap=True, style="green")
        for item in items:
            login = item.get("login") or {}
            table.add_row(
                str(item.get("id") or ""),
                vault_name(item, org_map),
                item_group_label(item, folder_map, col_map),
                str(item.get("name") or ""),
                str(login.get("username") or ""),
                str(login.get("password") or ""),
            )
        (console or Console()).print(table)
    else:
        for item in items:
            login = item.get("login") or {}
            print(
                "\t".join([
                    str(item.get("id") or ""),
                    vault_name(item, org_map),
                    item_group_label(item, folder_map, col_map),
                    str(item.get("name") or ""),
                    str(login.get("username") or ""),
                    str(login.get("password") or ""),
                ])
            )
