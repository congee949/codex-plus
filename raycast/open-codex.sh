#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Open Codex
# @raycast.mode compact

# Optional parameters:
# @raycast.icon 💬
# @raycast.packageName Codex
# @raycast.description Open ChatGPT (Codex) with prompt suggestions and transcript width

root=$(cd "$(dirname "$0")/.." && pwd)
for python in "$CODEX_PLUS_PYTHON" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  [ -n "$python" ] && [ -x "$python" ] && exec "$python" "$root/codex_launch.py" launch
done
echo "python3 not found" >&2
exit 1
