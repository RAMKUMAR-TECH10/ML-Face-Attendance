# Rule: Do Not Run Git Operations

The agent must never execute any Git commands (such as `git add`, `git commit`, `git status`, `git diff`, `git push`, etc.) without explicit instruction from the user in their prompt. All version control tracking and operations are to be handled manually by the user.
