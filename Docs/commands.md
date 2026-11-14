# Thule Custom Slash Commands
Defined as Markdown files in `~/.local_ai_cli/commands/<name>.md`, `./.thule/commands/<name>.md`, or `./.opencode/commands/<name>.md`.
The filename becomes the command trigger (e.g. audit.md -> /audit).
Inside the Markdown file, use `$ARGUMENTS` to capture user arguments passed after the command.
