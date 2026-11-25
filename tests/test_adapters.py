import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_support  # noqa: F401 - stubs optional runtime packages before imports
from farmctl import device_inventory as di
from farmctl.integration_inventory import COMPONENTS
from farmctl.onboarding import PLACEHOLDERS, _expand_argv, validate_adapters, onboard_node
from farmctl.purge import purge_node

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = ROOT / "examples" / "integrations"


class MemoryIntegrations:
    def __init__(self, settings=None, events=None, root=None):
        self.settings = settings or {}
        self.events = events or {}
        self.root = Path(root or tempfile.mkdtemp()) / "integrations"
        self.root.mkdir(parents=True, exist_ok=True)
        self.logged = []

    def get_component(self, name):
        return self.settings.get(name)

    def read_events(self, name, limit=50):
        return self.events.get(name, [])[-limit:]

    def log_event(self, name, event, data=None):
        self.logged.append((name, event, data))
        return self.root / name / "events.jsonl"


class MockArchive:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write_snapshot(self, profile):
        device_id = di._device_id(profile)
        directory = self.root / device_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "latest.json"
        path.write_text("{}")
        return path


class AdapterExampleTests(unittest.TestCase):
    def test_component_catalog_has_all_expected_components(self):
        self.assertEqual(len(COMPONENTS), 53)
        self.assertEqual(len(set(COMPONENTS)), len(COMPONENTS))

    def test_all_example_argv_arrays_are_valid_and_expand(self):
        files = sorted(EXAMPLE_DIR.glob("*.json"))
        self.assertGreaterEqual(len(files), 11)
        profile_values = {key: f"mock-{key}" for key in PLACEHOLDERS}
        for path in files:
            with self.subTest(example=path.name):
                config = json.loads(path.read_text())
                self.assertIn("preflight_argv", config)
                self.assertIsInstance(config["preflight_argv"], list)
                self.assertTrue(config["preflight_argv"])
                self.assertTrue(all(isinstance(arg, str) and arg for arg in config["preflight_argv"]))
                arrays = [config["preflight_argv"]]
                if "onboard_argv" in config:
                    arrays.append(config["onboard_argv"])
                for argv in arrays:
                    self.assertIsInstance(argv, list)
                    self.assertTrue(argv)
                    self.assertTrue(all(isinstance(arg, str) and arg for arg in argv))
                    for arg in argv:
                        import re
                        unknown = set(re.findall(r"\{([^{}]+)\}", arg)) - set(PLACEHOLDERS)
                        self.assertFalse(unknown, f"unknown placeholders in {path.name}: {unknown}")
                    expanded = _expand_argv(argv, profile_values)
                    self.assertTrue(all("{hostname}" not in arg for arg in expanded))

    def test_every_onboard_argv_has_matching_preflight(self):
        for path in EXAMPLE_DIR.glob("*.json"):
            with self.subTest(example=path.name):
                config = json.loads(path.read_text())
                if "onboard_argv" in config:
                    self.assertIn("preflight_argv", config)
                    self.assertTrue(config["onboard_argv"])


class MockNodeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.profile = di.DeviceProfile(
            hostname="mock-node", connection_type="ssh", os_family="linux",
            manufacturer="Mock", model="MockCompute", device_type="server",
            architecture="x86_64", cpu_threads="8", memory_mb="16384",
            kernel_release="6.1-mock",
        )

    def test_selected_mock_node_preflight_passes_without_onboarding(self):
        preflight = [sys.executable, "-c", "raise SystemExit(0)"]
        onboard = [sys.executable, "-c", "raise SystemExit(99)"]
        store = MemoryIntegrations({"kubernetes": {"enabled": True,
            "config": {"preflight_argv": preflight, "onboard_argv": onboard}}})
        with patch("farmctl.onboarding.subprocess.run") as run:
            run.return_value.returncode = 0
            result = validate_adapters(self.profile, store, components=["kubernetes"])
        self.assertTrue(result["passed"])
        self.assertEqual(result["steps"][0]["status"], "passed")
        run.assert_called_once_with(preflight, capture_output=True, text=True, timeout=30.0, check=False)

    def test_onboarding_apply_blocks_all_actions_on_mock_preflight_failure(self):
        preflight = [sys.executable, "-c", "exit(0)"]
        onboard = [sys.executable, "-c", "exit(0)"]
        settings = {"kubernetes": {"enabled": True,
                    "config": {"preflight_argv": preflight, "onboard_argv": onboard}}}
        integrations = MemoryIntegrations(settings)
        with tempfile.TemporaryDirectory() as tmp, \
             patch("farmctl.onboarding.di.fingerprint_device", return_value=self.profile), \
             patch("farmctl.onboarding.subprocess.run") as run:
            run.return_value.returncode = 1
            with self.assertRaisesRegex(RuntimeError, "Preflight failed"):
                onboard_node("mock-node", "ssh", apply=True, store=MockArchive(tmp), integrations=integrations)
            run.assert_called_once()
            self.assertFalse((Path(tmp) / di._device_id(self.profile)).exists())

    def test_onboarding_dry_run_never_runs_external_commands(self):
        integrations = MemoryIntegrations({"kubernetes": {"enabled": True,
            "config": {"preflight_argv": [sys.executable, "-c", "exit(0)"],
                       "onboard_argv": [sys.executable, "-c", "exit(0)"]}}})
        with patch("farmctl.onboarding.di.fingerprint_device", return_value=self.profile), \
             patch("farmctl.onboarding.subprocess.run") as run:
            result = onboard_node("mock-node", "ssh", apply=False,
                                  store=MockArchive(tempfile.mkdtemp()), integrations=integrations)
        self.assertFalse(result["applied"])
        run.assert_not_called()


class PurgeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.profile = di.DeviceProfile(
            hostname="mock-node", connection_type="ssh", os_family="linux",
            manufacturer="Mock", model="MockCompute", device_type="server",
            architecture="x86_64", cpu_threads="8", memory_mb="16384",
            kernel_release="6.1-mock",
        )
        self.device_id = di._device_id(self.profile)
        self.preflight = [sys.executable, "-c", "exit(0)"]
        self.teardown = [sys.executable, "-c", "exit(0)"]
        self.settings = {"kubernetes": {"enabled": True, "config": {
            "preflight_argv": self.preflight, "teardown_argv": self.teardown}}}
        self.events = {"kubernetes": [{"event": "node.onboarding", "data": {
            "device_id": self.device_id, "status": "succeeded"}}]}

    def test_purge_defaults_to_plan_without_running_commands_or_deleting(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = MockArchive(tmp)
            snapshot = archive.write_snapshot(self.profile)
            integrations = MemoryIntegrations(self.settings, self.events, tmp)
            with patch("farmctl.purge.di.fingerprint_device", return_value=self.profile), \
                 patch("farmctl.purge.subprocess.run") as run:
                result = purge_node("mock-node", "ssh", store=archive, integrations=integrations)
            self.assertFalse(result["applied"])
            self.assertTrue(snapshot.exists())
            run.assert_not_called()

    def test_purge_requires_exact_device_id_before_any_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = MockArchive(tmp)
            archive.write_snapshot(self.profile)
            integrations = MemoryIntegrations(self.settings, self.events, tmp)
            with patch("farmctl.purge.di.fingerprint_device", return_value=self.profile), \
                 patch("farmctl.purge.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "Confirmation mismatch"):
                    purge_node("mock-node", "ssh", apply=True, confirm_device_id="wrong",
                               store=archive, integrations=integrations)
                run.assert_not_called()

    def test_purge_preflights_then_tears_down_then_removes_only_node_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = MockArchive(tmp)
            snapshot = archive.write_snapshot(self.profile)
            other_id = "f" * 16
            (Path(tmp) / other_id).mkdir()
            integrations = MemoryIntegrations(self.settings, self.events, tmp)
            with patch("farmctl.purge.di.fingerprint_device", return_value=self.profile), \
                 patch("farmctl.purge.subprocess.run") as run:
                run.return_value.returncode = 0
                result = purge_node("mock-node", "ssh", apply=True,
                                    confirm_device_id=self.device_id,
                                    store=archive, integrations=integrations)
            self.assertTrue(result["applied"])
            self.assertFalse(snapshot.parent.exists())
            self.assertTrue((Path(tmp) / other_id).exists())
            self.assertEqual([call.args[0] for call in run.call_args_list],
                             [self.preflight, self.teardown])
            self.assertTrue((integrations.root / "purge_audit.jsonl").exists())

    def test_purge_refuses_missing_teardown_before_any_external_action(self):
        settings = {"kubernetes": {"enabled": True, "config": {"preflight_argv": self.preflight}}}
        with tempfile.TemporaryDirectory() as tmp:
            integrations = MemoryIntegrations(settings, self.events, tmp)
            with patch("farmctl.purge.di.fingerprint_device", return_value=self.profile), \
                 patch("farmctl.purge.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "teardown_missing"):
                    purge_node("mock-node", "ssh", apply=True, confirm_device_id=self.device_id,
                               store=MockArchive(tmp), integrations=integrations)
                run.assert_not_called()

    def test_purge_preflight_failure_runs_no_teardown(self):
        with tempfile.TemporaryDirectory() as tmp:
            integrations = MemoryIntegrations(self.settings, self.events, tmp)
            with patch("farmctl.purge.di.fingerprint_device", return_value=self.profile), \
                 patch("farmctl.purge.subprocess.run") as run:
                run.return_value.returncode = 1
                with self.assertRaisesRegex(RuntimeError, "no teardowns were run"):
                    purge_node("mock-node", "ssh", apply=True, confirm_device_id=self.device_id,
                               store=MockArchive(tmp), integrations=integrations)
                run.assert_called_once_with(self.preflight, capture_output=True, text=True,
                                            timeout=30.0, check=False)
                audit = integrations.root / "purge_audit.jsonl"
                self.assertTrue(audit.exists())
                self.assertIn('"local_snapshot_removed": false', audit.read_text())


if __name__ == "__main__":
    unittest.main()
