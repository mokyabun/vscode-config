# vscode-config

Personal VS Code and code-server settings for macOS and Linux.

```sh
./apply.sh --dry-run
./apply.sh
```

The script patches existing settings, creates a `.bak` backup, and installs
missing extensions. It does not remove unrelated settings or extensions.

Available profiles:

- `default`
- `node`
- `node-modern`
- `python`

Named profiles must already exist in VS Code:

```sh
./apply.sh --profile node-modern
```

code-server uses the `default` profile only.
