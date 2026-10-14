# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately** through GitHub's private vulnerability reporting:
the repository's **Security** tab → **Report a vulnerability**. If you cannot use that,
email **security.admin@happycynic.com**. Do not open a public issue.

Include what you found, how to reproduce it, and the version or image digest. You should get
an acknowledgement within a week. Fixes are released as a new version, and the advisory is
published once users have had a chance to update.

Only the latest release is supported.

## Threat model in short

Private iCloud MCP gives an MCP client read access to one person's iCloud Mail, Calendar and
Contacts, and, only where the operator turns it on, limited write access to Mail. The design
assumes the server runs on hardware the account owner controls and is reached over a private
network.

**Read-only by default.** Unless mail writes are enabled (below), no tool sends, moves,
deletes or flags mail, unless calendar writes are enabled no tool changes a calendar, and
unless contact writes are enabled no tool changes a contact (each below). The shared
CalDAV/CardDAV read client can only send `PROPFIND` and `REPORT`. The IMAP read path opens
folders with `EXAMINE` and fetches with `BODY.PEEK`, so reading never changes read state. A way to change iCloud data
without the switch and a grant described below is a vulnerability.

**Optional mail write access** ([ADR-0004](docs/adr/0004-opt-in-mail-write.md)). Writes
need both the deployment switch `PV_ICLOUD_MAIL_WRITE=enabled`
and a per-token grant (`move`, `archive`, `delete`, `draft`, `send`, each separately). Deletes
only move mail to Deleted Messages, where it is recoverable until iCloud empties that folder;
the server never permanently deletes. A message is removed from its source folder only after
its copy in the destination is confirmed. Moves into or out of protected folders are refused,
and each token has a write rate limit (default 30 per hour). These are vulnerabilities: any
write without the switch and the matching grant; any permanent deletion; any change to a
protected folder; a send to an address outside the token's `send_to` list; any IMAP write
command other than those ADR-0004 lists.

**Optional calendar write access** ([ADR-0005](docs/adr/0005-opt-in-calendar-write.md)).
Writes need both `PV_ICLOUD_CALENDAR_WRITE=enabled` and
`calendar_write: true` on the token. They create, update or delete one event (or one
occurrence of a series) at a time, count against the token's hourly write limit, only in
calendars the account may change (its own, and calendars shared with it read-write; never
subscribed or read-only ones, never `protected_calendars`),
always as conditional requests that cannot overwrite a change made elsewhere, and never with
attendees: iCloud emails invitations for any attendee, so events with attendees other than
the account cannot be changed through pv-icloud. These are vulnerabilities: a calendar write
without the switch and the grant; a write to a calendar the rules above exclude; any write
that makes iCloud email someone other than the account; any DAV method other than those
ADR-0005 lists.

**Optional contact write access** ([ADR-0006](docs/adr/0006-opt-in-contact-write.md); being
implemented). Writes need both `PV_ICLOUD_CONTACTS_WRITE=enabled` and a per-token grant
(`create`, `update`, `delete`, each separately; delete is permanent, as iCloud keeps no
per-contact trash). They change one contact at a time, as conditional requests, and an update
changes only the properties it was asked to change, keeping everything else in the card. They
never touch the me-card's existence, contact groups themselves, or contacts in excluded groups.
These are vulnerabilities: a contact write without the switch and the matching grant; any
change to a property the call did not ask to change; any write to a contact in an excluded
group; any DAV method other than those ADR-0006 lists.

Write access raises the stakes of prompt injection: untrusted mail could try to make a model
move, delete, draft or send. The defences above lower that risk but cannot remove it, and a
sent message cannot be recalled. Grant only the actions a client needs; prefer `draft` over
`send`; give `send` a `send_to` allowlist, or grant it only to clients that confirm each send
with a person.

**Authentication and isolation.** Every `/mcp` request needs a bearer token; the config
holds only SHA-256 hashes, compared in constant time. Each token belongs to exactly one
account, and each account has its own index file, connection pool and sync worker. A way to
read another account's data, or to call a tool without a valid token, is a vulnerability.
`/healthz` is open but returns only a status unless called with a valid token, and then only
for that token's account.

**Content is untrusted.** Mail, calendar and contact text is written by other people and can
contain prompt-injection attempts. Results are marked `content_trust: "untrusted"` and tool
descriptions tell the model to treat them as data. This lowers the risk but cannot remove it:
the MCP client and the person using it remain responsible for what the model does with
untrusted text. HTML mail is converted to text without fetching anything (images and remote
content are dropped).

**No TLS in the server.** It speaks plain HTTP and logs a warning when bound to a non-loopback
address. Run it on a private network (LAN, VPN) or behind a TLS reverse proxy; never expose it
directly to the internet. Anyone who can read the traffic can read the bearer token.

**Data at rest.** The per-account index under `/data` contains mail bodies. Put it on an
encrypted disk. iCloud credentials (Apple ID and app-specific password) come from environment
variables, never from the config file. An app-specific password gives access to the whole
iCloud account over IMAP, CalDAV and CardDAV; revoke it at appleid.apple.com if the host is
compromised.

**Logs.** Audit lines record tool, account, counts and timings; search text and ids are
redacted to their length. Message content, credentials and subscribed-calendar feed URLs are
never logged.

**Outbound requests.** The server connects to iCloud (`imap.mail.me.com`,
`caldav.icloud.com`, `contacts.icloud.com` and their `pNN-` hosts, plus `smtp.mail.me.com`
only for tokens granted `send`). If an account turns on
subscribed calendars (`calendar.subscriptions: true`; off in the example config), it also
fetches each feed from its publisher: only http(s), only public addresses, with the
connection made to the address that passed that check (so DNS rebinding cannot redirect it to
your LAN), no credentials sent, and size and time limits. A way to make the server reach a
private address is a vulnerability. [`docs/egress.md`](docs/egress.md) shows how to enforce
the same limits with a host firewall.

**Container.** The image runs as a non-root user with a read-only root filesystem; the example
Compose file also drops all capabilities and sets `no-new-privileges`.

## Out of scope

- Attacks that need control of the host, the Docker daemon or the iCloud account itself.
- Prompt injection that only works because a client ignores the untrusted-content marking,
  unless the server makes it worse (for example, by presenting untrusted text as trusted).
- Exposing the server to the internet without TLS against the guidance above.
