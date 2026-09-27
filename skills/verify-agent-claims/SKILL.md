---
name: verify-agent-claims
description: Checks whether what a coding agent said it did is true — "fixed the bug", "added tests", "all tests pass", "removed dead code", "no behaviour change", "verified" — by splitting its commits, PR description or chat summary into claims and giving each a verdict (holds, doesn't hold, can't tell) with evidence, written for a founder who doesn't read code. Also unsticks an agent that keeps fixing the same thing: finds which of two look-alike files is live, dead code kept alive by other dead code, and writes the CLAUDE.md rules that stop the loop. Ships a read-only, self-testing script. Use it whenever someone asks to verify, check or audit what Claude, Cursor, Codex or any AI agent did; says "did it really fix it", "can I trust this PR", "it says tests pass", "it keeps breaking the same thing", "it's going in circles"; is about to merge AI-written code they can't read; or inherited an AI-built codebase, even if they never say "verify".
---

# Verify agent claims

A coding agent reports its work in sentences: *fixed the login bug, added tests, all tests
pass, removed dead code, no behaviour change.* Each sentence is a claim, and a founder who
can't read the diff has no way to tell which ones are true. The failures are rarely lies.
They are claims made with no enforcer: a test that never imports the code it's named
after, a fix in a file the build doesn't use, a "removal" that left the importer behind,
a green suite that got green by skipping the case that failed.

This skill turns the agent's sentences into claims, checks each one against the repository,
and reports a verdict per claim in words the founder can act on.

## Scope

Covered here: **is what the agent said true?**, and getting an agent unstuck when it keeps
circling the same bug.

Well served elsewhere, so point there rather than redoing them:

| Need | Use |
|---|---|
| A general code-quality review of a PR | Anthropic's `pr-review-toolkit`, `/code-review` |
| Security review | `security-guidance`, `/security-review` |
| Whether the numbers an AI product shows are real | Cairn Signals (`ai-signals-audit`) |
| Where rules and memory should live so agents stop drifting | Cairn Memory (`memory-architecture`) |

## Mode 1: verify the claims

### 1. Collect what was said

Take the claims from wherever the agent made them: commit messages in the range, the PR
description, or the agent's final chat summary. Ask for the summary if the user has it.
Chat summaries hold the strongest claims ("everything works now") and never reach git.

### 2. Run the script

```bash
python <skill-dir>/scripts/verify_claims.py claims --repo <repo> --range <base>..<head> [--text summary.txt]
python <skill-dir>/scripts/verify_claims.py --self-test
```

It splits the text into claims and checks each against the diff:

- a file the claim says changed that the range never touched, or that exists nowhere;
- "added tests" with no test file changed;
- changed tests that import none of the changed code, so they can't fail for the right reason;
- a `.skip`, `.only` or `xfail` added while the claim is "tests pass";
- test-selection config changed in the same range (`load_tests`, `conftest.py`, `pytest.ini`,
  `testPathIgnorePatterns`), which drops tests with no marker in the test itself;
- test files deleted, or assertions going down;
- a deleted file still imported somewhere;
- a fix with no test showing it failed before and passes after;
- "no behaviour change" while existing assertions were rewritten;
- proof words ("verified", "works", "always") with nothing in the diff behind them.

Each commit's message is checked against that commit's own diff, and the summary against
the whole range, so an edit that a later commit undid still shows.

It is a **locator, not a judge**. It says CONTRADICTED, UNPROVEN or NO CONTRADICTION FOUND,
and none of those is a verdict until you've read the code. A claim with no contradiction
can still be false: the script can't run the app.

**Always run the orphan scan as well** (Mode 2, step 1), even when nobody says "stuck". A
fix whose changed file is orphaned, kept alive only by tests, or the unreachable half of a
twin is a fix in dead code: its own test passes, and the running app never sees it. The
claims check alone can't see that; the two together can.

### 3. Give each claim a verdict

For every claim, gather the cheapest evidence that could prove it false, then decide.
`references/recipes.md` has the check for each kind of claim, with commands. The core
moves are:

- **A fix:** find or write a test that fails on the commit before the fix and passes on
  the commit with it. Run it at both commits in a separate worktree (`git worktree add`),
  never by resetting the user's checkout. If the test passes on both, the claim isn't
  proven by it.
- **Tests added:** does each new test import the code it's named after? Break that code on
  purpose (in the worktree) and confirm the test fails. A test that passes against broken
  code proves nothing.
- **Tests pass:** run the suite and report the count **run**, not the count written: skipped,
  todo and excluded files included. Compare the test count before and after the range.
- **Removed / dead code:** confirm nothing imports what was deleted, then run the orphan
  scan (mode 2) to see what the removal newly orphaned.
- **Wired up / called by:** trace a caller from a real entry point (a route, a page, a job,
  a CLI). A function with a caller that is itself unreachable is not wired.
- **No behaviour change:** run the same tests at both commits. Any edited assertion needs a
  reason.

| Verdict | Meaning |
|---|---|
| **Holds** | evidence that could have failed, didn't |
| **Doesn't hold** | the repository contradicts it; say what the truth is |
| **Can't tell** | the proof is outside what you can run (production, another service, a paid API); give the one command or query that would settle it |

**Name what would disprove the verdict before you give it.** A search describes where you
looked, not what exists: a literal grep can't see a path built from a template string, and
a repo search can't see a caller in another service.

**Run things safely.** Work in a `git worktree` or a copy, never by checking out or
resetting the user's branch. Keep every scratch file (probe scripts, before/after output)
inside the worktree or one folder you create for the check, never in `/tmp` or next to the
repo, and delete it when you're done. Mock every paid API. Cap loops and run under a timeout. If
something hangs, stop only the process you started, by its PID. Never kill processes by
name (`taskkill /IM node.exe`, `pkill node`): that stops the user's dev servers and other
agents.

### 4. Report for a founder

Lead with the count and the one claim that matters most. Plain words, no jargon the user
didn't use first.

```markdown
# What the agent said, checked

**<N> claims: <a> hold, <b> don't, <c> can't tell.** <One sentence on the claim that matters most.>

| # | The agent said | Verdict | What's actually true | Evidence |
|---|---|---|---|---|
| 1 | "Fixed the login redirect" | Doesn't hold | The fix is in `auth-old.ts`, which nothing loads; the live file is `auth.ts` | `file:line`; orphan scan |

## What to tell the agent
<ready-to-paste instructions for each claim that doesn't hold: what to do and how to prove it>

## Can't tell yet
<each open claim, with the one command or query that settles it>
```

End with a ready-to-paste message for the agent. It is what the founder will actually use.

## Mode 2: the agent is stuck

Signs: the same bug "fixed" three times; fixes that land and change nothing; two files
with the same name; the agent editing code the app never runs. The usual cause is that the
agent is working in a dead copy of the thing that is broken.

### 1. Find what's live and what's dead

```bash
python <skill-dir>/scripts/verify_claims.py orphans --repo <repo> [--alias @=src] [--entry "glob" ...]
```

It builds the import graph and reports:

- **orphans in rounds.** Round 1 is files nothing imports. Round 2 is files only round 1
  imports, and so on until nothing new appears. Dead code is often kept alive only by other
  dead code, so one pass finds the first layer and misses the rest;
- **twins**: one exported name defined in several files, with which copy is reachable from
  an entry point. This is how an agent fixes `AuthProvider` in the wrong file;
- **named but never imported**: files mentioned only as a path string, for example in a
  config. Read these before deciding: the config may load them;
- **kept alive only by tests**: tests import these, but nothing a user reaches does.

Entry points default to Next.js, Node and Python conventions (`main.py`, `app.py`,
`wsgi.py`, `manage.py`), plus any file with a `__main__` block and anything `package.json`
names in `main`, `bin` or `scripts`. Pass `--entry` to add others (workers, cron handlers).
If most of the code comes out unreachable, the script warns: an entry point is missing.
Fix that before believing the result.

The script says "unreachable", not "unused". It can't follow an import built from a string
at runtime (`importlib.import_module("app." + name)`, `require(variable)`), a plugin system
or another service. **Before deleting anything, import or start the app once** in a
worktree: a file the scan calls dead but the app loads will fail right there.

### 2. Prove which one runs

For each twin the agent keeps editing, prove the live one at runtime, not by name:
- add one temporary log line to each copy and exercise the feature once;
- or break the suspected-dead copy in a worktree and confirm nothing changes.

Remove the temporary log line afterwards.

### 3. Stop the loop

Give the founder three things:

1. **The verdict:** which copy is live, which is dead, and the evidence.
2. **Deletion by reference, not by name:** delete the dead copy only after confirming
   nothing imports it through any channel (alias import, relative import, `require`, dynamic
   `import()`, path strings in configs). Re-run the orphan scan after each deletion, since
   removing one layer often orphans the next. Delete in one commit per round, and run the
   build and tests after each.
3. **Rules for the agent**, for `CLAUDE.md` or `AGENTS.md`, so it doesn't happen again:

```markdown
- The live <thing> is `<path>`. `<dead path>` is dead and scheduled for deletion; never edit it.
- Before fixing a bug, name the file the running app loads for it, and how you know
  (a caller from a route or page, or a log line you saw fire).
- A fix is done when a test that failed before it passes after it. Say which test.
```

If the repo has the Cairn Memory plugin, the settled question ("which auth is live")
also belongs in its closed-questions ledger.

## Scoring (0–10)

Score how trustworthy the agent's reporting is on this range, with evidence for every
point. Half points for partly met. Mark N/A what doesn't apply and rescale.

1. Every claim names what changed specifically enough to check.
2. Every file a claim says changed was changed.
3. Every fix has a test that fails without it.
4. New tests import and exercise what they're named after.
5. "Tests pass" matches the count that actually ran, with nothing newly skipped.
6. Deleted code has no remaining importers, and the removal orphaned nothing unnoticed.
7. "Wired" claims trace to a real entry point.
8. "No behaviour change" survives the same tests at both commits.
9. No proof word ("verified", "works", "always") without evidence behind it.
10. Nothing the agent edited is dead code.

## Why each check exists

`references/incidents.md` has the real incidents behind these checks, including a dead
auth system kept alive only by other dead code that took three rounds of deletion to
surface, a live and a dead `AuthProvider` with the same name, a contract documented as
"one function, two callers" that had zero callers, and twelve probes in one project that
failed because the probe was wrong, not the code. Read it when a check seems like
overkill.
