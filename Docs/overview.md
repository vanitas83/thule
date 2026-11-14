# Thule Architecture Overview
Thule is a terminal-based local AI engineering assistant supporting multi-agent orchestration,
hot-reloading Python plugins, deterministic lifecycle hooks, git checkpoints, and TUI split-pane themes.
Key storage:
- Global: `~/.local_ai_cli/` (Agents, Themes, Skills, Plugins, Docs, commands, hooks.json)
- Project: `./.thule/` (Plugins, Skills, commands, hooks.json, docs) and `./Agents/`, `./Themes/`, `./docs/`.
