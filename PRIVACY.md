# Privacy

Cairn Verify collects nothing.

- The skill is instructions that Claude reads inside your own session.
- The script (`skills/verify-agent-claims/scripts/verify_claims.py`) reads files in the
  repository you name, and runs `git log`, `git diff`, `git show`, `git cat-file` and
  `git ls-files` there. It prints its findings to your terminal.
- It writes nothing to your project. `--self-test` creates a temporary folder and deletes it
  afterwards.
- It makes no network requests. It has no telemetry, no analytics, no accounts and no API keys.
- When the skill runs your tests to check a claim, it works in a separate `git worktree` and
  never changes your checked-out branch.

The author receives no data about you, your code or your use of the plugin.

Questions: open an issue at https://github.com/nicuk/did-ai-really-fix-it/issues
