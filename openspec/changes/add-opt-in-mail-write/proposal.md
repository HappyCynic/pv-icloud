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
- Five new mail tools, each enabled independently per bearer token and all off by default:
  - `mail_move`: move one message to another in-scope folder.
  - `mail_archive`: move one message to the account's archive folder.
  - `mail_delete`: move one message to `Deleted Messages`. It never expunges or permanently
    removes mail.
  - `mail_draft`: save a new plain-text message (optionally a reply) in `Drafts`.
  - `mail_send`: send a new plain-text message (optionally a reply) through iCloud's SMTP
    server and keep a copy in `Sent Messages`.
- New per-token config `mail_write: [move, archive, delete, draft, send]` (default empty).
  Tools a token has not been granted are left out of its `tools/list` and refused if called
  anyway. A token without `mail_write` sees exactly today's tools, annotations and server
  instructions.
- New per-account config under `mail.write`: `protected_folders` (messages cannot be moved,
  archived or deleted into or out of them), `archive_folder` and `send_from`. Tokens can add
  further protected folders but never remove the account's.
- New IMAP commands on a separate write path only: `SELECT`, `UID MOVE` and `APPEND`. The
  read path is unchanged (`EXAMINE` and `BODY.PEEK` only) and read tools still never change
  `\Seen` or other flags. `STORE`, `COPY`, `EXPUNGE` and `UID EXPUNGE` stay forbidden
  everywhere, so nothing can be permanently deleted or flagged.
- New outbound host for `mail_send` only: `smtp.mail.me.com` port 587 (STARTTLS), contacted
  only when some token of the account has `send`.
- Every write is audit-logged (action, account, token label, outcome) with no message
  content, addresses, subjects or folder names.
- Docs: CLAUDE.md's read-only hard rule, `openspec/config.yaml`, README.md, docs/design.md
  (Goals), docs/egress.md, SECURITY.md and CONTRIBUTING.md are updated, and an ADR records
  the decision. The CLAUDE.md and `openspec/config.yaml` edits change agent rules, are
  CODEOWNERS-protected and are listed as tasks for the maintainer to approve in review.

Out of scope (unchanged from the issue): permanent delete or expunge; flagging and marking
read or unread; folder create, rename or delete; Calendar and Contacts writes. Also out of
scope here: attachments, HTML bodies and batch (multi-id) actions.

### Decision taken on the issue's open question

The issue leaves open whether protected folders are set per account, per token, or both.
This proposal takes **both, as a union**: the account list (merged from `defaults.mail`)
always applies, and a token may list extra folders. A token can only tighten protection,
never loosen it. The maintainer can change this in review before implementation.

## Capabilities

### New Capabilities

- `mail-write-tools`: the five write tools, their arguments, results, limits, protected-folder
  checks, special-folder resolution, what `delete` may and may not do, and SMTP sending.

### Modified Capabilities

- `read-only-and-egress`: the "no tool changes iCloud data" and "read-only IMAP commands"
  requirements are narrowed to read tools and the read path; a write-path command list is
  added; SMTP is added to the iCloud endpoints and outbound connections.
- `accounts-and-auth`: per-token `mail_write` permissions and per-account `mail.write`
  settings (protected folders, archive folder, sender address), with validation.
- `tool-conventions`: the fixed read-only tool set becomes the read tools plus whichever
  write tools the caller's token has; annotations and instructions for write tools; write
  audit lines and redaction of the new arguments.

## Impact

- Code: `src/pv_icloud/config.py`, `auth.py`, `server.py`, `mail/service.py`, a new
  `mail/imap_write.py` (write-path IMAP) and a new `mail/smtp.py`; `mail/sync.py` is asked to
  resync the folders a write touched.
- Tests: `tests/fakeimap.py` gains `SELECT`, `UID MOVE`, `APPEND`, `MOVE`/`UIDPLUS`
  capabilities and Deleted/Archive/Drafts/Sent folders; a new in-process fake SMTP server;
  `tests/test_repo_policy.py` gains static checks that forbidden IMAP commands appear
  nowhere and write commands appear only in the write module.
- Config: `config/pv-icloud.example.yaml` documents the new keys, all off.
- Dependencies: none new (`smtplib` and `email` from the standard library).
- Egress: `smtp.mail.me.com:587` when `send` is enabled; docs/egress.md is updated.
- Security posture: a token with write access can now change mail and, with `send`, send it.
  Prompt injection from untrusted mail becomes able to cause actions, not just reads; see
  `design.md` for mitigations.
- Compatibility: no breaking change. Existing configs keep working unchanged and behave
  exactly as today.
