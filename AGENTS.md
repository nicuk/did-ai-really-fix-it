---
authority: binding
status: live
---

# Cairn Verify: rules for agents working here

This repo is a Claude Code plugin: `skills/verify-agent-claims/SKILL.md` is the skill,
`skills/verify-agent-claims/scripts/verify_claims.py` its script, and
`.github/workflows/verify-claims.yml` an optional GitHub Action. It's public, and one of
three Cairn plugins whose shared principles and evidence are in `nicuk/cairn-principles`.

Each rule names what enforces it, or says that nothing does. The audit warns on a rule here
that does neither. Everything in `.github/workflows/self-test.yml` runs on every push.

## Rules

- **Every check the script gains gets a planted case in `--self-test`**, and is broken once on
  purpose to prove the case fails. Enforced by `skills/verify-agent-claims/scripts/verify_claims.py`:
  its self-test fails when a check on its list stops firing. That a new check joins the list is
  not enforced: add it in the same commit as the check.
- **The script is read-only and can't reach the network.** `--smoke` runs the user's own command
  in a temporary worktree that is always removed; it kills only its own child by PID. No
  networking import, in any form, is enforced by `.github/scripts/check_privacy.py`. The worktree
  cleanup is enforced by the self-test's `smoke` case. That the script writes nothing else, and
  that `PRIVACY.md` still describes it, is not enforced: re-read both whenever either changes.
- **The skill's frontmatter must parse as strict YAML.** Never put `: ` inside the description.
  Enforced by `.github/scripts/check_frontmatter.py`, because `claude plugin validate` doesn't.
- **Numbers the docs state about the repo come from their source.** Enforced by
  `.github/scripts/check_claims.py`: every "N checks" in the README, `plugin.json` and the demo
  images must equal the count the self-test reports.
- **README `## ` sections are shared across the family:** don't add, rename or reorder one here
  alone. Enforced by the daily drift check in `nicuk/cairn-principles`.
- **Every release raises `version` in `.claude-plugin/plugin.json`, with an entry in
  `CHANGELOG.md` and a git tag.** Version and changelog are enforced by
  `.github/scripts/check_claims.py`. The tag is not enforced: tag `vX.Y.Z` when the release merges.
- **Nothing private goes in this repo:** no client or product names, no private codebases, no
  absolute paths from anyone's machine. Absolute paths are enforced by
  `.github/scripts/check_privacy.py`. Names and private code are not enforced: check before committing.
- **This repo passes Cairn Memory's audit with no warnings.** Enforced by the last step of
  `.github/workflows/self-test.yml`, pinned to a Memory release. Raise the pin on purpose, once
  this repo passes the newer release.
