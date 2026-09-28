"""
check_claims.py — the facts this repo states about itself, checked against their sources, so a
number typed into prose can't drift from what it describes:

  - every "N checks" in README.md, plugin.json and the demo images equals the number of checks
    the script's own --self-test reports;
  - plugin.json's version heads CHANGELOG.md.

Run with --self-test to watch each matcher fire on a planted case.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COUNT_CLAIM = re.compile(r"\b(\d+)\s+(?:self-tested\s+|planted\s+)?(?:checks|planted defects)\b")
FIRED = re.compile(r"self-test passed: (\d+)/(\d+) checks fired")


def count_mismatches(text: str, n: int) -> list[str]:
    return [m.group(0) for m in COUNT_CLAIM.finditer(text) if int(m.group(1)) != n]


def changelog_head(changelog: str) -> str:
    return next((ln for ln in changelog.splitlines() if ln.startswith("## ")), "")


def self_test() -> int:
    cases = [
        ("count-drift-caught", count_mismatches("fires all 55 checks; 55 planted defects", 56) != []),
        ("count-match-quiet", count_mismatches("fires all 55 checks", 55) == []),
        ("unrelated-numbers-quiet", count_mismatches("a 0-10 score, 504 files, 22 dead files", 55) == []),
        ("fired-parsed", FIRED.search("self-test passed: 11/11 checks fired").group(2) == "11"),
        ("changelog-head-found", changelog_head("# Changelog\n\n## 1.2.0 (2026-09-27)\n## 1.1.0\n") == "## 1.2.0 (2026-09-27)"),
    ]
    for name, ok in cases:
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
    failed = [c for c, ok in cases if not ok]
    if failed:
        print(f"\ncheck_claims self-test FAILED: {len(failed)} case(s)", file=sys.stderr)
        return 1
    print(f"\ncheck_claims self-test passed: all {len(cases)} cases held")
    return 0


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return self_test()
    bad: list[str] = []
    scripts = sorted(ROOT.glob("skills/*/scripts/*.py"))
    if len(scripts) != 1:
        print(f"FAIL expected one script under skills/*/scripts/, found {len(scripts)}")
        return 1
    # A git hook exports GIT_DIR; the self-test runs git, so it must not inherit it.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    run = subprocess.run([sys.executable, str(scripts[0]), "--self-test"], capture_output=True, text=True,
                         cwd=ROOT, env=env)
    fired = FIRED.search(run.stdout)
    if not fired:
        print(f"FAIL {scripts[0].name} --self-test did not pass, so there is no check count to compare")
        return 1
    n = int(fired.group(2))
    stated = [ROOT / "README.md", ROOT / ".claude-plugin" / "plugin.json", *sorted((ROOT / "assets").glob("*.svg"))]
    for p in stated:
        for claim in count_mismatches(p.read_text(encoding="utf-8"), n):
            bad.append(f"{p.relative_to(ROOT).as_posix()}: says '{claim}', but the self-test reports {n} checks")

    version = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    head = changelog_head((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    if not head.startswith(f"## {version} "):
        bad.append(f"CHANGELOG.md: newest entry is '{head}', plugin.json says {version}")

    for b in bad:
        print("FAIL " + b)
    if not bad:
        print(f"ok   {n} checks as stated, version {version} heads the changelog")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
