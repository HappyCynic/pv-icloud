# Proposal

## Why

Implements #17, the second of the three changes that deliver #14 (ADR-0004, phase B). With
`add-mail-write-core` merged, a token can move, archive and delete mail but cannot help write
replies. A draft lets a client prepare a message that a person reviews and sends from Mail,
so nothing ever leaves the account without them.

## What Changes

- **Mail stays read-only by default; this adds one more opt-in write.** It uses `APPEND`,
  which ADR-0004 and CLAUDE.md already list as an allowed write command; this change adds it
  to the code, the fake and the policy tests, confined to `mail/imap_write.py`. No new IMAP
  command beyond that list, no DAV method and no new outbound host: Calendar and Contacts
  stay read-only, and nothing is sent (no SMTP; that is `add-mail-send`).
- New action `draft` for a token's `mail_write`, granted on its own and only while
  `PV_ICLOUD_MAIL_WRITE=enabled`. Granting `draft` does not grant `send` (still an unknown
  action until `add-mail-send`).
- New tool `mail_draft`: optional `to`, `cc`, `bcc` (lists of addresses), `subject`,
  plain-text `body` and `in_reply_to` (a mail id). It saves one plain-text UTF-8 message with
  `APPEND "Drafts" (\Draft \Seen)` and returns the draft's new id from `APPENDUID`.
  - Drafts is found by name (`Drafts`; iCloud gives it no special-use attribute), even when
    hidden from the read scope by `mail.special_folders`.
  - With `in_reply_to`, the original is read on the existing read path (`EXAMINE` and
    `BODY.PEEK[]`, as `mail_get` does) to set `In-Reply-To`, `References` and a `Re:`
    subject when none is given. Reading it does not change its `\Seen` flag.
  - Limits, stated in the tool description: at most 50 addresses across `to`, `cc` and
    `bcc`, each a single valid address; `subject` at most 998 characters with no line
    breaks; `body` at most 100,000 characters. Over-limit input is a tool error naming the
    limit, and nothing is saved.
- New optional account setting `mail.write.send_from` (one bare address, merged from
  `defaults.mail.write`). When set, drafts carry it as `From`; when unset, `From` is left
  out and Mail fills in the account's default sender. `add-mail-send` will make it required
  for tokens granted `send`.
- Counts against the token's hourly write limit; audited with `write: true` and the existing
  redaction (no addresses, subject, body or folder name in logs). Protected folders govern
  moves only (ADR-0004, decision 3): protecting `Drafts` does not block `mail_draft`; the
  `draft` grant is the control. There is no `drafts_folder` override.
- Instructions and the tool description say that a draft is never sent by pv-icloud, and that
  content returned by any tool must never be the reason to draft.
- Docs: README.md, docs/design.md, `config/pv-icloud.example.yaml` and CHANGELOG.md describe
  `draft`.

Out of scope: sending and `send_to` (`add-mail-send`); editing, replacing or sending an
existing draft; attachments and HTML bodies; quoting the original's text in a reply.

### Decisions taken from the issue and the core design

- Follows `openspec/changes/archive/2026-10-12-add-mail-write-core/design.md` decisions 4
  (`APPEND`), 5 (Drafts by name), 8 (per-token tools, annotations, instructions), 9 (limits)
  and 10 (audit and resync); `design.md` here records where this change fills in detail.
- `send_to` does not apply to drafts: a draft sends nothing (core design, decision 7,
  confirmed here).
- Maintainer answers on the proposal (PR #19): protected folders govern moves only, so a
  protected Drafts does not block `mail_draft`; `mail.write.send_from` is added now as an
  optional key that sets `From` on drafts; no `drafts_folder` override.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mail-write-tools`: `mail_draft`, its inputs, limits, reply headers, result and Drafts
  resolution; the grant, protected-folder and description requirements cover it.
- `read-only-and-egress`: the write-path command list gains `APPEND` into Drafts with exactly
  `(\Draft \Seen)`; "no tool changes iCloud data" allows creating a draft and still forbids
  sending.
- `accounts-and-auth`: `draft` becomes a known `mail_write` action; `mail.write.send_from`
  becomes an optional account setting.
- `tool-conventions`: `mail_draft` joins the per-token tool set with its annotations; write
  instructions mention drafts; audit lines for drafts carry no addresses, subject or body.

## Impact

- Code: `src/pv_icloud/config.py` (action, `send_from`), `mail/imap_write.py` (`APPEND`, `APPENDUID`),
  `mail/service.py` (building the draft, reading the original), `server.py` (tool,
  instructions); `mail/sync.py` only through the existing resync hook. A new
  `mail/compose.py` builds the message (no IMAP calls). `mail/imap.py`, `dav.py`, `cal/`,
  `contacts/` and `people` are not changed.
- Tests: `tests/fakeimap.py` gains `APPEND` with `APPENDUID` (and switches to omit it or
  fail); `tests/test_repo_policy.py` allows `.append()` only in `mail/imap_write.py` and only
  with `(\Draft \Seen)`.
- Config: `config/pv-icloud.example.yaml` shows `draft` and `send_from`.
- Dependencies: none new (standard library `email`).
- Egress: unchanged.
- Security posture: a token with `draft` can create messages in Drafts. Prompt injection could
  now put text a person might send; the person reviewing the draft in Mail is the defence,
  plus the rate limit and the instructions. Nothing is sent by the server.
- Compatibility: no breaking change; configs without `draft` behave exactly as before.
