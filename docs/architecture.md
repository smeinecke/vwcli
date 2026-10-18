# Architecture

vwcli is a thin, dependency-light Python wrapper (`rich` is the only runtime
dependency) around the official Bitwarden CLI (`bw`). It prefers the
long-running REST API exposed by `bw serve` and falls back to `bw` subprocess
calls whenever the daemon is unavailable or an endpoint is missing.

## Layout

```
src/vwcli/
├── cli.py         # argparse definition + command dispatch
├── client.py      # Client: bw/bw serve transport + all command handlers
├── config.py      # ~/.config/vwcli/config handling, secure file creation
├── constants.py   # paths, URI match table, env-tunable knobs
├── output.py      # Rich tables / TSV / JSON rendering
└── exceptions.py  # VwcliError
```

`cli.py` only parses arguments and dispatches to `Client.cmd_*` methods.
Commands flagged `use_serve` run inside `Client.with_bw_serve`, which ensures
a `bw serve` endpoint exists for the duration of the call:

- serve-backed: `create`, `update`, `clone`, `search`, `move`, `collections`
- direct `bw` CLI: `login`, `delete`, `attachment`

## Transport: bw serve first, CLI fallback

Every `bw_*` method in `client.py` follows the same pattern:

1. If `self.bw_serve_url` is set, try the REST endpoint(s) via
   `bw_serve_request_json` / `bw_serve_try_json`.
2. On failure print a `[warn]` to stderr and fall back to the equivalent
   `bw` subprocess (`bw_run`, `bw_run_interactive`, JSON-decoded helpers).

`bw_serve_try_json` can try several `(method, path)` candidates in order -
used where `bw serve` routes differ between CLI versions (e.g. `PUT` vs
`POST` on `/object/item/<id>`). Operations with `has_cli_fallback=False`
(collection create/update/delete) fail hard instead of downgrading, because
the plain `bw` CLI cannot manage collections.

Requests support both transports transparently:

- `http://host:port` - plain HTTP via `urllib`
- `unix:///path.sock` - HTTP over a Unix domain socket via
  `_UnixSocketHTTPConnection` (http.client with a custom `connect()`)

## Serve endpoint resolution

`start_bw_serve` picks an endpoint in order (details in
[configuration.md](configuration.md#how-bw-serve-is-selected)):

1. configured `BW_SERVE_URL` that answers `/status`
2. the `bitwarden-cli.service` systemd user unit over its Unix socket
3. a self-spawned `bw serve` on a free `127.0.0.1` port, SIGTERMed via
   `atexit`/`stop_bw_serve`

A managed (self-spawned) daemon gets its log in a temp file that is cleaned
up on exit. After every mutating command vwcli POSTs `/sync`
(`bw_serve_sync`) so the long-running daemon does not serve stale data -
cheap and idempotent.

## Stale-cipher retry

`bw` sometimes answers writes with "The client copy of this cipher is out of
date". `bw_run` detects that exact string, runs `bw sync` once and retries
the command before surfacing the error.

## Trash handling

List endpoints are unreliable about trashed items (`deletedDate` is omitted
in serve list responses). Two layers handle this:

- `_exclude_trash` filters any list on `deletedDate`
- `_verify_items` re-fetches each candidate by ID - a `GET /object/item/<id>`
  (or `bw get item`) 404s for trashed entries

Search verifies only the rows it actually displays (after `--limit`), so
large result sets do not cause an N+1 storm.

## Collections

Collection names are only resolvable through the local cache
(`~/.cache/vwcli/collections.json`), which stores full `Parent/Child` paths
computed by `_compute_collection_full_paths`. Resolution requires a unique
exact match on the full path.

- `collections add` with `--parent-id` inherits the parent's organization and
  expands a bare `--name` to `Parent/Name`.
- `collections move` tries a direct `PUT` parent update first; if the backend
  does not support re-parenting, it falls back to create-under-new-parent +
  move all items + delete old, with a rollback pass if an item move fails.
- `move` (items) resolves `--from`/`--to` through the cache and refuses
  cross-organization moves.

## Creating items in an org vault

`POST /ciphers` on Vaultwarden >= 1.37.4 validates `encryptedByKeyId` against
the **user** key id - but `bw` >= 2026.9 encrypts org-bound ciphers with the
org key, so direct org creates are rejected with "Invalid key cipher" (an
upstream regression; the share/move endpoints are exempt). `vwcli create
--vault/--organization-id` and `clone` therefore create the item in the
personal vault first and then share it into the org via
`bw_move_item_to_org` (`bw move` / `POST /move/<id>/<org>`), carrying the
collection assignments along in the same call - which is also how upstream
clients attach items to organizations.

## Clone semantics

`clone` is `update --clone`: the source item is fetched, server-managed keys
(`id`, `revisionDate`, `creationDate`, `deletedDate`, `passwordHistory`,
`attachments`) are stripped, the payload is re-created via `bw_create_item`,
and the requested edits are applied to the new item. Cross-vault clones drop
`folderId` and rebuild `collectionIds` from `--collection`, since those IDs
are vault-scoped.

## ansible-vault integration

`--to-ansible-vault` pipes the resulting password into
`ansible-vault encrypt-string --stdin-name password` (tries the
`encrypt_string` spelling too for older versions) and prints the encrypted
block. On `search` it applies to the first matched item only.

## bitwarden-serve/

Shell/systemd assets for a persistent local `bw serve` daemon: systemd socket
activation on `/run/user/<uid>/bw.sock`, a Node `--require` patch that moves
`bw serve` onto the inherited fd, optional encrypted-export timer, and a
NetworkManager VPN hook. See [bitwarden-serve.md](bitwarden-serve.md).
