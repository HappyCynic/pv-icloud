# mail-index-and-sync Specification

## Purpose
The per-account local mail index that mail search and `find_person` run on, and the background
worker that fills it from iCloud and keeps it current: backfill window, change detection with
CONDSTORE and IDLE, folder scope and the special folders.

## Requirements

### Requirement: Per-account full-text index
Each account SHALL have its own SQLite FTS5 index at `<data dir>/<account id>/mail.sqlite3`.
For every indexed message it SHALL store the folder, UIDVALIDITY, UID, Message-ID, In-Reply-To,
the References and In-Reply-To ids, the date, decoded From, To and Cc, subject, a snippet of at
most 300 characters, whether it has attachments and its size, and SHALL index the subject, From,
To plus Cc, and the first 100,000 characters of the plain-text body for full-text search, with
diacritics removed by the tokenizer. Besides these rows the index SHALL hold only each folder's
recorded sync state (UIDVALIDITY, HIGHESTMODSEQ, UIDNEXT, message count, time of last sync) and
its schema version.

#### Scenario: Index file location
- **WHEN** the server starts with account `alice` and data directory `data`
- **THEN** the index for `alice` is `data/alice/mail.sqlite3`

### Requirement: Backfill window
The sync worker SHALL index only messages that iCloud reports for `UID SEARCH SINCE` the window
start, which is today (UTC) minus `mail.max_age_days` (default 730). Messages that leave the
window SHALL be removed from the index the next time their folder changes.

#### Scenario: Old mail is not indexed
- **WHEN** a folder holds one message from 800 days ago and one from 3 days ago, with the
  default window
- **THEN** only the message from 3 days ago is indexed

### Requirement: Sync passes
Each account SHALL have one sync worker thread, started with the server. Each pass SHALL list
the account's folders, drop index rows for folders no longer listed or no longer in scope, and
sync every in-scope folder, `INBOX` first and then by name. Passes SHALL repeat every 300
seconds. A folder that fails (for example, iCloud refuses to open it) SHALL be recorded in
`folder_errors` and SHALL NOT stop the pass.

#### Scenario: One folder fails
- **WHEN** iCloud refuses to open folder `Work` during a pass
- **THEN** the other folders are still synced and `Work` appears in `folder_errors`

### Requirement: Change detection with CONDSTORE
After login the worker SHALL send `ENABLE CONDSTORE` when the server advertises it. For each
folder it SHALL `EXAMINE` the folder and skip it, without further commands, when the server
reports `HIGHESTMODSEQ` and the folder's UIDVALIDITY, HIGHESTMODSEQ, UIDNEXT and message count
all equal the values recorded after the previous sync. Otherwise it SHALL run one
`UID SEARCH SINCE <window start>`, remove from the index the UIDs the server no longer reports
(expunged, moved or aged out), fetch the UIDs only the server has, newest first, in batches of
50 with `BODY.PEEK[]` limited to the first 256 KiB of each message, index them, and record the
folder's new state.

#### Scenario: Unchanged folder
- **WHEN** nothing changed in any folder since the previous pass
- **THEN** the next pass sends no `UID SEARCH` and no `UID FETCH`

#### Scenario: New, deleted and moved mail
- **WHEN** after a pass one INBOX message is expunged, another is moved to `Work`, and a new one
  arrives in INBOX
- **THEN** after the next pass the index has the moved message under `Work`, the new message
  under INBOX, and no row for the expunged one

### Requirement: UIDVALIDITY change re-indexes a folder
When a folder's UIDVALIDITY differs from the recorded one, the worker SHALL drop all of the
folder's index rows and index it again from scratch.

#### Scenario: Folder recreated
- **WHEN** iCloud reports a new UIDVALIDITY for `Work`
- **THEN** after the next pass `Work`'s messages are indexed under the new UIDVALIDITY

### Requirement: IDLE on INBOX between passes
Between passes, when the server advertises `IDLE` and `INBOX` is in scope, the worker SHALL
`EXAMINE` INBOX and wait with `IDLE` in chunks of at most 120 seconds. An `EXISTS`, `EXPUNGE`,
`FETCH` or `VANISHED` response SHALL trigger an immediate sync of INBOX. Without IDLE support
the worker SHALL simply wait for the next pass.

#### Scenario: New mail pushed during IDLE
- **WHEN** iCloud sends `EXISTS` while the worker is idling on INBOX
- **THEN** INBOX is synced at once and the new message becomes searchable before the next pass

### Requirement: Folder scope
An account's folders SHALL be in scope when their display name (decoded from IMAP modified
UTF-7) is matched by `mail.folders.include` (`"*"` or a list of names or globs, default `"*"`),
is not matched by any glob in `mail.folders.exclude`, and is not a hidden special folder. Glob
matching SHALL be case-sensitive. Folders flagged `\Noselect` or `\NonExistent` SHALL be
ignored. A folder that leaves the scope SHALL have its rows purged from the index on the next
pass, and SHALL be treated as unknown by every mail tool at once.

#### Scenario: Excluded folder purged
- **WHEN** `Work` was indexed and the account's config is changed to exclude `Work` and the
  server restarted
- **THEN** after the next pass the index holds no `Work` rows and a search naming `Work` fails
  with `unknown folder: Work`

#### Scenario: Non-ASCII folder names
- **WHEN** iCloud lists the folder `Friends &- Family` or `B&APw-cher`
- **THEN** the folder is known as `Friends & Family` or `Bücher`

### Requirement: Special folders hidden by default
`Junk`, `Deleted Messages` and `Drafts` SHALL be out of scope, whatever `include` says, unless
`mail.special_folders.junk`, `mail.special_folders.deleted_messages` or
`mail.special_folders.drafts` is `true` for the account. A special folder that is turned on
SHALL be treated like any other folder, so `include` and `exclude` still apply to it.

#### Scenario: Default hides special folders
- **WHEN** an account uses the default configuration
- **THEN** mail in `Junk`, `Drafts` and `Deleted Messages` is not indexed or returned

#### Scenario: Junk turned on
- **WHEN** an account sets `mail.special_folders.junk: true` and includes every folder
- **THEN** `Junk` is indexed and searchable, and `Deleted Messages` stays hidden

#### Scenario: Turned on but not included
- **WHEN** an account sets `junk: true` and `folders.include: ["IN*"]`
- **THEN** `Junk` stays out of scope

### Requirement: Malformed messages do not stop the sync
A message that cannot be parsed SHALL be indexed as a stub with subject `(unparseable message)`
so it is not fetched again on every pass, and SHALL be counted in `malformed_messages`. Message
parsing SHALL otherwise degrade rather than fail: undecodable headers and parts are kept as far
as possible.

#### Scenario: One broken message among good ones
- **WHEN** a folder holds two good messages and one that the parser rejects
- **THEN** both good messages are searchable, the bad one has a stub row, and
  `malformed_messages` is 1

### Requirement: Message date fallback
A message without a parseable `Date` header SHALL be dated by the server's receipt time
(`INTERNALDATE`) so date-filtered searches still find it. When an index created by schema
version 1 is opened, rows without a date SHALL be deleted so the next sync fetches them again.

#### Scenario: Message without a Date header
- **WHEN** a message without a `Date` header arrived 3 days ago
- **THEN** it is indexed with that arrival date and found by a search

### Requirement: Sync failure back-off and status
When a sync session fails at the connection level, the worker SHALL set its state to `error`,
record the error, increase `consecutive_failures`, wait `min(900, 30 × 2^(failures − 1))`
seconds and reconnect; a successful pass SHALL reset the failure count. The worker's status SHALL
report `state` (`starting`, `backfilling` during the first pass, `ok`, `error` or `stopped`),
`last_success`, `last_error`, `consecutive_failures`, `passes`, `indexed_messages`,
`malformed_messages` and `folder_errors`, and is shown only through the caller's own
`/healthz` detail.

#### Scenario: Connection keeps failing
- **WHEN** the IMAP login fails three times in a row
- **THEN** the worker waits 30, 60 and then 120 seconds between attempts and reports state
  `error`

### Requirement: IMAP connection budget
Each account SHALL use at most `mail.max_connections` IMAP connections (default 2, minimum 2):
one for its sync worker and the rest in a pool for live tool calls (`mail_get`, `mail_thread`,
and `mail_folders` before the first sync). A tool call that waits more than 30 seconds for a
pooled connection SHALL fail. A pooled connection unused for 60 seconds or more SHALL be checked
with `NOOP` before reuse and replaced if it fails. IMAP connections SHALL use a 30-second
timeout.

#### Scenario: Pool exhausted
- **WHEN** every pooled connection is busy for more than 30 seconds
- **THEN** a waiting `mail_get` fails with the generic iCloud-unavailable tool error
