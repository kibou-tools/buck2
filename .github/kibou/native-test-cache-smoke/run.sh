#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
readonly BUCK2="${BUCK2:?BUCK2 must point to the patched debug binary}"
readonly CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-docker}"
readonly NATIVELINK_IMAGE="ghcr.io/tracemachina/nativelink@sha256:1d8e6cdfdf283025759ee15b0bd7d3b09a956402675321b8c2a4aa02f34ebe7d"
readonly STATE_ROOT="${STATE_ROOT:-${RUNNER_TEMP:-$REPO_ROOT/target}/buck2-native-test-cache-smoke}"
readonly WORKSPACE="$STATE_ROOT/workspace"
readonly LIVE_CACHE="$STATE_ROOT/live-cache"
readonly CACHE_ARCHIVE="${CACHE_ARCHIVE:-$STATE_ROOT/cache.tar.gz}"
readonly CONTAINER_NAME="buck2-native-test-cache-smoke"
readonly ISOLATION_DIR="kibou-native-test-cache-smoke"
readonly NONCE="${GITHUB_RUN_ID:-$$}"

container_running=false

fail() {
    echo "native-test cache smoke failure: $*" >&2
    exit 1
}

run_buck() {
    NANO_PRELUDE="$REPO_ROOT/tests/e2e_util/nano_prelude" \
        "$BUCK2" --isolation-dir="$ISOLATION_DIR" "$@"
}

what_ran() {
    run_buck log what-ran --emit-cache-queries
}

has_successful_upload() {
    run_buck log show | jq -e \
        'select(.Event.data.SpanEnd.data.CacheUpload.success == true)' >/dev/null
}

assert_contains() {
    local output="$1"
    local expected="$2"
    if [[ "$output" != *"$expected"* ]]; then
        echo "$output" >&2
        fail "expected log fragment: $expected"
    fi
}

assert_not_contains() {
    local output="$1"
    local unexpected="$2"
    [[ "$output" != *"$unexpected"* ]] || fail "unexpected log fragment: $unexpected"
}

stop_cache() {
    if [[ "$container_running" == true ]]; then
        "$CONTAINER_RUNTIME" stop "$CONTAINER_NAME" >/dev/null
        container_running=false
    fi
}

cleanup() {
    stop_cache
}
trap cleanup EXIT

start_cache() {
    mkdir -p \
        "$LIVE_CACHE/ac/content" "$LIVE_CACHE/ac/temp" \
        "$LIVE_CACHE/cas/content" "$LIVE_CACHE/cas/temp"
    "$CONTAINER_RUNTIME" run --rm -d \
        --name "$CONTAINER_NAME" \
        -p 50051:50051 \
        -v "$LIVE_CACHE:/data" \
        -v "$SCRIPT_DIR/nativelink.json5:/config.json5:ro" \
        "$NATIVELINK_IMAGE" /config.json5 >/dev/null
    container_running=true
    for _ in $(seq 1 30); do
        if "$CONTAINER_RUNTIME" logs "$CONTAINER_NAME" 2>&1 | grep -q "Ready, listening"; then
            return
        fi
        sleep 1
    done
    "$CONTAINER_RUNTIME" logs "$CONTAINER_NAME" >&2
    fail "NativeLink did not become ready"
}

archive_cache() {
    stop_cache
    mkdir -p "$(dirname "$CACHE_ARCHIVE")"
    tar -C "$LIVE_CACHE" -czf "$CACHE_ARCHIVE" ac cas
}

reset_buck_state() {
    run_buck kill >/dev/null 2>&1 || true
    case "$WORKSPACE" in
        "$STATE_ROOT"/*) rm -rf "$WORKSPACE/buck-out" ;;
        *) fail "refusing to remove Buck state outside the smoke directory" ;;
    esac
}

mkdir -p "$STATE_ROOT"
if [[ -d "$WORKSPACE" ]]; then
    (
        cd "$WORKSPACE"
        run_buck kill >/dev/null 2>&1 || true
    )
fi
rm -rf "$WORKSPACE" "$LIVE_CACHE"
cp -R "$SCRIPT_DIR/project" "$WORKSPACE"
mkdir -p "$LIVE_CACHE"

restored=false
if [[ -s "$CACHE_ARCHIVE" ]]; then
    tar -C "$LIVE_CACHE" -xzf "$CACHE_ARCHIVE"
    restored=true
fi

cd "$WORKSPACE"
start_cache
run_buck test //:cacheable -v=0
first_log="$(what_ran)"
assert_not_contains "$first_log" $'\tre\t'
if [[ "$restored" == true ]]; then
    assert_contains "$first_log" $'test.run\tcacheable\tcache\t'
else
    assert_contains "$first_log" $'test.run\tcacheable\tlocal\t'
    has_successful_upload || fail "first local test did not emit a successful upload"
fi

# Prove that both stores survive a full archive/restore and Buck state reset.
archive_cache
reset_buck_state
rm -rf "$LIVE_CACHE/ac" "$LIVE_CACHE/cas"
tar -C "$LIVE_CACHE" -xzf "$CACHE_ARCHIVE"
start_cache
run_buck test //:cacheable -v=0
restore_log="$(what_ran)"
assert_contains "$restore_log" $'test.run\tcacheable\tcache\t'
assert_not_contains "$restore_log" $'\tre\t'

# Failed executions never produce cache uploads and remain local after restart.
if run_buck test //:failing -v=0; then
    fail "failing test unexpectedly passed"
fi
failed_log="$(what_ran)"
assert_contains "$failed_log" $'test.run\tfailing\tlocal\t'
if has_successful_upload; then
    fail "failed test produced a cache upload"
fi
reset_buck_state
if run_buck test //:failing -v=0; then
    fail "failing test unexpectedly passed after restart"
fi
assert_contains "$(what_ran)" $'test.run\tfailing\tlocal\t'

# Provider opt-in, upload policy, and cache policy remain independent gates.
run_buck test //:not_opted_in -v=0
run_buck test //:not_opted_in -v=0
not_opted_in_log="$(what_ran)"
assert_contains "$not_opted_in_log" $'test.run\tnot_opted_in\tlocal\t'
assert_not_contains "$not_opted_in_log" $'\tcache_query\t'

run_buck test -c test.allow_cache_uploads=false //:upload_disabled -v=0
upload_disabled_log="$(what_ran)"
assert_contains "$upload_disabled_log" $'test.run\tupload_disabled\tlocal\t'
if has_successful_upload; then
    fail "upload-disabled test produced a cache upload"
fi
reset_buck_state
run_buck test -c test.allow_cache_uploads=false //:upload_disabled -v=0
assert_contains "$(what_ran)" $'test.run\tupload_disabled\tlocal\t'

run_buck test --no-remote-cache //:cacheable -v=0
no_cache_log="$(what_ran)"
assert_contains "$no_cache_log" $'test.run\tcacheable\tlocal\t'
assert_not_contains "$no_cache_log" $'\tcache\t'

# Mtime-only changes preserve the digest; content changes invalidate it.
touch resource.txt
run_buck test //:cacheable -v=0
assert_contains "$(what_ran)" $'test.run\tcacheable\tcache\t'
sleep 1
printf 'changed-%s\n' "$NONCE" > resource.txt
sleep 1
run_buck test //:cacheable -v=0
changed_log="$(what_ran)"
assert_contains "$changed_log" $'test.run\tcacheable\tlocal\t'
has_successful_upload || fail "content-invalidated test did not upload"

# A changed producer input reruns the producer while identical output bytes
# keep the downstream test result cached.
run_buck test //:generated_cacheable -v=0
generated_first_log="$(what_ran)"
assert_contains "$generated_first_log" $'test.run\tgenerated_cacheable\t'
sleep 1
printf 'changed-%s\n' "$NONCE" > producer-input.txt
sleep 1
run_buck test //:generated_cacheable -v=0
generated_second_log="$(what_ran)"
assert_contains "$generated_second_log" $'(generate_test_binary)\tlocal\t'
assert_contains "$generated_second_log" $'test.run\tgenerated_cacheable\tcache\t'
generated_script="$(find buck-out -path '*/__generated_cacheable__/generated_test.py' -type f -print -quit)"
[[ -n "$generated_script" ]] || fail "generated test script was not materialized"
cmp test_driver.py "$generated_script"

# Stop NativeLink before creating the cache artifact consumed by actions/cache.
archive_cache
echo "Native test cache smoke passed; cache archive: $CACHE_ARCHIVE"
