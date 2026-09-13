#!/usr/bin/env bash
# Self-test for the Mistral Bridge pre-commit hook.
#
# Synthesizes a temp git repo, stamps markers with various pid/expiry/owner
# shapes, and asserts BLOCK vs ADVISE vs silent. Pruned from the TaskWraith
# repo's test suite: no public-remote or monolith tests (those sections were
# dropped from this hook).
#
#   bash .githooks/pre-commit.test.sh
set -euo pipefail

hook_source="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pre-commit"
suite_root="$(mktemp -d "${TMPDIR:-/tmp}/mistral-bridge-hook.XXXXXX")"
foreign_pid=""
assertions=0
failures=0

cleanup() {
  if [ -n "$foreign_pid" ]; then
    kill "$foreign_pid" 2>/dev/null || true
    wait "$foreign_pid" 2>/dev/null || true
  fi
  rm -rf "$suite_root"
}
trap cleanup EXIT

# A long-lived foreign process so we can record a "live" pid in a marker.
sleep 300 &
foreign_pid=$!

# ── helpers ─────────────────────────────────────────────────────────────────

new_repo() {
  local name="$1"
  local repo="$suite_root/$name"
  mkdir -p "$repo/Source" "$repo/.githooks"
  git -C "$repo" init -q
  git -C "$repo" config user.name 'hook test'
  git -C "$repo" config user.email 'hook-test@mistral-bridge.invalid'
  git -C "$repo" config commit.gpgsign false
  git -C "$repo" config core.hooksPath /dev/null

  printf 'baseline\n' > "$repo/Source/gateway.py"
  printf 'baseline\n' > "$repo/Source/protocol.py"
  git -C "$repo" add Source/gateway.py Source/protocol.py
  git -C "$repo" commit -qm baseline

  cp "$hook_source" "$repo/.githooks/pre-commit"
  chmod +x "$repo/.githooks/pre-commit"
  git -C "$repo" config core.hooksPath .githooks
  printf '%s' "$repo"
}

stage_change() {
  local repo="$1" path="$2"
  printf 'staged change\n' >> "$repo/$path"
  git -C "$repo" add -- "$path"
}

# BSD date uses -v, GNU date uses -d. Probe and pick; both produce UTC ISO-8601.
iso_shift_minutes() {
  local mins="$1"
  date -v+${mins}M -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
    date -u -d "+${mins} minutes" +%Y-%m-%dT%H:%M:%SZ
}
iso_shift_minutes_ago() {
  local mins="$1"
  date -v-${mins}M -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
    date -u -d "-${mins} minutes" +%Y-%m-%dT%H:%M:%SZ
}

write_marker() {
  local repo="$1" name="$2" pid="$3" started="$4" expires="$5" path1="$6"
  cat > "$repo/$name" <<EOF
---
agent: test-marker
pid: $pid
started: $started
expires: $expires
paths:
  - $path1
---
test marker
EOF
}

write_owner_marker() {
  local repo="$1" name="$2" owner="$3" started="$4" expires="$5" path1="$6"
  cat > "$repo/$name" <<EOF
---
agent: test-seat
lockOwnerId: $owner
started: $started
expires: $expires
paths:
  - $path1
---
owner-id marker
EOF
}

write_contribution_marker() {
  local repo="$1" owner="$2" started="$3" expires="$4" path1="$5"
  local name=".WORK-IN-PROGRESS-taskwraith-contribution-$(printf '%s' "$owner" | shasum -a 256 | awk '{print $1}').md"
  cat > "$repo/$name" <<EOF
---
agent: taskwraith-contribution
lockOwnerId: $owner
started: $started
expires: $expires
paths:
  - $path1
---
Host-maintained intent claim for captured edits.
EOF
  printf '%s' "$name"
}

commit_capture() {
  local repo="$1"
  git -C "$repo" commit -q --allow-empty-message -m '' 2>&1 || true
}

# Run the hook directly and capture its exit + output.
# Actually stage and commit, capturing the hook's decision.
try_commit() {
  local repo="$1"
  local rc out
  out="$(cd "$repo" && git commit -q -m test 2>&1)" && rc=0 || rc=$?
  printf 'rc=%s\n%s' "$rc" "$out"
}

assert_blocked() {
  local desc="$1" result="$2"
  assertions=$((assertions + 1))
  if ! printf '%s' "$result" | grep -q 'BLOCKED'; then
    echo "FAIL: $desc — expected BLOCKED"
    printf '%s\n' "$result" | head -5
    failures=$((failures + 1))
  fi
}

assert_not_blocked() {
  local desc="$1" result="$2"
  assertions=$((assertions + 1))
  if printf '%s' "$result" | grep -q 'BLOCKED'; then
    echo "FAIL: $desc — expected NOT blocked"
    printf '%s\n' "$result" | head -5
    failures=$((failures + 1))
  fi
}

assert_advise() {
  local desc="$1" result="$2" pattern="$3"
  assertions=$((assertions + 1))
  if ! printf '%s' "$result" | grep -q "$pattern"; then
    echo "FAIL: $desc — expected advice matching '$pattern'"
    printf '%s\n' "$result" | head -10
    failures=$((failures + 1))
  fi
}

assert_silent() {
  local desc="$1" result="$2"
  assertions=$((assertions + 1))
  if [ -n "$result" ]; then
    echo "FAIL: $desc — expected silent, got:"
    printf '%s\n' "$result" | head -5
    failures=$((failures + 1))
  fi
}

# ── tests ───────────────────────────────────────────────────────────────────

echo "Running pre-commit hook tests..."

# 1. Live foreign claim blocks staging its path.
repo="$(new_repo 'live-block')"
stage_change "$repo" Source/gateway.py
started="$(iso_shift_minutes_ago 2)"
expires="$(iso_shift_minutes 10)"
write_marker "$repo" ".WORK-IN-PROGRESS-foreign.md" "$foreign_pid" "$started" "$expires" "Source/gateway.py"
result="$(try_commit "$repo")"
assert_blocked "live foreign claim blocks" "$result"
git -C "$repo" reset -q HEAD -- Source/gateway.py 2>/dev/null || true

# 2. TW_ALLOW_CLAIMED overrides the block.
repo2="$(new_repo 'override')"
stage_change "$repo2" Source/gateway.py
write_marker "$repo2" ".WORK-IN-PROGRESS-foreign.md" "$foreign_pid" "$started" "$expires" "Source/gateway.py"
result="$(cd "$repo2" && TW_ALLOW_CLAIMED=1 git commit -q -m test 2>&1)" || true
assert_not_blocked "TW_ALLOW_CLAIMED overrides" "$result"
assert_advise "override logs override" "$result" "override:"

# 3. Expired claim does NOT block (decayed → advisory).
repo3="$(new_repo 'expired')"
stage_change "$repo3" Source/gateway.py
exp_started="$(iso_shift_minutes_ago 30)"
exp_expires="$(iso_shift_minutes_ago 20)"
write_marker "$repo3" ".WORK-IN-PROGRESS-decayed.md" "$foreign_pid" "$exp_started" "$exp_expires" "Source/gateway.py"
result="$(try_commit "$repo3")"
assert_not_blocked "expired claim does not block" "$result"
git -C "$repo3" reset -q HEAD -- Source/gateway.py 2>/dev/null || true

# 4. Live claim on a DIFFERENT path does not block staging an unrelated path.
repo4="$(new_repo 'unrelated')"
stage_change "$repo4" Source/protocol.py
write_marker "$repo4" ".WORK-IN-PROGRESS-other.md" "$foreign_pid" "$started" "$expires" "Source/gateway.py"
result="$(try_commit "$repo4")"
assert_not_blocked "unrelated path not blocked" "$result"

# 5. Owner-id claim with matching env var is "own" → does not block.
repo5="$(new_repo 'own-owner')"
stage_change "$repo5" Source/gateway.py
owner_id="abc12345-1234-1234-1234-123456789abc"
write_owner_marker "$repo5" ".WORK-IN-PROGRESS-mine.md" "$owner_id" "$started" "$expires" "Source/gateway.py"
result="$(cd "$repo5" && TASKWRAITH_LOCK_OWNER_ID="$owner_id" git commit -q -m test 2>&1)" || true
assert_not_blocked "own owner-id claim does not block" "$result"

# 6. Foreign owner-id claim (no matching env) blocks.
repo6="$(new_repo 'foreign-owner')"
stage_change "$repo6" Source/gateway.py
owner_id="def67890-5678-5678-5678-567890abcdef"
write_owner_marker "$repo6" ".WORK-IN-PROGRESS-foreign-owner.md" "$owner_id" "$started" "$expires" "Source/gateway.py"
result="$(try_commit "$repo6")"
assert_blocked "foreign owner-id claim blocks" "$result"
git -C "$repo6" reset -q HEAD -- Source/gateway.py 2>/dev/null || true

# 7. Contribution marker blocks staging its claimed path.
repo7="$(new_repo 'contribution-block')"
stage_change "$repo7" Source/gateway.py
contrib_owner="11111111-2222-3333-4444-555555555555"
contrib_name="$(write_contribution_marker "$repo7" "$contrib_owner" "$started" "$expires" "Source/gateway.py")"
result="$(try_commit "$repo7")"
assert_blocked "contribution marker blocks" "$result"
git -C "$repo7" reset -q HEAD -- Source/gateway.py 2>/dev/null || true

# 8. Contribution marker with matching owner env does not block.
repo8="$(new_repo 'contribution-own')"
stage_change "$repo8" Source/gateway.py
contrib_owner="22222222-3333-4444-5555-666666666666"
write_contribution_marker "$repo8" "$contrib_owner" "$started" "$expires" "Source/gateway.py"
result="$(cd "$repo8" && TASKWRAITH_LOCK_OWNER_ID="$contrib_owner" git commit -q -m test 2>&1)" || true
assert_not_blocked "own contribution marker does not block" "$result"

# 9. No marker at all → advisory about no claim, but NOT blocked.
repo9="$(new_repo 'no-marker')"
stage_change "$repo9" Source/gateway.py
result="$(try_commit "$repo9")"
assert_not_blocked "no marker does not block" "$result"

# 10. Expired marker standing → advisory about decay.
repo10="$(new_repo 'decay-advise')"
stage_change "$repo10" Source/protocol.py
write_marker "$repo10" ".WORK-IN-PROGRESS-stale.md" "$foreign_pid" "$exp_started" "$exp_expires" "Source/gateway.py"
result="$(try_commit "$repo10")"
assert_advise "decayed marker advises" "$result" "decayed"

echo ""
echo "Results: $assertions assertions, $failures failures"
[ "$failures" -eq 0 ] && echo "ALL PASS" || echo "FAILURES"
exit "$failures"
