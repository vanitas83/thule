# Thule Agent Specification
Agents are JSON files in `~/.local_ai_cli/Agents/<name>.json` or `./Agents/<name>.json`.
Schema:
- "name": string (slug identifier, lowercase alphanumeric with hyphens)
- "description": string (1-line summary)
- "system_prompt": string (comprehensive instructions for the LLM)
- "provider": string (optional endpoint override, empty to inherit)
- "base_url": string (optional URL override, empty to inherit)
- "api_key": string (optional API key override, empty to inherit)
- "model": string (optional model override, empty to inherit)
- "skills": array of strings (skill names in Skills/ to inject into prompt)
- "type": "agent"
Execution modes: 'build' (read/write/shell allowed) or 'plan' (read-only, create_artifact).
