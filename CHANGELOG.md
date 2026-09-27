# Changelog

Each release raises `version` in `.claude-plugin/plugin.json` and is tagged `vX.Y.Z`.

## 1.2.0 (2026-09-27)

Driven by a measurement on three open-source repositories written largely by coding agents
(a notes app, a UI-framework monorepo and a data-platform monorepo), re-run afterwards on the
same pinned clones, commit ranges and labels. Precision is true findings over findings labelled
true or false (unclear ones left out).

| | Before | After |
|---|---|---|
| Claims findings | 63 | 19 |
| Claims precision | 4/58 (7%) | 4/13 (31%) |
| The 4 true claims findings still reported | | 4 of 4 |
| Orphans reported (notes app / UI monorepo / data monorepo) | 3 / 870 / 614 | 1 / 194 / 11 |
| Orphan precision, seeded sample | 3/43 (7%) | 15/31 (48%) |
| The 3 true orphans of the first sample still reported | | 3 of 3 |
| Planted problems found at defaults | 6/7 | 7/7 |
| False alarms on the 2 honest control commits | 0 | 0 |
| On a real 504-file codebase: orphans, and its guard's 22 dead files found | 31; 21 + 1 named only | 31; 21 + 1 named only (unchanged) |

What is still wrong after the change: claims keeps 9 false findings (5 proof words used to
describe, not to claim; 1 test-config change that adds rather than drops a test; 2 scripts
wired by a package.json script, not an import; 1 release-only "fix"). Most remaining false
orphans are modules loaded at runtime by name (Ray runners, template overlays, a framework's
folder conventions), which no import graph sees; `--smoke` and `--entry` are the answer there.
Ten of the fixes below (marked *) were added after the first re-measure's output showed the
gap, and the orphan sample was then re-drawn with the same seed, so part of that sample is
data the fixes were tuned on.

**Claims check**
- A backticked token is a file only if it has a folder or a file extension: `medallion.gold`,
  `json.loads` and `minio.bucket` are not files. "Exists nowhere" is reported only for a file
  the sentence says was added, changed, fixed, moved, renamed or removed, not for one it places
  in another repository or a dependency, and not for one a later sub-commit of a squash merge
  renamed.
- End-to-end specs (Playwright, Cypress, Maestro, `e2e/`) count as testing any change, and a
  test that reads a changed chart, template or config by path tests that change. A `.spec.json`
  is data, not a test.
- A deleted test replaced by a similarly named one (a rename plus a rewrite) is not "tests
  removed"; names are compared without their `test_`/`.test` affixes*. A skip is counted net
  per hunk, so a skip that is moved or re-emitted is not "added".
- Test-selection changes ignore an "only" in a comment, a line that only gained a trailing
  comma, a new package's own package.json*, and package.json lines that aren't about a test
  runner*.
- A finding about the whole diff is reported once per commit, under the claim it answers;
  the commit's other claims say where it is, so none of them reads as clean.
- "Verified: decoded both and compared SHA-256" and "verified before fixing (probe.py)" name
  their proof.

**Orphan scan**
- Entry patterns match case-sensitively on every OS (on Windows `app/components/Layout.tsx`
  was matching as a Next.js layout) and apply from every app or package folder, not only the
  repository root.
- New entry points: a Vite `index.html` module script, SvelteKit routes and hooks, Storybook
  stories, jest/vitest setup files, and `package.module:app` strings in any tracked file.
- Imports are followed from `.svelte`, `.vue` and `.mdx`* files; through workspace package
  names*, including named subpath exports that point at build output*; SvelteKit's `$lib`*;
  Nuxt's `~` (the app's folder, not the repo root); TypeScript's `./x.js` for `x.ts`*; Python
  packages under any `packages/*/src`, `services/*/src` or `src/`; and folders with a
  `pyproject.toml`*.
- When neither copy of a twin is reachable, it says "neither copy is reachable; is an entry
  missing?" instead of dropping the pair. Twin copies are listed first under "kept alive only
  by tests", so a cut-off list never hides them.
- The unreachable-code warning is per app, at 25% (was 50% for the whole repo), and counts code
  only tests reach; an app under 10 files warns only past half*.
- "Added tests" where no test file changed, but the changed source adds inline tests or
  extends a file that holds them (a Rust `#[test]`, a Go `TestX`, a Python `self_test`), is
  unproven, not contradicted. Found when this
  release's own pull request was checked by the Action.
- The self-test has 55 checks (was 23). Each new one has a case that must fire and one that
  must not, and was broken once on purpose to prove its case fails.

## 1.1.0 (2026-09-27)

**Added**
- The orphan scan reads import aliases from every `tsconfig`/`jsconfig` (comments and
  trailing commas allowed, `extends` followed, scoped to each config's folder) and resolves
  Python relative imports. On a 504-file app the result is unchanged without `--alias`;
  with the defaults displaced, the old version reported 175 orphans and this one 31.
- `--smoke "COMMAND"`: starts the app once in a temporary worktree of HEAD, removed
  afterwards, before any file is called dead. It catches imports built from strings, which
  no import graph can see.
- `--markdown` for the claims check, and a GitHub Action (`.github/workflows/verify-claims.yml`)
  that posts one sticky comment with each pull request's claims. It reports and never blocks.
- `evals/`: rebuildable fixtures for both test rounds, with the answer keys.
- `AGENTS.md`, and a CI step where this repo passes Cairn Memory's audit.
- A case study, linked from the Evidence section.

**Fixed**
- A path under a dot-folder (`.github/…`, `.claude-plugin/…`) was reported as "exists nowhere",
  because `lstrip("./")` strips characters, not the `./` prefix. Found by the Action on this
  release's own pull request; the self-test now plants a dot-folder claim.
- The self-test's temporary git repos could be redirected into the real repository when it
  ran inside a git hook, which exports `GIT_DIR`: that once committed its fixtures onto
  `main`. Every git call now drops inherited `GIT_*` variables, and the self-test runs
  itself with `GIT_DIR` pointing at a sentinel repo and fails if the sentinel changes.
- A local CI gate (`.githooks/pre-push`, enable with `git config core.hooksPath .githooks`)
  runs the push-triggered CI steps before a push; it now drops `GIT_*` before any step.
- The skill description contained `: `, which a strict YAML parser rejects. CI now parses
  the frontmatter strictly.

## 1.0.0 to 1.0.2 (2026-09-27)

First release, then the claims check counting a fix only when a sentence makes one, the
shared Cairn README layout and family section, and a link to the public principles and
evidence.
