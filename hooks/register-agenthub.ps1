<#
Registers this Windows machine's Claude and Codex agents with the AgentHub.

For the machine's agents (claude@<host> and codex@<host>) it:
  1. checks the hub answers;
  2. gets the machine's token (-Token, else read from the hub box over ssh, else asks);
  3. sets AGENTHUB_URL and AGENTHUB_TOKEN as user environment variables (Codex reads
     the token from AGENTHUB_TOKEN every time it starts);
  4. registers the hub as the `agenthub` MCP server for Claude Code (user scope) and
     for Codex, replacing any earlier `agenthub` entry;
  5. installs the notification hooks for both (install_hooks.py);
  6. installs and starts the wake bridges (install_bridge.py);
  7. announces both agents on the hub with hub_hello, which also proves the token.

The token is never written next to this script or printed.

Site settings come from site.json beside this script (or $env:AGENTHUB_SITE); copy
site.example.json to start one. Parameters override it.

    powershell -NoProfile -ExecutionPolicy Bypass -File register-agenthub.ps1 -WhatIf
    powershell -NoProfile -ExecutionPolicy Bypass -File register-agenthub.ps1

    -Token <value>        use this token instead of fetching it
    -HubUrl <url>         default: site.json hub_url, else $env:AGENTHUB_URL
    -HubSsh <user@host>   where tokens.json lives; default: site.json hub_ssh (none: ask)
    -Machine <name>       override the detected machine name
    -WhatIf               check everything, change nothing

Restart Claude Code and Codex afterwards: both load MCP servers and environment
variables only when they start.
#>
param(
    [string]$Machine = $env:COMPUTERNAME,
    [string]$HubUrl,
    [string]$Token,
    [string]$HubSsh,
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'

function Say([string]$Text)  { Write-Output $Text }
function Fail([string]$Text) { Write-Output "FAILED: $Text"; exit 1 }

$SitePath = if ($env:AGENTHUB_SITE) { $env:AGENTHUB_SITE } else { Join-Path $PSScriptRoot 'site.json' }
$Site = $null
if (Test-Path $SitePath) {
    try { $Site = Get-Content $SitePath -Raw | ConvertFrom-Json } catch { Fail "cannot parse $SitePath ($($_.Exception.Message))." }
}
if (-not $HubUrl -and $Site -and $Site.hub_url) { $HubUrl = $Site.hub_url }
if (-not $HubUrl) { $HubUrl = $env:AGENTHUB_URL }
if (-not $HubUrl) { Fail "no hub URL: pass -HubUrl, set hub_url in site.json, or set AGENTHUB_URL." }
$HubUrl = $HubUrl.TrimEnd('/')
if (-not $HubSsh -and $Site -and $Site.hub_ssh) { $HubSsh = $Site.hub_ssh }
$HubTokens = if ($Site -and $Site.hub_tokens) { $Site.hub_tokens } else { '~/agenthub/data/tokens.json' }

# Agent name = platform + machine. site.json `hosts` rules map a hostname to its short
# host part: a rule's `host`, or else the pattern's first capture group.
$HostPart = $null
if ($Site -and $Site.hosts) {
    foreach ($rule in $Site.hosts) {
        if ($Machine -match $rule.pattern) {
            $HostPart = if ($rule.host) { $rule.host } else { $Matches[1] }
            break
        }
    }
}
if (-not $HostPart) {
    if ($Machine -notmatch '^[A-Za-z0-9-]+$') { Fail "cannot derive a host part from machine '$Machine'; pass -Machine." }
    $HostPart = $Machine
}
$HostPart = $HostPart.ToLower()
$Short = $HostPart.Substring(0, 1).ToUpper() + $HostPart.Substring(1)
$Agents = @(
    @{ Platform = 'Claude'; Addr = "claude@$HostPart" },
    @{ Platform = 'Codex';  Addr = "codex@$HostPart"  }
)
Say "Machine $Machine -> Claude$Short (claude@$HostPart), Codex$Short (codex@$HostPart)"
if ($WhatIf) { Say "(WhatIf: checking only, nothing will be changed)" }

# Runs a native tool without PowerShell 5.1 turning its stderr into terminating errors.
function Invoke-Tool([string]$Exe, [string[]]$ToolArgs) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @ToolArgs 2>&1 | Out-String
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($script:TokenValue) { $out = $out.Replace($script:TokenValue, '*****') }
    return [pscustomobject]@{ Code = $code; Output = $out.Trim() }
}

# --- 1. hub -----------------------------------------------------------------
try {
    $health = (Invoke-WebRequest -Uri "$HubUrl/health" -UseBasicParsing -TimeoutSec 8).Content.Trim()
} catch {
    Fail "the hub at $HubUrl did not answer ($($_.Exception.Message))."
}
if ($health -ne 'ok') { Fail "the hub at $HubUrl answered '$health' instead of 'ok'." }
Say "hub         $HubUrl is up"

# --- 2. token ---------------------------------------------------------------
$source = '-Token'
$sshWhy = ''
if (-not $Token -and $HubSsh) {
    $ssh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
    if (-not (Test-Path $ssh)) {
        $sshWhy = 'the OpenSSH client is not installed (Settings > Optional features > OpenSSH Client)'
    } else {
        $r = Invoke-Tool $ssh @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', $HubSsh, "cat $HubTokens")
        if ($r.Code -ne 0) {
            $sshWhy = switch -Regex ($r.Output) {
                'Host key verification failed' { "this machine has never connected to $HubSsh, so it does not trust its host key yet (run: ssh $HubSsh exit, and answer yes)"; break }
                'Permission denied'            { "$HubSsh does not accept a key from this machine (add this machine's public key to its authorized_keys)"; break }
                'Could not resolve hostname'   { "the name '$($HubSsh.Split('@')[-1])' does not resolve on this machine; pass -HubSsh user@<ip>"; break }
                'timed out|No route|refused'   { "$HubSsh did not answer on port 22"; break }
                default                        { "ssh failed: $($r.Output)" }
            }
        }
        if ($r.Code -eq 0) {
            try {
                $map = $r.Output | ConvertFrom-Json
                foreach ($key in @("*@$HostPart", "claude@$HostPart")) {
                    # Match the name exactly: keys start with '*', which is a wildcard to PowerShell.
                    $val = $map.PSObject.Properties | Where-Object { $_.Name -ceq $key } | Select-Object -First 1
                    if ($val) { $Token = $val.Value; $source = "ssh $HubSsh ($key)"; break }
                }
            } catch { }
        }
    }
}
if (-not $Token) {
    if ($sshWhy) { Say "ssh         could not read the token: $sshWhy" }
    Say "Ask the hub's admin for the '*@$HostPart' token (it is in $HubTokens on the hub box), or rerun with -Token."
    $secure = Read-Host "Paste the *@$HostPart token" -AsSecureString
    $Token = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    $source = 'typed in'
}
if (-not $Token) { Fail "no token." }
$script:TokenValue = $Token
Say "token       from $source"

# --- proof: the hub accepts this token for both agents ------------------------
function Invoke-Hub([string]$Tool, [hashtable]$Arguments) {
    $body = @{ jsonrpc = '2.0'; id = 1; method = 'tools/call';
               params = @{ name = $Tool; arguments = $Arguments } } | ConvertTo-Json -Depth 6 -Compress
    $resp = Invoke-RestMethod -Uri "$HubUrl/mcp" -Method Post -Body $body -ContentType 'application/json' `
                              -Headers @{ Authorization = "Bearer $Token" } -TimeoutSec 15
    return $resp.result
}

# --- 3. environment ---------------------------------------------------------
foreach ($pair in @(@('AGENTHUB_URL', $HubUrl), @('AGENTHUB_TOKEN', $Token))) {
    $current = [Environment]::GetEnvironmentVariable($pair[0], 'User')
    if ($current -eq $pair[1]) { Say "env         $($pair[0]) already set"; continue }
    if ($WhatIf) { Say "env         would set $($pair[0]) (user)"; continue }
    [Environment]::SetEnvironmentVariable($pair[0], $pair[1], 'User')
    Say "env         set $($pair[0]) (user)"
}
$env:AGENTHUB_URL = $HubUrl
$env:AGENTHUB_TOKEN = $Token

# --- 4. MCP registrations ---------------------------------------------------
function Find-Exe([string]$Name, [string[]]$Candidates) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($c in $Candidates) {
        $hit = Get-ChildItem -Path $c -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

$claudeExe = Find-Exe 'claude' @(
    "$env:USERPROFILE\.local\bin\claude.exe",
    "$env:APPDATA\Claude\claude-code\*\claude.exe",
    "$env:APPDATA\npm\claude.cmd")
$codexExe = Find-Exe 'codex' @(
    "$env:LOCALAPPDATA\OpenAI\Codex\bin\*\codex.exe",
    "$env:APPDATA\npm\codex.cmd")

$failures = 0

if (-not $claudeExe) {
    Say "claude      NOT FOUND - install Claude Code, or register by hand (see SETUP.md)"
    $failures++
} elseif ($WhatIf) {
    Say "claude      would register agenthub -> $HubUrl/mcp  ($claudeExe)"
} else {
    Invoke-Tool $claudeExe @('mcp', 'remove', 'agenthub', '--scope', 'user') | Out-Null
    $r = Invoke-Tool $claudeExe @('mcp', 'add', '--transport', 'http', '--scope', 'user', 'agenthub',
                                  "$HubUrl/mcp", '--header', "Authorization: Bearer $Token")
    if ($r.Code -ne 0) { Say "claude      FAILED to register:`n$($r.Output)"; $failures++ }
    else { Say "claude      registered agenthub -> $HubUrl/mcp" }
}

if (-not $codexExe) {
    Say "codex       NOT FOUND - install Codex, or add [mcp_servers.agenthub] to ~/.codex/config.toml by hand"
    $failures++
} elseif ($WhatIf) {
    Say "codex       would register agenthub -> $HubUrl/mcp, token from AGENTHUB_TOKEN  ($codexExe)"
} else {
    Invoke-Tool $codexExe @('mcp', 'remove', 'agenthub') | Out-Null
    $r = Invoke-Tool $codexExe @('mcp', 'add', 'agenthub', '--url', "$HubUrl/mcp",
                                 '--bearer-token-env-var', 'AGENTHUB_TOKEN')
    if ($r.Code -ne 0) { Say "codex       FAILED to register:`n$($r.Output)"; $failures++ }
    else { Say "codex       registered agenthub -> $HubUrl/mcp (token from AGENTHUB_TOKEN)" }
}

# --- 5. notifications: hooks for both agents ----------------------------------
# Run each candidate: 'python' on PATH is often the Microsoft Store alias, which exists
# but only prints "Python was not found".
$py = $null; $pyPre = @()
foreach ($cand in @(@('python'), @('py', '-3'), @('python3'))) {
    $pre = @($cand | Select-Object -Skip 1)
    foreach ($cmd in @(Get-Command $cand[0] -All -CommandType Application -ErrorAction SilentlyContinue)) {
        $v = Invoke-Tool $cmd.Source ($pre + @('-c', 'import sys; print(sys.version_info[0])'))
        if ($v.Code -eq 0 -and $v.Output.Trim() -eq '3') { $py = $cmd; $pyPre = $pre; break }
    }
    if ($py) { break }
}
if (-not $py) {
    Say "hooks       NOT INSTALLED - Python 3 is not installed (the 'python' on PATH is only the Microsoft Store placeholder)."
    Say "            Install it:  winget install -e --id Python.Python.3.12 --scope user"
    Say "            then open a NEW PowerShell and run this script and install-pointers.ps1 again."
    $failures++
} else {
    $pyArgs = $pyPre + @((Join-Path $PSScriptRoot 'install_hooks.py'), '--host', $HostPart)
    if ($WhatIf) { $pyArgs += '--dry-run' }
    $r = Invoke-Tool $py.Source $pyArgs
    if ($r.Code -ne 0) { Say "hooks       FAILED:`n$($r.Output)"; $failures++ }
    else {
        ($r.Output -split "`r?`n") | Where-Object { $_ -and $_ -notmatch '^watch ' } | ForEach-Object { Say "hooks       $_" }
        if (-not $WhatIf) { Say "hooks       Codex: approve the new hooks once with /hooks in Codex, or they will not run" }
    }
}

# --- 6. wake bridges, auto-started ------------------------------------------
if ($py) {
    $bArgs = $pyPre + @((Join-Path $PSScriptRoot 'install_bridge.py'), '--host', $HostPart)
    if ($WhatIf) { $bArgs += '--dry-run' }
    $r = Invoke-Tool $py.Source $bArgs
    ($r.Output -split "`r?`n") | Where-Object { $_ } | ForEach-Object { Say "bridge      $_" }
    if ($r.Code -ne 0) { $failures++ }
}

# --- 7. announce both agents --------------------------------------------------
foreach ($a in $Agents) {
    if ($WhatIf) { Say "hub_hello   would announce $($a.Addr)"; continue }
    try {
        $res = Invoke-Hub 'hub_hello' @{ as = $a.Addr }
        if ($res.isError) { Say "hub_hello   $($a.Addr) REFUSED: $($res.content[0].text)"; $failures++ }
        else { Say "hub_hello   $($a.Addr) is on the hub" }
    } catch {
        Say "hub_hello   $($a.Addr) FAILED: $($_.Exception.Message)"; $failures++
    }
}

Say ''
if ($failures) {
    Say "$failures step(s) failed; see above."
    exit 1
}
if ($WhatIf) {
    Say 'Checks passed. Run again without -WhatIf to register.'
} else {
    Say "Done. Restart Claude Code and Codex so they load the agenthub server and AGENTHUB_TOKEN."
    Say "Board: $HubUrl/board"
}
