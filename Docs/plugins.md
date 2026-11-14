# Thule Plugin Developer Guide & PluginAPI SDK
Plugins are Python modules in `~/.local_ai_cli/Plugins/<slug>.py` or `./.thule/Plugins/<slug>.py`.
Every plugin must define:
1. `PLUGIN_META = {"name": "slug", "version": "0.1.0", "description": "..."}`
2. `def register(api: PluginAPI):`

Available PluginAPI methods:
- `api.cwd -> Path`: workspace root directory.
- `api.register_tool(name: str, description: str, parameters: dict, handler: Callable)`:
    * parameters: standard JSON Schema object {"type": "object", "properties": {...}, "required": [...]}
    * handler signature: `def handler(args: dict) -> str` (or `def handler(args: dict, api) -> str`)
- `api.register_command(name: str, description: str, handler: Callable)`:
    * registers slash command (e.g. name='todos' -> /todos)
    * handler signature: `def handler(arg_str: str) -> str`
- `api.register_hook(event: str, handler: Callable, matcher: str = "")`:
    * event: 'UserPromptSubmit' | 'PreToolUse' | 'PostToolUse' | 'Stop'
- `api.register_tab(name: str, title: str, render_fn: Callable, on_key: Optional[Callable] = None)`:
    * registers custom interactive TUI tab.
Plugins hot-reload automatically upon saving without restarting the CLI.
