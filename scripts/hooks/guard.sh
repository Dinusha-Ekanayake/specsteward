#!/usr/bin/env bash
# specsteward hook (guard): hands the event to the plugin launcher, scripts/sdd.sh.
exec bash "${CLAUDE_PLUGIN_ROOT}/scripts/sdd.sh" hook-guard
