# Thule Workflow Specification
Workflows are sequential multi-agent execution pipelines in `~/.local_ai_cli/Agents/<name>.json` or `./Agents/<name>.json`.
Schema:
- "name": string (slug)
- "description": string (1-line summary)
- "steps": array of objects, each containing:
    * "agent": string (name of an existing agent, e.g. 'planner', 'coder', 'reviewer')
    * "instruction": string (concrete task instruction for that step)
- "type": "workflow"
Context and diffs are handed off sequentially between agents across all pipeline steps.
