#!/usr/bin/env python3
"""
verify_claims.py — check what a coding agent said it did against what the repository shows.

It is a *locator*, not a judge. It finds claims that the diff contradicts or leaves
unproven; the skill (SKILL.md) turns those into verdicts by reading code and running tests.

Usage:
  python verify_claims.py claims  --repo PATH --range BASE..HEAD [--text FILE] [--json | --markdown] [--smoke CMD]
  python verify_claims.py orphans --repo PATH [--entry GLOB ...] [--alias @=.] [--json] [--smoke CMD]
  python verify_claims.py --self-test

`claims` reads the commit messages in the range (and an optional PR description or agent
summary in --text), splits them into claims, and checks each against the diff:
  file-not-in-diff        a file the claim names exists but the range never touched it
  file-does-not-exist     a file the claim names exists nowhere in the repository
  tests-claimed-none-changed   "added tests" but no test file was added or changed
  tests-never-import-changed   changed tests import none of the changed source files
  skip-added              a skip / only / todo marker was added to a test in the range
  tests-removed           test files were deleted, or assertions went down, in the range
  removed-still-referenced     a file deleted in the range is still imported at HEAD
  fix-without-test        a fix is claimed and no test changed to prove it
  behaviour-claim-tests-edited "no behaviour change" but existing assertions were edited
  assertion-word          "verified", "confirmed", "works", "always", "fully" with no proof in the diff

`orphans` builds the import graph (JS/TS and Python), then:
  orphan                  a file nothing imports or names, repeated until nothing new appears,
                          so dead code kept alive only by other dead code is found in rounds
  twin                    one exported name defined in several files, with which are reachable
                          from an entry point, so the live one is not deleted by name
Import aliases come from --alias and, automatically, from every tsconfig/jsconfig
`compilerOptions.paths` and `baseUrl` in the repository (comments and trailing commas allowed).

--smoke "COMMAND" starts the app once before you trust an unreachable file: it adds a temporary
`git worktree` of HEAD (or the range's head) in the system temp folder, runs COMMAND there under
a timeout, reports whether it started, and removes the worktree, even on failure or timeout.

Read-only: it reads files and runs `git log` / `git diff` / `git show` / `git ls-files` in
the repository you name. It writes nothing there and makes no network requests. Only --smoke
runs anything else: your own COMMAND, in the temporary worktree, never in your checkout.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

CODE_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py")
SKIP_DIRS = {"node_modules", ".git", ".next", "dist", "build", ".venv", "venv", "__pycache__",
             ".turbo", "coverage", "out", ".vercel", ".claude"}
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|e2e)(/|$)|\.(test|spec)\.[a-z]+$|(^|/)test_[^/]+\.py$|_test\.py$", re.I)
DEFAULT_ENTRIES = [
    "app/**/page.*", "app/**/layout.*", "app/**/route.*", "app/**/loading.*", "app/**/error.*",
    "app/**/not-found.*", "app/**/template.*", "app/**/default.*", "app/**/opengraph-image.*",
    "app/**/sitemap.*", "app/**/robots.*", "app/**/manifest.*", "pages/**", "src/app/**/page.*",
    "src/app/**/layout.*", "src/app/**/route.*", "src/pages/**", "middleware.*", "src/middleware.*",
    "instrumentation.*", "scripts/**", "bin/**", "*.config.*", "**/__main__.py", "main.py",
    "manage.py", "index.*", "src/index.*", "src/main.*", "server.*", "src/server.*",
    "**/main.py", "**/app.py", "**/wsgi.py", "**/asgi.py", "**/manage.py",
    "app/**/global-error.*", "src/app/**/global-error.*", "**/*.d.ts", "jest.setup.*", "vitest.setup.*",
    ".github/**", "supabase/functions/**",
]


def clean_env() -> dict:
    """The environment for every git call, minus inherited GIT_* variables.

    Git hooks run with GIT_DIR (and sometimes GIT_WORK_TREE or GIT_INDEX_FILE) set. A
    `git -C <temp folder>` call that inherits them acts on the hook's repository instead,
    which is how a pre-push hook running this self-test once committed its fixtures onto a
    real main branch and pushed them (2026-09-27). Every git call here uses this.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git_run(cmd: list, **kw):
    kw.setdefault("env", clean_env())
    return subprocess.run(cmd, **kw)


def run_git(repo: Path, *args: str) -> str:
    return git_run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=True).stdout


def is_test(path: str) -> bool:
    return bool(TEST_PATH.search(path.replace("\\", "/")))


# ------------------------------------------------------------------ claims

CLAIM_KINDS = [
    ("tests-added", re.compile(r"\b(add(ed|s|ing)?|wr(ote|ite|itten)|new|extend(ed)?)\b[^.\n]{0,40}\btests?\b|\btest coverage\b|\bcovered by (a |new )?tests?\b", re.I)),
    ("tests-pass", re.compile(r"\b(all\s+)?(the\s+)?tests?\s+(now\s+)?(pass|passes|passing|green)\b|\bsuite\s+(is\s+)?(passes|passing|green)\b|\bci\s+(is\s+)?green\b", re.I)),
    ("no-behaviour-change", re.compile(r"\bno\s+(behaviou?r(al)?|functional)\s+change|\bpure(ly)?\s+refactor|\brefactor(ed|ing)?\s+only\b", re.I)),
    ("removed", re.compile(r"\b(remov(e|ed|es|ing)|delet(e|ed|es|ing)|dead\s+code|unused|clean(ed)?\s+up)\b", re.I)),
    # A fix CLAIM, not the word: "Fixed the bug", "fix(auth): …", "this fixes the crash".
    # Not "thresholds are fixed", "resolves to nothing", "the fix exited" (tested on real commits).
    ("fixed", re.compile(r"^(fix(ed|es)?|resolv(ed|es)|patch(ed|es)?)\b(?!\s+(to|at|before|after|by)\b)|^fix(\([^)]*\))?!?:"
                         r"|\b(i|we|this( commit| change| pr)?|it|the agent)\s+(fix(ed|es)?|resolv(ed|es)|patch(ed|es)?)\b"
                         r"|\b(fix(ed|es)?|resolv(ed|es)|patch(ed|es)?)\s+(the|a|an|this|that|its|our)\s", re.I)),
    ("wired", re.compile(r"\b(wired|hooked\s+up|now\s+(calls|called|uses)|called\s+by|integrated|connected)\b", re.I)),
]
NAMES_ITS_PROOF = re.compile(r"\b(verified|confirmed|proved|proven|checked)\s+(by|with|via|using|against)\b|\b(ran|running|measured|"
                             r"\d+\s*(of|/)\s*\d+|output|exit code|red then green|fails? (before|without))\b", re.I)
ASSERTION_WORD = re.compile(r"\b(verified|confirmed|tested manually|works( now| correctly| as expected)?|always|fully|everywhere|all (cases|paths|callers))\b", re.I)
PATH_TOKEN = re.compile(r"`([^`\s]+\.[A-Za-z0-9]{1,6})`|(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.(?:tsx?|jsx?|mjs|cjs|py|json|ya?ml|sql|md|css|toml))\b")
SKIP_MARKER = re.compile(r"\b(it|test|describe)\.(skip|only|todo)\s*\(|\bx(it|describe|test)\s*\(|@pytest\.mark\.(skip|xfail)|pytest\.skip\(|\.skipIf\(|@unittest\.skip")
# Where tests get selected or dropped without a skip marker in the test itself.
TEST_CONFIG_FILE = re.compile(r"(^|/)(conftest\.py|pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|package\.json|"
                              r"(jest|vitest|karma|playwright|cypress)\.config\.[a-z]+|\.mocharc[.\w]*)$|"
                              r"(^|/)(tests?|__tests__)/__init__\.py$", re.I)
TEST_SELECTION = re.compile(r"load_tests|collect_ignore|--deselect|\s-k\s|testPathIgnorePatterns|testMatch|testRegex|"
                            r"testNamePattern|--grep|exclude|ignore|skip|only|addopts|norecursedirs|\"test\"\s*:", re.I)
ASSERT_LINE = re.compile(r"\bexpect\s*\(|\bassert\b|\bassert\w*\s*\(|\.should\b|\btoMatchSnapshot\b")


@dataclass
class Claim:
    text: str
    kinds: list[str]
    paths: list[str]
    findings: list[dict] = field(default_factory=list)
    source: str = ""          # the commit it came from, or "summary" for --text

    @property
    def status(self) -> str:
        if any(f["level"] == "CONTRADICTED" for f in self.findings):
            return "CONTRADICTED"
        if any(f["level"] == "UNPROVEN" for f in self.findings):
            return "UNPROVEN"
        return "NO CONTRADICTION FOUND"


def split_claims(text: str) -> list[str]:
    # Commit bodies are hard-wrapped at ~72 columns: rejoin each paragraph or bullet
    # before splitting into sentences, or one claim becomes three fragments.
    blocks: list[str] = []
    cur: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or re.match(r"^([-*•>]|\d+[.)])\s", line):
            if cur:
                blocks.append(" ".join(cur))
            cur = [re.sub(r"^([-*•>]|\d+[.)])\s+", "", line)] if line else []
        else:
            cur.append(line)
    if cur:
        blocks.append(" ".join(cur))
    out = []
    for b in blocks:
        if b.lower().startswith(("co-authored-by", "signed-off-by", "merge ")):
            continue
        for part in re.split(r"(?<=[.!?;])\s+(?=[A-Z`])", b):
            part = part.strip()
            if len(part) > 3:
                out.append(part)
    return out


CHANGE_VERB = re.compile(r"\b(fix(ed|es)?|add(ed|s)?|remov(ed|es)|delet(ed|es)|updat(ed|es)|chang(ed|es)|edit(ed|s)?|"
                         r"refactor(ed|s)?|renam(ed|es)|mov(ed|es)|wir(ed|es)|rewr(ote|ites|itten)|patch(ed|es)?|"
                         r"implement(ed|s)?|creat(ed|es)|introduc(ed|es)|replac(ed|es)|touch(ed|es))\b", re.I)
NEGATION = re.compile(r"\b(not|never|no|none|untouched|unchanged|kept|keep|keeps|left|existing|live)\b", re.I)


def claimed_changed(sentence: str, path: str) -> bool:
    """Is `path` the object of a change verb in its own clause, e.g. 'Fixed the bug in `x.ts`'?
    A path named as context ('the live one is x.ts', 'Kept: x.ts') is not a claim about x.ts."""
    i = sentence.find(path)
    if i < 0:
        return False
    clause = re.split(r"[;:,()]|\bthe live\b|\bbut\b", sentence[:i])[-1]
    return bool(CHANGE_VERB.search(clause)) and not NEGATION.search(clause)


def classify(sentence: str) -> Claim:
    kinds = [k for k, rx in CLAIM_KINDS if rx.search(sentence)]
    paths = [a or b for a, b in PATH_TOKEN.findall(sentence)]
    # "Verified by interrupting a run" names its own proof; "Verified it works" doesn't.
    if ASSERTION_WORD.search(sentence) and not NAMES_ITS_PROOF.search(sentence):
        kinds.append("assertion-word")
    return Claim(sentence, kinds, paths)


@dataclass
class Diff:
    status: dict[str, str]           # path -> A/M/D/R
    added: dict[str, list[str]]      # path -> added lines
    removed: dict[str, list[str]]    # path -> removed lines
    head_files: set[str]

    @property
    def touched(self) -> set[str]:
        return set(self.status)

    def tests(self, statuses: str = "AMR") -> set[str]:
        return {p for p, s in self.status.items() if is_test(p) and s[0] in statuses}

    def sources(self) -> set[str]:
        return {p for p, s in self.status.items() if not is_test(p) and p.endswith(CODE_EXT) and s[0] in "AMR"}


def read_diff(repo: Path, rng: str) -> Diff:
    status: dict[str, str] = {}
    for line in run_git(repo, "diff", "--name-status", "-M", rng).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            status[parts[-1]] = parts[0]
            if parts[0].startswith("R") and len(parts) == 3:
                status[parts[1]] = "D"
    added: dict[str, list[str]] = {}
    removed: dict[str, list[str]] = {}
    cur = None
    for line in run_git(repo, "diff", "-U0", "-M", rng).splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("--- "):
            if line.startswith("--- a/"):
                cur_old = line[6:]
                removed.setdefault(cur_old, [])
        elif line.startswith("+") and cur:
            added.setdefault(cur, []).append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            # attribute removed lines to the most recent old path
            key = cur or next(reversed(removed), None)
            if key:
                removed.setdefault(key, []).append(line[1:])
    head = rng.split("..")[-1] or "HEAD"
    head_files = set(run_git(repo, "ls-tree", "-r", "--name-only", head).splitlines())
    return Diff(status, added, removed, head_files)


def resolve_claimed(path: str, files: set[str]) -> list[str]:
    p = path.lstrip("./")
    if p in files:
        return [p]
    return sorted(f for f in files if f.endswith("/" + p) or f == p)


def import_specs(text: str) -> list[str]:
    # `export … from` re-exports count: an index file that re-exports a module keeps it alive.
    specs = re.findall(r"""(?:\b(?:import|export)\s[^'";]*?\sfrom\s*|import\s*\(\s*|require\s*\(\s*|^\s*import\s+)['"]([^'"]+)['"]""", text, re.M)
    for mod, names in re.findall(r"^[ \t]*from\s+([\w.]+)\s+import\s+(\([^)]*\)|[^\n#;]*)", text, re.M):
        # `from .models import M` is ./models; `from ..x import y` is ../x; `from . import helpers`
        # and `from pkg import submodule` name a module after `import`, so each name is a candidate too
        # (a name that is a function, not a module, resolves to no file and adds no edge).
        dots = len(mod) - len(mod.lstrip("."))
        rel = ("./" if dots == 1 else "../" * (dots - 1)) if dots else ""
        base = rel + mod[dots:].replace(".", "/")
        if mod[dots:]:
            specs.append(base)
        for part in names.strip("()").split(","):
            words = part.split()
            if words and words[0].isidentifier():
                specs.append(f"{base}/{words[0]}" if mod[dots:] else rel + words[0])
    specs += [m.replace(".", "/") for m in re.findall(r"^\s*import\s+([\w.]+)", text, re.M)]
    return specs


def spec_hits(spec: str, target: str) -> bool:
    """Loose match: does an import specifier plausibly point at this file?"""
    stem = re.sub(r"\.(tsx?|jsx?|mjs|cjs|py)$", "", target)
    stem = re.sub(r"/index$", "", stem)
    s = spec.split("?")[0].lstrip("@~").lstrip("/")
    s = re.sub(r"^(\.\./|\./)+", "", s)
    s = re.sub(r"\.(tsx?|jsx?|mjs|cjs|py)$", "", s)
    return bool(s) and (stem == s or stem.endswith("/" + s))


def extract(text: str, source: str) -> list[Claim]:
    out = []
    for s in split_claims(text):
        c = classify(s)
        if c.kinds or c.paths:
            c.source = source
            out.append(c)
    return out


def check_claims(repo: Path, rng: str, extra_text: str = "") -> list[Claim]:
    """Check each commit's message against that commit's own diff, and the summary text
    (a PR description or the agent's chat summary) against the whole range. A range-wide
    diff alone hides an edit one commit made and a later commit undid."""
    claims: list[Claim] = []
    for sha in run_git(repo, "rev-list", "--reverse", "--no-merges", rng).split():
        parents = run_git(repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]
        if not parents:
            continue
        msg = run_git(repo, "log", "-1", "--format=%B", sha)
        claims += evaluate(repo, f"{parents[0]}..{sha}", extract(msg, sha[:7]))
    if extra_text.strip():
        claims += evaluate(repo, rng, extract(extra_text, "summary"))
    return claims


def evaluate(repo: Path, rng: str, claims: list[Claim]) -> list[Claim]:
    if not claims:
        return claims
    diff = read_diff(repo, rng)

    tests_changed = diff.tests()
    tests_deleted = diff.tests("D")
    skip_added = [(p, l.strip()) for p, ls in diff.added.items() if is_test(p) for l in ls if SKIP_MARKER.search(l)]
    assert_added = sum(1 for p, ls in diff.added.items() if is_test(p) for l in ls if ASSERT_LINE.search(l))
    assert_removed = sum(1 for p, ls in diff.removed.items() if is_test(p) for l in ls if ASSERT_LINE.search(l))
    head = rng.split("..")[-1] or "HEAD"
    head_text: dict[str, str] = {}

    def head_read_many(paths: list[str]) -> None:
        """Read many blobs at `head` in one `git cat-file --batch` process."""
        need = [p for p in paths if p not in head_text]
        if not need:
            return
        out = git_run(["git", "-C", str(repo), "cat-file", "--batch"],
                             input="".join(f"{head}:{p}\n" for p in need).encode("utf-8"),
                             capture_output=True, check=True).stdout
        pos = 0
        for p in need:
            nl = out.index(b"\n", pos)
            header = out[pos:nl].decode("utf-8", "replace").split()
            pos = nl + 1
            if len(header) >= 3 and header[1] == "blob":
                size = int(header[2])
                head_text[p] = out[pos:pos + size].decode("utf-8", "replace")
                pos += size + 1
            else:
                head_text[p] = ""

    def head_read(p: str) -> str:
        head_read_many([p])
        return head_text[p]

    # Which files still import something deleted in the range: computed once, not per claim.
    deleted_code = [p for p, s in diff.status.items() if s[0] == "D" and p.endswith(CODE_EXT)]
    added_code = [p for p, s in diff.status.items() if s[0] == "A" and p.endswith(CODE_EXT) and not is_test(p)
                  and not p.endswith("__init__.py")]
    still_used: dict[str, list[str]] = {}
    unimported_new: list[str] = []
    want_removed = bool(deleted_code) and any("removed" in c.kinds for c in claims)
    want_wired = bool(added_code) and any("wired" in c.kinds for c in claims)
    if want_removed or want_wired:
        code_at_head = sorted(f for f in diff.head_files if f.endswith(CODE_EXT))
        head_read_many(code_at_head)
        specs_at_head = {f: import_specs(head_text[f]) for f in code_at_head}
        for d in deleted_code if want_removed else []:
            users = [f for f, specs in specs_at_head.items() if f != d and any(spec_hits(s, d) for s in specs)]
            if users:
                still_used[d] = sorted(users)
        for a in added_code if want_wired else []:
            if not any(f != a and not is_test(f) and any(spec_hits(s, a) for s in specs)
                       for f, specs in specs_at_head.items()):
                unimported_new.append(a)

    for c in claims:
        add = lambda level, check, msg: c.findings.append({"level": level, "check": check, "msg": msg})
        for p in c.paths:
            changed_claim = claimed_changed(c.text, p)
            hits = resolve_claimed(p, diff.head_files | diff.touched)
            if not hits:
                add("CONTRADICTED" if changed_claim else "UNPROVEN", "file-does-not-exist",
                    f"`{p}` exists nowhere in the repository at {head}, and the range did not delete it")
            elif changed_claim and not any(h in diff.touched for h in hits):
                add("CONTRADICTED", "file-not-in-diff", f"the claim says `{p}` changed, but this range never touched it")
        if "tests-added" in c.kinds and not tests_changed:
            add("CONTRADICTED", "tests-claimed-none-changed", "tests are claimed, but no test file was added or changed in the range")
        if ("tests-added" in c.kinds or "fixed" in c.kinds) and tests_changed and diff.sources():
            importing = [t for t in tests_changed
                         if any(spec_hits(s, src) for s in import_specs(head_read(t)) for src in diff.sources())]
            if not importing:
                add("UNPROVEN", "tests-never-import-changed",
                    f"the changed tests ({', '.join(sorted(tests_changed))}) import none of the changed source files "
                    f"({', '.join(sorted(diff.sources()))}), so they cannot fail for the right reason")
        if "tests-pass" in c.kinds or "tests-added" in c.kinds or "fixed" in c.kinds:
            for p, l in skip_added:
                add("UNPROVEN", "skip-added", f"a skip/only/todo marker was added in {p}: `{l[:90]}`")
            if tests_deleted:
                add("UNPROVEN", "tests-removed", f"test files were deleted in the range: {', '.join(sorted(tests_deleted))}")
            elif assert_removed > assert_added:
                add("UNPROVEN", "tests-removed", f"assertions went down in the range ({assert_removed} removed, {assert_added} added)")
        if "tests-pass" in c.kinds or "tests-added" in c.kinds or "fixed" in c.kinds:
            for p, ls in diff.added.items():
                hits = [l.strip() for l in ls if TEST_SELECTION.search(l)]
                if TEST_CONFIG_FILE.search(p) and hits:
                    add("UNPROVEN", "test-selection-changed",
                        f"{p} changed which tests run (`{hits[0][:80]}`); count what ran, not what's written")
        if "fixed" in c.kinds and not tests_changed:
            add("UNPROVEN", "fix-without-test", "a fix is claimed and no test changed, so nothing shows it failed before and passes now")
        if "no-behaviour-change" in c.kinds:
            edited = [p for p in tests_changed if diff.status.get(p, "")[:1] == "M"
                      and any(ASSERT_LINE.search(l) for l in diff.removed.get(p, []))]
            if edited:
                add("UNPROVEN", "behaviour-claim-tests-edited",
                    f"existing assertions were edited in {', '.join(sorted(edited))}; a change that needs its tests rewritten usually changed behaviour")
        if "removed" in c.kinds:
            for d, users in still_used.items():
                add("CONTRADICTED", "removed-still-referenced",
                    f"{d} was deleted but is still imported at {head} by {', '.join(users[:5])}")
        if "wired" in c.kinds:
            for a in unimported_new:
                add("CONTRADICTED", "wired-but-unimported",
                    f"{a} was added in this range, and nothing outside tests imports it at {head}")
        if "assertion-word" in c.kinds:
            add("UNPROVEN", "assertion-word",
                "a word that asserts proof ('verified', 'works', 'always', …): what ran to verify it, and could it have failed?")
    return claims


# ------------------------------------------------------------------ orphans and twins

EXPORT_NAME = re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|const|let|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)", re.M)
PY_DEF = re.compile(r"^(?:def|class)\s+([A-Za-z_]\w*)", re.M)


def list_code_files(repo: Path) -> list[str]:
    try:
        files = run_git(repo, "ls-files").splitlines()
    except Exception as e:
        print(f"note: git ls-files failed ({e.__class__.__name__}); walking the tree instead", file=sys.stderr)
        files = []
        for root, dirs, fs in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            files += [Path(root, f).relative_to(repo).as_posix() for f in fs]
    return sorted(f for f in files if f.endswith(CODE_EXT) and not any(part in SKIP_DIRS for part in f.split("/")))


@dataclass
class Alias:
    """One import alias: `key` is an exact name ("@config") or a pattern with one "*" ("@core/*");
    each target is a repo-relative path where "*" becomes whatever the key's "*" matched."""
    key: str
    targets: list[str]
    scope: str = ""          # only importers under this folder use it ("" = the whole repo)
    js_only: bool = False    # tsconfig/jsconfig aliases never apply to Python imports
    origin: str = ""         # where it came from, for the report

    def candidates(self, spec: str, importer: str) -> list[str]:
        if (self.scope and not importer.startswith(self.scope + "/")) or (self.js_only and importer.endswith(".py")):
            return []
        if "*" in self.key:
            pre, post = self.key.split("*", 1)
            if len(spec) < len(pre) + len(post) or not spec.startswith(pre) or not spec.endswith(post):
                return []
            mid = spec[len(pre):len(spec) - len(post)]
            return [posixpath.normpath(t.replace("*", mid, 1)) for t in self.targets]
        return [posixpath.normpath(t) for t in self.targets] if spec == self.key else []


def flag_aliases(pairs: dict[str, str], origin: str) -> list[Alias]:
    """`--alias @=src` means both `@` and `@/…` resolve under src/."""
    out = []
    for prefix, target in pairs.items():
        target = target.rstrip("/") or "."
        out += [Alias(prefix, [target], origin=origin), Alias(prefix + "/*", [target + "/*"], origin=origin)]
    return out


def strip_jsonc(text: str) -> str:
    """tsconfig.json and jsconfig.json allow // and /* */ comments and trailing commas; json.loads doesn't."""
    def outside_strings(s: str, step) -> str:
        out: list[str] = []
        i, in_str = 0, False
        while i < len(s):
            ch = s[i]
            if in_str:
                out.append(ch)
                if ch == "\\" and i + 1 < len(s):
                    out.append(s[i + 1])
                    i += 1
                elif ch == '"':
                    in_str = False
                i += 1
            elif ch == '"':
                in_str = True
                out.append(ch)
                i += 1
            else:
                i = step(s, i, out)
        return "".join(out)

    def no_comments(s: str, i: int, out: list[str]) -> int:
        if s.startswith("//", i):
            j = s.find("\n", i)
            return len(s) if j < 0 else j
        if s.startswith("/*", i):
            j = s.find("*/", i + 2)
            return len(s) if j < 0 else j + 2
        out.append(s[i])
        return i + 1

    def no_trailing_commas(s: str, i: int, out: list[str]) -> int:
        if s[i] == "," and re.match(r"\s*[}\]]", s[i + 1:]):
            return i + 1
        out.append(s[i])
        return i + 1

    return outside_strings(outside_strings(text.lstrip("\ufeff"), no_comments), no_trailing_commas)


CONFIG_FILE = re.compile(r"(^|/)(tsconfig|jsconfig)(\.[\w.-]+)?\.json$")


def config_aliases(repo: Path, tracked: list[str]) -> list[Alias]:
    """Aliases from every tracked tsconfig/jsconfig: `paths` (relative to `baseUrl`, or to the
    config that declares them) and `baseUrl` itself, following relative `extends`. Each applies
    only to files under its config's folder, deepest config first."""
    def read(rel: str) -> dict | None:
        try:
            data = json.loads(strip_jsonc((repo / rel).read_text(encoding="utf-8", errors="replace")))
        except (OSError, ValueError) as e:
            print(f"note: could not read {rel} ({e.__class__.__name__}: {e}); its import aliases are ignored", file=sys.stderr)
            return None
        return data if isinstance(data, dict) else None

    def effective(rel: str, depth: int = 0) -> tuple[str | None, dict | None, str | None]:
        """(baseUrl, paths, folder paths are relative to when there is no baseUrl), after `extends`."""
        data = read(rel) if depth < 10 else None
        if data is None:
            return None, None, None
        here = posixpath.dirname(rel)
        base_url = paths = paths_dir = None
        ext = data.get("extends")
        for e in ext if isinstance(ext, list) else [ext]:
            if isinstance(e, str) and e.startswith("."):       # a package preset (@tsconfig/next) isn't in the repo
                p = posixpath.normpath(posixpath.join(here, e))
                p = p if p.endswith(".json") else p + ".json"
                if not p.startswith("..") and (repo / p).is_file():
                    b, pa, pd = effective(p, depth + 1)
                    base_url = b if b is not None else base_url
                    if pa is not None:
                        paths, paths_dir = pa, pd
        opts = data.get("compilerOptions") if isinstance(data.get("compilerOptions"), dict) else {}
        if isinstance(opts.get("baseUrl"), str):
            base_url = posixpath.normpath(posixpath.join(here, opts["baseUrl"]))
        if isinstance(opts.get("paths"), dict):
            paths, paths_dir = opts["paths"], here
        return base_url, paths, paths_dir

    configs = sorted({f for f in tracked if CONFIG_FILE.search(f) and not any(p in SKIP_DIRS for p in f.split("/"))}
                     | {n for n in ("tsconfig.json", "jsconfig.json") if (repo / n).is_file()})
    out: list[Alias] = []
    seen: set[tuple] = set()
    for cfg in configs:
        base_url, paths, paths_dir = effective(cfg)
        scope = posixpath.dirname(cfg)
        found: list[Alias] = []
        for key, targets in (paths or {}).items():
            targets = [targets] if isinstance(targets, str) else targets
            if not isinstance(targets, list) or key.count("*") > 1:
                continue
            root = base_url if base_url is not None else (paths_dir or "")
            ts = [posixpath.normpath(posixpath.join(root, t)) for t in targets if isinstance(t, str) and t.count("*") <= 1]
            if ts:
                found.append(Alias(key, ts, scope, True, cfg))
        if base_url is not None:                       # a bare `lib/x` resolves from baseUrl too
            found.append(Alias("*", [posixpath.join(base_url, "*")], scope, True, cfg + " baseUrl"))
        for al in found:
            sig = (al.key, tuple(al.targets), al.scope)
            if sig not in seen:
                seen.add(sig)
                out.append(al)
    # deepest config first, then the most specific pattern, as TypeScript does
    return sorted(out, key=lambda a: (-(a.scope.count("/") + bool(a.scope)), -len(a.key.split("*")[0]), a.key == "*"))


def resolve_spec(spec: str, importer: str, files: set[str], aliases: list[Alias]) -> str | None:
    spec = spec.split("?")[0]
    cands: list[str] = []
    if spec.startswith("."):
        base = os.path.normpath(os.path.join(os.path.dirname(importer), spec)).replace("\\", "/")
        cands.append(base)
    else:
        for al in aliases:
            cands += al.candidates(spec, importer)
        if "/" in spec or importer.endswith(".py"):
            cands.append(spec)                    # python dotted module, or a bare repo path
            cands.append("src/" + spec)           # src layout: top-level Python packages live under src/
    for c in cands:
        c = c.lstrip("./") if not c.startswith("..") else c
        for suffix in ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py",
                       "/index.ts", "/index.tsx", "/index.js", "/index.jsx", "/__init__.py"):
            if c + suffix in files:
                return c + suffix
    return None


def check_orphans(repo: Path, entries: list[str], aliases: dict[str, str],
                  fallback_aliases: dict[str, str] | None = None) -> dict:
    """`aliases` (from --alias) are tried first, then every tsconfig/jsconfig alias, then
    `fallback_aliases` (the @ and ~ defaults, used when no --alias is given)."""
    files = list_code_files(repo)
    fileset = set(files)
    texts = {f: (repo / f).read_text(encoding="utf-8", errors="replace") for f in files}
    # Every tracked text file can name a code file by path (configs, middleware matchers, workflows).
    try:
        all_tracked = run_git(repo, "ls-files").splitlines()
    except Exception:
        all_tracked = files
    raw_texts = {}
    for f in all_tracked:
        if f.endswith(CODE_EXT) or f.endswith((".json", ".yml", ".yaml", ".toml", ".sh", ".html")):
            try:
                raw_texts[f] = texts.get(f) or (repo / f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

    from_config = config_aliases(repo, all_tracked)
    alias_list = flag_aliases(aliases, "--alias") + from_config + flag_aliases(fallback_aliases or {}, "default")
    edges: dict[str, set[str]] = {f: set() for f in files}     # file -> files it imports
    for f, t in texts.items():
        for spec in import_specs(t):
            tgt = resolve_spec(spec, f, fileset, alias_list)
            if tgt and tgt != f:
                edges[f].add(tgt)

    # Index every path-like string literal that is NOT an import specifier (configs,
    # middleware matchers, dynamic paths) once, so each lookup is a set membership test.
    def norm(s: str) -> str:
        s = re.sub(r"^(\.\./|\./|@/|~/|/)+", "", s.split("?")[0])
        return re.sub(r"(/index)?\.(tsx?|jsx?|mjs|cjs|py)$", "", s)

    # A test that names a file as a string (an allowlist, a fixture path) doesn't make it
    # live; only a test that imports it does, and that's an edge, handled above.
    named_by: dict[str, set[str]] = {}
    for f, t in raw_texts.items():
        if is_test(f):
            continue
        imports = set(import_specs(t))
        found = re.findall(r"""['"`]([^'"`\s]{3,200})['"`]""", t)
        if not f.endswith(CODE_EXT):   # YAML run lines, shell scripts: paths are often unquoted
            found += re.findall(r"[\w@.~<>$-]*(?:/[\w@.~-]+)+\.(?:m?[jt]sx?|cjs|py)\b", t)
        for lit in found:
            if "/" in lit and lit not in imports:
                parts = norm(lit).split("/")
                for i in range(len(parts)):            # index every suffix: <rootDir>/jest.setup -> jest.setup
                    named_by.setdefault("/".join(parts[i:]), set()).add(f)

    def named_elsewhere(target: str, alive: set[str]) -> list[str]:
        stem = norm(target)
        parts = stem.split("/")
        keys = {"/".join(parts[i:]) for i in range(len(parts) - 1)} | {stem}   # >= 2 segments, or the full stem
        return sorted({f for k in keys for f in named_by.get(k, ())
                       if f != target and (f not in fileset or f in alive)})

    # Entry points that declare themselves: a Python file with a __main__ block, and whatever
    # package.json names as main, bin or in a script.
    declared: set[str] = {f for f, t in texts.items() if f.endswith(".py") and re.search(r"__name__\s*==\s*['\"]__main__['\"]", t)}
    for pj in [f for f in all_tracked if f.endswith("package.json") and "node_modules" not in f]:
        try:
            meta = json.loads((repo / pj).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        root = os.path.dirname(pj)
        cands = [meta.get("main", "")] + list((meta.get("bin") or {}).values() if isinstance(meta.get("bin"), dict) else [meta.get("bin") or ""])
        cands += re.findall(r"[\w./-]+\.(?:m?[jt]sx?|cjs|py)\b", " ".join((meta.get("scripts") or {}).values()))
        for c in cands:
            p = os.path.normpath(os.path.join(root, c)).replace("\\", "/") if c else ""
            if p in fileset:
                declared.add(p)

    # fnmatch's `**/` needs at least one folder; `app/**/page.*` must also match `app/page.tsx`.
    # __init__.py is a package marker, loaded by importing the package, never by its own name.
    is_entry = lambda f: is_test(f) or f.endswith("__init__.py") or f in declared or any(
        fnmatch.fnmatch(f, g) or fnmatch.fnmatch(f, g.replace("**/", "")) for g in entries)
    alive = set(files)
    rounds: list[list[str]] = []
    named_only: dict[str, list[str]] = {}    # zero importers, kept only because a path string names them
    while True:
        importers = {f: 0 for f in alive}
        for f in alive:
            for t in edges[f]:
                if t in importers:
                    importers[t] += 1
        new = []
        for f in sorted(alive):
            if importers[f] or is_entry(f):
                continue
            namers = named_elsewhere(f, alive)
            if namers:
                named_only[f] = namers
            else:
                new.append(f)
        if not new:
            break
        rounds.append(new)
        alive -= set(new)

    # reachability from entry points, for twins
    reach: set[str] = set()
    stack = [f for f in files if is_entry(f) and not is_test(f)]
    while stack:
        f = stack.pop()
        if f in reach:
            continue
        reach.add(f)
        stack.extend(edges.get(f, ()))

    # Imported (directly or not) by tests, but unreachable from every non-test entry point:
    # the test passes, and nothing a user can reach runs this code.
    test_reach: set[str] = set()
    stack = [f for f in files if is_test(f)]
    while stack:
        f = stack.pop()
        if f in test_reach:
            continue
        test_reach.add(f)
        stack.extend(edges.get(f, ()))
    orphaned = {f for r in rounds for f in r}
    test_only = sorted(f for f in test_reach - reach - orphaned if not is_test(f))

    defs: dict[str, list[str]] = {}
    for f, t in texts.items():
        names = EXPORT_NAME.findall(t) if not f.endswith(".py") else PY_DEF.findall(t)
        for n in set(names):
            if len(n) > 3 and not n.startswith("_") and n not in {"default", "handler", "config", "metadata", "GET", "POST", "PUT", "DELETE", "PATCH", "Props", "main"}:
                defs.setdefault(n, []).append(f)
    twins = []
    for n, fs in sorted(defs.items()):
        if len(fs) > 1 and not all(is_test(f) for f in fs):
            live = sorted(f for f in fs if f in reach)
            dead = sorted(f for f in fs if f not in reach)
            if live and dead:
                twins.append({"name": n, "live": live, "not_reachable": dead})
    named_only = {f: n for f, n in named_only.items() if f in alive}
    non_test = [f for f in files if not is_test(f) and not f.endswith("__init__.py")]
    unreachable_share = len(orphaned) / len(non_test) if non_test else 0.0
    return {"rounds": rounds, "twins": twins, "named_only": named_only, "test_only": test_only,
            "files": len(files), "unreachable_share": round(unreachable_share, 2),
            "entries": sorted(f for f in files if is_entry(f) and not is_test(f))[:50],
            "config_aliases": [f"{a.key} -> {', '.join(a.targets)} ({a.origin})" for a in from_config]}


# ------------------------------------------------------------------ output

def print_claims(claims: list[Claim]) -> None:
    order = STATUS_ORDER
    for c in sorted(claims, key=lambda c: order[c.status]):
        print(f"\n[{c.status}] ({c.source}) {c.text}")
        for f in c.findings:
            print(f"  - {f['check']}: {f['msg']}")
    n = {s: sum(c.status == s for c in claims) for s in order}
    print(f"\n{len(claims)} claims: {n['CONTRADICTED']} contradicted, {n['UNPROVEN']} unproven, "
          f"{n['NO CONTRADICTION FOUND']} with no contradiction found — {NOT_A_VERDICT}")


STATUS_ORDER = {"CONTRADICTED": 0, "UNPROVEN": 1, "NO CONTRADICTION FOUND": 2}
NOT_A_VERDICT = "none of these is a verdict until the code is read"


def claims_markdown(claims: list[Claim], rng: str, max_rows: int = 60) -> str:
    """A summary line, a table of claims, and the standing line, e.g. for a PR comment."""
    def cell(s: str) -> str:
        # one table row per claim: no newlines, no pipes, no raw HTML (a claim can't hide or break the table)
        return re.sub(r"\s+", " ", s).replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;").strip()

    n = {s: sum(c.status == s for c in claims) for s in STATUS_ORDER}
    lines = [f"**{len(claims)} claims: {n['CONTRADICTED']} contradicted, {n['UNPROVEN']} unproven, "
             f"{n['NO CONTRADICTION FOUND']} no contradiction found** (range `{cell(rng)}`)", ""]
    ordered = sorted(claims, key=lambda c: STATUS_ORDER[c.status])
    if ordered:
        lines += ["| # | Status | Claim | From | Findings |", "|---|---|---|---|---|"]
        for i, c in enumerate(ordered[:max_rows], 1):
            text = c.text if len(c.text) <= 300 else c.text[:297] + "..."
            findings = "<br>".join(f"**{f['check']}**: {cell(f['msg'])}" for f in c.findings) or "none"
            source = "PR text" if c.source == "summary" else f"`{c.source}`"
            status = f"**{c.status}**" if c.status == "CONTRADICTED" else c.status
            lines.append(f"| {i} | {status} | {cell(text)} | {source} | {findings} |")
        if len(ordered) > max_rows:
            lines += ["", f"...and {len(ordered) - max_rows} more; run `verify_claims.py claims` locally for the full list."]
    lines += ["", f"_{NOT_A_VERDICT[0].upper() + NOT_A_VERDICT[1:]}. CONTRADICTED means the repository disagrees "
                  f"with the sentence, UNPROVEN means nothing in the diff backs it, and NO CONTRADICTION FOUND "
                  f"does not mean it is true._"]
    return "\n".join(lines) + "\n"


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop the command we started and everything it started: by its PID / process group, never by name."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)       # start_new_session made proc.pid the group id
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def smoke(repo: Path, command: str, timeout: float = 120, ref: str = "HEAD") -> dict:
    """Start the app once in a throwaway worktree of `ref`, created in the system temp folder
    (never inside the user's checkout, never on their branch), and remove it afterwards."""
    res = {"command": command, "ref": ref, "commit": "", "started": False, "timed_out": False,
           "exit_code": None, "tail": [], "error": "", "timeout": timeout, "worktree": ""}
    tmp = Path(tempfile.mkdtemp(prefix="cairn-smoke-"))
    wt = tmp / "worktree"
    res["worktree"] = str(wt)
    added = False
    try:
        try:
            sha = run_git(repo, "rev-parse", "--verify", ref + "^{commit}").strip()
            res["commit"] = sha[:12]
            git_run(["git", "-C", str(repo), "worktree", "add", "--detach", "-q", str(wt), sha],
                           check=True, capture_output=True, text=True, timeout=300)
            added = True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            detail = getattr(e, "stderr", "") or str(e)
            res["error"] = f"could not create a temporary worktree of {ref}: {str(detail).strip()[:300]}"
            return res
        log = tmp / "output.log"
        kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        with open(log, "wb") as out:
            proc = subprocess.Popen(command, shell=True, cwd=wt, stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=clean_env(), **kw)
            try:
                res["exit_code"] = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                res["timed_out"] = True
                kill_tree(proc)
                res["exit_code"] = proc.returncode
        res["started"] = res["exit_code"] == 0 and not res["timed_out"]
        res["tail"] = log.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
    finally:
        if added:
            r = git_run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                print(f"note: git worktree remove failed ({r.stderr.strip()[:200]}); deleting the folder and pruning",
                      file=sys.stderr)
        for _ in range(20):                    # Windows can hold a file briefly after a kill
            shutil.rmtree(tmp, ignore_errors=True)
            if not tmp.exists():
                break
            time.sleep(0.5)
        if tmp.exists():
            print(f"note: could not delete the temporary folder {tmp}; delete it by hand", file=sys.stderr)
        if added:
            git_run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True, timeout=300)
    return res


def smoke_lines(s: dict) -> list[str]:
    where = f"a temporary worktree of {s['ref']}" + (f" @ {s['commit']}" if s["commit"] else "")
    if s["error"]:
        head = [f"SMOKE TEST COULD NOT RUN: {s['error']}"]
    elif s["started"]:
        return [f"smoke: app starts (`{s['command']}` exited 0 in {where})"]
    else:
        why = (f"did not finish within {s['timeout']:g}s and was stopped; if it starts a server that keeps running, "
               f"use a command that exits once the app has loaded (an import, a build, one request)"
               if s["timed_out"] else f"exited with code {s['exit_code']}")
        head = [f"APP FAILED TO START: `{s['command']}` {why} (in {where})."]
    return head + ["Unreachable-file conclusions can't be trusted until it starts: a module the scan calls dead may be",
                   "loaded at runtime, or the app may already be broken. Fix the start first, then re-run."] + \
        (["last lines of output:"] + [f"  | {l}" for l in s["tail"]] if s["tail"] else [])


def print_smoke(s: dict) -> None:
    lines = smoke_lines(s)
    if s["started"]:
        print("\n" + lines[0])
        return
    bar = "!" * 78
    print("\n" + "\n".join([bar] + lines + [bar]))


def print_orphans(res: dict) -> None:
    if res.get("config_aliases"):
        print(f"import aliases read from tsconfig/jsconfig: {'; '.join(res['config_aliases'][:10])}")
    for i, r in enumerate(res["rounds"], 1):
        label = "nothing imports these" if i == 1 else f"only imported by round {i - 1}"
        print(f"\nround {i} ({len(r)}): {label}")
        for f in r[:40]:
            print(f"  {f}")
        if len(r) > 40:
            print(f"  … {len(r) - 40} more")
    if res["named_only"]:
        print(f"\nnamed but never imported ({len(res['named_only'])}): a path string mentions these; read it before deciding")
        for f, by in list(res["named_only"].items())[:40]:
            print(f"  {f}  <- named in {', '.join(by[:3])}")
    if res["test_only"]:
        print(f"\nkept alive only by tests ({len(res['test_only'])}): tests import these, nothing a user reaches does")
        for f in res["test_only"][:40]:
            print(f"  {f}")
    for t in res["twins"]:
        print(f"\ntwin `{t['name']}`: live in {', '.join(t['live'])}; not reachable in {', '.join(t['not_reachable'])}")
    total = sum(len(r) for r in res["rounds"])
    if res["unreachable_share"] > 0.5:
        print(f"\nWARNING: {res['unreachable_share']:.0%} of the code is unreachable, which usually means an entry point "
              f"is missing, not that the app is dead. Entry points found: {', '.join(res['entries'][:8]) or 'none'}. "
              f"Pass --entry GLOB for the file the app starts from, and re-run before believing any of this.")
    print(f"\n{total} orphaned files in {len(res['rounds'])} round(s), {len(res['named_only'])} named only, "
          f"{len(res['test_only'])} test-only, {len(res['twins'])} twin name(s), {res['files']} code files scanned "
          f"— delete by reference, never by name")


# ------------------------------------------------------------------ self-test

def self_test() -> int:
    """Run every check's planted case with GIT_DIR pointing at a sentinel repository, as it
    does inside a git hook, and fail if the sentinel changes at all."""
    with tempfile.TemporaryDirectory() as sd:
        sentinel = Path(sd) / "sentinel"
        sentinel.mkdir()
        base = ["git", "-C", str(sentinel), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        git_run(base + ["init", "-q"], check=True, capture_output=True)
        (sentinel / "keep.txt").write_text("sentinel" + chr(10), encoding="utf-8")
        git_run(base + ["add", "-A"], check=True, capture_output=True)
        git_run(base + ["commit", "-q", "-m", "sentinel"], check=True, capture_output=True)
        def state() -> tuple:
            head = git_run(["git", "-C", str(sentinel), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
            bare = git_run(["git", "-C", str(sentinel), "config", "core.bare"], capture_output=True, text=True).stdout
            count = git_run(["git", "-C", str(sentinel), "rev-list", "--all", "--count"], capture_output=True, text=True).stdout
            return head.strip(), bare.strip(), count.strip(), sorted(p.name for p in sentinel.iterdir())
        before = state()
        saved = {k: os.environ.get(k) for k in ("GIT_DIR", "GIT_WORK_TREE")}
        # Only GIT_DIR, as a pre-push hook sets it: git then treats the current folder as the
        # work tree, which is exactly how fixture files once got committed into a real repo.
        os.environ["GIT_DIR"] = str(sentinel / ".git")
        os.environ.pop("GIT_WORK_TREE", None)
        try:
            rc = _self_test_checks()
        except Exception as e:          # a leaked GIT_DIR usually crashes the fixtures first
            print(f"self-test crashed: {e.__class__.__name__}: {str(e)[:200]}")
            rc = 1
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        after = state()
    isolated = before == after
    print(f"{'ok  ' if isolated else 'MISS'} hook-env-isolation: a GIT_DIR inherited from a git hook never reaches the real repository")
    if not isolated:
        print(f"self-test FAILED: the self-test changed the repository named by GIT_DIR ({before} -> {after})")
        return 1
    return rc


def _self_test_checks() -> int:
    def git(repo: Path, *a: str) -> None:
        git_run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c",
                        "commit.gpgsign=false", *a], check=True, capture_output=True)

    def write(repo: Path, files: dict[str, str | None]) -> None:
        for rel, body in files.items():
            p = repo / rel
            if body is None:
                p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body, encoding="utf-8")

    fired: set[str] = set()
    with tempfile.TemporaryDirectory() as t:
        repo = Path(t) / "r"
        repo.mkdir()
        git(repo, "init", "-q")
        write(repo, {
            "src/login.ts": "import { old } from './old';\nexport function login() { return old(); }\n",
            "src/old.ts": "export function old() { return 1; }\n",
            "src/util.ts": "export function pad(s: string) { return s; }\n",
            "tests/login.test.ts": "import { login } from '../src/login';\nit('logs in', () => { expect(login()).toBe(1); });\n",
            "tests/util.test.ts": "import { pad } from '../src/util';\nit('pads', () => { expect(pad('a')).toBe('a'); });\nit('pads twice', () => { expect(pad('b')).toBe('b'); });\n",
        })
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "base")
        base = run_git(repo, "rev-parse", "HEAD").strip()

        # 1. Overclaiming commit: names a file that doesn't exist and one it didn't touch, claims tests
        #    and a removal, deletes a file still imported, and asserts proof with no test change.
        write(repo, {"src/login.ts": "import { old } from './old';\nexport function login() { return old() + 0; }\n",
                     "src/old.ts": None})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m",
            "Fixed the session bug in `src/auth.ts` and `src/util.ts`.\n\n- Added tests for login.\n- Removed dead code.\n- Verified it works.")
        c1 = run_git(repo, "rev-parse", "HEAD").strip()
        case1 = check_claims(repo, f"{base}..{c1}")
        for c in case1:
            fired |= {f["check"] for f in c.findings}

        # 1b. --markdown: a summary line, one table row per claim however hostile its text (a pipe,
        #     a newline, an HTML comment that would hide the rest of a PR comment), the standing line.
        hostile = Claim("Fixed `a|b.ts`\nand <!-- hidden --> it", ["fixed"], [],
                        [{"level": "UNPROVEN", "check": "fix-without-test", "msg": "x | y"}], "summary")
        md = claims_markdown(case1 + [hostile], f"{base[:7]}..{c1[:7]}")
        rows = [l for l in md.splitlines() if l.startswith("| ") and not l.startswith("| #")]
        if (re.match(r"\*\*\d+ claims: \d+ contradicted, \d+ unproven, \d+ no contradiction found\*\*", md)
                and len(rows) == len(case1) + 1 and "<!--" not in md
                and all(len(re.findall(r"(?<!\\)\|", r)) == 6 for r in rows)
                and md.rstrip().endswith("does not mean it is true._") and NOT_A_VERDICT[1:] in md):
            fired.add("markdown")

        # 2. Tests changed, but they import nothing that changed; a skip is added; an assertion is dropped.
        write(repo, {"src/login.ts": "import { old } from './old';\nexport function login() { return old() + 1; }\n",
                     "src/old.ts": "export function old() { return 1; }\n",
                     "tests/util.test.ts": "import { pad } from '../src/util';\nit.skip('pads', () => { expect(pad('a')).toBe('a'); });\n"})
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "Fix login off-by-one. All tests pass.")
        c2 = run_git(repo, "rev-parse", "HEAD").strip()
        for c in check_claims(repo, f"{c1}..{c2}"):
            fired |= {f["check"] for f in c.findings}

        # 3. "No behaviour change" while an existing assertion is rewritten, and a later commit in the
        #    same range puts the assertion back behind a skip. A range-wide diff would hide the edit;
        #    checking each commit against its own diff must not.
        write(repo, {"tests/login.test.ts": "import { login } from '../src/login';\nit('logs in', () => { expect(login()).toBe(2); });\n"})
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "Refactor login. No behaviour change.")
        write(repo, {"tests/login.test.ts": "import { login } from '../src/login';\nit('logs in', () => { expect(login()).toBe(1); });\n"})
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "Tidy tests.")
        c3 = run_git(repo, "rev-parse", "HEAD").strip()
        for c in check_claims(repo, f"{c2}..{c3}"):
            fired |= {f["check"] for f in c.findings}

        # 3a. "All tests pass" while a config hook drops a test, with no skip marker in the test file.
        write(repo, {"jest.config.js": "module.exports = { testPathIgnorePatterns: ['tests/util.test.ts'] };\n"})
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "Speed up CI. All tests pass.")
        c3a = run_git(repo, "rev-parse", "HEAD").strip()
        for c in check_claims(repo, f"{c3}..{c3a}"):
            fired |= {f["check"] for f in c.findings}
        c3 = c3a

        # 3b. "Wired into checkout" for a new file that nothing imports.
        write(repo, {"src/audit.ts": "export function logEvent() { return 1; }\n"})
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "Wired the audit log into checkout.")
        c3b = run_git(repo, "rev-parse", "HEAD").strip()
        for c in check_claims(repo, f"{c3}..{c3b}"):
            fired |= {f["check"] for f in c.findings}
        c3 = c3b

        # 4. A claim the diff supports must not be contradicted (false-positive guard). The body is
        #    hard-wrapped, and it names an untouched file as context, which is not a claim.
        write(repo, {"src/util.ts": "export function pad(s: string) { return s.trim(); }\n",
                     "tests/util.test.ts": "import { pad } from '../src/util';\nit('pads', () => { expect(pad(' a')).toBe('a'); });\n"})
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m",
            "Fixed pad in `src/util.ts` and added a test for it.\n\nThe live login path is src/login.ts, and\n"
            "none of it is touched here. Kept: src/old.ts, which login\nstill imports.")
        c4 = run_git(repo, "rev-parse", "HEAD").strip()
        clean = check_claims(repo, f"{c3}..{c4}")
        false_pos = [f for c in clean for f in c.findings]

        # 4b. Prose that uses "fixed", "resolves" and "verified" without claiming an unproven fix
        #     (sentences taken from real commits that the first version wrongly flagged).
        write(repo, {"README.md": "notes\n"})
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m",
            "Document the send path\n\nThresholds are fixed before sending. The old section reference resolves to\n"
            "nothing, so it goes. Verified by interrupting a run on purpose: a partial artefact landed.")
        c4b = run_git(repo, "rev-parse", "HEAD").strip()
        false_pos += [f for c in check_claims(repo, f"{c4}..{c4b}") for f in c.findings]

        # 5. Orphans in rounds, a config that names a file by path, and a live/dead twin.
        o = Path(t) / "o"
        o.mkdir()
        git(o, "init", "-q")
        write(o, {
            "app/layout.tsx": "import { AuthProvider } from '@/contexts/AuthContext';\nexport default function L() { return AuthProvider; }\n",
            "app/page.tsx": "import { live } from '@/lib/live';\nexport default function P() { return live(); }\n",
            "contexts/AuthContext.tsx": "export function AuthProvider() { return 1; }\n",
            "components/auth-provider.tsx": "import { factory } from '../lib/auth-factory';\nexport function AuthProvider() { return factory(); }\n",
            "lib/auth-factory.ts": "import { bridge } from './jwt-bridge';\nexport function factory() { return bridge(); }\n",
            "lib/jwt-bridge.ts": "export function bridge() { return 2; }\n",
            "lib/live.ts": "export function live() { return 3; }\n",
            "lib/configured.ts": "export const x = 1;\n",
            "next.config.js": "module.exports = { entry: 'lib/configured' };\n",
            # regressions found on a real repo (2026-09-27), each must stay live:
            "lib/sub/index.ts": "export * from './limits';\n",                       # re-export keeps a module live
            "lib/sub/limits.ts": "export const cap = 1;\n",
            "app/api/x/route.ts": "import { cap } from '@/lib/sub';\nexport function GET() { return cap; }\n",
            ".github/scripts/check.mjs": "console.log(1);\n",                           # run from a workflow, unquoted
            ".github/workflows/ci.yml": "steps:\n  - run: node tools/lint-paths.mjs\n",
            "tools/lint-paths.mjs": "console.log(2);\n",
            "jest.setup.js": "globalThis.x = 1;\n",
            "types/global.d.ts": "declare const y: number;\n",
            # a test that only NAMES a file (an allowlist) must not keep it alive
            "lib/__tests__/allow.test.ts": "const KNOWN = ['lib/allowlisted.ts'];\nit('x', () => expect(KNOWN.length).toBe(1));\n",
            "lib/allowlisted.ts": "export const z = 1;\n",
            "lib/tested-only.ts": "export const w = 1;\n",
            "lib/__tests__/tested-only.test.ts": "import { w } from '../tested-only';\nit('w', () => expect(w).toBe(1));\n",
        })
        git(o, "add", "-A"); git(o, "commit", "-q", "-m", "o")
        res = check_orphans(o, DEFAULT_ENTRIES, {"@": "."})
        rounds = res["rounds"]
        flat = [f for r in rounds for f in r]
        if len(rounds) == 3 and "components/auth-provider.tsx" in rounds[0] and rounds[2] == ["lib/jwt-bridge.ts"]:
            fired.add("orphan")
        if any(tw["name"] == "AuthProvider" and tw["live"] == ["contexts/AuthContext.tsx"] for tw in res["twins"]):
            fired.add("twin")
        if "lib/allowlisted.ts" in rounds[0] if rounds else False:
            fired.add("orphan-despite-allowlist")
        if res["named_only"].get("lib/configured.ts") == ["next.config.js"]:
            fired.add("named-only")
        if res["test_only"] == ["lib/tested-only.ts"]:
            fired.add("test-only")
        # 6. Python and Node apps whose entry points declare themselves (found on 2026-09-27 when an eval
        #    run reported a whole app/main.py app as unreachable), and a repo with no entry point at all.
        py = Path(t) / "py"
        py.mkdir()
        git(py, "init", "-q")
        write(py, {
            "app/__init__.py": "", "app/auth/__init__.py": "", "app/legacy/__init__.py": "",
            "app/main.py": "from app.auth.session import create_session\n",
            "app/auth/session.py": "def create_session():\n    return 1\n",
            "app/legacy/session.py": "def create_session():\n    return 2\n",
            "tests/test_session.py": "from app.legacy.session import create_session\n",
            "tools/run.py": "from app.util import go\nif __name__ == '__main__':\n    go()\n",
            "app/util.py": "def go():\n    return 3\n",
            "package.json": '{"main": "server/start.js", "scripts": {"cron": "node jobs/nightly.js"}}',
            "server/start.js": "console.log(1);\n",
            "jobs/nightly.js": "console.log(2);\n",
        })
        git(py, "add", "-A"); git(py, "commit", "-q", "-m", "py")
        pres = check_orphans(py, DEFAULT_ENTRIES, {"@": "."})
        py_flat = [f for r in pres["rounds"] for f in r]
        if (not any(f in py_flat for f in ["app/main.py", "app/auth/session.py", "tools/run.py", "app/util.py",
                                             "server/start.js", "jobs/nightly.js"])
                and pres["test_only"] == ["app/legacy/session.py"]
                and any(tw["name"] == "create_session" and tw["live"] == ["app/auth/session.py"] for tw in pres["twins"])):
            fired.add("declared-entries")
        q = Path(t) / "q"
        q.mkdir()
        git(q, "init", "-q")
        write(q, {"lib/a.py": "from lib.b import x\n", "lib/b.py": "x = 1\n"})
        git(q, "add", "-A"); git(q, "commit", "-q", "-m", "q")
        if check_orphans(q, DEFAULT_ENTRIES, {"@": "."})["unreachable_share"] > 0.5:
            fired.add("missing-entry-warning")

        # 7. Aliases read from tsconfig/jsconfig, with no --alias at all: a `paths` alias in a config
        #    with comments and trailing commas, and a nested jsconfig whose alias comes through
        #    `extends` and is relative to that config. A file imported only through an alias is live;
        #    a file nothing imports is still an orphan.
        ts = Path(t) / "ts"
        ts.mkdir()
        git(ts, "init", "-q")
        write(ts, {
            "tsconfig.json": '{\n  // path aliases\n  "compilerOptions": {\n    "baseUrl": ".",\n'
                             '    "paths": { "@core/*": ["lib/core/*"], /* trailing comma next */ },\n  },\n}\n',
            "app/page.tsx": "import { engine } from '@core/engine';\nexport default function P() { return engine(); }\n",
            "lib/core/engine.ts": "export function engine() { return 1; }\n",
            "lib/core/unused.ts": "export const u = 1;\n",
            "packages/web/jsconfig.json": '{ "extends": "./jsconfig.base.json" }\n',
            "packages/web/jsconfig.base.json": '{ "compilerOptions": { "paths": { "~ui/*": ["./src/ui/*"] } } }\n',
            "packages/web/pages/home.js": "import Button from '~ui/button';\nexport default Button;\n",
            "packages/web/src/ui/button.js": "export default function Button() {}\n",
        })
        git(ts, "add", "-A"); git(ts, "commit", "-q", "-m", "ts")
        tres = check_orphans(ts, DEFAULT_ENTRIES + ["packages/web/pages/*"], {})
        ts_flat = [f for r in tres["rounds"] for f in r]
        if ts_flat == ["lib/core/unused.ts"] and not tres["named_only"]:
            fired.add("config-alias")

        # 8. A src/ layout: packages under src/ are imported by their top-level name (`mypkg.core`),
        #    with relative (`from . import helpers`) and submodule (`from mypkg import extra`) imports.
        sl = Path(t) / "srclayout"
        sl.mkdir()
        git(sl, "init", "-q")
        write(sl, {
            "src/mypkg/__init__.py": "",
            "src/mypkg/cli.py": "from mypkg.core import run\nif __name__ == '__main__':\n    run()\n",
            "src/mypkg/core.py": "from . import helpers\nfrom .models import Model\nfrom mypkg import extra\n"
                                 "def run():\n    return helpers.h(), Model, extra.e\n",
            "src/mypkg/helpers.py": "def h():\n    return 1\n",
            "src/mypkg/models.py": "class Model:\n    pass\n",
            "src/mypkg/extra.py": "e = 1\n",
            "src/mypkg/dead.py": "d = 1\n",
        })
        git(sl, "add", "-A"); git(sl, "commit", "-q", "-m", "sl")
        sres = check_orphans(sl, DEFAULT_ENTRIES, {})
        if [f for r in sres["rounds"] for f in r] == ["src/mypkg/dead.py"] and not sres["named_only"]:
            fired.add("python-src-root")

        # 9. --smoke: a command that starts passes and one that fails doesn't; a command that hangs is
        #    stopped at the timeout (its PID is gone afterwards); it runs the committed HEAD, not the
        #    user's uncommitted edit, which it leaves alone; and no worktree or temp folder is left.
        sm = Path(t) / "smoke"
        sm.mkdir()
        git(sm, "init", "-q")
        write(sm, {"okmod.py": "VALUE = 1\n"})
        git(sm, "add", "-A"); git(sm, "commit", "-q", "-m", "sm")
        write(sm, {"okmod.py": "raise SystemExit('uncommitted edit')\n"})     # the user's dirty working tree
        py_exe = f'"{sys.executable}"'
        pid_file = (Path(t) / "smoke-pid.txt").as_posix()
        good = smoke(sm, f'{py_exe} -c "import okmod"', 60)
        bad = smoke(sm, f'{py_exe} -c "import okmod, missing_module_for_smoke"', 60)
        hung = smoke(sm, f'{py_exe} -c "import os, time; open(\'{pid_file}\', \'w\').write(str(os.getpid())); '
                         f'time.sleep(60)"', 3)

        def pid_alive(pid: int) -> bool:
            """Read-only: tasklist on Windows; on POSIX signal 0, counting a killed-but-unreaped zombie as dead."""
            for _ in range(25):                      # a killed grandchild can take a moment to be reaped
                if os.name == "nt":
                    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                                         text=True, timeout=30).stdout
                    alive = str(pid) in out.split()
                else:
                    try:
                        os.kill(pid, 0)
                        stat = Path(f"/proc/{pid}/stat")
                        alive = not (stat.exists() and stat.read_text().rsplit(")", 1)[-1].split()[0] == "Z")
                    except OSError:
                        alive = False
                if not alive:
                    return False
                time.sleep(0.2)
            return True
        hung_pid = int(Path(pid_file).read_text()) if Path(pid_file).exists() else 0
        worktrees = [l for l in run_git(sm, "worktree", "list", "--porcelain").splitlines() if l.startswith("worktree ")]
        if (good["started"] and not bad["started"] and bad["exit_code"] not in (0, None)
                and any("missing_module_for_smoke" in l for l in bad["tail"])
                and hung["timed_out"] and not hung["started"] and hung_pid and not pid_alive(hung_pid)
                and len(worktrees) == 1
                and not any(Path(x["worktree"]).parent.exists() for x in (good, bad, hung))
                and (sm / "okmod.py").read_text() == "raise SystemExit('uncommitted edit')\n"):
            fired.add("smoke")

        orphan_fp = [f for f in flat if f in {"lib/configured.ts", "lib/live.ts", "contexts/AuthContext.tsx",
                                              "lib/sub/limits.ts", "tools/lint-paths.mjs", ".github/scripts/check.mjs",
                                              "jest.setup.js", "types/global.d.ts"}]

    expected = {"file-not-in-diff", "file-does-not-exist", "tests-claimed-none-changed", "tests-never-import-changed",
                "skip-added", "tests-removed", "removed-still-referenced", "fix-without-test",
                "behaviour-claim-tests-edited", "assertion-word", "wired-but-unimported", "test-selection-changed",
                "orphan", "twin", "orphan-despite-allowlist", "named-only", "test-only",
                "declared-entries", "missing-entry-warning", "config-alias", "python-src-root", "smoke", "markdown"}
    for c in sorted(expected):
        print(f"{'ok  ' if c in fired else 'MISS'} {c}")
    if false_pos:
        print(f"FALSE POSITIVE on a supported claim: {[f['check'] for f in false_pos]}")
    if orphan_fp:
        print(f"FALSE POSITIVE orphan: {orphan_fp}")
    ok = expected <= fired and not false_pos and not orphan_fp
    print(f"\nself-test {'passed' if ok else 'FAILED'}: {len(expected & fired)}/{len(expected)} checks fired"
          f"{'' if not (false_pos or orphan_fp) else ', with false positives'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", choices=["claims", "orphans"])
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument("--range", help="BASE..HEAD, e.g. main..HEAD or HEAD~5..HEAD")
    ap.add_argument("--text", type=Path, help="a PR description or agent summary to check as well")
    ap.add_argument("--entry", action="append", help="glob for an extra entry point (repeatable), added to the defaults")
    ap.add_argument("--alias", action="append", default=[], help="import alias, e.g. @=. or @=src (repeatable)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--markdown", action="store_true", help="claims: a summary line and a table, e.g. for a PR comment")
    ap.add_argument("--smoke", metavar="COMMAND",
                    help="start the app once with COMMAND in a temporary git worktree of HEAD (claims: the range's head); "
                         "exit code 3 if it fails to start")
    ap.add_argument("--smoke-timeout", type=float, default=120, metavar="SECONDS", help="hard timeout for --smoke (default 120)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if a.self_test:
        return self_test()
    if a.json and a.markdown:
        ap.error("pick one of --json and --markdown")
    repo = a.repo.expanduser().resolve()
    if a.command == "claims":
        if not a.range:
            ap.error("claims needs --range BASE..HEAD")
        extra = a.text.read_text(encoding="utf-8", errors="replace") if a.text else ""
        claims = check_claims(repo, a.range, extra)
        s = smoke(repo, a.smoke, a.smoke_timeout, re.split(r"\.\.\.?", a.range)[-1] or "HEAD") if a.smoke else None
        if a.json:
            out = [{**asdict(c), "status": c.status} for c in claims]
            print(json.dumps({"claims": out, "smoke": s} if s else out, indent=2))
        elif a.markdown:
            print(claims_markdown(claims, a.range), end="")
            if s:
                print("\n" + ("\n".join(smoke_lines(s)[:1]) if s["started"] else
                              "**" + smoke_lines(s)[0] + "**\n\n```\n" + "\n".join(smoke_lines(s)[1:]) + "\n```"))
        else:
            print_claims(claims)
            if s:
                print_smoke(s)
        return 3 if s and not s["started"] else 0
    if a.command == "orphans":
        if a.markdown:
            ap.error("--markdown is for the claims command")
        aliases = dict(x.split("=", 1) for x in a.alias) if a.alias else {}
        fallback = {} if a.alias else {"@": ".", "~": "."}
        s = smoke(repo, a.smoke, a.smoke_timeout) if a.smoke else None
        res = check_orphans(repo, DEFAULT_ENTRIES + (a.entry or []), aliases, fallback)
        if s:
            res["smoke"] = s
        print(json.dumps(res, indent=2) if a.json else "", end="")
        if not a.json:
            print_orphans(res)
            if s:
                print_smoke(s)
        return 3 if s and not s["started"] else 0
    ap.error("give a command (claims or orphans) or --self-test")
    return 2


if __name__ == "__main__":
    sys.exit(main())
