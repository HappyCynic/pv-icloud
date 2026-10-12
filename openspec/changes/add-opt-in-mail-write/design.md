# Design

## Context

See proposal.md for motivation. Today the server is read-only by construction: the IMAP
client (`src/pv_icloud/mail/imap.py`) opens folders only with `EXAMINE` and fetches only with
`BODY.PEEK[]`, the DAV base (`src/pv_icloud/dav.py`) sends only `PROPFIND` and `REPORT`, and
`tests/test_mail.py` checks the commands the fake IMAP server sees against the allowed list.
Every tool is registered once on a single FastMCP instance with `READ_ONLY` annotations and a
fixed `INSTRUCTIONS` string (`src/pv_icloud/server.py`). The caller's account comes from a
context variable set by the auth middleware (`src/pv_icloud/auth.py`), which today knows the
token's account and label but nothing else about the token.

The maintainer checked a live iCloud account: it advertises `UIDPLUS` and `UNSELECT`, not
`MOVE`; Drafts and Archive have no special-use attribute but exist under those names.

Modules touched: `config`, `auth`, `server`, `mail/service`, `mail/sync`, new
`mail/imap_write`, new `mail/smtp`, new `ratelimit`. `mail/imap`, `dav`, `cal/`, `contacts/`
and `people` are not touched.

Prerequisite, outside this change: the maintainer's own pull request updates CLAUDE.md's
read-only hard rule, `.github/CODEOWNERS` (adding `mail/imap_write.py` and `mail/smtp.py`),
`openspec/config.yaml`, SECURITY.md and adds an ADR. Implementation starts after it merges.

## Goals / Non-Goals

**Goals:**

- Keep the read path provably unchanged: a token without `mail_write` produces byte-for-byte
  the same `tools/list`, instructions and IMAP command stream as today.
- Make write support impossible to turn on by accident: a deployment switch, then per-token
  grants.
- Make the set of IMAP commands that can change mail small, exact and confined to one
  module, so review and static tests can enforce it.
- Never lose mail: a message leaves its source only after its copy in the destination is
  confirmed, and nothing ever removes a message from Deleted Messages.

**Non-Goals:**

- Batch actions (several ids per call). Single-id tools keep failure handling simple and
  limit what a prompt-injected client can do per call.
- Attachments, HTML bodies, forwarding with the original attached, or editing/sending an
  existing draft by id.
- Undo. Move results carry `new_id`, so a message can be moved back by hand.

## Decisions

### 1. Deployment switch

`PV_ICLOUD_MAIL_WRITE` is read at startup. `enabled` turns write support on; unset, empty or
`disabled` leave it off; any other value is a config error (so a typo such as `true` is not
silently ignored). When off, `server.py` never imports `mail/imap_write.py`, `mail/smtp.py`
or registers a write tool, and a token with a non-empty `mail_write` is a config error naming
the account, the token label, `mail_write` and `PV_ICLOUD_MAIL_WRITE`. The other write keys
(`protected_folders`, `send_to`, `max_writes_per_hour`, `mail.write`) are still validated but
have no effect. *Why:* an operator who copies a config with grants onto a host where writes
should be off gets a loud failure, not silent write access.

### 2. Permissions live on the token

```yaml
accounts:
  - id: example
    tokens:
      - { label: laptop, sha256: "<hex>", mail_write: [move, archive, delete, draft] }
      - { label: phone,  sha256: "<hex>" }                     # read-only, as today
      - label: agent
        sha256: "<hex>"
        mail_write: [draft, send]
        send_to: [someone@example.com, "@example.org"]
        protected_folders: [Work]
        max_writes_per_hour: 10
    mail:
      write:
        protected_folders: [Taxes, Banking]
        archive_folder: Archive        # optional, default "Archive"
        send_from: someone@example.com # required only if some token has `send`
```

`TokenEntry` gains `mail_write: frozenset[str]`, `protected_folders: tuple[str, ...]`,
`send_to: tuple[str, ...] | None` and `max_writes_per_hour: int`; `AccountContext` carries
them into each request. Validation errors name the account, token label and key:

- `mail_write` not a list, or an unknown action;
- `protected_folders` not a list of strings;
- `send_to` present on a token without `send`, empty, or with an entry that is neither a
  single bare address nor `@` followed by a domain;
- `max_writes_per_hour` not a positive integer;
- `send` on an account without `mail.write.send_from`.

At startup, each token with `send` and no `send_to` gets a warning: `token <label> of account
<id> may send mail to any recipient (no send_to)`. Labels and account ids are already logged
in audit lines, so this logs nothing new.

*Alternative considered:* per-account switches. Rejected by the issue: two tokens on the same
Apple ID must be able to differ.

### 3. Protected folders: defaults ∪ account ∪ token

The effective list for a call is `defaults.mail.write.protected_folders`, plus the account's
`mail.write.protected_folders`, plus the token's `protected_folders`. Unlike other list
keys, an account list adds to the defaults instead of replacing them, so no layer can loosen
a fence set above it. Names are compared exactly, except `INBOX`, which IMAP treats
case-insensitively. The check runs before any IMAP command is sent, on both the source folder
(decoded from the id) and the destination, so a refused call changes nothing.

### 4. Guarded move on a separate write path

New module `mail/imap_write.py` holds the only code allowed to send `SELECT`, `UID COPY`,
`UID STORE`, `UID EXPUNGE` and `APPEND`. `mail/imap.py` keeps its read-only docstring and is
not changed.

Move, archive and delete all run the same sequence on one connection borrowed from the
account's pool:

1. Check the cached capabilities for `UIDPLUS`. Without it, fail with "this iCloud server
   cannot move messages safely" before sending anything else. (`UID EXPUNGE` and `COPYUID`
   both come from UIDPLUS; without it the only expunge is the folder-wide `EXPUNGE`.)
2. `SELECT <source>`; compare UIDVALIDITY with the id (mismatch: the existing `stale id`
   error) and confirm the UID exists (`UID FETCH <uid> (UID FLAGS)`); otherwise "not found".
3. `UID COPY <uid> <destination>`. Require a `COPYUID` response whose UIDVALIDITY is the
   destination's and whose source and destination sets are each exactly one UID, the source
   one being ours. If the command fails, nothing has changed. If it succeeds without a
   matching `COPYUID`, stop: the tool error says the copy could not be confirmed, the
   message was left in its source folder, and a copy may also now be in the destination.
4. `UID STORE <uid> +FLAGS.SILENT (\Deleted)`. If it fails, stop: the tool error says the
   message is now in both folders.
5. `UID EXPUNGE <uid>`. This removes only that UID, never other messages someone else marked
   `\Deleted`. If it fails, the tool error says the message is in the destination and still
   in the source, marked deleted.
6. `LOGOUT`. A connection that has run `SELECT` is never returned to the read pool; the pool
   opens a fresh connection when next needed. Writes are rare, so the extra login is cheap,
   and no read can run on a read-write selection. `CLOSE` is never used, because it expunges
   every `\Deleted` message in the folder.

The copy keeps the message's flags (`\Seen` included) because `UID COPY` runs before
`\Deleted` is set. The result's `new_id` comes from `COPYUID`, so it is always known.

Draft and send use `APPEND <folder> (<flags>) <message>`: `(\Draft \Seen)` into Drafts and
`(\Seen)` into Sent Messages, with the new id from `APPENDUID`. Setting flags on a message
being created is not `STORE`.

**Allowed write forms, exactly.** `SELECT`; `UID COPY` with one UID; `UID STORE` with one UID
and exactly `+FLAGS.SILENT (\Deleted)`; `UID EXPUNGE` with one UID; `APPEND`. All only in
`mail/imap_write.py`. **Forbidden everywhere:** `STORE`, `COPY`, `EXPUNGE` (non-UID),
`CLOSE`, `MOVE`/`UID MOVE`, any UID set or range, any other flag or `-FLAGS`/`FLAGS`,
`CREATE`, `DELETE`, `RENAME`, `SUBSCRIBE`, `UNSUBSCRIBE`. A static test in
`tests/test_repo_policy.py` inspects IMAP call sites (`imaplib` method calls and the command
names and arguments passed to `.uid(`, `._simple_command(` or `.xatom(`), not plain text,
since SQL `DELETE` and the string `COPYUID` are legitimate elsewhere. The fake IMAP server
rejects any other form, so tests also fail at run time.

*Alternatives considered:* `UID MOVE` (atomic, but iCloud does not advertise it); refusing to
move at all without `MOVE` (would make the feature unusable on iCloud); adding write methods
to `ImapClient` (the read path's guarantee would then rest on call sites, not on a module
boundary that CODEOWNERS and static tests can see).

### 5. Finding Archive, Deleted Messages, Drafts and Sent

- Archive: `mail.write.archive_folder`, default `Archive`, by name.
- Drafts: `Drafts`, by name.
- Deleted Messages: the folder `LIST` marks `\Trash`, otherwise `Deleted Messages`.
- Sent Messages: the folder `LIST` marks `\Sent`, otherwise `Sent Messages`.

iCloud gives Drafts and Archive no special-use attribute, so they are found by name only;
the fake mirrors this. If a folder is missing, the tool fails and changes nothing. These
fixed destinations are allowed even when hidden from the read scope by
`mail.special_folders` (Deleted Messages and Drafts are hidden by default); `mail_move`'s
caller-chosen destination must be in scope, so a caller cannot move mail into an excluded
folder it could then not see.

`mail_delete` on a message already in Deleted Messages is a tool error, never a further
action. Deleted mail stays recoverable in Deleted Messages until iCloud empties that folder
(iCloud's own retention); tool descriptions say so.

### 6. Per-token write rate limit

A small in-memory limiter (`src/pv_icloud/ratelimit.py`) keeps, per token hash, the times of
write calls in the last hour (sliding window). Every call to a granted write tool counts,
whatever its outcome, and the check runs first, before argument validation and before any
IMAP or SMTP traffic. Over the limit, the call fails with `write limit reached for this
token (N per hour); try again in about M minutes`, and the audit outcome is `throttled`. The
limit is per token, so two tokens of one account have separate budgets. It resets on
restart; that is acceptable because it guards against a runaway or prompt-injected client,
not against the operator.

### 7. Sending through iCloud SMTP, with `send_to`

`mail/smtp.py` uses the standard library's `smtplib.SMTP` to `smtp.mail.me.com:587`, requires
STARTTLS with certificate verification before `AUTH`, logs in with the account's Apple ID
and app-specific password, with a 30-second timeout. The message is built with
`email.message.EmailMessage`: `From` is `mail.write.send_from`, plain-text UTF-8 body, a
generated `Message-ID` and `Date`.

Order inside `mail_send`: rate limit; argument limits; `send_to` check; read the original if
`in_reply_to` is given; SMTP; save the sent copy. The `send_to` check lower-cases each
recipient: an exact entry matches the whole address; an `@domain` entry matches when the
part after the last `@` equals the domain exactly (so `@example.org` does not match
`a@mail.example.org`). Every To, Cc and Bcc address must match. Otherwise the call fails
with `recipients not allowed for this token: <addresses>`, listing only the refused
addresses, and nothing is sent or saved. The audit line still shows only counts. The
original sender of a message being replied to gets no exception.

With `in_reply_to` (a mail id) the original is read on the existing read path (`EXAMINE` and
`BODY.PEEK[]`, as `mail_get` does) to set `In-Reply-To`, `References` and a `Re:` subject
when none is given. After the server accepts the message, a copy is appended to Sent
Messages; if that append fails the tool still reports success with `sent_copy_saved: false`,
since the mail has gone. `Bcc` addresses go into the SMTP envelope but never into the headers
sent over SMTP; the copy in Sent Messages keeps the `Bcc` header, as Apple Mail does.

`send_to` does not apply to `mail_draft`: a draft sends nothing, and only a person in a mail
client can send it.

### 8. Per-token tool list and instructions

When the switch is on, the server registers the write tools on the same FastMCP instance
but overrides its `tools/list` handler to filter by the caller's `mail_write`. Every write
tool checks the permission again at call time; a call to a tool not granted gets the same
"unknown tool" error as a misspelt name, so it does not reveal what exists. The `initialize`
instructions are chosen per request: a token without `mail_write` gets today's text
unchanged; otherwise the text names the granted actions, states that delete only moves to
Deleted Messages, that Calendar and Contacts are read-only, and that untrusted content must
never be the reason to move, archive, delete, draft or send.

Annotations: `mail_move` and `mail_archive` `readOnlyHint: false, destructiveHint: false,
idempotentHint: false`; `mail_delete` `destructiveHint: true, idempotentHint: false`;
`mail_draft` `destructiveHint: false, idempotentHint: false`; `mail_send`
`destructiveHint: true, idempotentHint: false`. All `openWorldHint: true`. (A second move of
the same id fails as not found, so none of them is idempotent.)

### 9. Limits

`to`, `cc` and `bcc` together at most 50 addresses, each a single valid address; `subject` at
most 998 characters with no line breaks; `body` at most 100,000 characters; the per-token
hourly write limit (default 30). These are written into the tool descriptions. No new paging:
every write tool returns a single result.

### 10. Audit and index

Write tools use the existing audit line with an added `write: true`. The existing redaction
rules already reduce `id`, `folder`, `subject` and `body` to `<N chars>` and `to`/`cc`/`bcc`
to `<N items>`; a test pins that no address, subject, body or folder name appears, including
for refused `send_to` checks. After a successful write the account's sync worker is asked to
resync the folders involved, so `mail_search` catches up without waiting for the 5-minute
pass.

## Risks / Trade-offs

- [Prompt injection in untrusted mail can now cause actions, e.g. "forward this to …" or
  "delete all mail from …"] → Deployment switch; every action off by default and granted per
  token; `send_to` allowlist with a startup warning when absent; per-token rate limit; delete
  only moves to Deleted Messages; no batch tools; protected folders; instructions and tool
  descriptions say untrusted content is never a reason to act.
- [The guarded move is not atomic] → Each step is checked before the next. The worst
  outcomes are a duplicate (step 4 fails) or a source copy marked `\Deleted` next to a
  confirmed destination copy (step 5 fails); a message is never lost. The tool error says
  which happened.
- [`UID STORE` and `UID EXPUNGE` exist in the code at all] → Only the exact single-UID forms,
  only in one CODEOWNERS-protected module, only after a confirmed `COPYUID`; static tests and
  the fake reject every other form; plain `EXPUNGE` and `CLOSE` stay forbidden.
- [`SELECT` clears `\Recent` in the source folder] → Accepted: `\Recent` is session state
  that iCloud clients do not show; `\Seen` and other flags are untouched.
- [iCloud might save its own copy of SMTP-sent mail, leaving two in Sent Messages] → Open
  question below; the fake and the code follow whichever is true.
- [The rate limit resets on restart] → Accepted; see decision 6.
- [The capability name `read-only-and-egress` no longer describes Mail] → Kept to avoid a
  spec rename in the same change; its Purpose text is updated at archive time.

## Migration Plan

No migration: new keys default to off, so existing configs behave exactly as before.
Operators opt in by setting `PV_ICLOUD_MAIL_WRITE=enabled`, adding `mail_write` to a token
and, for `send`, `mail.write.send_from`, ideally `send_to`, and an egress rule for
`smtp.mail.me.com:587`. Rollback is unsetting the switch, removing `mail_write` and
restarting.

## Open Questions

- Does iCloud's SMTP server save a copy of sent mail in Sent Messages by itself? If it does,
  the explicit `APPEND` is skipped. This needs checking against a real account; it changes
  one step, not the specs.
- Does iCloud's `\Trash` and `\Sent` marking match what the fake will record? The name
  fallbacks cover it either way.
