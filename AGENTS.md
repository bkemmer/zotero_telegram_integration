# Repository Rules

## Git
- **Never commit** unless explicitly asked by the user (e.g., "commit this", "git commit", "push")
- Do not run `git add`, `git commit`, or `git push` on your own

## File Discovery
- When reading or listing repository files, **skip** `.git/` and `.venv/` directories
- Use `find . -not -path './.git/*' -not -path './.venv/*'` or `ls` with appropriate filters
- Do not read or suggest changes to files inside `.git/` or `.venv/`
