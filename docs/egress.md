# Restricting outbound traffic

pv-icloud only needs to reach a few hosts. Limiting the container to those hosts means that a
bug in a parser (mail, iCalendar) cannot be used to reach your LAN or send data elsewhere.
The server already refuses to fetch subscribed-calendar feeds from private addresses; the
rules below add the same limit at the network level, for everything the container does.

## What the server connects to

| Destination | Port | Used for |
|---|---|---|
| `imap.mail.me.com` | 993 | Mail (IMAP over TLS) |
| `caldav.icloud.com`, `pNN-caldav.icloud.com` | 443 | Calendar (CalDAV) |
| `contacts.icloud.com`, `pNN-contacts.icloud.com` | 443 | Contacts (CardDAV) |
| Your DNS resolver | 53 | Name lookups |
| Publishers of subscribed calendars | 443 (or 80) | Only with `calendar.subscriptions: true` |

All the iCloud hosts above resolved inside **17.0.0.0/8** (Apple's own network) when checked
on 2026-10-09. Apple can change this; if mail or calendar stop working after you apply the
rules, check with `getent ahosts imap.mail.me.com` and `getent ahosts caldav.icloud.com`.

Subscribed-calendar publishers can be any public host, so with subscriptions on the best a
network rule can do is block private and local addresses. Leave subscriptions off
(`calendar.subscriptions: false`, the default in the example config) for the tightest rules.

## Docker on Linux (iptables)

Docker routes container traffic through the host's `DOCKER-USER` chain, so rules there
apply to the container. (Docker Desktop and OrbStack run containers in a VM and do not use
the host's iptables; use their own network settings instead.)

1. Give the container its own network with a fixed subnet. In `compose.yml`, uncomment the
   `networks:` sections. Pick a subnet that is free on your host; this guide uses
   `172.30.71.0/24`.

2. Create the rules. Set `RESOLVER` to the DNS server the host uses (see
   `/etc/resolv.conf`, or `resolvectl status`).

   ```sh
   SUBNET=172.30.71.0/24
   RESOLVER=192.168.1.1
   iptables -N PV_ICLOUD_EGRESS 2>/dev/null || iptables -F PV_ICLOUD_EGRESS
   # Replies to MCP clients and to connections the rules below allowed.
   iptables -A PV_ICLOUD_EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
   iptables -A PV_ICLOUD_EGRESS -d "$RESOLVER" -p udp --dport 53 -j RETURN
   iptables -A PV_ICLOUD_EGRESS -d "$RESOLVER" -p tcp --dport 53 -j RETURN
   # iCloud: IMAP, CalDAV and CardDAV.
   iptables -A PV_ICLOUD_EGRESS -d 17.0.0.0/8 -p tcp -m multiport --dports 443,993 -j RETURN
   iptables -A PV_ICLOUD_EGRESS -j REJECT
   iptables -C DOCKER-USER -s "$SUBNET" -j PV_ICLOUD_EGRESS 2>/dev/null \
     || iptables -I DOCKER-USER -s "$SUBNET" -j PV_ICLOUD_EGRESS
   ```

   **With subscriptions on**, replace the `REJECT` line with these, so feeds can come from
   any public host but nothing private is reachable:

   ```sh
   for net in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 \
              172.16.0.0/12 192.168.0.0/16 224.0.0.0/3; do
     iptables -A PV_ICLOUD_EGRESS -d "$net" -j REJECT
   done
   iptables -A PV_ICLOUD_EGRESS -p tcp -m multiport --dports 80,443 -j RETURN
   iptables -A PV_ICLOUD_EGRESS -j REJECT
   ```

   If your resolver is on the LAN it is still allowed: its `RETURN` rule comes first.

3. Make the rules survive a reboot with your distribution's usual tool
   (`iptables-persistent`, a systemd unit, or your configuration management). Docker keeps
   `DOCKER-USER` across its own restarts but not across a host reboot.

4. Check them. The first command should print `blocked`; the second should succeed.

   ```sh
   docker compose exec pv-icloud python -c "import socket
   try: socket.create_connection(('1.1.1.1', 443), 3); print('open: rules not applied')
   except OSError: print('blocked')"
   docker compose exec pv-icloud python -c "import socket
   socket.create_connection(('imap.mail.me.com', 993), 5); print('iCloud reachable')"
   ```

The default Docker network is IPv4-only, so the container has no IPv6 route to filter. If you
enable IPv6 on its network, add the same rules with `ip6tables`.

## Other ways

- **Kubernetes:** a `NetworkPolicy` with `egress` to `17.0.0.0/8` on 443 and 993 plus DNS.
- **Firewall appliance:** match on the container host's address, or on the subnet above if it
  is routed rather than masqueraded.
