import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_config_safety_customizations import make_safety_upstream


class OverrideModalPatchOnlyTests(unittest.TestCase):
    def test_override_save_submits_only_id_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_safety_upstream(root)
            result = subprocess.run(
                [sys.executable, "scripts/fix_override_streamer_fields.py", str(root)],
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            modal = (root / "app/ui/OverrideModal.tsx").read_text(encoding="utf-8")
            self.assertIn(
                "await onOk({ id: entity?.id, override: cleanValues.override })",
                modal,
            )
            self.assertNotIn("await onOk({ ...entity, ...cleanValues })", modal)


if __name__ == "__main__":
    unittest.main()
