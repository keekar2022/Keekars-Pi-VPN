---
title: Keekar's Pi VPN — project notes
Concept: Mukesh Kesharwani
Contact: mukesh.kesharwani@adobe.com
---

# Keekar's Pi VPN — project notes

Everything that isn't a step-by-step execution instruction: why the stack
was chosen, the full incident narratives behind the gotchas listed in
[`docs/RUNBOOK.md`](RUNBOOK.md), and the design rationale for running
WireGuard in both client and server mode on one device. Read the runbook
first for "what to do"; come here for "why, and what actually happened."

## Part 1 — Stack research and base app

### Why this stack

Target hardware is the original **Pi Zero W**: single-core 1GHz ARMv6
(BCM2835/ARM1176JZF-S), 512MB RAM shared with the GPU — the most
resource-constrained member of the Pi family, verified via `uname -a`
(`armv6l`) rather than assumed.

- **Node.js ruled out**: dropped official ARMv6 support after v11.15.0;
  unofficial builds would be a permanent maintenance burden, plus a higher
  baseline memory footprint than needed.
- **Python (FastAPI) + Uvicorn**: preinstalled interpreter, `psutil`
  covers CPU/memory/network stats in a few lines, FastAPI has native async
  WebSocket support (though the shipped monitor page ended up using plain
  5s HTTP polling instead — see the CSP incident below for why),
  Pydantic gives free input validation on every config-mutating endpoint.
  Runs as a single Uvicorn worker — one ARMv6 core can't usefully
  parallelize a multi-worker pool anyway.
- **piwheels.org** as the pip index for on-device installs — `psutil`,
  `pyroute2`, and `cryptography` have no upstream `armv6l` wheels on plain
  PyPI, so without piwheels pip compiles from source, slow on this CPU.
- **pyroute2** for routing (not shelling out to `ip route`) — avoids
  parsing CLI output and any shell-injection surface, gives structured
  errors.
- **NetworkManager via `nmcli`** (list-args subprocess, never `shell=True`)
  for Wi-Fi/interface config, gated by a scoped polkit rule rather than
  broad sudo.
- **Server-rendered Jinja2 + htmx + vanilla JS, no SPA build step** —
  skips a Node/webpack toolchain on-device entirely, and vendoring
  htmx/Chart.js locally (not CDN) means the tool that fixes broken network
  config doesn't itself depend on working internet access to render.

### SSO integration specifics (Authentik)

`sso.keekar.au` is Authentik, which has two consequences the initial
design didn't know about until tested against the real IdP (both are now
baked into `app/auth.py` and called out in the runbook so they don't
regress):
- OIDC discovery is per-application (`/application/o/<slug>/...`), not at
  the issuer root — hence `SSO_APPLICATION_SLUG` as a setting distinct
  from `SSO_CLIENT_ID`.
- authlib doesn't enable PKCE by default — needs
  `"code_challenge_method": "S256"` explicitly in `client_kwargs`.

Credentials are the same ones already used by Guardian agent / Keekar's
Home Hub, supplied directly into `/etc/pi-config-ui/sso.env` (0600,
gitignored) — never hardcoded or committed.

### Incident: TLS trust gap on a brand-new Let's Encrypt root

`sso.keekar.au`'s cert chains through `ISRG Root YR` (Let's Encrypt's 2026
"Generation Y" hierarchy, live since 2026-01-07), which was not yet in
Python's `certifi` bundle, nor the Pi's OS `ca-certificates` package
(`20250419` — confirmed via `apt-cache policy` that no newer version
existed in the Raspbian trixie repo at the time). curl on a Mac with a
live-updated system trust store worked fine while the Pi failed with
`CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` —
diagnosed by comparing `openssl s_client -showcerts` output between the
two. Fixed two ways: `app/auth.py` uses the `truststore` package so the
OIDC HTTP client reads the OS-native trust store instead of a pinned
`certifi` bundle (guards against future `certifi` lag generally), and the
missing root was fetched from `letsencrypt.org/certs/gen-y/root-yr.pem`,
verified (self-signed, subject `CN=Root YR`, sha256 fingerprint
`E5:7B:7E:6F:...:A8:6F`), and installed via `update-ca-certificates`.

### Incident: httpx default timeout too short for this CPU

Even after the TLS fix, `/auth/login` intermittently failed with
`httpx.ConnectTimeout`. A plain `curl` to the same discovery URL from the
Pi took ~5.7s — TLS handshake math is slow on a single 1GHz ARMv6 core
with no crypto acceleration, and httpx's default timeout is 5s total.
Fixed with an explicit `"timeout": 20` in `client_kwargs` — a direct
consequence of the hardware constraint the whole design was built around.

### Incident: two independent DNS breakages

- `/etc/resolv.conf` was a plain file instead of the symlink the
  `resolvconf` package expects, so NetworkManager's DNS updates were
  silently dropped by the update hook's own safety check.
- A stale `/etc/resolvconf/resolv.conf.d/head` file hardcoded the router
  as the first nameserver, ahead of the resolver that actually knew the
  internal DNS zone — the router answered NXDOMAIN for unknown internal
  names instead of forwarding, so the correct resolver (listed later in
  `resolv.conf`) was never tried.

Both are Debian/`resolvconf` quirks independent of this app; fixes are in
the runbook §1.

### Incident: `.local` redirect URI collided with mDNS (RFC 6762)

The original `SSO_REDIRECT_URI` used `pi-config-ui.local`. `.local` is
hard-reserved for Bonjour/mDNS on essentially every modern OS; this Pi's
`/etc/nsswitch.conf` has `hosts: files mdns4_minimal [NOTFOUND=return]
dns` — `.local` names are intercepted by `mdns4_minimal` first, and
`[NOTFOUND=return]` means resolution **stops there**, never falling
through to ask real DNS servers, regardless of what records exist for it
anywhere. Fixed by moving to a real name under the existing DNS zone
(`pi-config-ui.keekar.au`), which needed three coordinated changes: a DNS
A record, `SSO_REDIRECT_URI` updated on the Pi, and the redirect URI
updated on the Authentik client itself.

### Incident: session cookie `SameSite=Strict` broke every login

Login failed with `{"detail":"Login failed"}` on every attempt, including
clean single-tab tries. Diagnostic logging added to `app/auth.py`
(`sso_login_initiated`/`sso_callback_failed`, including whether the
session cookie was present on the callback request) proved the exact
mechanism from a real capture:
```
sso_login_initiated had_session_cookie=False new_state='tkolfuumMHAgLH4hBgQnzrCqkZVcg1'
sso_callback_failed error='mismatching_state: CSRF Warning! ...' has_session_cookie=False
  state_param='tkolfuumMHAgLH4hBgQnzrCqkZVcg1' session_keys=[]
```
The `state` value itself matched between login and callback — the actual
problem was `has_session_cookie=False` on the callback: the cookie
`/auth/login` set (where authlib stashes the expected
`state`/`nonce`/PKCE `code_verifier`) never came back when Authentik
302-redirected the browser to `/auth/callback`, because `SessionMiddleware`
was configured `same_site="strict"`. The callback is a top-level
navigation resulting from a cross-origin redirect — exactly what
`SameSite=Strict` is defined to block (unlike `Lax`, which explicitly
permits cookies on top-level cross-site *GET* navigations). Fixed by
switching to `same_site="lax"`, which still blocks the cookie on
cross-site POST/subresource requests, so it doesn't meaningfully weaken
CSRF protection for this app's state-changing endpoints (all POST/DELETE,
never GET). Regression-tested — see runbook §13.

### Incident: CSP silently blocked every page's JavaScript

While investigating why the Monitor page showed no data even after login
started succeeding, found `app/main.py`'s CSP header
(`default-src 'self'; img-src 'self' data:`) has no `'unsafe-inline'`, no
nonce, no hash. Under CSP semantics, `'self'` does not cover inline
`<script>` blocks — only externally-loaded scripts count as same-origin.
Every page (`dashboard.html`, `network.html`, `routing.html`,
`wireguard.html`) had its actual logic in an inline `<script>` block, all
silently blocked in an enforcing browser (Safari, in this case) — no
console-visible error, no failed network request, the code just never
ran. This had been broken since the CSP header was first added; an
earlier attempt to fix "no data on the monitor page" by switching from a
WebSocket to HTTP polling never actually fixed anything, because the code
that would call either one never ran either way. Fixed by extracting every
page's script into an external `app/static/*.js` file, referenced via
`<script src="...">` — already same-origin under the existing CSP, no
policy weakening needed. Regression-tested — see runbook §13.

### Incident: dashboard polling raced its own login flow

A related, more subtle bug: the dashboard's 5-second stats poll used
`fetch()` with default redirect-following. If a *different* browser tab
still had the dashboard open when its session lapsed, that tab's
background poll would silently follow the resulting redirect chain all
the way to `/auth/login` — generating a fresh OAuth `state` and
overwriting the *shared* session cookie's stored value, clobbering
whatever login was actually in progress in another tab. Fixed by setting
`redirect: "manual"` on the poll's `fetch()` call and treating an
`opaqueredirect` response as "session expired, stop polling" instead of
letting it silently kick off a new OAuth handshake in the background.

### Incident: checkbox alignment fought the generic `label` flex rule

Two checkboxes on the WireGuard page (the tunnel-overlap "create anyway"
confirmation and the peer form's "Generate preshared key") rendered
inconsistently with each other. Root cause: `app/static/style.css` has a
site-wide `label { display: flex; flex-direction: column; ... }` rule,
written for the "label text above input" pattern every text field on this
app uses. Applied to a bare checkbox + trailing description text, that
same rule stacks the checkbox above/below its own label text instead of
placing it inline beside it — but only for the peer form's checkbox. The
overlap-confirmation checkbox looked different again because its wrapper
`<label>` was toggled with an **inline** `style="display:none"` /
`style.display = "block"` from JS — inline styles win over the stylesheet
rule regardless of specificity, so that one fell back to plain inline
flow (checkbox and text side by side) instead. Two different-looking bugs,
same root cause hitting each element differently. Fixed with a dedicated
`label.checkbox-label { flex-direction: row; align-items: center; }` rule
applied to both, and replacing the inline-style toggling with a `.hidden`
utility class via `classList.add/remove` so nothing overrides the
stylesheet rule anymore (see `docs/RUNBOOK.md` §11). Also moved the
overlap-confirmation checkbox to inside `<form id="tunnel-form">`,
directly above the Create button — it had been a stray sibling *after*
the form's closing tag, which is why it visually floated below the whole
form instead of sitting next to the control it actually gates.

### Incident: dashboard downtime figure undercounted by the entire boot window

Building the dashboard's "Last downtime" stat (`app/monitor.py`), the
first implementation computed downtime as `psutil.boot_time() -
last_seen`. Live-tested via an actual `sudo reboot` on the device: it
reported ~30 seconds of downtime for a reboot cycle that visibly took
close to 2.5 minutes end to end. Root cause: `boot_time()` marks when the
*kernel* started, not when the device is actually usable — on this Pi
Zero W, `NetworkManager.service` alone takes ~57s to bring up Wi-Fi
(`systemd-analyze blame`, §14), and `pi-config-ui` itself doesn't even
start until near the end of that ~2-minute boot sequence. Comparing
`boot_time()` to the pre-outage `last_seen` timestamp silently excluded
the entire boot/userspace-startup window. Fixed by computing downtime as
`now - last_seen`, where `now` is read at the very first heartbeat tick
*after this service has already started* — a far closer proxy for "back
on the network." Re-tested with a second real reboot: reported ~150
seconds, matching the observed outage. Separately, `boot_time()` itself
is unreliable for *any* purpose (including the "is this a new boot"
comparison used to distinguish a real reboot from an ordinary
`Restart=on-failure` service bounce) until `systemd-timesyncd` has
actually synced the clock — this Pi has no hardware RTC and no
`fake-hwclock`, so the wall clock is wrong from cold boot until NTP
corrects it, and the kernel recomputes `boot_time()` whenever that
correction happens. Every read/compare/persist of `boot_time()` is gated
on `/run/systemd/timesync/synchronized` existing first; see
`docs/RUNBOOK.md` §5e for the resulting design.

## Part 2 — Dual-mode WireGuard design (client wg0-role + server Bpl-Home)

### Requirements and the decision that shaped the design

The device needed to act as both a WireGuard **client** (dialing out to a
remote endpoint) and a WireGuard **server** (accepting inbound peers)
simultaneously. Confirmed with the device owner: server-side peers must
get access to the local LAN and internet through it, but must **not** be
able to reach the remote endpoint through the client tunnel — the two
stay strictly separate, no bridging.

### Interface naming

The client tunnel already existed under the name `Syd-Home` (not
literally `wg0`) by the time dual-mode server support was designed.
Decision (confirmed with the device owner): kept as `Syd-Home` rather than
renamed — the interface name is cosmetic; "wg0" in requirements docs means
"the client tunnel" generically, not a literal interface name.

### Server tunnel (Bpl-Home) design

Originally deployed and named generically as `wg1`; renamed to `Bpl-Home`
(2026-08-27) to match the "Bpl" identity already used elsewhere for this
device (`vpn.bpl.keekar.au`, the TLS cert hostname) and to make the admin
UI's "Server tunnels" table unambiguous next to the client tunnel
`Syd-Home`, which dials a *different* location's server. The rename only
touches the interface/systemd-unit name and the `PostUp`/`PostDown`
iptables lines that reference it literally (see runbook §10) — WireGuard
peers authenticate by public key and `Endpoint` host:port, never by
interface name, so no already-issued peer `.conf` needed to change.

- Subnet (`10.10.10.0/24` in this deployment) chosen to not overlap the
  local LAN or any client tunnel's remote subnets.
- NAT/forwarding rules (`MASQUERADE` on the LAN-facing interface, explicit
  `FORWARD` `ACCEPT` only for `<server-tunnel> <-> <lan-iface>`) live in
  the tunnel's own `PostUp`/`PostDown`, not injected by the app's
  create-tunnel API — that API only writes a bare `[Interface]` block by
  design (smaller templating/injection surface), so NAT setup is a
  one-time system-level step, documented in the runbook.
- `iptables -P FORWARD DROP` as the default policy is the actual
  enforcement of "peers can't reach the client tunnel" — discovered during
  setup that the *default* `FORWARD` policy on a fresh Trixie image is
  `ACCEPT`, which would have silently allowed bridging between the server
  and client tunnels the moment both existed, regardless of the "no rule
  references the other tunnel" intent. Setting an explicit default-deny
  closes that gap instead of relying on absence-of-a-rule alone.

### Safety fix applied during setup

While setting this up, found `wg-quick@Syd-Home.service` **enabled but
inactive** — meaning it wasn't running (so SSH worked), but would have
auto-started on the next reboot with `AllowedIPs` that, at the time,
included the local LAN subnet, reproducing the SSH-lockout incident below.
Disabled its auto-start immediately as a pure safety fix (no functional
change to anything running). It was later independently re-enabled with
the `AllowedIPs` correctly narrowed to exclude the local LAN (see below) —
confirmed safe by checking `ip route` showed the local LAN still routing
via the physical interface, not the tunnel.

### DDNS for Bpl-Home's public endpoint (2026-08-27)

The device is expected to be shipped between locations (different
city/ISP each time), which means its public IP isn't stable. Checking
`vpn.bpl.keekar.au` (the `CERT_CN` hostname above) surfaced that it
intentionally resolves to this Pi's *private* LAN IP, not a public one —
correct for its actual purpose (keeping the admin UI off the public
internet) but useless as the address a remote peer would dial to reach
`Bpl-Home` from outside the LAN, and unrelated to which city the device is
in. There was no separate public-facing hostname for that at all, and no
DDNS mechanism on the device — confirmed by checking for `ddclient`/
`inadyn`/any DDNS systemd unit or cron entry (none existed); the only
Cloudflare API usage on the Pi was `acme.sh`'s renewal cron, which only
ever touches a `_acme-challenge` TXT record, never an A record.

Fixed by creating a new, separate hostname (`wg.bpl.keekar.au`) whose only
job is being the WireGuard `Bpl-Home` tunnel's real public endpoint, and
adding `deploy/maintenance.sh`'s `cmd_ddns_update` (cron, every 10
minutes) to keep it pointed at whatever the device's current public IP
is — reusing the Cloudflare API token already provisioned for TLS
(§5b/§5c in the runbook) rather than requesting a second credential.
Deliberately update-only, never create-on-demand: a typo'd hostname
should fail loudly (a clear, repeating log error) rather than silently
create an unexpected record in a production DNS zone. The full setup
procedure, including the one-time manual record creation this implies, is
in [`docs/RUNBOOK.md`](RUNBOOK.md) §5c.

This still isn't sufficient on its own for a peer to reach `Bpl-Home` from
a new location — the physical router at wherever the device currently is
still needs its own UDP `51820` port-forward to the Pi's LAN IP, a manual
step outside this project's control that has to be redone at every new
network, DNS notwithstanding.

While implementing the above, also found and fixed a leftover from the
earlier `wg1` → `Bpl-Home` rename: `/etc/sysctl.d/99-wg1-forward.conf` (the
forwarding-persistence drop-in from §10) had never been renamed —
harmless functionally (`net.ipv4.ip_forward=1` isn't interface-specific),
but a stale filename referencing a tunnel that no longer existed. Renamed
to `99-Bpl-Home-forward.conf`; `docs/RUNBOOK.md` §10a's rename procedure
now calls this out explicitly so it isn't missed again.

A second, more consequential leftover from that same rename surfaced
later while setting up the "add a client peer" workflow: the app's own
peer-tracking metadata (`/etc/pi-config-ui/wireguard/peers.json`) had no
entry for `Bpl-Home` at all — the tunnel had been created/renamed directly
over SSH rather than through the app's "Create tunnel" form, which is the
only code path that ever writes a `peers.json` entry. The tunnel itself
was active and completely healthy; only `app/wireguard.py`'s `create_peer`
and `list_peers` endpoints hard-404'd with "Unknown tunnel", since (unlike
`list_tunnels`/`set_tunnel_state`, which were explicitly written to
tolerate a metadata-less tunnel) they assume an entry always exists. Fixed
by hand-writing a matching `peers.json` entry (address, listen port, empty
peer list) with the correct ownership — a one-time reconciliation, not a
code change, since the gap is inherent to any tunnel that's ever created
or renamed outside the UI. `docs/RUNBOOK.md` §10a's rename procedure now
calls this out as a required check.

### Routing a LAN device's traffic through `Syd-Home` (IP camera → NAS, 2026-08-27)

Requirement: an Annke IP camera on the LAN (static IP, and — checked
directly on its own config screen — its Gateway/DNS fields are also
settable, not fixed) needs its own traffic reaching a NAS on the far side
of `Syd-Home`, one-directionally (camera → NAS only; nothing needs to dial
back into the camera). Because the camera's Gateway field is genuinely
configurable, this could be solved as real gateway-based routing (Pi as
the camera's default gateway) rather than the narrower reverse-DNAT
workaround that would have been the only option with an IP-only-config
device.

Design choices made, and why:
- **`MASQUERADE` on the way out, not a bare route-through**: the remote
  side (pfSense/the NAS) only has a route for this Pi's own tunnel address
  (`10.6.0.6/32`, from the peer-separation work above), not the camera's
  real LAN IP. Routing the camera's real source IP through unchanged would
  need pfSense-side route/`AllowedIPs` changes for the NAS to ever route a
  reply back — `MASQUERADE` avoids touching the remote end at all, at the
  cost of the NAS seeing traffic as coming from `10.6.0.6`, not the
  camera's real IP. Fine for a one-way relationship; would need revisiting
  if the NAS ever needs to allowlist or log by the camera's real address.
- **Scoped to the camera's specific IP, allowed the full existing
  `AllowedIPs` range (`192.168.2.0/24`, `192.168.3.0/24`) as destination**
  (the device owner's choice — the alternative considered was scoping to
  the NAS's exact IP for tighter isolation, but the owner preferred
  simplicity here since both subnets are already trusted via the existing
  tunnel).
- **One-way only, no reverse DNAT added**: the device owner confirmed
  nothing needs to dial back into the camera. §17a in the runbook covers
  adding that later if it's ever needed (e.g. an NVR pulling RTSP
  on-demand instead of the camera pushing).

The generic, camera-agnostic version of this procedure (and the critical
same-subnet caveat — this Pi is expected to relocate between networks with
different DHCP ranges even when the Wi-Fi SSID/password stay identical,
so the camera's IP/Gateway need re-checking at each new location) is in
[`docs/RUNBOOK.md`](RUNBOOK.md) §17/§17a.

### Resource estimates (observed, not guessed)

See runbook §14 for the actual numbers — summary: everything running
(base OS, the app, the helper daemon, both WireGuard interfaces) uses
~160-210MB of 512MB RAM, comfortable headroom. The real constraint on this
hardware is the single ARMv6 core (no hardware crypto acceleration),
capping WireGuard throughput at tens of Mbps — fine for home-lab scale
(2-5 concurrent users, low sustained traffic), not for saturating a home
internet connection.

## Part 3 — WireGuard client tunnel (`Syd-Home`) incident history

Full narrative behind the "AllowedIPs overlap" rule in runbook §9.4, kept
here because the specific keys/addresses/timeline are only useful as
history, not as steps to repeat.

### Device

Raspberry Pi Zero W Rev 1.1, Raspbian 13 (Trixie), hostname `pi0w-1`,
`192.168.1.19`. Wi-Fi (BCM43438) has been observed to be intermittently
flaky — SSH drops and reconnects on its own even under normal conditions.

### Network topology discovered during investigation

- The actual WireGuard **server** is `192.168.1.101` (`vpn.keekar.au` via
  internal split-horizon DNS on `192.168.1.200`/`192.168.1.1`) — the Pi is
  only ever a client of it, never runs its own server on this identity.
- `keekar.ddns.net` no longer resolves at all (a domain migration in
  progress); bare `keekar.au` also doesn't resolve — only specific
  subdomains have records.
- The Pi was slated to be shipped to a remote physical location with the
  same Wi-Fi SSID/password as home, so its existing NetworkManager profile
  needed no changes for the move.

### Incident 1: duplicate identity with the Mac client

The Pi's originally-installed config had the **exact same `PrivateKey`
and tunnel address (`10.6.0.3/24`)** as the WireGuard profile already
imported on the user's Mac. Both devices presented as the identical peer,
so only one could hold an active session at a time — the dominant cause of
"not working as expected," independent of any DNS/domain issue. Fixed by
generating a brand-new keypair for the Pi only (Mac's identity left
untouched as the original/legitimate owner) and a distinct tunnel address
(`10.6.0.4/32`). The new public key
(`g0tH2wApD2US7ww7YGTHhblfaUdBWgdLsmtgXeaYqSs=`) had to be separately
registered as a peer on the server — no SSH access to `.101` was available
in the session that made this fix, so that registration was a manual
follow-up by whoever administers that box.

### Incident 2: self-inflicted SSH lockout

After the identity fix, `wg-quick@Syd-Home` was enabled **while the Pi was
still physically on `192.168.1.0/24`**, with `AllowedIPs` including
`192.168.1.0/24` — the exact subnet the SSH session was using. Bringing
the tunnel up added a competing route for that subnet via the new
interface, breaking normal LAN routing immediately and non-transiently (5
retries over 30+ seconds all timed out). Because the unit was `enable`d
(not just `start`ed), this was set to reproduce on every future boot until
fixed. Root cause and general prevention rule are in runbook §9.4.

### Resolution (final safe config)

`AllowedIPs` narrowed to `192.168.2.0/24, 192.168.3.0/24` — the local
LAN's own `192.168.1.0/24` deliberately excluded, even though the eventual
remote deployment will need it, specifically so the tunnel can stay
enabled while still on the home LAN without risk. Confirmed via `ip
route` that `192.168.1.0/24` still routes via `wlan0`, not the tunnel, and
via `sudo wg show Syd-Home` that the tunnel is actively handshaking
(non-zero bytes transferred).

### DNS notes specific to this investigation

- `getent hosts vpn.keekar.au` succeeds (→ `192.168.1.101`); bare
  `keekar.au` and `keekar.ddns.net` both fail consistently.
- The originally-exported client config's `DNS =` line (internal DNS
  servers only reachable via the server's own LAN) was deliberately
  dropped from the Pi's copy as unnecessary/risk-reducing.

### Outstanding items not yet resolved as of this writing

1. Confirm whether Raspberry Pi Connect (or another out-of-band recovery
   method) is genuinely installed/running on this device — not
   independently verified.
2. Update the Mac's already-imported tunnel: `Endpoint` should be
   `vpn.keekar.au:51820`, not the non-resolving bare `keekar.au`.
3. Confirm whether a *public* (non-split-horizon) DNS/DDNS record for
   `vpn.keekar.au` exists, pointing at the home's WAN IP — needed for
   any client connecting from outside the home network. Not confirmed
   either way.
4. `cloud-init` was flagged as a possible cause of slow boot on this
   device; not fully investigated (an earlier attempt to disable it failed
   on an interactive-password prompt for `sudo`). Verify current state
   (`test -e /etc/cloud/cloud-init.disabled`) before changing anything.
### Resolution 2 (2026-08-27) — pfSense peer separation confirmed and completed

Item 5 above is now resolved. Checking the live pfSense peer list
(`VPN → WireGuard → Peers`, pfSense 2.8.1-RELEASE, WireGuard package
0.2.9_6) surfaced a correction to the original Incident 1 writeup: the
address `10.6.0.4/32` recorded there as "the Pi's fixed address" was
actually already in use by a real, unrelated device (`Mukesh_iPhone`) —
not the laptop as assumed at the time. The laptop's actual peer is
`MacBook Pro M4`, public key `D2FRRn0rccRncYpAdamxtiBXaP4qxf+yjACY9hu/9VM=`,
address `10.6.0.3/32`. The Pi's own public key
(`g0tH2wApD2US7ww7YGTHhblfaUdBWgdLsmtgXeaYqSs=`, derived from its stored
`PrivateKey` and confirmed via `wg pubkey`) was already distinct from both
— so the identity fix from Incident 1 had held — but it had never been
registered as a pfSense peer, and its address needed to move off the
now-confirmed-conflicting `10.6.0.4/32`.

Fixed: registered as a new pfSense peer (`Pi0w-1 (Bpl-Home)`, tunnel
`tun_wg0`) at address `10.6.0.6/32`; Pi's `/etc/wireguard/Syd-Home.conf`
`Address` updated to match. The handshake then silently failed for several
keepalive cycles (bytes flowing both directions, `wg show` reporting
non-zero transfer, but "Latest Handshake: never" on the pfSense side) —
root cause was a **Preshared Key pfSense generated for this peer that
wasn't present on the Pi's side**; WireGuard requires an exact PSK match
if either side sets one, and fails the handshake silently rather than
erroring. Added the matching `PresharedKey` line to the Pi's `[Peer]`
block and restarted — handshake confirmed within seconds on both ends. The
generic version of this procedure lives in
[`docs/RUNBOOK.md`](RUNBOOK.md) §16.

Flagged but not fixed as part of this pass: `Visheshs_MacBook` (another
existing peer, `10.6.0.5/32`) shows the identical broken-handshake
signature (0 B received, tens of MiB sent, "Latest Handshake: never") —
worth checking whether it has the same missing-PSK problem.
