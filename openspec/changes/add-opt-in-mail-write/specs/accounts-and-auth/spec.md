# Spec Delta

## MODIFIED Requirements

### Requirement: Account definitions are validated
The configuration MUST list at least one account under `accounts`. Each account `id` MUST match
`[a-z0-9_]+` and be unique. Each account MUST have at least one entry under `tokens`; each token
MUST have a non-empty `label` and a `sha256` of exactly 64 hexadecimal characters (compared in
lower case), and no two tokens in the whole file MAY share a hash. A token MAY also have
`mail_write`, a list of actions from `move`, `archive`, `delete`, `draft` and `send` (default
empty), and `protected_folders`, a list of folder names (default empty). Any other value for
these keys, or an unknown action, SHALL stop startup with an error naming the account, the
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

#### Scenario: Token without write keys
- **WHEN** a token has only `label` and `sha256`
- **THEN** it loads with no write actions and no extra protected folders

## ADDED Requirements

### Requirement: Mail write settings per account
Each account's `mail` section (merged from `defaults.mail` as usual) MAY contain `write` with
`protected_folders` (list of folder names, default empty), `archive_folder` (folder name,
default unset) and `send_from` (an email address, default unset). The protected folders that
apply to an account SHALL be every name listed in `defaults.mail.write.protected_folders` and
in the account's own `mail.write.protected_folders`: an account list adds to the defaults and
never replaces them. A value of the wrong type, an invalid `send_from` address, or a token with
`send` on an account without `send_from` SHALL stop startup with an error naming the account
and key.

#### Scenario: Defaults and account both protect folders
- **WHEN** `defaults.mail.write.protected_folders` is `[Taxes]` and an account's is `[Banking]`
- **THEN** both `Taxes` and `Banking` are protected for that account

#### Scenario: Send without a sender address
- **WHEN** a token has `mail_write: [send]` and its account has no `mail.write.send_from`
- **THEN** loading the config fails with an error naming the account and `send_from`

### Requirement: Token permissions reach every call
The permissions and protected folders of the token a request authenticated with SHALL apply
to every tool call in that request, and to nothing else: two tokens of the same account SHALL
each get only their own permissions. Audit lines SHALL keep identifying the token only by its
label.

#### Scenario: Same account, different permissions
- **WHEN** tokens `laptop` (`mail_write: [archive]`) and `phone` (no `mail_write`) of the same
  account call `tools/list`
- **THEN** `laptop` sees `mail_archive` and `phone` does not
