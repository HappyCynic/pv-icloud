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

Modules touched: `config`, `auth`, `server`, `mail/service`, `mail/sync`, new
`mail/imap_write`, new `mail/smtp`. `dav`, `cal/`, `contacts/` and `people` are not touched.

## Goals / Non-Goals

**Goals:**

- Keep the read path provably unchanged: a token without `mail_write` produces byte-for-byte
  the same `tools/list`, instructions and IMAP command stream as today.
- Make the set of IMAP commands that can change mail small, explicit and confined to one
  module, so review and static tests can still enforce it.
- Make destructive outcomes impossible rather than discouraged: no command that can
  permanently remove mail or change flags exists anywhere in the code.

**Non-Goals:**

- Batch actions (several ids per call). Single-id tools keep "change nothing on failure"
  simple; batching can be a later change.
- Attachments, HTML bodies, forwarding with the original attached, or editing/sending an
  existing draft by id (that would need removing the draft afterwards, i.e. `STORE` +
  `EXPUNGE`).
- Undo. `mail_move` and `mail_archive` return enough to move a message back by hand.

## Decisions

### 1. Permissions live on the token, as a list of actions

```yaml
accounts:
  - id: example
    tokens:
      - { label: laptop, sha256: "<hex>", mail_write: [move, archive, delete, draft] }
      - { label: phone,  sha256: "<hex>" }                     # read-only, as today
      - { label: agent,  sha256: "<hex>", mail_write: [draft], protected_folders: [Work] }
    mail:
      write:
        protected_folders: [Taxes, Banking]
        archive_folder: Archive        # optional; see decision 4
        send_from: someone@example.com # required only if some token has `send`
```

`TokenEntry` gains `mail_write: frozenset[str]` and `protected_folders: tuple[str, ...]`;
`AccountContext` carries both into each request. Unknown action names, non-list values and a
token with `send` on an account without `send_from` fail startup with a `config error`.
*Alternative considered:* per-account switches. Rejected by the issue: two tokens on the same
Apple ID must be able to differ.

### 2. Protected folders: account list ∪ token list (issue's open question)

The effective list for a call is `defaults.mail.write.protected_folders`, plus the account's
own `mail.write.protected_folders`, plus the token's own `protected_folders`. Unlike other
list keys, an account list adds to the defaults instead of replacing them, so no layer can
loosen a fence set above it. A token
cannot remove an account entry. Names are compared exactly, except `INBOX`, which IMAP treats
case-insensitively. The check runs before any IMAP command is sent, on both the source folder
(decoded from the id) and the destination, so a refused call changes nothing.
*Alternatives:* account-only (simpler, but an operator cannot give a less trusted token a
tighter fence), token-only (an operator could forget to protect a folder on one token).
The union is the only option that can never be less safe than either list alone.

### 3. A separate write path with three new IMAP commands

New module `mail/imap_write.py` holds the only code allowed to send `SELECT`, `UID MOVE` and
`APPEND`. `mail/imap.py` keeps its read-only docstring and gains nothing.

- **Move, archive, delete** use `SELECT <source>` then `UID MOVE <uid> <destination>`
  (RFC 6851). `UID MOVE` needs a read-write selection, which is why `SELECT` is needed; it
  does not change `\Seen`. If the server does not advertise `MOVE` the tool fails with
  "this iCloud server does not support moving messages" and nothing is sent. There is **no
  fallback** to `COPY` + `STORE \Deleted` + `EXPUNGE`: that fallback is exactly the set of
  commands that can permanently delete mail, and keeping them out of the code entirely is
  what makes "delete never permanently removes mail" true by construction.
- **Draft** uses `APPEND <Drafts> (\Draft \Seen) <message>`; **send** appends the sent copy
  to `Sent Messages` with `(\Seen)`. `APPEND` creates a new message; setting flags on a
  message we are creating is not `STORE`, which stays forbidden.
- Before a move, UIDVALIDITY from `SELECT` is checked against the id, like `mail_get`
  does with `EXAMINE`; a mismatch is the existing `stale id` error.
- When the server returns `COPYUID`/`APPENDUID` (UIDPLUS) the result carries the new mail
  id; otherwise `new_id` is `null`.
- A connection that has run `SELECT` is never returned to the read pool: the write path
  borrows a pool slot, logs out after the write, and the pool opens a fresh connection when
  next needed. Writes are rare, so the extra login is cheap, and no read can ever run on a
  read-write selection. (`CLOSE` would expunge and `UNSELECT` is not guaranteed on iCloud.)

Forbidden everywhere, enforced by a static test over `src/`: `STORE`, `COPY`, `EXPUNGE`,
`UID EXPUNGE`, `CLOSE`, `CREATE`, `DELETE`, `RENAME`, `SUBSCRIBE`, `UNSUBSCRIBE`. `SELECT`,
`MOVE` and `APPEND` may appear only in `mail/imap_write.py`.
*Alternative considered:* adding write methods to `ImapClient`. Rejected: the read-only
guarantee for the read path would then rest on call sites rather than on module boundaries,
and the existing CODEOWNERS entry and static tests could no longer tell the paths apart.

### 4. Finding Archive, Deleted Messages, Drafts and Sent

Destinations are resolved from `LIST` special-use attributes (RFC 6154: `\Archive`,
`\Trash`, `\Drafts`, `\Sent`), falling back to iCloud's names `Archive`, `Deleted Messages`,
`Drafts` and `Sent Messages`. `mail.write.archive_folder` overrides the archive folder. If
the folder cannot be found the tool fails and changes nothing. These fixed destinations are
allowed even when hidden from the read scope by `mail.special_folders` (Deleted Messages and
Drafts are hidden by default); `mail_move`'s caller-chosen destination must be in scope, so a
caller cannot move mail into an excluded folder it could then not see.

`mail_delete` on a message already in Deleted Messages is a tool error ("already in Deleted
Messages"), never a permanent delete.

### 5. Sending through iCloud SMTP

`mail/smtp.py` uses the standard library's `smtplib.SMTP` to `smtp.mail.me.com:587`, requires
STARTTLS with certificate verification, logs in with the account's Apple ID and app-specific
password and a 30-second timeout. The message is built with `email.message.EmailMessage`:
`From` is `mail.write.send_from`, plain-text UTF-8 body, a generated `Message-ID` and `Date`.
With `in_reply_to` (a mail id) the original is read on the existing read path (`EXAMINE` and
`BODY.PEEK[]`, as `mail_get` does) to set `In-Reply-To`, `References` and a `Re:` subject
when none is given. After the server accepts the message a copy is appended to Sent
Messages; if that append fails the tool still reports success with `sent_copy_saved: false`,
since the mail has gone. `Bcc` addresses go into the SMTP envelope but never into the headers
sent over SMTP; the copy in Sent Messages keeps the `Bcc` header, as Apple Mail does, so the
operator can see who received it.

### 6. Per-token tool list and instructions

The server keeps one FastMCP instance but overrides its `tools/list` handler to filter by the
caller's `mail_write`, and every write tool checks the permission again at call time (a call
to a tool not granted gets the same "unknown tool" error as a misspelt name, so it does not
reveal what exists). The `initialize` instructions are chosen per request: a token without
`mail_write` gets today's text unchanged; otherwise the text names the granted actions,
states that delete only moves to Deleted Messages, that Calendar and Contacts are read-only,
and that untrusted content must never be the reason to move, delete, draft or send.

Annotations: `mail_move` and `mail_archive` `readOnlyHint: false, destructiveHint: false,
idempotentHint: true`; `mail_delete` `destructiveHint: true, idempotentHint: true`;
`mail_draft` `destructiveHint: false, idempotentHint: false`; `mail_send`
`destructiveHint: true, idempotentHint: false`. All `openWorldHint: true`.

### 7. Limits

`to`, `cc` and `bcc` together at most 50 addresses, each a single valid address; `subject` at
most 998 characters with no line breaks; `body` at most 100,000 characters. These limits are
written into the tool descriptions. No new paging: every write tool returns a single result.

### 8. Audit and index

Write tools use the existing audit line with an added `write: true`. The existing redaction
rules already reduce `id`, `folder`, `subject` and `body` to `<N chars>` and `to`/`cc`/`bcc`
to `<N items>`; a test pins that no address, subject, body or folder name appears. After a
successful write the account's sync worker is asked to resync the source and destination
folders, so `mail_search` catches up without waiting for the 5-minute pass.

## Risks / Trade-offs

- [Prompt injection in untrusted mail can now cause actions, e.g. "forward this to …" or
  "delete all mail from …"] → Every action is off by default and granted per token; delete
  can only move to Deleted Messages; no batch tools; protected folders; instructions and
  tool descriptions say untrusted content is never a reason to act; SECURITY.md tells
  operators to grant `send` only to clients that confirm each send with a person.
- [A bug in the write path could change mail unexpectedly] → Three commands only, confined
  to one CODEOWNERS-protected module; static tests forbid the rest; the fake IMAP server
  rejects any command outside the allowed lists so tests fail loudly.
- [`SELECT` clears `\Recent` in the source folder] → Accepted: `\Recent` is session state
  that iCloud clients do not show; `\Seen` and other flags are untouched.
- [iCloud may not advertise `MOVE` or `UIDPLUS`] → Without `MOVE` the tools refuse; without
  UIDPLUS `new_id` is `null` and the next sync finds the message. The implementation must
  record in the fake what iCloud actually advertises, per CLAUDE.md.
- [iCloud might save its own copy of SMTP-sent mail, leaving two in Sent Messages] →
  Open question below; the fake and the code follow whichever is true.
- [The capability name `read-only-and-egress` no longer describes Mail] → Kept to avoid a
  spec rename in the same change; its Purpose text is updated at archive time.

## Migration Plan

No migration: new keys default to off, so existing configs behave exactly as before.
Operators opt in by adding `mail_write` to a token and, for `send`, `mail.write.send_from`
and an egress rule for `smtp.mail.me.com:587`. Rollback is removing `mail_write` and
restarting.

## Open Questions

- Does iCloud's SMTP server save a copy of sent mail in Sent Messages by itself? If it does,
  the explicit `APPEND` is skipped. This must be checked against a real account by the
  maintainer; it changes one step, not the specs.
- Does iCloud advertise `MOVE` and `UIDPLUS`? Expected yes; the fake records the answer.
