---
name: codex-plus
description: Install, configure, update, diagnose or remove codex-plus, which adds Transcript width and prompt suggestions (next-message predictions in the composer) to the official Codex desktop app (ChatGPT.app) on macOS. Use when the user asks to install codex-plus, change the transcript width feature, set the model for prompt suggestions, or fix them not appearing.
---

# codex-plus

codex-plus opens the unmodified ChatGPT.app through a launcher that adds two features: Settings → Appearance → Visual style → Transcript width, and Settings → General → Composer → Prompt suggestions. The full guide is https://github.com/congee949/codex-plus/blob/main/docs/guide.md.

## Install or update

Requires macOS, `/Applications/ChatGPT.app` with bundle id `com.openai.codex`, and Python 3.9+.

```bash
curl -fsSL https://raw.githubusercontent.com/congee949/codex-plus/main/install.sh | bash
```

This installs to `~/.codex-plus`, creates `~/Applications/Codex.app` and the launchd agent `local.codex-plus.watch`. Ask the user before passing `--no-agent` or `--no-app`. The features appear after ChatGPT is quit and reopened; quitting the app closes the user's windows, so let the user do it or ask first.

## Prompt-suggestion provider

Suggestions call `GET /models` and `POST /responses` on an OpenAI-compatible provider. Check it with:

```bash
python3 ~/.codex-plus/codex_launch.py provider
```

The JSON lists `available` providers from `~/.codex/config.toml` and the resolved `base_url` or an `error`. To choose one, run `python3 ~/.codex-plus/codex_launch.py provider <name>`. If none exists, ask the user for a base URL and key and add a `[model_providers.<name>]` table with `base_url` and `experimental_bearer_token` to `~/.codex/config.toml`. Never print the token back.

## Diagnose

- `python3 ~/.codex-plus/codex_launch.py status`: `via_launcher` false means ChatGPT was opened directly; `patches_current` false means it runs older code. Either way `python3 ~/.codex-plus/codex_launch.py launch --restart` reopens it (ask first, since it quits ChatGPT).
- `~/Library/Application Support/codex-plus/logs/launcher.log`: lines starting with `suggestion` give each turn's outcome or skip reason; `patches unavailable` means the injection points were not found in this app version.

## Remove

```bash
curl -fsSL https://raw.githubusercontent.com/congee949/codex-plus/main/install.sh | bash -s -- --uninstall
```

Add `--purge` only if the user also wants settings and logs deleted.
