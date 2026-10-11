# subscribed-calendars Specification

## Purpose
Subscribed (webcal) calendars, whose events iCloud does not serve: the
`calendar.subscriptions` switch, how feeds are fetched from their publishers safely (public
addresses only, size and time limits), caching, and local recurrence expansion.

## Requirements

### Requirement: Subscriptions switch
When `calendar.subscriptions` is `true` (the default) for an account, the event tools SHALL read
each in-scope subscribed calendar by fetching its feed from the publisher. When it is `false`,
subscribed calendars SHALL still be listed by `calendar_list`, with `events_available` false and
a result `note` saying fetching is turned off (`calendar.subscriptions: false`), SHALL be left out
of the event tools by default, SHALL be a tool error when named, and SHALL never be fetched.

#### Scenario: Subscriptions off
- **WHEN** an account sets `calendar.subscriptions: false` and has a subscribed `TripIt`
- **THEN** `calendar_list` shows `TripIt` with `events_available` false and a `note`,
  `calendar_events` does not search it, naming it is a tool error mentioning `turned off`, and
  no request is sent to the publisher

#### Scenario: Subscriptions on
- **WHEN** subscriptions are on and a caller lists events in a range covering a feed event
- **THEN** the feed's events are returned with the subscribed calendar's name

### Requirement: Feed URLs
A subscription's source URL SHALL be turned into a fetch URL by mapping `webcal://` and
`webcals://` (any case) to `https://` and keeping `http://` and `https://` as they are; any other
scheme, or a URL without a host, SHALL be refused. The full feed URL MUST NOT be logged or
returned in any result or error, since it often contains a private token; only its host SHALL be
shown.

#### Scenario: webcal URL
- **WHEN** the source is `webcal://cal.example/a.ics`
- **THEN** the feed is fetched from `https://cal.example/a.ics`

#### Scenario: Unsupported scheme
- **WHEN** the source is `file:///etc/passwd` or `ftp://cal.example/a`
- **THEN** the feed is refused

#### Scenario: Token in the URL
- **WHEN** a feed URL contains a private token and the feed fails
- **THEN** the result's `errors` entry and the logs show only the host, never the token

### Requirement: Public addresses only
Before each request, including each redirect hop, the server SHALL resolve the host and refuse
the feed unless every address is public (globally routable and not multicast). It SHALL connect
to an address that passed this check, sending the host name in the `Host` header and as the TLS
SNI name so the certificate is verified against it, and SHALL try the next checked address when a
connection is refused. It SHALL NOT use a proxy from the environment.

#### Scenario: Private address
- **WHEN** a feed host resolves to `127.0.0.1`, `10.1.2.3`, `192.168.1.10`, `169.254.169.254`,
  `::1` or `fd00::1`
- **THEN** the feed is refused as resolving to a non-public address and no request is sent

#### Scenario: DNS rebinding
- **WHEN** a host first resolves to a public address and would later resolve to a private one
- **THEN** the request goes to the public address that was checked, with the name in `Host` and
  SNI

#### Scenario: One address unreachable
- **WHEN** a host resolves to two public addresses and the first refuses the connection
- **THEN** the feed is fetched from the second

### Requirement: Redirects are checked hop by hop
The server SHALL follow at most 5 redirects itself, checking each new location as above. A
redirect to a scheme other than http or https, or from https to http, SHALL be refused, as SHALL
a sixth redirect.

#### Scenario: Redirect to a private host
- **WHEN** a feed redirects to a host that resolves to `10.0.0.5`
- **THEN** the feed is refused

#### Scenario: Downgrade to http
- **WHEN** an https feed redirects to an http URL
- **THEN** the feed is refused as a redirect off https

#### Scenario: Redirect loop
- **WHEN** a feed keeps redirecting to itself
- **THEN** the feed fails with `too many redirects`

### Requirement: Feed request limits
Feed requests MUST NOT carry iCloud credentials. They SHALL send `User-Agent: pv-icloud`, use a
20-second timeout for each network operation, and stop with an error once the body exceeds
20 MB. Any HTTP status other than 200 (or 304 for a revalidation) SHALL be a feed error naming
the host and status.

#### Scenario: No credentials sent
- **WHEN** a feed is fetched
- **THEN** the request has no `Authorization` header

#### Scenario: Oversized feed
- **WHEN** a feed body is larger than 20 MB
- **THEN** the fetch fails with an error saying the feed is larger than 20 MB

### Requirement: Feed parsing
An empty feed body SHALL be read as a calendar with no events. A body that is not iCalendar SHALL
be a feed error (`not an iCalendar feed`). Durations written without the `T` before hours,
minutes or seconds (such as `P12H`) in `DURATION`, `TRIGGER`, `REFRESH-INTERVAL` and
`X-PUBLISHED-TTL` SHALL be repaired before parsing.

#### Scenario: Login page instead of a feed
- **WHEN** a publisher returns an HTML page
- **THEN** the feed fails with `not an iCalendar feed`

#### Scenario: Duration without T
- **WHEN** a feed has `REFRESH-INTERVAL;VALUE=DURATION:P12H` and `DURATION:P90M`
- **THEN** it parses, with 12 hours and 90 minutes

### Requirement: Feed cache
Parsed feeds SHALL be cached in memory, keyed by fetch URL and shared by all accounts, with at
most one fetch per feed at a time. A cached feed SHALL be used without a request for 15 minutes;
after that it SHALL be revalidated with `If-None-Match` and `If-Modified-Since` when the publisher
gave an ETag or Last-Modified, and a 304 SHALL keep the cached copy. When a fetch fails and a
cached copy exists, the cached copy SHALL be served and the next attempt made 15 minutes later;
without a cached copy the failure SHALL be reported for that calendar.

#### Scenario: Cached for 15 minutes
- **WHEN** the same feed is read twice within 15 minutes
- **THEN** one request is sent

#### Scenario: Revalidation
- **WHEN** a feed with ETag `"v1"` is read again after 15 minutes
- **THEN** the request carries `If-None-Match: "v1"` and a 304 answer keeps the cached events

#### Scenario: Publisher down
- **WHEN** a cached feed is due for revalidation and the publisher answers 503
- **THEN** the cached copy is served, and no further request is sent for another 15 minutes

### Requirement: Local recurrence expansion
Feed events SHALL be expanded by the server into occurrences overlapping the requested range.
It SHALL handle `RRULE` (evaluated in the event's own wall-clock time, so a 09:00 event stays at
09:00 across daylight-saving changes), `COUNT`, `UNTIL` (a date `UNTIL` includes that whole day),
`RDATE`, `EXDATE`, and overrides with `RECURRENCE-ID` that move or cancel an occurrence, including
moving it into or out of the range. `DTSTART` SHALL always be an occurrence, even when it does not
fit the rule. Rules with `FREQ` `SECONDLY` or `MINUTELY`, and rules that cannot be parsed, SHALL
contribute no occurrences besides `DTSTART` and any `RDATE`. `RANGE=THISANDFUTURE` overrides
SHALL be applied to the single occurrence they name. Rule iteration SHALL stop after 100,000 steps
per rule. A series that fails to expand SHALL be skipped without hiding the rest of the feed. An
event without a `UID` SHALL get a stable one derived from its content, starting `nouid-`.

#### Scenario: Weekly rule across a DST change
- **WHEN** a weekly 09:00 `America/Chicago` event spans the end of daylight saving time
- **THEN** every occurrence starts at 09:00 local time, with the offset changing from -05:00 to
  -06:00

#### Scenario: UNTIL, COUNT, EXDATE and RDATE
- **WHEN** a daily rule has a UTC `UNTIL`, one date excluded by `EXDATE` and one extra `RDATE`
- **THEN** the occurrences stop at `UNTIL`, skip the excluded date and include the extra date

#### Scenario: Moved and cancelled occurrences
- **WHEN** an override moves one occurrence two days later and another override moves an
  occurrence out of the range
- **THEN** the moved occurrence appears at its new time with its own title and the other does
  not appear

#### Scenario: Unsupported or broken rules
- **WHEN** one event has `RRULE:FREQ=MINUTELY` and another `RRULE:FREQ=NONSENSE`
- **THEN** each yields only its `DTSTART` occurrence and the rest of the feed is unaffected

### Requirement: Feed events in results
Feed occurrences SHALL be returned in the same shape as iCloud events, with `recurring` true for
occurrences of a series (a rule, `RDATE` or overrides) and false otherwise, and with event ids
whose reference is `uid:<UID>`. `calendar_event` on such an id SHALL read the occurrence from the
feed.

#### Scenario: Feed event detail
- **WHEN** a caller passes the id of a feed event to `calendar_event`
- **THEN** it returns the event with its full notes
