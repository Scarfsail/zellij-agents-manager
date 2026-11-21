#!/usr/bin/env bash
# The Claude Code hook of older versions. Agent state now comes from Zellij itself (OSC 7501, Zellij 0.46), so this
# does nothing; it stays until the next release so a settings.json that still lists it never errors.
# ./setup.sh (or scripts/install-hooks.sh --uninstall-hooks) removes it from there.
cat >/dev/null
exit 0
