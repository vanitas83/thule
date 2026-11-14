# Thule Skills & Claude SKILL.md Compatibility
Skills provide modular guidelines and prompt instructions.
Thule is reverse-compatible with Claude Code SKILL.md directories.
Search paths:
- ./.claude/skills/
- ./skills/
- ./.thule/Skills/ and ./.thule/skills/
- ~/.claude/skills/
- ~/.local_ai_cli/Skills/
Format:
- Claude format: `<folder>/<name>/SKILL.md` with YAML frontmatter (name, description) and optional scripts/, references/, examples/, resources/
- Flat format: `<name>.md`
Activation:
- In-turn mention: `@<skill-name>` anywhere in prompt
- Agent definition: `"skills": ["<skill-name>"]`
- Global: toggle in AGENTS tab or config.json `"active_skills"`.
