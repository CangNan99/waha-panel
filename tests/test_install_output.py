import re
import unittest
from pathlib import Path


class InstallerOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.shell = (cls.root / "install.sh").read_text(encoding="utf-8")
        cls.powershell = (cls.root / "install.ps1").read_text(encoding="utf-8")

    def test_shell_reports_masked_panel_and_waha_credentials(self):
        source = self.shell
        self.assertIn("mask_secret", source)
        for token in (
            "Panel administrator", "Panel password (masked)",
            "WAHA Dashboard username", "WAHA Dashboard password (masked)",
            "WAHA API key (masked)", "secrets/waha_credentials",
            "secrets/panel_admin_bootstrap",
        ):
            self.assertIn(token, source)
        self.assertNotRegex(source, r"printf[^\n]*Initial panel password[^\n]*\$admin_password")
        self.assertNotRegex(source, r"printf[^\n]*WAHA API key[^\n]*(?<!mask_secret \")\$waha_api_key")

    def test_powershell_reports_masked_panel_and_waha_credentials(self):
        source = self.powershell
        self.assertIn("Mask-Secret", source)
        for token in (
            "Panel administrator", "Panel password (masked)",
            "WAHA Dashboard username", "WAHA Dashboard password (masked)",
            "WAHA API key (masked)", "secrets/waha_credentials",
            "secrets/panel_admin_bootstrap",
        ):
            self.assertIn(token, source)
        self.assertNotRegex(source, r"Write-Host[^\n]*Initial panel password[^\n]*\$adminPassword")
        self.assertNotRegex(source, r"Write-Host[^\n]*WAHA API key[^\n]*\$wahaApiKey(?!\))")

    def test_existing_install_branch_only_points_to_preserved_files(self):
        for source in (self.shell, self.powershell):
            self.assertRegex(source, r"Existing administrator credentials were preserved|Existing administrator credentials")
            self.assertIn("waha_credentials", source)


if __name__ == "__main__":
    unittest.main()
