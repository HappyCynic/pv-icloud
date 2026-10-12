# Proposal

## Why

Implements #14. MCP clients can search and read iCloud Mail through pv-icloud but cannot act
on it, so triage (filing, archiving, cleaning up) and replying still happen in another app.
Different clients deserve different trust, so write access has to be granted per bearer
token, not per server, and has to be off unless an operator turns it on.

## What Changes

- **This change ends the read-only guarantee for Mail.** It needs the design discussion that
  CLAUDE.md requires before any write support; this proposal (and its `design.md`) is that
  discussion. Calendar and Contacts stay read-only: the DAV base still sends only `PROPFIND`
  and `REPORT`.
- **Deployment switch:** `PV_ICLOUD_MAIL_WRITE=enabled` (default off). Without it the write
  modules are never imported and any `mail_write` grant in the config is a config error.
- Five new mail tools, each enabled independently per bearer token and all off by default:
  - `mail_move`: move one message to another in-scope folder.
  - `mail_archive`: move one message to the account's `Archive` folder.
  - `mail_delete`: move one message to `Deleted Messages`. The message stays recoverable
    there until iCloud empties that folder; pv-icloud never removes it from there.
  - `mail_draft`: save a new plain-text message (optionally a reply) in `Drafts`.
  - `mail_send`: send a new plain-text message (optionally a reply) through iCloud's SMTP
    server and keep a copy in `Sent Messages`.
- New per-token config: `mail_write: [move, archive, delete, draft, send]` (default empty),
  `protected_folders`, `send_to` (optional recipient allowlist for `send`: exact addresses
  or `@domain` entries) and `max_writes_per_hour` (default 30). Tools a token has not been
  granted are left out of its `tools/list` and refused if called anyway. A token without
  `mail_write` sees exactly today's tools, annotations and server instructions.
- A token with `send` but no `send_to` may send to anyone, and the server logs a startup
  warning naming the token label. With `send_to`, every To, Cc and Bcc address must match
  before anything is sent or saved. Replying does not automatically allow the original
  sender.
- New per-account config under `mail.write`: `protected_folders` (messages cannot be moved,
  archived or deleted into or out of them), `archive_folder` and `send_from`. Tokens can add
  further protected folders but never remove the account's.
- **Guarded move.** iCloud does not advertise `MOVE`, so move, archive and delete use, on
  one selected folder and one UID only: `UID COPY`, a required `COPYUID` response, then
  `UID STORE <uid> +FLAGS.SILENT (\Deleted)` and `UID EXPUNGE <uid>`. **A message is removed
  from its source only after its copy in the destination is confirmed.** Plain `STORE`,
  `COPY`, `EXPUNGE`, `CLOSE`, `MOVE` and multi-UID forms stay forbidden. New IMAP commands
  are used only on a separate write path (`mail/imap_write.py`): `SELECT`, those three
  single-UID forms and `APPEND`. The read path is unchanged (`EXAMINE` and `BODY.PEEK`
  only), and read tools still never change `\Seen` or other flags.
- New outbound host, for `mail_send` only: `smtp.mail.me.com` port 587 (STARTTLS).
- Every write is rate limited per token and audit-logged (action, account, token label,
  outcome, `write: true`) with no message content, addresses, subjects or folder names.
- Docs: README.md, docs/design.md, docs/egress.md and CONTRIBUTING.md are updated with the
  implementation. The rule changes (CLAUDE.md's read-only hard rule, `.github/CODEOWNERS`,
  `openspec/config.yaml`, SECURITY.md and an ADR) are made by the maintainer in a separate
  pull request **before** implementation starts; they are not tasks of this change.

Out of scope (unchanged from the issue): permanent delete; flagging and marking read or
unread; folder create, rename or delete; Calendar and Contacts writes. Also out of scope
here: attachments, HTML bodies, batch (multi-id) actions and sending an existing draft by id.

### Decisions from the maintainer's review on #14

- Protected folders: **both** account and token lists, as a union, so a token can only
  tighten protection.
- iCloud advertises `UIDPLUS` and `UNSELECT` but not `MOVE` (checked against a live account),
  so moves use the guarded copy-then-expunge sequence above, and `UIDPLUS` is required.
- `draft` and `send` stay separate permissions; `send` gets the optional `send_to` allowlist.
- Deployment switch `PV_ICLOUD_MAIL_WRITE` and a per-token write rate limit.
- iCloud gives Drafts and Archive no special-use attribute, so they are found by name.

## Capabilities

### New Capabilities

- `mail-write-tools`: the five write tools, their arguments, results and limits, the guarded
  move, protected folders, special-folder resolution, the per-token rate limit, SMTP sending
  and the `send_to` allowlist.

### Modified Capabilities

- `read-only-and-egress`: the "no tool changes iCloud data" and "read-only IMAP commands"
  requirements are narrowed to read tools and the read path; a write-path command list is
  added; SMTP is added to the iCloud endpoints and outbound connections.
- `accounts-and-auth`: the `PV_ICLOUD_MAIL_WRITE` switch; per-token `mail_write`,
  `protected_folders`, `send_to` and `max_writes_per_hour`; per-account `mail.write`
  settings; validation and the startup warning for unrestricted `send`.
- `tool-conventions`: the fixed read-only tool set becomes the read tools plus whichever
  write tools the caller's token has; annotations and instructions for write tools; write
  audit lines; the write-limit error.

## Impact

- Code: `src/pv_icloud/config.py`, `auth.py`, `server.py`, `mail/service.py`, a new
  `mail/imap_write.py` (write-path IMAP), a new `mail/smtp.py` and a small rate limiter;
  `mail/sync.py` is asked to resync the folders a write touched. `mail/imap.py` is not
  changed.
- Tests: `tests/fakeimap.py` gains `SELECT`, the guarded single-UID forms, `APPEND`,
  `UIDPLUS`/`UNSELECT` capabilities (no `MOVE`, as on iCloud), and Archive, Deleted Messages,
  Drafts and Sent Messages folders with iCloud's attributes; a new in-process fake SMTP
  server; `tests/test_repo_policy.py` gains static checks on IMAP call sites.
- Config: `config/pv-icloud.example.yaml` documents the new keys, all off.
- Dependencies: none new (`smtplib` and `email` from the standard library).
- Egress: `smtp.mail.me.com:587` when `send` is enabled; docs/egress.md is updated.
- Security posture: a token with write access can now change mail and, with `send`, send it.
  Prompt injection from untrusted mail can now cause actions, not just reads; see
  `design.md` for mitigations.
- Compatibility: no breaking change. Existing configs keep working unchanged and behave
  exactly as today.
