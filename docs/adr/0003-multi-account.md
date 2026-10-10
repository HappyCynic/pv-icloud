# ADR-0003: Multiple iCloud accounts, one per bearer token

- **Status:** Accepted
- **Date:** 2026-10-08
- **Context:** `pv-icloud` must serve more than one person. Each caller authenticates to the MCP server with a bearer token; that token must map to exactly one iCloud account, and a caller must never see another account's mail or calendars. The first design assumed a single account (`ICLOUD_USER`, `ICLOUD_APP_PASSWORD`, `PV_ICLOUD_BEARER`).

## Decision

1. **Token → account, many-to-one.** Each account has one or more bearer tokens (e.g., `laptop`, `tailscale`, `portal`). Every token belongs to exactly one account. The auth middleware resolves the token to an `account_id` before any tool runs; tools receive an `AccountContext` and have no way to name a different account.
2. **Tokens are stored as SHA-256 hashes** in `config/pv-icloud.yaml`. The plaintext lives only with the client (and the owner's password manager). Lookup is hash-the-presented-token, then constant-time compare against each candidate. Config is therefore not secret. *(Amended 2026-10-08: not secret, but still per deployment — account ids, token hashes and per-person folder excludes describe one installation. Only the template `config/pv-icloud.example.yaml` is committed; the real `config/pv-icloud.yaml` is gitignored, copied from the template by the dev container, and provided per server (e.g. by configuration management).)*
3. **iCloud credentials come from the environment by convention:** `PV_ICLOUD_<ID>_ICLOUD_USER`, `PV_ICLOUD_<ID>_APP_PASSWORD`, where `<ID>` is the upper-cased account id. An account whose credentials are missing fails startup (loud, not silent).
4. **Isolation per account, not per row:**
   - One SQLite index file per account: `$PV_ICLOUD_DATA_DIR/<account_id>/mail.sqlite3`. Deleting an account is deleting a directory; a query bug cannot join across accounts.
   - One IMAP connection pool per account. iCloud's concurrent-session limit is per Apple ID, so `max_connections` is per account. Budget within it: 1 connection for the sync worker (IDLE on INBOX, then CONDSTORE/QRESYNC passes), the rest for live `mail_get`/`mail_thread`.
   - One sync worker task per account, all in the same process as the MCP server (one container). A failing account (bad password, revoked app password) backs off and reports in `/healthz` detail; it does not affect other accounts.
   - One CalDAV client per account. *(Amended 2026-10-09: and one CardDAV client, with that account's contacts cached in memory only.)*
5. **Opaque mail ids stay `folder|UIDVALIDITY|UID`**, base64. They are only ever resolved against the caller's own account, so an id from account A presented by account B simply fails to resolve (or resolves to an unrelated message B already owns). No account id is embedded.
6. **Folder scope is per account and default-include.** Folder names differ between accounts, so a global allowlist does not work. Each account has `folders.include` (default `"*"` = every visible folder) and `folders.exclude` (default empty). A system denylist (`Junk`, `Deleted Messages`, `Drafts`) applies by default. *(Amended 2026-10-09: each is now configurable per account via `mail.special_folders` — `junk`, `deleted_messages`, `drafts`, default `false` = hidden.)* Excluding a folder later purges its rows from that account's index on the next sync.
7. **Audit lines carry `account_id` and `token_label`**, never the token or its hash.
8. **Provisioning is static** (edit config, restart). No self-service sign-up or admin tools in the MCP surface.

## Config shape

```yaml
defaults:                      # applied to every account unless overridden
  mail:
    folders: { include: "*", exclude: [] }
    max_age_days: 730
    max_connections: 2
  calendar:
    calendars: { include: "*", exclude: [] }
    max_range_days: 90

accounts:
  - id: me                     # [a-z0-9_]+; drives env var names and data dir
    tokens:
      - { label: laptop,    sha256: "<hex>" }
      - { label: tailscale, sha256: "<hex>" }
    mail:
      folders: { exclude: [] } # e.g. [Banking, Taxes] to opt out later
```

## Consequences
- Memory and IMAP load scale linearly with accounts (one IDLE connection and one SQLite file each). Fine for a household; revisit past ~10 accounts.
- Rotating a token is a config edit; rotating an app password is an env change. Both need a restart (no hot reload for now).
- A gateway or MCP portal in front of the server must forward a per-user bearer, not a single shared upstream token, or every portal user would map to one account.
