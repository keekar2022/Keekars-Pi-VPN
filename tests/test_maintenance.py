# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com

import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MAINTENANCE_SCRIPT = REPO_ROOT / "deploy" / "maintenance.sh"
DEPLOY_SCRIPT = REPO_ROOT / "deploy" / "deploy.sh"


class MaintenancePolicyTests(unittest.TestCase):
    def test_shell_scripts_parse(self):
        for script in (MAINTENANCE_SCRIPT, DEPLOY_SCRIPT):
            subprocess.run(["bash", "-n", str(script)], check=True)

    def test_update_allows_new_packages_but_refuses_removals(self):
        source = MAINTENANCE_SCRIPT.read_text()
        safe_upgrade = 'full-upgrade --no-remove'

        self.assertGreaterEqual(source.count(safe_upgrade), 2)
        self.assertIn('DPkg::Lock::Timeout=600', source)
        self.assertIn('return 1', source)

    def test_system_changing_operations_share_a_lock(self):
        source = MAINTENANCE_SCRIPT.read_text()

        self.assertIn('run_system_locked 0 health cmd_health', source)
        self.assertIn('run_system_locked 0 cleanup cmd_cleanup', source)
        self.assertIn('run_system_locked 0 os-update cmd_os_update', source)
        self.assertIn('run_system_locked 7200 reboot cmd_reboot', source)
        self.assertIn('run_system_locked 0 boot-check cmd_boot_check', source)

    def test_cron_runs_updates_weekly_without_same_minute_collisions(self):
        source = DEPLOY_SCRIPT.read_text()
        cron_match = re.search(r"<<'CRON'\n(?P<body>.*?)\nCRON", source, re.DOTALL)
        self.assertIsNotNone(cron_match)
        cron = cron_match.group("body")

        self.assertIn('0 5 * * 3 root /opt/pi-config-ui/maintenance.sh os-update', cron)
        self.assertIn('35 5 * * 3 root /opt/pi-config-ui/maintenance.sh reboot', cron)
        self.assertIn('2,12,22,32,42,52 * * * * root /opt/pi-config-ui/maintenance.sh health', cron)
        self.assertIn('7,17,27,37,47,57 * * * * root /opt/pi-config-ui/maintenance.sh ddns-update', cron)

    def test_deploy_verification_does_not_print_session_cookie_value(self):
        source = DEPLOY_SCRIPT.read_text()

        self.assertNotIn('| grep -i set-cookie', source)
        self.assertIn('COOKIE_HEADER_LOWER=${COOKIE_HEADER,,}', source)
        self.assertIn('echo "$attribute=yes"', source)

    def test_deploy_is_safe_on_armbian_and_raspberry_pi_os(self):
        source = DEPLOY_SCRIPT.read_text()
        packages = re.search(r"PKGS=\((.*?)\)", source, re.S).group(1).split()

        # `wireguard` pulls a stock Debian kernel on Armbian; `dnsutils` is gone in trixie.
        self.assertNotIn("wireguard", packages)
        self.assertNotIn("dnsutils", packages)
        self.assertIn("systemctl is-active --quiet systemd-resolved || PKGS+=(resolvconf)", source)
        self.assertIn("armhf|armel) PIP_INDEX=(--index-url https://www.piwheels.org/simple)", source)
        self.assertIn("echo rolled-back", source)
        self.assertLess(source.index("    install_dependencies\n"), source.index("    migrate_to_networkmanager\n"))

    def test_maintenance_uses_piwheels_only_on_32bit_arm(self):
        source = MAINTENANCE_SCRIPT.read_text()

        self.assertEqual(source.count("piwheels.org"), 1)
        self.assertIn("armhf|armel) pip_index=", source)

    def test_each_device_uses_only_its_own_dns_names(self):
        deploy = DEPLOY_SCRIPT.read_text()
        maintenance = MAINTENANCE_SCRIPT.read_text()

        # A default name would let a new device overwrite a shipped device's records.
        self.assertIn('CERT_CN="${CERT_CN:-}"', deploy)
        self.assertNotIn("wg.bpl.keekar.au", maintenance)
        self.assertNotIn('DDNS_RECORD_NAME="wg', maintenance)
        self.assertIn('. "$DEVICE_ENV"', maintenance)
        self.assertIn("/etc/pi-config-ui/device.env", deploy)

    def test_ca_check_compares_distinguished_names_not_raw_output(self):
        source = DEPLOY_SCRIPT.read_text()

        self.assertNotIn('[ "$issuer" != "$subject" ]', source)
        self.assertIn("-checkhost", source)

    def test_cloudflare_token_comes_from_pass_and_acme_skips_doh_check(self):
        source = DEPLOY_SCRIPT.read_text()

        self.assertIn('CF_PASS_ENTRY="${CF_PASS_ENTRY-KeekarACI/Cloudflare}"', source)
        self.assertIn("s/^API_TOKEN=/CF_Token=/p; s/^Account_ID=/CF_Account_ID=/p", source)
        self.assertNotIn("CF_TOKEN_FILE", source)
        # The LAN resolver sinkholes DoH, so acme.sh's propagation check never passes.
        self.assertIn("--dns dns_cf --dnssleep", source)

    def test_ddns_publishes_ipv4_a_and_stable_ipv6_aaaa(self):
        source = MAINTENANCE_SCRIPT.read_text()

        self.assertIn("curl -4 -s --max-time 20 https://ifconfig.me", source)
        self.assertIn('_cf_upsert AAAA "$CF_Token" "$DDNS_RECORD_NAME" "$ipv6"', source)
        # Temporary (privacy) addresses rotate and ULA isn't routable.
        self.assertIn("grep -v -e temporary -e deprecated", source)
        self.assertIn("grep -iv '^f[cd]'", source)
        self.assertIn("-X DELETE", source)

    def test_admin_record_can_point_at_public_ip_for_remote_sites(self):
        deploy = DEPLOY_SCRIPT.read_text()
        maintenance = MAINTENANCE_SCRIPT.read_text()

        self.assertIn("ADMIN_RECORD_TARGET=%s", deploy)
        self.assertIn('ADMIN_RECORD_TARGET="${ADMIN_RECORD_TARGET:-${saved_admin:-lan}}"', deploy)
        self.assertIn('admin_ip="$current_ip"', maintenance)
        self.assertIn('_cf_upsert A "$CF_Token" "$CERT_CN" "$admin_ip"', maintenance)

    def test_wg_guard_runs_after_tunnel_start_and_from_cron(self):
        deploy = DEPLOY_SCRIPT.read_text()
        maintenance = MAINTENANCE_SCRIPT.read_text()

        self.assertIn("  wg-guard) cmd_wg_guard ;;", maintenance)
        self.assertIn("ExecStartPost=-/opt/pi-config-ui/maintenance.sh wg-guard", deploy)
        self.assertIn("4,14,24,34,44,54 * * * * root /opt/pi-config-ui/maintenance.sh wg-guard", deploy)

    def test_wg_guard_trims_live_client_peers_only(self):
        body = re.search(r"cmd_wg_guard\(\) \{(.*?)\n\}\n", MAINTENANCE_SCRIPT.read_text(), re.S).group(1)

        self.assertIn("Endpoint", body)
        self.assertIn('wg set "$tun" peer "$pub" allowed-ips "$keep"', body)
        # Runtime only: rewriting the .conf would keep the range away after a move back home.
        self.assertNotIn("sed -i", body)

    def test_wg_guard_overlap_logic_drops_only_the_local_lan(self):
        body = re.search(r"cmd_wg_guard\(\) \{(.*?)\n\}\n", MAINTENANCE_SCRIPT.read_text(), re.S).group(1)
        program = re.search(r"<<'PY'\n(.*?)\nPY\n", body, re.S).group(1)
        wanted = ["10.6.0.0/24", "192.168.1.0/24", "192.168.2.0/24", "192.168.3.0/24", "fd00::/64"]

        result = subprocess.run(
            ["python3", "-c", program, "192.168.1.0/24 10.7.0.0/24", *wanted],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()

        self.assertEqual(result, ["10.6.0.0/24,192.168.2.0/24,192.168.3.0/24,fd00::/64", "192.168.1.0/24"])


if __name__ == "__main__":
    unittest.main()
