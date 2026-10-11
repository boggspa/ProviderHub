# Provider Hub companion

A thin iOS 17+ companion for one paired Mac over a private LAN or Tailscale.
The Mac executes provider work and holds provider credentials. The phone lists
chats, watches a selected transcript, sends text, stops that chat, and answers
the exact pending approval. Text drafts are local to each chat and clear only
after a successful acknowledgement.

## Pair

1. In Provider Hub's remote settings, explicitly start the listener on the
   Mac's private interface IP and port. It is off by default. Wildcard and
   public interface binds are rejected.
2. Choose **Scan QR code** in the companion and scan the code in the Mac's
   Devices settings. Camera permission is requested only when opening the scanner.
   If the camera is unavailable or permission is off, copy the Mac's JSON pairing
   text into the companion and tap Connect. Only scan a code from your own Mac;
   the payload pins that Mac's public identity.
3. Compare the six-digit code shown on phone and Mac. Approve that identity
   on the Mac only when both codes match. An unknown device receives no chat
   events or command access before approval.
4. Reconnecting authenticates the stored identities and requests a fresh
   authoritative snapshot. Stop listening or revoke a phone in Mac settings.

The phone identity is stored in its device-only Keychain. The Mac identity and
device allowlist are atomically stored in its private `remote.json` file with
0600 permissions. No provider credential appears in this state or protocol.

## Build

The Swift core uses Apple CryptoKit, Network, Security and SwiftUI, with no
third-party Swift packages. Python uses the repository's pinned cryptography
dependency. Generate the iOS app target with the installed XcodeGen:

```sh
xcodegen generate --spec ios/ProviderHubCompanion/project.yml
```

Open the generated project, select a signing team, and run on your iPhone.
The target includes a local-network usage description. SwiftPM builds the
core and provides a macOS executable harness; it does not install an iOS app.
The camera usage description is also generated from `project.yml`. Keep
simulator ad-hoc signing enabled (`CODE_SIGN_IDENTITY=-`,
`CODE_SIGNING_ALLOWED=YES`) when running the companion: its device identity
uses Keychain and an unsigned simulator app cannot create that identity.

```sh
swift test --package-path Packages/ProviderHubRemote --scratch-path .local-only/remote-swift-build
uv run python -m unittest discover -s Source -p test_chat_remote.py
```

Tests exercise Python sockets, trust confirmation, revocation, reconnect,
exact approval binding, duplicate commands, and actual Swift Network TCP
handshake/encrypted snapshot/send/ack against Python, in addition to crypto
golden vectors. Test peers auto-confirm only inside test fixtures.

## Scope

This first version permits one active phone. No cloud relay, push notifications,
background execution or Bonjour discovery is included. QR generation/decoding
is verified with Core Image and Vision; live physical-camera scanning still
requires an on-device check. The camera stops when the scanner closes or the
companion goes into the background. Camera frames stay on the phone.
Reconnect refreshes state; unacknowledged messages remain drafts and are not
automatically resent. Request deduplication is bounded to the last 64 request
IDs per device during the Mac process lifetime, including socket reconnects;
it does not guarantee exactly-once execution across a Mac process restart.
Transcript projection sends the latest 30 entries and capped live text.
Transport flushes have a two-second upper bound before disconnecting a slow
peer. iPhone installation/signing and an on-device network trial require a
user's signing team and device. A signed Simulator app has completed actual
pairing, transcript subscription and a text-message round trip against the
production ChatHost using fixture data and a fake model transport.

## Protocol provenance

The cryptographic byte layout and golden vectors come from TaskWraith's
`taskwraith-e2ee-v1` implementation in AGBench
`src/shared/e2ee/{protocol,keys,keyschedule,cipher}.ts` and its
`crossImplVectors.test.ts` (2026-06-11). This implements the same X25519,
Ed25519 transcript authentication, HKDF-SHA256 and AES-256-GCM primitives;
the application envelope is `providerhub-remote-app-v1` over newline-framed
private TCP. It does not implement the TaskWraith relay protocol.
