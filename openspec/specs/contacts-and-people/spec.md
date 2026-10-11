# contacts-and-people Specification

## Purpose
The contacts tools (`contacts_search`, `contacts_get`, `contacts_groups`), the in-memory
address-book cache behind them, excluded groups, and `find_person`, which joins contacts with
the mail index to answer who someone is.

## Requirements

### Requirement: Contacts switch
When `contacts.enabled` is `false` for an account, `contacts_search`, `contacts_get` and
`contacts_groups` SHALL fail with the tool error `contacts are turned off for this account
(contacts.enabled: false)`, and the server SHALL send no CardDAV request for that account.

#### Scenario: Contacts turned off
- **WHEN** an account sets `contacts.enabled: false`
- **THEN** every contacts tool fails with a tool error mentioning `turned off`, and nothing is
  requested from iCloud Contacts, not even at startup

### Requirement: In-memory address book
Each account's address book SHALL be held in memory only, never written to disk. It SHALL be
loaded in the background at server startup and on first use, then refreshed at most once every
60 seconds when a contacts tool or `find_person` needs it, using CardDAV `sync-collection` so
only changed cards are fetched (with `addressbook-multiget`, 100 cards per request). When iCloud
rejects the sync token (HTTP 403, 409 or 412), the whole book SHALL be loaded again. A card that
cannot be parsed SHALL be skipped. Group cards SHALL never be returned as contacts.

#### Scenario: Only changes are fetched
- **WHEN** the book was loaded, then one card is added, one changed and one deleted, and a
  caller searches after 60 seconds
- **THEN** one sync request and one multiget for the two changed cards are sent, and the results
  reflect all three changes

#### Scenario: Nothing changed
- **WHEN** a caller searches again after 60 seconds and nothing changed
- **THEN** one small sync request is sent and no cards are fetched

#### Scenario: Within the refresh interval
- **WHEN** a caller searches twice within 60 seconds
- **THEN** the second search sends no request

### Requirement: Serving a stale copy
When a refresh fails after the address book was loaded once, the tools SHALL keep answering from
the loaded copy and add a `note` saying iCloud Contacts is not answering and the results come
from the copy loaded at `contacts_as_of`; the next attempt SHALL be made after another 60
seconds. When the first load fails, the call SHALL fail. Every contacts result SHALL include
`contacts_as_of`, the time of the last successful check with iCloud.

#### Scenario: iCloud busy after a load
- **WHEN** iCloud answers 503 to a refresh after the book was loaded
- **THEN** `contacts_search`, `contacts_get` and `contacts_groups` answer from the loaded copy
  with the stale `note`, and the note disappears once iCloud answers again

#### Scenario: First load fails
- **WHEN** iCloud answers HTTP 500 to the first load
- **THEN** the call fails, and the next call tries the load again

### Requirement: Excluded groups
Every contact that is a member of a group matched by `contacts.exclude_groups` (names or globs,
compared case-insensitively) SHALL be hidden from every tool, including `find_person`'s contact
matches, and the excluded groups SHALL not be listed or usable as a filter.

#### Scenario: Medical group excluded
- **WHEN** `contacts.exclude_groups` is `["medical"]` and `Dr. Strange` is in group `Medical`
- **THEN** `Dr. Strange` is not found by `contacts_search`, `contacts_get` on the id says not
  found, `contacts_groups` does not list `Medical`, and `group` `Medical` is
  `unknown group: Medical`

### Requirement: contacts_search
`contacts_search` SHALL return contacts in which every word of `query` (folded, with double
quotes ignored) appears in the display name, given, family or middle name, nickname, maiden name,
organization, department, job title, note, an email address, a phone number, a formatted postal
address, a related name or a social-profile user name. A word made only of digits and phone
punctuation with at least 3 digits SHALL also match when its digits appear in a phone number's
digits, whatever the formatting. Results SHALL be ordered by how many query words appear in the
contact's names (more first), then by display name and id. Without `query`, every contact SHALL be
listed alphabetically. `group` SHALL restrict results to one group (case-insensitive name); an
unknown or excluded group SHALL be a tool error `unknown group: <name>`. Each result SHALL have
`id`, `name`, `organization`, `title`, `emails` and `phones` (each `label` and `value`), and the
result SHALL include `total`, `offset`, `next_offset` and `contacts_as_of`. `limit` SHALL default
to 25 with a maximum of 100.

#### Scenario: Names, emails, company and accents
- **WHEN** a caller searches `jose`, `ALVAREZ jose`, `acme`, `jane.doe@home` or `springfield`
- **THEN** `José Álvarez` is found for the first two, and `Jane Doe` (company `Acme`, home email,
  address in Springfield) for the others

#### Scenario: Phone in any format
- **WHEN** a contact's phone is `+1 (312) 555-0134`
- **THEN** `3125550134` and `(312) 555-0134` both find the contact

#### Scenario: Name matches rank first
- **WHEN** `jane` is in one contact's name and in another contact's note
- **THEN** the contact named Jane comes first

#### Scenario: Unknown group
- **WHEN** `group` is `Nope`
- **THEN** the call fails with `unknown group: Nope`

### Requirement: contacts_get
`contacts_get` SHALL return one contact by id with `name`, `given_name`, `family_name`,
`middle_name`, `prefix`, `suffix`, `nickname`, `maiden_name`, `organization`, `department`,
`title`, `emails` and `phones` (each `label`, `value`, `preferred`), `addresses` (label, street,
city, region, postal code, country, formatted), `urls`, `social_profiles`, `instant_messaging`,
`birthday`, `dates`, `related_names`, `note`, `note_truncated`, `groups` (names of its visible
groups), `is_me` (it is the account owner's own card) and `contacts_as_of`. The note SHALL be cut
at 20,000 characters with `note_truncated` true. Dates without a year SHALL be written
`--MM-DD`. Photos SHALL be left out. An unknown, hidden or stale id SHALL be the tool error
`contact not found (deleted, hidden, or a stale id)`.

#### Scenario: Full contact
- **WHEN** a caller gets a contact in group `Family` with a birthday stored without a year
- **THEN** `groups` is `["Family"]`, `birthday` is `--03-15`, and no photo data is returned

#### Scenario: Long note
- **WHEN** a contact's note has 25,000 characters
- **THEN** 20,000 are returned and `note_truncated` is true

#### Scenario: Unknown id
- **WHEN** the id is `../../etc/passwd`
- **THEN** the call fails with a tool error mentioning `not found`

### Requirement: contacts_groups
`contacts_groups` SHALL list the visible groups by case-insensitive name, each with `name` and
`members` (the number of visible contacts in it), plus `contacts`, the number of visible contacts,
and `contacts_as_of`.

#### Scenario: Groups and counts
- **WHEN** the book has 5 visible contacts, `Family` with 2 and `Medical` with 1
- **THEN** the result lists `Family` (2) then `Medical` (1) and `contacts` is 5

### Requirement: Contact ids and names
A contact id SHALL be the card's file name without `.vcf`; it is never used to build a URL. A
contact's display name SHALL be its `FN`, else the name built from `N`, else its organization,
else its first email address, else its first phone number, else `(no name)`. A value's label
SHALL be its Apple item-group label (`X-ABLabel`): a built-in `_$!<Label>!$_` label in lower
case, a custom label as written. Without one, the label SHALL be the value's `TYPE`s in lower
case, ignoring `internet`, `pref`, `voice`, `x400` and `msg`, with `cell` shown as `mobile`
(dropped next to `iphone`) and `iphone` as `iPhone`; with no such type, the label is null.

#### Scenario: Name fallbacks
- **WHEN** a card has no `FN` and `N` is `Lee;Ann;;;`, or has only `ORG:Widgets;`, or only an
  email address
- **THEN** its name is `Ann Lee`, `Widgets`, or the email address

#### Scenario: Phone labels
- **WHEN** phones are typed `IPHONE,CELL`, labelled `_$!<Main>!$_`, or untyped
- **THEN** their labels are `iPhone`, `main` and null

### Requirement: find_person matches contacts and mail
`find_person` SHALL require a `query` with at least one word, and return people that match it:
contacts that match as in `contacts_search` (each with `name`, `contact_id`, `organization`,
`title`, `emails`, `phones`, `groups`, `is_me`), and, for addresses seen in mail that are on no
visible card, mail-only people (`name` the display name most used, `contact_id` null, `emails`
with the address, `names_seen` with up to 3 names, `is_me`). An address seen in mail SHALL count
for a person when it is one of the matched contacts' addresses, or its display name and address
together contain every query word; an address on a visible card SHALL be merged into that card's
person. People SHALL be ordered with the account owner last, then by most messages, then by name.
The result SHALL include `total`, `offset`, `next_offset` and `mail_searched_since`. `limit`
SHALL default to 10 with a maximum of 25. `find_person` SHALL NOT read calendars.

#### Scenario: Name prefix
- **WHEN** a caller searches `jan`
- **THEN** `Jane Doe`, `Janet Smith` and `Jan Stranger` are all returned

#### Scenario: Address on a card
- **WHEN** a caller searches an address that is on Jane Doe's card, used in mail under another
  display name
- **THEN** the only person returned is `Jane Doe`

#### Scenario: Mail-only correspondent
- **WHEN** the query matches only an address on no card
- **THEN** the person has `contact_id` null and the display name used in mail

#### Scenario: Paging and validation
- **WHEN** `query` is only spaces, or `limit` is 26
- **THEN** the call fails with a tool error

### Requirement: find_person mail statistics
`find_person` SHALL read mail only from the local index: in-scope folders inside the index
window, messages whose From, To or Cc contain every query word as a word prefix or any address of
a matched contact, newest first, at most 2,000 messages (`mail_scan_truncated` true when that
limit was reached, so counts are lower bounds). Each person SHALL have `mail` (null without mail)
with `messages`, `from_them`, `to_them`, `first`, `last` and `recent` (up to 3 newest, each `id`,
`date`, `folder`, `from`, `subject`).

#### Scenario: Contact merged with its mail
- **WHEN** contact Jane Doe has two addresses, and three in-scope messages involve either of them
  (two from her, one to her) under different display names
- **THEN** one person `Jane Doe` has `contact_id`, `mail.messages` 3, `from_them` 2, `to_them` 1
  and the three subjects newest first in `recent`

#### Scenario: Out-of-scope mail
- **WHEN** a matching sender appears only in `Junk`, which is hidden
- **THEN** that sender is not returned

### Requirement: The owner is marked
A person SHALL have `is_me` true when it is the account owner's own card (iCloud's me-card), or,
for a mail-only person, when its address is the account's Apple ID or an address on the me-card.

#### Scenario: Searching the owner's name
- **WHEN** a caller searches for the owner's name
- **THEN** the owner's card is returned last with `is_me` true

### Requirement: find_person without contacts
When contacts are turned off for the account, or iCloud Contacts is unavailable before the book
was ever loaded, `find_person` SHALL still answer from mail, with every match a mail-only person,
and add an entry to `notes` saying so. When it answers from a stale copy, `notes` SHALL include
the stale-copy note. A contact hidden by an excluded group SHALL appear only as a mail-only
person.

#### Scenario: Contacts off
- **WHEN** contacts are turned off and a caller searches `jane`
- **THEN** Jane appears as a mail-only person and `notes` says contacts are turned off

#### Scenario: Contacts down
- **WHEN** iCloud Contacts answers 503 before the book was loaded
- **THEN** mail matches are returned and `notes` says iCloud Contacts is unavailable

#### Scenario: Excluded group member
- **WHEN** `Dr. Strange` is in an excluded group and has sent mail
- **THEN** `find_person` returns that person with `contact_id` null and the mail statistics
