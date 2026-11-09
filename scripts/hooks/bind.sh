#!/usr/bin/env bash
# specsteward hook (bind): hands the event to the plugin launcher, scripts/sdd.sh.
exec bash "${CLAUDE_PLUGIN_ROOT}/scripts/sdd.sh" hook-bind
