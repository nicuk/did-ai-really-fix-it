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
  file-does-not-exist     a file the claim says was added or changed exists nowhere in the repository
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
`compilerOptions.paths` and `baseUrl` in the repository (comments and trailing commas allowed),
workspace package names, SvelteKit's $lib and Nuxt's ~. Entry patterns apply from every app or package
root (a folder with package.json or a next/svelte/nuxt/vite/astro config), matched case-sensitively;
a Vite index.html, SvelteKit routes and hooks, Storybook stories, test-runner setup files and
"package.module:app" strings (uvicorn, gunicorn, Helm, Dockerfiles) count as entries too.

--smoke "COMMAND" starts the app once before you trust an unreachable file: it adds a temporary
`git worktree` of HEAD (or the range's head) in the system temp folder, runs COMMAND there under
a timeout, reports whether it started, and removes the worktree, even on failure or timeout.

Read-only: it reads files and runs `git log` / `git diff` / `git show` / `git ls-files` in
the repository you name. It writes nothing there and makes no network requests. Only --smoke
runs anything else: your own COMMAND, in the temporary worktree, never in your checkout.
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import fnmatch
import io
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
# .svelte and .vue files (their <script> blocks) and .mdx pages (their top-level import lines, not the
# examples in code fences): their imports are read, and they count as used (a framework loads them by
# convention or auto-import, which no import graph sees), so they are never reported themselves.
MARKUP_EXT = (".svelte", ".vue", ".mdx")
SKIP_DIRS = {"node_modules", ".git", ".next", "dist", "build", ".venv", "venv", "__pycache__",
             ".turbo", "coverage", "out", ".vercel", ".claude"}
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|e2e)(/|$)|\.(test|spec)\.[a-z]+$|(^|/)test_[^/]+\.py$|_test\.py$", re.I)
# A test is code: `docs/generated/x.spec.json` is data that happens to match. The one exception is an
# end-to-end flow written in YAML (Maestro), which lives under an e2e or maestro folder.
TEST_CODE_EXT = CODE_EXT + (".svelte", ".vue", ".mts", ".cts", ".go", ".rs", ".rb", ".java", ".kt", ".swift", ".cs", ".php")
E2E_FLOW = re.compile(r"(^|/)(e2e|\.maestro|maestro)/.*\.ya?ml$", re.I)
# End-to-end specs drive the running app, not a module, so they never import the code they test.
E2E_PATH = re.compile(r"(^|/)(e2e|cypress|\.maestro|maestro|playwright)(/|$)|\.e2e\.[a-z]+$|"
                      r"(^|/)(playwright|cypress)\.config\.[a-z]+$", re.I)
E2E_IMPORT = re.compile(r"""(?:\bfrom\s+|\brequire\s*\(\s*|\bimport\s*\(?\s*)['"](?:@playwright/test|playwright|cypress)['"]"""
                        r"""|/// <reference types=["']cypress""")
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
    "src/instrumentation.*", "**/setupTests.*", "**/test-setup.*",
    # SvelteKit
    "src/routes/**/+page.*", "src/routes/**/+layout.*", "src/routes/**/+server.*", "src/routes/**/+error.*",
    "src/hooks.*", "src/params/*", "src/service-worker.*",
    # Astro
    "src/content/config.*", "src/content.config.*",
    # Storybook
    "**/*.stories.*", "**/*.story.*", "**/.storybook/**",
]
# Applied only under a folder with a nuxt.config: Nuxt loads these folders by convention and auto-imports.
NUXT_ENTRIES = ["server/**", "plugins/**", "middleware/**", "composables/**", "utils/**", "layouts/**",
                "app/**", "app.config.*", "error.*"]
# A folder with one of these is an app or package root: the entry patterns apply relative to it too.
APP_ROOT_MARKER = re.compile(r"(^|/)(package\.json|(next|svelte|nuxt|vite|astro)\.config\.[a-z]+)$")
# Warn when more than this share of an app root's code is unreachable (orphaned or reached only by tests),
# for a root with at least UNREACHABLE_WARN_MIN_FILES files; a smaller one warns only past half, since a
# small app with one dead copy in it (what this scan exists to find) is not an app missing its entry point.
UNREACHABLE_WARN = 0.25
UNREACHABLE_WARN_MIN_FILES = 10


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
    p = path.replace("\\", "/")
    return bool(TEST_PATH.search(p)) and (p.endswith(TEST_CODE_EXT) or bool(E2E_FLOW.search(p)))


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
# "Verified: decoded both and compared SHA-256" and "Verified before fixing (probe.py at 05a7d3e)" name a
# method; "Verified: the panel still has zoom 1" doesn't.
PROOF_METHOD = (r"(ran|run|running|re-?ran|decoded|compared|diffed|rendered|loaded|opened|measured|tested|built|executed|"
                r"grepped|reproduced|replayed|queried|hashed|probed|inspected|counted|clicked|curl(ed)?|traced|asserted|"
                r"sha-?\d+|checksum|[\w./-]+\.(py|sh|js|mjs|ts))")
NAMES_ITS_PROOF = re.compile(r"\b(verified|confirmed|proved|proven|checked)\s+(by|with|via|using|against)\b|\b(ran|running|measured|"
                             r"\d+\s*(of|/)\s*\d+|output|exit code|red then green|fails? (before|without))\b"
                             r"|\bverified(\s+before\s+\w+)?\s*[:(—-]\s*[^.]{0,80}?\b" + PROOF_METHOD + r"\b", re.I)
ASSERTION_WORD = re.compile(r"\b(verified|confirmed|tested manually|works( now| correctly| as expected)?|always|fully|everywhere|all (cases|paths|callers))\b", re.I)
# What a file name ends in. `medallion.gold`, `json.loads` and `minio.bucket` are identifiers, not files.
FILE_EXT = frozenset("""ts tsx js jsx mjs cjs mts cts py pyi go rs rb java kt kts swift c h cc cpp hpp cs php vue svelte astro
    json jsonc json5 jsonl yaml yml toml md mdx rst sql sh bash zsh ps1 bat css scss sass less html htm xml svg txt ini
    cfg conf lock csv tsv proto graphql gql tf hcl tpl j2 gradle properties ipynb dart lua ex exs scala prisma
    gitignore dockerignore npmrc nvmrc editorconfig prettierrc eslintrc babelrc""".split())
BARE_PATH = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.(?:" + "|".join(sorted(FILE_EXT)) + r"))\b")


def path_tokens(sentence: str) -> list[str]:
    """File paths a sentence names: a backticked token that contains "/" or ends in a file extension
    (a trailing `:12` or `#L12` is dropped), and an unquoted path with a folder and an extension."""
    out: list[str] = []
    for tok in re.findall(r"`([^`\s]+)`", sentence):
        tok = re.sub(r"(#L\d+.*|(:\d+)+)$", "", tok)
        base = tok.rsplit("/", 1)[-1]
        ext = re.search(r"\.([A-Za-z0-9]{1,6})$", base)
        if "://" in tok or not ext:
            continue
        if "/" in tok or ext.group(1) in FILE_EXT or base.startswith(".env"):
            out.append(tok)
    unquoted = re.sub(r"`[^`]*`", " ", sentence)
    return out + [p for p in BARE_PATH.findall(unquoted) if p not in out]
SKIP_MARKER = re.compile(r"\b(it|test|describe)\.(skip|only|todo)\s*\(|\bx(it|describe|test)\s*\(|@pytest\.mark\.(skip|xfail)|pytest\.skip\(|\.skipIf\(|@unittest\.skip")
# Where tests get selected or dropped without a skip marker in the test itself.
TEST_CONFIG_FILE = re.compile(r"(^|/)(conftest\.py|pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|package\.json|"
                              r"(jest|vitest|karma|playwright|cypress)\.config\.[a-z]+|\.mocharc[.\w]*)$|"
                              r"(^|/)(tests?|__tests__)/__init__\.py$", re.I)
TEST_SELECTION = re.compile(r"load_tests|collect_ignore|--deselect|\s-k\s|testPathIgnorePatterns|testMatch|testRegex|"
                            r"testNamePattern|--grep|exclude|ignore|skip|only|addopts|norecursedirs|\"test\"\s*:", re.I)


def strip_comment(line: str) -> str:
    """The code on a config line, without a // or # comment, a /* */ span or a JSDoc continuation."""
    s = re.sub(r"/\*.*?(\*/|$)", " ", line)
    if re.match(r"\s*\*", s):
        return ""
    return re.sub(r"(^|\s)(//|#).*$", "", s)


def same_line(a: str, b: str) -> bool:
    """Equal but for whitespace and a trailing comma: `"test": "x"` -> `"test": "x",` changes nothing."""
    return re.sub(r"\s+", "", a).rstrip(",") == re.sub(r"\s+", "", b).rstrip(",")
PACKAGE_TEST_LINE = re.compile(r"\btest|jest|vitest|mocha|jasmine|karma|playwright|cypress|pytest|\bava\b|\btap\b", re.I)
ASSERT_LINE = re.compile(r"\bexpect\s*\(|\bassert\b|\bassert\w*\s*\(|\.should\b|\btoMatchSnapshot\b")


@dataclass
class Claim:
    text: str
    kinds: list[str]
    paths: list[str]
    findings: list[dict] = field(default_factory=list)
    source: str = ""          # the commit it came from, or "summary" for --text
    see_also: list[str] = field(default_factory=list)   # claims in the same commit that carry its findings

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
# "Exists nowhere" is worth saying only about a file the sentence says was made or changed.
FILE_VERB = re.compile(r"\b(add(s|ed|ing)?|creat(e|es|ed|ing)|chang(e|es|ed|ing)|updat(e|es|ed|ing)|fix(es|ed|ing)?|"
                       r"edit(s|ed|ing)?|remov(e|es|ed|ing)|delet(e|es|ed|ing)|renam(e|es|ed|ing)|mov(e|es|ed|ing))\b", re.I)
# ...and not about one the sentence places in another repository or a dependency.
ELSEWHERE = re.compile(r"\ball-repos?\b|\b(an)?other\s+repo(sitor(y|ies))?s?\b|\bseparate\s+repo(sitory)?\b|\bupstream\b|"
                       r"\bdependenc(y|ies)\b|\bvendored\b|\bthird[- ]party\b|node_modules|site-packages|"
                       r"\bin\s+(the\s+)?[\w.@/-]+\s+(repo|repository|package|library|crate|gem|sdk)\b", re.I)


def claimed_changed(sentence: str, path: str, verbs: re.Pattern = CHANGE_VERB) -> bool:
    """Is `path` the object of a change verb in its own clause, e.g. 'Fixed the bug in `x.ts`'?
    A path named as context ('the live one is x.ts', 'Kept: x.ts') is not a claim about x.ts."""
    i = sentence.find(path)
    if i < 0:
        return False
    clause = re.split(r"[;:,()]|\bthe live\b|\bbut\b", sentence[:i])[-1]
    return bool(verbs.search(clause)) and not NEGATION.search(clause)


# In a squash-merge message a later sub-commit can rename a file an earlier one named:
# "mock-idp-username.yaml becomes mock-idp-approve.yaml", "renamed it to x.yaml".
RENAME = re.compile(r"`?(?P<old>[\w./-]+\.\w{1,6})`?\s+(?:becomes|became|is now|was renamed to|is renamed to|renamed to|"
                    r"moved to|->|→)\s+`?(?P<new>[\w./-]+\.\w{1,6})`?"
                    r"|\brenamed\s+(?:it\s+|this\s+|the file\s+)?(?:to|as)\s+`?(?P<new2>[\w./-]+\.\w{1,6})`?", re.I)


def is_squash(msg: str) -> bool:
    return bool(re.search(r"^\* \S", msg, re.M) or re.search(r"\bsquash(ed)?\b", msg, re.I))


def classify(sentence: str) -> Claim:
    kinds = [k for k, rx in CLAIM_KINDS if rx.search(sentence)]
    paths = path_tokens(sentence)
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
    hunks: dict[str, list[tuple[list[str], list[str]]]] = field(default_factory=dict)  # path -> [(removed, added)]

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
    hunks: dict[str, list[tuple[list[str], list[str]]]] = {}
    cur = old = None
    in_hunk = False
    # Three lines of context, as a reviewer sees it: an edit and the lines that replace it land in one
    # hunk, so a skip that is moved or re-emitted nets out instead of reading as added.
    for line in run_git(repo, "diff", "--no-color", "--no-ext-diff", "-U3", "-M", rng).splitlines():
        if line.startswith("diff --git "):
            cur = old = None
            in_hunk = False
        elif not in_hunk and line.startswith("--- "):
            old = line[6:] if line.startswith("--- a/") else None
            if old:
                removed.setdefault(old, [])
        elif not in_hunk and line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            in_hunk = True
            key = cur or old
            if key:
                hunks.setdefault(key, []).append(([], []))
        elif in_hunk and line.startswith("+") and cur:
            added.setdefault(cur, []).append(line[1:])
            hunks[cur][-1][1].append(line[1:])
        elif in_hunk and line.startswith("-"):
            key = cur or old            # a renamed file's removed lines go with its new path
            if key:
                removed.setdefault(key, []).append(line[1:])
                hunks[key][-1][0].append(line[1:])
    head = rng.split("..")[-1] or "HEAD"
    head_files = set(run_git(repo, "ls-tree", "-r", "--name-only", head).splitlines())
    return Diff(status, added, removed, head_files, hunks)


def resolve_claimed(path: str, files: set[str]) -> list[str]:
    p = re.sub(r"^(\./)+|^/", "", path)      # a "./" or "/" prefix; lstrip("./") would eat ".github"
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
        claims += evaluate(repo, f"{parents[0]}..{sha}", extract(msg, sha[:7]), msg)
    if extra_text.strip():
        claims += evaluate(repo, rng, extract(extra_text, "summary"), extra_text)
    return claims


def paired_with(deleted: str, added: set[str]) -> str | None:
    """The added test that replaces a deleted one: the same name, or a close name in the same folder.
    A rename plus a rewrite is too different for `git -M` to pair, and is not a removed test. Names are
    compared without the test affixes: test_reports.py and test_session.py share "test_" and ".py", not a name."""
    def stem(p: str) -> str:
        s = re.sub(r"(\.[A-Za-z0-9]+)+$", "", posixpath.basename(p))
        return re.sub(r"^test_|_test$|_spec$", "", s)
    for a in sorted(added):
        if stem(a) == stem(deleted) or (posixpath.dirname(a) == posixpath.dirname(deleted) and
                                        difflib.SequenceMatcher(None, stem(deleted), stem(a)).ratio() >= 0.6):
            return a
    return None


def evaluate(repo: Path, rng: str, claims: list[Claim], message: str = "") -> list[Claim]:
    if not claims:
        return claims
    diff = read_diff(repo, rng)

    tests_changed = diff.tests()
    tests_new = diff.tests("AR")
    tests_deleted = {d for d in diff.tests("D") if not paired_with(d, tests_new)}
    # A skip counts when a hunk adds more skip markers than it removes: one moved or re-emitted nets out.
    skip_added: list[tuple[str, str]] = []
    for p, hs in diff.hunks.items():
        if is_test(p):
            for rem, add_ in hs:
                marks = [l.strip() for l in add_ if SKIP_MARKER.search(l)]
                gone = sum(1 for l in rem if SKIP_MARKER.search(l))
                skip_added += [(p, l) for l in marks[gone:]]
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

    # Test linkage. An end-to-end spec drives the running app, so it is linked to any change; a test (or a
    # test helper it imports) that names a changed non-code file by path (a chart, a template folder) reads it.
    def is_e2e(t: str) -> bool:
        return bool(E2E_PATH.search(t) or E2E_IMPORT.search(head_read(t)))

    changed_data = [p for p, s in diff.status.items() if s[0] in "AMR" and not p.endswith(CODE_EXT) and not is_test(p)]
    data_names = {"/".join(p.split("/")[:i]) for p in changed_data for i in range(1, p.count("/") + 2)}
    data_names |= {posixpath.basename(p) for p in changed_data}

    head_code = sorted(f for f in diff.head_files if f.endswith(CODE_EXT))

    def reads_changed_data(t: str) -> bool:
        if not data_names:
            return False
        texts = [head_read(t)]
        for spec in import_specs(texts[0])[:20]:
            if spec.startswith("."):
                base = posixpath.normpath(posixpath.join(posixpath.dirname(t), spec))
                texts += [head_read(base + x) for x in ("", ".ts", ".js", ".py", ".mjs", ".tsx") if base + x in diff.head_files][:1]
            else:
                texts += [head_read(f) for f in head_code if spec_hits(spec, f)][:2]
        lits = {re.sub(r"^(\./)+|^/|/$", "", l) for x in texts for l in re.findall(r"""['"]([^'"\n]{1,200})['"]""", x)}
        return bool(lits & data_names)

    # Squash merges: a rename in a later sub-commit settles a file an earlier one named.
    flat_msg = " ".join(message.split())
    renames = [(m.start(), m.group("old"), m.group("new") or m.group("new2")) for m in RENAME.finditer(flat_msg)] \
        if is_squash(message) else []

    def renamed_later(c: Claim, p: str) -> bool:
        at = flat_msg.find(" ".join(c.text.split()))
        for pos, old, new in renames:
            if pos < at or not resolve_claimed(new, diff.head_files):
                continue
            if (old and posixpath.basename(old) == posixpath.basename(p)) or \
                    (not old and posixpath.splitext(new)[1] == posixpath.splitext(p)[1]):
                return True
        return False

    linked: dict[str, bool] = {}           # test -> can it fail for a change in this diff (computed once)

    def is_linked(t: str) -> bool:
        if t not in linked:
            linked[t] = (is_e2e(t) or any(spec_hits(s, src) for s in import_specs(head_read(t)) for src in diff.sources())
                         or reads_changed_data(t))
        return linked[t]

    for c in claims:
        def add(level: str, check: str, msg: str, c: Claim = c) -> None:
            if not any(f["check"] == check and f["msg"] == msg for f in c.findings):
                c.findings.append({"level": level, "check": check, "msg": msg})
        for p in c.paths:
            changed_claim = claimed_changed(c.text, p)
            hits = resolve_claimed(p, diff.head_files | diff.touched)
            if not hits:
                if claimed_changed(c.text, p, FILE_VERB) and not ELSEWHERE.search(c.text) and not renamed_later(c, p):
                    add("CONTRADICTED", "file-does-not-exist",
                        f"`{p}` exists nowhere in the repository at {head}, and the range did not delete it")
            elif changed_claim and not any(h in diff.touched for h in hits):
                add("CONTRADICTED", "file-not-in-diff", f"the claim says `{p}` changed, but this range never touched it")
        if "tests-added" in c.kinds and not tests_changed:
            add("CONTRADICTED", "tests-claimed-none-changed", "tests are claimed, but no test file was added or changed in the range")
        if ("tests-added" in c.kinds or "fixed" in c.kinds) and tests_changed and diff.sources():
            if not any(is_linked(t) for t in sorted(tests_changed)):
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
            for p, hs in diff.hunks.items():
                if not TEST_CONFIG_FILE.search(p):
                    continue
                if p.endswith("package.json") and diff.status.get(p, "")[:1] == "A":
                    continue                 # a new package's own scripts: it has no existing tests to drop
                hits = [l.strip() for rem, add_ in hs for l in add_
                        if TEST_SELECTION.search(strip_comment(l))               # not an "only" in a comment
                        and not any(same_line(l, r) for r in rem)                 # not a line that only gained a comma
                        # in package.json, only a line about a test runner (not "--lockfile-only", "--ignore-rules")
                        and (not p.endswith("package.json") or PACKAGE_TEST_LINE.search(l))]
                if hits:
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
    return once_per_commit(claims)


# The claim a finding about the whole diff belongs under, when several claims in one commit trigger it.
BEST_CLAIM = {"skip-added": "tests-pass", "tests-removed": "tests-pass", "test-selection-changed": "tests-pass",
              "tests-never-import-changed": "tests-added", "fix-without-test": "fixed"}


def once_per_commit(claims: list[Claim]) -> list[Claim]:
    """A finding about the diff is reported once, not under every claim that triggers it: under the first
    claim of the kind it answers ("all tests pass" for an added skip), else the first. A proof word is
    about its own sentence, so it stays with each claim."""
    owner: dict[tuple[str, str], Claim] = {}
    for c in claims:
        for f in c.findings:
            key, want = (f["check"], f["msg"]), BEST_CLAIM.get(f["check"])
            if f["check"] != "assertion-word" and (key not in owner or (want in c.kinds and want not in owner[key].kinds)):
                owner[key] = c
    for c in claims:
        moved = [owner[(f["check"], f["msg"])] for f in c.findings
                 if f["check"] != "assertion-word" and owner[(f["check"], f["msg"])] is not c]
        # A claim whose findings sit under a sibling still points there, so it never reads as clean.
        c.see_also = list(dict.fromkeys(o.text[:80] for o in moved))
        c.findings = [f for f in c.findings if f["check"] == "assertion-word" or owner[(f["check"], f["msg"])] is c]
    return claims


# ------------------------------------------------------------------ orphans and twins

EXPORT_NAME = re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|const|let|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)", re.M)
PY_DEF = re.compile(r"^(?:def|class)\s+([A-Za-z_]\w*)", re.M)


def list_code_files(repo: Path, ext: tuple = CODE_EXT) -> list[str]:
    try:
        files = run_git(repo, "ls-files").splitlines()
    except Exception as e:
        print(f"note: git ls-files failed ({e.__class__.__name__}); walking the tree instead", file=sys.stderr)
        files = []
        for root, dirs, fs in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            files += [Path(root, f).relative_to(repo).as_posix() for f in fs]
    return sorted(f for f in files if f.endswith(ext) and not any(part in SKIP_DIRS for part in f.split("/")))


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


def resolve_spec(spec: str, importer: str, files: set[str], aliases: list[Alias],
                 py_roots: dict[str, list[str]] | None = None) -> str | None:
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
        if importer.endswith(".py") and py_roots:  # monorepo: packages/*/src, services/*/src, any src/ with a package
            cands += [r + "/" + spec for r in py_roots.get(spec.split("/")[0], ())]
    for c in cands:
        c = re.sub(r"^(\./)+", "", c) if not c.startswith("..") else c
        for suffix in ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py",
                       "/index.ts", "/index.tsx", "/index.js", "/index.jsx", "/__init__.py"):
            if c + suffix in files:
                return c + suffix
        # TypeScript ESM spells a .ts import with .js (`./editors/Polygon.js` is editors/Polygon.ts).
        m = re.match(r"(.*)\.(m|c)?js(x?)$", c)
        if m and not importer.endswith(".py"):
            for ext in (f".{m.group(2) or ''}ts{m.group(3)}", ".tsx"):
                if m.group(1) + ext in files:
                    return m.group(1) + ext
    return None


def python_roots(files: set[str], projects: list[str] = ()) -> dict[str, list[str]]:
    """Import roots for Python, keyed by the top-level package under them: every packages/*/src and
    services/*/src, any src/ folder with a package (a folder holding __init__.py) directly in it, and
    every folder with a pyproject.toml, whose own modules import each other by bare name."""
    roots: dict[str, set[str]] = {}
    for proj in (p for p in projects if p):
        for f in files:
            if f.endswith(".py") and posixpath.dirname(f) == proj:
                roots.setdefault(posixpath.basename(f)[:-3], set()).add(proj)
            elif f.endswith("/__init__.py") and posixpath.dirname(posixpath.dirname(f)) == proj:
                roots.setdefault(posixpath.basename(posixpath.dirname(f)), set()).add(proj)
    for f in files:
        if not f.endswith(".py"):
            continue
        parts = f.split("/")
        for i in range(len(parts) - 2):
            if parts[i] != "src":
                continue
            root = "/".join(parts[:i + 1])
            if re.match(r"^(packages|services)/[^/]+/src$", root) or "/".join(parts[:i + 2]) + "/__init__.py" in files:
                roots.setdefault(parts[i + 1], set()).add(root)
    return {k: sorted(v) for k, v in roots.items()}


def resolve_file(base: str, files: set[str]) -> str | None:
    base = posixpath.normpath(base)
    for suffix in ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", "/index.ts", "/index.js"):
        if base + suffix in files:
            return base + suffix
    return None


def script_text(f: str, t: str) -> str:
    """A .svelte or .vue file's <script> blocks, an .mdx page's import lines outside code fences; any other file as is."""
    if f.endswith(".mdx"):
        prose = re.sub(r"^(```|~~~).*?^\1", "", t, flags=re.S | re.M)
        return "\n".join(l for l in prose.splitlines() if re.match(r"\s*(import|export)\s", l))
    if f.endswith(MARKUP_EXT):
        return "\n".join(re.findall(r"<script\b[^>]*>(.*?)</script>", t, re.S | re.I))
    return t


def check_orphans(repo: Path, entries: list[str], aliases: dict[str, str],
                  fallback_aliases: dict[str, str] | None = None) -> dict:
    """`aliases` (from --alias) are tried first, then every tsconfig/jsconfig alias, then
    `fallback_aliases` (the @ and ~ defaults, used when no --alias is given)."""
    files = list_code_files(repo, CODE_EXT + MARKUP_EXT)
    fileset = set(files)
    texts = {f: script_text(f, (repo / f).read_text(encoding="utf-8", errors="replace")) for f in files}
    # Every tracked text file can name a code file by path (configs, middleware matchers, workflows).
    try:
        all_tracked = run_git(repo, "ls-files").splitlines()
    except Exception as e:
        print(f"note: git ls-files failed ({e.__class__.__name__}); only code files are read", file=sys.stderr)
        all_tracked = files
    raw_texts = {}
    for f in all_tracked:
        if f.endswith(CODE_EXT) or f.endswith((".json", ".yml", ".yaml", ".toml", ".sh", ".html")):
            try:
                raw_texts[f] = texts.get(f) or (repo / f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

    # App and package roots: every folder with a package.json or a framework config. Entry patterns apply
    # relative to each one, so a Next.js app in docs/ has its pages found like one at the repo root.
    app_roots = sorted({posixpath.dirname(f) for f in all_tracked if APP_ROOT_MARKER.search(f)
                        and not any(part in SKIP_DIRS for part in f.split("/"))} | {""})
    nuxt_roots = sorted({posixpath.dirname(f) for f in all_tracked if re.search(r"(^|/)nuxt\.config\.[a-z]+$", f)})

    from_config = config_aliases(repo, all_tracked)
    # Nuxt's ~ and @ live in a generated .nuxt/tsconfig.json. Without it they mean the Nuxt app's own folder
    # (or its app/ folder), never the repo root the fallback assumes.
    nuxt_aliases: list[Alias] = []
    for r in nuxt_roots:
        if (repo / r / ".nuxt" / "tsconfig.json").is_file():
            continue
        here = r or "."
        for key in ("~", "@", "~~", "@@"):
            nuxt_aliases += [Alias(key, [here, posixpath.join(here, "app")], r, True, f"{r or '.'}/nuxt.config"),
                             Alias(key + "/*", [posixpath.join(here, "*"), posixpath.join(here, "app", "*")], r, True,
                                   f"{r or '.'}/nuxt.config")]
    # SvelteKit's $lib is generated into .svelte-kit/tsconfig.json, which is never committed.
    for r in sorted({posixpath.dirname(f) for f in all_tracked if re.search(r"(^|/)svelte\.config\.[a-z]+$", f)}):
        lib = posixpath.join(r or ".", "src", "lib")
        nuxt_aliases += [Alias("$lib", [lib], r, True, f"{r or '.'}/svelte.config"),
                         Alias("$lib/*", [lib + "/*"], r, True, f"{r or '.'}/svelte.config")]
    # Workspace packages imported by name (`@acme/engine`, `@acme/engine/editors/x`): package.json's
    # main/exports, else src/index; a subpath through its "./*" export, else src/ or the package folder.
    workspace_aliases: list[Alias] = []
    for pj in [f for f in all_tracked if f.endswith("package.json") and f != "package.json"
               and not any(part in SKIP_DIRS for part in f.split("/"))]:
        try:
            meta = json.loads((repo / pj).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = meta.get("name") if isinstance(meta, dict) else None
        if not isinstance(name, str) or not name:
            continue
        r = posixpath.dirname(pj)
        exp = meta.get("exports")
        exp = exp if isinstance(exp, dict) else {".": exp} if isinstance(exp, str) else {}
        def first(v):
            if isinstance(v, dict):
                v = next((v[k] for k in ("source", "svelte", "import", "default", "require", "types") if k in v), None)
                return first(v) if isinstance(v, dict) else v
            return v

        def in_repo(targets: list) -> list[str]:
            """Each target, and where its source lives when it points at build output: dist/x is built from
            src/x, or src/lib/x for a Svelte package."""
            out = []
            for m in targets:
                if isinstance(m, str) and m.count("*") <= 1:
                    m = posixpath.normpath(posixpath.join(r, m))
                    out.append(m)
                    rest = m[len(r) + 1:] if r else m
                    if rest.startswith(("dist/", "build/")):
                        tail = rest.split("/", 1)[1]
                        out += [posixpath.join(r, "src", tail), posixpath.join(r, "src", "lib", tail)]
            return out
        main = [first(exp.get(".")), meta.get("module"), meta.get("main"), "src/index", "index"]
        sub = [first(exp.get("./*")), "./src/*", "./*"]
        workspace_aliases += [Alias(name, in_repo(main), "", True, pj),
                              Alias(name + "/*", [m for m in in_repo(sub) if "*" in m], "", True, pj)]
        # named subpath exports: "@acme/ui/shell" -> exports["./shell"]
        workspace_aliases += [Alias(name + k[1:], in_repo([first(v)]), "", True, pj) for k, v in exp.items()
                              if isinstance(k, str) and k.startswith("./") and "*" not in k]
    alias_list = (flag_aliases(aliases, "--alias") + from_config + nuxt_aliases + workspace_aliases
                  + flag_aliases(fallback_aliases or {}, "default"))
    py_roots = python_roots(fileset, [posixpath.dirname(f) for f in all_tracked if posixpath.basename(f) == "pyproject.toml"])
    edges: dict[str, set[str]] = {f: set() for f in files}     # file -> files it imports
    for f, t in texts.items():
        for spec in import_specs(t):
            tgt = resolve_spec(spec, f, fileset, alias_list, py_roots)
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
        if not isinstance(meta, dict):
            continue
        root = os.path.dirname(pj)
        cands = [meta.get("main", "")] + list((meta.get("bin") or {}).values() if isinstance(meta.get("bin"), dict) else [meta.get("bin") or ""])
        cands += re.findall(r"[\w./-]+\.(?:m?[jt]sx?|cjs|py)\b", " ".join(str(v) for v in (meta.get("scripts") or {}).values()))
        for c in cands:
            p = os.path.normpath(os.path.join(root, c)).replace("\\", "/") if isinstance(c, str) and c else ""
            if p in fileset:
                declared.add(p)
    # <script type="module" src="/src/main.tsx"> in an index.html is how Vite (and plain ES modules) start.
    for page in [f for f in all_tracked if posixpath.basename(f) == "index.html"]:
        html = raw_texts.get(page, "")
        for tag in re.findall(r"<script\b[^>]*>", html, re.I):
            src = re.search(r"""\bsrc\s*=\s*["']([^"']+)["']""", tag)
            if src and re.search(r"""\btype\s*=\s*["']module["']""", tag, re.I) and "://" not in src.group(1):
                hit = resolve_file(posixpath.join(posixpath.dirname(page), src.group(1).lstrip("/")), fileset)
                if hit:
                    declared.add(hit)
    # Test-runner setup files, named in a jest/vitest config or package.json: setupFiles, setupFilesAfterEnv,
    # globalSetup, globalTeardown.
    for cfg in [f for f in raw_texts if re.search(r"(^|/)((jest|vitest|vite)\.config\.[a-z]+|package\.json)$", f)]:
        for block in re.findall(r"""["']?(?:setupFiles(?:AfterEnv)?|globalSetup|globalTeardown)["']?\s*[:=]\s*(\[[^\]]*\]|["'][^"']+["'])""",
                                raw_texts[cfg]):
            for spec in re.findall(r"""["']([^"']+)["']""", block):
                hit = resolve_file(posixpath.join(posixpath.dirname(cfg), spec.replace("<rootDir>/", "")), fileset)
                if hit:
                    declared.add(hit)
    # "package.module:app" names an ASGI/WSGI app or a console script (uvicorn, gunicorn, [project.scripts]),
    # in any tracked text file: Helm templates, Dockerfiles, compose files, pyproject.toml.
    module_ref = re.compile(r"(?<![\w./:@-])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):([A-Za-z_]\w*)\b")
    for f in all_tracked:
        base = posixpath.basename(f)
        if not (f.endswith((".yaml", ".yml", ".tpl", ".toml", ".ini", ".cfg", ".sh", ".json", ".py", ".conf", ".env",
                            ".service", ".txt")) or base.startswith(("Dockerfile", "Containerfile", "Procfile", "Makefile"))
                or base.endswith(".dockerfile")) or any(part in SKIP_DIRS for part in f.split("/")):
            continue
        text = raw_texts.get(f)
        if text is None:
            try:
                if (repo / f).stat().st_size > 2_000_000:
                    continue
                text = (repo / f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        if ":" not in text:
            continue
        for mod, _attr in module_ref.findall(text):
            spec = mod.replace(".", "/")
            for r in [""] + ["src"] + py_roots.get(spec.split("/")[0], []):
                hit = next((posixpath.join(r, spec) + x for x in (".py", "/__init__.py")
                            if posixpath.join(r, spec) + x in fileset), None)
                if hit:
                    declared.add(hit)
                    break

    # One regex for all entry patterns, each tried against the path from every app root that contains the
    # file. Built with fnmatch.translate and matched case-sensitively, as fnmatch.fnmatchcase does, on every
    # OS: fnmatch.fnmatch ignores case on Windows, which once made app/components/Layout.tsx a Next.js layout.
    # fnmatch's `**/` needs at least one folder; `app/**/page.*` must also match `app/page.tsx`.
    def entry_regex(globs: list[str]) -> re.Pattern:
        return re.compile("|".join(fnmatch.translate(g) for g in sorted(set(globs) | {g.replace("**/", "") for g in globs})))
    entry_rx = entry_regex(list(entries))
    nuxt_rx = entry_regex(NUXT_ENTRIES)

    def rel_paths(f: str) -> list[tuple[str, str]]:
        return [(r, f[len(r) + 1:] if r else f) for r in app_roots if not r or f.startswith(r + "/")]

    # __init__.py is a package marker, loaded by importing the package, never by its own name.
    entry_set = {f for f in files
                 if is_test(f) or f.endswith("__init__.py") or f.endswith(MARKUP_EXT) or f in declared
                 or any(entry_rx.match(rp) or (r in nuxt_roots and nuxt_rx.match(rp)) for r, rp in rel_paths(f))}
    is_entry = lambda f: f in entry_set
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
        if not f.endswith(CODE_EXT):
            continue
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
            elif not live and len(code := [f for f in dead if not is_test(f)]) > 1 and any(f not in orphaned for f in code):
                # Neither copy is reachable, yet something (a test, a path string) keeps one around: the
                # likelier story is a missing entry point, and the pair is still the thing to look at.
                twins.append({"name": n, "live": [], "not_reachable": code, "neither_reachable": True})
    named_only = {f: n for f, n in named_only.items() if f in alive}
    twin_files = {f for t in twins for f in t["not_reachable"]}
    test_only = sorted(test_only, key=lambda f: (f not in twin_files, f))    # a twin's copy first, so a cut list keeps it
    non_test = [f for f in files if not is_test(f) and not f.endswith("__init__.py") and not f.endswith(MARKUP_EXT)]
    unreachable = orphaned | set(test_only)
    unreachable_share = len(unreachable & set(non_test)) / len(non_test) if non_test else 0.0
    # The same share per app root (each file counted under the deepest root that holds it): one app with a
    # missing entry point hides inside a monorepo's average.
    by_root: dict[str, list[int]] = {}
    for f in non_test:
        r = max((r for r, _ in rel_paths(f)), key=len)
        n = by_root.setdefault(r, [0, 0])
        n[0] += f in unreachable
        n[1] += 1
    root_shares = {r: round(u / t, 2) for r, (u, t) in sorted(by_root.items()) if t}
    warn_roots = sorted((r for r, sh in root_shares.items()
                         if sh > UNREACHABLE_WARN and (by_root[r][1] >= UNREACHABLE_WARN_MIN_FILES or sh > 0.5)),
                        key=lambda r: -by_root[r][0])
    return {"rounds": rounds, "twins": twins, "named_only": named_only, "test_only": test_only,
            "files": len(files), "unreachable_share": round(unreachable_share, 2),
            "unreachable_by_root": {r: root_shares[r] for r in warn_roots},
            "app_roots": app_roots,
            "entries": sorted(f for f in files if is_entry(f) and not is_test(f) and not f.endswith(MARKUP_EXT))[:50],
            "config_aliases": [f"{a.key} -> {', '.join(a.targets)} ({a.origin})" for a in from_config + nuxt_aliases],
            "workspace_packages": len(workspace_aliases) // 2}


# ------------------------------------------------------------------ output

def print_claims(claims: list[Claim]) -> None:
    order = STATUS_ORDER
    for c in sorted(claims, key=lambda c: order[c.status]):
        print(f"\n[{c.status}] ({c.source}) {c.text}")
        for f in c.findings:
            print(f"  - {f['check']}: {f['msg']}")
        for t in c.see_also:
            print(f"  - this commit's findings are listed under: \"{t}\"")
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
            findings = "<br>".join([f"**{f['check']}**: {cell(f['msg'])}" for f in c.findings]
                                   + [f"this commit's findings are under: {cell(t)}" for t in c.see_also]) or "none"
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
        print(f"import aliases read from tsconfig/jsconfig and framework configs: {'; '.join(res['config_aliases'][:10])}")
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
        for f in res["test_only"][:40]:        # a twin's copy is listed first, so the cut never hides it
            print(f"  {f}")
        if len(res["test_only"]) > 40:
            print(f"  … {len(res['test_only']) - 40} more")
    # Twin lines print in full whatever was cut above: they are the lines that name the live copy.
    for t in [t for t in res["twins"] if t["live"]]:
        print(f"\ntwin `{t['name']}`: live in {', '.join(t['live'])}; not reachable in {', '.join(t['not_reachable'])}")
    neither = [t for t in res["twins"] if not t["live"]]
    for t in neither[:20]:
        print(f"\ntwin `{t['name']}`: neither copy is reachable; is an entry missing? ({', '.join(t['not_reachable'])})")
    if len(neither) > 20:
        print(f"\n… {len(neither) - 20} more twin names where neither copy is reachable (--json lists them)")
    total = sum(len(r) for r in res["rounds"])
    for r, share in list(res.get("unreachable_by_root", {}).items())[:10]:
        where = f"under {r}/" if r else "at the repository root"
        print(f"\nWARNING: {share:.0%} of the code {where} is unreachable (orphaned or reached only by tests), which "
              f"usually means an entry point is missing, not that the app is dead. Pass --entry GLOB for the file it "
              f"starts from, and re-run before believing that part of this.")
    if len(res.get("unreachable_by_root", {})) > 10:
        print(f"\n… and {len(res['unreachable_by_root']) - 10} more app roots over {UNREACHABLE_WARN:.0%} (--json lists them)")
    if res.get("unreachable_by_root"):
        print(f"Entry points found: {', '.join(res['entries'][:8]) or 'none'}.")
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
        case2 = check_claims(repo, f"{c1}..{c2}")
        case2_checks = {f["check"] for c in case2 for f in c.findings}
        fired |= case2_checks

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
        #    It also changes a file under a dot-folder: `.github/...` once read as "exists nowhere",
        #    because lstrip("./") strips characters, not the "./" prefix (found by the PR Action).
        write(repo, {"src/util.ts": "export function pad(s: string) { return s.trim(); }\n",
                     "tests/util.test.ts": "import { pad } from '../src/util';\nit('pads', () => { expect(pad(' a')).toBe('a'); });\n",
                     ".github/workflows/ci.yml": "on: push\n"})
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m",
            "Fixed pad in `src/util.ts` and added a test for it. Added `.github/workflows/ci.yml`.\n\nThe live login path is src/login.ts, and\n"
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

        # 4c. Precision cases from a real measurement (three open-source repos, 2026-09-27), each a pair: a
        #     planted case that must fire and a real-world shape that must not.
        prev = [c4b]

        def step(files: dict, msg: str) -> list[Claim]:
            write(repo, files)
            git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", msg)
            sha = run_git(repo, "rev-parse", "HEAD").strip()
            out, prev[0] = check_claims(repo, f"{prev[0]}..{sha}"), sha
            return out

        def found(cl: list[Claim], check: str, needle: str = "") -> list[dict]:
            return [f for c in cl for f in c.findings if f["check"] == check and needle in f["msg"]]

        # A backticked identifier is not a file; "exists nowhere" needs a change verb and a file in this repo.
        x = step({"README.md": "notes 2\n"},
                 "Tidy the publish path\n\nThe Job adds `minio.bucket` itself, and `json.loads` refuses it. Fixed the parser "
                 "in `src/parser.ts`. It defaults to no limit (lance/optimize.py). Updated `docs/release.md` in the upstream repo.")
        planted = bool(found(x, "file-does-not-exist", "src/parser.ts"))
        if planted and not [f for c in x for f in c.findings if "minio.bucket" in f["msg"] or "json.loads" in f["msg"]]:
            fired.add("path-token")
        if planted and not found(x, "file-does-not-exist", "lance/optimize.py"):
            fired.add("missing-file-verb")
        if planted and not found(x, "file-does-not-exist", "docs/release.md"):
            fired.add("missing-file-elsewhere")
        # A squash merge where a later sub-commit renames the file an earlier one named.
        x = step({"helpers/approve-step.yaml": "steps: []\n"},
                 "Harden the flows (#12)\n\n* test: move the steps\n\nMoved the steps into `helpers/user-step.yaml`.\n\n"
                 "* test: add a ghost\n\nAdded `helpers/ghost-step.yaml` for the retry.\n\n* test: reopen the page\n\n"
                 "user-step.yaml becomes approve-step.yaml.\n")
        if found(x, "file-does-not-exist", "ghost-step.yaml") and not found(x, "file-does-not-exist", "user-step.yaml"):
            fired.add("squash-rename")
        # An end-to-end spec never imports the code it drives.
        e2e = step({"src/login.ts": "import { old } from './old';\nexport function login() { return old() + 2; }\n",
                    "e2e/login.spec.ts": "import { test, expect } from '@playwright/test';\n"
                                         "test('logs in', async ({ page }) => { await page.goto('/'); expect(1).toBe(1); });\n"},
                   "Fix the login redirect and add an e2e test for it.")
        # A test that reads a changed chart by path (through a helper it imports) tests the chart.
        chart = step({"chart/templates/sub.yaml": "durable: true\n", "src/util.ts": "export function pad(s: string) { return s.trim() + ''; }\n",
                      "tests/chart_render.py": "import pathlib\nROOT = pathlib.Path(__file__).parents[1]\n"
                                               "def render():\n    return (ROOT / 'chart').exists()\n",
                      "tests/test_chart.py": "from tests import chart_render\ndef test_chart():\n    assert chart_render.render()\n"},
                     "fix(chart): the subscription is durable.")
        unlinked = step({"chart/templates/sub.yaml": "durable: false\n", "src/util.ts": "export function pad(s: string) { return s.trim(); }\n",
                         "tests/test_other.py": "def test_other():\n    assert 'docs' != 'x'\n"},
                        "fix(chart): keep the name.")
        if found(unlinked, "tests-never-import-changed") and not found(chart, "tests-never-import-changed"):
            fired.add("noncode-linked")
        # A .spec.json file is data, not a test.
        x = step({"src/util.ts": "export function pad(s: string) { return s.trim() ; }\n",
                  "docs/generated/prompt.spec.json": '{"rules": []}\n'}, "Added tests for pad.")
        if found(x, "tests-claimed-none-changed") and not found(x, "tests-never-import-changed"):
            fired.add("test-path-code-ext")
        # A skip replaced by an assertion and a narrower skip, in one hunk, is not a skip added.
        step({"src/render.py": "def run():\n    return 0\n",
              "tests/test_render.py": "import shutil\nimport pytest\nfrom src import render\n\n\ndef _render():\n"
                                      "    done = render.run()\n    if done != 0:\n        pytest.skip('could not render')\n"
                                      "    return done\n\n\ndef test_render():\n    assert _render() == 0\n",
              "package.json": '{\n  "scripts": {\n    "test": "jest"\n  }\n}\n'}, "chore: the render module")
        x = step({"tests/test_render.py": "import shutil\nimport pytest\nfrom src import render\n\n\ndef _render():\n"
                                          "    if shutil.which('helm') is None:\n        pytest.skip('helm not available')\n"
                                          "    done = render.run()\n    assert done == 0, 'refused'\n"
                                          "    return done\n\n\ndef test_render():\n    assert _render() == 0\n"},
                 "fix(render): a refused render fails the test. All tests pass.")
        if "skip-added" in case2_checks and not found(x, "skip-added"):
            fired.add("skip-net")
        # Config edits that select nothing: an "only" in a comment, a "test" script that gained a trailing
        # comma, and a new package's own test script.
        x = step({"jest.config.js": "// the only config the unit tests use\n"
                                    "module.exports = { testPathIgnorePatterns: ['tests/util.test.ts'] };\n",
                  "package.json": '{\n  "scripts": {\n    "test": "jest",\n    "lint": "eslint .",\n'
                                  '    "version:ci": "changeset version && pnpm install --lockfile-only"\n  }\n}\n',
                  "packages/a2/package.json": '{\n  "name": "a2",\n  "scripts": {\n    "test": "vitest run",\n'
                                              '    "check:attw": "attw --pack . --ignore-rules=no-resolution"\n  }\n}\n'},
                 "fix: tidy the config. All tests pass.")
        y = step({"package.json": '{\n  "scripts": {\n    "test": "jest --testPathIgnorePatterns legacy",\n    "lint": "eslint .",\n'
                                  '    "version:ci": "changeset version && pnpm install --lockfile-only"\n  }\n}\n'},
                 "chore: speed up the suite. All tests pass.")
        if found(y, "test-selection-changed", "package.json") and not found(x, "test-selection-changed"):
            fired.add("selection-noise")
        # One finding per commit, under the claim it answers.
        x = step({"src/util.ts": "export function pad(s: string) { return s.trim(); } // both ends\n"},
                 "fix(util): trim both ends\n\nFixed the trim. This fixes the padding bug too.")
        skip_owner = [c for c in case2 for f in c.findings if f["check"] == "skip-added"]
        if (len(found(x, "fix-without-test")) == 1 and sum("fixed" in c.kinds for c in x) >= 2
                and len(skip_owner) == 1 and "tests-pass" in skip_owner[0].kinds
                and all(c.findings or c.see_also for c in x if "fixed" in c.kinds)):
            fired.add("once-per-commit")
        # "Verified:" followed by a method names its proof; followed by a bare statement it doesn't.
        x = step({"README.md": "notes 3\n"},
                 "Re-encode the art\n\nEvery file verified: decoded both versions and compared SHA-256. Verified before "
                 "fixing (probe_door.py at 05a7d3e). Verified: the panel still has zoom 1.")
        words = [c.text for c in x for f in c.findings if f["check"] == "assertion-word"]
        if words == ["Verified: the panel still has zoom 1."]:
            fired.add("proof-named")
        # A test renamed and rewritten (too different for git to pair) is not a removed test; a test
        # deleted with no replacement is.
        renamed = step({"tests/util.test.ts": None,
                        "tests/utils.test.ts": "import { pad } from '../src/util';\n\ndescribe('pad', () => {\n"
                                               "  it('trims both ends of a padded value', () => { expect(pad(' b ')).toBe('b'); });\n"
                                               "  it('keeps the inner space', () => { expect(pad('a b')).toBe('a b'); });\n});\n",
                        "src/util.ts": "export function pad(s: string) { return s.trim(); } // both\n"},
                       "Fixed pad for both ends.")
        # (test_reports.py and a new test_session.py share only "test_" and ".py": that is not a rename)
        step({"tests/test_reports.py": "def test_report():\n    assert 1\n"}, "chore: a report test")
        deleted = step({"tests/login.test.ts": None, "tests/test_reports.py": None,
                        "tests/test_session.py": "def test_session():\n    assert 1\n",
                        "src/login.ts": "import { old } from './old';\nexport function login() { return old(); }\n"},
                       "Fixed login.")
        if (found(deleted, "tests-removed", "tests/login.test.ts") and found(deleted, "tests-removed", "tests/test_reports.py")
                and not found(renamed, "tests-removed")):
            fired.add("renamed-test-paired")
        if "tests-never-import-changed" in case2_checks and not found(e2e, "tests-never-import-changed"):
            fired.add("e2e-linked")

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

        # 8b. A monorepo, from the same measurement: apps and packages in subfolders, each with its own
        #     entry conventions. Every planted dead file (marked DEAD) must be an orphan; every live one mustn't.
        mono = Path(t) / "mono"
        mono.mkdir()
        git(mono, "init", "-q")
        many = {f"lib/t{i:02}.ts": f"export const t{i} = {i};\n" for i in range(45)}
        write(mono, {
            **many,
            "lib/__tests__/many.test.ts": "".join(f"import {{ t{i} }} from '../t{i:02}';\n" for i in range(45)) + "it('x', () => 1);\n",
            # Next.js entries match case-sensitively: components/Layout.tsx is not a layout
            "app/layout.tsx": "import { Shell } from './shell';\nimport { ssoMessage } from '../src/ssoError';\n"
                              "export default function L() { return [Shell, ssoMessage]; }\n",
            "app/shell.tsx": "export const Shell = 1;\n",
            "app/components/Layout.tsx": "export const Frame = 1;\n",                                   # DEAD
            # a Next.js app in a subfolder
            "docs/package.json": '{"name": "docs"}\n', "docs/next.config.mjs": "export default {};\n",
            "docs/app/page.tsx": "import { Hero } from '../components/hero';\nimport { A1 } from '../components/a1';\n"
                                 "import { A2 } from '../components/a2';\nimport { A3 } from '../components/a3';\n"
                                 "export default function P() { return [Hero, A1, A2, A3]; }\n",
            "docs/components/hero.tsx": "export const Hero = 1;\n", "docs/components/a1.tsx": "export const A1 = 1;\n",
            "docs/components/a2.tsx": "export const A2 = 1;\n", "docs/components/a3.tsx": "export const A3 = 1;\n",
            "docs/components/unused.tsx": "export const Unused = 1;\n",                               # DEAD
            # Vite: the entry is named only by index.html
            "web/package.json": '{"name": "web"}\n',
            "web/index.html": '<html><body><script type="module" src="/src/boot.tsx"></script></body></html>\n',
            "web/src/boot.tsx": "import { App } from './App';\nApp();\n",
            "web/src/App.tsx": "import { Chip } from '@acme/ui/widgets/Chip';\nimport { shell } from '@acme/ui/shell';\n"
                               "export function App() { return [Chip, shell]; }\n",
            "web/src/stray.tsx": "export const s = 1;\n",                                             # DEAD
            # SvelteKit routes and hooks, $lib, and imports inside .svelte / .vue files
            "sk/package.json": '{"name": "sk"}\n', "sk/svelte.config.js": "export default {};\n",
            "sk/src/routes/+layout.server.ts": "import { session } from '$lib/session';\nexport const load = () => session;\n",
            "sk/src/lib/session.ts": "export const session = 1;\n",
            "sk/src/hooks.server.ts": "export const handle = 1;\n",
            "sk/src/routes/+page.svelte": "<script lang=\"ts\">\n  import { tick } from '../lib/tick.svelte';\n</script>\n<p>{tick}</p>\n",
            "sk/src/lib/tick.svelte.ts": "export const tick = 1;\n",
            "sk/src/lib/dead.ts": "export const dead = 1;\n",                                        # DEAD
            "vueapp/src/App.vue": "<script setup>\nimport { helper } from './helper';\n</script>\n<template><p/></template>\n",
            "vueapp/src/helper.ts": "export const helper = 1;\n",
            # Storybook, a workspace package imported by name, and a TypeScript ESM `.js` import
            "ui/package.json": '{"name": "@acme/ui", "exports": {".": "./src/index.ts", "./*": "./src/*", '
                               '"./shell": {"types": "./dist/shell/index.d.ts", "svelte": "./dist/shell/index.js"}}}\n',
            "ui/src/lib/shell/index.ts": "export const shell = 1;\n",   # built to dist/shell by svelte-package
            # an MDX page's imports count; an import inside its code fence is an example
            "blog/post.mdx": "import { Demo } from './demo';\n\n# Post\n\n```tsx\nimport { Ghost2 } from './ghost2';\n```\n\n<Demo />\n",
            "blog/demo.tsx": "export const Demo = 1;\n",
            "blog/ghost2.tsx": "export const Ghost2 = 1;\n",                                     # DEAD
            # a Python project folder whose modules import each other by bare name
            "runners/kg/pyproject.toml": "[project]\nname = 'kg'\n",
            "runners/kg/adapter.py": "from generic_sv import clean\nif __name__ == '__main__':\n    clean()\n",
            "runners/kg/generic_sv.py": "def clean():\n    return 1\n",
            "runners/kg/stale.py": "x = 1\n",                                                       # DEAD
            "ui/.storybook/main.ts": "export default { stories: ['../src/**/*.stories.tsx'] };\n",
            "ui/src/Button.stories.tsx": "import { Button } from './Button';\nexport default { component: Button };\n",
            "ui/src/Button.tsx": "export const Button = 1;\n",
            "ui/src/Ghost.tsx": "export const Ghost = 1;\n",                                         # DEAD
            "ui/src/index.ts": "export * from './editors/Polygon.js';\n",
            "ui/src/editors/Polygon.ts": "export const Polygon = 1;\n",
            "ui/src/widgets/Chip.ts": "export const Chip = 1;\n",
            # a jest setup file named in a nested package's config
            "mob/package.json": '{"name": "mob"}\n',
            "mob/jest.config.js": "module.exports = { setupFilesAfterEnv: ['./jest.afterEnv.js'] };\n",
            "mob/jest.afterEnv.js": "globalThis.y = 1;\n",
            "mob/src/Lost.tsx": "export const Lost = 1;\n",                                          # DEAD
            # Python packages under packages/*/src and services/*/src, and an ASGI app named in a Helm chart
            "packages/kit/src/kit/__init__.py": "", "packages/kit/src/kit/blobs.py": "b = 1\n",
            "packages/kit/src/kit/extra.py": "e = 1\n",
            "packages/kit/src/kit/dead.py": "d = 1\n",                                               # DEAD
            "services/api/src/api/__init__.py": "",
            "services/api/src/api/main.py": "from kit.blobs import b\n",
            "services/api/src/api/service.py": "from kit.extra import e\napp = e\n",
            "services/api/src/api/unused.py": "u = 1\n",                                             # DEAD
            "chart/templates/api.yaml": 'command: ["uvicorn", "api.service:app", "--port", "8000"]\n',
            # Nuxt: ~ is the Nuxt app's folder, not the repo root; server/ is loaded by convention
            "nx/package.json": '{"name": "nx"}\n', "nx/nuxt.config.ts": "export default {};\n",
            "nx/server/api/chat.post.ts": "import { tools } from '~/lib/tools';\nexport default tools;\n",
            "nx/lib/tools.ts": "export const tools = 1;\n",
            "nx/lib/unused.ts": "export const u = 1;\n",                                             # DEAD
            "lib/tools.ts": "export const tools = 2;\n",                     # the repo-root decoy a default ~ would pick
            # two copies only tests reach: neither is reachable, and the pair is still reported
            "shared/a/token.ts": "export function makeToken() { return 1; }\n",
            "shared/b/token.ts": "export function makeToken() { return 2; }\n",
            "shared/__tests__/token.test.ts": "import { makeToken } from '../a/token';\nimport { makeToken as m2 } from '../b/token';\n"
                                              "it('t', () => expect(makeToken()).toBe(m2() - 1));\n",
            # a copy only a test reaches, sorted after 45 other test-only files
            "src/ssoError.ts": "export function ssoMessage() { return 1; }\n",
            "x/sso/errors.ts": "export function ssoMessage() { return 2; }\n",
            "x/sso/__tests__/errors.test.ts": "import { ssoMessage } from '../errors';\nit('m', () => expect(ssoMessage()).toBe(2));\n",
            # an app root whose entry reaches half its code
            "lost/package.json": '{"name": "lost"}\n',
            "lost/index.ts": "".join(f"import {{ c{i} }} from './lib/c{i}';\n" for i in range(8)) + "export default c0;\n",
            **{f"lost/lib/c{i}.ts": f"export const c{i} = {i};\n" for i in range(8)},
            "lost/lib/a.ts": "import { b } from './b';\nexport const a = b;\n",
            "lost/lib/b.ts": "export const b = 1;\n",
            "lost/lib/d.ts": "export const d = 1;\n", "lost/lib/e.ts": "export const e = 1;\n",
            # a three-file app with one dead file is a find, not a missing entry point: no warning
            "tiny/package.json": '{"name": "tiny"}\n', "tiny/index.ts": "import { a } from './a';\nexport default a;\n",
            "tiny/a.ts": "export const a = 1;\n", "tiny/dead.ts": "export const d = 1;\n",
        })
        git(mono, "add", "-A"); git(mono, "commit", "-q", "-m", "mono")
        mres = check_orphans(mono, DEFAULT_ENTRIES, {}, {"@": ".", "~": "."})
        dead_set = {f for r in mres["rounds"] for f in r}
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            print_orphans(mres)
        printed = printed.getvalue()
        live = lambda *fs: not any(f in dead_set for f in fs)
        if "app/components/Layout.tsx" in dead_set and live("app/layout.tsx", "app/shell.tsx"):
            fired.add("case-sensitive-entry")
        if "docs/components/unused.tsx" in dead_set and live("docs/app/page.tsx", "docs/components/hero.tsx"):
            fired.add("app-root-entries")
        if "web/src/stray.tsx" in dead_set and "web/src/boot.tsx" in mres["entries"] and live("web/src/App.tsx"):
            fired.add("html-module-entry")
        if "sk/src/lib/dead.ts" in dead_set and live("sk/src/routes/+layout.server.ts", "sk/src/hooks.server.ts"):
            fired.add("sveltekit-entries")
        if "sk/src/lib/dead.ts" in dead_set and live("sk/src/lib/session.ts"):
            fired.add("svelte-lib-alias")
        if ("sk/src/lib/dead.ts" in dead_set and live("sk/src/lib/tick.svelte.ts", "vueapp/src/helper.ts")
                and not any(f.endswith(MARKUP_EXT) for f in dead_set)):
            fired.add("markup-imports")
        if "ui/src/Ghost.tsx" in dead_set and live("ui/src/Button.stories.tsx", "ui/src/Button.tsx", "ui/.storybook/main.ts"):
            fired.add("storybook-entries")
        if "ui/src/Ghost.tsx" in dead_set and live("ui/src/editors/Polygon.ts"):
            fired.add("ts-esm-js-import")
        if "ui/src/Ghost.tsx" in dead_set and live("ui/src/widgets/Chip.ts"):
            fired.add("workspace-package")
        if "ui/src/Ghost.tsx" in dead_set and live("ui/src/lib/shell/index.ts"):
            fired.add("workspace-subpath-export")
        if "blog/ghost2.tsx" in dead_set and live("blog/demo.tsx"):
            fired.add("mdx-imports")
        if "runners/kg/stale.py" in dead_set and live("runners/kg/generic_sv.py"):
            fired.add("python-project-root")
        if "mob/src/Lost.tsx" in dead_set and live("mob/jest.afterEnv.js"):
            fired.add("test-setup-entries")
        if "packages/kit/src/kit/dead.py" in dead_set and live("packages/kit/src/kit/blobs.py"):
            fired.add("python-package-roots")
        if "services/api/src/api/unused.py" in dead_set and live("services/api/src/api/service.py", "packages/kit/src/kit/extra.py"):
            fired.add("module-string-entry")
        if "nx/lib/unused.ts" in dead_set and live("nx/lib/tools.ts", "nx/server/api/chat.post.ts"):
            fired.add("nuxt-app")
        if (any(tw["name"] == "makeToken" and tw.get("neither_reachable") for tw in mres["twins"])
                and "`makeToken`: neither copy is reachable; is an entry missing?" in printed
                and not any(tw["name"] == "ssoMessage" and tw.get("neither_reachable") for tw in mres["twins"])):
            fired.add("neither-twin")
        if "lost" in mres["unreachable_by_root"] and not {"docs", "tiny"} & set(mres["unreachable_by_root"]):
            fired.add("per-root-warning")
        test_only_block = printed.split("kept alive only by tests", 1)[-1].split("\ntwin ", 1)[0]
        if ("\n  x/sso/errors.ts\n" in test_only_block and "more" in test_only_block
                and "twin `ssoMessage`: live in src/ssoError.ts; not reachable in x/sso/errors.ts" in printed):
            fired.add("twin-first")
        orphan_fp_mono = sorted(dead_set - {"app/components/Layout.tsx", "docs/components/unused.tsx", "web/src/stray.tsx",
                                            "sk/src/lib/dead.ts", "ui/src/Ghost.tsx", "mob/src/Lost.tsx",
                                            "packages/kit/src/kit/dead.py", "services/api/src/api/unused.py",
                                            "nx/lib/unused.ts", "lib/tools.ts", "lost/lib/a.ts", "lost/lib/b.ts",
                                            "lost/lib/d.ts", "lost/lib/e.ts", "tiny/dead.ts",
                                            "blog/ghost2.tsx", "runners/kg/stale.py"})

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
                                              "jest.setup.js", "types/global.d.ts"}] + orphan_fp_mono

    expected = {"file-not-in-diff", "file-does-not-exist", "tests-claimed-none-changed", "tests-never-import-changed",
                "skip-added", "tests-removed", "removed-still-referenced", "fix-without-test",
                "behaviour-claim-tests-edited", "assertion-word", "wired-but-unimported", "test-selection-changed",
                "orphan", "twin", "orphan-despite-allowlist", "named-only", "test-only",
                "declared-entries", "missing-entry-warning", "config-alias", "python-src-root", "smoke", "markdown",
                # precision fixes from a real measurement (2026-09-27), each with a must-fire and a must-not-fire case
                "path-token", "missing-file-verb", "missing-file-elsewhere", "squash-rename", "e2e-linked",
                "noncode-linked", "test-path-code-ext", "renamed-test-paired", "skip-net", "selection-noise",
                "once-per-commit", "proof-named", "case-sensitive-entry", "app-root-entries", "html-module-entry",
                "sveltekit-entries", "svelte-lib-alias", "markup-imports", "storybook-entries", "ts-esm-js-import",
                "workspace-package", "test-setup-entries", "python-package-roots", "module-string-entry", "nuxt-app",
                "neither-twin", "per-root-warning", "twin-first", "workspace-subpath-export", "mdx-imports",
                "python-project-root"}
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
