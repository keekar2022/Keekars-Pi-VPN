# Keekar's Pi VPN

Concept: Mukesh Kesharwani
Contact: mukesh.kesharwani@adobe.com

A self-hosted, dual-mode WireGuard VPN and web-based admin console running
entirely on a **Raspberry Pi Zero W** — one of the weakest realistic ARM
targets (single-core 1GHz ARMv6, 512MB RAM), chosen deliberately to prove
this all runs comfortably on minimal hardware rather than assuming a
beefier Pi.

## What this is

A lightweight FastAPI web UI (`app/`) that turns a Raspberry Pi into a
remotely manageable home VPN gateway:

- **WireGuard, both directions at once**: the Pi can run its own WireGuard
  *server* (accepting inbound peers like phones/laptops) and, separately,
  dial *out* as a WireGuard *client* to another remote server — at the same
  time, on the same device, without the two interfering.
- **SSO-gated administration**: every admin action goes through OpenID
  Connect (Authentik) — no local passwords on the device.
- **Live system monitoring**: CPU/memory/disk, network throughput, top
  processes, and a "last downtime" stat that survives reboots.
- **Automated TLS**: a real Let's Encrypt certificate via DNS-01
  (Cloudflare), auto-renewing, with no manual cert wrangling.
- **Unattended maintenance with a real safety net**: scheduled OS updates,
  log rotation, DDNS, and a weekly reboot — backstopped by a **boot-health
  failsafe** that detects a broken update, attempts a best-effort package
  rollback, and raises an alert (both in the journal and as a banner on
  the dashboard) rather than silently leaving the VPN unreachable.
- **Least-privilege by design**: the web app itself runs unprivileged
  (`NoNewPrivileges`, no root); anything that genuinely needs root
  (writing WireGuard configs, running `wg-quick`, package rollbacks) is
  brokered through small, purpose-built root helpers instead of widening
  the main app's own privileges.

**Start here depending on what you need:**
- Setting up a new Pi from scratch → [`docs/RUNBOOK.md`](docs/RUNBOOK.md)
  — the ordered, gotcha-included checklist.
- Understanding *why* something is built the way it is, or what actually
  went wrong and how it got fixed → [`docs/PROJECT_NOTES.md`](docs/PROJECT_NOTES.md)
  — full incident narratives and design rationale, including a real
  self-inflicted SSH lockout and its recovery.
- Already familiar with the setup and just need a quick command reference
  → keep reading below.

## Project status

Actively developed and running in production on a real Pi Zero W as a
home VPN. **Estimated effort invested to date (as of 2026-08-27): ~100
hours** — spanning initial stack research and SSO integration, the
dual-mode WireGuard design, TLS/DDNS automation, the boot-health failsafe,
dashboard monitoring, and recovering from close to a dozen real incidents
documented in [`docs/PROJECT_NOTES.md`](docs/PROJECT_NOTES.md). This is an
approximate figure based on the scope of documented work, not a tracked
timesheet.

## Install on-device (Pi Zero W)

Use piwheels so C-extension dependencies (psutil, pyroute2) install as
prebuilt `armv6l` wheels instead of compiling from source:

```
python3 -m venv venv
source venv/bin/activate
pip install --index-url https://www.piwheels.org/simple -r requirements.txt
```

## Configuration

Copy `deploy/sso.env.example` to `/etc/pi-config-ui/sso.env`, fill in the
real `SSO_APPLICATION_SLUG`/`SSO_CLIENT_ID`/`SSO_CLIENT_SECRET` (from the
dedicated "Raspberry_pi" OIDC application on sso.keekar.au — provisioned via
`python3 homelab/configure.py raspberry-pi` in the Authentik homelab repo,
restricted to `mkesharw`/`aktaniakk20`) and a generated `SESSION_SECRET`.
`chmod 600` the file; never commit it.

sso.keekar.au is Authentik: OIDC discovery lives at
`/application/o/<SSO_APPLICATION_SLUG>/.well-known/openid-configuration`,
not at the issuer root — `app/auth.py` builds that URL from
`SSO_APPLICATION_SLUG`, which is why it's a separate setting from the
client ID even though the two happen to share the value `raspberry-pi`
today.

TLS: point `deploy/pi-config-ui.service`'s `--ssl-keyfile`/`--ssl-certfile`
at a real cert (a self-signed one is fine for LAN-only use) under
`/etc/pi-config-ui/tls/`. For a real publicly-trusted cert via Let's
Encrypt, see `docs/RUNBOOK.md` §5b.

## Run

```
sudo cp deploy/pi-config-ui.service /etc/systemd/system/
sudo cp deploy/polkit-rules/50-pi-config-ui-networkmanager.rules /etc/polkit-1/rules.d/
sudo useradd --system --no-create-home pi-config-ui
sudo systemctl daemon-reload
sudo systemctl enable --now pi-config-ui
```

Or use the fully automated path — `./deploy/deploy.sh` drives all of this
(and the WireGuard helper, maintenance cron jobs, and boot-health failsafe
below) over SSH from your own machine; see `docs/RUNBOOK.md` for details.

### WireGuard tab

The WireGuard page (app/wireguard.py) lets this Pi run its own small
WireGuard server for its own peers — separate from any client tunnel this
device also runs to a different remote server (see
[`docs/PROJECT_NOTES.md`](docs/PROJECT_NOTES.md) Part 2/3 for that
distinction and its incident history). It needs `wg`/`wg-quick`
(`wireguard-tools` package) plus a second, always-root helper service, since
`pi-config-ui.service` deliberately has no privilege to write
`/etc/wireguard` or run `wg-quick`/`systemctl` itself:

```
sudo groupadd --system pi-wg-helper
sudo mkdir -p /opt/pi-wg-helperd
sudo cp deploy/pi-wg-helperd/helper.py /opt/pi-wg-helperd/
sudo cp deploy/pi-wg-helperd.service /etc/systemd/system/
sudo mkdir -p /etc/pi-config-ui/wireguard
sudo chown pi-config-ui:pi-config-ui /etc/pi-config-ui/wireguard
sudo systemctl daemon-reload
sudo systemctl enable --now pi-wg-helperd
sudo systemctl restart pi-config-ui   # picks up the new ReadWritePaths/SupplementaryGroups
```

### Maintenance and the boot-health failsafe

`deploy/maintenance.sh`, installed by `deploy/deploy.sh` as a cron.d
drop-in, handles health-check-and-restart, TLS renewal monitoring, DDNS,
weekly OS updates, log rotation, and a scheduled weekly reboot. OS updates
allow required new dependency packages (including versioned kernels) but
abort rather than removing installed packages. System-changing maintenance
tasks are serialized so an update cannot overlap a health restart or reboot. A
companion systemd unit, `pi-config-ui-boot-check.service`, runs once per
boot to catch the realistic failure mode of an unattended update: it
retries a service restart, attempts a best-effort package rollback if a
recent update is implicated, and always raises an alert — readable over
SSH and shown as a banner on the dashboard — rather than failing silently.
See `docs/RUNBOOK.md` §5d/§5e for exactly what this does and does not
cover.

## Tests

Dev/CI-only — never install `requirements-dev.txt` on the Pi itself:

```
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

`tests/test_auth.py` guards two bugs that previously shipped silently to
production: a `SameSite=Strict` session cookie that broke every OIDC login
(the cookie never made it back on Authentik's redirect to `/auth/callback`),
and a `Content-Security-Policy` header that silently blocked every page's
inline JavaScript (no console error, no failed request — the scripts just
never ran). See [`docs/PROJECT_NOTES.md`](docs/PROJECT_NOTES.md) for the
full incident writeups.
