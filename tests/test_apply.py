from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vscode_config_apply", ROOT / "scripts" / "apply.py")
assert SPEC and SPEC.loader
apply = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apply)


class SettingsStateTest(unittest.TestCase):
    def test_removes_unchanged_managed_keys_and_preserves_unmanaged_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            settings_path.write_text(
                json.dumps({"search.exclude": {"**/custom": True}, "user.setting": 1})
            )
            state: dict[str, object] = {}

            apply.reconcile_settings(
                settings_path,
                {"search.exclude": {"**/dist": True}},
                state,
                dry_run=False,
                verbose=False,
            )
            first = json.loads(settings_path.read_text())
            self.assertEqual(first["search.exclude"], {"**/custom": True, "**/dist": True})

            apply.reconcile_settings(settings_path, {}, state, dry_run=False, verbose=False)
            second = json.loads(settings_path.read_text())
            self.assertEqual(second, {"search.exclude": {"**/custom": True}, "user.setting": 1})

    def test_preserves_manually_changed_key_when_removed_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            settings_path.write_text(json.dumps({"editor.fontSize": 12}))
            state: dict[str, object] = {}
            apply.reconcile_settings(
                settings_path,
                {"editor.fontSize": 12},
                state,
                dry_run=False,
                verbose=False,
            )
            settings_path.write_text(json.dumps({"editor.fontSize": 15}))

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                apply.reconcile_settings(settings_path, {}, state, dry_run=False, verbose=False)

            self.assertEqual(json.loads(settings_path.read_text()), {"editor.fontSize": 15})
            self.assertIn("preserving modified setting", stderr.getvalue())


class KeybindingStateTest(unittest.TestCase):
    def test_replaces_managed_binding_and_preserves_user_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keybindings.json"
            user_binding = {"key": "cmd+k", "command": "user.command"}
            old_binding = {"key": "shift+enter", "command": "terminal.send", "args": {"text": "old"}}
            new_binding = {"key": "shift+enter", "command": "terminal.send", "args": {"text": "new"}}
            path.write_text(json.dumps([user_binding]))
            state: dict[str, object] = {}

            apply.reconcile_keybindings(path, [old_binding], state, dry_run=False, verbose=False)
            apply.reconcile_keybindings(path, [new_binding], state, dry_run=False, verbose=False)
            self.assertEqual(json.loads(path.read_text()), [user_binding, new_binding])

            apply.reconcile_keybindings(path, [], state, dry_run=False, verbose=False)
            self.assertEqual(json.loads(path.read_text()), [user_binding])


class ExtensionStateTest(unittest.TestCase):
    def test_only_prunes_extensions_installed_by_this_tool(self) -> None:
        state: dict[str, object] = {}
        list_empty = SimpleNamespace(returncode=0, stdout="", stderr="deprecated warning")
        install_ok = SimpleNamespace(returncode=0, stdout="installed", stderr="deprecated warning")

        with patch.object(apply, "command_exists", return_value=True), patch.object(
            apply.subprocess, "run", side_effect=[list_empty, install_ok]
        ):
            self.assertTrue(
                apply.install_extensions(
                    "code", "default", ["example.extension"], state, False, False, False
                )
            )
        self.assertEqual(state["installedExtensions"], ["example.extension"])

        list_installed = SimpleNamespace(returncode=0, stdout="example.extension\n", stderr="")
        stderr = io.StringIO()
        with patch.object(apply, "command_exists", return_value=True), patch.object(
            apply.subprocess, "run", return_value=list_installed
        ), redirect_stderr(stderr):
            self.assertTrue(
                apply.install_extensions("code", "default", [], state, False, False, False)
            )
        self.assertIn("use --prune-extensions", stderr.getvalue())
        self.assertEqual(state["installedExtensions"], ["example.extension"])

        uninstall_ok = SimpleNamespace(returncode=0, stdout="uninstalled", stderr="")
        with patch.object(apply, "command_exists", return_value=True), patch.object(
            apply.subprocess, "run", side_effect=[list_installed, uninstall_ok]
        ):
            self.assertTrue(
                apply.install_extensions("code", "default", [], state, False, True, False)
            )
        self.assertEqual(state["installedExtensions"], [])


class StateStoreTest(unittest.TestCase):
    def test_persists_state_by_target_user_dir_and_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            user_dir = Path(directory) / "User"
            store = apply.StateStore(state_path)
            store.profile("vscode", user_dir, "default")["settings"] = {"/a": "sha256:test"}
            store.save()

            loaded = apply.StateStore(state_path)
            profile = loaded.profile("vscode", user_dir, "default")
            self.assertEqual(profile["settings"], {"/a": "sha256:test"})
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
