# AgentHub ambient delivery for Windows: a PostToolUse hook that injects new
# messages into the running session.
#
# The inbox is fetched at most every $env:AGENTHUB_EVERY seconds (default 15); a
# STOP is checked on every call. Always exits 0 -- a hub that is down must
# never block the agent.
#
#   $env:AGENTHUB_AS = 'claude@desk'
#   $env:AGENTHUB_URL = 'http://127.0.0.1:8787'
#   $env:AGENTHUB_TOKEN = '...'

$ErrorActionPreference = 'SilentlyContinue'

$as = $env:AGENTHUB_AS
if ([string]::IsNullOrWhiteSpace($as)) { exit 0 }
$url = if ($env:AGENTHUB_URL) { $env:AGENTHUB_URL } else { 'http://127.0.0.1:8787' }
$every = if ($env:AGENTHUB_EVERY) { [int]$env:AGENTHUB_EVERY } else { 15 }

$safe  = ($as -replace '[^A-Za-z0-9]', '_')
$stamp = Join-Path $env:TEMP ".agenthub-$safe"
$now   = [int][double]::Parse((Get-Date -UFormat %s))
$extra = ''
if (Test-Path $stamp) {
    $last = 0
    [int]::TryParse((Get-Content $stamp -Raw).Trim(), [ref]$last) | Out-Null
    if (($now - $last) -lt $every) { $extra = '&stop_only=1' }
}
if (-not $extra) { Set-Content -Path $stamp -Value $now -Encoding ascii }

$headers = @{}
if ($env:AGENTHUB_TOKEN) { $headers['Authorization'] = "Bearer $($env:AGENTHUB_TOKEN)" }

try {
    $resp = Invoke-WebRequest -Uri "$url/hook/poll?as=$([uri]::EscapeDataString($as))$extra" `
        -Headers $headers -TimeoutSec 4 -UseBasicParsing
    $text = $resp.Content
} catch { exit 0 }

if ([string]::IsNullOrWhiteSpace($text)) { exit 0 }

$payload = @{ hookSpecificOutput = @{
    hookEventName    = 'PostToolUse'
    additionalContext = $text
} } | ConvertTo-Json -Depth 5 -Compress

Write-Output $payload
exit 0
