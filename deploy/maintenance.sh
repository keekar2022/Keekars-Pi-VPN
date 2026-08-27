#!/usr/bin/env bash
# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com
#
# Scheduled maintenance for this Pi, installed by deploy/deploy.sh into
# /etc/cron.d/pi-config-ui-maintenance. Runs as root (cron.d specifies the
# user directly). Manual invocation:
#   sudo /opt/pi-config-ui/maintenance.sh {health|cert-renew|cleanup|os-update|ddns-update|reboot|boot-check}
#
# SAFETY: must never touch a WireGuard CLIENT-role tunnel (e.g. Syd-Home)
# — see docs/PROJECT_NOTES.md Part 3 for why (a client tunnel with
# AllowedIPs overlapping the current LAN has already caused a full SSH
# lockout once on this project). Only the app, pi-wg-helperd, and
# server-role tunnels this project's own WireGuard tab created are ever
# restarted here.

set -euo pipefail

# Extend only with tunnel names this project's own "Create tunnel" (server
# mode) feature created — never a client-role tunnel.
SERVER_TUNNELS=("Bpl-Home")

# Cloudflare zone/record for cmd_ddns_update, below. This zone ID is
# specific to this deployment's DNS zone (keekar.au) — if this project is
# ever pointed at a different domain, update both here. DDNS_RECORD_NAME
# is deliberately the WireGuard Bpl-Home tunnel's own public endpoint, NOT
# CERT_CN/vpn.bpl.keekar.au (deploy.sh) — that one intentionally resolves
# to this Pi's private LAN IP for the admin UI, see docs/RUNBOOK.md §5b/§5c.
CF_ZONE_ID="447c0f403d88c4810bfcb945d4466748"
DDNS_RECORD_NAME="wg.bpl.keekar.au"

log() {
  logger -t pi-config-ui-maintenance "$1" 2>/dev/null || true
  echo "$(date -Is) $1"
}

# Shared with app/monitor.py's own _load_state/_save_state (same file,
# same tmp-then-atomic-rename pattern) — this script only ever adds/removes
# its own keys (pending_update_backup, rollback_attempted), never touches
# the downtime-tracking fields the app itself owns.
_STATE_FILE=/etc/pi-config-ui/monitor/state.json

state_get() {
  python3 -c "
import json
try:
    d = json.load(open('$_STATE_FILE'))
    v = d.get('$1')
    print(v if v is not None else '')
except Exception:
    print('')
"
}

state_set() {
  python3 -c "
import json, os
path = '$_STATE_FILE'
try:
    d = json.load(open(path))
except Exception:
    d = {}
d['$1'] = '$2'
tmp = path + '.tmp'
with open(tmp, 'w') as f:
    json.dump(d, f)
os.replace(tmp, path)
"
}

state_unset() {
  python3 -c "
import json, os
path = '$_STATE_FILE'
try:
    d = json.load(open(path))
except Exception:
    d = {}
d.pop('$1', None)
tmp = path + '.tmp'
with open(tmp, 'w') as f:
    json.dump(d, f)
os.replace(tmp, path)
" 2>/dev/null || true
}

write_rollback_alert() {
  local reason="$1" reverted="$2" unavailable="$3"
  python3 -c "
import json, os, sys
path = '/etc/pi-config-ui/monitor/rollback_alert.json'
data = {
    'timestamp': sys.argv[1],
    'reason': sys.argv[2],
    'packages_reverted': [p for p in sys.argv[3].split('|') if p],
    'packages_unavailable': [p for p in sys.argv[4].split('|') if p],
}
tmp = path + '.tmp'
with open(tmp, 'w') as f:
    json.dump(data, f)
os.replace(tmp, path)
" "$(date -Is)" "$reason" "$reverted" "$unavailable"
  log "ALERT: $reason"
}

cmd_health() {
  for unit in pi-config-ui pi-wg-helperd; do
    if ! systemctl is-active --quiet "$unit"; then
      log "WARNING: $unit is not active, restarting"
      systemctl restart "$unit" || log "ERROR: failed to restart $unit"
    fi
  done

  for tunnel in "${SERVER_TUNNELS[@]}"; do
    unit="wg-quick@${tunnel}"
    if systemctl is-enabled --quiet "$unit" 2>/dev/null && ! systemctl is-active --quiet "$unit"; then
      log "WARNING: $unit is enabled but not active, restarting"
      systemctl restart "$unit" || log "ERROR: failed to restart $unit"
    fi
  done

  local port code
  port=$(ss -tlnp 2>/dev/null | grep uvicorn | grep -oE ':[0-9]+' | head -1 | tr -d ':') || true
  port="${port:-443}"
  # See deploy.sh's restart_services for why this must not be
  # `|| echo "000"` (curl already prints "000" itself on failure; doing
  # both produces "000000", which false-positives the check below).
  code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 "https://localhost:${port}/" 2>/dev/null || true)
  code="${code:-000}"
  if [ "$code" != "303" ] && [ "$code" != "200" ]; then
    log "WARNING: app health check returned HTTP $code, restarting pi-config-ui"
    systemctl restart pi-config-ui || log "ERROR: failed to restart pi-config-ui"
  fi
}

cmd_cert_renew() {
  # Real renewal is handled by acme.sh's own root crontab entry and its
  # --reloadcmd (installed by deploy.sh's provision_tls_cert, see
  # docs/RUNBOOK.md §5b) — that fires only when a renewal actually
  # happens, which is more reliable than reimplementing "did it renew"
  # detection here. This is just a health check to catch a silently-failed
  # acme.sh renewal before the cert actually expires.
  local cert=/etc/pi-config-ui/tls/cert.pem
  if [ ! -f "$cert" ]; then
    log "cert-renew: no cert found at $cert"
    return 0
  fi

  local issuer subject
  issuer=$(openssl x509 -in "$cert" -noout -issuer 2>/dev/null)
  subject=$(openssl x509 -in "$cert" -noout -subject 2>/dev/null)
  if [ "$issuer" = "$subject" ]; then
    log "cert-renew: still on a self-signed cert ($subject) — see docs/RUNBOOK.md §5b to provision a real one"
    return 0
  fi

  if ! openssl x509 -in "$cert" -noout -checkend $((14 * 86400)) >/dev/null 2>&1; then
    log "WARNING: cert-renew: $cert expires within 14 days and acme.sh has not renewed it — check 'acme.sh --list' and 'systemctl status pi-config-ui' on the Pi"
  fi
}

cmd_cleanup() {
  journalctl --vacuum-time=7d >/dev/null 2>&1 || true
  apt-get clean || true
  log "cleanup: journald vacuumed to 7d, apt cache cleaned"
}

cmd_os_update() {
  # Plain `upgrade`, not `dist-upgrade` — on a device with no easy physical
  # recovery, avoid letting apt remove/replace packages to satisfy new
  # dependencies unattended; a conservative upgrade is the safer default
  # here. ForceIPv4 matches deploy.sh's install_dependencies (this Pi's
  # IPv6 path to some hosts has been observed unreachable). --force-confdef
  # / --force-confold auto-resolve conffile prompts instead of hanging
  # cron waiting for interactive input that will never come.
  export DEBIAN_FRONTEND=noninteractive
  local apt_opts=(-o Acquire::ForceIPv4=true -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

  if ! apt-get "${apt_opts[@]}" update -qq; then
    log "ERROR: os-update: apt-get update failed — check /var/log/apt/term.log"
    return 0
  fi

  # Best-effort pre-upgrade snapshot for cmd_boot_check's rollback path
  # (docs/RUNBOOK.md) — records what's about to change and preserves each
  # package's CURRENT .deb if it's still sitting in the apt cache. Not
  # guaranteed: Raspbian's mirrors don't keep old versions downloadable
  # once superseded, so a package whose .deb was already evicted from the
  # cache simply has nothing to revert to later — this only ever improves
  # the odds, never blocks the upgrade itself on any failure here.
  local backup_dir
  backup_dir="/etc/pi-config-ui/monitor/pkg-backups/$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$backup_dir"
  apt-get "${apt_opts[@]}" upgrade -y -qq --simulate 2>/dev/null \
    | awk '/^Inst/ {print $2}' > "$backup_dir/pkgnames.txt" || true
  if [ -s "$backup_dir/pkgnames.txt" ]; then
    : > "$backup_dir/packages.txt"
    while read -r pkg; do
      ver=$(dpkg-query -W -f='${Version}' "$pkg" 2>/dev/null) || continue
      echo "${pkg}=${ver}" >> "$backup_dir/packages.txt"
      find /var/cache/apt/archives -maxdepth 1 -name "${pkg%%:*}_*.deb" -exec cp {} "$backup_dir/" \; 2>/dev/null || true
    done < "$backup_dir/pkgnames.txt"
    rm -f "$backup_dir/pkgnames.txt"
    state_set pending_update_backup "$backup_dir"
    state_set rollback_attempted "0"
    log "os-update: snapshotted $(wc -l < "$backup_dir/packages.txt" | tr -d ' ') package(s) to $backup_dir before upgrading"
  else
    rmdir "$backup_dir" 2>/dev/null || true
  fi
  # Keep only the 2 most recent snapshots — bounded disk use.
  ls -1dt /etc/pi-config-ui/monitor/pkg-backups/*/ 2>/dev/null | tail -n +3 | xargs -r rm -rf

  if apt-get "${apt_opts[@]}" upgrade -y -qq; then
    log "os-update: apt update/upgrade completed"
  else
    log "ERROR: os-update: apt update/upgrade failed — check /var/log/apt/term.log"
  fi
  # Deliberately does not reboot itself — a kernel/library upgrade needing
  # a reboot to take effect is far less risky than an unattended reboot of
  # a remote VPN box silently failing to come back up. Surface it and let
  # a human decide when (cmd_reboot below picks it up on its own schedule
  # regardless).
  if [ -f /var/run/reboot-required ]; then
    log "WARNING: os-update: a reboot is required to complete this update (/var/run/reboot-required present) — reboot manually when convenient; this script does not auto-reboot"
  fi

  # requirements.txt exact-pins every package (see the file itself) — this
  # only re-syncs the venv to those pinned versions (self-heals drift if
  # the venv was ever created before a pin changed), it never fetches
  # anything newer than what's already committed there. Bumping the pins
  # themselves to newer upstream releases is a deliberate, reviewed repo
  # change (edit requirements.txt, test, redeploy) — not something an
  # unattended cron job on a remote device with no rollback should do,
  # same conservative philosophy as `upgrade` vs `dist-upgrade` above.
  # `pip list --outdated` is read-only: it only logs what's newer
  # upstream for a human to decide on, mirroring cert-renew's expiry
  # warning and the reboot-required warning above.
  local venv=/opt/pi-config-ui/venv
  if [ -x "$venv/bin/pip" ]; then
    if sudo -u pi-config-ui "$venv/bin/pip" install -q --timeout 60 \
        --index-url https://www.piwheels.org/simple \
        -r /opt/pi-config-ui/requirements.txt; then
      log "os-update: pip deps re-synced to requirements.txt pins"
    else
      log "ERROR: os-update: pip sync failed — check the output above in /var/log/pi-config-ui-maintenance.log"
    fi

    # piwheels can lag behind PyPI for brand-new releases — informational
    # only, not a reason to fail this step.
    local outdated
    outdated=$(sudo -u pi-config-ui "$venv/bin/pip" list --outdated --timeout 60 \
      --index-url https://www.piwheels.org/simple 2>/dev/null | tail -n +3) || true
    if [ -n "$outdated" ]; then
      log "WARNING: os-update: newer versions available upstream (requirements.txt pins left untouched — review and bump manually): $(echo "$outdated" | tr '\n' ';' | sed 's/;$//')"
    fi
  else
    log "os-update: no venv at $venv, skipping pip sync"
  fi
}

cmd_ddns_update() {
  # Keeps wg.bpl.keekar.au (the WireGuard Bpl-Home tunnel's public,
  # internet-facing endpoint — deliberately separate from
  # vpn.bpl.keekar.au, which intentionally resolves to this Pi's private
  # LAN IP for the admin UI, see docs/RUNBOOK.md §5b) pointed at whatever
  # public IP this device currently has, so a move to a different network
  # (different city/ISP, different WAN IP) doesn't leave clients dialing a
  # stale address. No-ops quietly if the Cloudflare token from
  # provision_tls_cert (docs/RUNBOOK.md §5b) isn't present — this device
  # may not have a real cert/DDNS provisioned yet.
  local token_file=/root/.cf-dns-token
  local zone_id="$CF_ZONE_ID"
  local record_name="$DDNS_RECORD_NAME"

  if [ ! -f "$token_file" ]; then
    log "ddns-update: no $token_file — skipping (see docs/RUNBOOK.md §5c)"
    return 0
  fi

  local cf_token current_ip
  set -a
  # shellcheck disable=SC1090
  . "$token_file"
  set +a
  cf_token="${CF_Token:-}"
  if [ -z "$cf_token" ]; then
    log "ERROR: ddns-update: $token_file present but CF_Token is empty"
    return 0
  fi

  current_ip=$(curl -s --max-time 20 https://ifconfig.me) || true
  if ! [[ "$current_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    log "WARNING: ddns-update: couldn't determine current public IP (got '$current_ip'), skipping this run"
    return 0
  fi

  local lookup record_id existing_ip
  lookup=$(curl -s --max-time 20 \
    -H "Authorization: Bearer $cf_token" -H "Content-Type: application/json" \
    "https://api.cloudflare.com/client/v4/zones/${zone_id}/dns_records?name=${record_name}") || true

  record_id=$(python3 -c "
import json, sys
try:
    r = json.loads(sys.argv[1])['result']
    print(r[0]['id'] if r else '')
except Exception:
    print('')
" "$lookup")
  existing_ip=$(python3 -c "
import json, sys
try:
    r = json.loads(sys.argv[1])['result']
    print(r[0]['content'] if r else '')
except Exception:
    print('')
" "$lookup")

  if [ -z "$record_id" ]; then
    log "ERROR: ddns-update: no DNS record found for $record_name — create it once manually (see docs/RUNBOOK.md), this script only updates an existing record"
    return 0
  fi

  if [ "$existing_ip" = "$current_ip" ]; then
    return 0
  fi

  local resp
  resp=$(curl -s --max-time 20 -X PATCH \
    -H "Authorization: Bearer $cf_token" -H "Content-Type: application/json" \
    --data "{\"content\":\"${current_ip}\"}" \
    "https://api.cloudflare.com/client/v4/zones/${zone_id}/dns_records/${record_id}") || true

  if echo "$resp" | grep -q '"success":true'; then
    log "ddns-update: $record_name updated $existing_ip -> $current_ip"
  else
    log "ERROR: ddns-update: Cloudflare update failed: $resp"
  fi
}

cmd_reboot() {
  # Unconditional weekly reboot for general hygiene (clears any slow memory
  # creep, applies a kernel update if os-update flagged reboot-required
  # earlier in the week — see cmd_os_update). Scheduled for a low-traffic
  # window (docs/RUNBOOK.md); logged before rebooting since that write must
  # land before the box actually goes down.
  log "reboot: scheduled weekly reboot starting now"
  systemctl reboot
}

cmd_boot_check() {
  # Runs once per boot via pi-config-ui-boot-check.service. Covers "the
  # system boots fine but pi-config-ui/a package it needs is broken" — NOT
  # a kernel/bootloader that fails to boot at all, which this Pi Zero W's
  # hardware has no A/B/tryboot scheme to recover from automatically (see
  # docs/RUNBOOK.md). Rollback is best-effort: only packages whose
  # pre-upgrade .deb was preserved by cmd_os_update's snapshot step can
  # actually be reverted.
  local port code healthy=0 attempt
  port=$(ss -tlnp 2>/dev/null | grep uvicorn | grep -oE ':[0-9]+' | head -1 | tr -d ':') || true
  port="${port:-443}"

  for attempt in $(seq 1 18); do
    code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 5 "https://localhost:${port}/" 2>/dev/null || true)
    code="${code:-000}"
    if [ "$code" = "303" ] || [ "$code" = "200" ]; then
      healthy=1
      break
    fi
    sleep 10
  done

  local pending
  pending=$(state_get pending_update_backup)

  if [ "$healthy" = "1" ]; then
    if [ -n "$pending" ]; then
      state_unset pending_update_backup
      state_unset rollback_attempted
      log "boot-check: healthy — confirming last update as good"
    fi
    return 0
  fi

  log "WARNING: boot-check: pi-config-ui not healthy after boot, attempting restart"
  systemctl restart pi-config-ui || true
  # Poll, don't guess a fixed sleep: uvicorn's own import/startup on this
  # CPU has consistently taken ~20s in practice (single ARMv6 core, no
  # JIT — same observation as deploy.sh's restart_services) — a single
  # fixed sleep shorter than that produces a false "still unhealthy" alert
  # for a restart that actually would have succeeded a few seconds later.
  local restart_healthy=0
  for attempt in $(seq 1 9); do
    sleep 10
    code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 5 "https://localhost:${port}/" 2>/dev/null || true)
    code="${code:-000}"
    if [ "$code" = "303" ] || [ "$code" = "200" ]; then
      restart_healthy=1
      break
    fi
  done
  if [ "$restart_healthy" = "1" ]; then
    log "boot-check: recovered after restart"
    if [ -n "$pending" ]; then
      state_unset pending_update_backup
      state_unset rollback_attempted
    fi
    return 0
  fi

  local already_attempted
  already_attempted=$(state_get rollback_attempted)
  local reverted=() unavailable=()

  if [ -n "$pending" ] && [ "$already_attempted" != "1" ] && [ -f "$pending/packages.txt" ]; then
    log "WARNING: boot-check: still unhealthy — attempting package rollback from $pending"
    while read -r pkgver; do
      [ -z "$pkgver" ] && continue
      local pkg="${pkgver%%=*}"
      local deb
      deb=$(find "$pending" -maxdepth 1 -name "${pkg%%:*}_*.deb" 2>/dev/null | head -1)
      if [ -n "$deb" ]; then
        if apt-get install -y --allow-downgrades "$deb" >/dev/null 2>&1; then
          reverted+=("$pkgver")
        else
          unavailable+=("$pkgver (found .deb but install failed)")
        fi
      else
        unavailable+=("$pkgver (no cached .deb to revert to)")
      fi
    done < "$pending/packages.txt"

    state_set rollback_attempted "1"
    local reverted_str unavailable_str
    reverted_str=$(IFS='|'; echo "${reverted[*]:-}")
    unavailable_str=$(IFS='|'; echo "${unavailable[*]:-}")
    write_rollback_alert "pi-config-ui failed its post-update health check; attempted package rollback" "$reverted_str" "$unavailable_str"

    if [ "${#reverted[@]}" -gt 0 ]; then
      log "boot-check: reverted ${#reverted[@]} package(s), rebooting to apply"
      systemctl reboot
      return 0
    fi
  else
    write_rollback_alert "pi-config-ui failed its post-boot health check; no safe automatic rollback available (no pending update to blame, or already attempted once)" "" ""
  fi

  log "ERROR: boot-check: pi-config-ui still unhealthy and no further automatic action will be taken — manual intervention required"
}

case "${1:-}" in
  health) cmd_health ;;
  cert-renew) cmd_cert_renew ;;
  cleanup) cmd_cleanup ;;
  os-update) cmd_os_update ;;
  ddns-update) cmd_ddns_update ;;
  reboot) cmd_reboot ;;
  boot-check) cmd_boot_check ;;
  *) echo "Usage: $0 {health|cert-renew|cleanup|os-update|ddns-update|reboot|boot-check}" >&2; exit 1 ;;
esac
