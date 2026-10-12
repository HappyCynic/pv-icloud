# Tasks

Implements #14, first of three changes (`add-mail-write-core`; `add-mail-draft` and
`add-mail-send` follow from separate issues). Do not start until the maintainer has approved
the proposal and merged the separate rule-change pull request (CLAUDE.md read-only rule,
`.github/CODEOWNERS`, `openspec/config.yaml`, SECURITY.md, ADR). Those rule changes are not
tasks of this change. Each group ends with its tests passing.

## 1. Policy tests

- [ ] 1.1 Write failing tests in `tests/test_repo_policy.py` that inspect IMAP call sites
  under `src/pv_icloud/mail/` (imaplib method calls such as `.store(`/`.expunge(`/`.copy(`/
  `.append(` and the command names and arguments passed to `.uid(`, `._simple_command(` or
  `.xatom(`), not plain text, since SQL `DELETE` and `COPYUID` are legitimate: no call site
  uses `STORE`, `COPY`, `EXPUNGE`, `CLOSE`, `MOVE`, `APPEND`, `CREATE`, `DELETE`, `RENAME`,
  `SUBSCRIBE` or `UNSUBSCRIBE`; `SELECT`, `UID COPY`, `UID STORE` and `UID EXPUNGE` appear
  only in `src/pv_icloud/mail/imap_write.py`; `UID STORE` only with
  `+FLAGS.SILENT (\Deleted)`; `src/pv_icloud/dav.py` still allows only `PROPFIND` and
  `REPORT`; CODEOWNERS lists `/src/pv_icloud/mail/imap_write.py`. Verify they fail only for
  the missing module.

## 2. Deployment switch, configuration and auth

- [ ] 2.1 Write tests in `tests/test_units.py` for `PV_ICLOUD_MAIL_WRITE`: unset, empty and
  `disabled` are off; `enabled` is on; other values are a config error naming the variable; a
  `mail_write` grant while off is a config error naming account, label, `mail_write` and the
  variable.
- [ ] 2.2 Write tests for token keys: `mail_write` (`move`, `archive`, `delete`),
  `protected_folders` and `max_writes_per_hour` parsing and defaults; unknown action
  (including `draft` and `send`, which later changes add), non-positive limit and wrong types
  rejected with account, label and key; `mail.write` keys: the defaults/account union of
  `protected_folders`, the `archive_folder` default `Archive` and override, unknown keys
  (including `send_from`) and wrong types rejected with account and key.
- [ ] 2.3 Implement in `src/pv_icloud/config.py` (`TokenEntry`, `MailConfig`, the switch) and
  verify 2.1 and 2.2 pass.
- [ ] 2.4 Write a test that two tokens of one account resolve to `AccountContext`s with their
  own permissions, protected folders and write limit; extend `src/pv_icloud/auth.py` and
  verify it passes.
- [ ] 2.5 Document the switch and the new keys, all off, in `config/pv-icloud.example.yaml`
  and verify the example still loads in the config tests.

## 3. Fake IMAP server

- [ ] 3.1 Extend `tests/fakeimap.py`: advertise `UIDPLUS` and `UNSELECT` but not `MOVE`, as
  iCloud does, with a switch to turn `UIDPLUS` off; `SELECT` (read-write, UIDVALIDITY);
  `UID COPY` of one UID with `COPYUID` (and switches to omit it, return a mismatched one, or
  fail); `UID STORE` of one UID with `+FLAGS.SILENT (\Deleted)` only, and `UID EXPUNGE` of
  one UID, each with a switch to fail; folder `Archive` with no special-use attribute and
  `Deleted Messages` with `\Trash`. The fake SHALL reject every other write form (multi-UID,
  ranges, other flags, `-FLAGS`, `FLAGS`, `EXPUNGE`, `CLOSE`, `MOVE`, `STORE`, `COPY`,
  `APPEND`). Verify existing mail tests still pass.

## 4. Write-path IMAP

- [ ] 4.1 Write tests for the guarded move in `mail/imap_write.py` against the fake: exact
  command sequence `SELECT`, `UID FETCH`, `UID COPY`, `UID STORE`, `UID EXPUNGE`, `LOGOUT`;
  refusal without `UIDPLUS` before `SELECT`; stale UIDVALIDITY and missing UID refused before
  `UID COPY`; missing or mismatched `COPYUID` stops before `UID STORE`; failure of `UID COPY`,
  `UID STORE` or `UID EXPUNGE` reported with the state the message was left in; another
  `\Deleted` message in the source survives; flags, including `\Seen`, kept on the copy;
  `new_id` from `COPYUID`; the connection is never returned to the read pool.
- [ ] 4.2 Write tests for folder resolution: Archive by name only, with the
  `mail.write.archive_folder` override; Deleted Messages by `\Trash` then by name; a missing
  folder is an error and sends no write command.
- [ ] 4.3 Implement `src/pv_icloud/mail/imap_write.py` (SPDX header) and the pool change that
  logs out connections after a write; verify 4.1, 4.2 and 1.1 pass and the read-path command
  test in `tests/test_mail.py` is unchanged.

## 5. Rate limit

- [ ] 5.1 Write tests for `src/pv_icloud/ratelimit.py` with an injected clock: sliding
  one-hour window, every call counts, separate budgets per token, minutes-remaining rounding
  (at least 1), and a new limiter instance (as after a restart) starts empty.
- [ ] 5.2 Implement `src/pv_icloud/ratelimit.py` (SPDX header), in memory only; verify 5.1
  passes.

## 6. Move, archive and delete

- [ ] 6.1 Write tests in `tests/test_mail.py` for move, archive and delete: protected source
  and destination (defaults, account and token lists, INBOX in any case) refused with no
  write command sent; out-of-scope destination refused like a missing folder; same folder;
  already in Deleted Messages (no `SELECT`); already in Archive; missing archive folder;
  Deleted Messages and Archive reachable while hidden from the read scope; sync asked to
  resync both folders.
- [ ] 6.2 Implement move, archive and delete in `src/pv_icloud/mail/service.py` and the
  resync hook in `mail/sync.py`; verify 6.1 passes.

## 7. Tools, permissions and audit

- [ ] 7.1 Write tests in `tests/test_server.py`: with the switch off, `imap_write` is not
  imported and no write tool is registered; a token without `mail_write` gets the same
  `tools/list`, annotations and instructions as before (compare to a snapshot taken before
  the change); each of move, archive and delete alone exposes only its tool; a non-granted
  tool call fails as unknown with no IMAP command; two tokens of one account differ; the rate
  limit refuses the call after N with `outcome` `throttled` and no IMAP command; write
  annotations and instructions match the spec; descriptions state the one-message scope, the
  hourly limit, "Deleted Messages", recoverability and the untrusted-content warning; audit
  lines have `write: true` and no folder names.
- [ ] 7.2 Implement the three tools, conditional registration behind the switch, per-token
  `tools/list` filtering, call-time permission and rate-limit checks, per-token instructions
  and the audit field in `src/pv_icloud/server.py`; verify 7.1 passes.

## 8. Docs and checks

- [ ] 8.1 Update README.md (switch, tools, config), docs/design.md (Goals, Tools table, a
  "Mail writes" section on the guarded move) and CONTRIBUTING.md (wording that says Mail is
  read-only); add `Unreleased` entries to CHANGELOG.md for the switch, per-token move,
  archive and delete, protected folders and the write limit.
- [ ] 8.2 Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy` and `openspec validate --all --strict`; all pass.
