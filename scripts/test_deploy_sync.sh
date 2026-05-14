#!/usr/bin/env bash
# Integration test for deploy.sh sync_units.
#
# Sources scripts/deploy.sh with SYSTEMD_DRY_RUN=1 and fakes $REPO_DIR +
# $XIBI_SYSTEMD_USER_DIR so nothing actually touches the real systemd or
# the real repo. Asserts the SYNC_* accumulators match expectations across
# the five cases required by step-97 (Tests Required):
#   1) New unit install
#   2) Content drift update
#   3) No-op (byte-identical source + target)
#   4) Stale detection
#   5) SYSTEMD_DRY_RUN=1 prevents filesystem + systemd mutations

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TMP_ROOT=$(mktemp -d -t xibi-sync-test.XXXXXX)
trap 'rm -rf "$TMP_ROOT"' EXIT

FAIL_COUNT=0
PASS_COUNT=0

fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    echo "  FAIL: $*"
}

pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    echo "  PASS: $*"
}

assert_eq() {
    local got="$1" want="$2" label="$3"
    if [ "$got" = "$want" ]; then
        pass "$label"
    else
        fail "$label — got '$got', want '$want'"
    fi
}

# Each case gets a clean src/dst dir and its own state file under TMP_ROOT.
make_case() {
    local name="$1"
    local root="$TMP_ROOT/$name"
    mkdir -p "$root/repo/systemd" "$root/dst"
    echo "$root"
}

write_unit_with_install() {
    local path="$1"
    cat > "$path" <<'EOF'
[Unit]
Description=Fake xibi unit for testing

[Service]
Type=oneshot
ExecStart=/bin/true

[Install]
WantedBy=default.target
EOF
}

write_timer_with_install() {
    local path="$1"
    cat > "$path" <<'EOF'
[Unit]
Description=Fake xibi timer

[Timer]
OnBootSec=60
OnUnitActiveSec=60s

[Install]
WantedBy=timers.target
EOF
}

write_service_no_install() {
    local path="$1"
    cat > "$path" <<'EOF'
[Unit]
Description=Fake xibi onfail helper

[Service]
Type=oneshot
ExecStart=/bin/true
EOF
}

run_case() {
    local label="$1"
    shift
    echo ""
    echo "=== $label ==="
    # Reset accumulators between cases (explicit, so we see the fresh state
    # in each assertion — no subshell so pass/fail counters aggregate).
    SYNC_INSTALLED=""
    SYNC_UPDATED=""
    SYNC_ENABLED=""
    SYNC_STALE=""
    SYNC_REMOVED=""
    SYNC_WARNINGS=""
    SYNC_STALE_CHANGED=""
    "$@"
}

# --- Source deploy.sh once; BASH_SOURCE guard prevents main() from firing.
export SYSTEMD_DRY_RUN=1
# shellcheck disable=SC1091
source "$SCRIPT_DIR/deploy.sh"

# ---------------------------------------------------------------------------
# Case 1: New unit install (empty target dir → one service + one timer).
# ---------------------------------------------------------------------------
case1() {
    local root; root=$(make_case case1)
    export REPO_DIR="$root/repo"
    export XIBI_SYSTEMD_USER_DIR="$root/dst"
    export XIBI_DEPLOY_SYNC_STATE="$root/state"

    write_unit_with_install "$REPO_DIR/systemd/xibi-foo.service"
    write_timer_with_install "$REPO_DIR/systemd/xibi-foo.timer"

    sync_units

    assert_eq "$SYNC_INSTALLED" "xibi-foo.service xibi-foo.timer" "case1.installed set includes both"
    assert_eq "$SYNC_UPDATED" "" "case1.updated empty"
    # Condition 1: xibi-foo.service has sibling xibi-foo.timer → .service skipped for enable.
    # Timer gets enabled. So SYNC_ENABLED = just the timer.
    assert_eq "$SYNC_ENABLED" "xibi-foo.timer" "case1.enabled has only timer (service has sibling timer — carve-out)"
    assert_eq "$SYNC_STALE" "" "case1.stale empty"
    assert_eq "$SYNC_WARNINGS" "" "case1.warnings empty"
}
run_case "Case 1: new unit install (with timer-triggered-oneshot carve-out)" case1

# ---------------------------------------------------------------------------
# Case 2: Content drift update (target exists but differs from source).
# ---------------------------------------------------------------------------
case2() {
    local root; root=$(make_case case2)
    export REPO_DIR="$root/repo"
    export XIBI_SYSTEMD_USER_DIR="$root/dst"
    export XIBI_DEPLOY_SYNC_STATE="$root/state"

    write_timer_with_install "$REPO_DIR/systemd/xibi-bar.timer"
    # Put a different-content file in target to simulate drift.
    echo "# old content" > "$XIBI_SYSTEMD_USER_DIR/xibi-bar.timer"

    sync_units

    assert_eq "$SYNC_INSTALLED" "" "case2.installed empty"
    assert_eq "$SYNC_UPDATED" "xibi-bar.timer" "case2.updated set"
    # Under DRY_RUN, enable still populates SYNC_ENABLED for a timer with [Install].
    assert_eq "$SYNC_ENABLED" "xibi-bar.timer" "case2.enabled (dry-run marks timer enabled)"
    assert_eq "$SYNC_STALE" "" "case2.stale empty"
}
run_case "Case 2: content drift update" case2

# ---------------------------------------------------------------------------
# Case 3: No-op (src and dst byte-identical, standalone service).
# ---------------------------------------------------------------------------
case3() {
    local root; root=$(make_case case3)
    export REPO_DIR="$root/repo"
    export XIBI_SYSTEMD_USER_DIR="$root/dst"
    export XIBI_DEPLOY_SYNC_STATE="$root/state"
    # Seed the state file with empty — simulating steady state.
    : > "$XIBI_DEPLOY_SYNC_STATE"

    write_unit_with_install "$REPO_DIR/systemd/xibi-baz.service"
    # Copy identical content to dst so sync detects no drift.
    cp "$REPO_DIR/systemd/xibi-baz.service" "$XIBI_SYSTEMD_USER_DIR/xibi-baz.service"

    sync_units

    assert_eq "$SYNC_INSTALLED" "" "case3.installed empty"
    assert_eq "$SYNC_UPDATED" "" "case3.updated empty"
    # Standalone service (no sibling timer) with [Install] — under DRY_RUN,
    # enable always populates (we don't have a real is-enabled to skip).
    assert_eq "$SYNC_ENABLED" "xibi-baz.service" "case3.enabled (standalone .service gets enable, no timer sibling)"
    assert_eq "$SYNC_STALE" "" "case3.stale empty"
    assert_eq "$SYNC_STALE_CHANGED" "" "case3.stale unchanged — no telegram"
}
run_case "Case 3: no-op (byte-identical + standalone service)" case3

# ---------------------------------------------------------------------------
# Case 4: Stale detection (installed unit without repo source).
# ---------------------------------------------------------------------------
case4() {
    local root; root=$(make_case case4)
    export REPO_DIR="$root/repo"
    export XIBI_SYSTEMD_USER_DIR="$root/dst"
    export XIBI_DEPLOY_SYNC_STATE="$root/state"

    # Allow-list unit present in dst — must NOT appear in stale.
    write_unit_with_install "$XIBI_SYSTEMD_USER_DIR/xibi-deploy.service"
    # Stale unit — present in dst, absent from repo.
    write_unit_with_install "$XIBI_SYSTEMD_USER_DIR/xibi-dashboard.service"

    sync_units

    assert_eq "$SYNC_STALE" "xibi-dashboard.service" "case4.stale has dashboard only (deploy allow-listed)"
    # Step-130: removal phase populates SYNC_REMOVED even under DRY_RUN (intent
    # is recorded; the actual rm + disable are gated behind DRY_RUN below).
    assert_eq "$SYNC_REMOVED" "xibi-dashboard.service" "case4.removed populated with same set"
    # DRY_RUN must NOT actually delete the file from the fake dst dir.
    if [ ! -f "$XIBI_SYSTEMD_USER_DIR/xibi-dashboard.service" ]; then
        fail "case4.dry_run: stale file was deleted despite SYSTEMD_DRY_RUN=1"
    else
        pass "case4.dry_run: stale file preserved (no rm under dry-run)"
    fi
    # First-ever run: state file absent → previous empty, current {dashboard} → state change.
    assert_eq "$SYNC_STALE_CHANGED" "current:xibi-dashboard.service" "case4.stale_changed marks new stale set"

    # Test condition 2 second branch: run again on same state — should NOT re-fire.
    # Note: in dry-run the state file is never written, so we simulate by setting it.
    echo "xibi-dashboard.service" > "$XIBI_DEPLOY_SYNC_STATE"
    unset SYSTEMD_DRY_RUN
    export SYSTEMD_DRY_RUN=1
    sync_units

    assert_eq "$SYNC_STALE_CHANGED" "" "case4.second-run dedup: stale_changed empty (state matches)"

    # Clear the stale unit and re-run → expect 'cleared' branch.
    rm "$XIBI_SYSTEMD_USER_DIR/xibi-dashboard.service"
    sync_units
    assert_eq "$SYNC_STALE" "" "case4.cleared: SYNC_STALE empty after removal"
    assert_eq "$SYNC_REMOVED" "" "case4.cleared: SYNC_REMOVED empty (nothing to remove)"
    assert_eq "$SYNC_STALE_CHANGED" "cleared:xibi-dashboard.service" "case4.cleared: stale_changed reports cleared set"
}
run_case "Case 4: stale detection + dedup + cleared" case4

# ---------------------------------------------------------------------------
# Case 5: SYSTEMD_DRY_RUN=1 prevents mutations.
# ---------------------------------------------------------------------------
case5() {
    local root; root=$(make_case case5)
    export REPO_DIR="$root/repo"
    export XIBI_SYSTEMD_USER_DIR="$root/dst"
    export XIBI_DEPLOY_SYNC_STATE="$root/state"

    write_unit_with_install "$REPO_DIR/systemd/xibi-only.service"

    # Confirm dst is empty before
    [ -z "$(ls -A "$XIBI_SYSTEMD_USER_DIR")" ] || fail "case5.precondition: dst not empty"

    sync_units

    # Under DRY_RUN, accumulators populate but no file is copied.
    assert_eq "$SYNC_INSTALLED" "xibi-only.service" "case5.dry_run.installed populated"
    if [ -f "$XIBI_SYSTEMD_USER_DIR/xibi-only.service" ]; then
        fail "case5.dry_run: file was copied despite SYSTEMD_DRY_RUN=1"
    else
        pass "case5.dry_run: no file copied"
    fi
    if [ -f "$XIBI_DEPLOY_SYNC_STATE" ]; then
        fail "case5.dry_run: state file was written despite SYSTEMD_DRY_RUN=1"
    else
        pass "case5.dry_run: no state file written"
    fi
}
run_case "Case 5: SYSTEMD_DRY_RUN=1 prevents mutations" case5

# ---------------------------------------------------------------------------
# Case 6: Stale removal — non-dry-run actually deletes the file (step-130).
# Mocks `systemctl` so the disable call in the removal phase doesn't escape
# into the host user session. Verifies SYNC_REMOVED is populated and the
# unit file is gone from DST_DIR after sync.
# ---------------------------------------------------------------------------
# shellcheck disable=SC2317  # invoked indirectly via sync_units when DRY_RUN=0
systemctl() {
    # Swallow daemon-reload, enable, disable, is-enabled. Treat is-enabled
    # as "not enabled" (rc=1) so the enable phase does its thing without
    # caring. Treat disable as success. Everything else returns success.
    case "${2:-}" in
        is-enabled) return 1 ;;
        *) return 0 ;;
    esac
}
export -f systemctl

case6() {
    local root; root=$(make_case case6)
    export REPO_DIR="$root/repo"
    export XIBI_SYSTEMD_USER_DIR="$root/dst"
    export XIBI_DEPLOY_SYNC_STATE="$root/state"

    # Plant a stale unit with [Install] (exercises the disable branch).
    write_unit_with_install "$XIBI_SYSTEMD_USER_DIR/xibi-ghost.service"
    # And a stale unit without [Install] (exercises the bare-rm branch).
    write_service_no_install "$XIBI_SYSTEMD_USER_DIR/xibi-spirit.service"

    # Allow-list unit must survive.
    write_unit_with_install "$XIBI_SYSTEMD_USER_DIR/xibi-deploy.service"

    # Turn off dry-run for this case only.
    local saved="${SYSTEMD_DRY_RUN:-}"
    export SYSTEMD_DRY_RUN=0

    sync_units

    # Restore dry-run for subsequent cases (none currently, but defensive).
    export SYSTEMD_DRY_RUN="${saved:-1}"

    # SYNC_REMOVED should list BOTH stale units (allow-list excluded).
    # Order is the iteration order of cur_stale which is filesystem-glob order
    # (alphabetical): ghost before spirit.
    assert_eq "$SYNC_REMOVED" "xibi-ghost.service xibi-spirit.service" "case6.removed lists both stale units"
    if [ -f "$XIBI_SYSTEMD_USER_DIR/xibi-ghost.service" ]; then
        fail "case6.ghost file still present (rm did not fire)"
    else
        pass "case6.ghost file deleted from dst"
    fi
    if [ -f "$XIBI_SYSTEMD_USER_DIR/xibi-spirit.service" ]; then
        fail "case6.spirit file still present (rm did not fire)"
    else
        pass "case6.spirit file deleted from dst"
    fi
    # Allow-list survivor: must NOT be removed.
    if [ -f "$XIBI_SYSTEMD_USER_DIR/xibi-deploy.service" ]; then
        pass "case6.allow_list xibi-deploy.service preserved"
    else
        fail "case6.allow_list xibi-deploy.service was deleted — ALLOW_LIST not enforced"
    fi
}
run_case "Case 6: stale removal (non-dry-run, mocked systemctl)" case6
unset -f systemctl

echo ""
echo "=============================="
echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"
echo "=============================="
if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi
