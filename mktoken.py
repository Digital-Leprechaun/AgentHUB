#!/usr/bin/env python3
"""Add or rotate a token in data/tokens.json.

Two granularities, pick per machine:

  python mktoken.py claude@desk codex@desk   one token per vendor+host
  python mktoken.py "*@desk"                  one token for the whole machine

An exact vendor@host entry beats the host wildcard, so a box can share one token
and still pin a single agent to its own. Either way the token covers subagents,
since a subagent shares its parent's MCP connection and holds no credential.

Note what a token actually proves: on a single-user box every CLI can read every
other CLI's config, so agents on one host are mutually spoofable however many you
issue. The boundary it really draws is around the MACHINE.

  python mktoken.py --show
  python mktoken.py --rotate claude@desk

The hub re-reads the file on change; no restart needed.
"""
import argparse
import json
import os
import re
import secrets
import sys

PATH = os.environ.get("HUB_TOKENS_FILE", os.path.join("data", "tokens.json"))
FAMILY = re.compile(r"^(?:\*|[a-z0-9][a-z0-9._-]{0,31})@[a-z0-9][a-z0-9._-]{0,31}$", re.I)


def load() -> dict:
    try:
        with open(PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def save(d: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(PATH)), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, PATH)
    try:
        os.chmod(PATH, 0o600)
    except OSError:
        pass  # Windows / mounted volumes


def main() -> None:
    p = argparse.ArgumentParser(description="manage AgentHub family tokens")
    p.add_argument("families", nargs="*", help="vendor@host, e.g. claude@desk")
    p.add_argument("--show", action="store_true", help="print the current families and tokens")
    p.add_argument("--rotate", action="store_true", help="replace a token that already exists")
    a = p.parse_args()

    d = load()
    if a.show or not a.families:
        if not d:
            print(f"{PATH}: no tokens yet -- the hub runs in OPEN mode (any address, no auth).")
            print("Add one with:  python mktoken.py claude@desk")
            return
        print(f"{PATH}:")
        for k in sorted(d):
            print(f"  {k:28} {d[k]}")
        return

    made = []
    for fam in a.families:
        fam = fam.lower().strip()
        if not FAMILY.match(fam):
            sys.exit(f"bad family {fam!r}: expected vendor@host (claude@desk) or a "
                     "host wildcard (*@desk). No /role -- a token covers subagents.")
        if fam in d and not a.rotate:
            print(f"  {fam:28} {d[fam]}   (unchanged; --rotate to replace)")
            continue
        d[fam] = secrets.token_urlsafe(24)
        made.append(fam)
        print(f"  {fam:28} {d[fam]}   {'ROTATED' if a.rotate else 'new'}")
    save(d)
    if made:
        print(f"\nWrote {PATH}. Put each token in that machine's MCP config as a bearer token.")
        print("The hub picks the file up on its own; no restart needed.")


if __name__ == "__main__":
    main()
