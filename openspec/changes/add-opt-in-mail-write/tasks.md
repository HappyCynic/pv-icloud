# Tasks

Implements #14. Do not start until the maintainer has approved the proposal and merged the
separate rule-change pull request (CLAUDE.md read-only rule, `.github/CODEOWNERS`,
`openspec/config.yaml`, SECURITY.md, ADR). Those rule changes are not tasks of this change.
The phases are done in order; each phase ends with all checks passing.

## Phase A: switch, config, tools, protected folders, rate limit, move/archive/delete

### 1. Policy tests

- [ ] 1.1 Write failing tests in `tests/test_repo_policy.py` that inspect IMAP call sites
  under `src/pv_icloud/mail/` (imaplib method calls such as `.store(`/`.expunge(`/`.copy(`
  and the command names and arguments passed to `.uid(`, `._simple_command(` or `.xatom(`),
  not plain text, since SQL `DELETE` and `COPYUID` are legitimate: no call site uses `STORE`,
  `COPY`, `EXPUNGE`, `CLOSE`, `MOVE`, `CREATE`, `DELETE`, `RENAME`, `SUBSCRIBE` or
  `UNSUBSCRIBE`; `SELECT`, `UID COPY`, `UID STORE`, `UID EXPUNGE` and `APPEND` appear only
  in `src/pv_icloud/mail/imap_write.py`; `UID STORE` only with `+FLAGS.SILENT (\Deleted)`;
  `src/pv_icloud/dav.py` still allows only `PROPFIND` and `REPORT`; CODEOWNERS lists
  `/src/pv_icloud/mail/imap_write.py` and `/src/pv_icloud/mail/smtp.py`. Verify they fail
  only for the missing module.

### 2. Deployment switch, configuration and auth

- [ ] 2.1 Write tests in `tests/test_units.py` for `PV_ICLOUD_MAIL_WRITE`: unset, empty and
  `disabled` are off; `enabled` is on; other values are a config error; a `mail_write` grant
  while off is a config error naming account, label, `mail_write` and the variable.
- [ ] 2.2 Write tests for token keys: `mail_write`, `protected_folders` and
  `max_writes_per_hour` parsing and defaults; unknown action and wrong types rejected with
  account, label and key; `mail.write` keys with the defaults/account union of
  `protected_folders` and the `Archive` default; `send` without `send_from` rejected; invalid
  `send_from` rejected. (`send_to` is phase C.)
- [ ] 2.3 Implement in `src/pv_icloud/config.py` (`TokenEntry`, `MailConfig`, the switch) and
  verify 2.1 and 2.2 pass.
- [ ] 2.4 Write a test that two tokens of one account resolve to `AccountContext`s with their
  own permissions, protected folders and write limit; extend `src/pv_icloud/auth.py` and
  verify it passes.
- [ ] 2.5 Document the switch and the new keys, all off, in `config/pv-icloud.example.yaml`
  and verify the example still loads in the config tests.

### 3. Fake IMAP server

- [ ] 3.1 Extend `tests/fakeimap.py`: advertise `UIDPLUS` and `UNSELECT` but not `MOVE`, as
  iCloud does, with a switch to turn `UIDPLUS` off; `SELECT` (read-write, UIDVALIDITY);
  `UID COPY` of one UID with `COPYUID` (and a switch to omit it or fail); `UID STORE` of one
  UID with `+FLAGS.SILENT (\Deleted)` only; `UID EXPUNGE` of one UID; folders `Archive` and
  `Drafts` with no special-use attribute and `Deleted Messages` (`\Trash`) and
  `Sent Messages` (`\Sent`). The fake SHALL reject every other write form (multi-UID,
  ranges, other flags, `-FLAGS`, `EXPUNGE`, `CLOSE`, `MOVE`, `STORE`, `COPY`). Verify
  existing mail tests still pass.

### 4. Write-path IMAP

- [ ] 4.1 Write tests for the guarded move in `mail/imap_write.py` against the fake: exact
  command sequence `SELECT`, `UID FETCH`, `UID COPY`, `UID STORE`, `UID EXPUNGE`, `LOGOUT`;
  refusal without `UIDPLUS` before `SELECT`; stale UIDVALIDITY and missing UID refused before
  `UID COPY`; missing or mismatched `COPYUID` stops before `UID STORE`; failure of
  `UID STORE` or `UID EXPUNGE` reported as specified; another `\Deleted` message in the
  source survives; flags, including `\Seen`, kept on the copy; `new_id` from `COPYUID`; the
  connection is never returned to the read pool.
- [ ] 4.2 Write tests for folder resolution: Archive and Drafts by name only, Deleted
  Messages and Sent Messages by attribute then name, `mail.write.archive_folder` override,
  missing folder is an error.
- [ ] 4.3 Implement `src/pv_icloud/mail/imap_write.py` (SPDX header) and the pool change that
  logs out connections after a write; verify 4.1, 4.2 and 1.1 pass and the read-path
  command test in `tests/test_mail.py` is unchanged.

### 5. Rate limit

- [ ] 5.1 Write tests for `src/pv_icloud/ratelimit.py` with an injected clock: sliding
  one-hour window, every call counts, separate budgets per token, minutes-remaining rounding
  (at least 1).
- [ ] 5.2 Implement `src/pv_icloud/ratelimit.py` (SPDX header); verify 5.1 passes.

### 6. Move, archive and delete

- [ ] 6.1 Write tests in `tests/test_mail.py` for move, archive and delete: protected source
  and destination (defaults, account and token lists, INBOX in any case) refused with no
  write command sent; out-of-scope destination; same folder; already in Deleted Messages
  (no `SELECT`); already in Archive; missing archive folder; Deleted Messages and Archive
  reachable while hidden from the read scope; sync asked to resync both folders.
- [ ] 6.2 Implement move, archive and delete in `src/pv_icloud/mail/service.py` and the
  resync hook in `mail/sync.py`; verify 6.1 passes.

### 7. Tools, permissions and audit

- [ ] 7.1 Write tests in `tests/test_server.py`: with the switch off, `imap_write` and `smtp`
  are not imported and no write tool is registered; a token without `mail_write` gets the
  same `tools/list`, annotations and instructions as before (compare to a snapshot taken
  before the change); each of move, archive and delete alone exposes only its tool; a
  non-granted tool call fails as unknown with no IMAP command; two tokens of one account
  differ; the rate limit refuses the call after N with `outcome` `throttled` and no IMAP
  command; write annotations and instructions match the spec; descriptions state the
  one-message scope, the hourly limit, "Deleted Messages", recoverability and the
  untrusted-content warning; audit lines have `write: true` and no folder names.
- [ ] 7.2 Implement the three tools, conditional registration behind the switch, per-token
  `tools/list` filtering, call-time permission and rate-limit checks, per-token instructions
  and the audit field in `src/pv_icloud/server.py`; verify 7.1 passes.

### 8. Phase A docs and checks

- [ ] 8.1 Update README.md (switch, tools, config), docs/design.md (Goals, Tools table, a
  "Mail writes" section on the guarded move) and CONTRIBUTING.md (wording that says Mail is
  read-only); add `Unreleased` entries to CHANGELOG.md for the switch, per-token move,
  archive and delete, protected folders and the write limit.
- [ ] 8.2 Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy` and `openspec validate --all --strict`; all pass.

## Phase B: draft

- [ ] 9.1 Extend `tests/fakeimap.py` with `APPEND` (flags, `APPENDUID`); verify existing
  tests still pass.
- [ ] 9.2 Write tests for composing and `mail_draft`: recipient, subject and body limits;
  reply headers and `Re:` subject with the original's flags unchanged; draft in Drafts
  (found by name, hidden or not) with `\Draft \Seen`, `id` from `APPENDUID`; no SMTP
  connection; `draft` granted alone exposes only `mail_draft`; rate limit applies; audit
  shows only counts and lengths.
- [ ] 9.3 Implement composing (`mail/service.py`), `APPEND` in `mail/imap_write.py` and the
  `mail_draft` tool; verify 9.2 and 1.1 pass.
- [ ] 9.4 Update README.md and docs/design.md for drafts; add the CHANGELOG.md `Unreleased`
  entry.
- [ ] 9.5 Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy` and `openspec validate --all --strict`; all pass.

## Phase C: send with optional send_to

- [ ] 10.1 Write config tests for `send_to`: entries are a bare address or `@domain`; empty
  list, malformed entry and `send_to` without `send` rejected with account, label and key;
  one startup warning per `send` token without `send_to`, naming account and label and no
  address.
- [ ] 10.2 Implement `send_to` parsing and the startup warning in `config.py` (and the
  server start-up path); verify 10.1 passes.
- [ ] 10.3 Add `tests/fakesmtp.py`, an in-process SMTP server with STARTTLS on/off, AUTH,
  per-recipient accept/reject, connection failure and a record of envelopes and messages;
  verify with a smoke test.
- [ ] 10.4 Write tests for `mail/smtp.py` against the fake: STARTTLS required before AUTH;
  `Bcc` in the envelope but not in the submitted headers; rejection raises; no address or
  content in logs (use `caplog`).
- [ ] 10.5 Implement `src/pv_icloud/mail/smtp.py` with `smtplib` and `email` (SPDX header);
  verify 10.4 passes.
- [ ] 10.6 Write tests for `mail_send`: `send_to` exact and `@domain` matching,
  case-insensitive, subdomain refused, To, Cc and Bcc all checked, refusal names only the
  refused addresses with no SMTP connection and nothing saved, reply to a sender not on the
  list refused; without `send_to` any recipient; send then sent copy with `\Seen` and `Bcc`
  header; `sent_copy_saved: false` when the append fails; nothing saved when SMTP rejects;
  SMTP unreachable maps to "iCloud is unavailable"; `send` granted alone exposes only
  `mail_send`; audit has `write: true`, `outcome` `invalid` for refused recipients and no
  addresses, subject or body.
- [ ] 10.7 Implement `mail_send` in `mail/service.py` and `server.py`; verify 10.6 passes.
- [ ] 10.8 Update README.md (send, `send_to`, warning to grant `send` only to clients that
  confirm each send with a person), docs/design.md (Overview with SMTP) and docs/egress.md
  (`smtp.mail.me.com:587`, only with `send`); add the CHANGELOG.md `Unreleased` entry.
- [ ] 10.9 Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy` and `openspec validate --all --strict`; all pass.
