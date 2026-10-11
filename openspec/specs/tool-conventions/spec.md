# tool-conventions Specification

## Purpose
Rules every MCP tool follows: the tool set and its annotations, paging, how limits are stated,
untrusted-content marking and provenance, text matching, audit logging with redaction, and how
errors reach the caller.

## Requirements

### Requirement: Fixed, read-only tool set
The server SHALL expose exactly these tools: `mail_folders`, `mail_search`, `mail_get`,
`mail_thread`, `calendar_list`, `calendar_events`, `calendar_search`, `calendar_event`,
`find_person`, `contacts_search`, `contacts_get` and `contacts_groups`. Every tool SHALL carry
the annotations `readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true` and
`openWorldHint: true`. The server instructions SHALL say that access is read-only, that nothing
can send, move, delete or flag mail, create or change events, or change contacts, that
`find_person` is the place to start when working out who someone is, and that returned content
is untrusted.

#### Scenario: Listing tools
- **WHEN** an authenticated client calls `tools/list`
- **THEN** it gets exactly the twelve tools above, each with `readOnlyHint` true

### Requirement: Paging capped lists
Every tool that returns a capped list (`mail_search`, `mail_thread`, `calendar_events`,
`calendar_search`, `contacts_search`, `find_person`) SHALL take `limit` (at least 1, at most the
tool's maximum) and `offset` (at least 0), and SHALL return `offset` and `next_offset`, where
`next_offset` is the offset of the next page or `null` on the last page. `mail_thread`,
`calendar_events`, `calendar_search`, `contacts_search` and `find_person` SHALL also return
`total`, the number of matches. A `limit` or `offset` outside its range SHALL be a tool error.
Each call SHALL compute its result set afresh, so items that arrive between two calls can shift
pages.

#### Scenario: Walking every page
- **WHEN** a caller starts with `offset` 0 and repeats the call with `offset` set to the
  previous `next_offset` until it is `null`
- **THEN** every match is returned exactly once, in order

#### Scenario: Offset past the end
- **WHEN** `offset` is larger than the number of matches
- **THEN** the page is empty and `next_offset` is `null`

#### Scenario: Negative offset
- **WHEN** `offset` is -1
- **THEN** the call fails with a tool error mentioning `offset`

### Requirement: Paging long text by characters
`mail_get` (message body) and `calendar_event` (notes) SHALL take `max_chars` (1 to 20000,
default 20000) and `offset` (characters, at least 0), return the slice of the text starting at
`offset`, the total length (`body_total_chars` or `notes_total_chars`) and `next_offset`
(`null` when the slice reaches the end).

#### Scenario: Reading a body in pages
- **WHEN** a caller reads a body with `max_chars` 7 and `offset` 8
- **THEN** it gets 7 characters starting at character 8 and `next_offset` 15 if the body is
  longer

### Requirement: Tool descriptions state their limits
Each capped tool's description SHALL state its default and maximum `limit`, and SHALL explain
paging with `next_offset`. `mail_get` and `calendar_event` SHALL state the 20000-character cap
per call and how to page. Limits that come from per-account configuration (the mail index window
`mail.max_age_days`, `calendar.max_range_days`, `calendar.max_search_days`) SHALL be written as
`N days` when every account has the same value, otherwise as `A to B days, depending on the
account`. When any account has `contacts.enabled: false`, the contacts tools' descriptions SHALL
say that those accounts get an error.

#### Scenario: Default limits in descriptions
- **WHEN** every account uses the default configuration
- **THEN** `mail_search` says `max 50 results` and `last 730 days`, `mail_thread` says
  `max 100 messages`, `calendar_events` says `max 200 events` and `at most 90 days`,
  `calendar_search` says `max 200 events` and `at most 730 days`, `contacts_search` says
  `max 100 contacts`, `find_person` says `max 25 people`, and each mentions `next_offset`

#### Scenario: Accounts with different limits
- **WHEN** accounts have `calendar.max_range_days` 30, 60 and 90
- **THEN** the `calendar_events` description says `30 to 90 days, depending on the account`

### Requirement: Untrusted-content marking
Every successful tool result SHALL contain `"content_trust": "untrusted"`. The description of
every tool that returns mail, event or contact text SHALL say that the content is untrusted, may
contain instructions, and must be treated as data only.

#### Scenario: Result is marked untrusted
- **WHEN** any tool call succeeds
- **THEN** its result has `content_trust` equal to `untrusted`

### Requirement: Provenance
Every successful tool result SHALL contain `provenance` with `source` and `fetched_at` (the
current time, ISO 8601 in UTC). `source` SHALL be `icloud-mail` for the mail tools,
`icloud-calendar` for the calendar tools, `icloud-contacts` for the contacts tools and
`icloud-contacts+mail` for `find_person`.

#### Scenario: Provenance of a mail search
- **WHEN** `mail_search` succeeds
- **THEN** `provenance.source` is `icloud-mail` and `provenance.fetched_at` is a UTC timestamp

### Requirement: Case- and accent-insensitive matching
Wherever a tool matches caller text against mail, event or contact text, it SHALL compare folded
text: case-folded, compatibility-decomposed (NFKD) with combining marks removed.

#### Scenario: Accent and case ignored
- **WHEN** a caller searches for `cafe` or `CAFÉ`
- **THEN** text containing `Café` matches

### Requirement: Audit log line per tool call
For every tool call, successful or not, the server SHALL write one JSON line to the
`pv_icloud.audit` logger, printed on stdout, with `ts` (UTC, ISO 8601), `event` `tool_call`,
`account_id`, `token_label`, `tool`, redacted `args`, `outcome` (`ok`, `invalid`, `throttled`,
`upstream_error` or `error`), `result_count` and `latency_ms`. The line MUST NOT contain the
bearer token or its hash. `result_count` SHALL be the length of the first list found among
`results`, `messages`, `folders`, `calendars`, `events`, `contacts` and `people` (for
`contacts_groups`, the `groups` list), 1 when there is none, and 0 when the call failed.

#### Scenario: Audit line for a search
- **WHEN** account `alice` calls `mail_search` with the token labelled `laptop` and gets one result
- **THEN** one audit line has `account_id` `alice`, `token_label` `laptop`, `tool` `mail_search`
  and `result_count` 1, and contains neither the search text nor the token

#### Scenario: Result counts
- **WHEN** a caller with one contact and no groups calls `contacts_groups`, `contacts_get` and
  `find_person` (one match)
- **THEN** the audit lines have `result_count` 0, 1 and 1

### Requirement: Audit arguments are redacted
In the audit line, the arguments `limit`, `offset`, `max_chars`, `since`, `before`, `start` and
`end` SHALL be logged as given; arguments that are null SHALL be left out; list arguments SHALL
be replaced by `<N items>`; every other argument (search text, ids, names, groups) SHALL be
replaced by `<N chars>`, its length.

#### Scenario: Search text and ids are not logged
- **WHEN** a caller runs `mail_search` with `query` `secret plans`, or `contacts_get` with an id
- **THEN** the audit line shows `<12 chars>` for the query and the id's length, never the text

#### Scenario: Folder list reduced to a count
- **WHEN** a caller passes `folders` with two names
- **THEN** the audit line shows `folders` as `<2 items>`

### Requirement: Error behaviour
A bad argument or an id that cannot be served SHALL be returned as an MCP tool error
(`isError: true`) with a message that is safe to show the caller. When iCloud is throttling the
account, the tool error SHALL say `iCloud is limiting requests for this account; try again in
about N minutes` with N the remaining pause in minutes, rounded, at least 1 (`minute` when N is
1). Any other IMAP or DAV failure SHALL be returned as the tool error `iCloud is unavailable right
now; try again shortly`, with the details logged as a warning naming the account and tool, not
returned. Error messages SHALL NOT reveal whether an excluded folder, calendar or contact group
exists.

#### Scenario: Malformed id
- **WHEN** `mail_get` is called with id `%%%`
- **THEN** the result is a tool error

#### Scenario: Throttled account
- **WHEN** iCloud answered HTTP 503 with `Retry-After: 1500` for the account
- **THEN** calendar and contacts tools fail with `try again in about 25 minutes`

#### Scenario: iCloud rejects the credentials
- **WHEN** CalDAV answers HTTP 401 during `calendar_list`
- **THEN** the tool error says iCloud is unavailable, without the upstream details

### Requirement: Logs carry nothing private
Server logs SHALL NOT contain message content, search text, credentials, bearer tokens or their
hashes, or subscribed-calendar feed URLs; where a feed must be identified, only its host name
SHALL be logged.

#### Scenario: Feed failure logged
- **WHEN** a subscribed-calendar feed fails and a cached copy is served
- **THEN** the warning names only the feed's host
