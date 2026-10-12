## 1. Preconditions

- [ ] 1.1 Confirm `add-mail-draft` is merged and archived. Rebase this change's MODIFIED
      requirements onto the archived spec text: `mail-write-tools` "Write tools exist only
      for granted tokens", `accounts-and-auth` "Account definitions are validated" and "Mail
      write settings per account", `read-only-and-egress` "No tool changes iCloud data" and
      "Mail write IMAP commands", and `tool-conventions` "Fixed, read-only tool set" and
      "Audit log line per tool call". Keep the draft wording and add only the send parts. Run
      `openspec validate add-mail-send --strict`.
- [ ] 1.2 Record the maintainer's answers to the open questions in `design.md` (protected
      Sent Messages, whether and when iCloud files SMTP-sent mail, partial recipient refusal)
      and adjust the spec deltas if an answer changes behaviour.

## 2. Test fakes

- [ ] 2.1 Add `cryptography` as an explicit dev dependency (`uv add --dev`; already in
      `uv.lock`, Apache-2.0 or BSD) for a throwaway test certificate.
- [ ] 2.2 Add `tests/fakesmtp.py` (SPDX header): a threaded ESMTP server on `127.0.0.1` with
      `EHLO`, `STARTTLS` (self-signed `localhost` certificate made at test time), `AUTH`,
      `MAIL`, `RCPT`, `DATA`, `QUIT`. It records commands, envelope and raw data, and has
      switches for: no STARTTLS offered, refuse given recipients, refuse `AUTH`, drop the
      connection after `DATA`, and file its own copy into a `FakeServer`'s Sent Messages.
      Any `AUTH`, `MAIL` or `DATA` before TLS fails the test.
- [ ] 2.3 Extend `tests/fakeimap.py`: the `HEADER <name> <value>` search key, and `APPEND` into
      Sent Messages with exactly `(\Seen)` on a write connection, answered with `APPENDUID`.
      Any other `APPEND` folder or flag set is still refused. Add a switch that makes
      `APPEND` fail.

## 3. Config (tests first)

- [ ] 3.1 Tests in `tests/test_units.py` (or the existing config tests): `send` accepted as an
      action; `send_to` without `send`, empty, or with a malformed entry (display name, no `@`,
      domain without a dot) rejected naming the token and `send_to`; `send` without
      `mail.write.send_from` rejected naming the account, the token and
      `mail.write.send_from`; an invalid `send_from` rejected; `send_from` merged from
      defaults; entries stored lower-cased; the startup warning logged for `send` without
      `send_to` and not otherwise, containing no address.
- [ ] 3.2 Implement in `src/pv_icloud/config.py` (`MAIL_WRITE_ACTIONS`, `TokenEntry.send_to`,
      `MailWriteConfig.send_from`, `_MAIL_WRITE_KEYS`) and log the warning from
      `src/pv_icloud/__main__.py`.

## 4. SMTP module (tests first)

- [ ] 4.1 Tests against the fake SMTP server: a successful send passes the envelope and data
      through; STARTTLS not offered fails before `AUTH`; an untrusted certificate (default
      SSL context) fails before `AUTH`; rejected credentials, all recipients refused, some
      recipients refused (returned) and a dropped connection after `DATA` ("may have been
      sent") each give the right outcome; no error text contains the server's reply or an
      address.
- [ ] 4.2 Implement `src/pv_icloud/mail/smtp.py` (SPDX header): `smtp.mail.me.com:587`,
      `ssl.create_default_context()`, 30-second timeout, injectable connection factory for
      tests, no option to turn verification off.
- [ ] 4.3 `tests/test_repo_policy.py`: drop `/src/pv_icloud/mail/smtp.py` from
      `PLANNED_PATHS`; add a check that only `mail/smtp.py` imports `smtplib`.

## 5. mail_send (tests first)

- [ ] 5.1 Tests in `tests/test_mail_write.py`:
  - grant and visibility: `draft` alone cannot see or call `mail_send`; `send` alone does not
    see `mail_draft`; a call without the grant reaches neither SMTP nor IMAP;
  - rate limit counts `mail_send` and refuses before SMTP;
  - input limits (0 recipients, 51 addresses, invalid address, subject with a line break or
    999 characters, body over 100,000 characters) fail with no SMTP connection;
  - `send_to`: exact and `@domain` matches, case-insensitive; subdomain refused; a refused
    To, Cc or Bcc is named in the error, allowed ones are not; no SMTP connection and no
    IMAP command; replying to a sender not on the list is refused before the original is
    read;
  - reply: `In-Reply-To`, `References`, `Re:` subject; the original's `\Seen` unchanged;
  - Bcc is in the envelope, absent from the data sent over SMTP, present in the appended copy;
  - sent copy: with the fake SMTP filing its own copy, there is exactly one message with the
    Message-ID in Sent Messages and no `APPEND`; without it, exactly one, flagged `\Seen`,
    readable through `sent_copy_id`; a failing search still leads to an append; a failing
    `APPEND` returns success with `sent_copy_saved: false`; a dropped connection after `DATA`
    gives "may have been sent" and no `APPEND`; no Sent Messages folder fails before SMTP;
  - Sent Messages hidden from the read scope still works;
  - the IMAP command stream contains only read commands plus the one allowed `APPEND`.
- [ ] 5.2 Implement the send flow in `src/pv_icloud/mail/service.py`, sharing argument
      checks, message building and `in_reply_to` handling with `mail_draft`. Add the
      Sent-Messages `APPEND` to the `APPEND` helper in `src/pv_icloud/mail/imap_write.py`
      (fixed flags per destination). Ask the sync worker to resync Sent Messages after a
      send.

## 6. Server surface (tests first)

- [ ] 6.1 Tests in `tests/test_server.py`: `mail_send` annotations (`destructiveHint` true,
      `idempotentHint` false); description mentions that sending cannot be undone,
      `mail_draft`, the limits and the untrusted-content rule; instructions for a token with
      `send` versus one with only `draft`; a token without `mail_write` still gets the
      unchanged read-only instructions and tool list (snapshot); audit lines for a
      successful, a `send_to`-refused and an SMTP-failed send contain no address, subject or
      body and have `write: true`; with the switch off, `pv_icloud.mail.smtp` is not imported.
- [ ] 6.2 Implement in `src/pv_icloud/server.py`: register `mail_send` in `WRITE_TOOLS`,
      annotations, description, `write_instructions`, and import `mail/smtp.py` only when the
      switch is on.

## 7. Docs and changelog

- [ ] 7.1 README.md: `mail_send`, the `send` grant, `send_from`, `send_to`, the SMTP host, and a
      warning to prefer `draft` or set `send_to`.
- [ ] 7.2 docs/design.md: the send flow, SMTP/TLS, Bcc handling and the sent-copy check.
- [ ] 7.3 docs/egress.md: add `smtp.mail.me.com:587` (only when some token has `send`) to the
      table and to the iptables example as an optional rule. Check that SECURITY.md's egress
      list still matches.
- [ ] 7.4 `config/pv-icloud.example.yaml`: commented `send`, `send_to` and
      `mail.write.send_from` examples, all off, with the warning.
- [ ] 7.5 CHANGELOG.md `Unreleased`: `mail_send`, `send`, `send_from`, `send_to`, the startup
      warning and the new SMTP egress.

## 8. Checks

- [ ] 8.1 Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and
      `uv run mypy`, and `openspec validate --all --strict`; fix any failure.
