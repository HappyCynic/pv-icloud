# Proposal

## Why

Implements #14 (first of three changes). MCP clients can search and read iCloud Mail through
pv-icloud but cannot act on it, so triage (filing, archiving, cleaning up) still happens in
another app. Different clients deserve different trust, so write access has to be granted per
bearer token, not per server, and has to be off unless an operator turns it on.

At the maintainer's request #14 is split into three OpenSpec changes, each implemented and
archived on its own:

1. **`add-mail-write-core`** (this change): the deployment switch, per-token config and auth,
   per-token tools and instructions, protected folders, the write rate limit, and the guarded
   move behind `mail_move`, `mail_archive` and `mail_delete`.
2. `add-mail-draft` (separate issue): `mail_draft`.
3. `add-mail-send` (separate issue): `mail_send` with the optional `send_to` allowlist.

`design.md` keeps the whole design, including the parts the later two changes will use, so
the three fit together. Only this change's part is in its spec deltas and tasks.

## What Changes

- **This change ends the read-only guarantee for Mail.** It needs the design discussion that
  CLAUDE.md requires before any write support; this proposal and its `design.md` are that
  discussion. Calendar and Contacts stay read-only: the DAV base still sends only `PROPFIND`
  and `REPORT`. No new outbound host is added (SMTP comes with `add-mail-send`).
- **Deployment switch:** `PV_ICLOUD_MAIL_WRITE=enabled` (default off). Without it the write
  module is never imported and any `mail_write` grant in the config is a config error.
- Three new mail tools, each enabled independently per bearer token and all off by default:
  - `mail_move`: move one message to another in-scope folder.
  - `mail_archive`: move one message to the account's archive folder (`Archive` by name).
  - `mail_delete`: move one message to `Deleted Messages`. The message stays recoverable
    there until iCloud empties that folder; pv-icloud never removes it from there.
- New per-token config: `mail_write: [move, archive, delete]` (default empty),
  `protected_folders` and `max_writes_per_hour` (default 30). Tools a token has not been
  granted are left out of its `tools/list` and refused if called anyway. A token without
  `mail_write` sees exactly today's tools, annotations and server instructions.
- New per-account config under `mail.write`: `protected_folders` (messages cannot be moved,
  archived or deleted into or out of them) and `archive_folder`. Protection is the union of
  the defaults, account and token lists, so a token can only tighten it.
- **Guarded move.** iCloud does not advertise `MOVE`, so move, archive and delete use, on one
  selected folder and one UID only: `UID COPY`, a required `COPYUID` response, then
  `UID STORE <uid> +FLAGS.SILENT (\Deleted)` and `UID EXPUNGE <uid>`. **A message is removed
  from its source only after its copy in the destination is confirmed.** Plain `STORE`,
  `COPY`, `EXPUNGE`, `CLOSE`, `MOVE` and multi-UID forms stay forbidden. The new IMAP
  commands (`SELECT` and those three single-UID forms) are sent only from a separate write
  path, `mail/imap_write.py`. The read path is unchanged (`EXAMINE` and `BODY.PEEK` only),
  and read tools still never change `\Seen` or other flags.
- **Rate limit:** every write call counts against the token's hourly limit. The limit is held
  in memory and resets when the server restarts.
- Every write is audit-logged (action, account, token label, outcome, `write: true`) with no
  message content, addresses, subjects or folder names.
- Docs: README.md, docs/design.md and CONTRIBUTING.md are updated with the implementation.
  The rule changes (CLAUDE.md's read-only hard rule, `.github/CODEOWNERS`,
  `openspec/config.yaml`, SECURITY.md and an ADR) are made by the maintainer in a separate
  pull request **before** implementation starts; they are not tasks of this change.

Out of scope here: `mail_draft`, `mail_send` and `send_to` (the later changes); permanent
delete; flagging and marking read or unread; folder create, rename or delete; Calendar and
Contacts writes; batch (multi-id) actions.

### Decisions from the maintainer's review on #14

- Protected folders: **both** account and token lists, as a union, so a token can only
  tighten protection.
- iCloud advertises `UIDPLUS` and `UNSELECT` but not `MOVE` (checked against a live account),
  so moves use the guarded copy-then-expunge sequence above, and `UIDPLUS` is required.
- Deployment switch `PV_ICLOUD_MAIL_WRITE` and a per-token write rate limit, held in memory.
- iCloud gives Drafts and Archive no special-use attribute, so they are found by name.
- `draft` and `send` stay separate permissions, proposed in their own changes.

## Capabilities

### New Capabilities

- `mail-write-tools`: `mail_move`, `mail_archive` and `mail_delete`, their arguments, results
  and limits, the guarded move, protected folders, archive and Deleted Messages resolution,
  and the per-token rate limit. `add-mail-draft` and `add-mail-send` will add to it.

### Modified Capabilities

- `read-only-and-egress`: the "no tool changes iCloud data" and "read-only IMAP commands"
  requirements are narrowed to read tools and the read path; a write-path command list is
  added; Calendar and Contacts are stated to stay read-only.
- `accounts-and-auth`: the `PV_ICLOUD_MAIL_WRITE` switch; per-token `mail_write`,
  `protected_folders` and `max_writes_per_hour`; per-account `mail.write` settings.
- `tool-conventions`: the fixed read-only tool set becomes the read tools plus whichever
  write tools the caller's token has; annotations and instructions for write tools; write
  audit lines; the write-limit error.

## Impact

- Code: `src/pv_icloud/config.py`, `auth.py`, `server.py`, `mail/service.py`, a new
  `mail/imap_write.py` (write-path IMAP) and a new `ratelimit.py`; `mail/sync.py` is asked to
  resync the folders a write touched. `mail/imap.py` and `dav.py` are not changed.
- Tests: `tests/fakeimap.py` gains `SELECT`, the guarded single-UID forms,
  `UIDPLUS`/`UNSELECT` capabilities (no `MOVE`, as on iCloud), and `Archive` and
  `Deleted Messages` folders with iCloud's attributes; `tests/test_repo_policy.py` gains
  static checks on IMAP call sites.
- Config: `config/pv-icloud.example.yaml` documents the switch and the new keys, all off.
- Dependencies: none new.
- Egress: unchanged.
- Security posture: a token with write access can now move mail between folders and into
  Deleted Messages. Prompt injection from untrusted mail can now cause such moves, not just
  reads; see `design.md` for mitigations.
- Compatibility: no breaking change. Existing configs keep working unchanged and behave
  exactly as today.
