# Changelog

Each release raises `version` in `.claude-plugin/plugin.json` and is tagged `vX.Y.Z`.

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
