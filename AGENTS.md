---
authority: binding
status: live
---

# Cairn Verify: rules for agents working here

This repo is a Claude Code plugin: `skills/verify-agent-claims/SKILL.md` is the skill,
`skills/verify-agent-claims/scripts/verify_claims.py` its script, and
`.github/workflows/verify-claims.yml` an optional GitHub Action. It's public, and one of
three Cairn plugins whose shared principles and evidence are in `nicuk/cairn-principles`.

## Rules

- **Every check the script gains gets a planted case in `--self-test`**, and is broken once
  on purpose to prove the case fails. CI runs the self-test on every push.
- **The script is read-only and can't reach the network.** `--smoke` runs the user's own
  command in a temporary worktree that is always removed; it kills only its own child by PID.
  CI fails on a networking import, and `PRIVACY.md` must match what the script does.
- **The skill's frontmatter must parse as strict YAML.** Never put `: ` inside the
  description. CI checks it, because `claude plugin validate` doesn't.
- **README sections are shared across the family** and checked daily by the drift check in
  `nicuk/cairn-principles`: don't add, rename or reorder a `## ` heading here alone.
- **Every release raises `version` in `.claude-plugin/plugin.json`** and gets an entry in
  `CHANGELOG.md` and a git tag.
- **Nothing private goes in this repo:** no client or product names, no private codebases,
  no absolute paths from anyone's machine.
