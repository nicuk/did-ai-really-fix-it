#!/usr/bin/env python3
"""
verify_claims.py — check what a coding agent said it did against what the repository shows.

It is a *locator*, not a judge. It finds claims that the diff contradicts or leaves
unproven; the skill (SKILL.md) turns those into verdicts by reading code and running tests.

Usage:
  python verify_claims.py claims  --repo PATH --range BASE..HEAD [--text FILE] [--json]
  python verify_claims.py orphans --repo PATH [--entry GLOB ...] [--alias @=.] [--json]
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

Read-only: it reads files and runs `git log` / `git diff` / `git show` / `git ls-files` in
the repository you name. It writes nothing there and makes no network requests.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
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


def run_git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
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
    specs += [m.replace(".", "/") for m in re.findall(r"^\s*from\s+([\w.]+)\s+import\b", text, re.M)]
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
        out = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"],
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


def resolve_spec(spec: str, importer: str, files: set[str], aliases: dict[str, str]) -> str | None:
    spec = spec.split("?")[0]
    cands: list[str] = []
    if spec.startswith("."):
        base = os.path.normpath(os.path.join(os.path.dirname(importer), spec)).replace("\\", "/")
        cands.append(base)
    else:
        for prefix, target in aliases.items():
            if spec == prefix or spec.startswith(prefix + "/"):
                rest = spec[len(prefix):].lstrip("/")
                cands.append(os.path.normpath(os.path.join(target, rest)).replace("\\", "/"))
        if "/" in spec or importer.endswith(".py"):
            cands.append(spec)                    # python dotted module, or a bare repo path
            cands.append("src/" + spec)
    for c in cands:
        c = c.lstrip("./") if not c.startswith("..") else c
        for suffix in ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py",
                       "/index.ts", "/index.tsx", "/index.js", "/index.jsx", "/__init__.py"):
            if c + suffix in files:
                return c + suffix
    return None


def check_orphans(repo: Path, entries: list[str], aliases: dict[str, str]) -> dict:
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

    edges: dict[str, set[str]] = {f: set() for f in files}     # file -> files it imports
    for f, t in texts.items():
        for spec in import_specs(t):
            tgt = resolve_spec(spec, f, fileset, aliases)
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
            "entries": sorted(f for f in files if is_entry(f) and not is_test(f))[:50]}


# ------------------------------------------------------------------ output

def print_claims(claims: list[Claim]) -> None:
    order = {"CONTRADICTED": 0, "UNPROVEN": 1, "NO CONTRADICTION FOUND": 2}
    for c in sorted(claims, key=lambda c: order[c.status]):
        print(f"\n[{c.status}] ({c.source}) {c.text}")
        for f in c.findings:
            print(f"  - {f['check']}: {f['msg']}")
    n = {s: sum(c.status == s for c in claims) for s in order}
    print(f"\n{len(claims)} claims: {n['CONTRADICTED']} contradicted, {n['UNPROVEN']} unproven, "
          f"{n['NO CONTRADICTION FOUND']} with no contradiction found — none of these is a verdict until the code is read")


def print_orphans(res: dict) -> None:
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
    def git(repo: Path, *a: str) -> None:
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c",
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
        for c in check_claims(repo, f"{base}..{c1}"):
            fired |= {f["check"] for f in c.findings}

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

        orphan_fp = [f for f in flat if f in {"lib/configured.ts", "lib/live.ts", "contexts/AuthContext.tsx",
                                              "lib/sub/limits.ts", "tools/lint-paths.mjs", ".github/scripts/check.mjs",
                                              "jest.setup.js", "types/global.d.ts"}]

    expected = {"file-not-in-diff", "file-does-not-exist", "tests-claimed-none-changed", "tests-never-import-changed",
                "skip-added", "tests-removed", "removed-still-referenced", "fix-without-test",
                "behaviour-claim-tests-edited", "assertion-word", "wired-but-unimported", "test-selection-changed",
                "orphan", "twin", "orphan-despite-allowlist", "named-only", "test-only",
                "declared-entries", "missing-entry-warning"}
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
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if a.self_test:
        return self_test()
    repo = a.repo.expanduser().resolve()
    if a.command == "claims":
        if not a.range:
            ap.error("claims needs --range BASE..HEAD")
        extra = a.text.read_text(encoding="utf-8", errors="replace") if a.text else ""
        claims = check_claims(repo, a.range, extra)
        if a.json:
            print(json.dumps([{**asdict(c), "status": c.status} for c in claims], indent=2))
        else:
            print_claims(claims)
        return 0
    if a.command == "orphans":
        aliases = dict(x.split("=", 1) for x in a.alias) if a.alias else {"@": ".", "~": "."}
        res = check_orphans(repo, DEFAULT_ENTRIES + (a.entry or []), aliases)
        print(json.dumps(res, indent=2) if a.json else "", end="")
        if not a.json:
            print_orphans(res)
        return 0
    ap.error("give a command (claims or orphans) or --self-test")
    return 2


if __name__ == "__main__":
    sys.exit(main())
