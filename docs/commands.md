# Command reference

`vwcli <command> [options]`. If the first argument is not a known command and
does not start with `-`, it is treated as a search query:

```bash
vwcli github        # same as: vwcli search github
vwcli foo bar       # same as: vwcli search foo bar
```

`vwcli help` (or bare `vwcli`) prints the top-level help.

Item selectors: commands that operate on a single existing item take either
`--id <uuid>` (exact) or `--search <string>` (must resolve to exactly one
item; multiple matches are listed and the command aborts). A UUID passed as
`--search` is fetched directly by ID.

---

## login

```
vwcli login
```

Unlocks the vault and caches the session.

- Runs `bw login --check`; if a `bw login` is required it is performed
  interactively.
- Unlocks with `bw unlock --raw` (uses `--passwordenv BW_PASSWORD` when
  `BW_PASSWORD` is set in the environment).
- Writes `BW_SESSION` and `BW_SESSION_EXPIRES` to `~/.config/vwcli/config`.
- If `bitwarden-cli.service` is installed, updates
  `~/.config/systemd/user/bitwarden-cli.env` and restarts the service.
- If a cached session is still valid (`bw login --check` succeeds), it is
  simply re-cached and propagated - no re-login.

## search

```
vwcli search [QUERY ...] [--uri URL] [--json] [--limit N] [--to-ansible-vault]
```

Searches vault items. At least one of `QUERY` or `--uri` is required.

| Flag | Default | Meaning |
|------|---------|---------|
| `QUERY ...` | - | Search terms, joined with spaces |
| `--uri URL` | - | Keep only items whose URI list contains `URL` (substring, case-insensitive) |
| `--json` | off | Print the raw item JSON list |
| `--limit N` | `20` | Max rows in text output |
| `--to-ansible-vault` | off | Encrypt the **first** match's password via `ansible-vault encrypt-string` and print it |

Output: a Rich table on a TTY, tab-separated lines otherwise
(`id`, `vault`, `folder/collection`, `name`, `username`, `password`).
Trashed items are excluded; displayed items are re-fetched individually to
verify they are not in trash.

## create

```
vwcli create --name VALUE [options]
```

Creates a new login item (type `login`).

| Flag | Default | Meaning |
|------|---------|---------|
| `--name VALUE` | (required) | Item name |
| `--username VALUE` | empty | Login username |
| `--password VALUE` | empty | Login password (mutually exclusive with `--generate-password`) |
| `--generate-password` | off | Generate a 24-char password (upper+lower+digits+special); printed after creation |
| `--notes VALUE` | `null` | Secure notes text |
| `--organization-id VALUE` | - | Target organization ID |
| `--vault NAME` | - | Organization **name**; resolved to `organizationId` (must be unique) |
| `--uri URL[::MATCH]` | `[]` | Login URI; repeatable. `MATCH` is a URI match type, default is server default |
| `--collection VALUE` | - | Collection name from the local cache (full path, must be unique); assigns the item after creation, moving it into the org if needed |
| `--refresh-cache` | off | Rebuild the collection cache before resolving `--collection` |
| `--to-ansible-vault` | off | Print the password encrypted via `ansible-vault encrypt-string` |
| `--dry-run` | off | Print the item JSON; do not write to the vault |

### URI match types

Append `::TYPE` to a URI (the `::` separator avoids clashing with `:` inside
URLs):

| Type | Aliases | Bitwarden match |
|------|---------|-----------------|
| `default` | `null` | server default (`null`) |
| `base_domain` | `basedomain` | `0` |
| `host` | | `1` |
| `starts_with` | `startswith` | `2` |
| `exact` | | `3` |
| `regexp` | `regex` | `4` |
| `never` | | `5` |

```bash
vwcli create --name app --username u --generate-password \
    --uri https://app.example.com::base_domain \
    --uri http://localhost:3000::exact
```

## update

```
vwcli update (--id VALUE | --search VALUE) [options]
```

Updates an existing item in place. With `--clone`, clones first and applies
the edits to the new item (see `clone` below).

Selector (required, mutually exclusive): `--id VALUE` or `--search VALUE`.

Field flags: `--name`, `--username`, `--password`/`--generate-password`,
`--notes`, `--organization-id`, `--vault` - same as `create`.

URI flags (all repeatable):

| Flag | Meaning |
|------|---------|
| `--uri URL[::MATCH]` | **Replace** the entire URI list |
| `--add-uri URL[::MATCH]` | Append to the existing URI list |
| `--remove-uri URL` | Remove URIs by exact URL match |

Other flags: `--collection`, `--refresh-cache`, `--clone`, `--dry-run`,
`--to-ansible-vault` (encrypts the resulting password of the updated item).

```bash
vwcli update --search github --generate-password
vwcli update --id <uuid> --name "new name" --notes "rotated 2026-10"
vwcli update --search app --add-uri https://admin.example.com::host
```

## clone

```
vwcli clone (--id VALUE | --search VALUE) [options]
```

Alias for `update --clone`: duplicates the selected item, then applies the
given edits to the copy. The original item is never modified.

Cloning strips server-managed fields (`id`, `revisionDate`, `creationDate`,
`deletedDate`, `passwordHistory`, `attachments`).

When cloning across vaults (`--vault`/`--organization-id` different from the
source item's org), `folderId` is dropped (folders are vault-scoped) and
collection assignments are rebuilt from `--collection`; without
`--collection` the clone lands uncollectioned in the target vault.

```bash
vwcli clone --search prod-db --name prod-db-copy --generate-password
vwcli clone --search myitem --vault "Other Org" --collection "Shared"
```

## delete

```
vwcli delete (--id VALUE | --search VALUE) [--yes]
```

Moves an item to the trash (Bitwarden soft-delete). Prompts for confirmation
unless `--yes`/`-y` is given.

## move

```
vwcli move --from COLLECTION --to COLLECTION [--search VALUE] [--dry-run] [--yes] [--refresh-cache]
```

Bulk-moves items from one collection to another. Both names are resolved
through the local collection cache and must live in the same organization.

| Flag | Meaning |
|------|---------|
| `--from COLLECTION` | Source collection name (required) |
| `--to COLLECTION` | Target collection name (required) |
| `--search VALUE` | Only move items whose name contains this substring |
| `--dry-run` | List matching items; change nothing |
| `--yes`, `-y` | Skip the confirmation prompt |
| `--refresh-cache` | Rebuild the collection cache first |

## attachment

```
vwcli attachment [--id VALUE | --search VALUE] [--json]
vwcli attachment list (--id VALUE | --search VALUE) [--json]
vwcli attachment add (--id VALUE | --search VALUE) --file PATH
vwcli attachment delete (--id VALUE | --search VALUE) --attachment-id VALUE
```

Manage item attachments. Bare `vwcli attachment --id X` is equivalent to
`attachment list`.

- `list` - table (TTY) or TSV of `id`, `fileName`, `size`; `--json` for raw JSON
- `add` - upload `--file` to the item
- `delete` - remove `--attachment-id` (get IDs from `list`)

Attachment operations always go through the `bw` CLI (the `bw serve` API has
no attachment endpoints); vwcli runs `bw sync` first when a serve daemon is
active so the CLI cache is fresh.

## collections

```
vwcli collections <subcommand>
```

Manage organization collections. Names in the cache are full paths like
`Infra/Databases` built from `parentId` chains.

| Subcommand | Arguments | Purpose |
|------------|-----------|---------|
| `cache` | - | Rebuild `~/.cache/vwcli/collections.json` |
| `list` | `[--refresh-cache]` | List all collections (live, not cached) |
| `search` | `QUERY ...` | Substring-match cached collection paths |
| `add` | `--name VALUE [--organization-id ID | --parent-id ID]` | Create a collection. With `--parent-id`, org is inherited from the parent and a bare `--name` becomes `Parent/Name` |
| `update` | `--id ID --name VALUE [--parent-id ID]` | Rename and/or re-parent a collection |
| `delete` | `--id ID [--organization-id ID] [--yes]` | Delete (asks for confirmation; `--organization-id` only needed for backends that do not return it) |
| `move` | `--id ID --to-parent-id ID` | Move under a different parent |

`collections move` first tries a direct parent update. If the backend rejects
it, vwcli falls back to recreate-and-reassign: create the collection under
the new parent, move every item over (with rollback on partial failure),
then delete the old collection.

`cache-collections` still works as a deprecated alias for
`collections cache` (prints a warning).
