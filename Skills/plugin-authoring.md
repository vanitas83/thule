# Skill: Thule Plugin Authoring
Plugins live in `~/.local_ai_cli/Plugins/<slug>.py` (global) or `./.thule/Plugins/<slug>.py` (project).
Every plugin MUST define `PLUGIN_META` and `def register(api):`:

```python
PLUGIN_META = {
    "name": "my-plugin",
    "version": "0.1.0",
    "description": "What this plugin adds",
}

def register(api):
    # 1. Register an LLM tool
    def my_tool(args: dict) -> str:
        return f"Result: {args.get('query', '')}"

    api.register_tool(
        name="my_tool",
        description="Describe when the model should call this tool.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Input text"}},
            "required": ["query"],
        },
        handler=my_tool,
    )

    # 2. Register a /slash command
    def my_cmd(arg_str: str) -> str:
        return f"Ran /mycmd with: {arg_str}"

    api.register_command("mycmd", "Custom slash command", my_cmd)

    # 3. Optional lifecycle hook: UserPromptSubmit | PreToolUse | PostToolUse | Stop
    # api.register_hook("PostToolUse", lambda payload: None, matcher="write_file")
```

Rules:
- Tool handlers accept `(args: dict)` (or `(args: dict, api)`) and return a `str`.
- Prefer Python standard library + `httpx` so plugins load with zero extra dependencies.
- Use `create_plugin` (or `write_file` into `~/.local_ai_cli/Plugins/`) and Thule hot-reloads immediately.
