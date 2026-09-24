#!/usr/bin/env python3
"""Patch VS Code-compatible settings without taking ownership of the whole file."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


STATE_VERSION = 1


def default_state_path() -> Path:
    override = os.environ.get("VSCODE_CONFIG_STATE_FILE")
    if override:
        return Path(override).expanduser()
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "vscode-config" / "state.json"


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        if path.exists():
            loaded = load_jsonc(path, {})
            if not isinstance(loaded, dict):
                raise RuntimeError(f"invalid state file: {path}")
            version = loaded.get("version")
            if version != STATE_VERSION:
                raise RuntimeError(f"unsupported state version {version!r} in {path}")
            self.data = loaded
        else:
            self.data: dict[str, Any] = {"version": STATE_VERSION, "targets": {}}

    def profile(self, target: str, user_dir: Path, profile: str) -> dict[str, Any]:
        targets = self.data.setdefault("targets", {})
        target_state = targets.setdefault(target, {})
        instances = target_state.setdefault("instances", {})
        instance = instances.setdefault(str(user_dir.expanduser().resolve()), {})
        profiles = instance.setdefault("profiles", {})
        return profiles.setdefault(profile, {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)


def strip_jsonc(text: str) -> str:
    """Remove JSONC comments and trailing commas while preserving string content."""
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False

    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
        elif char == "/" and next_char == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
        elif char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(text) and text[index : index + 2] != "*/":
                if text[index] in "\r\n":
                    output.append(text[index])
                index += 1
            index += 2
        else:
            output.append(char)
            index += 1

    text = "".join(output)
    output = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def load_jsonc(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot parse {path}: {error}") from error


def deep_merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        result = dict(base)
        for key, value in patch.items():
            result[key] = deep_merge(result[key], value) if key in result else value
        return result
    return patch


def value_hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def encode_pointer(parts: tuple[str, ...]) -> str:
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def decode_pointer(pointer: str) -> tuple[str, ...]:
    if not pointer.startswith("/"):
        raise RuntimeError(f"invalid managed setting path: {pointer}")
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/"))


def flatten_leaves(value: Any, prefix: tuple[str, ...] = ()) -> dict[str, Any]:
    if isinstance(value, dict) and value:
        leaves: dict[str, Any] = {}
        for key, child in value.items():
            leaves.update(flatten_leaves(child, (*prefix, key)))
        return leaves
    return {encode_pointer(prefix): value} if prefix else {}


def get_path(value: dict[str, Any], parts: tuple[str, ...]) -> tuple[bool, Any]:
    current: Any = value
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def set_path(value: dict[str, Any], parts: tuple[str, ...], desired: Any) -> None:
    current = value
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = copy.deepcopy(desired)


def remove_path(value: dict[str, Any], parts: tuple[str, ...]) -> None:
    parents: list[tuple[dict[str, Any], str]] = []
    current: Any = value
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        parents.append((current, part))
        current = current[part]
    if not isinstance(current, dict):
        return
    current.pop(parts[-1], None)
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            parent.pop(key, None)
        else:
            break


def write_if_changed(path: Path, value: Any, dry_run: bool, verbose: bool) -> None:
    current = load_jsonc(path, [] if isinstance(value, list) else {})
    if current == value:
        if verbose:
            print(f"ok:   {path}")
        return
    if dry_run:
        print(f"would patch: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    if verbose:
        print(f"patched: {path}")


def reconcile_settings(
    path: Path,
    desired: dict[str, Any],
    profile_state: dict[str, Any],
    dry_run: bool,
    verbose: bool,
) -> None:
    current = load_jsonc(path, {})
    if not isinstance(current, dict):
        raise RuntimeError(f"settings must contain a JSON object: {path}")

    previous = profile_state.get("settings", {})
    if not isinstance(previous, dict):
        previous = {}
    desired_leaves = flatten_leaves(desired)
    result = copy.deepcopy(current)

    stale = set(previous) - set(desired_leaves)
    for pointer in sorted(stale, key=lambda item: len(decode_pointer(item)), reverse=True):
        parts = decode_pointer(pointer)
        exists, current_value = get_path(result, parts)
        if not exists:
            continue
        if value_hash(current_value) == previous[pointer]:
            remove_path(result, parts)
        else:
            print(f"warn: preserving modified setting removed from config: {pointer}", file=sys.stderr)

    for pointer, desired_value in desired_leaves.items():
        set_path(result, decode_pointer(pointer), desired_value)

    write_if_changed(path, result, dry_run, verbose)
    if not dry_run:
        profile_state["settings"] = {
            pointer: value_hash(value) for pointer, value in sorted(desired_leaves.items())
        }


def keybinding_identity(binding: Any) -> str:
    if not isinstance(binding, dict):
        return value_hash(binding)
    return value_hash({key: binding.get(key) for key in ("key", "command", "when")})


def reconcile_keybindings(
    path: Path,
    desired: list[Any],
    profile_state: dict[str, Any],
    dry_run: bool,
    verbose: bool,
) -> None:
    current = load_jsonc(path, [])
    if not isinstance(current, list):
        raise RuntimeError(f"keybindings must contain a JSON array: {path}")

    previous_raw = profile_state.get("keybindings", [])
    previous = [
        item
        for item in previous_raw
        if isinstance(item, dict) and isinstance(item.get("identity"), str) and isinstance(item.get("hash"), str)
    ] if isinstance(previous_raw, list) else []
    desired_meta = [
        {"identity": keybinding_identity(binding), "hash": value_hash(binding)} for binding in desired
    ]
    desired_identities = {item["identity"] for item in desired_meta}
    desired_hashes = {item["hash"] for item in desired_meta}
    previous_hashes = {item["hash"] for item in previous}
    previous_identities = {item["identity"] for item in previous}
    warned: set[str] = set()
    result: list[Any] = []

    for binding in current:
        binding_hash = value_hash(binding)
        identity = keybinding_identity(binding)
        if binding_hash in previous_hashes:
            if binding_hash in desired_hashes:
                result.append(binding)
            continue
        if identity in previous_identities and identity in desired_identities:
            continue
        if identity in previous_identities and identity not in desired_identities and identity not in warned:
            print("warn: preserving modified keybinding removed from config", file=sys.stderr)
            warned.add(identity)
        result.append(binding)

    existing_hashes = {value_hash(binding) for binding in result}
    for binding, metadata in zip(desired, desired_meta):
        if metadata["hash"] not in existing_hashes:
            result.append(binding)
            existing_hashes.add(metadata["hash"])

    write_if_changed(path, result, dry_run, verbose)
    if not dry_run:
        profile_state["keybindings"] = desired_meta


def walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def profile_map_from_menu(user_dir: Path) -> dict[str, str]:
    storage = load_jsonc(user_dir / "globalStorage" / "storage.json", {})
    profiles: dict[str, str] = {}
    prefix = "workbench.profiles.actions.profileEntry."
    for item in walk_dicts(storage):
        item_id = item.get("id")
        label = item.get("label")
        if isinstance(item_id, str) and item_id.startswith(prefix) and isinstance(label, str):
            profile_id = item_id[len(prefix) :]
            if profile_id != "__default__profile__":
                profiles[label] = profile_id
    return profiles


def profile_map_from_sync(user_dir: Path) -> dict[str, str]:
    path = user_dir / "sync" / "profiles" / "lastSyncprofiles.json"
    outer = load_jsonc(path, {})
    sync_data = outer.get("syncData") if isinstance(outer, dict) else None
    content = sync_data.get("content") if isinstance(sync_data, dict) else None
    if not isinstance(content, str):
        return {}
    try:
        profiles = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return {
        item["name"]: item["id"]
        for item in profiles
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("id"), str)
    }


def resolve_profiles(user_dir: Path) -> dict[str, str]:
    profiles = profile_map_from_sync(user_dir)
    profiles.update(profile_map_from_menu(user_dir))
    return profiles


def read_extensions(*paths: Path) -> list[str]:
    extensions: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line and line.lower() not in {item.lower() for item in extensions}:
                extensions.append(line)
    return extensions


def command_exists(command: str) -> bool:
    return Path(command).is_file() or shutil.which(command) is not None


def install_extensions(
    command: str,
    profile: str,
    extensions: list[str],
    profile_state: dict[str, Any],
    dry_run: bool,
    prune: bool,
    verbose: bool,
) -> bool:
    profile_args = [] if profile == "default" else ["--profile", profile]
    if not command_exists(command):
        print(f"warn: extension command not found: {command}", file=sys.stderr)
        return False
    list_result = subprocess.run(
        [command, *profile_args, "--list-extensions"],
        check=False,
        capture_output=True,
        text=True,
    )
    if list_result.returncode != 0:
        print(f"warn: cannot list {profile} extensions", file=sys.stderr)
        if verbose:
            message = list_result.stderr.strip() or list_result.stdout.strip()
            if message:
                print(message, file=sys.stderr)
        return False
    installed = {line.strip().lower() for line in list_result.stdout.splitlines() if line.strip()}
    desired = {extension.lower(): extension for extension in extensions}
    owned_raw = profile_state.get("installedExtensions", [])
    owned = {item.lower() for item in owned_raw if isinstance(item, str)} if isinstance(owned_raw, list) else set()
    success = True

    for extension_key, extension in desired.items():
        if extension_key in installed:
            continue
        if dry_run:
            print(f"would install ({profile}): {extension}")
            continue
        result = subprocess.run(
            [command, *profile_args, "--install-extension", extension],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"warn: failed to install ({profile}): {extension}", file=sys.stderr)
            if verbose:
                message = result.stderr.strip() or result.stdout.strip()
                if message:
                    print(message, file=sys.stderr)
            success = False
        else:
            owned.add(extension_key)
            installed.add(extension_key)
            if verbose:
                print(f"installed ({profile}): {extension}")

    for extension_key in sorted(owned - set(desired)):
        if extension_key not in installed:
            owned.discard(extension_key)
            continue
        if not prune:
            print(
                f"warn: managed extension is no longer configured ({profile}): {extension_key}; "
                "use --prune-extensions to remove it",
                file=sys.stderr,
            )
            continue
        if dry_run:
            print(f"would uninstall ({profile}): {extension_key}")
            continue
        result = subprocess.run(
            [command, *profile_args, "--uninstall-extension", extension_key],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"warn: failed to uninstall ({profile}): {extension_key}", file=sys.stderr)
            if verbose:
                message = result.stderr.strip() or result.stdout.strip()
                if message:
                    print(message, file=sys.stderr)
            success = False
        else:
            owned.discard(extension_key)
            if verbose:
                print(f"uninstalled ({profile}): {extension_key}")

    if not dry_run:
        profile_state["installedExtensions"] = sorted(owned)
    if verbose and success and not (set(desired) - installed) and not (owned - set(desired)):
        print(f"ok:   {profile} extensions")
    return success


def configured_profiles(repo: Path) -> list[str]:
    return sorted(
        path.name
        for path in (repo / "profiles").iterdir()
        if path.is_dir() and not path.name.startswith("_")
    )


def apply_profile(
    repo: Path,
    user_dir: Path,
    profile: str,
    profile_id: str | None,
    command: str,
    profile_state: dict[str, Any],
    dry_run: bool,
    skip_extensions: bool,
    prune_extensions: bool,
    verbose: bool,
) -> bool:
    base_dir = repo / "profiles" / "_base"
    profile_dir = repo / "profiles" / profile
    target_dir = user_dir if profile == "default" else user_dir / "profiles" / str(profile_id)

    settings = {}
    for source in (base_dir / "settings.jsonc", profile_dir / "settings.jsonc"):
        settings = deep_merge(settings, load_jsonc(source, {}))
    if not isinstance(settings, dict):
        raise RuntimeError(f"settings sources must contain JSON objects: {profile_dir}")
    reconcile_settings(
        target_dir / "settings.json",
        settings,
        profile_state,
        dry_run,
        verbose,
    )

    keybindings_source = profile_dir / "keybindings.jsonc"
    desired_keybindings = load_jsonc(keybindings_source, [])
    if not isinstance(desired_keybindings, list):
        raise RuntimeError(f"keybindings source must contain a JSON array: {keybindings_source}")
    reconcile_keybindings(
        target_dir / "keybindings.json",
        desired_keybindings,
        profile_state,
        dry_run,
        verbose,
    )

    if skip_extensions:
        return True
    extensions = read_extensions(base_dir / "extensions.txt", profile_dir / "extensions.txt")
    return install_extensions(
        command,
        profile,
        extensions,
        profile_state,
        dry_run,
        prune_extensions,
        verbose,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--target", required=True, choices=("vscode", "code-server"))
    parser.add_argument("--user-dir", required=True, type=Path)
    parser.add_argument("--command", required=True)
    parser.add_argument("--profile", default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-extensions", action="store_true")
    parser.add_argument("--prune-extensions", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--state-file", type=Path, default=default_state_path())
    args = parser.parse_args()

    available = configured_profiles(args.repo)
    requested = available if args.profile == "all" else [args.profile]
    unknown = [profile for profile in requested if profile not in available]
    if unknown:
        print(f"error: unknown profile: {', '.join(unknown)}", file=sys.stderr)
        return 2

    if args.target == "code-server":
        if args.profile not in ("all", "default"):
            print("error: code-server supports only the default profile", file=sys.stderr)
            return 2
        requested = ["default"]

    if args.verbose or args.dry_run:
        print(f"\n[{args.target}] {args.user_dir}")
    state_store = StateStore(args.state_file)
    profile_ids = resolve_profiles(args.user_dir) if args.target == "vscode" else {}
    success = True
    for profile in requested:
        profile_id = None if profile == "default" else profile_ids.get(profile)
        if profile != "default" and profile_id is None:
            print(
                f"warn: VS Code profile '{profile}' was not found; skipping",
                file=sys.stderr,
            )
            continue
        profile_state = state_store.profile(args.target, args.user_dir, profile)
        profile_success = apply_profile(
            args.repo,
            args.user_dir,
            profile,
            profile_id,
            args.command,
            profile_state,
            args.dry_run,
            args.skip_extensions,
            args.prune_extensions,
            args.verbose,
        )
        if not args.dry_run:
            state_store.save()
        success = profile_success and success
    return 0 if success else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
