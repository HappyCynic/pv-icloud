# Tasks

Implements #17 (`add-mail-draft`, phase B of ADR-0004). Do not start until the maintainer has
approved the proposal. Each group ends with its tests passing.

## 1. Policy tests

- [x] 1.1 Update `tests/test_repo_policy.py`: `.append()` on an IMAP connection is allowed
  only in `src/pv_icloud/mail/imap_write.py` and only with the flags argument the literal
  `(\Draft \Seen)`; add cases that `.append()` in `imap.py`, or in `imap_write.py` with
  other flags (`(\Seen)`, `(\Draft)`, a variable, none), is a violation; extend the
  "uses every write form" guard to require one `.append()` in the write module. Verify the
  new guard fails until 4.3 is done and every other policy test still passes.

## 2. Configuration

- [x] 2.1 Write tests in `tests/test_units.py`: `mail_write: [draft]` loads with the switch
  on; `draft` alone gives no other action; `send` is still an unknown action listing the
  allowed ones; `draft` with the switch off is the existing config error;
  `mail.write.send_from` is optional, merged from `defaults.mail.write` (account wins), and
  must be one bare address (display-name form, no `@`, two addresses or a non-string
  rejected, naming the account and `mail.write.send_from`).
- [x] 2.2 Add `draft` to `MAIL_WRITE_ACTIONS` and `send_from` to `MailWriteConfig` and
  `_MAIL_WRITE_KEYS` in `src/pv_icloud/config.py`; verify 2.1 passes. Show `draft` and
  `send_from` in `config/pv-icloud.example.yaml` (commented, off) and verify the example
  still loads.

## 3. Fake IMAP server

- [x] 3.1 Extend `tests/fakeimap.py`: `APPEND` into an existing folder with exactly
  `(\Draft \Seen)` stores the message with those flags and answers
  `[APPENDUID <uidvalidity> <uid>]`; switches to omit `APPENDUID` and to fail the command;
  `APPEND` into a missing folder fails; any other flags are rejected. Keep `Drafts` without a
  special-use attribute. Verify existing mail tests still pass.

## 4. Compose and write path

- [x] 4.1 Write tests for `src/pv_icloud/mail/compose.py`: address validation (bare and
  display-name forms accepted; empty, two in one entry, group syntax, no `@`, CR or LF
  refused, error names field and index but not the value); the 50-address, 998-character
  subject (and no line breaks) and 100,000-character body limits at and just over the limit;
  `nothing to draft`; headers (`From` only when `send_from` is given, `To`, `Cc`, `Bcc`,
  `Subject`, `Date`, `Message-ID` without the local host name), `text/plain; charset=utf-8`, non-ASCII subject and body round-trip
  through `parse_message`; reply headers (`In-Reply-To`, `References` from `References`, or
  from `In-Reply-To` when absent, no headers when the original lacks `Message-ID`) and the
  `Re:` subject rule (added, kept for `Re:`/`RE:`, caller subject wins, empty original).
- [x] 4.2 Implement `src/pv_icloud/mail/compose.py` (SPDX header, standard library only, no
  IMAP); verify 4.1 passes.
- [x] 4.3 Write tests in `tests/test_mail_write.py` for `MailWriter.append_draft` against the
  fake: command stream `LIST`, `APPEND "Drafts" (\Draft \Seen)`, `LOGOUT` on one write
  connection with no `SELECT`, `STORE` or `UID`; the draft is in Drafts with `\Draft` and
  `\Seen`; `id` from `APPENDUID`; `id` null when `APPENDUID` is missing, with exactly one
  draft saved; no `UIDPLUS` refused before `APPEND`; missing Drafts refused before `APPEND`;
  `APPEND` failure reported as not saved; Drafts found while hidden from the read scope; the
  connection is never returned to the read pool. Then implement `append_draft` in
  `src/pv_icloud/mail/imap_write.py` and update its module docstring; verify 4.3 and 1.1
  pass and the read-path command test in `tests/test_mail.py` is unchanged.

## 5. Service

- [x] 5.1 Write tests in `tests/test_mail_write.py` (or `tests/test_mail.py`) for
  `MailService.draft`: a protected `Drafts` (account and token lists) does not block the
  draft; `From` is the account's `send_from` when set; input limits refused with no IMAP command; reply to an unread message keeps
  it unread and reads it only with `EXAMINE` and `BODY.PEEK[]`; reply id malformed, out of
  scope, stale or not found refused with nothing saved; resync of Drafts requested only when
  it is in the read scope; no SMTP connection (patch `smtplib.SMTP` to fail the test if
  used).
- [x] 5.2 Implement `draft` in `src/pv_icloud/mail/service.py`; verify 5.1 passes.

## 6. Tool, permissions and audit

- [x] 6.1 Write tests in `tests/test_server.py`: a token without `draft` does not list
  `mail_draft` and calling it fails as unknown with no IMAP command; a `draft`-only token
  lists `mail_draft` and no send tool; annotations (`readOnlyHint` false, `destructiveHint`
  false, `idempotentHint` false, `openWorldHint` true); the description states Drafts, never
  sends, `50`, `998`, `100000`, the hourly limit, the read-scope note for the id and the
  untrusted-content warning; instructions for a `draft` token name `mail_draft` and say
  drafts are never sent; a read-only token's `tools/list` and instructions are unchanged;
  the call counts against the write limit (throttled after N, no IMAP command); audit lines
  for successful, over-limit and throttled drafts have `write: true` and contain no address,
  subject, body text or folder name.
- [x] 6.2 Implement `mail_draft` in `src/pv_icloud/server.py` (`WRITE_TOOLS`, registration
  behind the switch, description, `write_instructions`); verify 6.1 passes.

## 7. Docs and checks

- [x] 7.1 Update README.md (the `draft` action, `mail_draft` and `send_from`), docs/design.md (Tools
  table and the "Mail writes" section: `APPEND` into Drafts, `APPENDUID`, replies) and add
  an `Unreleased` entry to CHANGELOG.md for `mail_draft`.
- [x] 7.2 Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy` and `openspec validate --all --strict`; all pass.
