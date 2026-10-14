# ADR-0004: Opt-in mail write access

- **Status:** Accepted
- **Date:** 2026-10-12
- **Context:** Until now `pv-icloud` has been read-only by construction (ADR-0002, SECURITY.md).
  Issue #14 asks for triage and replies from MCP clients: move, archive, delete, draft and
  send, granted per bearer token because different clients deserve different trust. Writes
  change the threat model: prompt injection in untrusted mail could now cause actions, and a
  sent message cannot be recalled. Calendar and Contacts are not part of this decision and
  stay read-only. *(Amended 2026-10-14: calendar event writes are decided separately in
  [ADR-0005](0005-opt-in-calendar-write.md); Contacts stay read-only.)*

## Decision

1. **Off unless the deployment turns it on.** Mail writes exist only when the environment
   variable `PV_ICLOUD_MAIL_WRITE=enabled`. Unset, empty or `disabled` leaves writes off; any
   other value is a config error. While off, the write modules are not imported and a token
   with a write grant is a config error. "Read-only" stays true for every deployment that has
   not opted in.
2. **Granted per token, per action.** A token lists the actions it may use
   (`mail_write: [move, archive, delete, draft, send]`); every action is off by default and
   each can be granted alone. Tools that a token was not granted are absent from its
   `tools/list` and refused as unknown if called. Two tokens on one Apple ID can differ. A
   token without `mail_write` behaves exactly as before this decision.
3. **Protected folders only tighten.** The effective list is the union of the defaults, the
   account's list and the token's list. Moving into or out of a protected folder fails before
   any command is sent.
4. **Writes are confined to two modules.** IMAP writes live only in `mail/imap_write.py`,
   sending only in `mail/smtp.py`. The read path (`mail/imap.py`) keeps `EXAMINE` and
   `BODY.PEEK` only, and the DAV base keeps `PROPFIND` and `REPORT` only.
5. **Guarded move, because iCloud has no `MOVE`.** iCloud advertises `UIDPLUS` and `UNSELECT`
   but not `MOVE` (checked 2026-10-12). Move, archive and delete therefore run, for one
   message: `SELECT` → `UID COPY <uid>` → require a `COPYUID` naming exactly that UID →
   `UID STORE <uid> +FLAGS.SILENT (\Deleted)` → `UID EXPUNGE <uid>` → `LOGOUT`. **A message is
   removed from its source only after its copy in the destination is confirmed.** Each partial
   failure stops and reports where the message is; a message is never lost. These single-UID
   forms and `APPEND` are the only commands that change mail. Plain `STORE`, `COPY` and
   `EXPUNGE`, `CLOSE`, `MOVE`, UID sets and ranges, other flags, and folder commands
   (`CREATE`, `DELETE`, `RENAME`, `SUBSCRIBE`) stay forbidden everywhere. A connection that ran
   `SELECT` is logged out, never returned to the read pool.
6. **Delete means Deleted Messages.** Delete moves the message to Deleted Messages; it is
   recoverable there until iCloud empties that folder. The server never permanently deletes.
7. **Draft and send are separate grants.** Draft appends to Drafts; nothing leaves the
   account. Send goes through iCloud SMTP (`smtp.mail.me.com:587`, STARTTLS) and accepts an
   optional per-token recipient allowlist `send_to` (exact addresses or `@domain`). With a
   list, every To, Cc and Bcc address must match before anything is sent; without one, the
   token may send to anyone and a startup warning names it. After sending, a copy is saved to
   Sent Messages only if iCloud has not already saved one (checked by Message-ID).
8. **A per-token write rate limit.** Every write call counts against `max_writes_per_hour`
   (default 30), whatever its outcome. The count is held in memory and resets on restart.
9. **Audited without content.** Write calls get the usual audit line plus `write: true`; ids,
   folders, addresses, subjects and bodies are redacted as for reads.
10. **Phased implementation.** Core (switch, grants, protected folders, rate limit,
    move/archive/delete), then draft, then send, each as its own OpenSpec change and pull
    request.

## Consequences

- The project's promise becomes "read-only by default". README, SECURITY.md, docs/design.md,
  CLAUDE.md and the specs say so; a write without the switch and a grant is a vulnerability.
- Prompt injection can now cause actions. The defences are layered: deployment switch,
  per-token grants, protected folders, no batch tools, the rate limit, recoverable deletes,
  instructions that untrusted content is never a reason to act, and for sending the
  allowlist. They lower the risk but cannot remove it; operators should grant `send`, and
  `send` without `send_to`, only to clients that confirm each action with a person.
- The guarded move is not atomic: a failure can leave a duplicate, or a source copy marked
  `\Deleted` beside the confirmed destination copy. Both are visible and recoverable.
- `SELECT` clears `\Recent` in the source folder (session state that iCloud clients do not
  show); `\Seen` and other flags are unchanged.
- Changing the list of allowed write commands needs a new ADR.
