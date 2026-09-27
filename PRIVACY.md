# Privacy

Cairn Verify collects nothing.

- The skill is instructions that Claude reads inside your own session.
- The script (`skills/verify-agent-claims/scripts/verify_claims.py`) reads files in the
  repository you name, and runs `git log`, `git diff`, `git show`, `git cat-file` and
  `git ls-files` there. It prints its findings to your terminal.
- It writes nothing to your project. `--self-test` creates a temporary folder and deletes it
  afterwards.
- It makes no network requests. It has no telemetry, no analytics, no accounts and no API keys.
- `--smoke "COMMAND"` runs your own command, on your machine, in a temporary `git worktree`
  of HEAD created in the system temp folder. Git records the worktree in your repository's
  `.git` folder while it exists; the script removes the worktree and that record afterwards,
  even if the command fails or times out. Your checked-out branch and files are not touched.
  What the command itself does (for example, installing packages) is up to the command.
- When the skill runs your tests to check a claim, it works in a separate `git worktree` and
  never changes your checked-out branch.
- The GitHub Action (`.github/workflows/verify-claims.yml`) runs only if a repository owner
  adds it. It runs on GitHub's servers, reads the pull request's commits and description,
  and posts or updates one comment on that pull request through GitHub's API using the
  workflow's own `GITHUB_TOKEN`. It sends nothing anywhere else, and the author receives
  nothing from it.

The author receives no data about you, your code or your use of the plugin.

Questions: open an issue at https://github.com/nicuk/did-ai-really-fix-it/issues
