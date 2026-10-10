# Re-signed build

`codex_tw.py run` writes Transcript width and prompt suggestions into `app.asar` and installs a re-signed copy of the app. The [launcher](guide.md#打开方式) is the default; this mode is kept for running the features however the app is opened.

## Costs

- App tools stop working. The app server rejects tool peers whose code signature is not OpenAI's (`dynamic_app_tools_peer_rejected reason=untrusted-code-signing-identity` in the app log).
- The built-in updater cannot install over a locally signed bundle (Team ID mismatch), so a patched build turns off Sparkle's automatic checks and installs, both in Info.plist and in the `com.openai.codex` user defaults. "Check for Updates…" fails on a patched build. `restore` turns the updater back on.
- The restricted entitlements are dropped: keychain access groups, APS and team id.
- The smoke test launches the staged app against your real profile, because the runtime ignores `--user-data-dir`.
- If the bundle swap fails with a permissions error, give the Python app App Management permission (System Settings → Privacy & Security → App Management).

## Build and install

`run [--force] [--no-install] [--wait-hours N]`:

1. Reads the official Sparkle appcast (`updates.oaistatic.com/codex/app/appcast`). When there is a newer build, it downloads it and verifies the EdDSA signature against the pinned `SUPublicEDKey`. It also checks that the bundle is intact, OpenAI-signed and has the expected build number.
2. **Native-feature scan**: looks in the locale catalogs and settings messages for an official width setting.
   - High-confidence hit: installs the pure official build, sends a notification, and removes the launchd job.
   - Suspected hit: installs the pure official build and notifies you. Run `codex_tw.py ack` if it turns out to be unrelated. Each scan is saved to `scans/<build>.json`.
3. Finds the injection points (see [implementation-notes.md](implementation-notes.md)). Anything ambiguous aborts the run.
4. Rebuilds `app.asar`, updates the Info.plist `ElectronAsarIntegrity` hash, writes the matching digest into `Codex Framework`'s `__asar_integrity` section, and re-signs the helpers, then the framework, then the app with your Apple Development identity (see [Signing and the keychain](#signing-and-the-keychain)).
5. Waits for ChatGPT to quit, launches the staged copy, and checks that it doesn't crash, the width applies and the patched settings chunk loads.
6. Atomically swaps the whole bundle. The previous app is kept under `backups/`.

Any failure leaves the installed app untouched and posts a notification. The patch id is a hash of the assets plus `INJECT_VERSION` in `codex_tw.py`, so editing `assets/` triggers a rebuild on the next run.

`install-agent` runs `run --wait-hours 3` daily at 04:30 through the launchd job `local.codex-plus.rebuild`; `uninstall-agent` removes it. `status` shows the installed build and cache state.

## Signing and the keychain

The app keeps its storage key in the login keychain as `Codex Storage Key`. macOS checks two permissions on it, and both have to stay stable across builds to avoid a password prompt after every update:

- **Trusted-application ACL**: matched against the app's designated requirement, which names the bundle id and the signing certificate's common name.
- **Partition list**: matched by `teamid:` for code signed with an Apple-issued certificate, and by cdhash otherwise. Each patched build has a new cdhash, so ad-hoc or self-signed builds ask again after every update.

`run` therefore signs with the newest valid Apple Development identity of the team set as `sign_team` in `~/Library/Application Support/codex-plus/config.toml` (`sign_team()` in `codex_tw.py`), for example `sign_team = "ABCDE12345"`. Without it, `run` stops before signing. After the first install with this identity, answer both keychain prompts with "Always Allow"; later builds keep the same requirement and team id. When the certificate expires, renew it in Xcode → Settings → Accounts. Until then `run` stops before signing and leaves the installed app untouched.
