#!/usr/bin/env bash
# specsteward hook (format): hands the event to the plugin launcher, scripts/sdd.sh.
exec "$BASH" "${CLAUDE_PLUGIN_ROOT}/scripts/sdd.sh" hook-format
