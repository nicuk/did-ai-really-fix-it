# Verification recipes, one per kind of claim

Each recipe gives the cheapest evidence that could prove the claim **false**. Run
everything in a separate worktree so the user's checkout is never touched:

```bash
git worktree add ../verify-before <base>     # the code before the agent's change
git worktree add ../verify-after  <head>     # the code after it
# … run checks in each …
git worktree remove ../verify-before && git worktree remove ../verify-after
```

Mock paid APIs, cap loops and use a timeout (`timeout 120 npm test`). If something hangs,
stop only the process you started, by its PID.

## Contents
- "Fixed X"
- "Added tests for X"
- "All tests pass"
- "Removed dead code" / "cleaned up"
- "Wired up" / "now called by" / "integrated"
- "No behaviour change" / "pure refactor"
- "Verified" / "works" / "confirmed"
- "Updated the docs" / comments that assert reach
- Claims about the database, env vars or another service

---

## "Fixed X"

1. Find the test that reproduces X. If there isn't one, write the smallest one that
   exercises the reported symptom through the code path the app actually uses.
2. Run it in `verify-before`. **It must fail there.** If it passes before the fix, it
   doesn't test the bug, and the claim is unproven however green it is after.
3. Run it in `verify-after`. It must pass.
4. Confirm the changed file is the one the app runs. If the orphan scan lists it, or a
   twin of it is the reachable copy, the fix is in dead code: **Doesn't hold**.

## "Added tests for X"

1. Does each new test import X, directly or through the module X lives in? A test named
   `login.test.ts` that imports only a utility can't fail for a login bug.
2. Break X on purpose in `verify-after` (return a wrong value, throw) and run the new test.
   **It must fail.** Restore X afterwards.
3. Count the assertions. A test with one `expect(true).toBe(true)`, or one that asserts only
   that a function "is defined", proves nothing about X.

## "All tests pass"

1. Run the suite in `verify-after` and record **run / passed / failed / skipped**, not the
   number of tests written.
2. Diff the skip markers across the range: `.skip`, `.only`, `xit`, `todo`, `@pytest.mark.skip`,
   `xfail`, and excluded paths in the test config (`testPathIgnorePatterns`, `exclude`).
3. Compare the test count before and after. Fewer tests after a "fix" usually means the
   failing one was deleted or skipped.
4. Check that CI runs the same command, and that it isn't allowed to fail
   (`continue-on-error: true`, `|| true`).

## "Removed dead code" / "cleaned up"

1. For each deleted file, search for importers through every channel: alias imports
   (`@/lib/x`), relative imports, `require`, dynamic `import()`, and path strings in configs,
   middleware and workflows. The script's `removed-still-referenced` covers direct imports
   only.
2. Build and type-check in `verify-after`.
3. Run the orphan scan with `--smoke "<a command that loads the app and exits>"`. The smoke
   run starts HEAD in its own temporary worktree, so a deleted file that is still loaded at
   runtime (`importlib`, `require(variable)`) shows up as "app failed to start", which no
   import graph can see. The removal may also have orphaned the next layer.
4. **Delete by reference, never by name.** Two files can export the same name, and only
   one of them is live.

## "Wired up" / "now called by" / "integrated"

1. Name the entry point that reaches the code: a route, page, job, CLI or event handler.
2. Trace the call path from that entry point to the new code. Every hop must be reachable.
   A caller that is itself an orphan doesn't count.
3. If the path depends on a flag or env var, check its default in production.
4. Prove it at runtime when you can: one temporary log line, one request, then remove the
   log line.

## "No behaviour change" / "pure refactor"

1. Run the same test files in `verify-before` and `verify-after`. The results must match.
2. List assertions that were edited in the range. Each one needs a reason; an edited
   expectation is a behaviour change, even if it's a welcome one.
3. Diff the exported names and their signatures. A removed export, or a changed default
   parameter, is a change for callers.

## "Verified" / "works" / "confirmed"

Ask: verified **how**? The proof has to be something that could have failed: a test run,
a request, a query. If the agent can't point to it, the verdict is **Can't tell**, and the
report gives the command that would verify it. Words like "always", "every", "all callers"
and "fully" need the same treatment.

## "Updated the docs" / comments that assert reach

A doc or comment that says "called by X", "used in every route" or "verified" is a claim
too. Check it like code: find the caller. If the claim matters and has no enforcer, suggest
one assertion that fails when it stops being true, or remove the word.

## Claims about the database, env vars or another service

"The migration is applied", "the webhook is registered", "the env var is set in
production": the repository can't show these. The verdict is **Can't tell** unless you can
run the narrowest read-only check, such as a single `select` on the migration table, or
listing one env var's presence (never its value). Name that check in the report. Never
reach for a broad command (`db push`, redeploy) to find out.
