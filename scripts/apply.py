#!/usr/bin/env python3
"""Patch VS Code-compatible settings without taking ownership of the whole file."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


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


def merge_keybindings(base: list[Any], patch: list[Any]) -> list[Any]:
    result = list(base)
    for binding in patch:
        if binding not in result:
            result.append(binding)
    return result


def write_if_changed(path: Path, value: Any, dry_run: bool) -> None:
    current = load_jsonc(path, [] if isinstance(value, list) else {})
    if current == value:
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
    print(f"patched: {path}")


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


def install_extensions(command: str, profile: str, extensions: list[str], dry_run: bool) -> bool:
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
        message = list_result.stderr.strip() or list_result.stdout.strip()
        print(f"warn: cannot list {profile} extensions: {message}", file=sys.stderr)
        return False
    installed = {line.strip().lower() for line in list_result.stdout.splitlines() if line.strip()}
    missing = [extension for extension in extensions if extension.lower() not in installed]
    if not missing:
        print(f"ok:   {profile} extensions")
        return True
    success = True
    for extension in missing:
        if dry_run:
            print(f"would install ({profile}): {extension}")
            continue
        result = subprocess.run(
            [command, *profile_args, "--install-extension", extension], check=False
        )
        if result.returncode != 0:
            print(f"warn: failed to install ({profile}): {extension}", file=sys.stderr)
            success = False
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
    dry_run: bool,
    skip_extensions: bool,
) -> bool:
    base_dir = repo / "profiles" / "_base"
    profile_dir = repo / "profiles" / profile
    target_dir = user_dir if profile == "default" else user_dir / "profiles" / str(profile_id)

    settings = {}
    for source in (base_dir / "settings.jsonc", profile_dir / "settings.jsonc"):
        settings = deep_merge(settings, load_jsonc(source, {}))
    current_settings = load_jsonc(target_dir / "settings.json", {})
    write_if_changed(target_dir / "settings.json", deep_merge(current_settings, settings), dry_run)

    keybindings_source = profile_dir / "keybindings.jsonc"
    if keybindings_source.exists():
        current_keybindings = load_jsonc(target_dir / "keybindings.json", [])
        desired_keybindings = load_jsonc(keybindings_source, [])
        write_if_changed(
            target_dir / "keybindings.json",
            merge_keybindings(current_keybindings, desired_keybindings),
            dry_run,
        )

    if skip_extensions:
        return True
    extensions = read_extensions(base_dir / "extensions.txt", profile_dir / "extensions.txt")
    return install_extensions(command, profile, extensions, dry_run)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--target", required=True, choices=("vscode", "code-server"))
    parser.add_argument("--user-dir", required=True, type=Path)
    parser.add_argument("--command", required=True)
    parser.add_argument("--profile", default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-extensions", action="store_true")
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

    print(f"\n[{args.target}] {args.user_dir}")
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
        success = apply_profile(
            args.repo,
            args.user_dir,
            profile,
            profile_id,
            args.command,
            args.dry_run,
            args.skip_extensions,
        ) and success
    return 0 if success else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
