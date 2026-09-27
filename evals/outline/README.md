# Outline fixture for the verify-agent-claims with/without comparison

This folder is the **key**. Keep it away from the agents being compared: never copy
anything from here into the fixture, and don't put the fixture inside this folder.

| File | What it is |
|---|---|
| `build_outline_fixture.py` | Builds the fixture from scratch. Standard library and git only. |
| `answer_key.json` | What was planted, where, and the correct verdicts. Written before planting. |
| `evals.json` | The two prompts and the assertions to grade the answers. |

## Rebuild the fixture

```bash
python build_outline_fixture.py --dest /some/new/folder/outline
```

It fetches `outline/outline` at `0e704e6ed26c07260ffb20cf486cdd7dedc9fa8c` (main on
2026-09-26) with `--depth 1`, tags it `base`, adds four commits with fixed author and dates,
and writes `agent-summary.txt` untracked in the fixture root. `--dest` must not exist; the
script never overwrites. It needs network access to GitHub once (a few seconds, about 2,800
files), or `--source <path to a local outline clone that has the commit>`.

The commit ids are deterministic. A correct rebuild prints:

```
base        0e704e6ed26c07260ffb20cf486cdd7dedc9fa8c
setup       f2147fffd08e5c0be002f56ade39ac0cf2c42320
honest_fix  629d1c4b99a1e27dbdee2724adaefe4273be3398
fix         07bc75c0c7be9f8d7ba4fbdf976dcaae7ce1fad4
cleanup     0612005f54c7c10c23fffbf1c87d266a67be01ff
```

If an id differs, the base fetch or a planted edit went wrong. The script refuses to
continue if any edit doesn't match exactly once.

## What the comparison should see

The agent says it fixed a sign-in loop and that its tests pass. The fix and its test sit in
a copy of the URL helpers that the running app never loads. A chain of three files that
nothing renders keeps the copy looking used. Details are in `answer_key.json`.

The script's output on the fixture, recorded 2026-09-27 on Windows 11 with Python 3.11:

| Command | Result | Time |
|---|---|---|
| `orphans --repo <fixture>` | 536 orphans in 8 rounds; the chain is `LoginFooter.tsx` in round 1, `RedirectHint.tsx` in round 2 and `useSafeRedirect.ts` in round 3; app/utils/url/index.ts is under "kept alive only by tests"; 8 twin lines say `live in app/utils/urls.ts; not reachable in app/utils/url/index.ts` | 67 s on the first run after checkout, 2.5 s after that |
| `claims --repo <fixture> --range base..HEAD --text agent-summary.txt` | 12 claims, all NO CONTRADICTION FOUND | 0.8 s |

The upstream base alone gives 533 orphans in 8 rounds, 542 kept alive only by tests and 25
twin names in 64 s (cold). So the plant is one signal among a lot of noise, most of it
false (see "Known noise" below). That is the point: the with-skill arm has to read the
output, not just run it.

`claims` finds nothing, by design. The skill tells the agent to always run `orphans`
too. An arm that runs only `claims` and trusts it will pass the fake fix.

The first run is slow because Windows reads the freshly checked-out files cold. The
scan itself takes about 2.5 s.

## Known noise in the output

These are the script's limits on this repository. They are not part of the plant:

- `server/**` (381 files) and `plugins/**` (141) come out as orphans because the server
  entry, server/index.ts, matches no default entry pattern.
- On Windows, `fnmatch` ignores case, so app/components/Layout.tsx,
  app/models/Template.ts, `.../Error.tsx` and `.../Loading.tsx` match the Next.js entry
  patterns `app/**/layout.*` and so on, and become entry points by accident. On Linux or
  macOS they don't. With case-sensitive matching (simulated), the plant looks the same:
  3 rounds and 8 twin lines. But "kept alive only by tests" grows from 543 to 1,010, and
  the copy is no longer among the 40 entries the text output prints. The twin lines still
  name it.
- app/scenes/Login/urls.ts is a real, live, Login-specific module with different names.
  It is not the twin. An answer that calls it dead is wrong.

## Tests

The fixture has no `node_modules`, and the planted tests were not run, because installing
packages was out of scope. Their logic was checked with node on the extracted functions. The fixed `isAllowedLoginRedirect` passes all ten cases, the live
one returns `true` for `/logout/`, and the new `isSplittablePath` returns `false` for
`/Settings/members`. The copy imports exactly what app/utils/urls.ts imports, and
app/utils/urls.test.ts already runs in the same vitest `app` project. So
app/utils/url/index.test.ts should pass under `yarn test:app`. Confirm that once in a
worktree before relying on "the test passes".

## Running the comparison

1. Build two fresh fixtures, one per arm, from the same builder.
2. Give each arm the prompt from `evals.json` with the fixture as its working directory.
   Give the with-skill arm the skill.
3. Grade each answer against that eval's assertions. A failed `must` assertion fails the
   eval.
4. After each run, check that `git -C <fixture> rev-parse HEAD` still prints `0612005f…`
   and that `git status` shows only `agent-summary.txt` (assertions A10 and B9).
