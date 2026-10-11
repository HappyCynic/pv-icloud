# mail-tools Specification

## Purpose
The mail tools: `mail_folders`, `mail_search` (filters, case and accent folding, window),
`mail_get` and `mail_thread`, with their ids, result shapes and limits. Search runs on the local
index; single messages are read live from iCloud.

## Requirements

### Requirement: mail_folders
`mail_folders` SHALL list the account's in-scope folders with their message counts, `INBOX`
first and the rest in case-insensitive name order, with `count_source` and `counts_as_of`. Once
the sync worker has recorded any folder, names and counts SHALL come from the index
(`count_source` `index`, `counts_as_of` the oldest of the folders' last sync times). Before that
they SHALL be read live with `LIST` and `STATUS` (`count_source` `live`, `counts_as_of` now).

#### Scenario: Folders after a sync
- **WHEN** INBOX, `Work` and `Friends & Family` are in scope and synced
- **THEN** the folders are listed as INBOX, `Friends & Family`, `Work`, each with its message
  count, and `count_source` is `index`

#### Scenario: Before the first sync
- **WHEN** `mail_folders` is called before the sync worker has recorded any folder
- **THEN** the in-scope folders are read live from iCloud and `count_source` is `live`

### Requirement: mail_search filters
`mail_search` SHALL return messages that match every given filter: `query` (full text over
subject, From, To, Cc and body, where every word must appear and a `"quoted phrase"` must appear
as a phrase), `from` (substring of the From addresses), `to` (substring of To or Cc),
`subject` (substring of the subject), `folders` (list of folder names), `since` (on or after)
and `before` (strictly before). `%` and `_` in the substring filters SHALL match themselves.
With no filters it SHALL return every indexed message in scope and in the window. It SHALL always search the local
index, never iCloud, even for a single folder.

#### Scenario: Cross-folder full-text search
- **WHEN** `Quarterly report` is in INBOX and `Re: Quarterly report` in `Work`, and a caller
  searches `query` `quarterly report`
- **THEN** both are returned, newest first, and the result's `source` is `index`

#### Scenario: Sender with a comma in the name
- **WHEN** a message is from `"Doe, Jane" <jane@example.com>` and a caller searches `from`
  `Doe, Jane`
- **THEN** the message is returned

#### Scenario: Single folder uses the index
- **WHEN** a caller searches with `folders` `["INBOX"]`
- **THEN** no IMAP `SEARCH` command is sent

#### Scenario: Wildcard characters are literal
- **WHEN** a caller searches `subject` `caf_`
- **THEN** a message with subject `Café Müller` is not returned

### Requirement: Mail matching ignores case and accents
Every `mail_search` filter SHALL match regardless of case and accents, on both the caller's text
and the message text.

#### Scenario: Folded matching
- **WHEN** a message has subject `Café Müller — PLANS`, sender `Jürgen Müller`, recipient `Zoë`
  and body `Zürich trip`
- **THEN** it is found by `subject` `cafe muller` or `CAFÉ MÜLLER`, `from` `jurgen` or `MÜLLER`,
  `to` `zoe`, and `query` `zurich`, but not by `subject` `cafe mueller`

### Requirement: Search dates and window
`since` and `before` SHALL accept an ISO date (`YYYY-MM-DD`) or datetime; a value without an
offset SHALL be read as UTC. Any other value SHALL be a tool error. A search SHALL never reach
before the index window (`mail.max_age_days` before now): when `since` is missing or earlier,
the search SHALL start at the window start and the result SHALL include `searched_since`, the
window start date.

#### Scenario: Invalid date
- **WHEN** `since` is `last tuesday`
- **THEN** the call fails with a tool error asking for an ISO date or datetime

#### Scenario: Search without since
- **WHEN** a caller searches without `since`
- **THEN** the result includes `searched_since`, the date `mail.max_age_days` days ago

### Requirement: mail_search results
Results SHALL be ordered newest first (messages without a date last). Each result SHALL have
`id`, `folder`, `uid`, `message_id`, `date` (ISO 8601, UTC), `from`, `to`, `subject`, `snippet`
(the body with whitespace collapsed, at most 300 characters, ending in `…` when cut) and
`has_attachments`. The result SHALL include `source` `index`, `offset`, `next_offset` and
`index_synced_at` (the time of the last successful sync pass). `limit` SHALL default to 20 with
a maximum of 50; `mail_search` returns no `total`.

#### Scenario: Paging a search
- **WHEN** five messages match and a caller pages with `limit` 2
- **THEN** three pages return all five messages, and the last page's `next_offset` is `null`

### Requirement: Folder arguments are scope-checked
A folder name passed to `mail_search`, or contained in an id, that is outside the account's
folder scope SHALL be a tool error `unknown folder: <name>`, a message that does not confirm the
folder exists. A name that is in scope but not in the index (for example, a folder that does not
exist) SHALL match no messages.

#### Scenario: Excluded folder named
- **WHEN** `Work` is excluded and a caller searches with `folders` `["Work"]`
- **THEN** the call fails with `unknown folder: Work`

### Requirement: Mail ids
A mail id SHALL be the URL-safe base64 encoding, without padding, of
`<folder>|<UIDVALIDITY>|<UID>`; folder names may contain `|`. An id that does not decode SHALL be
a tool error `malformed mail id`. When the folder's UIDVALIDITY no longer matches, the id SHALL be
rejected as stale (`stale id (folder UIDVALIDITY changed); search again`).

#### Scenario: Folder name containing a pipe
- **WHEN** an id is built for folder `A|B Folder`, UIDVALIDITY 42, UID 7
- **THEN** it decodes back to the same folder, UIDVALIDITY and UID

#### Scenario: Stale id
- **WHEN** a folder's UIDVALIDITY changed after a search returned an id from it
- **THEN** `mail_get` with that id fails with a tool error mentioning `stale`

### Requirement: mail_get
`mail_get` SHALL read one message live from iCloud: `EXAMINE` its folder, check UIDVALIDITY and
fetch it with `BODY.PEEK[]`, limited to the first 5 MiB. It SHALL return `id`, `folder`, `uid`,
`headers` (`message_id`, `in_reply_to`, `references`, `date`, `date_header`, `from`, `to`, `cc`,
`subject`), the plain-text `body` paged by characters (`body_offset`, `body_total_chars`,
`next_offset`), `message_truncated` (the message was larger than 5 MiB), `size_bytes`,
`attachments` (each `filename`, `content_type`, `size`), `web_url`
`https://www.icloud.com/mail/` and `web_url_note` saying that iCloud Mail has no per-message
links. A message no longer on the server SHALL be a tool error saying it was not found; a message
that cannot be parsed SHALL be a tool error.

#### Scenario: Reading a message with an attachment
- **WHEN** a caller reads a message from `"Doe, Jane" <jane@example.com>` with body
  `Numbers for Q3 attached` and a 4-byte `q3.pdf`
- **THEN** `headers.from` is `Doe, Jane <jane@example.com>`, `body` is the text, and
  `attachments` is one entry `q3.pdf`, `application/pdf`, size 4

#### Scenario: Message deleted since the search
- **WHEN** the message was expunged after the search
- **THEN** `mail_get` fails with a tool error saying the message was not found

### Requirement: Message body as plain text
The body SHALL be the message's `text/plain` part when there is one, otherwise its HTML part
converted to text. The HTML conversion SHALL never fetch anything; it SHALL drop images,
scripts, styles, `<head>` and hidden elements (`display:none`, `visibility:hidden`, `hidden`)
and zero-width characters; keep paragraphs, line breaks, lists, table rows, block quotes and
`<pre>` text in readable form; and write links as `text (url)` only for `http`, `https` and
`mailto` targets. Malformed HTML SHALL not cause an error.

#### Scenario: HTML-only message with a tracking image
- **WHEN** a message's only body is HTML with `<p>Hello <b>world</b></p>` and an image from
  `track.example`
- **THEN** the body contains `Hello world` and does not contain `track.example`

#### Scenario: Script links dropped
- **WHEN** an HTML body has a link to `javascript:steal()` with text `js`
- **THEN** the body shows `js` without the target

### Requirement: Address lists
`from`, `to` and `cc` SHALL be decoded address lists written as `Name <address>` (or the bare
address), separated by `, `, with encoded words decoded and commas inside display names kept.

#### Scenario: Encoded subject and quoted names
- **WHEN** a message's To is `"Smith, Bob" <bob@example.com>, carol@example.com` and its subject
  is `=?utf-8?q?Caf=C3=A9_plans?=`
- **THEN** `to` is `Smith, Bob <bob@example.com>, carol@example.com` and the subject is
  `Café plans`

### Requirement: mail_thread
`mail_thread` SHALL return every in-scope message linked to the given one through Message-ID,
In-Reply-To and References, directly or through other messages, across folders, oldest first,
as the same summaries `mail_search` returns, with `source` `index`, `total`, `offset` and
`next_offset`. Discovery SHALL stop after 1,000 messages. When the given message is not yet in
the index, it SHALL be read live and indexed first. `limit` SHALL default to 50 with a maximum
of 100.

#### Scenario: Thread across folders
- **WHEN** a reply in `Work` references a message in INBOX
- **THEN** `mail_thread` on the reply returns the INBOX message, then the reply

#### Scenario: Message newer than the last sync
- **WHEN** a new reply arrived after the last sync and a caller passes its id
- **THEN** the reply is read live, indexed, and returned with the rest of its thread

#### Scenario: Limit above the maximum
- **WHEN** `limit` is 101
- **THEN** the call fails with a tool error mentioning `limit`
