# Quickstart

Get from zero to a working vwcli setup. Assumes a Vaultwarden (or Bitwarden)
account you can log into.

## 1. Install the Bitwarden CLI

vwcli drives the official `bw` binary. Either put `bw` in `PATH`, or install it
under nvm - vwcli will find it via `nvm exec` automatically.

```bash
# Option A: nvm (recommended, matches bitwarden-serve/ scripts)
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
# reload shell, then:
nvm install --lts
nvm alias default 'lts/*'
npm install -g @bitwarden/cli

# Option B: any other install method, as long as `bw` is in PATH
bw --version
```

## 2. Point bw at your server

```bash
# Vaultwarden / self-hosted:
bw config server https://vault.example.com

# bitwarden.com: nothing to do (default)
```

## 3. Install vwcli

```bash
cd vwcli
uv tool install .          # or: uv sync && uv run vwcli help
vwcli help
```

## 4. Log in

```bash
vwcli login
```

This runs `bw login` if needed, then `bw unlock --raw`, and caches the session
token in `~/.config/vwcli/config` (mode `0600`) together with an expiry
timestamp. If the `bitwarden-cli.service` systemd unit is installed (see
[docs/bitwarden-serve.md](docs/bitwarden-serve.md)) the new token is also
written to its env file and the service is restarted.

Tip: set `BW_PASSWORD` in the environment to unlock non-interactively
(`bw unlock --passwordenv BW_PASSWORD`).

## 5. Build the collection cache

Only needed if you use organization collections (names instead of IDs):

```bash
vwcli collections cache
vwcli collections list
```

This writes `~/.cache/vwcli/collections.json` with full `Parent/Child` paths.
Re-run `vwcli collections cache` (or pass `--refresh-cache` to commands that
support it) after changing collections on the server.

## 6. Everyday commands

```bash
# Search - bare words are treated as `vwcli search`:
vwcli github
vwcli search github --limit 5
vwcli search --uri example.com        # filter by stored URI (substring)
vwcli search github --json            # raw item JSON

# Create:
vwcli create --name example.com --username me@example.com \
    --generate-password \
    --uri https://example.com::base_domain \
    --uri https://login.example.com::exact

# Create into an org vault + collection:
vwcli create --name db-prod --username admin --generate-password \
    --vault "My Org" --collection "Infra/Databases"

# Update (selector is --id or --search resolving to exactly one item):
vwcli update --search example.com --generate-password
vwcli update --id <uuid> --add-uri https://app.example.com::host
vwcli update --search example.com --remove-uri https://old.example.com

# Clone then edit the copy (original untouched):
vwcli clone --search example.com --name example-staging \
    --uri https://staging.example.com::base_domain

# Delete (moves to trash, asks for confirmation):
vwcli delete --search example.com
vwcli delete --id <uuid> --yes
```

## 7. Preview before writing

`create`, `update`/`clone` and `move` accept `--dry-run`, which prints the JSON
that would be sent (or the items that would be moved) without touching the
vault:

```bash
vwcli update --search example.com --generate-password --dry-run
vwcli move --from "Infra/Old" --to "Infra/New" --dry-run
```

## 8. Optional: persistent bw serve + backups

For a always-on local REST daemon (Unix socket, hardened systemd unit) and
scheduled encrypted exports, install the user service:

```bash
cd bitwarden-serve
./install-bitwarden-cli-user-service.sh                 # daemon only
./install-bitwarden-cli-user-service.sh --install-backup # + export timer
```

Details: [docs/bitwarden-serve.md](docs/bitwarden-serve.md).

## Next steps

- Full flag reference: [docs/commands.md](docs/commands.md)
- Config files, env vars, session renewal: [docs/configuration.md](docs/configuration.md)
- How vwcli talks to `bw`/`bw serve`: [docs/architecture.md](docs/architecture.md)
