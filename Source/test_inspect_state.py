"""inspect_state reports the desktop harness state the shell launches from."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import bridge_core
import vibe_profile


class InspectStateVibeTests(unittest.TestCase):
    def test_inspect_state_reports_vibe_desktop_state(self):
        vibe_status = {
            "vibe_app_path": "/Applications/Vibe.app",
            "vibe_running": True,
            "vibe_profile_active": False,
            "vibe_recovery_needed": True,
            "vibe_home": "/tmp/vibe-home",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(bridge_core, "load_settings", return_value={}), \
                    mock.patch.object(bridge_core, "claude_running", return_value=False), \
                    mock.patch.object(bridge_core, "ClaudeProfile") as claude_profile, \
                    mock.patch.object(bridge_core, "vibe_settings", return_value={}), \
                    mock.patch.object(bridge_core, "credentials", return_value=("", "source")), \
                    mock.patch.object(bridge_core, "cached_catalogue", return_value={"models": []}), \
                    mock.patch.object(bridge_core, "provider_presentations", return_value=[]), \
                    mock.patch.object(bridge_core, "model_labels", return_value={}), \
                    mock.patch.object(vibe_profile, "VibeProfile") as vibe_profile_cls:
                claude_profile.return_value.active.return_value = False
                vibe_profile_cls.return_value.status.return_value = vibe_status
                result = bridge_core.inspect_state(root)
        for key, value in vibe_status.items():
            self.assertEqual(result[key], value)


if __name__ == "__main__":
    unittest.main()
