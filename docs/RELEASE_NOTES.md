---
title: Keekar's Pi VPN — release notes
Concept: Mukesh Kesharwani
Contact: mukesh.kesharwani@adobe.com
---

# Release notes

## 1.0.3 — 2026-09-29

### Fixed

- **`ERR_TOO_MANY_REDIRECTS` when the sign-in service is unreachable.**
  `/auth/login` must fetch Authentik's OIDC metadata before redirecting;
  when that failed, the catch-all error handler redirected to `/`, which
  (signed out) redirected back to `/auth/login` — forever. `/auth/login`
  now shows a 503 "Sign-in service unreachable" page (`Retry-After: 30`),
  `/auth/logout` still signs you out, and the error handler never
  redirects `/` or `/auth/*`.

### New

- **`SPLIT_DNS`** (`deploy.sh`, e.g. `SPLIT_DNS=keekar.au=192.168.1.200`):
  sends only that domain's lookups to that server via systemd-resolved,
  so `sso.keekar.au` keeps resolving when the resolver falls back to a DNS
  server without the split-horizon records. Per device and opt-in (a
  remote-site device can't reach the home DNS server); saved in
  `device.env`, `SPLIT_DNS=none` removes it.
- **`maintenance.sh health`** checks that the SSO issuer resolves; if not,
  it restarts systemd-resolved once and logs a `WARNING`/`ERROR`.
- `deploy.sh` verification prints `/auth/login`'s status (302 = SSO
  reachable, 503 = not).

## 1.0.2 — 2026-09-29

Adds support for the **Walnut Pi Zero W** (Armbian, arm64) alongside the
Raspberry Pi Zero W, **IPv6** DDNS, and per-device identities so several
devices can share one Cloudflare zone and one pfSense WireGuard server
without overwriting each other.

### New

- **Walnut Pi / Armbian support** in `deploy/deploy.sh`:
  - Board-agnostic package set: `wireguard-tools` instead of the
    `wireguard` metapackage (which pulls a stock Debian kernel on
    Armbian), `bind9-dnsutils` instead of `dnsutils` (gone in trixie),
    `resolvconf` only when `systemd-resolved` isn't running, and
    `polkitd`/`wpasupplicant`/`avahi-daemon` that Armbian minimal omits.
  - Python wheels come from piwheels only on 32-bit ARM (`armhf`/`armel`);
    arm64 boards use PyPI's prebuilt aarch64 wheels.
  - `migrate_to_networkmanager` hands Wi-Fi from netplan/systemd-networkd
    to NetworkManager (which the Network tab and Wi-Fi recovery need). It
    runs detached on the Pi, clears netplan's `NM_UNMANAGED` udev tag and
    the `netplan-wpa-*` supplicant, and rolls itself back if
    NetworkManager isn't online within ~90 s. No-op on Raspberry Pi OS.
  - If the handover changes the Pi's IP, the deploy continues via
    `<hostname>.local` (mDNS), pinned to the original host key.
- **Per-device identity** (`/etc/pi-config-ui/device.env`, written by
  `deploy.sh`): `CERT_CN` (admin UI name), `DDNS_RECORD_NAME` (WireGuard
  endpoint, defaults to `CERT_CN` with `vpn` → `wg`) and
  `ADMIN_RECORD_TARGET` (`lan` or `public`). A first deploy refuses to run
  without `CERT_CN`, so a new device can't claim another device's names.
- **IPv6 DDNS**: `ddns-update` publishes an `AAAA` record for the
  WireGuard endpoint from the device's stable global address (temporary
  and ULA addresses are skipped), and deletes it when the device moves to
  a network without IPv6.
- **`ADMIN_RECORD_TARGET=public`** for devices at a remote site: the admin
  name resolves to the site's public IPv4 instead of its LAN IP.
- **`wg-guard`** (`maintenance.sh wg-guard`): trims any client-tunnel
  `AllowedIPs` entry that overlaps a directly connected subnet — the
  SSH-lockout failure from `docs/PROJECT_NOTES.md` Part 3. Runs right
  after every `wg-quick` start (systemd drop-in) and every 10 minutes from
  cron. Runtime only; the `.conf` is never rewritten.
- **Preflight checks** in `deploy.sh`: SSH key login, passwordless sudo,
  hostname/SSID validation, before anything changes on the device.
- **Cloudflare token from `pass`** (`CF_PASS_ENTRY`, default
  `KeekarACI/Cloudflare`): piped straight to `/root/.cf-dns-token`, never
  on a command line or in output. Account-owned tokens are supported
  (`CF_Account_ID`).
- **Extra Wi-Fi networks** (`WIFI_EXTRA_SSIDS`, e.g. a 5 GHz SSID sharing
  the same password), preferred when in range. The password is read on
  the Pi and never leaves it.

### Fixed

- `provision_tls_cert` treated every self-signed cert as CA-issued (it
  compared the raw `issuer=`/`subject=` output, whose prefixes always
  differ), so a real certificate was never issued. It now compares the
  distinguished names and checks the cert covers `CERT_CN`.
- `ddns-update` published nothing on dual-stack links because
  `ifconfig.me` answered with IPv6; the IPv4 lookup is now forced (`-4`).
- `ddns-update` only updated existing records, so a new device's name was
  answered by other DNS instead; missing records are now created.
- acme.sh's DNS-01 propagation check never passed behind a resolver that
  blocks DNS-over-HTTPS; issuance now waits a fixed `--dnssleep 60`.
- `ACME_EMAIL` is optional; the placeholder account email from older
  deploys (rejected by Let's Encrypt) is removed.
- The weekly pip re-sync failed on arm64 because it always used piwheels.

### Upgrade notes

- `PI_HOST` no longer has a default; set it explicitly.
- Existing devices without `device.env` skip DDNS until redeployed once
  with `CERT_CN` set, e.g.
  `PI_HOST=user@host CERT_CN=vpn.bpl.keekar.au ./deploy/deploy.sh --skip-deps`.
- The hardcoded `wg.bpl.keekar.au` DDNS name is gone from
  `maintenance.sh`; each device publishes only the names in its own
  `device.env`.

## 1.0.1 — 2026-09-03

- Serialized system-changing maintenance tasks (update, health restart,
  reboot) behind a shared lock; OS updates allow required new packages
  such as versioned kernels but abort rather than remove anything.

## 1.0.0 — 2026-08-31

- Dashboard uptime stat and polkit-gated Reboot/Shutdown buttons.
- Physical-console Wi-Fi recovery, consolidated into `maintenance.sh`.
