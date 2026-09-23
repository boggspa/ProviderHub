# Contributing

Provider Hub is a native macOS menu bar app: a Swift front end plus a Python
worker that connects Claude Desktop and Codex / ChatGPT Desktop to a set of
model API providers.

## Read this first

[`AGENTS.md`](AGENTS.md) is the repository's operating doctrine, and it is not
decorative. This checkout is designed to be edited by several humans and agents
at once, and its rules — ownership markers, explicit-path staging, private
indexes, lease expiry — exist because each of them has been violated at cost.

Be precise about what is actually enforced. The hook in `.githooks/` **blocks
exactly one thing**: staging a path another live session has claimed. Bulk
staging, your own expired lease, and unclaimed dirty work are advisory only.
The rest of the doctrine binds by convention, not by code — so read it rather
than relying on the hook to catch you.

`CLAUDE.md` routes to it rather than restating it.

## Development setup

You need an Apple Silicon Mac, macOS 14 or newer, Xcode command line tools, and
[`uv`](https://docs.astral.sh/uv/).

```bash
uv sync                          # Python 3.13 plus cryptography
bash scripts/hooks_install.sh    # installs the pre-commit guard
bash Source/build.sh             # ad-hoc signed development build
```

`Source/build.sh` writes `Provider Hub Preview.app` beside the checkout and
ad-hoc signs it, which is enough to run locally. Developer ID signing and
notarization are maintainer-only steps and are not needed to contribute.

## Running the tests

```bash
uv run python -m unittest discover -s Source -p 'test_*.py'
uv run python -m pytest Source/ -q
uv run python scripts/test_work_guard.py
bash .githooks/pre-commit.test.sh
```

Use uv's CPython 3.13, not the system Python 3.9. Some gateway tests bind a
local socket and can fail inside a restricted sandbox — run them unsandboxed
before concluding they are broken.

`AGENTS.md` treats the `pytest` invocation as primary and the `unittest`
invocation as the fallback; both collect the same tests. Run the two
`work_guard` and hook suites after touching `scripts/work_guard.py` or
`.githooks/pre-commit`.

### Running the built app

`uv sync` creates `.venv`, but the app's Python discovery does **not** search
it: `Source/MistralBridge.swift` prefers a bundled runtime and then looks for a
system-wide interpreter. A source build with no `PROVIDER_HUB_PYTHON_RUNTIME`
therefore needs a discoverable Python 3.11 or newer on the machine, not just
the one in `.venv`. Treat `.venv` as the test environment.

## Where scratch files go

`.local-only/`, which is gitignored. Never the repo root: untracked files there
become permanent noise in `git status --porcelain`, the one signal the doctrine
tells you to trust before writing. See "Scratch goes in `.local-only/`" in
`AGENTS.md`.

Note that gitignored files are **not** captured by `work_guard.py` snapshots,
so treat `.local-only/` as disposable rather than as a safety net.

## Pull requests

- Keep changes scoped. The pre-commit hook blocks staging a path another live
  session has claimed, and reviewers benefit from a diff they can hold in mind.
- Stage by explicit path. `git add -A`, `git add .` and `git add -u` are
  prohibited by the doctrine, because this checkout holds other sessions' work.
- Add or update tests with behaviour changes. Provider adapters have
  deterministic protocol tests; follow the existing pattern.
- Do not commit build output: `.app` bundles, zips, `dist/`, or `work/`.
- Do not commit credentials, Keychain exports, or Apple certificates.

## Adding a provider

See [`PROVIDER-ADDITIONS.md`](PROVIDER-ADDITIONS.md) for the provider
contract, metadata sources, and qualification expectations.

## Conduct

Be direct, be kind, assume good faith, and prefer showing evidence over
asserting a conclusion.
