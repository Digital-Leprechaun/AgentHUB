#!/usr/bin/env bash
# Registers this Linux machine's Claude and Codex agents with the AgentHub, and
# installs message notifications for both.
#
#   1. checks the hub answers;
#   2. gets the machine's token (--token, else ~/agenthub/data/tokens.json when this
#      box hosts the hub, else over ssh from the hub box, else asks);
#   3. puts AGENTHUB_URL and AGENTHUB_TOKEN in ~/.bashrc (Codex reads the token from
#      the environment every time it starts);
#   4. registers the `agenthub` MCP server for Claude Code (user scope) and Codex,
#      replacing any earlier `agenthub` entry;
#   5. installs the notification hooks for both (install_hooks.py);
#   6. installs and starts the wake bridges (install_bridge.py);
#   7. announces both agents with hub_hello.
#
# The token is never written next to this script or printed.
#
# Site settings come from site.json beside this script (or $AGENTHUB_SITE); copy
# site.example.json to start one. Arguments override it.
#
#   bash register-agenthub.sh [--dry-run] [--url URL] [--token T] [--machine NAME] [--hub-ssh USER@HOST]
#
# Restart Claude Code and Codex afterwards, and approve the hooks once in Codex with /hooks.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE="${AGENTHUB_SITE:-$HERE/site.json}"
# site KEY: a string setting from site.json, or nothing.
site() {
  [ -r "$SITE" ] || return 0
  python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print(v if isinstance(v, str) else "")' "$SITE" "$1"
}
MACHINE="$(hostname)"
URL=""
TOKEN=""
HUB_SSH="$(site hub_ssh)"
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --url)     URL="$2"; shift ;;
    --token)   TOKEN="$2"; shift ;;
    --machine) MACHINE="$2"; shift ;;
    --hub-ssh) HUB_SSH="$2"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

# Agent name = platform + machine. site.json `hosts` rules map a hostname to its short
# host part: a rule's `host`, or else the pattern's first capture group.
HOSTPART="$(python3 - "$SITE" "$MACHINE" <<'PY'
import json, re, sys
path, name = sys.argv[1:3]
try:
    rules = json.load(open(path)).get("hosts", [])
except (OSError, ValueError):
    rules = []
for rule in rules:
    m = re.match(rule.get("pattern", "(?!)"), name, re.I)
    if m:
        print((rule.get("host") or m.group(1)).lower())
        break
else:
    if re.match(r"^[A-Za-z0-9-]+$", name):
        print(name.lower())
PY
)"
[ -n "$HOSTPART" ] || { echo "FAILED: cannot derive a host part from machine '$MACHINE'; pass --machine." >&2; exit 1; }
SHORT="$(printf '%s' "${HOSTPART:0:1}" | tr '[:lower:]' '[:upper:]')${HOSTPART:1}"

# Kept unexpanded: over ssh the ~ belongs to the hub box's user.
TOKENS_PATH="$(site hub_tokens)"; [ -n "$TOKENS_PATH" ] || TOKENS_PATH='~/agenthub/data/tokens.json'
TOKENS_LOCAL="${TOKENS_PATH/#\~/$HOME}"
if [ -z "$URL" ]; then
  if [ -r "$TOKENS_LOCAL" ]; then URL="http://127.0.0.1:8787"; else URL="$(site hub_url)"; URL="${URL:-${AGENTHUB_URL:-}}"; fi
fi
[ -n "$URL" ] || { echo "FAILED: no hub URL: pass --url, set hub_url in site.json, or set AGENTHUB_URL." >&2; exit 1; }
URL="${URL%/}"

say() { printf '%s\n' "$*"; }
FAILURES=0
echo "Machine $MACHINE -> Claude$SHORT (claude@$HOSTPART), Codex$SHORT (codex@$HOSTPART)"
[ "$DRY" = 1 ] && say "(dry run: checking only, nothing will be changed)"

# 1. hub
if [ "$(curl -s --max-time 8 "$URL/health")" != "ok" ]; then
  say "FAILED: the hub at $URL did not answer."; exit 1
fi
say "hub         $URL is up"

# 2. token
pick() { python3 -c 'import json,sys; d=json.load(sys.stdin); h=sys.argv[1]; print(d.get("*@"+h) or d.get("claude@"+h) or "")' "$HOSTPART"; }
SOURCE="--token"
if [ -z "$TOKEN" ] && [ -r "$TOKENS_LOCAL" ]; then TOKEN="$(pick < "$TOKENS_LOCAL")"; SOURCE="$TOKENS_LOCAL"; fi
if [ -z "$TOKEN" ] && [ -n "$HUB_SSH" ]; then
  TOKEN="$(ssh -o BatchMode=yes -o ConnectTimeout=8 "$HUB_SSH" "cat $TOKENS_PATH" 2>/dev/null | pick 2>/dev/null)"
  SOURCE="ssh $HUB_SSH"
fi
if [ -z "$TOKEN" ]; then
  read -r -s -p "Paste the *@$HOSTPART token: " TOKEN; echo; SOURCE="typed in"
fi
[ -n "$TOKEN" ] || { say "FAILED: no token."; exit 1; }
say "token       from $SOURCE"

# 3. environment
set_env() {
  local name="$1" value="$2"
  if grep -q "^export $name=" "$HOME/.bashrc" 2>/dev/null; then
    if grep -qx "export $name=$value" "$HOME/.bashrc"; then say "env         $name already set"; return; fi
    [ "$DRY" = 1 ] && { say "env         would update $name in ~/.bashrc"; return; }
    python3 - "$HOME/.bashrc" "$name" "$value" <<'PY'
import re, sys
path, name, value = sys.argv[1:4]
text = open(path).read()
text = re.sub(rf"^export {re.escape(name)}=.*$", f"export {name}={value}", text, flags=re.M)
open(path, "w").write(text)
PY
    say "env         updated $name in ~/.bashrc"
  else
    [ "$DRY" = 1 ] && { say "env         would add $name to ~/.bashrc"; return; }
    printf 'export %s=%s\n' "$name" "$value" >> "$HOME/.bashrc"
    say "env         added $name to ~/.bashrc"
  fi
}
set_env AGENTHUB_URL "$URL"
set_env AGENTHUB_TOKEN "$TOKEN"
export AGENTHUB_URL="$URL" AGENTHUB_TOKEN="$TOKEN"

# 4. MCP registrations
if command -v claude >/dev/null; then
  if [ "$DRY" = 1 ]; then say "claude      would register agenthub -> $URL/mcp"
  else
    claude mcp remove agenthub --scope user >/dev/null 2>&1
    if claude mcp add --transport http --scope user agenthub "$URL/mcp" --header "Authorization: Bearer $TOKEN" >/dev/null 2>&1
    then say "claude      registered agenthub -> $URL/mcp"
    else say "claude      FAILED to register"; FAILURES=$((FAILURES+1)); fi
  fi
else say "claude      NOT FOUND"; FAILURES=$((FAILURES+1)); fi

if command -v codex >/dev/null; then
  if [ "$DRY" = 1 ]; then say "codex       would register agenthub -> $URL/mcp, token from AGENTHUB_TOKEN"
  else
    codex mcp remove agenthub >/dev/null 2>&1
    if codex mcp add agenthub --url "$URL/mcp" --bearer-token-env-var AGENTHUB_TOKEN >/dev/null 2>&1
    then say "codex       registered agenthub -> $URL/mcp (token from AGENTHUB_TOKEN)"
    else say "codex       FAILED to register"; FAILURES=$((FAILURES+1)); fi
  fi
else say "codex       NOT FOUND"; FAILURES=$((FAILURES+1)); fi

# 5. notification hooks
args=(--host "$HOSTPART"); [ "$DRY" = 1 ] && args+=(--dry-run)
if out="$(python3 "$HERE/install_hooks.py" "${args[@]}" 2>&1)"; then
  printf '%s\n' "$out" | grep -v '^watch ' | grep -v '^$' | sed 's/^/hooks       /'
  [ "$DRY" = 1 ] || say "hooks       Codex: approve the new hooks once with /hooks in Codex, or they will not run"
else
  say "hooks       FAILED:"; printf '%s\n' "$out"; FAILURES=$((FAILURES+1))
fi

# 6. wake bridges, auto-started
bargs=(--host "$HOSTPART"); [ "$DRY" = 1 ] && bargs+=(--dry-run)
if bout="$(python3 "$HERE/install_bridge.py" "${bargs[@]}" 2>&1)"; then
  printf '%s\n' "$bout" | grep -v '^$' | sed 's/^/bridge      /'
else
  printf '%s\n' "$bout" | grep -v '^$' | sed 's/^/bridge      /'; FAILURES=$((FAILURES+1))
fi

# 7. announce both agents
for addr in "claude@$HOSTPART" "codex@$HOSTPART"; do
  if [ "$DRY" = 1 ]; then say "hub_hello   would announce $addr"; continue; fi
  body="{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"hub_hello\",\"arguments\":{\"as\":\"$addr\"}}}"
  res="$(curl -s --max-time 10 -X POST "$URL/mcp" -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" -d "$body")"
  if printf '%s' "$res" | grep -q '"isError": false'; then say "hub_hello   $addr is on the hub"
  else say "hub_hello   $addr FAILED"; FAILURES=$((FAILURES+1)); fi
done

say ""
if [ "$FAILURES" -gt 0 ]; then say "$FAILURES step(s) failed; see above."; exit 1; fi
if [ "$DRY" = 1 ]; then say "Checks passed. Run again without --dry-run to register."
else say "Done. Restart Claude Code and Codex. Board: $URL/board"; fi
