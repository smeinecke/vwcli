from __future__ import annotations

import argparse
import atexit
import sys
from typing import Callable

from .client import Client
from .exceptions import VwcliError


KNOWN_COMMANDS = frozenset(
    {
        "help",
        "login",
        "create",
        "update",
        "clone",
        "search",
        "delete",
        "move",
        "attachment",
        "collections",
        "cache-collections",
    }
)


def build_parser() -> argparse.ArgumentParser:
    def add_update_options(
        p: argparse.ArgumentParser, *, include_clone_flag: bool, force_clone: bool
    ) -> None:
        sel = p.add_mutually_exclusive_group(required=True)
        sel.add_argument("--id", default="", metavar="VALUE", help="Exact item ID")
        sel.add_argument(
            "--search",
            default="",
            metavar="VALUE",
            help="Search string (must resolve to exactly one item)",
        )
        p.add_argument("--name", default="", metavar="VALUE")
        p.add_argument("--username", default="", metavar="VALUE")
        pwd_group = p.add_mutually_exclusive_group()
        pwd_group.add_argument("--password", default="", metavar="VALUE")
        pwd_group.add_argument(
            "--generate-password",
            dest="generate_password",
            action="store_true",
            help="Auto-generate a secure password (printed to stdout after update)",
        )
        p.add_argument("--notes", default="", metavar="VALUE")
        p.add_argument(
            "--organization-id", dest="organization_id", default="", metavar="VALUE"
        )
        p.add_argument(
            "--uri",
            dest="uris",
            action="append",
            default=[],
            metavar="URL[::MATCH]",
            help="Replace all URIs; append ::MATCH_TYPE (default, base_domain, host, starts_with, exact, regexp, never)",
        )
        p.add_argument(
            "--add-uri",
            dest="add_uris",
            action="append",
            default=[],
            metavar="URL[::MATCH]",
            help="Add a URI to the existing list (supports ::MATCH_TYPE suffix)",
        )
        p.add_argument(
            "--remove-uri",
            dest="remove_uris",
            action="append",
            default=[],
            metavar="URL",
            help="Remove a URI from the existing list by exact URL",
        )
        p.add_argument(
            "--collection",
            default="",
            metavar="VALUE",
            help="Collection name (resolved via cache; must be unique)",
        )
        p.add_argument(
            "--vault",
            default="",
            metavar="NAME",
            help="Vault (organization name) to place the item in; resolves to organizationId",
        )
        if include_clone_flag:
            p.add_argument(
                "--clone",
                action="store_true",
                help="Clone selected item first, then update clone",
            )
        p.add_argument(
            "--refresh-cache",
            dest="refresh_cache",
            action="store_true",
            help="Refresh collection cache before resolving collection",
        )
        p.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            help="Print JSON that would be sent, do not modify vault",
        )
        p.add_argument(
            "--to-ansible-vault",
            dest="to_ansible_vault",
            action="store_true",
            help="Encrypt resulting password via ansible-vault encrypt-string",
        )
        p.set_defaults(clone=force_clone)

    parser = argparse.ArgumentParser(
        prog="vwcli",
        description="Bitwarden/Vaultwarden CLI wrapper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("help", help="Show this help")
    sub.add_parser(
        "login", help="Unlock vault and cache BW_SESSION to ~/.config/vwcli/config"
    )

    p_create = sub.add_parser("create", help="Create a new login item")
    p_create.add_argument("--name", required=True, metavar="VALUE")
    p_create.add_argument("--username", default="", metavar="VALUE")
    pwd_group = p_create.add_mutually_exclusive_group()
    pwd_group.add_argument("--password", default="", metavar="VALUE")
    pwd_group.add_argument(
        "--generate-password",
        dest="generate_password",
        action="store_true",
        help="Auto-generate a secure password (printed to stdout after creation)",
    )
    p_create.add_argument("--notes", default=None, metavar="VALUE")
    p_create.add_argument(
        "--organization-id", dest="organization_id", default="", metavar="VALUE"
    )
    p_create.add_argument(
        "--uri",
        dest="uris",
        action="append",
        default=[],
        metavar="URL[::MATCH]",
        help="URI for autofill; append ::MATCH_TYPE to set match (default, base_domain, host, starts_with, exact, regexp, never)",
    )
    p_create.add_argument(
        "--collection",
        default="",
        metavar="VALUE",
        help="Collection name (resolved via cache; must be unique)",
    )
    p_create.add_argument(
        "--vault",
        default="",
        metavar="NAME",
        help="Vault (organization name) to create the item in; resolves to organizationId",
    )
    p_create.add_argument(
        "--refresh-cache",
        dest="refresh_cache",
        action="store_true",
        help="Refresh collection cache before resolving collection",
    )
    p_create.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Print JSON that would be sent, do not modify vault",
    )

    p_update = sub.add_parser(
        "update", help="Update an existing item, or clone and update the clone"
    )
    add_update_options(p_update, include_clone_flag=True, force_clone=False)

    p_clone = sub.add_parser("clone", help="Alias for: update --clone")
    add_update_options(p_clone, include_clone_flag=False, force_clone=True)

    p_search = sub.add_parser("search", help="Search items")
    p_search.add_argument("query", nargs="*", metavar="QUERY")
    p_search.add_argument(
        "--uri",
        dest="uri_filter",
        default="",
        metavar="URL",
        help="Filter results to items whose URI list contains this URL (substring match)",
    )
    p_search.add_argument(
        "--json", dest="output_json", action="store_true", help="Print raw JSON"
    )
    p_search.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="Limit text output rows (default: 20)",
    )
    p_search.add_argument(
        "--to-ansible-vault",
        dest="to_ansible_vault",
        action="store_true",
        help="Encrypt first matched item password via ansible-vault encrypt-string",
    )

    p_delete = sub.add_parser("delete", help="Delete an item (moves to trash)")
    del_sel = p_delete.add_mutually_exclusive_group(required=True)
    del_sel.add_argument("--id", default="", metavar="VALUE", help="Exact item ID")
    del_sel.add_argument(
        "--search",
        default="",
        metavar="VALUE",
        help="Search string (must resolve to exactly one item)",
    )
    p_delete.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )

    p_move = sub.add_parser(
        "move", help="Bulk move items from one collection to another"
    )
    p_move.add_argument(
        "--from",
        dest="from_collection",
        required=True,
        metavar="COLLECTION",
        help="Source collection name",
    )
    p_move.add_argument(
        "--to",
        dest="to_collection",
        required=True,
        metavar="COLLECTION",
        help="Target collection name",
    )
    p_move.add_argument(
        "--search",
        default="",
        metavar="VALUE",
        help="Filter items by name (substring match); omit to move all items",
    )
    p_move.add_argument(
        "--refresh-cache",
        dest="refresh_cache",
        action="store_true",
        help="Refresh collection cache before resolving collections",
    )
    p_move.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="List items that would be moved without making changes",
    )
    p_move.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )

    p_col = sub.add_parser("collections", help="Manage collections")
    col_sub = p_col.add_subparsers(dest="collections_command")

    col_sub.add_parser(
        "cache",
        help="Refresh local collection cache (~/.cache/vwcli/collections.json)",
    )

    p_col_list = col_sub.add_parser("list", help="List collections")
    p_col_list.add_argument(
        "--refresh-cache",
        dest="refresh_cache",
        action="store_true",
        help="Refresh cache before listing",
    )

    p_col_add = col_sub.add_parser("add", help="Create a new collection")
    p_col_add.add_argument(
        "--name", required=True, metavar="VALUE", help="Collection name"
    )
    p_col_add.add_argument(
        "--organization-id",
        dest="organization_id",
        default="",
        metavar="VALUE",
        help="Organization ID (defaults to parent's organization if --parent-id is given)",
    )
    p_col_add.add_argument(
        "--parent-id",
        dest="parent_id",
        default="",
        metavar="VALUE",
        help="Parent collection ID for nesting",
    )

    p_col_search = col_sub.add_parser(
        "search", help="Search collections in local cache"
    )
    p_col_search.add_argument(
        "query", nargs="+", metavar="QUERY", help="Substring to match against collection full paths"
    )

    p_col_upd = col_sub.add_parser(
        "update", help="Update a collection (rename or change parent)"
    )
    p_col_upd.add_argument(
        "--id", required=True, metavar="VALUE", help="Collection ID"
    )
    p_col_upd.add_argument(
        "--name", required=True, metavar="VALUE", help="New collection name"
    )
    p_col_upd.add_argument(
        "--parent-id",
        dest="parent_id",
        default="",
        metavar="VALUE",
        help="Parent collection ID for nesting",
    )

    p_col_del = col_sub.add_parser("delete", help="Delete a collection")
    p_col_del.add_argument(
        "--id", required=True, metavar="VALUE", help="Collection ID"
    )
    p_col_del.add_argument(
        "--organization-id",
        dest="organization_id",
        default="",
        metavar="VALUE",
        help="Organization ID (required for some backends)",
    )
    p_col_del.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )

    p_col_move = col_sub.add_parser(
        "move", help="Move a collection under a different parent"
    )
    p_col_move.add_argument(
        "--id", required=True, metavar="VALUE", help="Collection ID"
    )
    p_col_move.add_argument(
        "--to-parent-id",
        dest="to_parent_id",
        required=True,
        metavar="VALUE",
        help="Target parent collection ID",
    )

    p_att = sub.add_parser("attachment", help="Manage item attachments")
    att_top_sel = p_att.add_mutually_exclusive_group()
    att_top_sel.add_argument(
        "--id",
        default="",
        metavar="VALUE",
        help="Item ID (default: list attachments)",
    )
    att_top_sel.add_argument(
        "--search",
        default="",
        metavar="VALUE",
        help="Search string (default: list attachments)",
    )
    p_att.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Print raw JSON (default: list attachments)",
    )
    att_sub = p_att.add_subparsers(dest="attachment_command")

    p_att_show = att_sub.add_parser(
        "list", help="List attachments for an item (default)"
    )
    att_show_sel = p_att_show.add_mutually_exclusive_group(required=True)
    att_show_sel.add_argument("--id", default="", metavar="VALUE", help="Item ID")
    att_show_sel.add_argument(
        "--search",
        default="",
        metavar="VALUE",
        help="Search string (must resolve to one item)",
    )
    p_att_show.add_argument(
        "--json", dest="output_json", action="store_true", help="Print raw JSON"
    )

    p_att_add = att_sub.add_parser("add", help="Upload a file as an attachment")
    att_add_sel = p_att_add.add_mutually_exclusive_group(required=True)
    att_add_sel.add_argument("--id", default="", metavar="VALUE", help="Item ID")
    att_add_sel.add_argument(
        "--search",
        default="",
        metavar="VALUE",
        help="Search string (must resolve to one item)",
    )
    p_att_add.add_argument(
        "--file", required=True, metavar="PATH", help="Path to file to upload"
    )

    p_att_del = att_sub.add_parser(
        "delete", help="Delete an attachment from an item"
    )
    att_del_sel = p_att_del.add_mutually_exclusive_group(required=True)
    att_del_sel.add_argument("--id", default="", metavar="VALUE", help="Item ID")
    att_del_sel.add_argument(
        "--search",
        default="",
        metavar="VALUE",
        help="Search string (must resolve to one item)",
    )
    p_att_del.add_argument(
        "--attachment-id",
        dest="attachment_id",
        required=True,
        metavar="VALUE",
        help="Attachment ID (from attachment list)",
    )

    return parser

def run(client: Client, argv: list[str]) -> int:
    args = argv[1:]
    if args and not args[0].startswith("-") and args[0] not in KNOWN_COMMANDS:
        args = ["search", *args]
    if args and args[0] == "cache-collections":
        print(
            "[warn] 'cache-collections' is deprecated; use 'collections cache' instead.",
            file=sys.stderr,
        )
        args = ["collections", "cache", *args[1:]]

    parser = build_parser()
    ns = parser.parse_args(args)

    if not ns.command or ns.command == "help":
        parser.print_help()
        return 0

    client.need_bw()

    dispatch: dict[str, tuple[Callable[[argparse.Namespace], None], bool]] = {
        "login": (client.cmd_login, False),
        "create": (client.cmd_create, True),
        "update": (client.cmd_update, True),
        "clone": (client.cmd_update, True),
        "search": (client.cmd_search, True),
        "delete": (client.cmd_delete, False),
        "move": (client.cmd_move, True),
        "attachment": (client.cmd_attachment, False),
        "collections": (client.cmd_collections, True),
        "cache-collections": (client.cmd_cache_collections, False),
    }

    cmd_func, use_serve = dispatch[ns.command]
    if use_serve:
        client.with_bw_serve(cmd_func, ns)
    else:
        cmd_func(ns)
    return 0



def main() -> int:
    client = Client()
    atexit.register(client.stop_bw_serve)
    try:
        return run(client, sys.argv)
    except VwcliError as exc:
        if str(exc):
            print(str(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
