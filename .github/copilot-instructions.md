# Copilot Instructions

## General rules
- Keep changes minimal: prefer editing one file per task unless the issue explicitly allows more.
- Never introduce secrets, credentials, or environment variables into source code.
- Never run destructive operations (e.g. `DROP`, `DELETE *`, `rm -rf`) without an explicit instruction.
- Do not install new dependencies unless the issue explicitly requests it.
- Match the existing code style and conventions in the file being edited.

## Pull requests
- Write a short, descriptive PR title and a one-paragraph summary of what changed and why.
- List every file changed and the reason in the PR description.
- Reference the originating issue number (e.g. `Closes #123`).

## Safety checklist (add to every PR description)
- [ ] No secrets or credentials added to source code
- [ ] No destructive operations introduced
- [ ] Changes limited to the minimum required by the issue
- [ ] Local test passed: `git pull; uvicorn web_app:app --reload;` then verified in browser
