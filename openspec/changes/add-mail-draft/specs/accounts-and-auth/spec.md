# Spec Delta

## MODIFIED Requirements

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
