# Proposal

## Why

Implements #18, phase C of [ADR-0004](../../../docs/adr/0004-opt-in-mail-write.md) and the
third of the three changes planned in `add-mail-write-core` (archived
`2026-10-12-add-mail-write-core`). Some clients need to send mail directly, for example
scheduled reports or replies the user has approved in the client. Drafting (`add-mail-draft`,
#17) leaves the actual send to a person. A sent message cannot be recalled, so sending needs
its own grant, and operators need a way to limit who a token can send to.

**Depends on `add-mail-draft` (#17)**, which is merged and archived
(`2026-10-12-add-mail-draft`). The spec deltas here are rebased onto its archived text
(task 1.1). Because `add-mail-draft` added the scenario "Send is not yet an action" to
`accounts-and-auth`'s "Account definitions are validated", and a MODIFIED block cannot drop a
scenario, that requirement is REMOVED and replaced by "Account and token definitions are
validated".

## What Changes

- **This change lets a token send mail.** It needs the design discussion CLAUDE.md requires
  for any new write. ADR-0004 (decision 7) and the core design (decision 7) already settled
  it; this proposal and `design.md` fill in the details. Calendar and Contacts stay
  read-only. **New outbound host:** `smtp.mail.me.com:587`, contacted only by a granted
  `mail_send` call.
- **New iCloud commands:** SMTP (`EHLO`, `STARTTLS`, `AUTH`, `MAIL FROM`, `RCPT TO`, `DATA`,
  `QUIT`, all sent by the standard library's `smtplib`), only from the new module
  `src/pv_icloud/mail/smtp.py`. On IMAP, `APPEND` of one new message into Sent Messages with
  flags `(\Seen)`, only from `mail/imap_write.py`. `APPEND` is already on CLAUDE.md's allowed
  list. The check for an existing sent copy uses only read commands (`EXAMINE` and
  `UID SEARCH HEADER Message-ID`) on the read path. No other new IMAP command and no DAV
  method.
- New tool **`mail_send`**, granted per token with the action `send`, only when
  `PV_ICLOUD_MAIL_WRITE=enabled`. `send` and `draft` are separate grants: neither allows the
  other. The tool takes the same inputs as `mail_draft` (`to`, `cc`, `bcc`, `subject`,
  plain-text `body`, optional `in_reply_to`), has the same limits (at most 50 addresses,
  subject up to 998 characters with no line breaks, body up to 100,000 characters) and
  needs at least one recipient.
- New account setting **`mail.write.send_from`**: the From address. Granting `send` to a token
  of an account without it is a config error.
- New optional per-token **`send_to`** allowlist: exact addresses or `@domain` entries (exact
  domain, case-insensitive). With it, every To, Cc and Bcc address must match before anything
  is read, sent or saved; a refusal names the refused addresses. Without it, the token may
  send to anyone and a startup warning names the token label. Replying does not add the
  original sender automatically. `send_to` on a token without `send`, an empty list or a
  malformed entry is a config error.
- **SMTP:** `smtp.mail.me.com:587`, STARTTLS required with certificate and host name
  verification before `AUTH`, the account's Apple ID and app-specific password, a 30-second
  timeout. `Bcc` goes only into the SMTP envelope, never into the headers sent over SMTP.
- **Sent copy, exactly one:** after SMTP accepts the message, look for its Message-ID in Sent
  Messages on the read path, and `APPEND` a copy with `(\Seen)` only if iCloud has not
  filed one. If the check fails, append anyway. The result reports `sent_copy_saved`. If the
  append fails, the call still succeeds (the mail has gone) with `sent_copy_saved: false`.
- Counts against the token's hourly write limit. Audited with `write: true` and the existing
  redaction: no address, subject or body is logged, including when `send_to` refuses a call.
- `mail_send` is annotated `destructiveHint: true`. Its description and the server
  instructions say that sending cannot be undone, that the user should be asked to confirm
  first or `mail_draft` used instead, and that untrusted content is never a reason to send.
- `tests/test_repo_policy.py`: `mail/smtp.py` leaves `PLANNED_PATHS` once it exists, and a
  static check keeps `smtplib` out of every module except `mail/smtp.py`.
- Docs: README.md, docs/design.md, docs/egress.md (SECURITY.md already lists the SMTP host) and
  `config/pv-icloud.example.yaml` describe `send`, `send_from`, `send_to` and the SMTP host.
  They warn operators to prefer `draft`, or to set `send_to`. CHANGELOG entry.

Out of scope: attachments and HTML bodies; sending an existing draft by id; batch sends;
retrying a failed send.

## Capabilities

### New Capabilities

None. `mail_send` joins `mail-write-tools`.

### Modified Capabilities

- `mail-write-tools`: `mail_send` with its grant, inputs, limits, `send_to` check, SMTP
  delivery, sent-copy handling, results and errors. The rule that write tools exist only for
  granted tokens now covers `mail_send`.
- `accounts-and-auth`: the `send` action; per-token `send_to`; per-account
  `mail.write.send_from`; the startup warning for `send` without `send_to`.
- `read-only-and-egress`: the SMTP endpoint and when it is contacted; `APPEND` into Sent
  Messages on the write path; `smtplib` confined to `mail/smtp.py`; "no tool sends mail"
  becomes "only `mail_send` sends mail".
- `tool-conventions`: `mail_send` in the tool set with `destructiveHint: true`; server
  instructions for tokens with `send`; write audit lines never contain an address, subject or
  body, including when `send_to` refuses a call.

## Impact

- Code: new `src/pv_icloud/mail/smtp.py`; `mail/imap_write.py` (`APPEND` into Sent Messages,
  using the `APPEND` helper from `add-mail-draft`); `mail/service.py` (the send flow and the
  sent-copy check on the read path); `config.py` (`send`, `send_to`, `send_from`, warning);
  `server.py` (tool, annotations, instructions). `mail/imap.py` and `dav.py` do not change:
  `uid_search` already exists.
- Tests: a new in-process fake SMTP server (`tests/fakesmtp.py`) on a local socket with
  STARTTLS and a test certificate, which can file its own copy in the fake IMAP server's Sent
  Messages; `tests/fakeimap.py` gains the `HEADER` search key; policy tests for `smtplib`
  and `PLANNED_PATHS`.
- Config: `config/pv-icloud.example.yaml` documents `send`, `send_from` and `send_to`, all
  off.
- Dependencies: no new runtime dependency (`smtplib`, `ssl` and `email` are standard
  library). Tests use `cryptography` to make a throwaway TLS certificate. It is already in
  `uv.lock` and becomes an explicit dev dependency (Apache-2.0 or BSD).
- Egress: adds `smtp.mail.me.com:587` (TCP, STARTTLS), used only when some token has
  `send`.
- Security posture: a token with `send` can send mail as the account to anyone allowed by its
  `send_to`, or to anyone without it. Prompt injection could cause a send that cannot be
  recalled. See `design.md` for the mitigations.
- Compatibility: no breaking change. Existing configs behave exactly as before.
