## ADDED Requirements

### Requirement: Contacts write deployment switch
The server SHALL read `PV_ICLOUD_CONTACTS_WRITE` at startup, independently of
`PV_ICLOUD_MAIL_WRITE` and `PV_ICLOUD_CALENDAR_WRITE`. Contact write support SHALL be on only
when it is `enabled`; unset, empty or `disabled` SHALL leave it off, and any other value SHALL
stop startup with a config error naming the variable. While it is off, a token with a
non-empty `contacts_write` SHALL stop startup with an error naming the account, the token
label, `contacts_write` and `PV_ICLOUD_CONTACTS_WRITE`, and no contact write tool SHALL be
registered. A token with a non-empty `contacts_write` on an account with
`contacts.enabled: false` SHALL stop startup with an error naming the account, the token
label, `contacts_write` and `contacts.enabled`.

#### Scenario: Grant without the switch
- **WHEN** `PV_ICLOUD_CONTACTS_WRITE` is unset and a token has `contacts_write: [update]`
- **THEN** loading the config fails with an error naming the token, `contacts_write` and
  `PV_ICLOUD_CONTACTS_WRITE`

#### Scenario: Misspelt switch
- **WHEN** `PV_ICLOUD_CONTACTS_WRITE` is `on`
- **THEN** loading the config fails with an error saying the value must be `enabled` or
  `disabled`

#### Scenario: Switch on, no grants
- **WHEN** `PV_ICLOUD_CONTACTS_WRITE` is `enabled` and no token has `contacts_write`
- **THEN** every token sees exactly the tools it saw with the switch off

#### Scenario: Switches are independent
- **WHEN** `PV_ICLOUD_CALENDAR_WRITE` is `enabled`, `PV_ICLOUD_CONTACTS_WRITE` is unset and a
  token has `calendar_write: true`
- **THEN** the config loads, the token can write events, and no contact write tool is
  registered

#### Scenario: Grant on an account with contacts off
- **WHEN** `PV_ICLOUD_CONTACTS_WRITE` is `enabled`, an account sets `contacts.enabled: false`
  and one of its tokens has `contacts_write: [create]`
- **THEN** loading the config fails with an error naming the account, the token,
  `contacts_write` and `contacts.enabled`

## MODIFIED Requirements

### Requirement: Token permissions reach every call
The permissions (mail write actions, `calendar_write` and contact write actions), protected
folders, protected calendars and write limit of the token a request authenticated with SHALL
apply to every tool call in that request, and to nothing else: two tokens of the same account
SHALL each get only their own. Audit lines SHALL keep identifying the token only by its label.

#### Scenario: Same account, different permissions
- **WHEN** tokens `laptop` (`mail_write: [archive]`) and `phone` (no `mail_write`) of the same
  account call `tools/list`
- **THEN** `laptop` sees `mail_archive` and `phone` does not

#### Scenario: Same account, different calendar permissions
- **WHEN** tokens `laptop` (`calendar_write: true`) and `phone` (no `calendar_write`) of the
  same account call `tools/list`
- **THEN** `laptop` sees `calendar_event_create` and `phone` does not

#### Scenario: Same account, different contact permissions
- **WHEN** tokens `laptop` (`contacts_write: [update, delete]`) and `agent`
  (`contacts_write: [update]`) of the same account call `tools/list`
- **THEN** both see `contacts_update`, only `laptop` sees `contacts_delete`, and neither sees
  `contacts_create`

### Requirement: Account and token definitions are validated
The configuration MUST list at least one account under `accounts`. Each account `id` MUST match
`[a-z0-9_]+` and be unique. Each account MUST have at least one entry under `tokens`; each token
MUST have a non-empty `label` and a `sha256` of exactly 64 hexadecimal characters (compared in
lower case), and no two tokens in the whole file MAY share a hash. A token MAY also have:
`mail_write`, a list of actions from `move`, `archive`, `delete`, `draft` and `send` (default
empty); `calendar_write`, `true` or `false` (default `false`); `contacts_write`, a list of
actions from `create`, `update` and `delete` (default empty); `protected_folders`, a list of
folder names (default empty); `protected_calendars`, a list of calendar display names (default
empty) added to the account's protected calendars for that token only;
`max_writes_per_hour`, a positive integer (default 30) that limits the token's mail, calendar
and contact write calls together; and `send_to`, only on a token with `send`, a non-empty list
whose entries are each a single bare address (`local@domain`) or `@` followed by a domain
(default absent: any recipient). Any other value for these keys, an unknown action, or `send_to` on a token without `send` SHALL stop startup with an error naming the
account, the token label and the key.

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

#### Scenario: Calendar write grant
- **WHEN** `PV_ICLOUD_CALENDAR_WRITE` is `enabled` and a token has `calendar_write: true`
- **THEN** it loads with calendar write on and no mail write actions

#### Scenario: Calendar write not a boolean
- **WHEN** a token has `calendar_write: [create]` or `calendar_write: "yes"`
- **THEN** loading the config fails with an error naming the token and `calendar_write`

#### Scenario: Contact write grant
- **WHEN** `PV_ICLOUD_CONTACTS_WRITE` is `enabled` and a token has
  `contacts_write: [create, update]`
- **THEN** it loads with the contact write actions `create` and `update`, no `delete`, and no
  mail or calendar write

#### Scenario: Unknown or malformed contact write action
- **WHEN** a token has `contacts_write: [update, merge]` or `contacts_write: true`
- **THEN** loading the config fails with an error naming the token and `contacts_write`, and
  for `merge` the allowed actions

#### Scenario: Token protects a calendar
- **WHEN** a token has `calendar_write: true` and `protected_calendars: [Work]`
- **THEN** it loads, and `Work` is protected for that token in addition to the account's
  protected calendars

#### Scenario: Protected calendars not a list
- **WHEN** a token has `protected_calendars: Work`
- **THEN** loading the config fails with an error naming the token and `protected_calendars`

#### Scenario: Invalid write limit
- **WHEN** a token has `max_writes_per_hour: 0`
- **THEN** loading the config fails with an error naming the token and `max_writes_per_hour`

#### Scenario: Token without write keys
- **WHEN** a token has only `label` and `sha256`
- **THEN** it loads with no write actions, calendar write off, no contact write actions, no
  extra protected folders or calendars, a write limit of 30 per hour and no `send_to`

#### Scenario: send_to without send
- **WHEN** a token has `mail_write: [draft]` and `send_to: [a@example.com]`
- **THEN** loading the config fails with an error naming the token and `send_to`

#### Scenario: Malformed send_to entry
- **WHEN** a token with `send` has `send_to: ["Bob <b@example.com>"]`, `send_to: []` or
  `send_to: ["example.org"]`
- **THEN** loading the config fails with an error naming the token and `send_to`
