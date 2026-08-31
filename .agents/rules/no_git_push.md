# Rule: Do Not Run Git Push

The agent must never execute any `git push` commands. All code changes should be made locally, and staging/committing can be done by the agent, but pushing to remote repositories must be done manually by the user.
