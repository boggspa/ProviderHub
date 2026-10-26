# Separate Antigravity accounts

Antigravity's **Add account…** control accepts a separate home folder, for
example `~/.antigravity-work`. Start with an empty directory and sign in
through the official `agy` CLI. The CLI creates `.gemini/antigravity-cli`
inside that folder for its login, settings and history. Provider Hub stores
only the account label and folder path.

1. In Antigravity's provider settings, choose **Add account…** and select or
   create the new home folder.
2. Use **Sign in…** on that account row. Terminal opens the official CLI;
   open the Google link it prints, choose the intended account, and paste the
   code back into Terminal.
3. The CLI saves that login in the home's private `antigravity-oauth-token`
   file. An account home has no default macOS Keychain, so Provider Hub runs
   agy there with its remote-session marker (`SSH_CLIENT`), which makes agy
   choose its own file storage directly. Without it, every sign-in, check and
   turn raised **Keychain Not Found** and waited five seconds before agy fell
   back to the same file. If an older build shows that prompt, choose
   **Cancel**, never **Reset To Defaults**. Complete the CLI's onboarding.
4. Verify the account email in the CLI. Select that account in Provider Hub
   and **Save**; new requests and model discovery use that home. Existing
   turns finish on the account they started with.

**Check sign-in** runs the selected account's own `agy models` command.
A successful nonempty catalogue shows a positive model-access check; an
explicit request to sign in shows missing, while network errors or empty
responses remain unknown. This does not submit a model turn or inspect saved
tokens. Verify the account email in the CLI itself. Model pickers refresh on
opening so newly saved account rows appear without restarting Chat.

For Antigravity, the selected folder is an entire subprocess `HOME`, not an
existing `.gemini` folder. Extra accounts also receive private XDG config,
cache, data, state and runtime locations, and `GEMINI_CLI_HOME` for Gemini
tools launched within that environment. This does not sign Gemini CLI in:
its authentication remains a separate vendor-owned flow. The installed `agy`
executable is resolved before selecting a home. Automatic CLI updates are
disabled in these isolated processes; update the shared installation normally.

The default account keeps the normal home and existing login. Extra homes
cannot overlap one another or point into the default `.gemini` directory;
symlink aliases are resolved before validation. Retained CLI processes are
scoped to their account and cannot be resumed under another account.
Removing an account row leaves its folder and login intact.

## Account risk

Directory isolation separates local state; it does not establish permission
to access Antigravity through third-party software. Google's
[Antigravity terms](https://antigravity.google/terms), section 6, explicitly
restrict third-party access. Provider Hub's existing opt-in remains required.
Accounts are selected explicitly; there is no quota-driven account rotation
or fallback to a different login.

The official [authentication guide](https://antigravity.google/docs/install?tab=cli)
describes CLI-owned Google sign-in and local Keychain storage.
[Gemini CLI's isolation setting](https://geminicli.com/docs/cli/enterprise/)
is `GEMINI_CLI_HOME`, whose `.gemini` subdirectory must not be confused with
Antigravity's home-relative state.
