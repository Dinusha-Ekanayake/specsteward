#!/usr/bin/env bash
# End-to-end run of the scripted half of specsteward on a copy of examples/python-hello.
# The implementer and reviewer work is scripted here; the subagents need Claude Code. Run in CI.
set -euo pipefail

PLUGIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
export SDD_HOME="$WORK/home"               # an isolated trust store, outside the project
export CLAUDE_PROJECT_DIR="$WORK/hello"
export GIT_CEILING_DIRECTORIES="$WORK"
export SDD_PYTHON="${SDD_PYTHON:-python}"
mkdir -p "$SDD_HOME"
cp -r "$PLUGIN/examples/python-hello" "$CLAUDE_PROJECT_DIR"
rm -rf "$CLAUDE_PROJECT_DIR/.sdd"
cd "$CLAUDE_PROJECT_DIR"
git init -q -b main && git config user.email e2e@example.com && git config user.name e2e
git add . && git commit -q -m "example"

step() { printf '\n=== %s\n' "$*"; }
SDD="bash .sdd/bin/sdd"

step "init (keeps the example's sdd.toml, does not trust it)"
bash "$PLUGIN/scripts/sdd.sh" init
step "check before trust must refuse"
$SDD check && { echo "UNEXPECTED: ran untrusted"; exit 1; } || true
step "trust, doctor, analyze"
$SDD trust
$SDD doctor || true
$SDD analyze 001
step "approve and start in one prompt"
$SDD approve 001 --start
step "next"
$SDD next
$SDD brief T-001-01 >/dev/null && head -12 .sdd/briefs/T-001-01.md

step "implementer: T-001-01 (test first)"
mkdir -p src tests
cat > tests/test_hello.py <<'PY'
import pytest

from hello import greet


def test_greets_by_name() -> None:
    assert greet("Ada") == "Hello, Ada!"
PY
$SDD check && { echo "UNEXPECTED: gate passed without the code"; exit 1; } || echo "(RED as expected)"
cat > src/hello.py <<'PY'
def greet(name: str) -> str:
    """Greet someone by name."""
    return f"Hello, {name}!"
PY
$SDD check
step "done before review must refuse"
$SDD done T-001-01 && { echo "UNEXPECTED: ticked without review"; exit 1; } || true
$SDD review T-001-01 clean
$SDD done T-001-01

step "next group: T-001-02 (high risk)"
$SDD next
cat >> tests/test_hello.py <<'PY'


@pytest.mark.parametrize("name", ["", "   "])
def test_blank_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError):
        greet(name)
PY
cat > src/hello.py <<'PY'
def greet(name: str) -> str:
    """Greet someone by name; a blank name is an error."""
    if not name.strip():
        raise ValueError("name must not be blank")
    return f"Hello, {name}!"
PY
$SDD check
$SDD review T-001-02 clean
$SDD done T-001-02

step "hook runner: the guards answer through scripts/hooks/run.cmd (the way Claude Code starts them)"
export CLAUDE_PLUGIN_ROOT="$PLUGIN"
HOOK_RUN="$PLUGIN/scripts/hooks/run.cmd"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) echo "(Windows: covered by the hooks test in the manifests job)" ;;
  *)
    out="$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"bash .sdd/bin/sdd approve 001"}}' | "$HOOK_RUN" shell-guard)"
    echo "$out" | grep -q '"ask"' || { echo "UNEXPECTED: shell guard did not ask: $out"; exit 1; }
    out="$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"ls"}}' | "$HOOK_RUN" shell-guard)"
    [ -z "$out" ] || { echo "UNEXPECTED: shell guard blocked ls: $out"; exit 1; }
    out="$(printf '%s' '{"tool_name":"Write","tool_input":{"file_path":".sdd/state.json"}}' | "$HOOK_RUN" guard)"
    echo "$out" | grep -q 'deny' || { echo "UNEXPECTED: file guard did not deny: $out"; exit 1; }
    ;;
esac

step "converge (nothing missing), status, views"
$SDD next
$SDD converge
$SDD status
$SDD statusline </dev/null
$SDD dashboard
$SDD stop
echo "E2E OK in $WORK"
