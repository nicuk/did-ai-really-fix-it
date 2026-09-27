#!/usr/bin/env python3
"""
run_ci_locally.py — run this repo's push-triggered CI steps on a commit before it is pushed,
so a failing check is caught on this machine instead of turning the public badge red.

Called by .githooks/pre-push (enable once per clone: git config core.hooksPath .githooks).
Also runnable by hand:  python .githooks/run_ci_locally.py [COMMIT]   (default HEAD)

It reads the workflow files themselves, so it can't drift from CI: every `run:` step of every
workflow triggered on `push` runs, in order, in a temporary worktree of the exact commit being
pushed (never the working folder, which may hold uncommitted changes).

What it can't do locally, it says so and skips:
  - `uses:` steps (actions); checkout and setup-python are what the local worktree replaces
  - steps with an `if:` or with ${{ … }} expressions, which need GitHub's context
  - `pip install` lines (install once on this machine; a missing module then fails loudly)
Sibling repos that CI checks out into `plugins/<repo>` are cloned there from `../<repo>` (their
committed HEAD, which is what CI would see once pushed), and GITHUB_WORKSPACE is set, so each
step runs exactly as written.
If `claude` is on PATH and the repo is a plugin, `claude plugin validate --strict` runs too.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Git runs hooks with GIT_DIR (and sometimes GIT_WORK_TREE / GIT_INDEX_FILE) set. Inherited by
# a CI step, they redirect any `git` the step runs in another folder, such as a self-test's
# temporary fixture repo, into THIS repository. On 2026-09-27 that committed a self-test's
# fixtures onto main, set core.bare, and pushed. Nothing below needs them, so drop them.
for _k in [k for k in os.environ if k.startswith("GIT_")]:
    del os.environ[_k]

try:
    import yaml
except ImportError:
    sys.exit("run_ci_locally: PyYAML is needed to read the workflows (pip install pyyaml); push blocked")


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def push_steps(workflow: Path) -> list[tuple[str, str]]:
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
    triggers = doc.get("on", doc.get(True, {}))            # PyYAML reads a bare `on:` key as True
    if isinstance(triggers, str):
        triggers = {triggers: None}
    elif isinstance(triggers, list):
        triggers = {t: None for t in triggers}
    if "push" not in (triggers or {}):
        return []
    steps = []
    for job in (doc.get("jobs") or {}).values():
        for i, step in enumerate(job.get("steps") or []):
            name = step.get("name") or f"step {i + 1}"
            if "run" not in step:
                continue
            if "if" in step or "${{" in step["run"]:
                print(f"  skip  {name}  (needs GitHub's context)")
                continue
            steps.append((name, step["run"]))
    return steps


def main() -> int:
    repo = Path(git("rev-parse", "--show-toplevel", cwd=Path.cwd()))
    commit = sys.argv[1] if len(sys.argv) > 1 else "HEAD"
    sha = git("rev-parse", commit, cwd=repo)
    workflows = sorted((repo / ".github" / "workflows").glob("*.y*ml"))
    bash = shutil.which("bash") or "bash"
    tmp = Path(tempfile.mkdtemp(prefix="ci-local-"))
    wt = tmp / repo.name          # named like the repo: some checks read the folder name
    runner_temp = tmp / "runner-temp"
    runner_temp.mkdir()
    git("worktree", "add", "--detach", str(wt), sha, cwd=repo)
    failed: list[str] = []
    ran = 0
    try:
        for wf in workflows:
            steps = push_steps(wt / ".github" / "workflows" / wf.name) if (wt / ".github" / "workflows" / wf.name).exists() else []
            for name, script in steps:
                script = "\n".join(l for l in script.splitlines() if not l.strip().startswith("pip install"))
                # CI checks sibling repos out into plugins/<repo>; build the same folder here from
                # the local sibling clones, so the step runs unmodified.
                missing = []
                for sib in sorted(set(re.findall(r"\bplugins/([A-Za-z0-9._-]+)", script))):
                    src, dst = repo.parent / sib, wt / "plugins" / sib
                    if dst.exists():
                        continue
                    if not (src / ".git").exists():
                        missing.append(str(src))
                        continue
                    subprocess.run(["git", "clone", "-q", str(src), str(dst)], check=True, capture_output=True)
                if missing:
                    print(f"  skip  {name}  (sibling repo not on this machine: {', '.join(missing)})")
                    continue
                r = subprocess.run([bash, "-e", "-c", script], cwd=wt, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=600,
                                   env={**os.environ, "CI": "true",
                                        # the folders GitHub's runner provides, so steps that use them run as written
                                        "GITHUB_WORKSPACE": str(wt).replace("\\", "/"),
                                        "RUNNER_TEMP": str(runner_temp).replace("\\", "/")})
                ran += 1
                if r.returncode == 0:
                    print(f"  ok    {name}")
                else:
                    failed.append(name)
                    print(f"  FAIL  {name}")
                    print("\n".join("        " + l for l in (r.stdout + r.stderr).strip().splitlines()[-15:]))
        # A plugin push can drift from the family's shared parts, which cairn-principles only
        # checks daily. Run that check now, against this commit, so it can't go red tomorrow.
        drift = repo.parent / "cairn-principles" / "scripts" / "check_drift.py"
        if (wt / ".claude-plugin" / "plugin.json").exists() and drift.exists():
            r = subprocess.run([sys.executable, str(drift), str(wt)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300)
            ran += 1
            label = "matches the Cairn family's shared parts (cairn-principles drift check)"
            if r.returncode == 0:
                print(f"  ok    {label}")
            else:
                failed.append(label)
                print(f"  FAIL  {label}")
                print("\n".join("        " + l for l in (r.stdout + r.stderr).strip().splitlines()[-10:]))
        if (wt / ".claude-plugin" / "plugin.json").exists() and shutil.which("claude"):
            r = subprocess.run(["claude", "plugin", "validate", "--strict", "."], cwd=wt, capture_output=True, text=True)
            ran += 1
            label = "claude plugin validate --strict (local extra)"
            if r.returncode == 0:
                print(f"  ok    {label}")
            else:
                failed.append(label)
                print(f"  FAIL  {label}\n        " + (r.stdout + r.stderr).strip()[-400:])
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    if failed:
        print(f"\n{len(failed)} CI step(s) fail on {sha[:7]}; push blocked. Fix and commit, then push again.")
        return 1
    print(f"\nall {ran} local CI step(s) pass on {sha[:7]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
