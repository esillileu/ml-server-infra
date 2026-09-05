#!/usr/bin/env bash

set -euo pipefail

readonly SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
readonly TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
assert_contains() { [[ $1 == *"$2"* ]] || fail "expected output to contain: $2"; }
assert_fails() { if "$@" >"$TEST_ROOT/out" 2>"$TEST_ROOT/err"; then fail "command unexpectedly succeeded: $*"; fi; }

mkdir -p "$TEST_ROOT/bin" "$TEST_ROOT/config/svc-ln"
ln -s "$SOURCE_DIR/services" "$TEST_ROOT/config/svc-ln/services"
ln -s "$SOURCE_DIR/targets" "$TEST_ROOT/config/svc-ln/targets"

cat >"$TEST_ROOT/bin/ssh" <<'EOF'
#!/usr/bin/env bash
printf '<%s>\n' "$@"
EOF

cat >"$TEST_ROOT/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$MOCK_SYSTEMCTL_LOG"
if [[ ${MOCK_SYSTEMCTL_FAIL:-0} == 1 ]]; then exit 1; fi
if [[ " $* " == *" show "* ]]; then
    printf 'ActiveState=%s\nSubState=%s\n' "${MOCK_ACTIVE:-inactive}" "${MOCK_SUB:-dead}"
fi
EOF
chmod +x "$TEST_ROOT/bin/ssh" "$TEST_ROOT/bin/systemctl"

export HOME="$TEST_ROOT/home"
export XDG_CONFIG_HOME="$TEST_ROOT/config"
export PATH="$TEST_ROOT/bin:$PATH"
export MOCK_SYSTEMCTL_LOG="$TEST_ROOT/systemctl.log"
: >"$MOCK_SYSTEMCTL_LOG"

output=$($SOURCE_DIR/svc-ln _run super)
assert_contains "$output" '<127.0.0.1:15001:127.0.0.1:5001>'
assert_contains "$output" '<127.0.0.1:15002:127.0.0.1:5002>'
assert_contains "$output" '<127.0.0.1:15432:127.0.0.1:5432>'
assert_contains "$output" '<127.0.0.1:19000:127.0.0.1:9000>'
[[ $(grep -c '^<-R>$' <<<"$output") == 4 ]] || fail 'expected four reverse forwards'
[[ $output != *Compression* ]] || fail 'compression must not be enabled'

export MOCK_ACTIVE=active MOCK_SUB=running
$SOURCE_DIR/svc-ln on super >/dev/null
assert_contains "$(<"$MOCK_SYSTEMCTL_LOG")" '--user start svc-ln@super.service'
$SOURCE_DIR/svc-ln restart super >/dev/null
assert_contains "$(<"$MOCK_SYSTEMCTL_LOG")" '--user restart svc-ln@super.service'

for state in 'active running ON' 'activating auto-restart RECONNECTING' 'inactive dead OFF' 'failed failed DOWN'; do
    read -r MOCK_ACTIVE MOCK_SUB expected <<<"$state"
    export MOCK_ACTIVE MOCK_SUB
    output=$($SOURCE_DIR/svc-ln status super)
    assert_contains "$output" "$expected"
done

export MOCK_SYSTEMCTL_FAIL=1
assert_fails "$SOURCE_DIR/svc-ln" status super
assert_contains "$(<"$TEST_ROOT/out")" DOWN
unset MOCK_SYSTEMCTL_FAIL

mv "$TEST_ROOT/config/svc-ln/targets" "$TEST_ROOT/config/svc-ln/targets.saved"
$SOURCE_DIR/svc-ln off deleted >/dev/null
assert_contains "$(<"$MOCK_SYSTEMCTL_LOG")" '--user stop svc-ln@deleted.service'
mv "$TEST_ROOT/config/svc-ln/targets.saved" "$TEST_ROOT/config/svc-ln/targets"

mkdir -p "$TEST_ROOT/bad/svc-ln/services" "$TEST_ROOT/bad/svc-ln/targets"
cat >"$TEST_ROOT/bad/svc-ln/targets/bad.conf" <<'EOF'
HOST=example.com
USER=user
SERVICES=one,two
EOF
cat >"$TEST_ROOT/bad/svc-ln/services/one.conf" <<'EOF'
LOCAL_HOST=127.0.0.1
LOCAL_PORT=1
REMOTE_PORT=1000
EOF
cat >"$TEST_ROOT/bad/svc-ln/services/two.conf" <<'EOF'
LOCAL_HOST=127.0.0.1
LOCAL_PORT=2
REMOTE_PORT=1000
EOF
XDG_CONFIG_HOME="$TEST_ROOT/bad" assert_fails "$SOURCE_DIR/svc-ln" _run bad
assert_contains "$(<"$TEST_ROOT/err")" 'REMOTE_PORT 1000 conflicts'

cat >>"$TEST_ROOT/bad/svc-ln/services/two.conf" <<'EOF'
UNEXPECTED=value
EOF
XDG_CONFIG_HOME="$TEST_ROOT/bad" assert_fails "$SOURCE_DIR/svc-ln" _run bad
assert_contains "$(<"$TEST_ROOT/err")" 'unknown key UNEXPECTED'

cat >"$TEST_ROOT/bad/svc-ln/targets/down.conf" <<'EOF'
HOST=example.com
USER=user
SERVICES=down
EOF
cat >"$TEST_ROOT/bad/svc-ln/services/down.conf" <<'EOF'
LOCAL_HOST=127.0.0.1
LOCAL_PORT=1
REMOTE_PORT=1001
EOF
export MOCK_ACTIVE=active MOCK_SUB=running
output=$(XDG_CONFIG_HOME="$TEST_ROOT/bad" "$SOURCE_DIR/svc-ln" status down)
assert_contains "$output" 'down'
assert_contains "$output" '127.0.0.1:1'
assert_contains "$output" 'DOWN'
export MOCK_ACTIVE=inactive MOCK_SUB=dead
output=$(XDG_CONFIG_HOME="$TEST_ROOT/bad" "$SOURCE_DIR/svc-ln" status down)
assert_contains "$output" 'OFF'

mkdir -p "$HOME/.local/bin"
ln -s "$SOURCE_DIR/svc-ln" "$HOME/.local/bin/svc-ln"
output=$(SSH_CONNECTION=1 bash --noprofile --norc -ic "source '$SOURCE_DIR/login-hook.bash'" 2>/dev/null)
assert_contains "$output" 'TARGET'
output=$(SSH_CONNECTION= bash --noprofile --norc -ic "source '$SOURCE_DIR/login-hook.bash'" 2>/dev/null)
[[ -z $output ]] || fail 'local interactive shell printed login status'

$SOURCE_DIR/deploy >/dev/null
$SOURCE_DIR/deploy >/dev/null
[[ $(readlink "$HOME/.local/bin/svc-ln") == "$SOURCE_DIR/svc-ln" ]] || fail 'deploy created the wrong executable link'

refusal_home="$TEST_ROOT/refusal-home"
mkdir -p "$refusal_home/.local/bin"
touch "$refusal_home/.local/bin/svc-ln"
HOME="$refusal_home" XDG_CONFIG_HOME="$refusal_home/.config" assert_fails "$SOURCE_DIR/deploy"
assert_contains "$(<"$TEST_ROOT/err")" 'refusing existing path'

printf 'All svc-ln tests passed.\n'
