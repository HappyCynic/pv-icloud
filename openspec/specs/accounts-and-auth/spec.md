# accounts-and-auth Specification

## Purpose
How the server is configured, how a caller's bearer token selects exactly one iCloud account,
how accounts are kept apart, and what the open `/healthz` endpoint reveals.

## Requirements

### Requirement: Configuration file and data directory
The server SHALL read its configuration from the YAML file named by `PV_ICLOUD_CONFIG`
(default `config/pv-icloud.yaml`) and SHALL keep per-account data under the directory named by
`PV_ICLOUD_DATA_DIR` (default `data`). A missing file or any invalid value SHALL stop startup
with a message that starts `pv-icloud: config error:` and names the problem.

#### Scenario: Config file missing
- **WHEN** the server starts and the config file does not exist
- **THEN** it exits with `pv-icloud: config error: config file not found: <path>`

#### Scenario: Data directory from the environment
- **WHEN** `PV_ICLOUD_DATA_DIR` is set to a directory
- **THEN** each account's mail index is created at `<that directory>/<account id>/mail.sqlite3`

### Requirement: Account definitions are validated
The configuration MUST list at least one account under `accounts`. Each account `id` MUST match
`[a-z0-9_]+` and be unique. Each account MUST have at least one entry under `tokens`; each token
MUST have a non-empty `label` and a `sha256` of exactly 64 hexadecimal characters (compared in
lower case), and no two tokens in the whole file MAY share a hash. A token MAY also have:
`mail_write`, a list of actions from `move`, `archive`, `delete` and `draft` (default empty);
`protected_folders`, a list of folder names (default empty); and `max_writes_per_hour`, a
positive integer (default 30). Any other value for these keys, or an unknown action, SHALL
stop startup with an error naming the account, the token label and the key.

#### Scenario: Placeholder hash rejected
- **WHEN** a token's `sha256` is `REPLACE_ME`
- **THEN** loading the config fails with an error saying the hash must be 64 hex characters

#### Scenario: Duplicate account id
- **WHEN** two accounts share the same `id`
- **THEN** loading the config fails with `duplicate account id`

#### Scenario: Hash shared by two tokens
- **WHEN** two tokens, in the same or different accounts, have the same `sha256`
- **THEN** loading the config fails with an error saying the hash is used by another token

#### Scenario: Unknown write action
- **WHEN** a token has `mail_write: [move, expunge]`
- **THEN** loading the config fails with an error naming the token, `mail_write`, `expunge`
  and the allowed actions

#### Scenario: Draft grant
- **WHEN** `PV_ICLOUD_MAIL_WRITE` is `enabled` and a token has `mail_write: [draft]`
- **THEN** it loads with the single write action `draft`

#### Scenario: Send is not yet an action
- **WHEN** a token has `mail_write: [draft, send]`
- **THEN** loading the config fails with an error naming the token, `mail_write`, `send` and
  the allowed actions

#### Scenario: Invalid write limit
- **WHEN** a token has `max_writes_per_hour: 0`
- **THEN** loading the config fails with an error naming the token and `max_writes_per_hour`

#### Scenario: Token without write keys
- **WHEN** a token has only `label` and `sha256`
- **THEN** it loads with no write actions, no extra protected folders and a write limit of 30
  per hour

### Requirement: Defaults merged into each account
The server SHALL apply the `defaults.mail`, `defaults.calendar` and `defaults.contacts` sections
to every account, and SHALL let an account's own `mail`, `calendar` and `contacts` sections
override them key by key, merging nested mappings rather than replacing them.

#### Scenario: Account override on top of defaults
- **WHEN** `defaults.mail.special_folders` turns on `deleted_messages` and an account's
  `mail.special_folders` turns on `junk`
- **THEN** that account shows both `Deleted Messages` and `Junk` and still hides `Drafts`

#### Scenario: Default applies to an account without its own section
- **WHEN** an account has no `mail` section
- **THEN** it uses `defaults.mail` unchanged

### Requirement: Configuration values are validated
The server SHALL reject, with an error naming the account and key, any of: `mail.max_age_days`,
`mail.max_connections`, `calendar.max_range_days` or `calendar.max_search_days` that is not a
positive integer; `mail.max_connections` below 2; `calendar.subscriptions`, `contacts.enabled`
or a `mail.special_folders` value that is not `true` or `false`; a `mail.special_folders` key
other than `junk`, `deleted_messages` or `drafts`; a `calendar.timezone` that is not a known
IANA zone; an `include` that is neither `"*"` nor a list of strings; an `exclude` or
`contacts.exclude_groups` that is not a list of strings. Defaults SHALL be: `max_age_days` 730,
`max_connections` 2, `max_range_days` 90, `max_search_days` 730, `subscriptions` true,
`timezone` unset, `contacts.enabled` true, `exclude_groups` empty, `include` `"*"`, `exclude`
empty.

#### Scenario: Unknown time zone
- **WHEN** `calendar.timezone` is `Mars/Olympus`
- **THEN** loading the config fails with an error mentioning an unknown time zone

#### Scenario: Too few IMAP connections
- **WHEN** `mail.max_connections` is 1
- **THEN** loading the config fails with an error saying it must be at least 2

#### Scenario: Unknown special folder key
- **WHEN** `mail.special_folders` contains the key `spam`
- **THEN** loading the config fails with an error naming the unknown key and the allowed keys

### Requirement: iCloud credentials come from the environment
The server SHALL read each account's Apple ID from `PV_ICLOUD_<ID>_ICLOUD_USER` and its
app-specific password from `PV_ICLOUD_<ID>_APP_PASSWORD`, where `<ID>` is the account id in
upper case, with surrounding whitespace removed. It MUST NOT read iCloud credentials from the
config file. A missing or empty variable SHALL stop startup with an error naming both variables.

#### Scenario: Missing credentials fail loudly
- **WHEN** account `x` is configured and `PV_ICLOUD_X_ICLOUD_USER` is not set
- **THEN** loading the config fails with an error naming `PV_ICLOUD_X_ICLOUD_USER` and
  `PV_ICLOUD_X_APP_PASSWORD`

### Requirement: MCP endpoint
The server SHALL serve MCP as stateless Streamable HTTP with JSON responses at `/mcp`. It SHALL
NOT reject requests because of their `Host` header; bearer authentication takes the place of
DNS-rebinding host checks.

#### Scenario: Tool call over HTTP
- **WHEN** an authenticated client POSTs a JSON-RPC `tools/call` request to `/mcp`
- **THEN** the response is HTTP 200 with a JSON-RPC result

### Requirement: Bearer token authentication
Every HTTP request except `GET /healthz` MUST carry `Authorization: Bearer <token>`. The server
SHALL hash the presented token with SHA-256 and compare it in constant time against every
configured hash, so lookup time does not depend on which hash matched. A missing header, a
scheme other than `Bearer`, an empty token or an unknown token SHALL get HTTP 401 with body
`{"error": "unauthorized"}` and the header `WWW-Authenticate: Bearer realm="pv-icloud"`, and
no tool SHALL run.

#### Scenario: No token
- **WHEN** a client calls `tools/list` without an `Authorization` header
- **THEN** the response is HTTP 401

#### Scenario: Wrong scheme or unknown token
- **WHEN** a client sends `Authorization: Basic abc` or a bearer token that matches no hash
- **THEN** the response is HTTP 401

### Requirement: Each token selects exactly one account
Each token SHALL belong to exactly one account, and every tool call SHALL operate only on the
account its token belongs to; no tool argument can name another account. Each account SHALL
have its own mail index file, IMAP connection pool, sync worker, CalDAV and CardDAV clients,
iCloud throttle and contacts cache. Ids presented by a caller (mail, event and contact ids) SHALL
be resolved only within the caller's own account.

#### Scenario: Same query, different accounts
- **WHEN** two callers with tokens for different accounts run the same `mail_search`
- **THEN** each sees only messages from their own account

#### Scenario: Another account's id
- **WHEN** a caller passes a mail, event or contact id obtained by a different account
- **THEN** the id is resolved against the caller's own account only, and the result never
  contains the other account's data

### Requirement: Health endpoint
`GET /healthz` SHALL be served without authentication and SHALL return `{"status": "ok"}` with
HTTP 200, or `{"status": "degraded"}` with HTTP 503 when sync workers are running and no account's
mail sync is in state `starting`, `backfilling` or `ok`. When the request carries a valid bearer
token, the body SHALL also contain `account` with that token's account `id`, its `mail_sync`
status and its `indexed` message count, and SHALL NOT contain anything about other accounts. An
invalid token SHALL get the same minimal answer as no token, not a 401.

#### Scenario: Unauthenticated health check
- **WHEN** `/healthz` is called without a token, or with an unknown token
- **THEN** the body is exactly `{"status": "ok"}` (while some account's sync is healthy)

#### Scenario: Health detail for the caller's own account
- **WHEN** `/healthz` is called with a valid token for account `alice`
- **THEN** the body contains `account.id` `alice`, `mail_sync` and `indexed`, and nothing about
  any other account

#### Scenario: Every account's sync failing
- **WHEN** sync workers are running and every account's mail sync is in state `error`
- **THEN** `/healthz` answers HTTP 503 with `{"status": "degraded"}`

### Requirement: Token generation command
`pv-icloud hash-token` SHALL print a new random token (32 random bytes, URL-safe base64) and its
SHA-256 hex digest, and SHALL NOT start the server or read the config.

#### Scenario: Generate a token
- **WHEN** an operator runs `pv-icloud hash-token`
- **THEN** it prints a `token:` line and a `sha256:` line whose value is the SHA-256 of that token

### Requirement: Listen address
The server SHALL listen on `PV_ICLOUD_HOST` (default `127.0.0.1`) and `PV_ICLOUD_PORT` (default
8771) over plain HTTP. When the host is not a loopback address it SHALL log a warning that the
server speaks plain HTTP and must be reached only through a private network or a TLS reverse
proxy.

#### Scenario: Default loopback
- **WHEN** `PV_ICLOUD_HOST` is unset, or set to `::1`
- **THEN** the server binds to that loopback address and logs no warning

#### Scenario: Non-loopback address
- **WHEN** `PV_ICLOUD_HOST` is `0.0.0.0`
- **THEN** the server binds to it and logs a warning mentioning plain HTTP

### Requirement: Mail write deployment switch
The server SHALL read `PV_ICLOUD_MAIL_WRITE` at startup. Mail write support SHALL be on only
when it is `enabled`; unset, empty or `disabled` SHALL leave it off, and any other value SHALL
stop startup with a config error naming the variable. While it is off, a token with a
non-empty `mail_write` SHALL stop startup with an error naming the account, the token label,
`mail_write` and `PV_ICLOUD_MAIL_WRITE`, and no write tool SHALL be registered.

#### Scenario: Grant without the switch
- **WHEN** `PV_ICLOUD_MAIL_WRITE` is unset and a token has `mail_write: [archive]`
- **THEN** loading the config fails with an error naming the token, `mail_write` and
  `PV_ICLOUD_MAIL_WRITE`

#### Scenario: Misspelt switch
- **WHEN** `PV_ICLOUD_MAIL_WRITE` is `true`
- **THEN** loading the config fails with an error saying the value must be `enabled` or
  `disabled`

#### Scenario: Switch on, no grants
- **WHEN** `PV_ICLOUD_MAIL_WRITE` is `enabled` and no token has `mail_write`
- **THEN** every token sees exactly the read tools

### Requirement: Mail write settings per account
Each account's `mail` section (merged from `defaults.mail` as usual) MAY contain `write` with
`protected_folders` (list of folder names, default empty), `archive_folder` (folder name,
default `Archive`) and `send_from` (a single bare address `local@domain`, default unset). The
protected folders that apply to an account SHALL be every name listed in
`defaults.mail.write.protected_folders` and in the account's own
`mail.write.protected_folders`: an account list adds to the defaults and never replaces them.
`send_from` SHALL be taken from the account, or else from `defaults.mail.write`. A value of
the wrong type, a `send_from` that is not one bare address, or an unknown key under
`mail.write`, SHALL stop startup with an error naming the account and key.

#### Scenario: Defaults and account both protect folders
- **WHEN** `defaults.mail.write.protected_folders` is `[Taxes]` and an account's is `[Banking]`
- **THEN** both `Taxes` and `Banking` are protected for that account

#### Scenario: Archive folder override
- **WHEN** an account sets `mail.write.archive_folder: Old Mail`
- **THEN** `mail_archive` for that account moves messages to `Old Mail`

#### Scenario: From address for drafts
- **WHEN** `defaults.mail.write.send_from` is `me@example.com` and an account sets none
- **THEN** that account's drafts carry `From: me@example.com`

#### Scenario: Invalid From address
- **WHEN** an account sets `mail.write.send_from` to `not-an-address` or
  `Me <me@example.com>`
- **THEN** loading the config fails with an error naming the account and
  `mail.write.send_from`

### Requirement: Token permissions reach every call
The permissions, protected folders and write limit of the token a request authenticated with
SHALL apply to every tool call in that request, and to nothing else: two tokens of the same
account SHALL each get only their own. Audit lines SHALL keep identifying the token only by
its label.

#### Scenario: Same account, different permissions
- **WHEN** tokens `laptop` (`mail_write: [archive]`) and `phone` (no `mail_write`) of the same
  account call `tools/list`
- **THEN** `laptop` sees `mail_archive` and `phone` does not
