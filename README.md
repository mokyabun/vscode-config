# vscode-config

Personal VS Code and code-server settings for macOS and Linux.

```sh
curl -fsSL https://raw.githubusercontent.com/mokyabun/vscode-config/main/apply.sh | sh
```

Or from a local clone:

```sh
./apply.sh --dry-run
./apply.sh
```

The script patches existing settings, creates a `.bak` backup, and installs
missing extensions. It does not remove unrelated settings or extensions.
Managed state is stored in
`${XDG_STATE_HOME:-$HOME/.local/state}/vscode-config/state.json`.
It is used to remove obsolete managed settings without deleting user-owned values.

Available profiles:

- `default`
- `node`
- `node-modern`
- `python`

Named profiles must already exist in VS Code:

```sh
./apply.sh --profile node-modern
```

Missing profiles are skipped with a warning.

Remove extensions previously installed by this tool:

```sh
./apply.sh --prune-extensions
```

code-server uses the `default` profile only.
