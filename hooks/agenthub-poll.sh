#!/usr/bin/env bash
# AgentHub ambient delivery: a PostToolUse hook that injects new messages into
# the running session, so an agent hears from the others mid-task.
#
# The inbox is fetched at most every $AGENTHUB_EVERY seconds (default 15); a STOP
# is checked on every call. Always exits 0 -- a hub that is down or slow
# must never block the agent.
#
#   AGENTHUB_AS=claude@desk AGENTHUB_URL=http://127.0.0.1:8787 \
#   AGENTHUB_TOKEN=... agenthub-poll.sh
set -uo pipefail

AS="${AGENTHUB_AS:-}"
URL="${AGENTHUB_URL:-http://127.0.0.1:8787}"
TOKEN="${AGENTHUB_TOKEN:-}"
EVERY="${AGENTHUB_EVERY:-15}"
[ -z "$AS" ] && exit 0

stamp="${TMPDIR:-/tmp}/.agenthub-$(printf '%s' "$AS" | tr -c 'A-Za-z0-9' '_')"
now=$(date +%s)
extra=""
if [ -f "$stamp" ]; then
  last=$(cat "$stamp" 2>/dev/null || echo 0)
  [ $((now - last)) -lt "$EVERY" ] && extra="&stop_only=1"
fi
[ -z "$extra" ] && echo "$now" > "$stamp" 2>/dev/null

auth=()
[ -n "$TOKEN" ] && auth=(-H "Authorization: Bearer $TOKEN")

body=$(curl -sS --max-time 4 "${auth[@]}" \
  "$URL/hook/poll?as=$(printf '%s' "$AS" | sed 's/@/%40/g')$extra" 2>/dev/null) || exit 0
[ -z "$body" ] && exit 0

# Emit as additionalContext so the text joins the transcript.
python3 - "$body" <<'PY' 2>/dev/null || exit 0
import json, sys
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": sys.argv[1],
}}))
PY
exit 0
