"""
check_privacy.py — proves two things PRIVACY.md and AGENTS.md say about this repo, by reading it:

  - the skill's scripts import nothing that can reach the network, in any form
    (`import json, socket`, `from urllib import ...`, `__import__`, importlib);
  - no tracked file holds an absolute path from someone's machine.

A grep for `import socket` misses the first three forms above; this reads the syntax tree.
It finds the scripts itself (skills/*/scripts/*.py), so a new script is covered without an edit.
Run with --self-test to watch each rule fire on a planted case.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

NETWORK = {"socket", "ssl", "urllib", "urllib3", "http", "requests", "httpx", "aiohttp", "ftplib", "smtplib",
           "poplib", "imaplib", "telnetlib", "xmlrpc", "asyncio", "webbrowser", "importlib", "ctypes",
           "multiprocessing", "socketserver", "selectors"}
BANNED_CALLS = {"__import__", "eval", "exec", "compile"}
HOME_PATH = re.compile(r"(?:/Users/[A-Za-z]|/home/[a-z][\w.-]*/|\b[A-Za-z]:\\Users\\)")


def network_problems(source: str) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out += [f"line {node.lineno}: imports {a.name}, which can reach the network"
                    for a in node.names if a.name.split(".")[0] in NETWORK]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in NETWORK:
            out.append(f"line {node.lineno}: imports from {node.module}, which can reach the network")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
            out.append(f"line {node.lineno}: calls {node.func.id}(), which can import or run anything")
    return out


def home_paths(text: str) -> list[str]:
    return [m.group(0) for m in HOME_PATH.finditer(text)]


def tracked_files() -> list[str]:
    # A git hook exports GIT_DIR; inherited, it would point this at another repository.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True,
                         check=True, env=env).stdout
    return [f for f in out.split("\0") if f]


def self_test() -> int:
    planted = {
        "import-in-a-list": "import json, socket\n",
        "dotted-import": "import urllib.request\n",
        "from-import": "from http.client import HTTPSConnection\n",
        "dunder-import": "x = __import__('socket')\n",
        "importlib": "import importlib\n",
        "exec": "exec('import socket')\n",
    }
    clean = "import argparse, json, os, re, subprocess, sys, tempfile\nfrom pathlib import Path\n"
    # Assembled from pieces, so this file holds no path the check itself would flag.
    paths = {"mac-path": "see /" + "Users/alice/repo", "linux-path": "cloned to /" + "home/bob/work",
             "windows-path": "C:" + "\\Users\\carol\\repo"}
    failed = 0
    for label, src in planted.items():
        hit = bool(network_problems(src))
        print(f"{'ok  ' if hit else 'MISS'} {label}")
        failed += not hit
    for label, text in paths.items():
        hit = bool(home_paths(text))
        print(f"{'ok  ' if hit else 'MISS'} {label}")
        failed += not hit
    quiet = network_problems(clean) + home_paths("~/.claude/CLAUDE.md, /tmp/fixture, src/app.ts, /home/")
    print(f"{'ok  ' if not quiet else 'FAIL'} allowed-patterns-quiet" + (f": {quiet}" if quiet else ""))
    failed += bool(quiet)
    if failed:
        print(f"\ncheck_privacy self-test FAILED: {failed} case(s)", file=sys.stderr)
        return 1
    print(f"\ncheck_privacy self-test passed: {len(planted) + len(paths)} planted breaches caught, allowed patterns quiet")
    return 0


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return self_test()
    bad = []
    scripts = sorted(ROOT.glob("skills/*/scripts/*.py"))
    if not scripts:
        bad.append("no script found under skills/*/scripts/, so nothing was checked")
    for s in scripts:
        bad += [f"{s.relative_to(ROOT).as_posix()} {p}" for p in network_problems(s.read_text(encoding="utf-8"))]
    for rel in tracked_files():
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue                                   # binary, a folder link, or deleted
        bad += [f"{rel}: holds an absolute path from someone's machine ('{h}...')" for h in home_paths(text)]
    for b in bad:
        print("FAIL " + b)
    if bad:
        print("\nPRIVACY.md says the scripts make no network requests, and AGENTS.md that nothing private is "
              "committed; the files above say otherwise.", file=sys.stderr)
        return 1
    names = ", ".join(s.name for s in scripts)
    print(f"ok   {names}: no network imports in any form; no tracked file holds a personal path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
