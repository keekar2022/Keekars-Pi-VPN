#!/usr/bin/env bash
# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com
#
# One-shot deploy: bootstraps a brand-new Pi or safely re-applies to an
# already-deployed one. Run from the pi_project repo root, on your Mac (or
# any dev machine with SSH access) — this drives the Pi entirely over SSH;
# it does not need anything pre-copied onto the Pi to start.
#
# SAFETY (non-negotiable): this script must never enable/start/restart a
# WireGuard CLIENT-role tunnel (e.g. Syd-Home). See docs/PROJECT_NOTES.md
# Part 3 — bringing up a client tunnel whose AllowedIPs overlap the
# current LAN has already caused a full SSH lockout once on this project.
# Only the app, pi-wg-helperd, and server-role tunnel(s) this script's own
# bootstrap created (currently none by default — Bpl-Home is created via
# the WireGuard tab's UI, not by this script) are ever touched.
#
# Usage:
#   ./deploy/deploy.sh                  # full bootstrap + update
#   ./deploy/deploy.sh --skip-deps      # skip apt/pip install (faster iteration)
#   ./deploy/deploy.sh --only <func>    # run a single function by name
#   PI_HOST=user@host ./deploy/deploy.sh
#   CERT_CN=your.hostname ./deploy/deploy.sh   # only used the first time a
#                                                # TLS cert is generated
#   ACME_EMAIL=you@example.com ./deploy/deploy.sh   # required only to
#     # provision a real (Let's Encrypt) cert via provision_tls_cert; also
#     # requires a Cloudflare API token dropped at /root/.cf-dns-token on
#     # the Pi (see docs/RUNBOOK.md §5b — never supplied by this script).
#     # Leave ACME_EMAIL unset to keep the self-signed cert from
#     # bootstrap_system.
#
# Functions run in this order on a full pass (grouped to match the three
# concerns asked for — dependencies, code, permissions/cron — while
# actually respecting the real dependency order: e.g. the pi-config-ui
# user must exist before its venv can be created):
#   bootstrap_system -> provision_tls_cert -> deploy_code ->
#   install_dependencies -> configure_sso -> install_units ->
#   set_permissions -> setup_cron -> restart_services -> verify_deployment
#
# --only runs a single function in isolation and assumes prior functions'
# state already exists (e.g. --only setup_cron needs deploy_code to have
# already staged deploy/maintenance.sh onto the Pi at least once).

set -euo pipefail

PI_HOST="${PI_HOST:-mkesharw@192.168.1.19}"
CERT_CN="${CERT_CN:-vpn.bpl.keekar.au}"
ACME_EMAIL="${ACME_EMAIL:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE_DIR="/tmp/pi-config-ui-deploy-$$"
SKIP_DEPS=0
ONLY=""

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$1"; }

# Runs a script read from stdin as root on the Pi. Use a quoted heredoc
# (<<'REMOTE') when the block needs no local variable substitution, or an
# unquoted heredoc (<<REMOTE) when it does (escape any $ meant to be
# evaluated remotely instead, e.g. \$(hostname)).
remote() {
  ssh "$PI_HOST" 'sudo bash -s'
}

bootstrap_system() {
  log "Bootstrapping system user/groups/directories"
  remote <<'REMOTE'
set -euo pipefail
getent group pi-wg-helper >/dev/null || groupadd --system pi-wg-helper
id -u pi-config-ui >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin pi-config-ui
install -d -o pi-config-ui -g pi-config-ui -m 0755 /opt/pi-config-ui
install -d -m 0755 /etc/pi-config-ui
install -d -o pi-config-ui -g pi-config-ui -m 0700 /etc/pi-config-ui/tls
install -d -o pi-config-ui -g pi-config-ui -m 0755 /etc/pi-config-ui/wireguard
install -d -o pi-config-ui -g pi-config-ui -m 0755 /etc/pi-config-ui/monitor
# Root-owned (not pi-config-ui): only maintenance.sh's boot-check, running
# as root via systemd (see pi-config-ui-boot-check.service), ever writes
# here — apt-get install for a rollback needs root, so this deliberately
# sits outside the sandboxed app's ReadWritePaths grant.
install -d -o root -g root -m 0755 /etc/pi-config-ui/monitor/pkg-backups
REMOTE

  # Cert generation needs $CERT_CN from the local side, so this block uses
  # an unquoted heredoc — \$( ) below is escaped so it runs on the Pi, not
  # on the Mac.
  remote <<REMOTE
set -euo pipefail
if [ ! -f /etc/pi-config-ui/tls/cert.pem ]; then
  echo "No existing TLS cert found — generating a self-signed one for ${CERT_CN}."
  openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
    -keyout /etc/pi-config-ui/tls/key.pem -out /etc/pi-config-ui/tls/cert.pem \
    -subj "/CN=${CERT_CN}" \
    -addext "subjectAltName=DNS:${CERT_CN},IP:\$(hostname -I | awk '{print \$1}')"
  chown pi-config-ui:pi-config-ui /etc/pi-config-ui/tls/key.pem /etc/pi-config-ui/tls/cert.pem
  chmod 600 /etc/pi-config-ui/tls/key.pem
else
  echo "Existing TLS cert found (\$(openssl x509 -in /etc/pi-config-ui/tls/cert.pem -noout -subject)) — leaving it untouched."
fi
REMOTE
}

# Upgrades the self-signed cert from bootstrap_system to a real Let's
# Encrypt one via acme.sh + Cloudflare DNS-01 (needed because keekar.au's
# public A record for $CERT_CN points at this Pi's LAN IP, which rules out
# HTTP-01 — see docs/RUNBOOK.md §5b). No-ops safely if the prerequisites
# (the Cloudflare token, ACME_EMAIL) aren't there yet, or if a real cert
# is already installed — this script never fabricates or stores the
# Cloudflare token itself, same convention as configure_sso/sso.env.
provision_tls_cert() {
  log "Provisioning trusted TLS certificate (Let's Encrypt via Cloudflare DNS-01)"

  if ! remote <<'REMOTE'
test -f /root/.cf-dns-token
REMOTE
  then
    warn "No /root/.cf-dns-token on the Pi — leaving the self-signed cert in place."
    warn "See docs/RUNBOOK.md §5b to provision a real cert (Cloudflare API token scoped to this zone's DNS, dropped at /root/.cf-dns-token as CF_Token=<token>, mode 600)."
    return 0
  fi

  if remote <<'REMOTE'
set -euo pipefail
test -f /etc/pi-config-ui/tls/cert.pem
issuer=$(openssl x509 -in /etc/pi-config-ui/tls/cert.pem -noout -issuer)
subject=$(openssl x509 -in /etc/pi-config-ui/tls/cert.pem -noout -subject)
[ "$issuer" != "$subject" ]
REMOTE
  then
    echo "Existing cert is already CA-issued — leaving it untouched."
    return 0
  fi

  if [ -z "$ACME_EMAIL" ]; then
    warn "ACME_EMAIL not set — skipping cert issuance (self-signed cert stays in place)."
    warn "Re-run: ACME_EMAIL=you@example.com ./deploy/deploy.sh --only provision_tls_cert"
    return 0
  fi

  remote <<'REMOTE'
set -euo pipefail
if [ ! -d /root/.acme.sh ]; then
  curl -s https://get.acme.sh | sh -s email=acme-bootstrap@invalid
fi
REMOTE

  # Needs $ACME_EMAIL/$CERT_CN from the local side, so this block uses an
  # unquoted heredoc (no $ needing remote-side escaping here — the whole
  # block already runs as one root bash script per the remote() helper, so
  # sourcing the token file at the top just exports CF_Token for every
  # command below it, no nested sh -c needed).
  remote <<REMOTE
set -euo pipefail
set -a
. /root/.cf-dns-token
set +a
/root/.acme.sh/acme.sh --register-account -m "${ACME_EMAIL}" --server letsencrypt
/root/.acme.sh/acme.sh --set-default-ca --server letsencrypt
/root/.acme.sh/acme.sh --issue --dns dns_cf -d ${CERT_CN} --server letsencrypt
/root/.acme.sh/acme.sh --install-cert -d ${CERT_CN} --ecc --key-file /etc/pi-config-ui/tls/key.pem --fullchain-file /etc/pi-config-ui/tls/cert.pem --reloadcmd "chown pi-config-ui:pi-config-ui /etc/pi-config-ui/tls/key.pem /etc/pi-config-ui/tls/cert.pem && chmod 600 /etc/pi-config-ui/tls/key.pem && chmod 644 /etc/pi-config-ui/tls/cert.pem && systemctl restart pi-config-ui"
REMOTE
}

deploy_code() {
  log "Deploying application code"
  ssh "$PI_HOST" "mkdir -p '$STAGE_DIR/app' '$STAGE_DIR/deploy'"
  rsync -az --delete --exclude '__pycache__' --exclude '*.pyc' \
    "$REPO_ROOT/app/" "$PI_HOST:$STAGE_DIR/app/"
  rsync -az --exclude 'sso.env' \
    "$REPO_ROOT/deploy/" "$PI_HOST:$STAGE_DIR/deploy/"
  scp -q "$REPO_ROOT/requirements.txt" "$PI_HOST:$STAGE_DIR/requirements.txt"

  remote <<REMOTE
set -euo pipefail
rsync -a --delete "$STAGE_DIR/app/" /opt/pi-config-ui/app/
cp "$STAGE_DIR/requirements.txt" /opt/pi-config-ui/requirements.txt
mkdir -p /opt/pi-config-ui/deploy
rsync -a "$STAGE_DIR/deploy/" /opt/pi-config-ui/deploy/
chown -R pi-config-ui:pi-config-ui /opt/pi-config-ui/app /opt/pi-config-ui/requirements.txt /opt/pi-config-ui/deploy
rm -rf "$STAGE_DIR"
REMOTE
}

install_dependencies() {
  if [ "$SKIP_DEPS" = "1" ]; then
    warn "Skipping dependency install (--skip-deps)"
    return
  fi
  log "Installing/updating dependencies"
  remote <<'REMOTE'
set -euo pipefail
# ForceIPv4: this Pi's path to some hosts (e.g. Cloudflare's IPv6
# addresses, hit while debugging DNS earlier in this project) has been
# observed as unreachable over IPv6 — force apt to stick to IPv4 so a
# transient IPv6 routing issue can't stall/fail package operations.
APT_OPTS=(-o Acquire::ForceIPv4=true)
apt-get "${APT_OPTS[@]}" update -qq
# python3-venv/iptables/wireguard*: needed by the app and WireGuard tab.
# python3: python3-venv pulls this in transitively on Debian, but ensured
# explicitly here too — maintenance.sh's cmd_ddns_update shells out to the
# system python3 directly (not the app's venv) to parse the Cloudflare
# API's JSON responses.
# resolvconf: required by NetworkManager's DNS integration (see the
# resolv.conf-symlink gotcha in docs/RUNBOOK.md §1) — usually preinstalled
# on Raspberry Pi OS but ensured explicitly here rather than assumed.
# network-manager: ditto, ensured rather than assumed for a truly minimal
# base image.
# dnsutils: dig/nslookup, for diagnosing DNS issues on-device (uvicorn
# itself is a Python/pip dependency, installed via requirements.txt below,
# not an apt package).
# rsync: deploy_code (this script) rsyncs into place ON the Pi, not just
# from the Mac — without it here, deploy_code fails on a bare image.
# curl: used by this script's own verify_deployment and by
# maintenance.sh's health/cert-renew and ddns-update checks (the latter
# also calls the Cloudflare API and an external IP-echo service over
# HTTPS — see docs/RUNBOOK.md §5c).
# cron: maintenance.sh is installed as a cron.d job (setup_cron) — without
# the cron daemon present, that file just sits there unread.
# openssl: used by bootstrap_system to generate the self-signed TLS cert.
# iproute2: app/routing.py (pyroute2) and this project's own scripts all
# assume `ip`/`ss` exist — virtually always preinstalled, but a full
# bootstrap shouldn't silently assume it.
apt-get "${APT_OPTS[@]}" install -y --no-install-recommends \
  python3 python3-venv wireguard wireguard-tools iptables \
  resolvconf network-manager dnsutils \
  rsync curl cron openssl iproute2
if [ ! -d /opt/pi-config-ui/venv ]; then
  sudo -u pi-config-ui python3 -m venv /opt/pi-config-ui/venv
fi
sudo -u pi-config-ui /opt/pi-config-ui/venv/bin/pip install -q \
  --index-url https://www.piwheels.org/simple \
  -r /opt/pi-config-ui/requirements.txt
REMOTE
}

configure_sso() {
  log "Checking SSO configuration"
  if remote <<'REMOTE'
test -f /etc/pi-config-ui/sso.env
REMOTE
  then
    echo "sso.env already present — leaving it untouched."
  else
    warn "No /etc/pi-config-ui/sso.env found — installing the placeholder template."
    scp -q "$REPO_ROOT/deploy/sso.env.example" "$PI_HOST:/tmp/sso.env.staged-$$"
    remote <<REMOTE
set -euo pipefail
mv "/tmp/sso.env.staged-$$" /etc/pi-config-ui/sso.env
chown pi-config-ui:pi-config-ui /etc/pi-config-ui/sso.env
chmod 600 /etc/pi-config-ui/sso.env
REMOTE
    warn "Fill in real values in /etc/pi-config-ui/sso.env on the Pi (SSH in directly — this script never sees or fabricates secrets), then re-run this script."
    exit 1
  fi
}

install_units() {
  log "Installing systemd units and polkit rule"
  remote <<'REMOTE'
set -euo pipefail
cp /opt/pi-config-ui/deploy/pi-config-ui.service /etc/systemd/system/pi-config-ui.service
cp /opt/pi-config-ui/deploy/pi-wg-helperd.service /etc/systemd/system/pi-wg-helperd.service
cp /opt/pi-config-ui/deploy/pi-config-ui-boot-check.service /etc/systemd/system/pi-config-ui-boot-check.service
mkdir -p /opt/pi-wg-helperd
cp /opt/pi-config-ui/deploy/pi-wg-helperd/helper.py /opt/pi-wg-helperd/helper.py
chown root:root /opt/pi-wg-helperd/helper.py
chmod 755 /opt/pi-wg-helperd/helper.py
cp /opt/pi-config-ui/deploy/polkit-rules/50-pi-config-ui-networkmanager.rules /etc/polkit-1/rules.d/
systemctl daemon-reload
systemctl enable pi-config-ui pi-wg-helperd pi-config-ui-boot-check
REMOTE
}

set_permissions() {
  log "Setting file/directory permissions"
  remote <<'REMOTE'
set -euo pipefail
chown -R pi-config-ui:pi-config-ui /opt/pi-config-ui
chown -R pi-config-ui:pi-config-ui /etc/pi-config-ui/tls /etc/pi-config-ui/wireguard /etc/pi-config-ui/monitor
if [ -f /etc/pi-config-ui/sso.env ]; then
  chown pi-config-ui:pi-config-ui /etc/pi-config-ui/sso.env
  chmod 600 /etc/pi-config-ui/sso.env
fi
if [ -f /etc/pi-config-ui/tls/key.pem ]; then
  chmod 600 /etc/pi-config-ui/tls/key.pem
fi
if [ -f /opt/pi-wg-helperd/helper.py ]; then
  chown root:root /opt/pi-wg-helperd/helper.py
  chmod 755 /opt/pi-wg-helperd/helper.py
fi
REMOTE
}

setup_cron() {
  log "Installing maintenance script and cron schedule"
  remote <<'REMOTE'
set -euo pipefail
cp /opt/pi-config-ui/deploy/maintenance.sh /opt/pi-config-ui/maintenance.sh
chown root:root /opt/pi-config-ui/maintenance.sh
chmod 755 /opt/pi-config-ui/maintenance.sh

cat > /etc/cron.d/pi-config-ui-maintenance <<'CRON'
# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com
# Managed by deploy/deploy.sh — edits here are overwritten on next deploy.
*/10 * * * * root /opt/pi-config-ui/maintenance.sh health >> /var/log/pi-config-ui-maintenance.log 2>&1
*/10 * * * * root /opt/pi-config-ui/maintenance.sh ddns-update >> /var/log/pi-config-ui-maintenance.log 2>&1
0 3 * * * root /opt/pi-config-ui/maintenance.sh cert-renew >> /var/log/pi-config-ui-maintenance.log 2>&1
0 4 * * 0 root /opt/pi-config-ui/maintenance.sh cleanup >> /var/log/pi-config-ui-maintenance.log 2>&1
0 5 * * 3 root /opt/pi-config-ui/maintenance.sh os-update >> /var/log/pi-config-ui-maintenance.log 2>&1
30 5 * * 3 root /opt/pi-config-ui/maintenance.sh reboot >> /var/log/pi-config-ui-maintenance.log 2>&1
CRON
chmod 644 /etc/cron.d/pi-config-ui-maintenance
touch /var/log/pi-config-ui-maintenance.log

# Never rotated otherwise: five cron jobs above (two every 10 minutes)
# append to this file forever. logrotate itself is already installed and
# run daily by the OS (Raspbian default) — this just gives it a target.
cat > /etc/logrotate.d/pi-config-ui-maintenance <<'LOGROTATE'
/var/log/pi-config-ui-maintenance.log {
  weekly
  rotate 4
  compress
  missingok
  notifempty
  create 644 root root
}
LOGROTATE
REMOTE
}

restart_services() {
  log "Restarting services (app + WireGuard helper only — never client tunnels)"
  remote <<'REMOTE'
set -euo pipefail
systemctl restart pi-wg-helperd
sleep 1
systemctl restart pi-config-ui

# uvicorn's own import/startup on this CPU has consistently taken ~20s in
# practice (single ARMv6 core, no JIT) — poll instead of guessing a fixed
# sleep, up to a generous ceiling. The initial settle delay matters: right
# after `restart`, the OLD process can still be mid-graceful-shutdown and
# briefly answer requests on the same port, which would otherwise read as
# a false-positive "already up" on the very first check.
sleep 5
PORT=$(ss -tlnp 2>/dev/null | grep uvicorn | grep -oE ':[0-9]+' | head -1 | tr -d ':') || true
PORT="${PORT:-443}"
for i in $(seq 1 30); do
  # curl's -w "%{http_code}" already prints "000" on connection failure by
  # itself (even though curl's own exit code is nonzero) — do not also
  # `|| echo "000"` here, or a failure prints "000000" and the "!= 000"
  # check below false-positives as success. Normalize a totally-empty
  # result (curl couldn't even write that much) to "000" too.
  CODE=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 3 "https://localhost:${PORT}/" 2>/dev/null || true)
  CODE="${CODE:-000}"
  if [ "$CODE" != "000" ]; then
    echo "App responding after ~${i}x2s."
    break
  fi
  sleep 2
done
REMOTE
}

verify_deployment() {
  log "Verifying deployment"
  remote <<'REMOTE'
set -euo pipefail
echo "-- service states --"
systemctl is-active pi-config-ui pi-wg-helperd || true
PORT=$(ss -tlnp 2>/dev/null | grep uvicorn | grep -oE ':[0-9]+' | head -1 | tr -d ':') || true
PORT="${PORT:-443}"
echo "-- root endpoint (expect 303) --"
curl -sk -o /dev/null -w "%{http_code}\n" "https://localhost:${PORT}/"
echo "-- login cookie attributes (expect samesite=lax, secure, httponly) --"
curl -sk -D - -o /dev/null "https://localhost:${PORT}/auth/login" | grep -i set-cookie || true
echo "-- Syd-Home client tunnel state (must be UNCHANGED by this script) --"
systemctl is-enabled wg-quick@Syd-Home 2>/dev/null || echo "not present"
systemctl is-active wg-quick@Syd-Home 2>/dev/null || echo "not active"
echo "-- TLS cert in use --"
openssl x509 -in /etc/pi-config-ui/tls/cert.pem -noout -issuer -enddate
echo "-- Cloudflare DDNS token present? (needed by maintenance.sh ddns-update, see docs/RUNBOOK.md §5c) --"
test -f /root/.cf-dns-token && echo "yes" || echo "no — ddns-update will no-op every run until this is provisioned"
REMOTE
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --skip-deps) SKIP_DEPS=1 ;;
      --only) ONLY="${2:?--only requires a function name}"; shift ;;
      -h|--help) grep '^#' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
      *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
  done

  if [ -n "$ONLY" ]; then
    "$ONLY"
  else
    bootstrap_system
    provision_tls_cert
    deploy_code
    install_dependencies
    configure_sso
    install_units
    set_permissions
    setup_cron
    restart_services
    verify_deployment
  fi

  log "Done."
}

main "$@"
