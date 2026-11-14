# Thule Lifecycle Hooks Reference
Configured in `~/.local_ai_cli/hooks.json` or `./.thule/hooks.json`.
Events:
- UserPromptSubmit: fires before sending prompt to LLM. Stdin: {"prompt": "...", "cwd": "..."}
- PreToolUse: fires before tool execution. Stdin: {"tool_name": "...", "args": {...}, "cwd": "..."}
- PostToolUse: fires after tool completes. Stdin: {"tool_name": "...", "args": {...}, "result": "...", "cwd": "..."}
- Stop: fires when agent turn finishes.
Interface:
- Hook commands receive JSON on stdin.
- Exit code 0: allow execution; stdout is passed as feedback to agent/user.
- Exit code 2: BLOCKS execution immediately; stderr/stdout reported as blocking reason.
- Regex matcher: optional field filtering by tool_name or prompt.
