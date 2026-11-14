# Thule — Local AI CLI & Autonomous Engineering Assistant

> High-performance terminal AI engineering assistant with multi-agent orchestration, hot-reloading Python plugins, deterministic lifecycle hooks, visual TUI themes, and git checkpoints.

---

## Highlights

- **Multi-Agent Pipelines**: Compose specialized agents (`planner`, `coder`, `reviewer`) into sequential workflow pipelines with automated context handoff.
- **Python Plugin SDK (`PluginAPI`)**: Extend Thule at runtime with custom LLM tools, slash commands, lifecycle hooks, and interactive TUI tabs.
- **Deterministic Lifecycle Hooks**: Intercept prompts and tool calls with external shell scripts or linters; block unsafe executions via exit code 2.
- **Split-Pane TUI**: Interactive terminal interface with live token streaming, mouse scrolling, split-pane navigation, and 7-stop hex theme gradients.
- **Safety & Checkpoints**: `plan` mode for read-only exploration; automatic git diff checkpoints with instant rewind support.
- **Self-Documenting AI**: Integrated documentation engine allowing the AI to read authoritative schemas when generating agents, themes, workflows, and plugins.

---

## Documentation

Full documentation is available in the [`docs/`](docs/) directory:

- [**Overview & Architecture**](Docs/overview.md) — System diagram, core concepts, and directory layout.
- [**Agent Specification & Authoring**](Docs/agents.md) — Schema, system prompts, skills, and execution modes.
- [**Skills & Claude SKILL.md Compatibility**](Docs/skills.md) — Full support for Claude Code `SKILL.md` folders and YAML frontmatter.
- [**Theme & Layout Guide**](Docs/themes.md) — Palettes, gradient engine, and split-pane layout configurations.
- [**Workflow Pipelines**](Docs/workflows.md) — Multi-step sequential agent chains and state passing.
- [**Plugin Authoring & SDK**](Docs/plugins.md) — Writing Python plugins with `PluginAPI` for custom tools, commands, and tabs.
- [**Lifecycle Hooks Reference**](Docs/hooks.md) — Intercepting `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop`.
- [**Custom Slash Commands**](Docs/commands.md) — Reusable prompt templates and `$ARGUMENTS` substitution.
- [**Configuration & Navigation**](Docs/configuration.md) — Runtime configuration, provider profiles, and keyboard shortcuts.

---

## Quickstart

### 1. Launch Thule
```bash
python3 thule.py
```

### 2. Configure Endpoint
Press `Ctrl+M` or type `/connect` to set up your LLM provider (Ollama, LM Studio, vLLM, OpenAI, Anthropic, or custom endpoint).

### 3. Generate New Assets
Ask the AI or use the `/create` slash command:
```bash
# Create a specialized agent
/create agent security-auditor audits code for injection vulnerabilities and OWASP flaws

# Create a custom theme
/create theme dracula dark purple and slate with rounded borders

# Create a multi-step workflow
/create workflow tdd-pipeline planner authors tests, coder implements, reviewer verifies

# Create a Python plugin
/create plugin todo-scanner scans workspace for TODO and FIXME comments
```

### 4. Read Documentation In-App
```bash
/docs          # Show overview of all documentation topics
/docs agents   # View agent authoring guide and schema
/docs plugins  # View plugin SDK guide and examples
```

---

## License

MIT
