# Thule Theme Specification
Themes are JSON files in `~/.local_ai_cli/Themes/<name>.json` or `./Themes/<name>.json`.
Schema:
- "name": string (slug)
- "accent": string (#rrggbb hex color)
- "user": string (#rrggbb hex color)
- "ok": string (#rrggbb hex color for success and git additions)
- "warn": string (#rrggbb hex color for warnings and diff context)
- "err": string (#rrggbb hex color for errors and deletions)
- "grad_start": string (#rrggbb hex color for header gradient start)
- "grad_end": string (#rrggbb hex color for header gradient end)
- "layout": optional object with keys:
    * header_style: 'default' | 'antigravity'
    * header_align: 'left' | 'center' | 'right'
    * header_rule: 'line' | 'none'
    * tab_separator: string (e.g. '   ', ' | ')
    * meta_position: 'below' | 'top' | 'hidden'
    * chat_style: 'default' | 'cli'
    * split_mode: 'horizontal' | 'vertical' | 'inspector_only'
    * split_ratio: float (0.20 to 0.65)
    * panel_box: 'rounded' | 'heavy' | 'double' | 'square' | 'minimal' | 'none'
    * prompt_prefix: string (e.g. '> ', '❯ ', 'λ ')
    * prompt_rule: 'line' | 'none'
    * compact_logo: bool
