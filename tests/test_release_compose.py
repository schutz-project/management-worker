import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = "/opt/schutz-monitoring/"
BASE = [ROOT + name for name in (
    "compose.otel-only.yaml", "compose.prometheus-only.yaml",
    "compose.management-ec2.yaml",
    "updates/incident-drafts/compose.incident-drafts.yaml",
    "compose.loki-only.yaml", "compose.blackbox-only.yaml",
    "compose.grafana-only.yaml", "compose.ecr.yaml")]
HTTPS = ROOT + "compose.management-https.yaml"

class ReleaseComposeTests(unittest.TestCase):
    def validate(self, files, present=True):
        for script in sorted((Path(__file__).resolve().parents[1] / "deploy").glob("*release.sh")):
            source = script.read_text()
            if "COMPOSE_FILES_PY" not in source:
                continue
            code = source.split("<<'COMPOSE_FILES_PY'\n", 1)[1].split("\nCOMPOSE_FILES_PY", 1)[0]
            with patch.object(sys, "argv", ["validator", ",".join(files)]), patch.object(Path, "is_file", return_value=present), patch("builtins.print") as output:
                exec(compile(code, str(script), "exec"), {})
                self.assertEqual(output.call_args.args[0].splitlines(), files)

    def test_original_eight_files_preserve_order(self):
        self.validate(BASE)

    def test_https_ninth_file_preserves_order(self):
        self.validate(BASE + [HTTPS])

    def test_missing_required_file_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "COMPOSE_FILE_SET_MISMATCH"):
            self.validate(BASE[:-1])

    def test_unknown_file_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "COMPOSE_FILE_SET_MISMATCH"):
            self.validate(BASE + [ROOT + "unexpected.yaml"])

    def test_duplicate_file_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "COMPOSE_FILES_DUPLICATED"):
            self.validate(BASE + [BASE[0]])

    def test_https_override_order_is_enforced(self):
        with self.assertRaisesRegex(SystemExit, "HTTPS_OVERRIDE_MUST_BE_LAST"):
            self.validate([HTTPS] + BASE)

    def test_missing_disk_file_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "COMPOSE_FILE_MISSING"):
            self.validate(BASE, present=False)

if __name__ == "__main__":
    unittest.main()
