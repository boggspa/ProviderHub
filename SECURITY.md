# Security policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue.

Use this repository's private vulnerability reporting feature once it is
enabled on the public remote; until then, contact the maintainer through the
address on their GitHub profile. Please do not file a public issue.

We aim to acknowledge reports within five working days, and will agree a
disclosure timeline with you before publishing anything. We will not ask you to
delay disclosure indefinitely.

## Supported versions

Only the current `main` branch receives security fixes. The single existing
tag, `v0.5.0`, predates this policy and is not maintained.

## Threat model — what this software handles

Provider Hub is a local macOS menu bar app. These are the properties that
matter when assessing a report:

- **Credentials.** Provider API keys are held in the macOS Keychain under the
  service named by `BridgeKeychainService` in the app's `Info.plist`. They are
  not written into this repository, and no code path deliberately persists them
  to disk or returns them in a response. The Swift front end reads that plist
  key and passes it to the Python worker as `MISTRAL_BRIDGE_KEYCHAIN_SERVICE`
  (`Source/MistralBridge.swift`), so both halves share one namespace; the
  worker's built-in default applies only when it runs standalone, outside the
  app.
- **Delegated CLIs.** For CLI-backed routes, Provider Hub spawns the vendor's
  own binary and lets that process own and refresh its login. It does not read,
  copy, cache or forward vendor credentials. See `Source/cli_routes.py`.
- **Delegated CLIs run with your privileges.** Those child processes can execute
  arbitrary code as your user, using whatever tools you have enabled inside
  them. Provider Hub does not sandbox them and cannot. Enabling a CLI route
  means trusting that vendor's binary.
- **Child environments are allowlisted.** `Source/cli_session.py` builds a
  minimal environment for spawned CLIs and deliberately strips provider
  variables, so one delegated agent cannot pick up another provider's key.
- **Local gateway.** The bridge listens on loopback only, on the port given by
  `BridgeDefaultPort`, and requires a per-installation token
  (`gateway_token` in `Source/bridge_core.py`).
  A request whose `Host` header is neither `127.0.0.1` nor `localhost` is
  refused with 403 before any other check. One route, `/_bridge/health`, is
  exempt from the token so a desktop client can probe liveness; it returns only
  the service name, product name and build identity, and is reachable only from
  this machine. Every other route, `/_bridge/status` included, requires the
  token.
- **The token is not a sandbox.** It separates authorised local callers from
  unauthorised ones, but it lives in a file this app controls, so another
  process already running as your user can read it. The gateway defends against
  other users and the network — not against code you have already chosen to run.
- **Encryption at rest.** Stored reasoning envelopes are sealed with Fernet
  using a key derived from a private per-installation token
  (`Source/responses_bridge.py`).

## Out of scope

- The security of third-party provider CLIs, provider APIs, or the desktop
  harnesses this app launches (Claude Desktop, Codex / ChatGPT Desktop).
- Actions a delegated agent takes with tools you enable inside that agent's own
  product.
- Content arriving from a provider, including prompt injection in model output.

## Release signing

Distribution builds are signed with an Apple Developer ID and notarized. The
signing identity and the `notarytool` Keychain profile exist **only on the
maintainer's machine** and are never committed. Source builds are ad-hoc
signed by `Source/build.sh` and are for development only.

No private key, provisioning profile, notarization credential or API key has
ever been committed to this repository. If you believe a credential has leaked,
please report it privately and immediately.
