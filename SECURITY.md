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
Contacts. The design assumes the server runs on hardware the account owner controls and is
reached over a private network.

**Read-only by construction.** No tool sends, moves, deletes or flags mail, or changes events
or contacts. The IMAP layer opens folders with `EXAMINE` and fetches with `BODY.PEEK`, so read
state never changes; the CalDAV/CardDAV client can only send `PROPFIND` and `REPORT`. A bug
that let a tool call change iCloud data is a vulnerability.

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
`caldav.icloud.com`, `contacts.icloud.com` and their `pNN-` hosts). If an account turns on
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
