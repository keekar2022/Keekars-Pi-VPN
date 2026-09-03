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


if __name__ == "__main__":
    unittest.main()
