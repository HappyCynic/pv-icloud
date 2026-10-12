# Spec Delta

## MODIFIED Requirements

### Requirement: Account definitions are validated
The configuration MUST list at least one account under `accounts`. Each account `id` MUST match
`[a-z0-9_]+` and be unique. Each account MUST have at least one entry under `tokens`; each token
MUST have a non-empty `label` and a `sha256` of exactly 64 hexadecimal characters (compared in
lower case), and no two tokens in the whole file MAY share a hash. A token MAY also have:
`mail_write`, a list of actions from `move`, `archive` and `delete` (default empty);
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

#### Scenario: Invalid write limit
- **WHEN** a token has `max_writes_per_hour: 0`
- **THEN** loading the config fails with an error naming the token and `max_writes_per_hour`

#### Scenario: Token without write keys
- **WHEN** a token has only `label` and `sha256`
- **THEN** it loads with no write actions, no extra protected folders and a write limit of 30
  per hour

## ADDED Requirements

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
`protected_folders` (list of folder names, default empty) and `archive_folder` (folder name,
default `Archive`). The protected folders that apply to an account SHALL be every name listed
in `defaults.mail.write.protected_folders` and in the account's own
`mail.write.protected_folders`: an account list adds to the defaults and never replaces them.
A value of the wrong type, or an unknown key under `mail.write`, SHALL stop startup with an
error naming the account and key.

#### Scenario: Defaults and account both protect folders
- **WHEN** `defaults.mail.write.protected_folders` is `[Taxes]` and an account's is `[Banking]`
- **THEN** both `Taxes` and `Banking` are protected for that account

#### Scenario: Archive folder override
- **WHEN** an account sets `mail.write.archive_folder: Old Mail`
- **THEN** `mail_archive` for that account moves messages to `Old Mail`

### Requirement: Token permissions reach every call
The permissions, protected folders and write limit of the token a request authenticated with
SHALL apply to every tool call in that request, and to nothing else: two tokens of the same
account SHALL each get only their own. Audit lines SHALL keep identifying the token only by
its label.

#### Scenario: Same account, different permissions
- **WHEN** tokens `laptop` (`mail_write: [archive]`) and `phone` (no `mail_write`) of the same
  account call `tools/list`
- **THEN** `laptop` sees `mail_archive` and `phone` does not
