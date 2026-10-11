# Tasks

Implements #14. Do not start until the maintainer has approved the proposal, including the
protected-folder decision and the new IMAP commands (`SELECT`, `UID MOVE`, `APPEND`) and
SMTP egress in `design.md`.

## 1. Rules, decision record and policy tests

- [ ] 1.1 Add `docs/adr/0004-opt-in-mail-write.md` recording decisions 1–5 of `design.md`
  (per-token grants, protected-folder union, three IMAP write commands confined to one
  module, no expunge path, SMTP); verify it follows the format of ADR-0003 and is linked
  from docs/design.md.
- [ ] 1.2 Write failing tests in `tests/test_repo_policy.py` that inspect IMAP call sites
  (imaplib method calls such as `.store(`/`.expunge(` and the command names passed to
  `.uid(`, `._simple_command(` or `.xatom(`) under `src/pv_icloud/mail/`, not plain text,
  since SQL `DELETE` and `COPYUID` are legitimate: no call site uses `STORE`, `COPY`,
  `EXPUNGE`, `CLOSE`, `CREATE`, `DELETE`, `RENAME`, `SUBSCRIBE` or `UNSUBSCRIBE`;
  `SELECT`, `MOVE` and `APPEND` are used only in `src/pv_icloud/mail/imap_write.py`; `src/pv_icloud/dav.py` still allows only `PROPFIND`
  and `REPORT`; CODEOWNERS lists `/src/pv_icloud/mail/imap_write.py` and
  `/src/pv_icloud/mail/smtp.py`. Verify they fail for the missing files.
- [ ] 1.3 Update CLAUDE.md's read-only hard rule (and `openspec/config.yaml`'s summary of it)
  to: read path `EXAMINE`/`BODY.PEEK` only; write path `SELECT`, `UID MOVE`, `APPEND` only,
  in `mail/imap_write.py`; never `STORE`, `COPY`, `EXPUNGE`; DAV unchanged. Add the two new
  modules to `.github/CODEOWNERS`. These are maintainer-reviewed rule changes; verify the
  existing `test_repo_policy.py` tests still pass.

## 2. Configuration and auth

- [ ] 2.1 Write tests in `tests/test_units.py` for: token `mail_write` and `protected_folders`
  parsing and defaults; unknown action and wrong types rejected with account, label and key;
  `mail.write` keys with defaults/account union of `protected_folders`; `send` without
  `send_from` rejected; invalid `send_from` rejected.
- [ ] 2.2 Implement in `src/pv_icloud/config.py` (`TokenEntry`, `MailConfig`) and verify 2.1
  passes.
- [ ] 2.3 Write a test that two tokens of one account resolve to `AccountContext`s with their
  own permissions and protected folders; extend `src/pv_icloud/auth.py` and verify it passes.
- [ ] 2.4 Document the new keys, all off, in `config/pv-icloud.example.yaml` and verify the
  example still loads in the config tests.

## 3. Fakes

- [ ] 3.1 Extend `tests/fakeimap.py`: `SELECT` (read-write selection, records UIDVALIDITY),
  `UID MOVE` (with `COPYUID` when UIDPLUS is on), `APPEND` (with flags and `APPENDUID`),
  `MOVE`/`UIDPLUS` capabilities that tests can switch off, special-use attributes in `LIST`
  and `Archive`, `Deleted Messages`, `Drafts` and `Sent Messages` folders. The fake SHALL
  reject any command not on the read or write lists. Verify existing mail tests still pass.
- [ ] 3.2 Add `tests/fakesmtp.py`, an in-process SMTP server with STARTTLS on/off, AUTH,
  per-recipient accept/reject and a record of envelopes and messages; verify with a smoke
  test.

## 4. Write-path IMAP and SMTP

- [ ] 4.1 Write tests for `mail/imap_write.py` against the fake: move sends exactly `SELECT`
  + `UID MOVE` and logs out; refuses without `MOVE` before `SELECT`; stale UIDVALIDITY
  refused; append sets the given flags; returns new UIDs when UIDPLUS is on and `None`
  otherwise; special-folder resolution by attribute then by name.
- [ ] 4.2 Implement `src/pv_icloud/mail/imap_write.py` (SPDX header) and the pool change that
  logs out connections after a write; verify 4.1 passes and the read-path command test in
  `tests/test_mail.py` is unchanged.
- [ ] 4.3 Write tests for `mail/smtp.py` against the fake: STARTTLS required before AUTH;
  `Bcc` in the envelope but not in the submitted headers; rejection raises; no address or
  content in logs (use `caplog`).
- [ ] 4.4 Implement `src/pv_icloud/mail/smtp.py` with `smtplib` and `email` (SPDX header);
  verify 4.3 passes.

## 5. Mail write service

- [ ] 5.1 Write tests in `tests/test_mail.py` for move, archive and delete: protected source
  and destination (account, defaults and token lists, INBOX in any case) refused with no
  write command sent; out-of-scope destination; same folder; already in Deleted Messages;
  missing archive folder; flags including `\Seen` unchanged; sync asked to resync both
  folders.
- [ ] 5.2 Write tests for draft and send: recipient, subject and body limits; reply headers
  and `Re:` subject with the original's flags unchanged; draft in Drafts with `\Draft \Seen`
  and no SMTP; send then sent copy with `\Seen`; `sent_copy_saved: false` when the append
  fails; nothing saved when SMTP rejects.
- [ ] 5.3 Implement the write operations in `src/pv_icloud/mail/service.py` (and the resync
  hook in `mail/sync.py`); verify 5.1 and 5.2 pass.

## 6. Tools, permissions and audit

- [ ] 6.1 Write tests in `tests/test_server.py`: a token without `mail_write` gets the same
  `tools/list`, annotations and instructions as before (compare to a snapshot taken before
  the change); each action alone exposes only its tool; a non-granted tool call fails as
  unknown with no IMAP command; two tokens of one account differ; write annotations and
  instructions match the spec; descriptions state limits, "Deleted Messages" and the
  untrusted-content warning; audit lines have `write: true` and no addresses, subject, body
  or folder names; SMTP failure maps to "iCloud is unavailable".
- [ ] 6.2 Implement the five tools, per-token `tools/list` filtering, call-time permission
  check, per-token instructions and audit field in `src/pv_icloud/server.py`; verify 6.1
  passes.

## 7. Documentation

- [ ] 7.1 Update README.md (tools, config, warning about `send`), docs/design.md (Goals,
  Overview diagram with SMTP, Tools table, a "Mail writes" section), docs/egress.md
  (`smtp.mail.me.com:587`, only with `send`), SECURITY.md (threat model: write access,
  prompt injection, grant `send` only to clients that confirm each send) and
  CONTRIBUTING.md (rule wording); verify every doc that says "read-only" for Mail is
  updated with `grep -rn "read-only" README.md docs SECURITY.md CONTRIBUTING.md`.
- [ ] 7.2 Add `Unreleased` entries to CHANGELOG.md: opt-in mail write tools per token,
  protected folders, SMTP egress when `send` is enabled.

## 8. Checks

- [ ] 8.1 Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and
  `uv run mypy`, and `openspec validate --all --strict`; all pass.
