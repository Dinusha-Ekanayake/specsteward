#!/usr/bin/env bash
# specsteward hook (subagent-stop): hands the event to the plugin launcher, scripts/sdd.sh.
exec "$BASH" "${CLAUDE_PLUGIN_ROOT}/scripts/sdd.sh" hook-subagent-stop
