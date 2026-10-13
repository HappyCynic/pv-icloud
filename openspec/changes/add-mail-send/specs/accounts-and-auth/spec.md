## ADDED Requirements

### Requirement: Account and token definitions are validated
The configuration MUST list at least one account under `accounts`. Each account `id` MUST match
`[a-z0-9_]+` and be unique. Each account MUST have at least one entry under `tokens`; each token
MUST have a non-empty `label` and a `sha256` of exactly 64 hexadecimal characters (compared in
lower case), and no two tokens in the whole file MAY share a hash. A token MAY also have:
`mail_write`, a list of actions from `move`, `archive`, `delete`, `draft` and `send` (default
empty); `protected_folders`, a list of folder names (default empty); `max_writes_per_hour`, a
positive integer (default 30); and `send_to`, only on a token with `send`, a non-empty list
whose entries are each a single bare address (`local@domain`) or `@` followed by a domain
(default absent: any recipient). Any other value for these keys, an unknown action, or
`send_to` on a token without `send` SHALL stop startup with an error naming the account, the
token label and the key.

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

#### Scenario: Send grant
- **WHEN** `PV_ICLOUD_MAIL_WRITE` is `enabled`, the account sets `mail.write.send_from` and a
  token has `mail_write: [send]`
- **THEN** it loads with the single write action `send`

#### Scenario: Invalid write limit
- **WHEN** a token has `max_writes_per_hour: 0`
- **THEN** loading the config fails with an error naming the token and `max_writes_per_hour`

#### Scenario: Token without write keys
- **WHEN** a token has only `label` and `sha256`
- **THEN** it loads with no write actions, no extra protected folders, a write limit of 30
  per hour and no `send_to`

#### Scenario: send_to without send
- **WHEN** a token has `mail_write: [draft]` and `send_to: [a@example.com]`
- **THEN** loading the config fails with an error naming the token and `send_to`

#### Scenario: Malformed send_to entry
- **WHEN** a token with `send` has `send_to: ["Bob <b@example.com>"]`, `send_to: []` or
  `send_to: ["example.org"]`
- **THEN** loading the config fails with an error naming the token and `send_to`

### Requirement: Warning for unrestricted sending
At startup, while `PV_ICLOUD_MAIL_WRITE` is `enabled`, the server SHALL log one warning for
each token that has `send` and no `send_to`: `token <label> of account <id> may send mail to
any recipient (no send_to)`. The warning SHALL NOT contain the token, its hash, `send_from` or
any other address.

#### Scenario: Token without an allowlist
- **WHEN** the server starts with token `agent` of account `alice` granted `send` and no
  `send_to`
- **THEN** it logs a warning naming `agent` and `alice` and saying the token may send mail to
  any recipient

#### Scenario: Token with an allowlist
- **WHEN** every token with `send` has `send_to`
- **THEN** no such warning is logged

## MODIFIED Requirements

### Requirement: Mail write settings per account
Each account's `mail` section (merged from `defaults.mail` as usual) MAY contain `write` with
`protected_folders` (list of folder names, default empty), `archive_folder` (folder name,
default `Archive`) and `send_from` (a single bare address `local@domain`, default unset). The
protected folders that apply to an account SHALL be every name listed in
`defaults.mail.write.protected_folders` and in the account's own
`mail.write.protected_folders`: an account list adds to the defaults and never replaces them.
`send_from` SHALL be taken from the account, or else from `defaults.mail.write`. A value of
the wrong type, a `send_from` that is not one bare address, or an unknown key under
`mail.write`, SHALL stop startup with an error naming the account and key. An account with a
token granted `send` and no `send_from` SHALL stop startup with an error naming the account,
the token label and `mail.write.send_from`.

#### Scenario: Defaults and account both protect folders
- **WHEN** `defaults.mail.write.protected_folders` is `[Taxes]` and an account's is `[Banking]`
- **THEN** both `Taxes` and `Banking` are protected for that account

#### Scenario: Archive folder override
- **WHEN** an account sets `mail.write.archive_folder: Old Mail`
- **THEN** `mail_archive` for that account moves messages to `Old Mail`

#### Scenario: From address for drafts
- **WHEN** `defaults.mail.write.send_from` is `me@example.com` and an account sets none
- **THEN** that account's drafts carry `From: me@example.com`

#### Scenario: Send without a From address
- **WHEN** a token has `mail_write: [send]` and its account has no `mail.write.send_from`
- **THEN** loading the config fails with an error naming the account, the token and
  `mail.write.send_from`

#### Scenario: Invalid From address
- **WHEN** an account sets `mail.write.send_from` to `not-an-address` or
  `Me <me@example.com>`
- **THEN** loading the config fails with an error naming the account and
  `mail.write.send_from`

## REMOVED Requirements

### Requirement: Account definitions are validated
**Reason**: Replaced by "Account and token definitions are validated". The old requirement
had the scenario "Send is not yet an action", which `send` makes false, and a MODIFIED block
cannot drop a scenario.
**Migration**: None for operators. Configs that loaded before still load; `send` and
`send_to` are new optional keys.
