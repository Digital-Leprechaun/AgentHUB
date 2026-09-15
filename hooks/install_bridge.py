#!/usr/bin/env python3
"""Installs the AgentHub wake bridges and makes them start automatically.

One bridge per platform this machine actually has:

  codex@<host>   woken with `codex queue`      -> "AgentHub Codex Bridge"
  claude@<host>  woken with `claude --bg --resume` -> "AgentHub Claude Bridge"

  - copies agenthub_wake_bridge.py into ~/.agenthub/;
  - Windows: a Scheduled Task per bridge, started at logon (windowless, via pythonw),
    restarted if it dies, then (re)started now;
  - Linux: a systemd user service per bridge with Restart=always, enabled and (re)started,
    with lingering on so it survives logout.

Safe to run again; a rerun restarts the bridges so they pick up a new version.

    python install_bridge.py [--host desk] [--dry-run]

It needs ~/.agenthub/credentials.json (written by install_hooks.py), because an
autostarted bridge does not inherit a shell's environment.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
AGENT_DIR = os.path.join(HOME, ".agenthub")
SCRIPT = os.path.join(AGENT_DIR, "agenthub_wake_bridge.py")
SITE = os.path.join(AGENT_DIR, "site.json")
OLD_SCRIPTS = [os.path.join(AGENT_DIR, "agenthub_codex_bridge.py")]


def load_site() -> dict:
    """Site settings kept out of source control (see site.example.json): the first
    site.json found in $AGENTHUB_SITE, next to this script, or in ~/.agenthub/."""
    for path in (os.environ.get("AGENTHUB_SITE", ""), os.path.join(HERE, "site.json"),
                 os.path.join(AGENT_DIR, "site.json")):
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                pass
    return {}


def default_host() -> str:
    """The hub host part for this machine: the first site.json `hosts` rule whose
    `pattern` matches the hostname gives its `host`, or else its first capture group;
    with no matching rule, the lowercased hostname."""
    name = socket.gethostname()
    for rule in load_site().get("hosts", []):
        m = re.match(rule.get("pattern", "(?!)"), name, re.I)
        if m:
            return (rule.get("host") or m.group(1)).lower()
    if re.match(r"^[A-Za-z0-9-]+$", name):
        return name.lower()
    sys.exit(f"cannot derive the host part from hostname {name!r}; pass --host")


def find_codex() -> str | None:
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", os.path.join(HOME, "AppData", "Local"))
        hits = glob.glob(os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe"))
        if hits:
            return max(hits, key=os.path.getmtime)
    return shutil.which("codex")


def find_claude() -> str | None:
    hit = shutil.which("claude")
    if hit:
        return hit
    for cand in (os.path.join(HOME, ".local", "bin", "claude.exe"),
                 os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd")):
        if cand and os.path.exists(cand):
            return cand
    return None


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def install_windows(vendor: str, addr: str, dry: bool) -> int:
    task = f"AgentHub {vendor.capitalize()} Bridge"
    log = os.path.join(AGENT_DIR, f"{vendor}_bridge.log")
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    args = f'"{SCRIPT}" --as {addr} --log "{log}"'
    if dry:
        print(f"would register scheduled task '{task}': {pyw} {args} (at logon, restart on failure)")
        return 0
    ps = f"""
$ErrorActionPreference = 'Stop'
$user = "$env:USERDOMAIN\\$env:USERNAME"
$a = New-ScheduledTaskAction -Execute '{pyw}' -Argument '{args}' -WorkingDirectory '{AGENT_DIR}'
$t = New-ScheduledTaskTrigger -AtLogOn -User $user
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
       -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
       -MultipleInstances IgnoreNew
$p = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName '{task}' -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null
Stop-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
  Where-Object {{ $_.CommandLine -like '*agenthub_*bridge.py*--as {addr}*' }} |
  ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
Start-Sleep -Seconds 1
Start-ScheduledTask -TaskName '{task}'
Start-Sleep -Seconds 4
$running = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
  Where-Object {{ $_.CommandLine -like '*agenthub_wake_bridge.py*--as {addr}*' }}
if ($running) {{ "running    {addr} pid $(@($running)[0].ProcessId)" }}
else {{ "NOT RUNNING {addr} - see {log}"; exit 3 }}
"""
    r = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        print(f"FAILED to register or start '{task}':\n{out}")
        return 1
    print(f"autostart  scheduled task '{task}' (at logon, restart on failure)")
    print(out)
    return 0


def install_linux(vendor: str, addr: str, dry: bool) -> int:
    unit = f"agenthub-{vendor}-bridge"
    unit_dir = os.path.join(HOME, ".config", "systemd", "user")
    unit_path = os.path.join(unit_dir, f"{unit}.service")
    log = os.path.join(AGENT_DIR, f"{vendor}_bridge.log")
    python = shutil.which("python3") or sys.executable
    if dry:
        print(f"would install systemd user service {unit} ({addr}) and enable it")
        return 0
    os.makedirs(unit_dir, exist_ok=True)
    with open(unit_path, "w", encoding="utf-8") as fh:
        fh.write(f"""[Unit]
Description=AgentHub wake bridge for {addr}
After=network-online.target

[Service]
ExecStart={python} {SCRIPT} --as {addr} --log {log}
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
""")
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", unit],
                ["systemctl", "--user", "restart", unit]):
        r = run(cmd, env=env)
        if r.returncode != 0:
            print(f"FAILED: {' '.join(cmd)}:\n{(r.stdout + r.stderr).strip()}")
            return 1
    state = run(["systemctl", "--user", "is-active", unit], env=env).stdout.strip()
    print(f"autostart  systemd user service {unit} ({addr}); running    {state}")
    return 0 if state == "active" else 3


def enable_linger() -> None:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    user = os.environ.get("USER") or os.path.basename(HOME)
    if run(["loginctl", "show-user", user, "-p", "Linger"], env=env).stdout.strip() == "Linger=yes":
        print("linger     already enabled")
        return
    if run(["loginctl", "enable-linger", user], env=env).returncode == 0:
        print("linger     enabled (bridges run even when nobody is logged in)")
    else:
        print(f"linger     NOT enabled -- bridges stop when {user} logs out. "
              f"Fix once with: sudo loginctl enable-linger {user}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", help="hub host part, e.g. desk; default from site.json or the hostname")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    host = a.host or default_host()

    present = [(v, exe) for v, exe in (("codex", find_codex()), ("claude", find_claude())) if exe]
    if not present:
        print("skipped    neither Codex nor Claude is installed on this machine")
        return
    if not os.path.exists(os.path.join(AGENT_DIR, "credentials.json")):
        sys.exit("FAILED: ~/.agenthub/credentials.json is missing; run install_hooks.py first")

    src = os.path.join(HERE, "agenthub_wake_bridge.py")
    same = os.path.exists(SCRIPT) and open(src, "rb").read() == open(SCRIPT, "rb").read()
    if same:
        print(f"unchanged  {SCRIPT}")
    elif a.dry_run:
        print(f"would copy  {SCRIPT}")
    else:
        os.makedirs(AGENT_DIR, exist_ok=True)
        shutil.copy2(src, SCRIPT)
        print(f"copied     {SCRIPT}")
    site_src = os.path.join(HERE, "site.json")
    if os.path.isfile(site_src) and os.path.abspath(site_src) != os.path.abspath(SITE):
        if a.dry_run:
            print(f"would copy  {SITE}")
        else:
            shutil.copy2(site_src, SITE)
            print(f"copied     {SITE}")
    for old in OLD_SCRIPTS:
        if os.path.exists(old) and not a.dry_run:
            os.remove(old)
            print(f"removed    {old} (superseded)")

    rc = 0
    for vendor, exe in present:
        addr = f"{vendor}@{host}"
        print(f"{vendor:10} {exe}")
        rc |= (install_windows(vendor, addr, a.dry_run) if os.name == "nt"
               else install_linux(vendor, addr, a.dry_run))
    if os.name != "nt" and not a.dry_run:
        enable_linger()
    sys.exit(rc)


if __name__ == "__main__":
    main()
