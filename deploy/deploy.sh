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
#   PI_HOST=user@host ./deploy/deploy.sh      # required, e.g. root@192.168.1.24
#
# First deploy of a NEW device (each device needs its own names, or it
# overwrites another device's DNS records):
#   PI_HOST=root@<ip> CERT_CN=vpn2.bpl.keekar.au \
#     WIFI_EXTRA_SSIDS="keekar5G" ./deploy/deploy.sh
#
#   CERT_CN           admin UI hostname; saved on the Pi in
#                     /etc/pi-config-ui/device.env, so later runs omit it.
#   DDNS_RECORD_NAME  WireGuard public endpoint; defaults to CERT_CN with
#                     vpn -> wg (vpn2.bpl.keekar.au -> wg2.bpl.keekar.au).
#   ADMIN_RECORD_TARGET  lan (default) or public: which IP CERT_CN resolves
#                     to. Use public for a device at a remote site whose LAN
#                     isn't reachable (e.g. Bhopal); saved in device.env.
#   CF_PASS_ENTRY     `pass` entry holding API_TOKEN=<Cloudflare token with
#                     Zone:DNS:Edit on keekar.au> and Account_ID=<id>;
#                     default KeekarACI/Cloudflare, set empty to skip.
#                     Copied to /root/.cf-dns-token (mode 600); enables
#                     the Let's Encrypt cert and DNS registration.
#   ACME_EMAIL        optional Let's Encrypt account email.
#   WIFI_EXTRA_SSIDS  space-separated SSIDs sharing the current Wi-Fi's
#                     password (e.g. a 5 GHz band); preferred when in range.
#
# Functions run in this order on a full pass (grouped to match the three
# concerns asked for — dependencies, code, permissions/cron — while
# actually respecting the real dependency order: e.g. the pi-config-ui
# user must exist before its venv can be created):
#   preflight -> bootstrap_system -> provision_tls_cert -> deploy_code ->
#   install_dependencies -> migrate_to_networkmanager -> configure_wifi ->
#   configure_sso -> install_units -> install_wifi_recovery ->
#   set_permissions -> setup_cron -> register_dns -> restart_services ->
#   verify_deployment
#
# --only runs a single function in isolation and assumes prior functions'
# state already exists (e.g. --only setup_cron needs deploy_code to have
# already staged deploy/maintenance.sh onto the Pi at least once).
#
# Supported targets: any Pi Zero-class board on a Debian-based OS, e.g.
# Raspberry Pi OS (armhf/arm64) or Armbian (Walnut Pi Zero W, arm64).
# PI_HOST's user needs passwordless sudo; on Armbian use root@<ip> with
# an SSH key, since its first user is created without NOPASSWD sudo.

set -euo pipefail

PI_HOST="${PI_HOST:-}"
CERT_CN="${CERT_CN:-}"
DDNS_RECORD_NAME="${DDNS_RECORD_NAME:-}"
ADMIN_RECORD_TARGET="${ADMIN_RECORD_TARGET:-}"
ACME_EMAIL="${ACME_EMAIL:-}"
CF_PASS_ENTRY="${CF_PASS_ENTRY-KeekarACI/Cloudflare}"
WIFI_EXTRA_SSIDS="${WIFI_EXTRA_SSIDS:-}"
# Pins host-key checks to the original address, so the run can continue
# via <hostname>.local if the Wi-Fi handover changes the Pi's IP.
HOST_KEY_ALIAS=""
PI_HOSTNAME=""
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
pi_ssh() {
  ssh -o "HostKeyAlias=$HOST_KEY_ALIAS" "$@"
}

remote() {
  pi_ssh "$PI_HOST" 'sudo bash -s'
}

# Like remote(), but first defines the named local variables (shell-quoted)
# in the remote script, so a quoted heredoc can use them safely.
remote_with() {
  local v
  { for v in "$@"; do printf '%s=%q\n' "$v" "${!v}"; done; cat; } | remote
}

# Prints the pass entry in acme.sh dns_cf / maintenance.sh format. The
# token is account-owned, so acme.sh also needs CF_Account_ID.
cf_token_env() {
  pass show "$CF_PASS_ENTRY" 2>/dev/null \
    | sed -n 's/^API_TOKEN=/CF_Token=/p; s/^Account_ID=/CF_Account_ID=/p'
}

preflight() {
  log "Preflight checks"
  local name_re='^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$' ssid_re='^[A-Za-z0-9_.-]+$' ssid
  if [ -z "$PI_HOST" ]; then
    warn "Set PI_HOST, e.g. PI_HOST=root@192.168.1.24 $0"
    exit 1
  fi
  HOST_KEY_ALIAS="${PI_HOST#*@}"
  if ! pi_ssh -o BatchMode=yes -o ConnectTimeout=10 "$PI_HOST" true; then
    warn "Can't SSH to $PI_HOST with a key. Connect once with 'ssh $PI_HOST' (host key) and run 'ssh-copy-id $PI_HOST'."
    exit 1
  fi
  if ! pi_ssh -o BatchMode=yes "$PI_HOST" 'sudo -n true' 2>/dev/null; then
    warn "$PI_HOST has no passwordless sudo. On Armbian use PI_HOST=root@<ip>."
    exit 1
  fi

  local saved saved_cn saved_ddns saved_admin
  saved=$(pi_ssh "$PI_HOST" 'cat /etc/pi-config-ui/device.env 2>/dev/null' || true)
  saved_cn=$(printf '%s\n' "$saved" | sed -n 's/^CERT_CN=//p')
  saved_ddns=$(printf '%s\n' "$saved" | sed -n 's/^DDNS_RECORD_NAME=//p')
  saved_admin=$(printf '%s\n' "$saved" | sed -n 's/^ADMIN_RECORD_TARGET=//p')
  ADMIN_RECORD_TARGET="${ADMIN_RECORD_TARGET:-${saved_admin:-lan}}"
  case "$ADMIN_RECORD_TARGET" in
    lan|public) ;;
    *) warn "ADMIN_RECORD_TARGET must be lan or public."; exit 1 ;;
  esac
  CERT_CN="${CERT_CN:-$saved_cn}"
  if [ -z "$CERT_CN" ]; then
    warn "First deploy to this device: give it its own hostname, e.g. CERT_CN=vpn2.bpl.keekar.au (never reuse another device's)."
    exit 1
  fi
  if [ -z "$DDNS_RECORD_NAME" ]; then
    if [ -n "$saved_ddns" ] && [ "$CERT_CN" = "$saved_cn" ]; then
      DDNS_RECORD_NAME="$saved_ddns"
    else
      case "$CERT_CN" in
        vpn*) DDNS_RECORD_NAME="wg${CERT_CN#vpn}" ;;
        *) warn "Set DDNS_RECORD_NAME (WireGuard public hostname) for $CERT_CN."; exit 1 ;;
      esac
    fi
  fi
  if ! [[ "$CERT_CN" =~ $name_re && "$DDNS_RECORD_NAME" =~ $name_re ]]; then
    warn "CERT_CN/DDNS_RECORD_NAME must be plain lowercase hostnames."
    exit 1
  fi
  for ssid in $WIFI_EXTRA_SSIDS; do
    [[ "$ssid" =~ $ssid_re ]] || { warn "Unsupported SSID '$ssid' (letters, digits, _ . - only)."; exit 1; }
  done
  if [ -n "$CF_PASS_ENTRY" ]; then
    if ! command -v pass >/dev/null; then
      warn "pass not installed — no Cloudflare token; the cert stays self-signed (set CF_PASS_ENTRY= to silence)."
      CF_PASS_ENTRY=""
    elif ! cf_token_env | grep -q '^CF_Token=.'; then
      warn "pass entry '$CF_PASS_ENTRY' has no API_TOKEN= line (or pass is locked)."
      exit 1
    fi
  fi
  PI_HOSTNAME=$(pi_ssh "$PI_HOST" hostname)
  echo "Device $PI_HOSTNAME: admin UI $CERT_CN (-> $ADMIN_RECORD_TARGET IP), WireGuard endpoint $DDNS_RECORD_NAME"
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

  remote_with CERT_CN DDNS_RECORD_NAME ADMIN_RECORD_TARGET <<'REMOTE'
set -euo pipefail
# Read by maintenance.sh ddns-update and by later deploys (see preflight).
printf 'CERT_CN=%s\nDDNS_RECORD_NAME=%s\nADMIN_RECORD_TARGET=%s\n' "$CERT_CN" "$DDNS_RECORD_NAME" "$ADMIN_RECORD_TARGET" > /etc/pi-config-ui/device.env
chmod 644 /etc/pi-config-ui/device.env
CERT=/etc/pi-config-ui/tls/cert.pem
self_signed() {
  [ "$(openssl x509 -in "$CERT" -noout -issuer -nameopt RFC2253 | cut -d= -f2-)" = \
    "$(openssl x509 -in "$CERT" -noout -subject -nameopt RFC2253 | cut -d= -f2-)" ]
}
if [ ! -f "$CERT" ] || { self_signed && ! openssl x509 -in "$CERT" -noout -checkhost "$CERT_CN" | grep -q 'does match'; }; then
  echo "Generating a self-signed cert for $CERT_CN."
  openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes -quiet \
    -keyout /etc/pi-config-ui/tls/key.pem -out "$CERT" \
    -subj "/CN=$CERT_CN" \
    -addext "subjectAltName=DNS:$CERT_CN,IP:$(hostname -I | awk '{print $1}')"
  chown pi-config-ui:pi-config-ui /etc/pi-config-ui/tls/key.pem "$CERT"
  chmod 600 /etc/pi-config-ui/tls/key.pem
else
  echo "Existing TLS cert found ($(openssl x509 -in "$CERT" -noout -subject)) — leaving it untouched."
fi
REMOTE
}

# Upgrades the self-signed cert from bootstrap_system to a real Let's
# Encrypt one via acme.sh + Cloudflare DNS-01 (needed because keekar.au's
# public A record for $CERT_CN points at this Pi's LAN IP, which rules out
# HTTP-01 — see docs/RUNBOOK.md §5b). No-ops safely if the Cloudflare
# token isn't available, or if a real cert for CERT_CN is already
# installed. The token comes from `pass` (CF_PASS_ENTRY) and is piped
# straight to the Pi — never on a command line, in output, or on disk here.
provision_tls_cert() {
  log "Provisioning trusted TLS certificate (Let's Encrypt via Cloudflare DNS-01)"

  if [ -n "$CF_PASS_ENTRY" ]; then
    cf_token_env | pi_ssh "$PI_HOST" "sudo sh -c 'umask 077; cat > /root/.cf-dns-token'"
    echo "Cloudflare token from pass '$CF_PASS_ENTRY' installed at /root/.cf-dns-token (mode 600)."
  fi

  if ! remote <<'REMOTE'
test -f /root/.cf-dns-token
REMOTE
  then
    warn "No /root/.cf-dns-token on the Pi — leaving the self-signed cert in place."
    warn "See docs/RUNBOOK.md §5b to provision a real cert (Cloudflare API token scoped to this zone's DNS, dropped at /root/.cf-dns-token as CF_Token=<token>, mode 600)."
    return 0
  fi

  if remote_with CERT_CN <<'REMOTE'
set -euo pipefail
CERT=/etc/pi-config-ui/tls/cert.pem
test -f "$CERT"
# -nameopt + cut compare the DNs themselves; the raw "issuer="/"subject=" prefixes always differ.
[ "$(openssl x509 -in "$CERT" -noout -issuer -nameopt RFC2253 | cut -d= -f2-)" != \
  "$(openssl x509 -in "$CERT" -noout -subject -nameopt RFC2253 | cut -d= -f2-)" ]
openssl x509 -in "$CERT" -noout -checkhost "$CERT_CN" | grep -q 'does match'
REMOTE
  then
    echo "Existing cert is already CA-issued for $CERT_CN — leaving it untouched."
    return 0
  fi

  remote_with ACME_EMAIL <<'REMOTE'
set -euo pipefail
if [ ! -d /root/.acme.sh ]; then
  curl -s https://get.acme.sh | sh -s ${ACME_EMAIL:+email="$ACME_EMAIL"}
fi
# Older deploys installed with a placeholder address Let's Encrypt rejects.
sed -i "/^ACCOUNT_EMAIL='\?acme-bootstrap@invalid/d" /root/.acme.sh/account.conf 2>/dev/null || true
REMOTE

  remote_with ACME_EMAIL CERT_CN <<'REMOTE'
set -euo pipefail
set -a
. /root/.cf-dns-token
set +a
/root/.acme.sh/acme.sh --register-account ${ACME_EMAIL:+-m "$ACME_EMAIL"} --server letsencrypt
/root/.acme.sh/acme.sh --set-default-ca --server letsencrypt
# Exit code 2 means "already issued and not due for renewal".
# --dnssleep: the LAN resolver blocks DoH (cloudflare-dns.com -> 0.0.0.0),
# so acme.sh's own propagation check never succeeds; wait a fixed time instead.
rc=0
/root/.acme.sh/acme.sh --issue --dns dns_cf --dnssleep 60 -d "$CERT_CN" --server letsencrypt || rc=$?
[ "$rc" -eq 0 ] || [ "$rc" -eq 2 ]
# try-restart: on a first deploy the unit isn't installed yet.
/root/.acme.sh/acme.sh --install-cert -d "$CERT_CN" --ecc --key-file /etc/pi-config-ui/tls/key.pem --fullchain-file /etc/pi-config-ui/tls/cert.pem --reloadcmd "chown pi-config-ui:pi-config-ui /etc/pi-config-ui/tls/key.pem /etc/pi-config-ui/tls/cert.pem && chmod 600 /etc/pi-config-ui/tls/key.pem && chmod 644 /etc/pi-config-ui/tls/cert.pem && (systemctl try-restart pi-config-ui || true)"
REMOTE
}

deploy_code() {
  log "Deploying application code"
  pi_ssh "$PI_HOST" "mkdir -p '$STAGE_DIR/app' '$STAGE_DIR/deploy'"
  rsync -az -e "ssh -o HostKeyAlias=$HOST_KEY_ALIAS" --delete --exclude '__pycache__' --exclude '*.pyc' \
    "$REPO_ROOT/app/" "$PI_HOST:$STAGE_DIR/app/"
  rsync -az -e "ssh -o HostKeyAlias=$HOST_KEY_ALIAS" --exclude 'sso.env' \
    "$REPO_ROOT/deploy/" "$PI_HOST:$STAGE_DIR/deploy/"
  scp -q -o "HostKeyAlias=$HOST_KEY_ALIAS" "$REPO_ROOT/requirements.txt" "$PI_HOST:$STAGE_DIR/requirements.txt"

  remote <<REMOTE
set -euo pipefail
rsync -a --delete "$STAGE_DIR/app/" /opt/pi-config-ui/app/
cp "$STAGE_DIR/requirements.txt" /opt/pi-config-ui/requirements.txt
mkdir -p /opt/pi-config-ui/deploy
# --delete matters here: without it, a file removed from the repo (e.g.
# an old script folded into another during a consolidation) silently
# lingers on the Pi forever, executable and out of sync with what's
# actually referenced by any systemd unit/cron entry — exactly the kind
# of orphaned-file failure risk this project is trying to keep down.
rsync -a --delete "$STAGE_DIR/deploy/" /opt/pi-config-ui/deploy/
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
export DEBIAN_FRONTEND=noninteractive
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
# Board-agnostic choices (Raspberry Pi OS and Armbian alike):
# - wireguard-tools, not the `wireguard` metapackage: on Armbian the
#   metapackage pulls a stock Debian kernel that can replace the board's.
# - bind9-dnsutils: plain `dnsutils` has no candidate on Debian trixie.
# - resolvconf only without systemd-resolved: they conflict, and apt
#   would remove the resolver the board is currently using.
# - polkitd/wpasupplicant: preinstalled on Raspberry Pi OS, absent on
#   Armbian minimal; --no-install-recommends would otherwise skip them.
# avahi-daemon: <hostname>.local keeps the Pi reachable if its IP changes.
PKGS=(python3 python3-venv wireguard-tools iptables network-manager
  wpasupplicant bind9-dnsutils whiptail rsync curl cron openssl iproute2
  avahi-daemon)
if apt-cache show polkitd >/dev/null 2>&1; then PKGS+=(polkitd); else PKGS+=(policykit-1); fi
systemctl is-active --quiet systemd-resolved || PKGS+=(resolvconf)

# On a systemd-networkd board (Armbian), NetworkManager's postinst starts
# it immediately; keep it hands-off every interface until
# migrate_to_networkmanager hands Wi-Fi over with a rollback guard.
if systemctl is-active --quiet systemd-networkd && ! systemctl is-active --quiet NetworkManager; then
  install -d /etc/NetworkManager/conf.d
  printf '[keyfile]\nunmanaged-devices=*\n' > /etc/NetworkManager/conf.d/99-pi-config-ui-hold.conf
fi
apt-get "${APT_OPTS[@]}" install -y --no-install-recommends "${PKGS[@]}"

if ! modprobe wireguard 2>/dev/null && [ ! -d /sys/module/wireguard ]; then
  echo "ERROR: this kernel has no WireGuard support (needs Linux >= 5.6 or the module)." >&2
  exit 1
fi

if [ ! -d /opt/pi-config-ui/venv ]; then
  sudo -u pi-config-ui python3 -m venv /opt/pi-config-ui/venv
fi
# piwheels only builds 32-bit ARM wheels; arm64 boards get prebuilt
# aarch64 wheels from PyPI directly.
PIP_INDEX=()
case "$(dpkg --print-architecture)" in
  armhf|armel) PIP_INDEX=(--index-url https://www.piwheels.org/simple) ;;
esac
sudo -u pi-config-ui env PIP_NO_CACHE_DIR=1 /opt/pi-config-ui/venv/bin/pip install -q \
  "${PIP_INDEX[@]}" -r /opt/pi-config-ui/requirements.txt
REMOTE
}

# The Network tab (app/network.py) and Wi-Fi recovery drive nmcli, so
# NetworkManager must own Wi-Fi. Raspberry Pi OS already does this; Armbian
# ships netplan + systemd-networkd instead. The switch runs detached on the
# Pi (this SSH session drops with Wi-Fi) and reverts itself unless
# NetworkManager brings a connection with a default route up within ~90s.
migrate_to_networkmanager() {
  log "Ensuring NetworkManager manages networking"
  local state
  state=$(remote <<'REMOTE'
set -euo pipefail
HOLD=/etc/NetworkManager/conf.d/99-pi-config-ui-hold.conf
# networkd stays running after a successful migration until the next reboot.
if ! systemctl is-active --quiet systemd-networkd || [ -f /etc/netplan/90-pi-config-ui-networkmanager.yaml ]; then
  rm -f "$HOLD"
  echo done
  exit 0
fi
if ! command -v netplan >/dev/null; then
  echo no-netplan
  exit 0
fi
rm -f /run/pi-config-ui-nm-migrate.result
cat > /run/pi-config-ui-nm-migrate.sh <<'MIGRATE'
#!/bin/bash
set -u
HOLD=/etc/NetworkManager/conf.d/99-pi-config-ui-hold.conf
OVERRIDE=/etc/netplan/90-pi-config-ui-networkmanager.yaml
RESULT=/run/pi-config-ui-nm-migrate.result
online() {
  nmcli -t -f STATE device 2>/dev/null | grep -qx connected && ip -4 route show default | grep -q .
}
printf 'network:\n  version: 2\n  renderer: NetworkManager\n' > "$OVERRIDE"
chmod 600 "$OVERRIDE"
rm -f "$HOLD"
netplan generate
# netplan apply leaves netplan-wpa-wlan0 holding the radio, so NM's
# wpa_supplicant "couldn't grab this interface"; release it explicitly.
systemctl stop 'netplan-wpa-*.service' systemd-networkd.socket systemd-networkd.service
# netplan's udev rule tagged wlan0 NM_UNMANAGED at boot; re-evaluate with the regenerated rules.
udevadm control --reload
udevadm trigger --action=change --subsystem-match=net
udevadm settle
systemctl enable NetworkManager NetworkManager-wait-online
systemctl restart NetworkManager
for _ in $(seq 1 45); do
  if online; then
    # networkd now manages nothing; its wait-online would stall every boot.
    systemctl disable systemd-networkd.service systemd-networkd.socket systemd-networkd-wait-online.service
    echo ok > "$RESULT"
    exit 0
  fi
  sleep 2
done
rm -f "$OVERRIDE"
printf '[keyfile]\nunmanaged-devices=*\n' > "$HOLD"
systemctl restart NetworkManager
netplan generate
systemctl start systemd-networkd.socket systemd-networkd.service
netplan apply
echo rolled-back > "$RESULT"
MIGRATE
systemd-run --quiet --unit=pi-config-ui-nm-migrate --collect /bin/bash /run/pi-config-ui-nm-migrate.sh
echo started
REMOTE
)

  case "$state" in
    done) echo "NetworkManager already manages networking."; return 0 ;;
    no-netplan)
      warn "systemd-networkd is active but netplan is missing — switch networking to NetworkManager manually (nmtui), then re-run."
      exit 1 ;;
  esac

  warn "Handing Wi-Fi to NetworkManager; SSH will drop briefly (auto-rollback after ~90s if it fails)."
  warn "NetworkManager's DHCP client ID differs from networkd's, so the Pi may get a new IP — a DHCP reservation avoids this."
  local result="" host user_prefix=""
  case "$PI_HOST" in *@*) user_prefix="${PI_HOST%%@*}@" ;; esac
  for _ in $(seq 1 36); do
    sleep 5
    for host in "$PI_HOST" "${user_prefix}${PI_HOSTNAME}.local"; do
      result=$(pi_ssh -o ConnectTimeout=5 -o BatchMode=yes "$host" \
        'cat /run/pi-config-ui-nm-migrate.result 2>/dev/null' 2>/dev/null || true)
      [ -n "$result" ] && break
    done
    case "$result" in
      ok)
        echo "NetworkManager now manages networking."
        if [ "$host" != "$PI_HOST" ]; then
          warn "The Pi's IP changed; continuing via $host (use PI_HOST=$host next time)."
          PI_HOST="$host"
        fi
        return 0 ;;
      rolled-back)
        warn "NetworkManager didn't come online; the Pi rolled back to systemd-networkd. Check: journalctl -u NetworkManager"
        exit 1 ;;
    esac
  done
  warn "Couldn't reach $PI_HOST after the switch. It may have a new IP: check your router, then re-run with PI_HOST=user@<new-ip>."
  exit 1
}

# Extra SSIDs reuse the active Wi-Fi's password, read on the Pi itself so it
# never crosses SSH or lands in this terminal.
configure_wifi() {
  [ -n "$WIFI_EXTRA_SSIDS" ] || return 0
  log "Adding Wi-Fi networks: $WIFI_EXTRA_SSIDS"
  remote_with WIFI_EXTRA_SSIDS <<'REMOTE'
set -euo pipefail
active=$(nmcli -t -f NAME,TYPE connection show --active | awk -F: '$2=="802-11-wireless"{print $1; exit}')
[ -n "$active" ] || { echo "No active Wi-Fi connection to copy the password from." >&2; exit 1; }
for ssid in $WIFI_EXTRA_SSIDS; do
  if nmcli -t -f NAME connection show | grep -qxF "$ssid" \
    || [ "$(nmcli -g 802-11-wireless.ssid connection show "$active")" = "$ssid" ]; then
    echo "$ssid already configured."
    continue
  fi
  psk=$(nmcli -s -g 802-11-wireless-security.psk connection show "$active")
  [ -n "$psk" ] || { echo "Active Wi-Fi '$active' has no PSK to reuse." >&2; exit 1; }
  file="/etc/NetworkManager/system-connections/$ssid.nmconnection"
  (umask 077; cat > "$file" <<EOF
[connection]
id=$ssid
uuid=$(cat /proc/sys/kernel/random/uuid)
type=wifi
interface-name=wlan0
autoconnect-priority=10

[wifi]
mode=infrastructure
ssid=$ssid

[wifi-security]
key-mgmt=wpa-psk
psk=$psk

[ipv4]
method=auto

[ipv6]
method=auto
EOF
  )
  echo "$ssid added (preferred when in range; takes effect on the next reconnect)."
done
nmcli connection reload
REMOTE
}

configure_sso() {
  log "Checking SSO configuration"
  if ! remote <<'REMOTE' && [ -f "$REPO_ROOT/deploy/sso.env" ]
test -f /etc/pi-config-ui/sso.env
REMOTE
  then
    # Same OIDC client for every device; only the redirect host differs (fixed below).
    pi_ssh "$PI_HOST" "sudo sh -c 'umask 077; cat > /etc/pi-config-ui/sso.env'" < "$REPO_ROOT/deploy/sso.env"
    echo "Copied local deploy/sso.env to the Pi."
  fi
  if remote <<'REMOTE'
test -f /etc/pi-config-ui/sso.env
REMOTE
  then
    remote_with CERT_CN <<'REMOTE'
set -euo pipefail
F=/etc/pi-config-ui/sso.env
want="SSO_REDIRECT_URI=https://$CERT_CN/auth/callback"
if ! grep -qxF "$want" "$F"; then
  sed -i "s#^SSO_REDIRECT_URI=.*#$want#" "$F"
  echo "SSO redirect set to https://$CERT_CN/auth/callback — add it to the Authentik provider's redirect URIs."
fi
chown pi-config-ui:pi-config-ui "$F"
chmod 600 "$F"
REMOTE
  else
    warn "No /etc/pi-config-ui/sso.env found — installing the placeholder template."
    scp -q -o "HostKeyAlias=$HOST_KEY_ALIAS" "$REPO_ROOT/deploy/sso.env.example" "$PI_HOST:/tmp/sso.env.staged-$$"
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
cp /opt/pi-config-ui/deploy/polkit-rules/51-pi-config-ui-power.rules /etc/polkit-1/rules.d/
systemctl daemon-reload
systemctl enable pi-config-ui pi-wg-helperd pi-config-ui-boot-check
REMOTE
}

# Physical-console Wi-Fi recovery: HDMI+keyboard fallback when the Pi has
# no working network at all (so the web UI is unreachable too) — see
# docs/RUNBOOK.md. Two scripts (already synced to /opt/pi-config-ui/deploy/
# by deploy_code) + two units (a gate, and a separate TTY-owning console
# unit only ever started explicitly by the gate — never both enabled) +
# one udev rule for the hotplug trigger.
install_wifi_recovery() {
  log "Installing physical-console Wi-Fi recovery"
  remote <<'REMOTE'
set -euo pipefail
cp /opt/pi-config-ui/deploy/wifi-recovery-check.service /etc/systemd/system/wifi-recovery-check.service
cp /opt/pi-config-ui/deploy/wifi-recovery-console.service /etc/systemd/system/wifi-recovery-console.service
cp /opt/pi-config-ui/deploy/udev-rules/99-wifi-recovery.rules /etc/udev/rules.d/99-wifi-recovery.rules
udevadm control --reload-rules
systemctl daemon-reload
systemctl enable wifi-recovery-check.service
REMOTE
}

set_permissions() {
  log "Setting file/directory permissions"
  remote <<'REMOTE'
set -euo pipefail
chown -R pi-config-ui:pi-config-ui /opt/pi-config-ui
chown -R pi-config-ui:pi-config-ui /etc/pi-config-ui/tls /etc/pi-config-ui/wireguard /etc/pi-config-ui/monitor
# Package rollback artifacts are consumed by a root-run systemd service and
# must not be replaceable by the unprivileged web application.
chown -R root:root /etc/pi-config-ui/monitor/pkg-backups
chmod 755 /etc/pi-config-ui/monitor/pkg-backups
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
2,12,22,32,42,52 * * * * root /opt/pi-config-ui/maintenance.sh health >> /var/log/pi-config-ui-maintenance.log 2>&1
7,17,27,37,47,57 * * * * root /opt/pi-config-ui/maintenance.sh ddns-update >> /var/log/pi-config-ui-maintenance.log 2>&1
4,14,24,34,44,54 * * * * root /opt/pi-config-ui/maintenance.sh wg-guard >> /var/log/pi-config-ui-maintenance.log 2>&1
0 3 * * * root /opt/pi-config-ui/maintenance.sh cert-renew >> /var/log/pi-config-ui-maintenance.log 2>&1
0 4 * * 0 root /opt/pi-config-ui/maintenance.sh cleanup >> /var/log/pi-config-ui-maintenance.log 2>&1
0 5 * * 3 root /opt/pi-config-ui/maintenance.sh os-update >> /var/log/pi-config-ui-maintenance.log 2>&1
35 5 * * 3 root /opt/pi-config-ui/maintenance.sh reboot >> /var/log/pi-config-ui-maintenance.log 2>&1
CRON
chmod 644 /etc/cron.d/pi-config-ui-maintenance
touch /var/log/pi-config-ui-maintenance.log

# Boot is when an overlapping client tunnel locks the Pi out, so guard right
# after every wg-quick start, not just on the cron backstop. "-" = never fail the start.
install -d /etc/systemd/system/wg-quick@.service.d
cat > /etc/systemd/system/wg-quick@.service.d/pi-config-ui-guard.conf <<'UNIT'
# Managed by deploy/deploy.sh — see maintenance.sh cmd_wg_guard.
[Service]
ExecStartPost=-/opt/pi-config-ui/maintenance.sh wg-guard
UNIT
systemctl daemon-reload

# Never rotated otherwise: seven cron jobs above (three every 10 minutes)
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

# Creates/updates only this device's own records (from device.env).
register_dns() {
  log "Registering DNS records in Cloudflare"
  remote <<'REMOTE'
set -euo pipefail
/opt/pi-config-ui/maintenance.sh ddns-update
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
COOKIE_HEADER=$(curl -sk -D - -o /dev/null "https://localhost:${PORT}/auth/login" \
  | awk 'tolower($1) == "set-cookie:" {sub(/\r$/, ""); print; exit}')
if [ -z "$COOKIE_HEADER" ]; then
  echo "set-cookie header: missing"
else
  COOKIE_HEADER_LOWER=${COOKIE_HEADER,,}
  for attribute in "samesite=lax" "secure" "httponly"; do
    if [[ "$COOKIE_HEADER_LOWER" == *"$attribute"* ]]; then
      echo "$attribute=yes"
    else
      echo "$attribute=no"
    fi
  done
fi
echo "-- Syd-Home client tunnel state (must be UNCHANGED by this script) --"
systemctl is-enabled wg-quick@Syd-Home 2>/dev/null || echo "not present"
systemctl is-active wg-quick@Syd-Home 2>/dev/null || echo "not active"
echo "-- WireGuard client overlap guard (no WARNING = no overlap with the local LAN) --"
/opt/pi-config-ui/maintenance.sh wg-guard
wg show all allowed-ips 2>/dev/null || true
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

  preflight
  if [ -n "$ONLY" ]; then
    "$ONLY"
  else
    bootstrap_system
    provision_tls_cert
    deploy_code
    install_dependencies
    migrate_to_networkmanager
    configure_wifi
    configure_sso
    install_units
    install_wifi_recovery
    set_permissions
    setup_cron
    register_dns
    restart_services
    verify_deployment
  fi

  log "Done."
}

main "$@"
