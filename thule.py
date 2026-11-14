#!/usr/bin/env python3
"""Thule Local AI CLI — minimalist terminal coding assistant & agent orchestrator."""

__version__ = "1.0.0"

import argparse
import atexit
import base64
import signal
import ast
import asyncio
import difflib
import importlib.util
import json
import select
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from rich.align import Align
from rich.box import DOUBLE, HEAVY, MINIMAL, ROUNDED, SQUARE
from rich.console import Console, Group
from rich.layout import Layout
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

_READ_KEY_PUSHBACK = bytearray()
if os.name == "nt":
    import msvcrt

    _READ_KEY_PUSHBACK = bytearray()
else:
    import termios
    import tty

_INITIAL_TERMIOS = None
_TUI_ACTIVE = False
_TERMINAL_RESIZED = False

if os.name != "nt":
    try:
        if sys.stdin.isatty():
            _INITIAL_TERMIOS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        _INITIAL_TERMIOS = None

    def _on_sigwinch(signum, frame):
        global _TERMINAL_RESIZED
        _TERMINAL_RESIZED = True
        try:
            sz = shutil.get_terminal_size(fallback=(80, 24))
            os.environ["COLUMNS"] = str(sz.columns)
            os.environ["LINES"] = str(sz.lines)
        except Exception:
            pass

    try:
        signal.signal(signal.SIGWINCH, _on_sigwinch)
    except Exception:
        pass


def _consume_terminal_resized() -> bool:
    global _TERMINAL_RESIZED
    if _TERMINAL_RESIZED:
        _TERMINAL_RESIZED = False
        return True
    return False


def _restore_terminal_state() -> None:
    global _TUI_ACTIVE
    if os.name != "nt" and _INITIAL_TERMIOS is not None:
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _INITIAL_TERMIOS)
        except Exception:
            pass
    if _TUI_ACTIVE:
        _TUI_ACTIVE = False
        try:
            sys.stdout.write("\033[?2026l\033[?25h\033[?1006l\033[?1000l\033[?2004l\033[?1049l")
            sys.stdout.flush()
        except Exception:
            pass


atexit.register(_restore_terminal_state)
_ORIG_EXCEPTHOOK = sys.excepthook


def _thule_excepthook(exc_type, exc_value, exc_tb):
    _restore_terminal_state()
    _ORIG_EXCEPTHOOK(exc_type, exc_value, exc_tb)


sys.excepthook = _thule_excepthook

def _try_paste_clipboard_image() -> Optional[str]:
    image_bytes = None
    image_ext = "png"
    if shutil.which("wl-paste"):
        try:
            types_res = subprocess.run(["wl-paste", "--list-types"], capture_output=True, text=True, timeout=0.6)
            if types_res.returncode == 0:
                avail = set(types_res.stdout.splitlines())
                for mime in ("image/png", "image/jpeg", "image/webp"):
                    if mime in avail:
                        img_res = subprocess.run(["wl-paste", "--no-newline", "--type", mime], capture_output=True, timeout=1.5)
                        if img_res.returncode == 0 and img_res.content:
                            image_bytes = img_res.content
                            image_ext = "jpg" if mime == "image/jpeg" else mime.split("/")[-1]
                            break
        except Exception:
            pass
    if not image_bytes and shutil.which("xclip"):
        try:
            img_res = subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"], capture_output=True, timeout=1.5)
            if img_res.returncode == 0 and img_res.content:
                image_bytes = img_res.content
                image_ext = "png"
        except Exception:
            pass
    if image_bytes:
        art_dir = BASE_DIR / "Artifacts"
        art_dir.mkdir(parents=True, exist_ok=True)
        img_name = f"pasted_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{image_ext}"
        img_path = art_dir / img_name
        img_path.write_bytes(image_bytes)
        return f"PASTE:@{img_path} "
    return None


def read_key(timeout: Optional[float] = None) -> str:
    global _READ_KEY_PUSHBACK
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        if _READ_KEY_PUSHBACK:
            raw = bytes(_READ_KEY_PUSHBACK)
            _READ_KEY_PUSHBACK.clear()
        else:
            if _consume_terminal_resized():
                return "RESIZE"
            if timeout is not None:
                rlist, _, _ = select.select([fd], [], [], timeout)
                if not rlist:
                    return "RESIZE" if _consume_terminal_resized() else ""
            else:
                while True:
                    rlist, _, _ = select.select([fd], [], [], 0.25)
                    if rlist:
                        break
                    if _consume_terminal_resized():
                        return "RESIZE"
            raw = os.read(fd, 64)

        # === BEGIN BRACKETED PASTE BLOCK ===
        if raw.startswith(b"\x1b[200~"):
            pbuf = bytearray(raw[6:])
            while b"\x1b[201~" not in pbuf:
                rlist, _, _ = select.select([fd], [], [], 0.1)
                if rlist:
                    chunk = os.read(fd, 1024)
                    if not chunk:
                        break
                    pbuf.extend(chunk)
                else:
                    break
            end_idx = pbuf.find(b"\x1b[201~")
            if end_idx != -1:
                paste_bytes = pbuf[:end_idx]
                rest = pbuf[end_idx + 6:]
                if rest:
                    _READ_KEY_PUSHBACK.extend(rest)
            else:
                paste_bytes = pbuf
            decoded = paste_bytes.decode("utf-8", errors="ignore")
            if not decoded.strip():
                img_paste = _try_paste_clipboard_image()
                if img_paste:
                    return img_paste
            return "PASTE:" + decoded
        # === END BRACKETED PASTE BLOCK ===

        if raw == b"\x16":
            img_paste = _try_paste_clipboard_image()
            if img_paste:
                return img_paste
            for clip_cmd in (["wl-paste", "-n"], ["xclip", "-o", "-selection", "clipboard"], ["xsel", "-ob"]):
                try:
                    res = subprocess.run(clip_cmd, capture_output=True, text=True, timeout=1.0)
                    if res.returncode == 0 and res.stdout:
                        return "PASTE:" + res.stdout
                except Exception:
                    continue
            return "CTRL_V"
        if not raw:
            return ""
        if raw == b"\x1b":
            rlist, _, _ = select.select([fd], [], [], 0.03)
            if rlist:
                raw += os.read(fd, 64)
        if raw.startswith(b"\x1b[M") and len(raw) >= 6:
            btn = raw[3]
            if btn == 96:
                return "WHEEL_UP"
            if btn == 97:
                return "WHEEL_DOWN"
            return ""
        seq = raw.decode("utf-8", errors="ignore")
        if "\x1b[<64;" in seq or "\x1b[64;" in seq:
            return "WHEEL_UP"
        if "\x1b[<65;" in seq or "\x1b[65;" in seq:
            return "WHEEL_DOWN"
        if seq.startswith("\x1b[<"):
            return ""
        seq_map = {
            "\x1b": "ESC",
            "\x1b[A": "UP",
            "\x1b[B": "DOWN",
            "\x1b[D": "LEFT",
            "\x1b[C": "RIGHT",
            "\x1bOA": "UP",
            "\x1bOB": "DOWN",
            "\x1bOD": "LEFT",
            "\x1bOC": "RIGHT",
            "\x1b[1;2A": "WHEEL_UP",
            "\x1b[1;2B": "WHEEL_DOWN",
            "\x1b[1;5A": "WHEEL_UP",
            "\x1b[1;5B": "WHEEL_DOWN",
            "\x1b[H": "HOME",
            "\x1b[F": "END",
            "\x1bOH": "HOME",
            "\x1bOF": "END",
            "\x1b[1~": "HOME",
            "\x1b[4~": "END",
            "\x1b[3~": "DEL",
            "\x1b[5~": "PGUP",
            "\x1b[6~": "PGDN",
            "\x1b[Z": "SHIFT_TAB",
            "\x1b\r": "SHIFT_ENTER",
            "\x1b\n": "SHIFT_ENTER",
            "\x1b[13;2u": "SHIFT_ENTER",
            "\x1b[27;2;13~": "SHIFT_ENTER",
            "\x1b[13;2~": "SHIFT_ENTER",
            "\x1b[13;5u": "SHIFT_ENTER",
            "\r": "ENTER",
            "\n": "ENTER",
            "\t": "TAB",
            "\x7f": "BACKSPACE",
            "\x08": "BACKSPACE",
            "\x01": "CTRL_A",
            "\x02": "PGUP",
            "\x05": "CTRL_E",
            "\x06": "PGDN",
            "\x07": "CTRL_G",
            "\x0b": "CTRL_K",
            "\x0f": "CTRL_O",
            "\x10": "CTRL_P",
            "\x12": "CTRL_R",
            "\x15": "CTRL_U",
            "\x17": "CTRL_W",
            "\x18": "CTRL_X",
            "\x19": "CTRL_Y",
        }
        if seq == "\x03":
            raise KeyboardInterrupt
        if seq in seq_map:
            mapped = seq_map[seq]
            if mapped in ("UP", "DOWN", "WHEEL_UP", "WHEEL_DOWN"):
                while select.select([fd], [], [], 0.0)[0]:
                    peek = os.read(fd, 64)
                    if not peek:
                        break
                    p_str = peek.decode("utf-8", errors="ignore")
                    if p_str == seq or (mapped == "WHEEL_UP" and "\x1b[<64;" in p_str) or (mapped == "WHEEL_DOWN" and "\x1b[<65;" in p_str):
                        continue
                    _READ_KEY_PUSHBACK.extend(peek)
                    break
            return mapped
        return seq if len(seq) == 1 else ""
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _resolve_base_dir() -> Path:
    env_home = os.environ.get("THULE_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser().resolve()
    script_dir = Path(__file__).resolve().parent
    if script_dir.name == "bin" or not os.access(script_dir, os.W_OK):
        return (Path.home() / ".local_ai_cli").resolve()
    return script_dir


def _atomic_write_text(target: Path, content: str, encoding: str = "utf-8") -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding=encoding)
    os.replace(tmp, target)


BASE_DIR = _resolve_base_dir()
CONFIG_FILE = BASE_DIR / "config.json"
PROFILES_FILE = BASE_DIR / "profiles.json"
HISTORY_FILE = BASE_DIR / "history.json"
THEMES_DIR = BASE_DIR / "Themes"
AGENTS_DIR = BASE_DIR / "Agents"
SKILLS_DIR = BASE_DIR / "Skills"
PLUGINS_DIR = BASE_DIR / "Plugins"
DOCS_DIR = BASE_DIR / "Docs"
SESSIONS_DIR = BASE_DIR / "Sessions"
COMMANDS_DIR = BASE_DIR / "commands"
CHECKPOINTS_DIR = BASE_DIR / "Checkpoints"
ARTIFACTS_DIR = BASE_DIR / "Artifacts"
HOOKS_FILE = BASE_DIR / "hooks.json"

LOGO_LINES = [
    "   )=(",
    "   ( )",
    "   ) (",
    "o__(=)__o",
    "   | |_           _",
    "   | | |         | |",
    "   | | |         | |",
    "   | | |__  _   _| | ___",
    "   | | '_ \\| | | | |/ _ \\",
    "   | | | | | |_| | |  __/",
    "   | |_| |_|\\__,_|_|\\___|",
    "   | |",
    "   | |",
    "   \\ /",
    "    V",
]

BOX_STYLES: Dict[str, Any] = {
    "rounded": ROUNDED,
    "heavy": HEAVY,
    "double": DOUBLE,
    "square": SQUARE,
    "minimal": MINIMAL,
    "none": None,
}

DEFAULT_LAYOUT: Dict[str, Any] = {
    "header_style": "default",     # "default" | "antigravity"
    "header_align": "center",      # "left" | "center" | "right"
    "header_rule": "line",         # "line" | "none"
    "tab_separator": "   ",        # e.g. "   ", " | ", " · "
    "meta_position": "below",      # "below" | "top" | "hidden"
    "chat_style": "default",       # "default" | "cli"
    "split_mode": "horizontal",    # "horizontal" | "vertical" | "inspector_only"
    "split_ratio": 0.36,           # 0.20 .. 0.65
    "panel_box": "rounded",        # "rounded" | "heavy" | "double" | "square" | "minimal" | "none"
    "prompt_prefix": "> ",         # e.g. "> ", "❯ ", "λ "
    "prompt_rule": "line",         # "line" | "none"
    "compact_logo": False,         # True forces compact logo
}

DEFAULT_THEME: Dict[str, Any] = {
    "name": "default",
    "accent": "#7b3ae5",
    "user": "#3b82f6",
    "ok": "#22c55e",
    "warn": "#eab308",
    "err": "#ef4444",
    "grad_start": "#3b82f6",
    "grad_end": "#ab04d9",
    "layout": DEFAULT_LAYOUT.copy(),
}

def sanitize_layout(raw: Any) -> Dict[str, Any]:
    ly = DEFAULT_LAYOUT.copy()
    if not isinstance(raw, dict):
        return ly
    if str(raw.get("header_style", "")).lower() in ("default", "antigravity"):
        ly["header_style"] = str(raw["header_style"]).lower()
    if str(raw.get("header_align", "")).lower() in ("left", "center", "right"):
        ly["header_align"] = str(raw["header_align"]).lower()
    if str(raw.get("header_rule", "")).lower() in ("line", "none"):
        ly["header_rule"] = str(raw["header_rule"]).lower()
    if "tab_separator" in raw and raw["tab_separator"] is not None:
        ly["tab_separator"] = str(raw["tab_separator"])[:8]
    if str(raw.get("meta_position", "")).lower() in ("below", "top", "hidden"):
        ly["meta_position"] = str(raw["meta_position"]).lower()
    if str(raw.get("chat_style", "")).lower() in ("default", "cli"):
        ly["chat_style"] = str(raw["chat_style"]).lower()
    if str(raw.get("split_mode", "")).lower() in ("horizontal", "vertical", "inspector_only"):
        ly["split_mode"] = str(raw["split_mode"]).lower()
    if "split_ratio" in raw:
        try:
            ly["split_ratio"] = max(0.20, min(0.65, float(raw["split_ratio"])))
        except Exception:
            pass
    if str(raw.get("panel_box", "")).lower() in BOX_STYLES:
        ly["panel_box"] = str(raw["panel_box"]).lower()
    if "prompt_prefix" in raw and raw["prompt_prefix"] is not None:
        ly["prompt_prefix"] = str(raw["prompt_prefix"])[:12]
    if str(raw.get("prompt_rule", "")).lower() in ("line", "none"):
        ly["prompt_rule"] = str(raw["prompt_rule"]).lower()
    if "compact_logo" in raw:
        ly["compact_logo"] = bool(raw["compact_logo"])
    return ly

TABS = ["prompt", "agents", "advanced", "history", "settings"]

PROVIDER_PRESETS: List[Dict[str, str]] = [
    {
        "id": "ollama",
        "label": "Ollama (Local)",
        "url": "http://localhost:11434/v1",
        "model": "qwen2.5-coder:14b",
        "env_key": "",
        "hint": "Local daemon on port 11434",
    },
    {
        "id": "llamacpp",
        "label": "llama.cpp Server",
        "url": "http://localhost:8080/v1",
        "model": "local-model",
        "env_key": "",
        "hint": "llama-server --port 8080",
    },
    {
        "id": "lmstudio",
        "label": "LM Studio (Local)",
        "url": "http://localhost:1234/v1",
        "model": "local-model",
        "env_key": "",
        "hint": "Local OpenAI-compatible server on 1234",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter API",
        "url": "https://openrouter.ai/api/v1",
        "model": "anthropic/claude-sonnet-4",
        "env_key": "OPENROUTER_API_KEY",
        "hint": "Unified cloud gateway (uses OPENROUTER_API_KEY)",
    },
    {
        "id": "gemini",
        "label": "Google Gemini API",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
        "env_key": "GEMINI_API_KEY",
        "hint": "OpenAI-compat endpoint (uses GEMINI_API_KEY)",
    },
    {
        "id": "openai",
        "label": "OpenAI / Custom Endpoint",
        "url": "https://api.openai.com/v1",
        "model": "gpt-4.1",
        "env_key": "OPENAI_API_KEY",
        "hint": "Any OpenAI-compatible /v1 endpoint",
    },
]


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (59, 130, 246)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _interpolate_hex(start_hex: str, end_hex: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = _hex_to_rgb(start_hex)
    r2, g2, b2 = _hex_to_rgb(end_hex)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def universal_gradient_text(
    text: str,
    theme: Dict[str, str],
    start_col: int,
    screen_width: int,
    bold: bool = True,
) -> Text:
    start_c = theme.get("grad_start", DEFAULT_THEME["grad_start"])
    end_c = theme.get("grad_end", DEFAULT_THEME["grad_end"])
    denom = max(1, screen_width - 1)
    out = Text()
    for i, ch in enumerate(text):
        t = (start_col + i) / denom
        color = _interpolate_hex(start_c, end_c, t)
        out.append(ch, style=f"bold {color}" if bold else color)
    return out


def build_logo_text(theme: Dict[str, str], screen_width: int, lines: Optional[List[str]] = None) -> Text:
    use_lines = lines or LOGO_LINES
    max_w = max(len(line) for line in use_lines)
    start_col = max(0, (screen_width - max_w) // 2)
    logo = Text()
    for idx, line in enumerate(use_lines):
        padded = line.ljust(max_w)
        logo.append_text(universal_gradient_text(padded, theme, start_col, screen_width, bold=True))
        if idx < len(use_lines) - 1:
            logo.append("\n")
    return logo


def _emit_osc52(text: str) -> bool:
    try:
        if not sys.stdout.isatty():
            return False
        raw = text.encode("utf-8")[:74994]
        b64 = base64.b64encode(raw).decode("ascii")
        sys.stdout.write(f"\033]52;c;{b64}\a")
        sys.stdout.flush()
        return True
    except Exception:
        return False


def copy_to_clipboard(text: str) -> tuple[bool, str]:
    osc_ok = _emit_osc52(text)
    candidates = [
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["pbcopy"],
        ["clip"],
    ]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text, text=True, check=True, timeout=3)
                return True, f"{cmd[0]}+osc52" if osc_ok else cmd[0]
            except Exception:
                continue
    if osc_ok:
        return True, "osc52"
    return False, "no clipboard utility found (wl-copy / xclip / pbcopy)"


def get_git_summary() -> str:
    try:
        br = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if br.returncode != 0:
            return ""
        branch = br.stdout.strip()
        st = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        dirty = len([ln for ln in st.stdout.splitlines() if ln.strip()])
        return f"git:{branch}*{dirty}" if dirty else f"git:{branch}"
    except Exception:
        return ""


@dataclass
class AgentDef:
    name: str
    description: str
    system_prompt: str
    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    skills: List[str] = field(default_factory=list)
    type: str = "agent"


@dataclass
class WorkflowStep:
    agent: str
    instruction: str


@dataclass
class WorkflowDef:
    name: str
    description: str
    steps: List[WorkflowStep] = field(default_factory=list)
    type: str = "workflow"


@dataclass
class Config:
    provider: str = "ollama"
    base_url: str = "http://localhost:11434/v1"
    api_key: str = ""
    model: str = "qwen3:8b"
    temperature: float = 0.2
    num_ctx: int = 32768
    max_tokens: int = 4096
    theme: str = "default"
    active_mode: str = "agent"
    active_name: str = "default"
    execution_mode: str = "build"
    active_skills: List[str] = field(default_factory=list)
    always_allow: List[str] = field(default_factory=list)
    disabled_plugins: List[str] = field(default_factory=list)
    auto_approve: bool = False
    auto_commit: bool = False
    auto_docs_skills: bool = True
    stream: bool = True
    command_timeout: int = 60

    @classmethod
    def load(cls) -> "Config":
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text())
                return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        _atomic_write_text(CONFIG_FILE, json.dumps(asdict(self), indent=2))


def load_profiles() -> List[Dict[str, str]]:
    if PROFILES_FILE.exists():
        try:
            data = json.loads(PROFILES_FILE.read_text())
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def save_profile(name: str, provider: str, base_url: str, model: str, api_key: str) -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    profiles = [p for p in load_profiles() if p.get("name") != name]
    profiles.insert(
        0,
        {
            "name": name,
            "provider": provider,
            "base_url": base_url.rstrip("/"),
            "model": model,
            "api_key": api_key,
        },
    )
    _atomic_write_text(PROFILES_FILE, json.dumps(profiles[:12], indent=2))


def seed_defaults() -> None:
    THEMES_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    external_themes = [
        {
            "name": "mono",
            "accent": "#e5e7eb",
            "user": "#ffffff",
            "ok": "#d1d5db",
            "warn": "#9ca3af",
            "err": "#ffffff",
            "grad_start": "#ffffff",
            "grad_end": "#6b7280",
        },
        {
            "name": "cyan",
            "accent": "#06b6d4",
            "user": "#38bdf8",
            "ok": "#22c55e",
            "warn": "#eab308",
            "err": "#ef4444",
            "grad_start": "#06b6d4",
            "grad_end": "#3b82f6",
        },
        {
            "name": "emerald",
            "accent": "#10b981",
            "user": "#34d399",
            "ok": "#22c55e",
            "warn": "#f59e0b",
            "err": "#ef4444",
            "grad_start": "#10b981",
            "grad_end": "#06b6d4",
        },
        {
            "name": "amber",
            "accent": "#f59e0b",
            "user": "#fbbf24",
            "ok": "#22c55e",
            "warn": "#f97316",
            "err": "#ef4444",
            "grad_start": "#f59e0b",
            "grad_end": "#ea580c",
        },
        {
            "name": "ruby",
            "accent": "#e11d48",
            "user": "#fb7185",
            "ok": "#22c55e",
            "warn": "#f59e0b",
            "err": "#f43f5e",
            "grad_start": "#f43f5e",
            "grad_end": "#9f1239",
        },
        {
            "name": "claude",
            "accent": "#d97757",
            "user": "#e89b7e",
            "ok": "#6b9e78",
            "warn": "#d99b52",
            "err": "#c94a4a",
            "grad_start": "#e08263",
            "grad_end": "#b85636",
        },
    ]
    for th in external_themes:
        p = THEMES_DIR / f"{th['name']}.json"
        if not p.exists():
            p.write_text(json.dumps(th, indent=2))

    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    plugin_skill = SKILLS_DIR / "plugin-authoring.md"
    if not plugin_skill.exists():
        plugin_skill.write_text(
            "# Skill: Thule Plugin Authoring\n"
            "Plugins live in `~/.local_ai_cli/Plugins/<slug>.py` (global) or `./.thule/Plugins/<slug>.py` (project).\n"
            "Every plugin MUST define `PLUGIN_META` and `def register(api):`:\n\n"
            "```python\n"
            "PLUGIN_META = {\n"
            '    "name": "my-plugin",\n'
            '    "version": "0.1.0",\n'
            '    "description": "What this plugin adds",\n'
            "}\n\n"
            "def register(api):\n"
            "    # 1. Register an LLM tool\n"
            "    def my_tool(args: dict) -> str:\n"
            '        return f"Result: {args.get(\'query\', \'\')}"\n\n'
            "    api.register_tool(\n"
            '        name="my_tool",\n'
            '        description="Describe when the model should call this tool.",\n'
            "        parameters={\n"
            '            "type": "object",\n'
            '            "properties": {"query": {"type": "string", "description": "Input text"}},\n'
            '            "required": ["query"],\n'
            "        },\n"
            "        handler=my_tool,\n"
            "    )\n\n"
            "    # 2. Register a /slash command\n"
            "    def my_cmd(arg_str: str) -> str:\n"
            '        return f"Ran /mycmd with: {arg_str}"\n\n'
            '    api.register_command("mycmd", "Custom slash command", my_cmd)\n\n'
            "    # 3. Optional lifecycle hook: UserPromptSubmit | PreToolUse | PostToolUse | Stop\n"
            "    # api.register_hook(\"PostToolUse\", lambda payload: None, matcher=\"write_file\")\n"
            "```\n\n"
            "Rules:\n"
            "- Tool handlers accept `(args: dict)` (or `(args: dict, api)`) and return a `str`.\n"
            "- Prefer Python standard library + `httpx` so plugins load with zero extra dependencies.\n"
            "- Use `create_plugin` (or `write_file` into `~/.local_ai_cli/Plugins/`) and Thule hot-reloads immediately.\n"
        )

    if not any(SKILLS_DIR.glob("*.md")):
        (SKILLS_DIR / "clean-code.md").write_text(
            "# Skill: Clean Code\n"
            "- Write modular, idiomatic, well-structured code.\n"
            "- Prefer clear naming and minimal dependencies.\n"
            "- Verify edge cases and handle errors explicitly.\n"
        )
        (SKILLS_DIR / "shell-ops.md").write_text(
            "# Skill: Safe Shell Operations\n"
            "- Inspect files using `read_file`, `list_files`, and `search_files` before modifying anything.\n"
            "- Prefer surgical edits with `edit_file` over overwriting entire files.\n"
            "- Keep shell commands concise and non-destructive.\n"
        )
        (SKILLS_DIR / "concise-review.md").write_text(
            "# Skill: Concise Code Review\n"
            "- Focus strictly on bugs, security flaws, and logic gaps.\n"
            "- Provide concrete fixes rather than vague suggestions.\n"
        )

    if not any(AGENTS_DIR.glob("*.json")):
        defaults = [
            AgentDef(
                name="default",
                description="Full-stack coding & terminal assistant",
                system_prompt=(
                    "You are Thule, a fast local terminal coding agent. "
                    "Use `list_files`, `search_files`, and `read_file` to inspect code, "
                    "`edit_file` or `write_file` to make clean edits, and `run_shell_command` to build/test."
                ),
                skills=["shell-ops"],
            ),
            AgentDef(
                name="planner",
                description="Read-only architect that drafts implementation plans",
                system_prompt=(
                    "You are a software architect. Inspect the repository using `list_files`, `search_files`, "
                    "and `read_file`, then produce a clear, numbered implementation plan with exact file paths."
                ),
                skills=["shell-ops"],
            ),
            AgentDef(
                name="coder",
                description="Implements features and fixes cleanly in the workspace",
                system_prompt=(
                    "You are an expert software engineer. Read existing files first, then apply clean, "
                    "complete implementations using `edit_file` / `write_file` and verify via `run_shell_command`."
                ),
                skills=["clean-code", "shell-ops"],
            ),
            AgentDef(
                name="reviewer",
                description="Audits code for bugs, edge cases, and polishes output",
                system_prompt=(
                    "You are a senior code reviewer. Inspect the code and diffs for bugs, missing imports, "
                    "or edge cases, apply fixes if needed, and summarize clearly."
                ),
                skills=["concise-review"],
            ),
        ]
        for ag in defaults:
            (AGENTS_DIR / f"{ag.name}.json").write_text(json.dumps(asdict(ag), indent=2))

        wf = WorkflowDef(
            name="build-and-review",
            description="3-step pipeline: Planner -> Coder -> Reviewer",
            steps=[
                WorkflowStep(
                    agent="planner",
                    instruction="Analyze the user's goal and outline a step-by-step technical plan.",
                ),
                WorkflowStep(
                    agent="coder",
                    instruction="Using the plan above, implement the changes in the workspace.",
                ),
                WorkflowStep(
                    agent="reviewer",
                    instruction="Review the changes for bugs or gaps and present the final result.",
                ),
            ],
        )
        (AGENTS_DIR / f"{wf.name}.json").write_text(json.dumps(asdict(wf), indent=2))

    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    for topic, doc_content in EMBEDDED_DOCS.items():
        doc_path = DOCS_DIR / f"{topic}.md"
        if not doc_path.exists():
            repo_doc = Path.cwd() / "docs" / f"{topic}.md"
            if repo_doc.exists():
                doc_path.write_text(repo_doc.read_text())
            else:
                doc_path.write_text(doc_content.strip() + "\n")
    if not any(COMMANDS_DIR.glob("*.md")):
        (COMMANDS_DIR / "audit.md").write_text(
            "Perform a deep security and bug audit of the workspace or target: $ARGUMENTS\n"
            "Read all relevant files first, save a structured review artifact via `create_artifact`, "
            "and list concrete fixes with file paths and line numbers."
        )
    if not HOOKS_FILE.exists():
        HOOKS_FILE.write_text(
            json.dumps(
                {
                    "UserPromptSubmit": [],
                    "PreToolUse": [],
                    "PostToolUse": [],
                    "Stop": [],
                },
                indent=2,
            )
        )



EMBEDDED_DOCS: Dict[str, str] = {
    "overview": """# Thule Architecture Overview
Thule is a terminal-based local AI engineering assistant supporting multi-agent orchestration,
hot-reloading Python plugins, deterministic lifecycle hooks, git checkpoints, and TUI split-pane themes.
Key storage:
- Global: `~/.local_ai_cli/` (Agents, Themes, Skills, Plugins, Docs, commands, hooks.json)
- Project: `./.thule/` (Plugins, Skills, commands, hooks.json, docs) and `./Agents/`, `./Themes/`, `./docs/`.""",

    "agents": """# Thule Agent Specification
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
Execution modes: 'build' (read/write/shell allowed) or 'plan' (read-only, create_artifact).""",

    "themes": """# Thule Theme Specification
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
    * compact_logo: bool""",

    "workflows": """# Thule Workflow Specification
Workflows are sequential multi-agent execution pipelines in `~/.local_ai_cli/Agents/<name>.json` or `./Agents/<name>.json`.
Schema:
- "name": string (slug)
- "description": string (1-line summary)
- "steps": array of objects, each containing:
    * "agent": string (name of an existing agent, e.g. 'planner', 'coder', 'reviewer')
    * "instruction": string (concrete task instruction for that step)
- "type": "workflow"
Context and diffs are handed off sequentially between agents across all pipeline steps.""",

    "plugins": """# Thule Plugin Developer Guide & PluginAPI SDK
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
Plugins hot-reload automatically upon saving without restarting the CLI.""",

    "hooks": """# Thule Lifecycle Hooks Reference
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
- Regex matcher: optional field filtering by tool_name or prompt.""",

    "commands": """# Thule Custom Slash Commands
Defined as Markdown files in `~/.local_ai_cli/commands/<name>.md`, `./.thule/commands/<name>.md`, or `./.opencode/commands/<name>.md`.
The filename becomes the command trigger (e.g. audit.md -> /audit).
Inside the Markdown file, use `$ARGUMENTS` to capture user arguments passed after the command.""",

        "skills": """# Thule Skills & Claude SKILL.md Compatibility
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
- Global: toggle in AGENTS tab or config.json `"active_skills"`.""",
    "configuration": """# Thule Configuration & Settings Reference
Files:
- `~/.local_ai_cli/config.json`: settings for provider, model, temperature, context, execution mode ('build'/'plan'), theme, etc.
- `~/.local_ai_cli/profiles.json`: saved provider endpoints (Ollama, LM Studio, vLLM, OpenAI, Anthropic, Gemini, etc.).
Keyboard shortcuts:
- Tab / Shift+Tab: switch top-level tabs.
- Left / Right: toggle focus between Navigator and Inspector panes.
- Ctrl+C: interrupt generation.
- Ctrl+E: edit prompt in $EDITOR.
- Ctrl+R: quick switch agent/workflow.
- Ctrl+T: quick switch theme.
- Ctrl+M: quick switch model from endpoint.
- PageUp / PageDown / Mouse wheel: scroll chat viewport.
Commands: /help, /docs, /create, /mode, /diff, /git, /commit, /map, /artifacts, /rewind, /tasks, /hooks, /plugins, /theme, /sessions, /new, /undo, /redo, /compact, /auto, /init, /export, /editor, /clear, /exit."""
}


def load_documentation(topic: str = "all") -> str:
    """Load Thule documentation from workspace docs/, ~/.local_ai_cli/Docs/, or embedded fallback."""
    topic_clean = topic.strip().lower().rstrip("s")
    if topic_clean in ("all", "doc", "docs", "help", "overview", ""):
        header = (
            "# Thule Local AI CLI — Official Documentation & Architecture Reference\n\n"
            "Thule is a high-performance local terminal AI assistant with multi-agent orchestration, "
            "hot-reloading Python plugins, lifecycle hooks, visual TUI themes, and git checkpoints.\n\n"
            "## Available Documentation Topics\n"
            "- **agents**: Agent definition schema, skills injection, prompt engineering, execution modes.\n"
            "- **themes**: Hex color themes and layout geometry configuration.\n"
            "- **workflows**: Multi-agent sequential pipeline execution and handoffs.\n"
            "- **plugins**: PluginAPI SDK, custom LLM tools, slash commands, hooks, and tabs.\n"
            "- **hooks**: Deterministic lifecycle interception, regex filtering, blocking with exit code 2.\n"
            "- **commands**: Custom markdown slash commands and $ARGUMENTS substitution.\n"
            "- **configuration**: Config files, endpoint profiles, keyboard shortcuts, and commands.\n\n"
        )
        sections = []
        for key in ("agents", "themes", "workflows", "plugins", "hooks", "commands", "configuration"):
            sections.append(load_documentation(key))
        return header + "\n\n---\n\n".join(sections)

    alias_map = {
        "agent": "agents",
        "theme": "themes",
        "workflow": "workflows",
        "plugin": "plugins",
        "hook": "hooks",
        "command": "commands",
        "cmd": "commands",
        "skill": "skills",
        "config": "configuration",
        "configure": "configuration",
        "setting": "configuration",
        "settings": "configuration",
    }
    canon = alias_map.get(topic_clean, topic_clean)

    search_paths = [
        Path.cwd() / "docs" / f"{canon}.md",
        Path.cwd() / ".thule" / "docs" / f"{canon}.md",
        Path.cwd() / "docs" / f"{topic_clean}.md",
        DOCS_DIR / f"{canon}.md",
        DOCS_DIR / f"{topic_clean}.md",
    ]
    for p in search_paths:
        if p.exists():
            try:
                content = p.read_text().strip()
                if content:
                    return content
            except Exception:
                pass

    return EMBEDDED_DOCS.get(canon) or EMBEDDED_DOCS.get(topic_clean) or f"No documentation found for '{topic}'. Available topics: agents, themes, workflows, plugins, hooks, commands, configuration, all."


def load_themes() -> Dict[str, Dict[str, Any]]:
    themes: Dict[str, Dict[str, Any]] = {
        "default": {**DEFAULT_THEME, "layout": DEFAULT_LAYOUT.copy()}
    }
    for folder in (THEMES_DIR, Path.cwd() / "Themes"):
        if folder.exists():
            for p in sorted(folder.glob("*.json")):
                try:
                    data = json.loads(p.read_text())
                    tname = data.get("name", p.stem)
                    merged: Dict[str, Any] = {**DEFAULT_THEME, "layout": sanitize_layout(data.get("layout"))}
                    for k, v in data.items():
                        if k != "layout":
                            merged[k] = str(v)
                    merged["name"] = tname
                    themes[tname] = merged
                except Exception:
                    pass
    return themes


@dataclass
class PluginToolSpec:
    plugin: str
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Any


@dataclass
class PluginCommandSpec:
    plugin: str
    name: str
    description: str
    handler: Any


@dataclass
class PluginHookSpec:
    plugin: str
    event: str
    matcher: str
    handler: Any


@dataclass
class PluginTabSpec:
    plugin: str
    name: str
    title: str
    render_fn: Any
    on_key: Any = None


class PluginAPI:
    def __init__(self, plugin_name: str, registry: "PluginRegistry") -> None:
        self.plugin_name = plugin_name
        self._registry = registry

    @property
    def cwd(self) -> Path:
        return Path.cwd()

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Any,
    ) -> None:
        clean_name = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_")
        if clean_name:
            self._registry.tools[clean_name] = PluginToolSpec(
                plugin=self.plugin_name,
                name=clean_name,
                description=description,
                parameters=parameters or {"type": "object", "properties": {}},
                handler=handler,
            )

    def register_command(self, name: str, description: str, handler: Any) -> None:
        cmd_name = "/" + name.lstrip("/").strip().lower()
        if len(cmd_name) > 1:
            self._registry.commands[cmd_name] = PluginCommandSpec(
                plugin=self.plugin_name,
                name=cmd_name,
                description=description,
                handler=handler,
            )

    def register_hook(self, event: str, handler: Any, matcher: str = "") -> None:
        if event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
            self._registry.hooks.append(
                PluginHookSpec(plugin=self.plugin_name, event=event, matcher=matcher, handler=handler)
            )

    def register_tab(
        self,
        name: str,
        title: str,
        render_fn: Any,
        on_key: Any = None,
    ) -> None:
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower()
        if slug and slug not in TABS:
            self._registry.tabs[slug] = PluginTabSpec(
                plugin=self.plugin_name,
                name=slug,
                title=title or slug.upper(),
                render_fn=render_fn,
                on_key=on_key,
            )


class PluginRegistry:
    def __init__(self) -> None:
        self.plugins: Dict[str, Dict[str, Any]] = {}
        self.tools: Dict[str, PluginToolSpec] = {}
        self.commands: Dict[str, PluginCommandSpec] = {}
        self.hooks: List[PluginHookSpec] = []
        self.tabs: Dict[str, PluginTabSpec] = {}
        self.errors: Dict[str, str] = {}

    def reload(self, disabled: Optional[List[str]] = None) -> None:
        disabled_set = set(disabled or [])
        self.plugins.clear()
        self.tools.clear()
        self.commands.clear()
        self.hooks.clear()
        self.tabs.clear()
        self.errors.clear()

        for folder in (PLUGINS_DIR, Path.cwd() / ".thule" / "Plugins"):
            if not folder.exists():
                continue
            for fp in sorted(folder.glob("*.py")):
                if fp.name.startswith("_"):
                    continue
                slug = fp.stem
                meta = {"name": slug, "version": "0.1.0", "description": "", "path": str(fp), "enabled": slug not in disabled_set}
                self.plugins[slug] = meta
                if slug in disabled_set:
                    continue
                try:
                    spec = importlib.util.spec_from_file_location(f"thule_plugin_{slug}_{abs(hash(str(fp)))}", fp)
                    if spec is None or spec.loader is None:
                        continue
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    raw_meta = getattr(mod, "PLUGIN_META", None)
                    if isinstance(raw_meta, dict):
                        meta.update({k: v for k, v in raw_meta.items() if k in ("name", "version", "description")})
                    reg_fn = getattr(mod, "register", None)
                    if callable(reg_fn):
                        reg_fn(PluginAPI(slug, self))
                except Exception as exc:
                    self.errors[slug] = str(exc)

    def tool_definitions(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for t in self.tools.values():
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": f"[plugin:{t.plugin}] {t.description}",
                        "parameters": t.parameters,
                    },
                }
            )
        return out


PLUGIN_REGISTRY = PluginRegistry()


def load_custom_commands() -> Dict[str, str]:
    cmds: Dict[str, str] = {}
    for folder in (COMMANDS_DIR, Path.cwd() / ".thule" / "commands", Path.cwd() / ".opencode" / "commands"):
        if folder.exists():
            for fp in sorted(folder.glob("*.md")):
                cmds[fp.stem.lower()] = fp.read_text().strip()
    return cmds


def load_hooks() -> Dict[str, List[Dict[str, Any]]]:
    merged: Dict[str, List[Dict[str, Any]]] = {
        "UserPromptSubmit": [],
        "PreToolUse": [],
        "PostToolUse": [],
        "Stop": [],
    }
    for hf in (HOOKS_FILE, Path.cwd() / ".thule" / "hooks.json"):
        if hf.exists():
            try:
                data = json.loads(hf.read_text())
                for k in merged:
                    if isinstance(data.get(k), list):
                        merged[k].extend(data[k])
            except Exception:
                pass
    return merged


def run_lifecycle_hooks(event: str, payload: Dict[str, Any]) -> tuple[bool, str]:
    hooks = load_hooks().get(event, [])
    feedback: List[str] = []
    for h in hooks:
        cmd = h.get("command", "").strip()
        matcher = h.get("matcher", "")
        if not cmd:
            continue
        if matcher and not re.search(matcher, str(payload.get("tool_name", "") or payload.get("prompt", ""))):
            continue
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                input=json.dumps({"event": event, "cwd": str(Path.cwd()), **payload}),
                text=True,
                capture_output=True,
                timeout=15,
            )
            if proc.returncode == 2:
                reason = (proc.stderr or proc.stdout or f"Blocked by {event} hook").strip()
                return False, reason
            if proc.stdout.strip():
                feedback.append(proc.stdout.strip())
        except Exception as exc:
            feedback.append(f"[hook {event} error: {exc}]")
    return True, "\n".join(feedback)


def parse_frontmatter(text: str) -> tuple[Dict[str, str], str]:
    meta: Dict[str, str] = {}
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, flags=re.DOTALL)
    if m:
        fm_text = m.group(1)
        body = m.group(2)
        try:
            import yaml
            parsed = yaml.safe_load(fm_text)
            if isinstance(parsed, dict):
                meta = {str(k).lower(): str(v) for k, v in parsed.items()}
        except Exception:
            for line in fm_text.splitlines():
                if ":" in line and not line.strip().startswith("#"):
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip().strip("\"'")
    return meta, body.strip()


def load_skills() -> Dict[str, str]:
    """Load skills supporting both Claude SKILL.md folder structure and traditional flat .md files."""
    skills: Dict[str, str] = {}
    search_dirs: List[Path] = [
        Path.cwd() / ".claude" / "skills",
        Path.cwd() / "skills",
        Path.cwd() / ".thule" / "Skills",
        Path.cwd() / ".thule" / "skills",
        SKILLS_DIR,
        Path.home() / ".claude" / "skills",
        Path.home() / ".local_ai_cli" / "skills",
    ]
    seen_dirs = set()
    valid_folders = []
    for d in search_dirs:
        try:
            res = d.resolve()
            if res not in seen_dirs and d.exists() and d.is_dir():
                seen_dirs.add(res)
                valid_folders.append(d)
        except Exception:
            pass

    for folder in valid_folders:
        try:
            # 1. Claude-style skill directories: <folder>/<slug>/SKILL.md
            for sub in sorted(folder.iterdir()):
                if sub.is_dir() and not sub.name.startswith("."):
                    skill_file = None
                    for candidate in ("SKILL.md", "skill.md", "Skill.md"):
                        cf = sub / candidate
                        if cf.is_file():
                            skill_file = cf
                            break
                    if skill_file:
                        try:
                            raw_text = skill_file.read_text(errors="ignore").strip()
                            meta, body = parse_frontmatter(raw_text)
                            slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", meta.get("name") or sub.name).strip("-").lower()
                            if slug and slug not in skills:
                                extra_resources: List[str] = []
                                for res_dir in ("scripts", "references", "examples", "resources"):
                                    rf = sub / res_dir
                                    if rf.is_dir():
                                        sub_files = [p.name for p in sorted(rf.iterdir()) if not p.name.startswith(".")]
                                        if sub_files:
                                            extra_resources.append(f"- **{res_dir}/**: `{rf}` ({', '.join(sub_files[:8])})")
                                full_content = body
                                if extra_resources:
                                    full_content += "\n\n### Skill Resources\n" + "\n".join(extra_resources)
                                skills[slug] = full_content
                        except Exception:
                            pass

            # 2. Flat markdown skills: <folder>/<name>.md
            for p in sorted(folder.glob("*.md")):
                if p.is_file() and p.name.upper() not in ("SKILL.MD", "README.MD"):
                    try:
                        raw_text = p.read_text(errors="ignore").strip()
                        meta, body = parse_frontmatter(raw_text)
                        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", meta.get("name") or p.stem).strip("-").lower()
                        if slug and slug not in skills:
                            skills[slug] = body
                    except Exception:
                        pass
        except Exception:
            pass

    return skills


def load_agents_and_workflows() -> tuple[Dict[str, AgentDef], Dict[str, WorkflowDef]]:
    agents: Dict[str, AgentDef] = {}
    workflows: Dict[str, WorkflowDef] = {}
    for folder in (AGENTS_DIR, Path.cwd() / "Agents"):
        if not folder.exists():
            continue
        for p in sorted(folder.glob("*.json")):
            try:
                data = json.loads(p.read_text())
                if data.get("type") == "workflow" or "steps" in data:
                    steps = [WorkflowStep(**s) for s in data.get("steps", [])]
                    wf_name = data.get("name", p.stem)
                    workflows[wf_name] = WorkflowDef(
                        name=wf_name,
                        description=data.get("description", ""),
                        steps=steps,
                    )
                else:
                    valid = {k: v for k, v in data.items() if k in AgentDef.__dataclass_fields__}
                    valid.setdefault("name", p.stem)
                    agents[valid["name"]] = AgentDef(**valid)
            except Exception:
                pass
    return agents, workflows


def append_history(entry: Dict[str, Any]) -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    if HISTORY_FILE.exists():
        try:
            items = json.loads(HISTORY_FILE.read_text())
        except Exception:
            items = []
    items.append(entry)
    _atomic_write_text(HISTORY_FILE, json.dumps(items[-300:], indent=2))


def get_history() -> List[Dict[str, Any]]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            return []
    return []


def command_prefix(cmd: str) -> str:
    try:
        parts = shlex.split(cmd.strip())
        return parts[0] if parts else cmd.strip()
    except ValueError:
        return cmd.strip().split(" ")[0]


def format_unified_diff(old_text: str, new_text: str, filename: str) -> str:
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            lineterm="",
            n=2,
        )
    )
    if not diff_lines:
        return "[dim](no changes)[/]"
    styled: List[str] = []
    for line in diff_lines[:60]:
        esc = line.replace("[", "\\[")
        if line.startswith("+++") or line.startswith("---"):
            styled.append(f"[bold]{esc}[/]")
        elif line.startswith("@@"):
            styled.append(f"[cyan]{esc}[/]")
        elif line.startswith("+"):
            styled.append(f"[green]{esc}[/]")
        elif line.startswith("-"):
            styled.append(f"[red]{esc}[/]")
        else:
            styled.append(f"[dim]{esc}[/]")
    if len(diff_lines) > 60:
        styled.append(f"[dim]... ({len(diff_lines) - 60} more diff lines)[/]")
    return "\n".join(styled)


TOOL_DEFINITIONS = [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the public web for up-to-date information, documentation, or facts. Requires user approval before running.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The search query string."
                            },
                            "max_results": {
                                "type": "integer",
                                "description": "Maximum number of search results to return (1-10, default 5)."
                            }
                        },
                        "required": ["query"]
                    }
                }
            },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the current workspace directory (ignores .git, node_modules, __pycache__).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory path (default '.')"},
                    "max_depth": {"type": "integer", "description": "Max folder depth (default 3)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for a regex or substring pattern across files in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Text or regex to search for"},
                    "path": {"type": "string", "description": "Directory or file path (default '.')"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "start_line": {"type": "integer", "description": "1-based start line (optional)"},
                    "end_line": {"type": "integer", "description": "1-based end line (optional)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact target string in an existing file and show a unified diff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to edit"},
                    "old_text": {"type": "string", "description": "Exact existing snippet to replace"},
                    "new_text": {"type": "string", "description": "Replacement snippet"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file or overwrite an existing file with full content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to write"},
                    "content": {"type": "string", "description": "Full file contents"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell_command",
            "description": "Run a shell command on the user's machine (builds, tests, git, scripts).",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_artifact",
            "description": "Save a structured Markdown artifact (implementation plan, architecture review, diff walkthrough) for user inspection.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short artifact title (e.g. implementation-plan)"},
                    "kind": {"type": "string", "description": "One of: plan, review, architecture, walkthrough"},
                    "content": {"type": "string", "description": "Rich Markdown content of the artifact"},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_tasks",
            "description": "Create or update structured task items for tracking multi-step work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
                            },
                            "required": ["title", "status"],
                        },
                    }
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_subagent",
            "description": "Delegate a focused read-only investigation or review task to a specialized subagent in an isolated context window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Subagent name (e.g. planner, reviewer)"},
                    "task": {"type": "string", "description": "Self-contained prompt/question for the subagent"},
                },
                "required": ["agent_name", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_documentation",
            "description": "Read official Thule documentation guides and specifications. Use when creating or generating agents, workflows, themes, plugins, hooks, or configuring Thule.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Documentation topic: 'agents', 'themes', 'workflows', 'plugins', 'hooks', 'commands', 'configuration', or 'all'",
                    }
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_skill",
            "description": "List available skills or load the full instructions of a specific skill from Skills/ whenever specialized domain rules or workflows are needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill slug name to load (e.g. 'clean-code', 'shell-ops'), or empty/'list' to list all available skills.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_skill",
            "description": "Create a Claude-compatible SKILL.md folder (skills/<name>/SKILL.md) with YAML frontmatter and activate it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill slug name (e.g. rust-idioms)"},
                    "description": {"type": "string", "description": "1-line description of the skill"},
                    "content": {"type": "string", "description": "Markdown instructions with YAML frontmatter"}
                },
                "required": ["name", "description", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_agent",
            "description": "Create and activate a new specialized AI agent JSON definition in ~/.local_ai_cli/Agents/. Consult get_documentation('agents') for schema and guidelines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Slug name (e.g. rust-auditor)"},
                    "description": {"type": "string", "description": "Short 1-line summary"},
                    "system_prompt": {"type": "string", "description": "Detailed system instructions"},
                    "model": {"type": "string", "description": "Optional model override (empty to inherit)"},
                    "skills": {"type": "array", "items": {"type": "string"}, "description": "Optional skill names"}
                },
                "required": ["name", "description", "system_prompt"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_workflow",
            "description": "Create and activate a new multi-step sequential agent workflow JSON in ~/.local_ai_cli/Agents/. Consult get_documentation('workflows') for pipeline schema.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Workflow slug name (e.g. audit-and-fix)"},
                    "description": {"type": "string", "description": "Short workflow description"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent": {"type": "string", "description": "Agent name (e.g. planner, coder, reviewer)"},
                                "instruction": {"type": "string", "description": "Step instruction for this agent"}
                            },
                            "required": ["agent", "instruction"]
                        }
                    }
                },
                "required": ["name", "description", "steps"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_theme",
            "description": "Create and apply a new hex color & gradient TUI theme JSON in ~/.local_ai_cli/Themes/. Accepts hex colors and optional layout settings. Consult get_documentation('themes') for layout options.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Theme slug name (e.g. nord-frost)"},
                    "accent": {"type": "string", "description": "Hex color e.g. #38bdf8"},
                    "user": {"type": "string", "description": "Hex color e.g. #7dd3fc"},
                    "ok": {"type": "string", "description": "Hex color e.g. #22c55e"},
                    "warn": {"type": "string", "description": "Hex color e.g. #eab308"},
                    "err": {"type": "string", "description": "Hex color e.g. #ef4444"},
                    "grad_start": {"type": "string", "description": "Gradient start hex color"},
                    "grad_end": {"type": "string", "description": "Gradient end hex color"},
                    "layout": {
                        "type": "object",
                        "description": "Optional layout settings (header_style, header_align, split_mode, split_ratio, panel_box, prompt_prefix)",
                    }
                },
                "required": ["name", "accent", "grad_start", "grad_end"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_plugin",
            "description": "Create and hot-load a Python Thule plugin in ~/.local_ai_cli/Plugins/ that registers custom tools, /slash commands, lifecycle hooks, or TUI tabs. Consult get_documentation('plugins') for the PluginAPI SDK.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Plugin slug name (e.g. json-formatter, git-stats)"},
                    "description": {"type": "string", "description": "Short 1-line summary of what the plugin adds"},
                    "code": {"type": "string", "description": "Complete valid Python module code defining PLUGIN_META and def register(api):"}
                },
                "required": ["name", "description", "code"]
            }
        }
    },
]


def get_all_tabs() -> List[str]:
    return list(TABS) + [t for t in PLUGIN_REGISTRY.tabs.keys() if t not in TABS]


def get_all_tool_definitions(allow_docs_skills: bool = True) -> List[Dict[str, Any]]:
    base_tools = (
        TOOL_DEFINITIONS
        if allow_docs_skills
        else [t for t in TOOL_DEFINITIONS if t["function"]["name"] not in ("get_documentation", "get_skill")]
    )
    return base_tools + PLUGIN_REGISTRY.tool_definitions()



def get_system_hardware_info() -> Dict[str, Any]:
    """Collects CPU, RAM, Swap, GPU, and local LLM process telemetry."""
    info: Dict[str, Any] = {
        "cpu_count_logical": 1,
        "cpu_count_physical": 1,
        "cpu_percent": 0.0,
        "load_avg": (0.0, 0.0, 0.0),
        "ram_total_gb": 1.0,
        "ram_used_gb": 0.0,
        "ram_percent": 0.0,
        "swap_total_gb": 0.0,
        "swap_used_gb": 0.0,
        "swap_percent": 0.0,
        "gpus": [],
        "llm_processes": [],
    }
    try:
        import psutil
        info["cpu_count_logical"] = psutil.cpu_count(logical=True) or 1
        info["cpu_count_physical"] = psutil.cpu_count(logical=False) or info["cpu_count_logical"]
        info["cpu_percent"] = psutil.cpu_percent(interval=None)
        try:
            info["load_avg"] = os.getloadavg()
        except Exception:
            pass
        vm = psutil.virtual_memory()
        info["ram_total_gb"] = vm.total / (1024**3)
        info["ram_used_gb"] = vm.used / (1024**3)
        info["ram_percent"] = vm.percent
        sw = psutil.swap_memory()
        info["swap_total_gb"] = sw.total / (1024**3)
        info["swap_used_gb"] = sw.used / (1024**3)
        info["swap_percent"] = sw.percent

        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                pname = (proc.info["name"] or "").lower()
                if any(k in pname for k in ["ollama", "llama", "vllm", "lmstudio", "text-gen"]):
                    mem_mb = proc.info["memory_info"].rss / (1024**2) if proc.info.get("memory_info") else 0
                    info["llm_processes"].append({"pid": proc.info["pid"], "name": proc.info["name"], "mem_mb": mem_mb})
            except Exception:
                pass
    except Exception:
        pass

    # GPU Detection: NVIDIA via nvidia-smi
    gpu_list = []
    try:
        p = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=1.0
        )
        if p.returncode == 0:
            for line in p.stdout.strip().splitlines():
                parts = [x.strip() for x in line.split(",")]
                if len(parts) >= 5:
                    gpu_list.append({
                        "type": "nvidia",
                        "name": parts[0],
                        "gpu_pct": float(parts[1]),
                        "vram_used_mb": float(parts[2]),
                        "vram_total_mb": float(parts[3]),
                        "temp_c": float(parts[4]),
                    })
    except Exception:
        pass

    # GPU Detection: AMD via sysfs
    if not gpu_list:
        for card_dev in sorted(Path("/sys/class/drm").glob("card[0-9]*/device")):
            try:
                busy_file = card_dev / "gpu_busy_percent"
                vram_used_file = card_dev / "mem_info_vram_used"
                vram_total_file = card_dev / "mem_info_vram_total"
                if vram_total_file.exists():
                    v_tot = int(vram_total_file.read_text().strip()) / (1024**2)
                    v_used = int(vram_used_file.read_text().strip()) / (1024**2) if vram_used_file.exists() else 0
                    g_pct = int(busy_file.read_text().strip()) if busy_file.exists() else 0
                    card_name = "AMD Radeon GPU"
                    try:
                        pci = subprocess.run(["lspci"], capture_output=True, text=True, timeout=1.0)
                        for ln in pci.stdout.splitlines():
                            if any(k in ln.lower() for k in ["vga", "3d", "display"]) and "amd" in ln.lower():
                                card_name = ln.split(":")[-1].strip()
                                break
                    except Exception:
                        pass
                    gpu_list.append({
                        "type": "amd",
                        "name": card_name,
                        "gpu_pct": g_pct,
                        "vram_used_mb": v_used,
                        "vram_total_mb": v_tot,
                    })
            except Exception:
                pass

    info["gpus"] = gpu_list
    return info

class LocalAICLI:
    def __init__(self) -> None:
        seed_defaults()
        self.console = Console()
        self.config = Config.load()
        PLUGIN_REGISTRY.reload(self.config.disabled_plugins)
        self.active_tab = "prompt"
        self.chat_items: List[Any] = []
        self.thoughts: List[str] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.messages: List[Dict[str, Any]] = []
        self.prompt_history: List[str] = []
        self.scroll_offset = 0
        self.session_id = datetime.now().strftime("session-%Y%m%d-%H%M%S")
        self._active_spinners: List[Dict[str, Any]] = []
        self._spinner_task: Optional[asyncio.Task] = None
        self._spinner_seq = 0
        self._spinner_rendered_lines = 0
        self.tasks: List[Dict[str, str]] = []
        self.checkpoints: List[Dict[str, Any]] = []
        self.redo_stack: List[Dict[str, Any]] = []
        self._input_anchor_row = 0
        self._last_input_visual_rows = 1
        self._pause_esc_watcher = False

    @property
    def colors(self) -> Any:
        themes = load_themes()
        d = themes.get(self.config.theme, DEFAULT_THEME)
        class _CallableDict(dict):
            def __call__(self_d):
                return self_d
        return _CallableDict(d)

    @colors.setter
    def colors(self, value: Any) -> None:
        self._colors_cache = value

    @property
    def layout(self) -> Dict[str, Any]:
        return sanitize_layout(self.colors.get("layout"))

    def _box_style(self) -> Any:
        return BOX_STYLES.get(self.layout.get("panel_box", "rounded"), ROUNDED)

    def _align_renderable(self, renderable: Any, align_mode: Optional[str] = None) -> Any:
        mode = (align_mode or self.layout.get("header_align", "center")).lower()
        if mode == "left":
            return Align.left(renderable)
        if mode == "right":
            return Align.right(renderable)
        return Align.center(renderable)

    def _header_row_count(self) -> int:
        ly = self.layout
        if ly.get("header_style") == "antigravity":
            return 8 if ly.get("header_rule", "line") != "none" else 7
        rows = 1
        if ly.get("meta_position", "below") != "hidden":
            rows += 1
        if ly.get("header_rule", "line") != "none":
            rows += 1
        return rows

    def _begin_sync_frame(self) -> None:
        self._frame_buf: List[str] = ["\033[?2026h\033[?25l\033[H"]
        self._orig_stdout = sys.stdout

        class _FrameStream:
            def __init__(self, chunks: List[str]) -> None:
                self._chunks = chunks
            def write(self, s: str) -> int:
                if s:
                    self._chunks.append(s.replace("\r\n", "\n").replace("\n", "\033[K\r\n"))
                return len(s)
            def flush(self) -> None:
                pass
            def isatty(self) -> bool:
                return True

        sys.stdout = _FrameStream(self._frame_buf)  # type: ignore[assignment]
        self.console.file = sys.stdout

    def _end_sync_frame(self, restore_cursor: bool = False) -> None:
        if hasattr(self, "_orig_stdout") and self._orig_stdout is not None:
            sys.stdout = self._orig_stdout
            self.console.file = sys.stdout
            self._orig_stdout = None
        if getattr(self, "_frame_buf", None):
            self._frame_buf.append("\033[?25h" if restore_cursor else "\033[?25l")
            self._frame_buf.append("\033[?2026l")
            payload = "".join(self._frame_buf).encode("utf-8", errors="replace")
            self._frame_buf = []
            os.write(sys.stdout.fileno(), payload)

    def clear(self) -> None:
        sys.stdout.write("\033[H\033[J\033[?25l")
        sys.stdout.flush()

    def resolve_endpoint(self, agent: AgentDef) -> tuple[str, str, str, str]:
        provider = agent.provider.strip() or self.config.provider
        base_url = (agent.base_url.strip() or self.config.base_url).rstrip("/")
        api_key = agent.api_key.strip() if agent.api_key.strip() else self.config.api_key
        model = agent.model.strip() or self.config.model
        return provider, base_url, api_key, model

    def save_session(self) -> None:
        if not self.messages:
            return
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        title = "untitled"
        for m in self.messages:
            if m.get("role") == "user":
                title = m.get("content", "").strip().splitlines()[0][:50]
                break
        payload = {
            "id": self.session_id,
            "title": title,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "messages": self.messages,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }
        _atomic_write_text(SESSIONS_DIR / f"{self.session_id}.json", json.dumps(payload, indent=2))

    def _ensure_sane_tty(self) -> None:

        if os.name == "nt":
            return
        try:
            fd = sys.stdin.fileno()
            attrs = termios.tcgetattr(fd)
            attrs[1] |= (termios.OPOST | termios.ONLCR)
            attrs[3] |= termios.ISIG
            termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
        except Exception:
            pass

    def select_menu(self, title: str, options: List[str], header_fn=None, centered: bool = False) -> int:
        self._ensure_sane_tty()
        sys.stdout.write("\033[?1000h\033[?1006h")
        sys.stdout.flush()
        idx = 0
        scroll = 0
        while True:
            self.clear()
            if header_fn:
                header_fn()

            w = self.console.width
            h = max(14, self.console.height)
            # Reserve rows for pinned logo/header (~13-17 rows), title (2), scroll indicators (2), and bottom grey footer (2)
            header_rows = (12 if h < 30 else 16) if header_fn == self.render_startup_banner else (4 if header_fn else 1)
            visible_rows = max(4, h - header_rows - 6)

            if idx < scroll:
                scroll = idx
            elif idx >= scroll + visible_rows:
                scroll = idx - visible_rows + 1

            window = options[scroll : scroll + visible_rows]
            has_above = scroll > 0
            has_below = (scroll + visible_rows) < len(options)

            if centered:
                self.console.print(Align.center(Text(title, style="bold")))
                if has_above:
                    self.console.print(Align.center(Text(f"↑ {scroll} more above", style="dim")))
                else:
                    self.console.print()
                block = Text()
                max_opt_w = max(len(o) + 2 for o in options)
                start_col = max(0, (w - max_opt_w) // 2)
                for off, opt in enumerate(window):
                    real_i = scroll + off
                    line_str = (f"> {opt}" if real_i == idx else f"  {opt}").ljust(max_opt_w)
                    if real_i == idx:
                        block.append_text(universal_gradient_text(line_str, self.colors, start_col, w, bold=True))
                    else:
                        block.append(line_str)
                    if off < len(window) - 1:
                        block.append("\n")
                self.console.print(Align.center(block))
                if has_below:
                    self.console.print(Align.center(Text(f"↓ {len(options) - (scroll + visible_rows)} more below", style="dim")))
                else:
                    self.console.print()
                footer_row = max(h - 1, header_rows + visible_rows + 3)
                sys.stdout.write(f"\033[{footer_row};1H\033[K")
                sys.stdout.flush()
                self.console.print(
                    Align.center(Text("[↑/↓ or Scroll to move · Enter to select · Esc to cancel]", style="dim")),
                    end="",
                )
            else:
                self.console.print(f"{title}")
                if has_above:
                    self.console.print(f"[dim]  ↑ {scroll} more above[/]")
                else:
                    self.console.print()
                for off, opt in enumerate(window):
                    real_i = scroll + off
                    if real_i == idx:
                        self.console.print(universal_gradient_text(f"> {opt}", self.colors, 0, w, bold=True))
                    else:
                        self.console.print(f"  {opt}")
                if has_below:
                    self.console.print(f"[dim]  ↓ {len(options) - (scroll + visible_rows)} more below[/]")
                footer_row = max(h - 1, header_rows + visible_rows + 3)
                sys.stdout.write(f"\033[{footer_row};1H\033[K")
                sys.stdout.flush()
                self.console.print("[dim][↑/↓ or Scroll to move · Enter to select · Esc to cancel][/]", end="")



            key = read_key()
            if key == "RESIZE":
                continue
            if key in ("UP", "WHEEL_UP"):
                idx = (idx - 1) % len(options)
            elif key in ("DOWN", "WHEEL_DOWN"):
                idx = (idx + 1) % len(options)
            elif key == "PGUP":
                idx = max(0, idx - visible_rows)
            elif key == "PGDN":
                idx = min(len(options) - 1, idx + visible_rows)
            elif key == "ENTER":
                return idx
            elif key == "ESC":
                return -1

    # --------------------------------------------------------------------------
    # 1. INTERACTIVE LANDING & ENDPOINT SETUP (LIVE PROBE + PROFILES + TEST)
    # --------------------------------------------------------------------------
    def _probe_endpoints(self) -> Dict[str, Dict[str, Any]]:
        targets = {
            "ollama": "http://localhost:11434/v1",
            "llamacpp": "http://localhost:8080/v1",
            "lmstudio": "http://localhost:1234/v1",
        }
        results: Dict[str, Dict[str, Any]] = {}
        with httpx.Client(timeout=0.45) as client:
            for pid, url in targets.items():
                t0 = time.perf_counter()
                try:
                    r = client.get(f"{url}/models")
                    ms = int((time.perf_counter() - t0) * 1000)
                    if r.status_code == 200:
                        models = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
                        results[pid] = {"online": True, "models": models, "ms": ms}
                        continue
                except Exception:
                    pass
                results[pid] = {"online": False, "models": [], "ms": 0}
        return results

    def _fetch_models_for(self, base_url: str, api_key: str = "") -> tuple[bool, List[str], str]:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        models: List[str] = []
        try:
            with httpx.Client(timeout=4.0) as client:
                r = client.get(f"{base_url.rstrip('/')}/models", headers=headers)
                if r.status_code == 200:
                    for m in r.json().get("data", []):
                        mid = m.get("id")
                        if mid and mid not in models:
                            models.append(mid)
                    return True, models, f"Connected ({len(models)} models)"
                if "11434" in base_url:
                    root = base_url.rstrip("/").replace("/v1", "")
                    r2 = client.get(f"{root}/api/tags")
                    if r2.status_code == 200:
                        for m in r2.json().get("models", []):
                            mname = m.get("name")
                            if mname and mname not in models:
                                models.append(mname)
                        return True, models, f"Connected via Ollama API ({len(models)} models)"
                return False, [], f"HTTP {r.status_code}"
        except Exception as exc:
            return False, [], str(exc)[:60]

    def _prompt_line_field(self, label: str, initial: str, mask: bool = False) -> str:
        buf = initial
        cursor = len(buf)
        reveal = not mask
        while True:
            shown = buf if reveal else ("*" * len(buf))
            hint = "  [dim](Ctrl+R reveal · Enter confirm · Esc keep)[/]" if mask else "  [dim](Enter confirm · Ctrl+U clear · Esc keep)[/]"
            sys.stdout.write("\r\033[K\033[?25h")
            self.console.print(f"[bold {self.colors['accent']}]{label}:[/] {shown}{hint}", end="")
            sys.stdout.flush()
            key = read_key()
            if key == "ENTER":
                sys.stdout.write("\033[?25l")
                print()
                return buf.strip()
            if key == "ESC":
                sys.stdout.write("\033[?25l")
                print()
                return initial
            if key == "CTRL_R" and mask:
                reveal = not reveal
            elif key == "CTRL_U":
                buf = ""
                cursor = 0
            elif key == "BACKSPACE":
                if cursor > 0:
                    buf = buf[: cursor - 1] + buf[cursor:]
                    cursor -= 1
            elif key == "LEFT":
                cursor = max(0, cursor - 1)
            elif key == "RIGHT":
                cursor = min(len(buf), cursor + 1)
            elif key.startswith("PASTE:"):
                clip_txt = key[6:].replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
                buf = buf[:cursor] + clip_txt + buf[cursor:]
                cursor += len(clip_txt)
            elif len(key) == 1 and key.isprintable():
                buf = buf[:cursor] + key + buf[cursor:]
                cursor += 1

    def render_startup_banner(self) -> None:
        w = self.console.width
        compact = self.console.height < 30
        logo_slice = LOGO_LINES[:11] if compact else LOGO_LINES
        self.console.print(Align.center(build_logo_text(self.colors, w, lines=logo_slice)))
        self.console.print(
            Align.center(
                Text(
                    f"workspace: {Path.cwd()}   |   theme: {self.config.theme} (press 't' to cycle)",
                    style="dim",
                )
            )
        )

    def run_setup(self) -> None:
        probes = self._probe_endpoints()
        idx = 0

        while True:
            profiles = load_profiles()
            rows: List[Dict[str, Any]] = [
                {
                    "kind": "resume",
                    "name": f"Quick Launch: {self.config.provider} / {self.config.model}",
                    "endpoint": self.config.base_url,
                    "status": "[bold green]READY[/]" if self.config.base_url else "[dim]UNSET[/]",
                    "detail": "Press Enter to launch immediately with saved config",
                }
            ]
            for pr in PROVIDER_PRESETS:
                pid = pr["id"]
                info = probes.get(pid)
                if info is not None:
                    if info["online"]:
                        cnt = len(info["models"])
                        st = f"[bold {self.colors['ok']}]ONLINE[/] [dim]({info['ms']}ms · {cnt} models)[/]"
                    else:
                        st = "[dim]OFFLINE[/]"
                else:
                    env_name = pr["env_key"]
                    has_env = bool(env_name and os.environ.get(env_name))
                    st = f"[{self.colors['ok']}]ENV KEY SET[/]" if has_env else "[dim]API KEY[/]"
                rows.append(
                    {
                        "kind": "preset",
                        "preset": pr,
                        "name": pr["label"],
                        "endpoint": pr["url"],
                        "status": st,
                        "detail": pr["hint"],
                    }
                )
            for pf in profiles[:4]:
                rows.append(
                    {
                        "kind": "profile",
                        "profile": pf,
                        "name": f"Profile: {pf.get('name', 'saved')}",
                        "endpoint": pf.get("base_url", ""),
                        "status": f"[cyan]{pf.get('model', '')}[/]",
                        "detail": f"provider: {pf.get('provider', '')}",
                    }
                )

            idx = idx % len(rows)
            self.clear()
            self.render_startup_banner()

            table = Table(
                box=ROUNDED,
                border_style=self.colors["accent"],
                header_style="bold",
                expand=False,
                padding=(0, 1),
            )
            table.add_column("", width=2)
            avail_w = max(56, self.console.width - 14)
            c1_w = max(22, int(avail_w * 0.36))
            c2_w = max(24, int(avail_w * 0.38))
            c3_w = max(14, avail_w - c1_w - c2_w)
            table.add_column("Backend / Profile", style="bold", width=c1_w, no_wrap=True, overflow="ellipsis")
            table.add_column("Endpoint", style="dim", width=c2_w, no_wrap=True, overflow="ellipsis")
            table.add_column("Status", width=c3_w, no_wrap=True, overflow="ellipsis")

            h = max(14, self.console.height)
            banner_rows = 12 if h < 30 else 16
            max_table_rows = max(3, h - banner_rows - 8)
            if "setup_scroll" not in locals():
                setup_scroll = 0
            if idx < setup_scroll:
                setup_scroll = idx
            elif idx >= setup_scroll + max_table_rows:
                setup_scroll = idx - max_table_rows + 1
            setup_scroll = max(0, min(setup_scroll, max(0, len(rows) - max_table_rows)))

            visible_slice = rows[setup_scroll : setup_scroll + max_table_rows]
            for _offset, row in enumerate(visible_slice):
                i = setup_scroll + _offset
                pointer = ">" if i == idx else " "
                style = f"bold {self.colors['grad_start']}" if i == idx else ""
                pointer = ">" if i == idx else " "
                style = f"bold {self.colors['grad_start']}" if i == idx else ""
                table.add_row(pointer, row["name"], row["endpoint"], row["status"], style=style)

            self.console.print(Align.center(table))
            page_info = ""
            if len(rows) > max_table_rows:
                up_mark = "▲ " if setup_scroll > 0 else "  "
                dn_mark = " ▼" if (setup_scroll + max_table_rows) < len(rows) else "  "
                page_info = f"{up_mark}rows {setup_scroll + 1}–{setup_scroll + len(visible_slice)} of {len(rows)}{dn_mark}   |   "

            sys.stdout.write(f"\033[{h - 2};1H\033[K")
            sys.stdout.flush()
            self.console.print(Align.center(Text(f"{page_info}{rows[idx]['detail']}", style="dim")), end="")
            sys.stdout.write(f"\033[{h - 1};1H\033[K")
            sys.stdout.flush()
            self.console.print(
                Align.center(
                    Text(
                        "[↑/↓ or Scroll] Select   [Enter] Configure / Launch   [r] Re-probe Local   [t] Theme   [q] Quit",
                        style="dim",
                    )
                ),
                end="",
            )

            key = read_key()
            if key == "RESIZE":
                continue
            if key in ("UP", "WHEEL_UP"):
                idx = (idx - 1) % len(rows)
            elif key in ("DOWN", "WHEEL_DOWN"):
                idx = (idx + 1) % len(rows)
            elif key == "PGUP":
                idx = max(0, idx - max_table_rows)
            elif key == "PGDN":
                idx = min(len(rows) - 1, idx + max_table_rows)
            elif key in ("r", "R"):
                probes = self._probe_endpoints()
            elif key in ("t", "T"):
                tnames = list(load_themes().keys())
                self.config.theme = tnames[(tnames.index(self.config.theme) + 1) % len(tnames)]
                self.config.save()
            elif key in ("q", "Q"):
                raise KeyboardInterrupt
            elif key == "ENTER":
                chosen = rows[idx]
                if chosen["kind"] == "resume":
                    return
                if chosen["kind"] == "profile":
                    pf = chosen["profile"]
                    self.config.provider = pf.get("provider", "ollama")
                    self.config.base_url = pf.get("base_url", self.config.base_url)
                    self.config.model = pf.get("model", self.config.model)
                    self.config.api_key = pf.get("api_key", "")
                    self.config.save()
                    return

                pr = chosen["preset"]
                self.clear()
                self.render_startup_banner()
                panel = Panel(
                    f"[bold]{pr['label']}[/]  [dim]— {pr['hint']}[/]",
                    box=ROUNDED,
                    border_style=self.colors["accent"],
                    expand=False,
                )
                self.console.print(Align.center(panel))
                self.console.print()

                self.config.provider = pr["id"]
                default_url = pr["url"]
                new_url = self._prompt_line_field("Base URL", default_url)
                self.config.base_url = (new_url or default_url).rstrip("/")

                env_var = pr.get("env_key", "")
                env_val = os.environ.get(env_var, "") if env_var else ""
                if pr["id"] in ("openrouter", "gemini", "openai"):
                    init_key = self.config.api_key or env_val
                    self.config.api_key = self._prompt_line_field("API Key", init_key, mask=True)
                else:
                    self.config.api_key = ""

                ok, discovered, status_msg = self._fetch_models_for(self.config.base_url, self.config.api_key)
                if ok:
                    self.console.print(f"[{self.colors['ok']}]✓ {status_msg}[/]")
                else:
                    self.console.print(f"[{self.colors['warn']}]! Probe note: {status_msg} (you can still enter a model manually)[/]")

                if discovered:
                    pick_opts = [f"{m}" for m in discovered[:20]] + ["+ Enter custom model name..."]
                    m_idx = self.select_menu(
                        f"Discovered Models @ {self.config.base_url}:",
                        pick_opts,
                        header_fn=self.render_startup_banner,
                        centered=True,
                    )
                    if 0 <= m_idx < len(discovered[:20]):
                        self.config.model = discovered[m_idx]
                    else:
                        self.config.model = self._prompt_line_field("Model", pr["model"]) or pr["model"]
                else:
                    self.config.model = self._prompt_line_field("Model", pr["model"]) or pr["model"]

                self.config.save()
                save_profile(
                    f"{self.config.provider}:{self.config.model}",
                    self.config.provider,
                    self.config.base_url,
                    self.config.model,
                    self.config.api_key,
                )
                return

    # --------------------------------------------------------------------------
    # 2. CENTERED HEADER & TAB BAR (UNIVERSAL SCREEN-SPACE GRADIENT)
    # --------------------------------------------------------------------------
    def render_header(self) -> None:
        w = self.console.width
        c = self.colors
        ly = self.layout

        agents, _ = load_agents_and_workflows()
        if self.config.active_mode == "workflow":
            mode_label = f"workflow:{self.config.active_name}"
            target_info = f"{self.config.provider}/{self.config.model}"
        else:
            ag = agents.get(self.config.active_name)
            if ag:
                prov, _, _, mod = self.resolve_endpoint(ag)
                target_info = f"{prov}/{mod}"
            else:
                target_info = f"{self.config.provider}/{self.config.model}"
            mode_label = f"agent:{self.config.active_name}"

        skills_str = ",".join(self.config.active_skills) if self.config.active_skills else "none"
        cwd_short = str(Path.cwd()).replace(str(Path.home()), "~")

        if ly.get("header_style") == "antigravity":
            emblem_rows = [
                "          .   *          ",
                "    |                    ",
                "   )|(                   ",
                "O===[====================-",
                "   )|(                   ",
                "    |                    ",
                "          *   .          ",
            ]
            row_colors = [
                c.get("grad_start", "#3b82f6"),
                "#f59e0b",
                "#f97316",
                "#ef4444",
                "#f97316",
                "#f59e0b",
                c.get("grad_end", "#ab04d9"),
            ]
            all_tabs = get_all_tabs()
            tab_strip = Text()
            for idx, t in enumerate(all_tabs):
                if idx > 0:
                    tab_strip.append(" · ", style="dim")
                if t == self.active_tab:
                    tab_strip.append(t.upper(), style=f"bold {c['accent']}")
                else:
                    tab_strip.append(t.lower(), style="dim")

            info_rows: List[Text] = [
                Text.assemble(("Thule CLI", f"bold {c['accent']}"), ("  [", "dim"), (self.config.theme, "dim"), ("]", "dim")),
                Text.assemble((f"{mode_label}", "bold"), (f"  ({self.config.execution_mode.upper()})", "dim")),
                Text.assemble((target_info, "")),
                Text.assemble((cwd_short, "dim")),
                Text.assemble((f"skills: {skills_str}", "dim")),
                tab_strip,
                Text("(←/→ tabs | Tab agent | Ctrl+P commands)", style="dim"),
            ]
            for i in range(7):
                line = Text("  ")
                line.append(emblem_rows[i], style=f"bold {row_colors[i]}")
                line.append("   ")
                line.append_text(info_rows[i])
                self.console.print(line)
            if ly.get("header_rule", "line") != "none":
                self.console.print(Rule(style="dim"))
            return

        sep = str(ly.get("tab_separator", "   "))
        align_mode = str(ly.get("header_align", "center"))
        meta_pos = str(ly.get("meta_position", "below"))
        show_rule = str(ly.get("header_rule", "line")) != "none"

        suffix = "    (←/→ tabs | Tab agent | Ctrl+P commands)"
        all_tabs = get_all_tabs()
        plain_nav = sep.join(t.upper() for t in all_tabs) + suffix
        nav_start_col = (
            0 if align_mode == "left"
            else (max(0, w - len(plain_nav)) if align_mode == "right" else max(0, (w - len(plain_nav)) // 2))
        )

        nav = Text()
        cursor = 0
        for i, t in enumerate(all_tabs):
            if i > 0:
                nav.append(sep, style="dim")
                cursor += len(sep)
            label = t.upper()
            if t == self.active_tab:
                nav.append_text(universal_gradient_text(label, self.colors, nav_start_col + cursor, w, bold=True))
            else:
                nav.append(label, style="dim")
            cursor += len(label)
        nav.append(suffix, style="dim")

        exec_tag = f" | mode:{self.config.execution_mode.upper()}"
        auto_tag = " | AUTO" if self.config.auto_approve else ""
        done_t = sum(1 for t in self.tasks if t.get("status") == "done")
        task_tag = f" | tasks:{done_t}/{len(self.tasks)}" if self.tasks else ""
        cp_tag = f" | cp:{len(self.checkpoints)}" if self.checkpoints else ""
        left_part = f"{target_info} | "
        right_part = f" | skills:{skills_str}{exec_tag}{auto_tag}{task_tag}{cp_tag}"
        plain_meta = left_part + mode_label + right_part
        meta_start_col = (
            0 if align_mode == "left"
            else (max(0, w - len(plain_meta)) if align_mode == "right" else max(0, (w - len(plain_meta)) // 2))
        )

        meta = Text()
        meta.append(left_part, style="dim")
        meta.append_text(universal_gradient_text(mode_label, self.colors, meta_start_col + len(left_part), w, bold=False))
        meta.append(right_part, style="dim")

        if meta_pos == "top":
            self.console.print(self._align_renderable(meta, align_mode))
            self.console.print(self._align_renderable(nav, align_mode))
        elif meta_pos == "hidden":
            self.console.print(self._align_renderable(nav, align_mode))
        else:
            self.console.print(self._align_renderable(nav, align_mode))
            self.console.print(self._align_renderable(meta, align_mode))

        if show_rule:
            self.console.print(Rule(style="dim"))

    def switch_tab(self, direction: int) -> None:
        all_tabs = get_all_tabs()
        if self.active_tab not in all_tabs:
            self.active_tab = "prompt"
            return
        cur = all_tabs.index(self.active_tab)
        self.active_tab = all_tabs[(cur + direction) % len(all_tabs)]

    def quick_select_agent_or_workflow(self) -> None:
        agents, workflows = load_agents_and_workflows()

        entries: List[tuple[str, str, str]] = []
        for name, wf in workflows.items():
            entries.append(("workflow", name, f"[workflow] {name} — {wf.description}"))
        for name, ag in agents.items():
            ov = f" ({ag.provider or self.config.provider}:{ag.model})" if ag.model else ""
            entries.append(("agent", name, f"[agent]    {name}{ov} — {ag.description}"))

        if not entries:
            return

        idx = self.select_menu(
            "Select Active Agent or Workflow:",
            [e[2] for e in entries],
            header_fn=self.render_header,
        )
        if idx < 0:
            return
        mode, name, _ = entries[idx]
        self.config.active_mode = mode
        self.config.active_name = name
        self.config.save()
        self.messages.clear()

    # --------------------------------------------------------------------------
    # 2B. COMMAND PALETTE, GIT QOL & CHECKPOINTS
    # --------------------------------------------------------------------------
    def record_checkpoint(self, rel_path: str, existed: bool, previous_content: str) -> None:
        cp_dir = CHECKPOINTS_DIR / self.session_id
        cp_dir.mkdir(parents=True, exist_ok=True)
        cp_id = len(self.checkpoints) + 1
        snap_file = cp_dir / f"cp_{cp_id:03d}_{Path(rel_path).name}.bak"
        snap_file.write_text(previous_content)
        self.checkpoints.append(
            {
                "id": cp_id,
                "time": datetime.now().strftime("%H:%M:%S"),
                "path": rel_path,
                "existed": existed,
                "snapshot": str(snap_file),
                "msg_len": len(self.messages),
            }
        )

    def rewind_checkpoint(self) -> None:
        if not self.checkpoints:
            self.chat_items.append("[dim]No file checkpoints recorded in this session yet.[/]")
            return
        options = [
            f"#{cp['id']} [{cp['time']}] Revert {cp['path']} ({'restore' if cp['existed'] else 'delete'})"
            for cp in reversed(self.checkpoints)
        ]
        idx = self.select_menu(
            "Rewind File Checkpoint:",
            options,
            header_fn=self.render_header,
        )
        if idx < 0:
            return
        real_idx = len(self.checkpoints) - 1 - idx
        to_revert = self.checkpoints[real_idx:]
        for cp in reversed(to_revert):
            target = (Path.cwd() / cp["path"]).resolve()
            if cp["existed"]:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(Path(cp["snapshot"]).read_text())
            else:
                target.unlink(missing_ok=True)
        self.checkpoints = self.checkpoints[:real_idx]
        self.chat_items.append(
            f"[{self.colors['ok']}]Rewound {len(to_revert)} file checkpoint(s) back to #{to_revert[0]['id']}.[/]"
        )

    def show_git_diff(self) -> None:
        res = subprocess.run(["git", "diff"], capture_output=True, text=True)
        diff_txt = res.stdout.strip() if res.returncode == 0 else ""
        if not diff_txt and self.checkpoints:
            cp_diffs: List[str] = []
            seen_paths: set[str] = set()
            for cp in self.checkpoints:
                rel = cp["path"]
                if rel in seen_paths:
                    continue
                seen_paths.add(rel)
                old_txt = Path(cp["snapshot"]).read_text(errors="replace") if cp.get("existed") and Path(cp["snapshot"]).exists() else ""
                cur_p = (Path.cwd() / rel).resolve()
                new_txt = cur_p.read_text(errors="replace") if cur_p.is_file() else ""
                u = format_unified_diff(old_txt, new_txt, rel)
                if u.strip():
                    cp_diffs.append(u)
            diff_txt = "\n".join(cp_diffs).strip()
        if not diff_txt:
            msg = "[dim]Working tree clean (no unstaged git diff or session file diffs).[/]" if res.returncode == 0 else "[dim]Not a git repository and no session file checkpoints recorded.[/]"
            self.chat_items.append(msg)
            return
        styled: List[str] = ["[bold]git diff[/]"]
        for line in diff_txt.splitlines()[:120]:
            esc = line.replace("[", "\\[")
            if line.startswith("+++") or line.startswith("---"):
                styled.append(f"[bold]{esc}[/]")
            elif line.startswith("@@"):
                styled.append(f"[cyan]{esc}[/]")
            elif line.startswith("+"):
                styled.append(f"[green]{esc}[/]")
            elif line.startswith("-"):
                styled.append(f"[red]{esc}[/]")
            else:
                styled.append(f"[dim]{esc}[/]")
        self.chat_items.append("\n".join(styled))

    def show_git_status(self) -> None:
        st = subprocess.run(["git", "status", "-sb"], capture_output=True, text=True)
        if st.returncode != 0:
            self.chat_items.append(f"[{self.colors['err']}]Not a git repository.[/]")
            return
        log = subprocess.run(["git", "log", "-n", "5", "--oneline"], capture_output=True, text=True)
        self.chat_items.append(
            f"[bold]Git Status[/]\n{st.stdout.strip()}\n\n[bold]Recent Commits[/]\n[dim]{log.stdout.strip()}[/]"
        )

    async def ai_git_commit(self) -> None:
        diff = subprocess.run(["git", "diff", "HEAD"], capture_output=True, text=True).stdout.strip()
        if not diff:
            self.chat_items.append("[dim]Nothing to commit (working tree matches HEAD).[/]")
            return
        agents, _ = load_agents_and_workflows()
        ag = agents.get(self.config.active_name) or AgentDef("default", "", "")
        _, base_url, api_key, model_name = self.resolve_endpoint(ag)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                r = await client.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": model_name,
                        "messages": [
                            {
                                "role": "system",
                                "content": "Write a single-line conventional git commit message (max 72 chars) for this diff. Output ONLY the commit message.",
                            },
                            {"role": "user", "content": diff[:6000]},
                        ],
                        "temperature": 0.1,
                    },
                )
                r.raise_for_status()
                msg = r.json()["choices"][0]["message"]["content"].strip().splitlines()[0].strip("`\"'")
                msg = re.sub(r"<think>.*?</think>", "", msg).strip()
        except Exception as exc:
            self.chat_items.append(f"[{self.colors['err']}]Commit message generation failed: {exc}[/]")
            return

        decision = self.ask_approval(f"git add -A && git commit -m {shlex.quote(msg)}", "git", force_manual=True)
        if decision in ("once", "always", "auto"):
            subprocess.run(["git", "add", "-A"], check=False)
            c = subprocess.run(["git", "commit", "-m", msg], capture_output=True, text=True)
            out = (c.stdout or c.stderr).strip()
            self.chat_items.append(f"[{self.colors['ok']}]Committed:[/] {msg}\n[dim]{out}[/]")
        else:
            self.chat_items.append("[dim]Commit cancelled.[/]")

    def show_repo_map(self) -> None:
        ignore = {".git", "node_modules", "__pycache__", ".venv", "venv", "target", "dist", "build"}
        lines = [f"[bold]Repository Map ({Path.cwd().name})[/]"]
        count = 0
        for root, dirs, files in os.walk(Path.cwd()):
            dirs[:] = [d for d in sorted(dirs) if d not in ignore and not d.startswith(".")]
            rel_dir = Path(root).relative_to(Path.cwd())
            depth = len(rel_dir.parts)
            if depth > 3:
                continue
            indent = "  " * depth
            if depth > 0:
                lines.append(f"{indent}[bold]{rel_dir.name}/[/]")
            for f in sorted(files):
                if f.startswith("."):
                    continue
                fp = Path(root) / f
                defs: List[str] = []
                if fp.suffix in (".py", ".ts", ".js", ".go", ".rs", ".c", ".h", ".lua"):
                    try:
                        txt = fp.read_text(errors="ignore")
                        for m in re.finditer(r"^(?:def|class|func|fn|function)\s+([a-zA-Z0-9_]+)", txt, re.M):
                            defs.append(m.group(1))
                            if len(defs) >= 5:
                                break
                    except Exception:
                        pass
                sym = f" [dim]({', '.join(defs)})[/]" if defs else ""
                lines.append(f"{indent}  {f}{sym}")
                count += 1
                if count >= 90:
                    break
            if count >= 90:
                break
        self.chat_items.append("\n".join(lines))

    def copy_last_response(self, code_only: bool = False) -> None:
        for m in reversed(self.messages):
            if m.get("role") == "assistant" and m.get("content"):
                clean = re.sub(r"<think>.*?</think>", "", str(m["content"]), flags=re.DOTALL).strip()
                label = "last response"
                if code_only:
                    blocks = re.findall(r"```(?:[a-zA-Z0-9_+-]+)?\n(.*?)```", clean, flags=re.DOTALL)
                    if not blocks:
                        self.chat_items.append("[dim]No fenced code block found in last response.[/]")
                        return
                    clean = blocks[-1].strip()
                    label = "last code block"
                ok, info = copy_to_clipboard(clean)
                if ok:
                    self.chat_items.append(f"[{self.colors['ok']}]Copied {label} ({len(clean)} chars) via {info}.[/]")
                else:
                    self.chat_items.append(f"[{self.colors['err']}]Clipboard failed: {info}[/]")
                return
        self.chat_items.append("[dim]No assistant response to copy yet.[/]")

    def browse_artifacts(self) -> None:
        local_dir = Path.cwd() / ".thule" / "Artifacts"
        files: List[Path] = []
        for d in (local_dir, ARTIFACTS_DIR):
            if d.exists():
                files.extend(sorted(d.glob("*.md"), reverse=True))
        if not files:
            self.chat_items.append("[dim]No artifacts generated yet.[/]")
            return
        idx = self.select_menu(
            "Review Structured Artifacts:",
            [f"{f.parent.parent.name}/{f.name}" for f in files[:25]],
            header_fn=self.render_header,
        )
        if idx < 0:
            return
        chosen = files[idx]
        self.chat_items.append(f"[{self.colors['grad_start']}]artifact: {chosen.name}[/]")
        self.chat_items.append(Markdown(chosen.read_text()))

    async def ask_btw_side_question(self, question: str) -> None:
        if not question.strip():
            if self.chat_items:
                self.chat_items.append(Rule(style="dim"))
            self.chat_items.append("[dim]Usage: /btw <quick side question>[/]")
            return
        agents, _ = load_agents_and_workflows()
        ag = agents.get(self.config.active_name) or AgentDef("default", "", "")
        _, base_url, api_key, model_name = self.resolve_endpoint(ag)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        recent = [m for m in self.messages[-6:] if m.get("role") in ("user", "assistant") and m.get("content")]
        msgs = [
            {"role": "system", "content": f"Answer concisely as a side-note. Workspace: {Path.cwd()}"},
            *recent,
            {"role": "user", "content": question},
        ]
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json={"model": model_name, "messages": msgs, "temperature": 0.2},
                )
                r.raise_for_status()
                ans = r.json()["choices"][0]["message"]["content"].strip()
                ans = re.sub(r"<think>.*?</think>|<\|channel>thought.*?<channel\|>", "", ans, flags=re.DOTALL).strip()
                self.chat_items.append(f"[dim][btw] {question}[/]")
                self.chat_items.append(Markdown(ans))
        except Exception as exc:
            self.chat_items.append(f"[{self.colors['err']}]/btw error: {exc}[/]")

    def open_command_palette(self) -> Optional[str]:
        actions = [
            ("/create", "AI-generate a workflow, agent, theme, or plugin"),
            ("/plugins", "List and reload installed Python plugins"),
        ("/connect", "Open endpoint & provider landing setup"),
            ("/models", "Switch / discover available models from endpoint"),
            ("/mode", f"Toggle Plan vs Build mode (current: {self.config.execution_mode.upper()})"),
            ("/diff", "Show unstaged git diff in chat"),
            ("/git", "Show git branch, status, and recent commits"),
            ("/commit", "Generate AI commit message and commit changes"),
            ("/map", "Show compact repository structure & symbol map"),
            ("/copy", "Copy last assistant response to clipboard"),
            ("/artifacts", "Review generated Markdown artifacts (plans/reviews)"),
            ("/rewind", "Rewind file edits to a saved checkpoint"),
            ("/tasks", "View current agent task checklist"),
            ("/hooks", "Inspect lifecycle hooks (hooks.json)"),
            ("/sessions", "Browse and resume saved sessions"),
            ("/new", "Start a new clean session"),
            ("/undo", "Undo the last prompt & assistant response"),
            ("/redo", "Redo previously undone turn"),
            ("/compact", "Summarize older turns to compact context"),
            ("/ref", f"Toggle AI docs & skills lookup (current: {'ON' if self.config.auto_docs_skills else 'OFF'})"),
            ("/theme", f"Switch theme (current: {self.config.theme})"),
            ("/init", "Generate AGENTS.md project context in current directory"),
            ("/export", "Export current conversation to Markdown"),
            ("/editor", "Compose multi-line prompt in $EDITOR"),
            ("/help", "Show all slash commands & keybindings"),
        ]
        idx = self.select_menu(
            "Command Palette (Ctrl+P):",
            [f"{cmd:<10} — {desc}" for cmd, desc in actions],
            header_fn=self.render_header,
        )
        if idx < 0:
            return None
        return actions[idx][0]

    def switch_model_picker(self) -> None:
        _, discovered, _ = self._fetch_models_for(self.config.base_url, self.config.api_key)
        options = [f"{m} {'(current)' if m == self.config.model else ''}".strip() for m in discovered]
        options.append("+ Enter custom model name...")
        idx = self.select_menu(
            f"Select Model ({self.config.provider} @ {self.config.base_url}):",
            options,
            header_fn=self.render_header,
        )
        if idx < 0:
            return
        if idx < len(discovered):
            self.config.model = discovered[idx]
        else:
            self.clear()
            self.render_header()
            val = input("Model name: ").strip()
            if val:
                self.config.model = val
        self.config.save()

    def switch_theme_picker(self) -> None:
        themes = load_themes()
        names = list(themes.keys())
        idx = self.select_menu(
            "Select Theme:",
            [f"{n} {'(current)' if n == self.config.theme else ''}".strip() for n in names],
            header_fn=self.render_header,
        )
        if idx >= 0:
            self.config.theme = names[idx]
            self.config.save()

    def browse_sessions(self) -> None:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(SESSIONS_DIR.glob("*.json"), reverse=True)
        if not files:
            self.chat_items.append("[dim]No saved sessions found yet.[/]")
            return
        entries = []
        for f in files[:25]:
            try:
                data = json.loads(f.read_text())
                entries.append((f, f"{data.get('updated', '')} | {data.get('title', f.stem)}"))
            except Exception:
                pass
        if not entries:
            return
        idx = self.select_menu(
            "Resume Saved Session:",
            [label for _, label in entries],
            header_fn=self.render_header,
        )
        if idx < 0:
            return
        chosen = json.loads(entries[idx][0].read_text())
        self.session_id = chosen.get("id", entries[idx][0].stem)
        self.messages = chosen.get("messages", [])
        self.prompt_tokens = chosen.get("prompt_tokens", 0)
        self.completion_tokens = chosen.get("completion_tokens", 0)
        self.chat_items.clear()
        self.scroll_offset = 0
        for m in self.messages:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role == "user" and content:
                if self.chat_items:
                    self.chat_items.append(Rule(style="dim"))
                self.chat_items.append(f"[{self.colors['grad_start']}]>[/] {content}")
            elif role == "assistant" and content:
                self.chat_items.append(f"[{self.colors['grad_start']}]{self.config.active_name}:[/]")
                self.chat_items.append(Markdown(content))

    def init_agents_md(self) -> None:
        target = Path.cwd() / "AGENTS.md"
        if target.exists():
            self.chat_items.append(f"[{self.colors['warn']}]AGENTS.md already exists in {Path.cwd()}[/]")
            return
        top_files = sorted(p.name for p in Path.cwd().iterdir() if not p.name.startswith("."))[:30]
        content = (
            "# Project Context (AGENTS.md)\n\n"
            f"- Working Directory: `{Path.cwd()}`\n"
            f"- Top-level entries: {', '.join(top_files) if top_files else '(empty)'}\n\n"
            "## Conventions\n"
            "- Inspect relevant files before editing.\n"
            "- Keep changes minimal, idiomatic, and verified.\n"
        )
        target.write_text(content)
        self.chat_items.append(f"[{self.colors['ok']}]Created {target}[/]")

    def export_session_md(self, filename: str = "") -> None:
        if not self.messages:
            self.chat_items.append("[dim]Nothing to export yet.[/]")
            return
        raw_name = filename.strip()
        if raw_name:
            if not raw_name.endswith(".md"):
                raw_name += ".md"
            out_path = (Path.cwd() / raw_name).resolve()
        else:
            out_path = Path.cwd() / f"{self.session_id}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Thule Session ({self.session_id})",
            "",
            f"- **Provider/Model**: `{self.config.provider}/{self.config.model}`",
            f"- **Tokens**: prompt={self.prompt_tokens:,}, completion={self.completion_tokens:,}",
            "",
        ]
        for m in self.messages:
            role = m.get("role")
            if role in ("user", "assistant") and m.get("content"):
                lines.append(f"## {role.capitalize()}\n\n{m['content']}\n")
            elif role == "tool" and m.get("content"):
                lines.append(f"<details><summary>Tool Output</summary>\n\n```\n{str(m['content'])[:600]}\n```\n</details>\n")
        out_path.write_text("\n".join(lines))
        self.chat_items.append(f"[{self.colors['ok']}]Exported session to {out_path}[/]")

    def open_external_editor(self, initial_text: str = "") -> str:
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or ("notepad" if os.name == "nt" else "nano")
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = BASE_DIR / f"draft_prompt_{os.getpid()}.md"
        tmp.write_text(initial_text)
        saved_tty = None
        if os.name != "nt" and sys.stdin.isatty():
            try:
                saved_tty = termios.tcgetattr(sys.stdin.fileno())
                if _INITIAL_TERMIOS is not None:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _INITIAL_TERMIOS)
            except Exception:
                saved_tty = None
        try:
            sys.stdout.write("\033[?25h\033[?1006l\033[?1000l\033[?2004l\033[?1049l")
            sys.stdout.flush()
            subprocess.call([*shlex.split(editor), str(tmp)])
        finally:
            if _TUI_ACTIVE:
                sys.stdout.write("\033[?1049h\033[?2004h\033[?1000h\033[?1006h\033[?25l")
                sys.stdout.flush()
            if saved_tty is not None:
                try:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved_tty)
                except Exception:
                    pass
        if tmp.exists():
            text = tmp.read_text().strip()
            tmp.unlink(missing_ok=True)
            return text
        return ""

    async def compact_session(self) -> None:
        if len(self.messages) <= 4:
            self.chat_items.append("[dim]Session is already compact.[/]")
            return
        sys_msg = self.messages[0] if self.messages[0].get("role") == "system" else None
        convo_text = "\n\n".join(
            f"{m.get('role')}: {str(m.get('content', ''))[:1200]}"
            for m in self.messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        )
        agents, _ = load_agents_and_workflows()
        ag = agents.get(self.config.active_name) or AgentDef("default", "", "")
        _, base_url, api_key, model_name = self.resolve_endpoint(ag)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": model_name,
                        "messages": [
                            {
                                "role": "system",
                                "content": "Summarize this coding session concisely, preserving file paths, decisions, and open tasks.",
                            },
                            {"role": "user", "content": convo_text},
                        ],
                        "temperature": 0.1,
                    },
                )
                resp.raise_for_status()
                summary = resp.json()["choices"][0]["message"]["content"].strip()
                new_msgs = []
                if sys_msg:
                    new_msgs.append(sys_msg)
                new_msgs.append({"role": "user", "content": f"[Compacted Session Summary]\n{summary}"})
                self.messages = new_msgs
                self.chat_items.append(f"[{self.colors['ok']}]Session compacted.[/]\n[dim]{summary}[/]")
                self.save_session()
        except Exception as exc:
            self.chat_items.append(f"[{self.colors['err']}]Compact failed: {exc}[/]")

    async def ai_create_asset(self, spec: str) -> None:
        parts = spec.strip().split(None, 1)
        valid_kinds = ("workflow", "agent", "theme", "plugin", "skill", "workflows", "agents", "themes", "plugins", "skills")
        if len(parts) < 2 or parts[0].lower() not in valid_kinds:
            self.chat_items.append(
                "[dim]Usage: /create <workflow|agent|theme|plugin> <description>[/]\n"
                "[dim]Examples:[/]\n"
                "  /create workflow security audit pipeline with planner, auditor, and patcher\n"
                "  /create agent rust-perf focused on zero-copy lifetimes and criterion benchmarks\n"
                "  /create theme nord-frost icy cyan and deep slate palette\n"
                "  /create plugin todo-scanner tool and /todos command that finds TODO/FIXME comments"
            )
            return

        kind = parts[0].lower().rstrip("s")
        desc = parts[1].strip()
        agents, _ = load_agents_and_workflows()
        ag = agents.get(self.config.active_name) or AgentDef("default", "", "")
        _, base_url, api_key, model_name = self.resolve_endpoint(ag)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        doc_snippet = load_documentation(kind)

        if kind == "theme":
            schema_hint = (
                'Return ONLY valid JSON with keys: "name" (slug), "accent", "user", "ok", "warn", "err", '
                '"grad_start", "grad_end" (all 7-char #rrggbb hex strings), and optional "layout" object '
                '(keys: header_style, header_align, split_mode, split_ratio, panel_box, prompt_prefix).'
            )
        elif kind == "agent":
            avail_sk = list(load_skills().keys())
            schema_hint = (
                'Return ONLY valid JSON with keys: "name" (slug), "description" (1 line), '
                '"system_prompt" (detailed instructions), "model" ("" unless specified), '
                f'"skills" (subset of {json.dumps(avail_sk)}).'
            )
        elif kind == "plugin":
            schema_hint = (
                'Return ONLY valid JSON with keys: "name" (slug), "description" (1 line), and "code" '
                '(complete Python plugin source defining PLUGIN_META = {"name": ..., "version": "0.1.0", "description": ...} '
                'and def register(api): using api.register_tool(name, description, parameters, handler) and/or '
                'api.register_command(name, description, handler) and/or api.register_hook(event, handler, matcher="") and/or '
                'api.register_tab(name, title, render_fn, on_key=None)).'
            )
        elif kind == "skill":
            schema_hint = (
                'Return ONLY valid JSON with keys: "name" (slug), "description" (1 line), '
                'and "content" (Markdown skill instructions starting with YAML frontmatter: '
                '---\nname: <slug>\ndescription: ...\n---\n\n# Skill Title\n...)'
            )
        else:
            avail_ag = list(agents.keys()) or ["planner", "coder", "reviewer"]
            schema_hint = (
                'Return ONLY valid JSON with keys: "name" (slug), "description" (1 line), '
                '"steps" (list of {"agent": one of ' + json.dumps(avail_ag) + ', "instruction": "..."}).'
            )

        self.render_prompt_view("", 0)
        spin_id = self._start_thinking_spinner(label=f"{kind}", verb="Generating")
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                r = await client.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": model_name,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    f"You are the Thule CLI asset architect generating a {kind} configuration.\n\n"
                                    f"--- OFFICIAL THULE {kind.upper()} SPECIFICATION & DOCUMENTATION ---\n"
                                    f"{doc_snippet}\n\n"
                                    f"--- FORMAT REQUIREMENTS ---\n"
                                    f"{schema_hint}\n"
                                    "Return ONLY the raw JSON object. No explanation, no markdown backticks, no code fences."
                                ),
                            },
                            {"role": "user", "content": desc},
                        ],
                        "temperature": 0.2,
                    },
                )
                r.raise_for_status()
                raw = r.json()["choices"][0]["message"]["content"]
                raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                m_obj = re.search(r"\{.*\}", raw, flags=re.DOTALL)
                if not m_obj:
                    raise ValueError("Model did not return a JSON object.")
                data = json.loads(m_obj.group(0))

            slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(data.get("name") or f"custom-{kind}")).strip("-").lower()
            if kind == "theme":
                merged = DEFAULT_THEME.copy()
                for k in ("accent", "user", "ok", "warn", "err", "grad_start", "grad_end"):
                    v = str(data.get(k, "")).strip()
                    if re.match(r"^#[0-9a-fA-F]{6}$", v):
                        merged[k] = v
                if isinstance(data.get("layout"), dict):
                    merged["layout"] = sanitize_layout(data["layout"])
                merged["name"] = slug
                (THEMES_DIR / f"{slug}.json").write_text(json.dumps(merged, indent=2))
                self.config.theme = slug
                self.config.save()
                self.chat_items.append(f"[{self.colors['ok']}]Created & applied theme '{slug}'[/] ({merged['grad_start']} → {merged['grad_end']})")
            elif kind == "agent":
                new_ag = AgentDef(
                    name=slug,
                    description=str(data.get("description", desc)),
                    system_prompt=str(data.get("system_prompt", desc)),
                    model=str(data.get("model", "")),
                    skills=[s for s in (data.get("skills") or []) if s in load_skills()],
                    type="agent",
                )
                (AGENTS_DIR / f"{slug}.json").write_text(json.dumps(asdict(new_ag), indent=2))
                self.config.active_mode = "agent"
                self.config.active_name = slug
                self.config.save()
                self.messages.clear()
                self.chat_items.append(f"[{self.colors['ok']}]Created & activated agent '{slug}'[/] — {new_ag.description}")
            elif kind == "skill":
                slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(data.get("name") or f"custom-{kind}")).strip("-").lower()
                desc = str(data.get("description", ""))
                body = str(data.get("content", ""))
                skill_dir = Path.cwd() / "skills" / slug
                skill_dir.mkdir(parents=True, exist_ok=True)
                if not body.startswith("---"):
                    body = f"---\nname: {slug}\ndescription: {desc}\n---\n\n" + body
                (skill_dir / "SKILL.md").write_text(body.strip() + "\n")
                if slug not in self.config.active_skills:
                    self.config.active_skills.append(slug)
                    self.config.save()
                self.chat_items.append(f"[{self.colors['ok']}]Created & activated Claude-compatible skill '@{slug}'[/] ({skill_dir / 'SKILL.md'})")
            elif kind == "plugin":
                code_str = str(data.get("code", "")).strip()
                if not code_str:
                    raise ValueError("Generated plugin had empty code.")
                ast.parse(code_str)
                PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
                p_file = PLUGINS_DIR / f"{slug}.py"
                p_file.write_text(code_str + "\n")
                if slug in self.config.disabled_plugins:
                    self.config.disabled_plugins.remove(slug)
                    self.config.save()
                PLUGIN_REGISTRY.reload(self.config.disabled_plugins)
                if slug in PLUGIN_REGISTRY.errors:
                    raise ValueError(f"Plugin load error: {PLUGIN_REGISTRY.errors[slug]}")
                t_cnt = sum(1 for t in PLUGIN_REGISTRY.tools.values() if t.plugin == slug)
                c_cnt = sum(1 for c in PLUGIN_REGISTRY.commands.values() if c.plugin == slug)
                self.chat_items.append(
                    f"[{self.colors['ok']}]Created & loaded plugin '{slug}'[/] ({t_cnt} tools, {c_cnt} commands)"
                )
            else:
                steps = [
                    WorkflowStep(agent=str(s.get("agent", "default")), instruction=str(s.get("instruction", "")))
                    for s in (data.get("steps") or [])
                    if isinstance(s, dict)
                ]
                if not steps:
                    raise ValueError("Generated workflow had no steps.")
                new_wf = WorkflowDef(name=slug, description=str(data.get("description", desc)), steps=steps, type="workflow")
                (AGENTS_DIR / f"{slug}.json").write_text(json.dumps(asdict(new_wf), indent=2))
                self.config.active_mode = "workflow"
                self.config.active_name = slug
                self.config.save()
                self.messages.clear()
                step_list = " → ".join(s.agent for s in steps)
                self.chat_items.append(f"[{self.colors['ok']}]Created & activated workflow '{slug}'[/] ({step_list})")
        except Exception as exc:
            self.chat_items.append(f"[{self.colors['err']}]Failed to generate {kind}: {exc}[/]")
        finally:
            await self._stop_thinking_spinner(spin_id)


    def show_help_modal(self) -> None:
        self._ensure_sane_tty()
        sys.stdout.write("\033[?1000h\033[?1006h")
        sys.stdout.flush()

        all_cmds = self._all_slash_commands()
        builtin_set = {c for c, _ in self.SLASH_COMMANDS} | {
            "/mode", "/artifacts", "/rewind", "/tasks", "/hooks", "/btw", "/redo", "/plugins", "/generate", "/docs"
        }
        plugin_set = set(PLUGIN_REGISTRY.commands.keys())

        rows: List[Dict[str, str]] = []
        for cmd, desc in all_cmds:
            if cmd in plugin_set:
                cat = "PLUGIN"
            elif cmd in builtin_set:
                cat = "CORE"
            else:
                cat = "CUSTOM"
            rows.append({"cmd": cmd, "cat": cat, "desc": desc})

        ref_items = [
            ("@<file>", "CONTEXT", "Attach file contents (supports @file.py#10-40 line ranges; Tab completes)"),
            ("@<skill>", "CONTEXT", "Inject a saved skill from ~/.local_ai_cli/Skills/ into the turn"),
            ("!<cmd>", "SHELL", "Run an immediate local shell command and attach output to context"),
            ("Tab", "KEY", "Autocomplete slash command / @file, or switch active agent/workflow"),
            ("Shift+Tab", "KEY", "Toggle between BUILD (read/write) and PLAN (read-only) modes"),
            ("Ctrl+P", "KEY", "Open the interactive Command Palette"),
            ("Ctrl+X <k>", "KEY", "Quick chords: n=new, l=sessions, c=compact, u=undo, r=redo, m=models, t=theme, d=diff, g=git, y=copy"),
            ("Esc", "KEY", "Interrupt active model generation or tool execution"),
            ("← / →", "KEY", "Switch top navigation tabs (PROMPT, AGENTS, ADVANCED, HISTORY, SETTINGS, plugin tabs)"),
            ("PgUp / PgDn", "KEY", "Scroll chat history or paginated lists"),
        ]
        for cmd, cat, desc in ref_items:
            rows.append({"cmd": cmd, "cat": cat, "desc": desc})

        idx = 0
        scroll = 0
        filter_q = ""
        filtering = False

        while True:
            filtered = (
                [
                    r for r in rows
                    if filter_q.lower() in r["cmd"].lower()
                    or filter_q.lower() in r["desc"].lower()
                    or filter_q.lower() in r["cat"].lower()
                ]
                if filter_q
                else rows
            )
            if not filtered:
                filtered = [{"cmd": "(no match)", "cat": "INFO", "desc": f"No commands matching '{filter_q}'"}]

            idx = idx % len(filtered)
            self.clear()
            self.render_header()

            w = max(64, self.console.width)
            h = max(16, self.console.height)
            # Locked header (3 rows) + title (1) + table chrome (4) + footer (2) = 10 fixed rows
            max_rows = max(4, h - 10)

            if idx < scroll:
                scroll = idx
            elif idx >= scroll + max_rows:
                scroll = idx - max_rows + 1
            scroll = max(0, min(scroll, max(0, len(filtered) - max_rows)))

            visible = filtered[scroll : scroll + max_rows]

            sub = f"filter: /{filter_q}_" if filtering else f"{len(all_cmds)} commands · {len(ref_items)} shortcuts"
            self.console.print(
                Align.center(
                    universal_gradient_text(
                        f"COMMAND & KEYBINDING REFERENCE  ({sub})",
                        self.colors,
                        max(0, (w - 52) // 2),
                        w,
                        bold=True,
                    )
                )
            )

            avail_w = max(56, w - 12)
            c1_w = max(14, int(avail_w * 0.22))
            c2_w = 10
            c3_w = max(24, avail_w - c1_w - c2_w)

            table = Table(
                box=ROUNDED,
                border_style=self.colors["accent"],
                header_style="bold",
                expand=False,
                padding=(0, 1),
            )
            table.add_column("", width=2, no_wrap=True)
            table.add_column("Command / Key", style="bold", width=c1_w, no_wrap=True, overflow="ellipsis")
            table.add_column("Type", width=c2_w, no_wrap=True, overflow="ellipsis")
            table.add_column("Description", width=c3_w, no_wrap=True, overflow="ellipsis")

            for off, item in enumerate(visible):
                real_i = scroll + off
                sel = real_i == idx
                ptr = ">" if sel else " "
                row_style = f"bold {self.colors['grad_start']}" if sel else ""
                cat_style = (
                    self.colors["ok"]
                    if item["cat"] == "PLUGIN"
                    else (self.colors["grad_end"] if item["cat"] == "CUSTOM" else "dim")
                )
                table.add_row(
                    ptr,
                    item["cmd"],
                    Text(item["cat"], style=cat_style),
                    item["desc"],
                    style=row_style,
                )

            self.console.print(Align.center(table))

            up_m = "▲ " if scroll > 0 else "  "
            dn_m = " ▼" if (scroll + max_rows) < len(filtered) else "  "
            page_str = f"{up_m}items {scroll + 1}–{scroll + len(visible)} of {len(filtered)}{dn_m}"

            sys.stdout.write(f"\033[{h - 1};1H\033[K")
            sys.stdout.flush()
            self.console.print(
                Align.center(
                    Text(
                        f"{page_str}   |   [↑/↓ or Scroll] Navigate   [PgUp/PgDn] Page   [/] Filter   [Esc / Enter] Close",
                        style="dim",
                    )
                ),
                end="",
            )


            key = read_key()
            if key == "RESIZE":
                continue
            if filtering:
                if key in ("ESC", "ENTER"):
                    filtering = False
                    if key == "ESC":
                        filter_q = ""
                        idx = 0
                        scroll = 0
                elif key == "BACKSPACE":
                    filter_q = filter_q[:-1]
                    idx = 0
                    scroll = 0
                elif len(key) == 1 and key.isprintable():
                    filter_q += key
                    idx = 0
                    scroll = 0
                continue

            if key in ("UP", "WHEEL_UP"):
                idx = (idx - 1) % len(filtered)
            elif key in ("DOWN", "WHEEL_DOWN"):
                idx = (idx + 1) % len(filtered)
            elif key == "PGUP":
                idx = max(0, idx - max_rows)
            elif key == "PGDN":
                idx = min(len(filtered) - 1, idx + max_rows)
            elif key == "/":
                filtering = True
                filter_q = ""
                idx = 0
                scroll = 0
            elif key in ("ESC", "ENTER", "q", "Q"):
                return

    def handle_slash_command(self, text: str) -> bool:
        cmd = text.split()[0].lower()
        if cmd == "/nudge":
            hint = text[len(cmd):].strip()
            nudge_prompt = (
                "Pause for a moment and critically second-guess your previous response and assumptions. "
                "Check for any subtle bugs, flawed logic, edge cases, or better approaches"
                + (f" (specifically: {hint})" if hint else "")
                + ", then continue with your corrected/refined answer."
            )
            if self.chat_items:
                self.chat_items.append(Rule(style="dim"))
            self.chat_items.append(f"[bold {self.colors['warn']}]⟳ /nudge[/] [dim]{hint or 'second-guessing previous step...'}[/]")
            self.render_prompt_view("", 0)
            asyncio.run(self._run_prompt_interruptible(nudge_prompt, is_continuation=True))
            self.save_session()
            return True
        if cmd in ("/exit", "/quit", "/q"):
            raise KeyboardInterrupt
        if cmd in ("/clear", "/new"):
            self.chat_items.clear()
            self.messages.clear()
            self.scroll_offset = 0
            self.session_id = datetime.now().strftime("session-%Y%m%d-%H%M%S")
            return True
        if cmd == "/docs":
            topic = text[len(cmd):].strip() or "all"
            doc_text = load_documentation(topic)
            self.chat_items.append(f"[{self.colors['accent']}]--- Thule Documentation ({topic}) ---[/]\n" + doc_text)
            return True
        if cmd in ("/create", "/generate"):
            asyncio.run(self.ai_create_asset(text[len(cmd):].strip()))
            return True
        if cmd == "/plugins":
            PLUGIN_REGISTRY.reload(self.config.disabled_plugins)
            if not PLUGIN_REGISTRY.plugins:
                if self.chat_items:
                    self.chat_items.append(Rule(style="dim"))
                self.chat_items.append(f"[dim]No plugins installed in {PLUGINS_DIR} or .thule/Plugins. Try /create plugin <desc>[/]")
            else:
                lines = ["[bold]Loaded Thule Plugins[/]"]
                for slug, meta in PLUGIN_REGISTRY.plugins.items():
                    st = "ON" if meta.get("enabled") else "OFF"
                    err = f" [red](ERR: {PLUGIN_REGISTRY.errors[slug]})[/]" if slug in PLUGIN_REGISTRY.errors else ""
                    lines.append(f"  [{st}] [bold]{slug}[/] — {meta.get('description', '')}{err}")
                self.chat_items.append("\n".join(lines))
            return True
        if cmd in PLUGIN_REGISTRY.commands:
            spec = PLUGIN_REGISTRY.commands[cmd]
            arg_str = text[len(cmd):].strip()
            try:
                out = spec.handler(arg_str)
                if out:
                    self.chat_items.append(f"[{self.colors['ok']}]{cmd}[/]\n{out}")
            except Exception as exc:
                self.chat_items.append(f"[{self.colors['err']}]{cmd} error: {exc}[/]")
            return True
        if cmd == "/connect":
            self.run_setup()
            return True
        if cmd == "/diff":
            self.show_git_diff()
            return True
        if cmd == "/git":
            self.show_git_status()
            return True
        if cmd == "/commit":
            asyncio.run(self.ai_git_commit())
            return True
        if cmd == "/map":
            self.show_repo_map()
            return True
        if cmd == "/copy":
            arg = text[len(cmd):].strip().lower()
            self.copy_last_response(code_only=(arg in ("code", "block")))
            return True
        if cmd == "/mode":
            self.config.execution_mode = "plan" if self.config.execution_mode == "build" else "build"
            self.config.save()
            self.chat_items.append(f"[{self.colors['ok']}]Execution mode: {self.config.execution_mode.upper()}[/]")
            return True
        if cmd in ("/rewind", "/checkpoints"):
            self.rewind_checkpoint()
            return True
        if cmd == "/artifacts":
            self.browse_artifacts()
            return True
        if cmd == "/tasks":
            if not self.tasks:
                self.chat_items.append("[dim]No active tasks tracked yet.[/]")
            else:
                lines = ["[bold]Agent Task Checklist[/]"]
                for t in self.tasks:
                    st = t.get("status", "pending")
                    box = "[x]" if st == "done" else ("[~]" if st == "in_progress" else "[ ]")
                    lines.append(f"  {box} {t.get('title', '')}")
                self.chat_items.append("\n".join(lines))
            return True
        if cmd == "/hooks":
            hk = load_hooks()
            summary = ", ".join(f"{k}:{len(v)}" for k, v in hk.items())
            self.chat_items.append(
                f"[bold]Lifecycle Hooks[/] ({summary})\n"
                f"[dim]Global: {HOOKS_FILE} | Project: {Path.cwd() / '.thule' / 'hooks.json'}[/]"
            )
            return True
        if cmd == "/btw":
            q = text[len(cmd):].strip()
            asyncio.run(self.ask_btw_side_question(q))
            return True
        if cmd == "/redo":
            if not self.redo_stack:
                self.chat_items.append("[dim]Nothing to redo.[/]")
            else:
                restored = self.redo_stack.pop()
                self.messages.extend(restored["messages"])
                self.chat_items.extend(restored["chat"])
                for f_st in restored.get("files", []):
                    target = (Path.cwd() / f_st["path"]).resolve()
                    if f_st["after_existed"]:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(f_st["after_content"])
                    else:
                        target.unlink(missing_ok=True)
                    if f_st.get("checkpoint"):
                        self.checkpoints.append(f_st["checkpoint"])
                self.chat_items.append(f"[{self.colors['ok']}]Redid last turn.[/]")
                self.save_session()
            return True
        if cmd == "/help":
            self.show_help_modal()
            return True
        if cmd == "/models":
            self.switch_model_picker()
            return True
        if cmd in ("/sessions", "/resume"):
            self.browse_sessions()
            return True
        if cmd == "/theme":
            self.switch_theme_picker()
            return True
        if cmd == "/auto":
            self.config.auto_approve = not self.config.auto_approve
            self.config.save()
            state = "ON" if self.config.auto_approve else "OFF"
            self.chat_items.append(f"[{self.colors['ok']}]Auto-approve tools: {state}[/]")
            return True
        if cmd in ("/ref", "/docs-skills", "/self-ref"):
            self.config.auto_docs_skills = not self.config.auto_docs_skills
            self.config.save()
            state = "ON" if self.config.auto_docs_skills else "OFF"
            self.chat_items.append(f"[{self.colors['ok']}]AI on-demand docs & skills lookup: {state}[/]")
            return True
        if cmd == "/undo":
            turns = int(text[len(cmd):].strip()) if text[len(cmd):].strip().isdigit() else 1
            turns = max(1, min(20, turns))
            reverted_count = 0
            for _ in range(turns):
                popped_msgs: List[Dict[str, Any]] = []
                while self.messages and self.messages[-1].get("role") != "user":
                    popped_msgs.insert(0, self.messages.pop())
                if self.messages and self.messages[-1].get("role") == "user":
                    popped_msgs.insert(0, self.messages.pop())
                popped_chat = [self.chat_items.pop()] if self.chat_items else []
                reverted_files: List[Dict[str, Any]] = []
                while self.checkpoints and self.checkpoints[-1].get("msg_len", 0) >= len(self.messages):
                    cp = self.checkpoints.pop()
                    target = (Path.cwd() / cp["path"]).resolve()
                    after_existed = target.is_file()
                    after_content = target.read_text(errors="replace") if after_existed else ""
                    if cp["existed"]:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(Path(cp["snapshot"]).read_text(errors="replace"))
                    else:
                        target.unlink(missing_ok=True)
                    reverted_files.append({
                        "path": cp["path"],
                        "after_existed": after_existed,
                        "after_content": after_content,
                        "checkpoint": cp,
                    })
                reverted_count += len(reverted_files)
                if popped_msgs or popped_chat or reverted_files:
                    self.redo_stack.append({"messages": popped_msgs, "chat": popped_chat, "files": reverted_files})
            f_note = f" and reverted {reverted_count} file edit(s)" if reverted_count else ""
            self.chat_items.append(f"[{self.colors['warn']}]Undid {turns} turn(s){f_note} (/redo to restore).[/]")
            self.save_session()
            return True
        if cmd == "/compact":
            asyncio.run(self.compact_session())
            return True
        if cmd == "/init":
            self.init_agents_md()
            return True
        if cmd == "/export":
            self.export_session_md(text[len(cmd):].strip())
            return True
        if cmd == "/editor":
            draft = self.open_external_editor()
            if draft:
                if self.chat_items:
                    self.chat_items.append(Rule(style="dim"))
                self.chat_items.append(f"[{self.colors['grad_start']}]>[/] {draft}")
                self.render_prompt_view("", 0)
                asyncio.run(self._run_prompt_interruptible(draft))
            return True
        custom_cmds = load_custom_commands()
        cname = cmd.lstrip("/")
        if cname in custom_cmds:
            arg_str = text[len(cmd):].strip()
            expanded = custom_cmds[cname].replace("$ARGUMENTS", arg_str)
            if self.chat_items:
                self.chat_items.append(Rule(style="dim"))
            self.chat_items.append(f"[{self.colors['grad_start']}]>[/] /{cname} {arg_str}".strip())
            self.render_prompt_view("", 0)
            asyncio.run(self._run_prompt_interruptible(expanded))
            self.save_session()
            return True
        return False

    # --------------------------------------------------------------------------
    # 3. PROMPT TAB (LIVE SLASH + @FILE AUTOCOMPLETE, GHOST PREDICTION & HISTORY)
    # --------------------------------------------------------------------------
    SLASH_COMMANDS: List[tuple[str, str]] = [
        ("/help", "Show all slash commands & keybindings"),
        ("/docs", "Read Thule documentation (agents, themes, workflows, plugins, etc.)"),
        ("/create", "AI-generate a workflow, agent, theme, or plugin"),
        ("/generate", "Alias for /create (workflow, agent, theme, plugin)"),
        ("/connect", "Open endpoint & provider landing setup"),
        ("/models", "Switch / discover available models from endpoint"),
        ("/diff", "View colored unstaged git diff"),
        ("/git", "View git status & recent commits"),
        ("/commit", "Generate AI commit message & commit changes"),
        ("/map", "Show compact repository file & symbol map"),
        ("/copy", "Copy last assistant response to clipboard"),
        ("/sessions", "Browse and resume saved sessions"),
        ("/new", "Start a new clean session"),
        ("/undo", "Undo the last prompt & assistant response"),
        ("/compact", "Summarize older turns to compact context"),
        ("/auto", "Toggle auto-approve for file edits & commands"),
        ("/ref", "Toggle AI on-demand docs & skills lookup"),
        ("/theme", "Switch visual theme"),
        ("/init", "Generate AGENTS.md project context in current directory"),
        ("/export", "Export current conversation to Markdown"),
        ("/editor", "Compose multi-line prompt in $EDITOR"),
        ("/clear", "Clear current chat view and reset session"),
        ("/exit", "Exit Thule Local AI CLI"),
    ]

    def _all_slash_commands(self) -> List[tuple[str, str]]:
        extra = [
            ("/mode", "Toggle Plan (read-only) vs Build execution mode"),
            ("/artifacts", "Review generated Markdown artifacts"),
            ("/rewind", "Rewind file edits to a saved checkpoint"),
            ("/tasks", "View current agent task checklist"),
            ("/hooks", "Inspect lifecycle hooks"),
            ("/btw", "Ask an ephemeral side question"),
            ("/redo", "Restore last undone turn"),
            ("/nudge", "Halt AI mid-thought, ask it to second-guess itself, and continue"),
        ]
        plugin_cmds = [(k, v.description[:75]) for k, v in PLUGIN_REGISTRY.commands.items()]
        custom = [(f"/{k}", v.splitlines()[0][:55]) for k, v in load_custom_commands().items()] + plugin_cmds + [("/plugins", "List and reload installed Python plugins")]
        seen = set()
        merged: List[tuple[str, str]] = []
        for item in extra + self.SLASH_COMMANDS + custom:
            if item[0] not in seen:
                seen.add(item[0])
                merged.append(item)
        return merged

    def _slash_matches(self, buf: str) -> List[tuple[str, str]]:
        if not buf.startswith("/"):
            return []
        if " " in buf:
            parts = buf.split(" ")
            if len(parts) == 2 and parts[0].lower() in ("/create", "/generate"):
                sub_prefix = parts[1].lower()
                sub_opts = [
                    (f"{parts[0]} workflow", "Generate & activate a multi-step agent workflow"),
                    (f"{parts[0]} agent", "Generate & activate a specialized AI agent"),
                    (f"{parts[0]} theme", "Generate & apply a hex/gradient TUI theme"),
                    (f"{parts[0]} plugin", "Generate & hot-load a Python tool/command plugin"),
                ]
                return [(cmd, desc) for cmd, desc in sub_opts if parts[1].lower() == "" or cmd.split(" ", 1)[1].startswith(sub_prefix)]
            return []
        prefix = buf.lower()
        return [(cmd, desc) for cmd, desc in self._all_slash_commands() if cmd.startswith(prefix)]

    def _at_file_matches(self, buf: str) -> List[str]:
        m = re.search(r"(?:^|\s)@([a-zA-Z0-9_./-]*)$", buf)
        if not m:
            return []
        frag = m.group(1).lower()
        ignore = {".git", "node_modules", "__pycache__", ".venv", "venv", "target", "dist", "build"}
        hits: List[str] = []
        for root, dirs, files in os.walk(Path.cwd()):
            dirs[:] = [d for d in sorted(dirs) if d not in ignore and not d.startswith(".")]
            for f in sorted(files):
                if f.startswith("."):
                    continue
                rel = str(Path(root, f).relative_to(Path.cwd()))
                if not frag or frag in rel.lower():
                    hits.append(rel)
                    if len(hits) >= 8:
                        return hits
        return hits

    def _slash_prediction(self, buf: str) -> str:
        matches = self._slash_matches(buf)
        if matches:
            top_cmd = matches[0][0]
            return top_cmd[len(buf):] if len(top_cmd) > len(buf) else ""
        m = re.search(r"(?:^|\s)@([a-zA-Z0-9_./-]*)$", buf)
        if m:
            frag = m.group(1)
            f_hits = self._at_file_matches(buf)
            if f_hits and f_hits[0].lower().startswith(frag.lower()) and len(f_hits[0]) > len(frag):
                return f_hits[0][len(frag):]
        return ""

    def _input_visual_rows(self, buf: str, cursor: int) -> int:
        w = max(20, self.console.width)
        ghost = self._slash_prediction(buf) if cursor == len(buf) else ""
        rows = sum(max(1, ((2 + len(ln)) // w) + 1) for ln in (buf + ghost).split("\n"))
        return max(1, rows)

    def _redraw_input_line(self, buf: str, cursor: int) -> None:
        w = max(20, self.console.width)
        ghost = self._slash_prediction(buf) if cursor == len(buf) else ""

        if self._input_anchor_row > 0:
            sys.stdout.write(f"\033[{self._input_anchor_row};1H\033[J")
        else:
            sys.stdout.write("\r\033[J")

        for idx, ln in enumerate(buf.split("\n")):
            sys.stdout.write(f"> {ln}" if idx == 0 else f"\n· {ln}")
        if ghost:
            sys.stdout.write(f"\033[2m{ghost}\033[0m")

        matches = self._slash_matches(buf)
        file_matches = [] if matches else self._at_file_matches(buf)
        if matches:
            sys.stdout.write("\n\033[2m  Tab/→ to complete · Enter to run\033[0m")
            for idx, (cmd, desc) in enumerate(matches[:8]):
                if idx == 0:
                    sys.stdout.write(f"\n  \033[1m{cmd:<11}\033[0m \033[2m— {desc}\033[0m")
                else:
                    sys.stdout.write(f"\n  \033[2m{cmd:<11} — {desc}\033[0m")
        elif file_matches:
            sys.stdout.write("\n\033[2m  Tab to attach @file\033[0m")
            for idx, rel in enumerate(file_matches[:6]):
                if idx == 0:
                    sys.stdout.write(f"\n  \033[1m@{rel}\033[0m")
                else:
                    sys.stdout.write(f"\n  \033[2m@{rel}\033[0m")

        self._last_input_visual_rows = self._input_visual_rows(buf, cursor)
        cur_lines = buf[:cursor].split("\n")
        cursor_row_off = sum(max(1, ((2 + len(p)) // w) + 1) for p in cur_lines[:-1])
        last_seg_abs = 2 + len(cur_lines[-1])
        cursor_row_off += last_seg_abs // w
        cursor_col = (last_seg_abs % w) + 1
        if self._input_anchor_row > 0:
            target_row = min(self.console.height, self._input_anchor_row + cursor_row_off)
            sys.stdout.write(f"\033[{target_row};{cursor_col}H\033[?25h")
        else:
            sys.stdout.write(f"\r\033[{cursor_col}G\033[?25h")
        sys.stdout.flush()

    def _format_spinner_lines(self) -> List[Text]:
        if not self._active_spinners:
            return []
        frames = ["⠟", "⠯", "⠷", "⠾", "⠽", "⠻"]
        dots = [".", "..", "..."]
        accent = self.colors.get("accent", DEFAULT_THEME["accent"])
        multi = len(self._active_spinners) > 1
        out: List[Text] = []
        for entry in self._active_spinners:
            tick = entry["tick"]
            frame = frames[tick % len(frames)]
            dot = dots[(tick // 2) % len(dots)]
            verb = entry.get("verb") or "Thinking"
            t = Text()
            t.append(frame, style=f"bold {accent}")
            t.append(f" {verb}{dot}")
            if entry.get("label") and (multi or verb != "Thinking"):
                t.append(f" ({entry['label']})", style="dim")
            out.append(t)
        return out


    def _format_tool_indicator(
        self,
        verb: str,
        subject: str,
        meta: str = "",
        failed: bool = False,
        expandable: bool = False,
    ) -> Text:
        """Render a bulleted tool activity line matching the active theme:
        • Verb subject (meta) (ctrl+o to expand)
        """
        bullet_color = self.colors.get("grad_end", self.colors.get("accent", DEFAULT_THEME["accent"]))
        verb_color = (
            self.colors.get("error", "#ff5555")
            if failed
            else self.colors.get("accent", self.colors.get("grad_start", DEFAULT_THEME["accent"]))
        )
        t = Text()
        t.append("• ", style=f"bold {bullet_color}")
        t.append(verb, style=f"bold {verb_color}")
        if subject:
            t.append(f" {subject}")
        if meta:
            t.append(f" ({meta})", style="dim")
        if expandable:
            t.append(" (ctrl+o to expand)", style="dim")
        return t

    def _append_tool_activity(
        self,
        verb: str,
        target: str = "",
        failed: bool = False,
        detail: str = "",
    ) -> None:
        """Append or coalesce consecutive tool indicators into:
        • Ran 4 commands (1 failed)
        • Edited 1 file (configuration.md)
        • Read 3 files (a.py, b.py, c.py)
        """
        entry = {
            "_tool_activity": True,
            "verb": verb,
            "items": [(target, failed, detail)],
            "expanded": False,
        }
        if (
            self.chat_items
            and isinstance(self.chat_items[-1], dict)
            and self.chat_items[-1].get("_tool_activity")
            and self.chat_items[-1].get("verb") == verb
        ):
            self.chat_items[-1]["items"].append((target, failed, detail))
        else:
            self.chat_items.append(entry)

    def _render_tool_activity_entry(self, entry: dict) -> Any:
        verb = entry.get("verb", "Ran")
        items = entry.get("items", [])
        count = len(items)
        fail_count = sum(1 for _, f, _ in items if f)
        has_detail = any(bool(d) for _, _, d in items)
        targets = [t for t, _, _ in items if t]

        noun_map = {
            "Ran": ("command", "commands"),
            "Read": ("file", "files"),
            "Edited": ("file", "files"),
            "Wrote": ("file", "files"),
            "Deleted": ("file", "files"),
            "Listed": ("directory", "directories"),
            "Searched": ("query", "queries"),
            "Fetched": ("URL", "URLs"),
        }
        singular, plural = noun_map.get(verb, ("item", "items"))
        noun = singular if count == 1 else plural
        subject = f"{count} {noun}"

        meta_parts = []
        if verb == "Ran":
            if fail_count:
                meta_parts.append(f"{fail_count} failed")
            elif count == 1 and targets:
                cmd_preview = targets[0].strip().splitlines()[0]
                if len(cmd_preview) > 48:
                    cmd_preview = cmd_preview[:45] + "..."
                meta_parts.append(cmd_preview)
        else:
            if targets:
                shown = ", ".join(targets[:3])
                if len(targets) > 3:
                    shown += f", +{len(targets) - 3} more"
                meta_parts.append(shown)
            if fail_count:
                meta_parts.append(f"{fail_count} failed")

        expanded = bool(entry.get("expanded"))
        header = self._format_tool_indicator(
            verb=verb,
            subject=subject,
            meta="; ".join(meta_parts),
            failed=(fail_count == count and count > 0),
            expandable=(has_detail and not expanded),
        )
        if not (expanded and has_detail):
            return header
        blocks: List[Any] = [header]
        for tgt, is_fail, det in items:
            if not det:
                continue
            st_col = self.colors.get("err", "#ef4444") if is_fail else "dim"
            blocks.append(Text.from_markup(f"  [{st_col}]↳ {tgt or verb}:[/]"))
            blocks.append(Padding(Text("\n".join(str(det).strip().splitlines()[:24]), style="dim"), (0, 0, 0, 4)))
        return Group(*blocks)


    async def _tool_web_search(self, query: str, max_results: int = 5) -> str:
        """Search the web via DuckDuckGo Lite/HTML using httpx (no API key required)."""
        import html
        import urllib.parse

        q = (query or "").strip()
        if not q:
            return "Error: empty search query."
        max_results = max(1, min(int(max_results or 5), 10))
        url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36",
            "Referer": "https://html.duckduckgo.com/",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.post(url, data={"q": q}, headers=headers)
                resp.raise_for_status()
                body = resp.text
        except Exception as exc:
            return f"Web search failed: {exc}"

        blocks = re.findall(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
            r'(?:<a[^>]*class="result__snippet"[^>]*>(.*?)</a>|<div[^>]*class="result__snippet"[^>]*>(.*?)</div>)',
            body,
            flags=re.DOTALL | re.IGNORECASE,
        )
        results = []
        for raw_href, raw_title, snip_a, snip_b in blocks:
            title = html.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
            snippet = html.unescape(re.sub(r"<[^>]+>", "", snip_a or snip_b or "")).strip()
            href = html.unescape(raw_href)
            if "uddg=" in href:
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                if "uddg" in parsed:
                    href = parsed["uddg"][0]
            if title and href:
                results.append(f"- {title}\n  URL: {href}\n  Snippet: {snippet}")
            if len(results) >= max_results:
                break

        if not results:
            return f"No web results found for: {q}"
        return f"Web search results for '{q}':\n\n" + "\n\n".join(results)

    def _render_chat_viewport(self, start_row: int, available_rows: int) -> int:
        spinner_lines = self._format_spinner_lines()
        if available_rows <= 0 or (not self.chat_items and not spinner_lines):
            return start_row
        width = max(20, self.console.width)
        chat_budget = max(0, available_rows - len(spinner_lines))
        rendered_lines: List[Text] = []
        if chat_budget > 0 and self.chat_items:
            temp_console = Console(width=width, force_terminal=True, color_system=self.console.color_system)
            for item in self.chat_items:
                renderable = (
                    self._render_tool_activity_entry(item)
                    if isinstance(item, dict) and item.get("_tool_activity")
                    else item
                )
                with temp_console.capture() as cap:
                    temp_console.print(renderable)
                for rl in cap.get().splitlines():
                    t = Text.from_ansi(rl)
                    t.truncate(width, overflow="crop")
                    rendered_lines.append(t)

        total_lines = len(rendered_lines)
        max_scroll = max(0, total_lines - chat_budget)
        self.scroll_offset = max(0, min(self.scroll_offset, max_scroll))

        end_line = total_lines - self.scroll_offset
        start_line = max(0, end_line - chat_budget)

        has_top_indicator = start_line > 0 and (end_line - start_line) > 2
        if has_top_indicator:
            start_line += 1
        has_bot_indicator = self.scroll_offset > 0 and (end_line - start_line) > 2
        if has_bot_indicator:
            end_line -= 1

        row = start_row
        if has_top_indicator:
            sys.stdout.write(f"\033[{row};1H\033[K")
            sys.stdout.flush()
            self.console.print(f"[dim]↑ {start_line} earlier lines above (Wheel / PgUp)[/]", end="")
            row += 1

        for line in rendered_lines[start_line:end_line]:
            sys.stdout.write(f"\033[{row};1H\033[K")
            sys.stdout.flush()
            self.console.print(line, end="", overflow="crop", no_wrap=True)
            row += 1

        if has_bot_indicator:
            sys.stdout.write(f"\033[{row};1H\033[K")
            sys.stdout.flush()
            self.console.print(f"[dim]↓ {self.scroll_offset} lines below (Wheel / PgDn)[/]", end="")
            row += 1

        for sp_line in spinner_lines[:available_rows]:
            sp_line.truncate(width, overflow="crop")
            sys.stdout.write(f"\033[{row};1H\033[K")
            sys.stdout.flush()
            self.console.print(sp_line, end="", overflow="crop", no_wrap=True)
            row += 1

        return row

    def render_prompt_view(self, current_input: str = "", cursor: int = 0) -> None:
        if not current_input and getattr(self, "_live_buf", ""):
            current_input = self._live_buf
            cursor = getattr(self, "_live_cursor", len(current_input))
        sys.stdout.write("\033[H")
        sys.stdout.flush()
        self.render_header()

        height = max(12, self.console.height)
        matches = self._slash_matches(current_input)
        f_matches = [] if matches else self._at_file_matches(current_input)
        ac_rows = (1 + min(8, len(matches))) if matches else ((1 + min(6, len(f_matches))) if f_matches else 0)
        in_rows = self._input_visual_rows(current_input, cursor)
        footer_rows = 2 + in_rows + ac_rows
        chat_start_row = self._header_row_count() + 1
        max_chat_rows = max(1, height - (chat_start_row - 1) - footer_rows)

        footer_start_row = self._render_chat_viewport(chat_start_row, max_chat_rows)

        sys.stdout.write(f"\033[{footer_start_row};1H\033[K")
        sys.stdout.flush()
        self.console.print(Rule(style="dim"), end="")

        sys.stdout.write(f"\033[{footer_start_row + 1};1H\033[K")
        sys.stdout.flush()
        cwd_short = Path.cwd().name
        git_info = get_git_summary()
        git_seg = f" ({git_info})" if git_info else ""
        used_tokens = self.prompt_tokens + self.completion_tokens
        ctx_pct = min(100, int((used_tokens / max(1, self.config.num_ctx)) * 100))
        if ctx_pct >= 95:
            ctx_badge = f"[bold {self.colors['err']}]ctx:{ctx_pct}% (!)[/]"
        elif ctx_pct >= 85:
            ctx_badge = f"[bold {self.colors['warn']}]ctx:{ctx_pct}% (!)[/]"
        else:
            ctx_badge = f"[dim]ctx:{ctx_pct}%[/]"
        self.console.print(
            f"[dim]{cwd_short}{git_seg} | [/]{ctx_badge}[dim] | / commands · @ files · Shift+Enter newline · Esc interrupt[/]",
            end="",
        )

        self._input_anchor_row = footer_start_row + 2
        self._redraw_input_line(current_input, cursor)

    def _update_input(self, prev_buf: str, new_buf: str, cursor: int) -> None:
        prev_ac = len(self._slash_matches(prev_buf)) + len(self._at_file_matches(prev_buf))
        new_ac = len(self._slash_matches(new_buf)) + len(self._at_file_matches(new_buf))
        new_in_rows = self._input_visual_rows(new_buf, cursor)
        if prev_ac != new_ac or new_in_rows != self._last_input_visual_rows:
            self.render_prompt_view(new_buf, cursor)
        else:
            self._redraw_input_line(new_buf, cursor)


    def run_prompt_tab(self) -> None:
        buf = ""
        cursor = 0
        hist_idx = len(self.prompt_history)
        saved_draft = ""
        sys.stdout.write("\033[?1000h\033[?1006h")
        sys.stdout.flush()
        self.render_prompt_view(buf, cursor)

        while self.active_tab == "prompt":
            key = read_key()
            if not key:
                continue
            if key == "RESIZE":
                sys.stdout.write("\033[H\033[J")
                sys.stdout.flush()
                self.render_prompt_view(buf, cursor)
                continue
            prev_buf = buf
            if key == "LEFT":
                if buf == "":
                    self.switch_tab(-1)
                    return
                if cursor > 0:
                    cursor -= 1
                    self._redraw_input_line(buf, cursor)
            elif key == "RIGHT":
                if buf == "":
                    self.switch_tab(1)
                    return
                if cursor == len(buf) and self._slash_prediction(buf):
                    buf += self._slash_prediction(buf)
                    cursor = len(buf)
                    self._update_input(prev_buf, buf, cursor)
                elif cursor < len(buf):
                    cursor += 1
                    self._redraw_input_line(buf, cursor)
            elif key == "HOME" or key == "CTRL_A":
                cursor = 0
                self._redraw_input_line(buf, cursor)
            elif key == "END" or key == "CTRL_E":
                cursor = len(buf)
                self._redraw_input_line(buf, cursor)
            elif key == "CTRL_U":
                buf = buf[cursor:]
                cursor = 0
                self._update_input(prev_buf, buf, cursor)
            elif key == "CTRL_K":
                buf = buf[:cursor]
                self._update_input(prev_buf, buf, cursor)
            elif key == "CTRL_W":
                left = buf[:cursor].rstrip()
                cut = left.rfind(" ") + 1
                buf = buf[:cut] + buf[cursor:]
                cursor = cut
                self._update_input(prev_buf, buf, cursor)
            elif key == "WHEEL_UP":
                self.scroll_offset += 3
                self.render_prompt_view(buf, cursor)
            elif key == "WHEEL_DOWN":
                self.scroll_offset = max(0, self.scroll_offset - 3)
                self.render_prompt_view(buf, cursor)
            elif key == "PGUP":
                self.scroll_offset += max(4, self.console.height // 3)
                self.render_prompt_view(buf, cursor)
            elif key == "PGDN":
                self.scroll_offset = max(0, self.scroll_offset - max(4, self.console.height // 3))
                self.render_prompt_view(buf, cursor)
            elif key == "UP":
                if buf == "" and self.scroll_offset > 0:
                    self.scroll_offset += 1
                    self.render_prompt_view(buf, cursor)
                elif self.prompt_history and hist_idx > 0:
                    if hist_idx == len(self.prompt_history):
                        saved_draft = buf
                    hist_idx -= 1
                    buf = self.prompt_history[hist_idx]
                    cursor = len(buf)
                    self._update_input(prev_buf, buf, cursor)
            elif key == "DOWN":
                if buf == "" and self.scroll_offset > 0:
                    self.scroll_offset = max(0, self.scroll_offset - 1)
                    self.render_prompt_view(buf, cursor)
                elif hist_idx < len(self.prompt_history):
                    hist_idx += 1
                    buf = saved_draft if hist_idx == len(self.prompt_history) else self.prompt_history[hist_idx]
                    cursor = len(buf)
                    self._update_input(prev_buf, buf, cursor)
            elif key == "SHIFT_TAB":
                self.config.execution_mode = "plan" if self.config.execution_mode == "build" else "build"
                self.config.save()
                self.chat_items.append(f"[{self.colors['ok']}]Switched to {self.config.execution_mode.upper()} mode[/]")
                self.render_prompt_view(buf, cursor)
            elif key == "CTRL_X":
                chord = read_key()
                chord_map = {
                    "n": "/new",
                    "l": "/sessions",
                    "c": "/compact",
                    "u": "/undo",
                    "r": "/redo",
                    "m": "/models",
                    "t": "/theme",
                    "e": "/editor",
                    "a": "/artifacts",
                    "x": "/export",
                    "d": "/diff",
                    "g": "/git",
                    "y": "/copy",
                }
                if chord in chord_map:
                    self.handle_slash_command(chord_map[chord])
                    self.render_prompt_view(buf, cursor)
            elif key == "TAB":
                pred = self._slash_prediction(buf)
                f_hits = self._at_file_matches(buf)
                if pred:
                    buf += pred
                    cursor = len(buf)
                    self._update_input(prev_buf, buf, cursor)
                elif f_hits:
                    buf = re.sub(r"@([a-zA-Z0-9_./-]*)$", f"@{f_hits[0]} ", buf)
                    cursor = len(buf)
                    self._update_input(prev_buf, buf, cursor)
                else:
                    self.quick_select_agent_or_workflow()
                    self.render_prompt_view(buf, cursor)
            elif key == "CTRL_O":
                for item in reversed(self.chat_items):
                    if isinstance(item, dict) and item.get("_tool_activity") and any(bool(d) for _, _, d in item.get("items", [])):
                        item["expanded"] = not bool(item.get("expanded"))
                        self.render_prompt_view(buf, cursor)
                        break
            elif key == "CTRL_Y":
                self.copy_last_response()
                self.render_prompt_view(buf, cursor)
            elif key == "CTRL_G":
                draft = self.open_external_editor(initial_text=buf)
                if draft:
                    buf = draft
                    cursor = len(buf)
                self.render_prompt_view(buf, cursor)
            elif key in ("SHIFT_ENTER", "ALT_ENTER"):
                buf = buf[:cursor] + "\n" + buf[cursor:]
                cursor += 1
                self._update_input(prev_buf, buf, cursor)
            elif key == "CTRL_P":
                chosen = self.open_command_palette()
                if chosen:
                    self.handle_slash_command(chosen)
                self.render_prompt_view(buf, cursor)
            elif key == "ENTER":
                text = buf.strip()
                matches = self._slash_matches(text)
                if len(matches) == 1:
                    text = matches[0][0]
                buf = ""
                cursor = 0
                self.scroll_offset = 0
                if not text:
                    self.render_prompt_view(buf, cursor)
                    continue

                if not self.prompt_history or self.prompt_history[-1] != text:
                    self.prompt_history.append(text)
                hist_idx = len(self.prompt_history)

                if text.startswith("!") and len(text) > 1:
                    sh_cmd = text[1:].strip()
                    out = asyncio.run(self.execute_shell(sh_cmd))
                    self.chat_items.append(f"[{self.colors['ok']}]! {sh_cmd}[/]\n[dim]{out[:1800]}[/]")
                    self.messages.append({"role": "user", "content": f"Ran shell command `{sh_cmd}`:\n```\n{out[:4000]}\n```"})
                    self.save_session()
                    self.render_prompt_view(buf, cursor)
                    continue

                if text.startswith("/") and self.handle_slash_command(text):
                    self.render_prompt_view(buf, cursor)
                    continue

                if self.chat_items:
                    self.chat_items.append(Rule(style="dim"))
                self.chat_items.append(f"[{self.colors['grad_start']}]>[/] {text}")
                self.render_prompt_view("", 0)
                asyncio.run(self._run_prompt_interruptible(text))
                self.save_session()
                buf = getattr(self, "_live_buf", "")
                cursor = getattr(self, "_live_cursor", len(buf))
                self._live_buf = ""
                self._live_cursor = 0
                self.render_prompt_view(buf, cursor)
            elif key == "BACKSPACE":
                if cursor > 0:
                    buf = buf[: cursor - 1] + buf[cursor:]
                    cursor -= 1
                    self._update_input(prev_buf, buf, cursor)
            elif key == "DEL":
                if cursor < len(buf):
                    buf = buf[:cursor] + buf[cursor + 1 :]
                    self._update_input(prev_buf, buf, cursor)
            elif key.startswith("PASTE:"):
                clip_txt = key[6:].replace("\r\n", "\n").replace("\r", "\n")
                buf = buf[:cursor] + clip_txt + buf[cursor:]
                cursor += len(clip_txt)
            elif len(key) == 1 and key.isprintable():
                buf = buf[:cursor] + key + buf[cursor:]
                cursor += 1
                self._update_input(prev_buf, buf, cursor)

    # --------------------------------------------------------------------------
    # 4. SYSTEM PROMPT & @FILE / @SKILL CONTEXT COMPOSITION
    # --------------------------------------------------------------------------
    def build_system_prompt(self, agent: AgentDef, inline_skills: List[str]) -> str:
        all_skills = load_skills()
        combined_skill_names: List[str] = []
        for s in agent.skills + self.config.active_skills + inline_skills:
            if s in all_skills and s not in combined_skill_names:
                combined_skill_names.append(s)

        parts = [
            agent.system_prompt,
            f"\nWorkspace root: {Path.cwd()}",
            (
                "\n--- WORKSPACE & TOOL RULES ---\n"
                "- You have direct access to workspace and web tools: `list_files`, `search_files`, `read_file`, "
                "`edit_file`, `write_file`, `run_shell_command`, and `web_search` (requires user approval).\n"
                "- NEVER ask the user to paste or provide project files or code. Read files directly with `read_file`.\n"
                "- Do not repeatedly call `list_files` on the same directory once you have the file list; "
                "read the relevant files and deliver a concrete, thorough answer.\n"
                "- You may invoke `run_shell_command` to run tests, linters, builds, or git commands; "
                "every shell command is presented to the user for manual acceptance before execution.\n"
                "- Use `create_artifact` to save structured Markdown plans, code reviews, or architecture docs.\n"
                "- Use `manage_tasks` to track multi-step progress and `delegate_subagent` for isolated sub-investigations.\n"
                "- Use `get_documentation` and `get_skill` whenever you need to check Thule specs, guidelines, or domain skills during a task.\n"
                "- When asked to generate or create an agent, workflow, theme, plugin, or skill, consult Thule documentation (via `get_documentation` or reading `docs/`) and use `create_agent`, `create_workflow`, `create_theme`, `create_plugin`, or `create_skill` (which creates Claude-compatible `SKILL.md` packages). Always adhere strictly to the schema, lifecycle, and best practices.\n"
                "- PLUGIN SDK: Plugins are Python files in `~/.local_ai_cli/Plugins/<name>.py` defining `PLUGIN_META = {\"name\": ..., \"version\": \"0.1.0\", \"description\": ...}` "
                "and `def register(api):` which calls `api.register_tool(name, description, parameters, handler)`, "
                "`api.register_command(name, description, handler)`, `api.register_hook(event, handler, matcher=\"\")`, and `api.register_tab(name, title, render_fn, on_key=None)`."
            ),
        ]
        if self.config.execution_mode == "plan":
            parts.append(
                "\n--- PLAN MODE ACTIVE (READ-ONLY) ---\n"
                "You are currently in PLAN mode. File edits, file writes, and shell commands are disabled. "
                "Inspect the codebase thoroughly with `list_files`, `search_files`, and `read_file`, then save "
                "a detailed implementation plan artifact via `create_artifact`."
            )
        seen_docs: set[Path] = set()
        cur_dir = Path.cwd().resolve()
        home_dir = Path.home().resolve()
        walk_dirs: List[Path] = []
        while True:
            walk_dirs.append(cur_dir)
            if (cur_dir / ".git").exists() or cur_dir == home_dir or cur_dir.parent == cur_dir:
                break
            cur_dir = cur_dir.parent
        for d in reversed(walk_dirs):
            for candidate in (d / "THULE.md", d / ".thule" / "instructions.md", d / "AGENTS.md"):
                if candidate.is_file():
                    rp = candidate.resolve()
                    if rp not in seen_docs:
                        seen_docs.add(rp)
                        try:
                            rel_lbl = candidate.relative_to(Path.cwd().resolve()) if Path.cwd().resolve() in candidate.parents else candidate
                            parts.append(f"\n--- PROJECT INSTRUCTIONS ({rel_lbl}) ---\n{candidate.read_text()[:4000]}")
                        except Exception:
                            pass

        if combined_skill_names:
            parts.append("\n--- ACTIVE SKILLS ---")
            for sname in combined_skill_names:
                parts.append(f"\n[{sname}]\n{all_skills[sname]}")
        if self.config.auto_docs_skills:
            avail_names = sorted(all_skills.keys())
            parts.append(
                "\n--- ON-DEMAND DOCS & SKILLS LOOKUP (ENABLED) ---\n"
                "You can check back at documentation and skills at any point mid-task before acting:\n"
                "- Call `get_documentation(topic=...)` for: agents, themes, workflows, plugins, hooks, commands, skills, configuration, all.\n"
                f"- Call `get_skill(name=...)` to inspect or load any available skill: {', '.join(avail_names) if avail_names else '(none)'}."
            )
        return "\n".join(parts)

    def resolve_mentions(self, text: str) -> tuple[str, List[str], List[Dict[str, Any]]]:
        import base64
        available_skills = load_skills()
        found_skills: List[str] = []
        attached_files: List[str] = []
        attached_images: List[Dict[str, Any]] = []
        img_exts = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

        for raw_tok in re.findall(r"@([a-zA-Z0-9_./#-]+)", text):
            line_range = None
            token = raw_tok
            if "#" in raw_tok:
                token, rng = raw_tok.split("#", 1)
                m_rng = re.match(r"^(\d+)(?:-(\d+))?$", rng)
                if m_rng:
                    s_ln = int(m_rng.group(1))
                    e_ln = int(m_rng.group(2) or s_ln)
                    line_range = (s_ln, e_ln)
            if token in available_skills and token not in found_skills:
                found_skills.append(token)
                continue
            candidate = Path(token).expanduser().resolve() if token.startswith(("/", "~")) else (Path.cwd() / token).resolve()
            if not candidate.is_file():
                for hit in Path.cwd().rglob(f"*{Path(token).name}*"):
                    if hit.is_file() and ".git" not in hit.parts and "node_modules" not in hit.parts:
                        candidate = hit
                        token = str(hit.relative_to(Path.cwd()))
                        break
            if candidate.is_file() and candidate.suffix.lower() in img_exts and candidate.stat().st_size < 10_000_000:
                mime = img_exts[candidate.suffix.lower()]
                b64 = base64.b64encode(candidate.read_bytes()).decode("ascii")
                attached_images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                self.chat_items.append(f"[dim]attached image @{candidate.name}[/]")
                continue
            if candidate.is_file() and candidate.stat().st_size < 180_000:
                try:
                    lines = candidate.read_text(errors="replace").splitlines()
                    if line_range:
                        s_ln = max(1, line_range[0])
                        e_ln = min(len(lines), line_range[1])
                        slice_txt = "\n".join(f"{i:>4} | {lines[i-1]}" for i in range(s_ln, e_ln + 1))
                        attached_files.append(f"--- Attached File: {token} (lines {s_ln}-{e_ln}) ---\n{slice_txt}")
                        self.chat_items.append(f"[dim]attached @{token}#{s_ln}-{e_ln}[/]")
                    else:
                        content = "\n".join(lines)
                        attached_files.append(f"--- Attached File: {token} ---\n{content}")
                        self.chat_items.append(f"[dim]attached @{token} ({len(lines)} lines)[/]")
                except Exception:
                    pass

        if attached_files:
            text = f"{text}\n\n" + "\n\n".join(attached_files)
        return text, found_skills, attached_images

    # --------------------------------------------------------------------------
    # 5. TOOL APPROVAL & EXECUTION (FILES, SEARCH, DIFFS, SHELL)
    # --------------------------------------------------------------------------
    def ask_approval(self, label: str, allow_key: str, force_manual: bool = False) -> str:
        if not force_manual and (self.config.auto_approve or allow_key in self.config.always_allow):
            return "auto"

        options = (
            [
                "1. Accept & Run command (Enter / y)",
                "2. Deny command (Esc / n)",
            ]
            if force_manual
            else [
                "1. Allow once (Enter / y)",
                f'2. Always allow "{allow_key}" in this workspace',
                "3. Deny (Esc / n)",
            ]
        )

        self._pause_esc_watcher = True
        saved_spinners = list(getattr(self, "_active_spinners", []))
        if hasattr(self, "_active_spinners"):
            self._active_spinners.clear()
        self._ensure_sane_tty()
        idx = 0
        try:
            while True:
                self._ensure_sane_tty()
                sys.stdout.write("\033[H\033[J\033[?25l")
                sys.stdout.flush()
                self.render_header()
                w = self.console.width
                kind = "MANUAL COMMAND APPROVAL REQUIRED" if force_manual else "PERMISSION REQUESTED"
                self.console.print(f"\n[bold {self.colors['warn']}]{kind}[/]")
                self.console.print(f"[bold]{label}[/]\n")
                for i, opt in enumerate(options):
                    if i == idx:
                        self.console.print(universal_gradient_text(f"> {opt}", self.colors, 0, w, bold=True))
                    else:
                        self.console.print(f"  {opt}")
                self.console.print("\n[dim][Up/Down to move · Enter or 'y' to accept · Esc or 'n' to deny][/]")

                key = read_key()
                if key == "RESIZE":
                    continue
                if key == "UP":
                    idx = (idx - 1) % len(options)
                elif key == "DOWN":
                    idx = (idx + 1) % len(options)
                elif key in ("y", "Y"):
                    return "once"
                elif key in ("n", "N", "ESC"):
                    return "deny"
                elif key == "ENTER":
                    if force_manual:
                        return "once" if idx == 0 else "deny"
                    if idx == 0:
                        return "once"
                    if idx == 1:
                        if allow_key not in self.config.always_allow:
                            self.config.always_allow.append(allow_key)
                            self.config.save()
                        return "always"
                    return "deny"
        finally:
            if hasattr(self, "_active_spinners"):
                self._active_spinners.extend(saved_spinners)
            self._pause_esc_watcher = False
            sys.stdout.write("\033[H\033[J")
            sys.stdout.flush()
            if getattr(self, "active_tab", "") == "prompt" and hasattr(self, "render_prompt_view"):
                self.render_prompt_view("", 0)


    @staticmethod
    def _truncate_head_tail(text: str, head_bytes: int = 4096, tail_bytes: int = 8192) -> str:
        raw = text.encode("utf-8", errors="replace")
        if len(raw) <= head_bytes + tail_bytes:
            return text
        omitted = len(raw) - (head_bytes + tail_bytes)
        return (
            raw[:head_bytes].decode("utf-8", errors="ignore")
            + f"\n\n[... {omitted:,} bytes truncated ...]\n\n"
            + raw[-tail_bytes:].decode("utf-8", errors="ignore")
        )

    def _resolve_workspace_path(self, rel: str, action: str = "access") -> tuple[Path, bool]:
        raw_p = Path(rel).expanduser()
        fp = raw_p.resolve() if raw_p.is_absolute() else (Path.cwd() / raw_p).resolve()
        ws = Path.cwd().resolve()
        base = BASE_DIR.resolve()
        if fp == ws or ws in fp.parents or fp == base or base in fp.parents:
            return fp, True
        decision = self.ask_approval(
            f"OUTSIDE WORKSPACE ({action} {rel} -> {fp})",
            "outside_workspace",
            force_manual=True,
        )
        return fp, decision in ("once", "always", "auto")
    def _maybe_auto_commit(self, rel_path: str, action: str) -> None:
        if not self.config.auto_commit:
            return
        if not (Path.cwd() / ".git").exists():
            return
        subprocess.run(["git", "add", rel_path], check=False, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"thule: {action} {rel_path}"],
            check=False,
            capture_output=True,
        )

    async def run_tool_call(self, fn_name: str, args: Dict[str, Any], agent_label: str) -> str:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ignore_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "target", "dist", "build"}

        if self.config.execution_mode == "plan" and fn_name in ("edit_file", "write_file", "run_shell_command"):
            return f"Blocked: '{fn_name}' is disabled in PLAN mode. Save a plan with `create_artifact` or ask user to switch to BUILD mode (Shift+Tab)."

        allowed, hook_msg = run_lifecycle_hooks("PreToolUse", {"tool_name": fn_name, "tool_input": args, "agent": agent_label})
        if not allowed:
            self.chat_items.append(f"[{self.colors['err']}]PreToolUse hook blocked {fn_name}: {hook_msg}[/]")
            return f"Blocked by PreToolUse hook: {hook_msg}"

        if fn_name == "list_files":
            rel = args.get("path", ".") or "."
            max_depth = int(args.get("max_depth", 3))
            base, _allowed_ws = self._resolve_workspace_path(rel, "list")
            if not _allowed_ws:
                return f"Blocked: user denied list outside workspace ({rel})."
            if not base.exists():
                return f"Directory not found: {rel}"
            results: List[str] = []
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in sorted(dirs) if d not in ignore_dirs and not d.startswith(".")]
                depth = len(Path(root).relative_to(base).parts)
                if depth >= max_depth:
                    dirs[:] = []
                for f in sorted(files):
                    if f.startswith("."):
                        continue
                    rel_f = str(Path(root, f).relative_to(Path.cwd()))
                    results.append(rel_f)
                    if len(results) >= 200:
                        break
                if len(results) >= 200:
                    break
            self.chat_items.append(f"[dim]list {rel} ({len(results)} files)[/]")
            return "\n".join(results) or "(empty directory)"

        if fn_name == "search_files":
            pattern = args.get("pattern", "")
            rel = args.get("path", ".") or "."
            base, _allowed_ws = self._resolve_workspace_path(rel, "search")
            if not _allowed_ws:
                return f"Blocked: user denied search outside workspace ({rel})."
            if not pattern or not base.exists():
                return "Invalid pattern or path."
            try:
                rx = re.compile(pattern)
            except re.error:
                rx = re.compile(re.escape(pattern))
            matches: List[str] = []
            targets = [base] if base.is_file() else []
            if not targets:
                for root, dirs, files in os.walk(base):
                    dirs[:] = [d for d in sorted(dirs) if d not in ignore_dirs and not d.startswith(".")]
                    for f in sorted(files):
                        targets.append(Path(root) / f)
            for fp in targets:
                try:
                    if fp.stat().st_size > 250_000:
                        continue
                    for lno, line in enumerate(fp.read_text(errors="replace").splitlines(), 1):
                        if rx.search(line):
                            rel_f = str(fp.relative_to(Path.cwd()))
                            matches.append(f"{rel_f}:{lno}: {line.strip()[:160]}")
                            if len(matches) >= 80:
                                break
                except Exception:
                    pass
                if len(matches) >= 80:
                    break
            self.chat_items.append(f"[dim]search '{pattern}' ({len(matches)} hits)[/]")
            return "\n".join(matches) or "No matches found."

        if fn_name == "web_search":
            query = str(args.get("query", "")).strip()
            if not query:
                return "Empty search query."
            max_results = int(args.get("max_results", 5) or 5)
            decision = self.ask_approval(f"web_search: {query}", "web_search", force_manual=True)
            if decision not in ("once", "always", "auto"):
                self._append_tool_activity("Searched", query, failed=True)
                append_history({"time": ts, "agent": agent_label, "status": "DENIED", "cmd": f"web_search {query}", "out": ""})
                return "User denied permission to run web_search."
            tool_result = await self._tool_web_search(query, max_results=max_results)
            failed = tool_result.startswith(("Error:", "Web search failed:"))
            self._append_tool_activity("Searched", query, failed=failed, detail=tool_result)
            status = "AUTO" if decision == "auto" else "APPROVED"
            append_history({"time": ts, "agent": agent_label, "status": status, "cmd": f"web_search {query}", "out": tool_result[:500]})
            return tool_result
        if fn_name == "read_file":
            rel = args.get("path", "")
            fp, _allowed_ws = self._resolve_workspace_path(rel, "edit")
            if not _allowed_ws:
                return f"Blocked: user denied edit outside workspace ({rel})."
            if not fp.is_file():
                return f"File not found: {rel}"
            lines = fp.read_text(errors="replace").splitlines()
            start = max(1, int(args.get("start_line") or 1))
            end = min(len(lines), int(args.get("end_line") or min(len(lines), start + 499)))
            numbered = [f"{i:>4} | {lines[i - 1]}" for i in range(start, end + 1)]
            self.chat_items.append(f"[dim]read {rel} ({start}-{end}/{len(lines)})[/]")
            return "\n".join(numbered) or "(empty file)"

        if fn_name == "edit_file":
            rel = args.get("path", "")
            old_text = args.get("old_text", "")
            new_text = args.get("new_text", "")
            fp, _allowed_ws = self._resolve_workspace_path(rel, "write")
            if not _allowed_ws:
                return f"Blocked: user denied write outside workspace ({rel})."
            if not fp.is_file():
                return f"File not found: {rel}"
            current = fp.read_text(errors="replace")
            if old_text not in current:
                return f"Target snippet not found in {rel}. Read the file first to match exact text."
            updated = current.replace(old_text, new_text, 1)
            diff_str = format_unified_diff(current, updated, rel)

            decision = self.ask_approval(f"edit_file: {rel}", "edit_file")
            if decision == "deny":
                self.chat_items.append(f"[{self.colors['err']}]denied edit: {rel}[/]")
                append_history({"time": ts, "agent": agent_label, "status": "DENIED", "cmd": f"edit {rel}", "out": ""})
                return "User denied file edit."

            self.record_checkpoint(rel, True, current)
            fp.write_text(updated)
            self._maybe_auto_commit(rel, "edit")
            self.chat_items.append(f"[{self.colors['ok']}]edited {rel}[/]\n{diff_str}")
            append_history({"time": ts, "agent": agent_label, "status": "APPROVED", "cmd": f"edit {rel}", "out": ""})
            run_lifecycle_hooks("PostToolUse", {"tool_name": "edit_file", "path": rel})
            return f"Successfully edited {rel}."

        if fn_name == "write_file":
            rel = args.get("path", "")
            content = args.get("content", "")
            fp, _allowed_ws = self._resolve_workspace_path(rel, "write")
            if not _allowed_ws:
                return f"Blocked: user denied write outside workspace ({rel})."
            old = fp.read_text(errors="replace") if fp.is_file() else ""
            diff_str = format_unified_diff(old, content, rel)

            decision = self.ask_approval(f"write_file: {rel}", "write_file")
            if decision == "deny":
                self.chat_items.append(f"[{self.colors['err']}]denied write: {rel}[/]")
                append_history({"time": ts, "agent": agent_label, "status": "DENIED", "cmd": f"write {rel}", "out": ""})
                return "User denied file write."

            self.record_checkpoint(rel, fp.is_file(), old)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content)
            self._maybe_auto_commit(rel, "write")
            self.chat_items.append(f"[{self.colors['ok']}]wrote {rel}[/]\n{diff_str}")
            append_history({"time": ts, "agent": agent_label, "status": "APPROVED", "cmd": f"write {rel}", "out": ""})
            run_lifecycle_hooks("PostToolUse", {"tool_name": "write_file", "path": rel})
            return f"Successfully wrote {rel} ({len(content.splitlines())} lines)."

        if fn_name == "run_shell_command":
            cmd = args.get("command", "").strip()
            if not cmd:
                return "Empty command."
            prefix = command_prefix(cmd)
            decision = self.ask_approval(f"$ {cmd}", prefix, force_manual=True)
            if decision in ("once", "always", "auto"):
                output = await self.execute_shell(cmd)
                status = "AUTO" if decision == "auto" else "APPROVED"
                self.chat_items.append(f"[{self.colors['ok']}]$ {cmd}[/]\n[dim]{output[:1200]}[/]")
                append_history({"time": ts, "agent": agent_label, "status": status, "cmd": cmd, "out": output[:500]})
                return output
            else:
                self.chat_items.append(f"[{self.colors['err']}]denied: {cmd}[/]")
                append_history({"time": ts, "agent": agent_label, "status": "DENIED", "cmd": cmd, "out": ""})
                return "User denied permission to run this command."

        if fn_name == "create_artifact":
            title = re.sub(r"[^a-zA-Z0-9_-]+", "-", args.get("title", "artifact")).strip("-") or "artifact"
            kind = args.get("kind", "plan")
            content = args.get("content", "")
            out_dir = Path.cwd() / ".thule" / "Artifacts"
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            art_path = out_dir / f"{stamp}-{kind}-{title}.md"
            art_path.write_text(content)
            self.chat_items.append(f"[{self.colors['ok']}]artifact saved: .thule/Artifacts/{art_path.name} (/artifacts to view)[/]")
            return f"Saved artifact to {art_path}"

        if fn_name == "manage_tasks":
            raw_tasks = args.get("tasks") or []
            self.tasks = [
                {"title": str(t.get("title", "")), "status": str(t.get("status", "pending"))}
                for t in raw_tasks
            ]
            done_c = sum(1 for t in self.tasks if t["status"] == "done")
            self.chat_items.append(f"[dim]tasks updated ({done_c}/{len(self.tasks)} done)[/]")
            return f"Updated {len(self.tasks)} tasks."

        if fn_name == "delegate_subagent":
            sub_name = args.get("agent_name", "reviewer")
            sub_task = args.get("task", "")
            agents, _ = load_agents_and_workflows()
            sub_ag = agents.get(sub_name) or agents.get("reviewer") or AgentDef(
                name=sub_name, description="", system_prompt="You are a focused read-only subagent."
            )
            self.chat_items.append(f"[dim]spawning subagent @{sub_ag.name}...[/]")
            sub_convo = [
                {"role": "system", "content": self.build_system_prompt(sub_ag, [])},
                {"role": "user", "content": sub_task},
            ]
            sub_res = await self.run_single_agent(sub_ag, sub_convo, label=f"subagent:{sub_ag.name}")
            return f"[Subagent {sub_ag.name} Report]\n{sub_res}"

        if fn_name == "create_skill":
            slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(args.get("name", "custom-skill"))).strip("-").lower() or "custom-skill"
            desc = str(args.get("description", ""))
            body = str(args.get("content", ""))
            skill_dir = Path.cwd() / "skills" / slug
            skill_dir.mkdir(parents=True, exist_ok=True)
            if not body.startswith("---"):
                body = f"---\nname: {slug}\ndescription: {desc}\n---\n\n" + body
            (skill_dir / "SKILL.md").write_text(body.strip() + "\n")
            if slug not in self.config.active_skills:
                self.config.active_skills.append(slug)
                self.config.save()
            self.chat_items.append(f"[{self.colors['ok']}]created & activated skill: @{slug}[/]")
            return f"Created Claude-compatible skill '{slug}' at {skill_dir / 'SKILL.md'} and activated it."

        if fn_name == "create_agent":
            slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(args.get("name", "custom-agent"))).strip("-").lower() or "custom-agent"
            new_ag = AgentDef(
                name=slug,
                description=str(args.get("description", "")),
                system_prompt=str(args.get("system_prompt", "")),
                model=str(args.get("model", "")),
                skills=[s for s in (args.get("skills") or []) if s in load_skills()],
                type="agent",
            )
            (AGENTS_DIR / f"{slug}.json").write_text(json.dumps(asdict(new_ag), indent=2))
            self.config.active_mode = "agent"
            self.config.active_name = slug
            self.config.save()
            self.chat_items.append(f"[{self.colors['ok']}]created & activated agent: {slug}[/]")
            return f"Created and activated agent '{slug}'."

        if fn_name == "create_workflow":
            slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(args.get("name", "custom-workflow"))).strip("-").lower() or "custom-workflow"
            steps = [
                WorkflowStep(agent=str(s.get("agent", "default")), instruction=str(s.get("instruction", "")))
                for s in (args.get("steps") or [])
                if isinstance(s, dict)
            ]
            new_wf = WorkflowDef(name=slug, description=str(args.get("description", "")), steps=steps, type="workflow")
            (AGENTS_DIR / f"{slug}.json").write_text(json.dumps(asdict(new_wf), indent=2))
            self.config.active_mode = "workflow"
            self.config.active_name = slug
            self.config.save()
            self.chat_items.append(f"[{self.colors['ok']}]created & activated workflow: {slug}[/]")
            return f"Created and activated workflow '{slug}' with {len(steps)} steps."

        if fn_name == "get_documentation":
            if not self.config.auto_docs_skills:
                return "Blocked: On-demand docs & skills lookup is toggled OFF (/ref or Settings)."
            topic = str(args.get("topic", "all")).strip()
            self.chat_items.append(f"[dim]checked doc: {topic}[/]")
            return load_documentation(topic)

        if fn_name == "get_skill":
            if not self.config.auto_docs_skills:
                return "Blocked: On-demand docs & skills lookup is toggled OFF (/ref or Settings)."
            skills_map = load_skills()
            req_name = str(args.get("name", "")).strip().lstrip("@").lower()
            if not req_name or req_name in ("list", "all", "*"):
                if not skills_map:
                    return "No skills found in Skills/ directories."
                listing = []
                for s_slug, s_body in sorted(skills_map.items()):
                    first_line = next((ln.lstrip("# ").strip() for ln in s_body.splitlines() if ln.strip()), "")
                    listing.append(f"- {s_slug}: {first_line[:90]}")
                self.chat_items.append(f"[dim]listed skills ({len(skills_map)})[/]")
                return "Available skills (call get_skill(name='<slug>') to read full instructions):\n" + "\n".join(listing)
            if req_name in skills_map:
                self.chat_items.append(f"[dim]checked skill: @{req_name}[/]")
                return f"--- Skill: @{req_name} ---\n{skills_map[req_name]}"
            matches = [k for k in skills_map if req_name in k]
            if len(matches) == 1:
                self.chat_items.append(f"[dim]checked skill: @{matches[0]}[/]")
                return f"--- Skill: @{matches[0]} ---\n{skills_map[matches[0]]}"
            return f"Skill '{req_name}' not found. Available skills: {', '.join(sorted(skills_map.keys())) or 'none'}"

        if fn_name == "create_theme":
            slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(args.get("name", "custom-theme"))).strip("-").lower() or "custom-theme"
            merged = DEFAULT_THEME.copy()
            for k in ("accent", "user", "ok", "warn", "err", "grad_start", "grad_end"):
                v = str(args.get(k, "")).strip()
                if re.match(r"^#[0-9a-fA-F]{6}$", v):
                    merged[k] = v
            if isinstance(args.get("layout"), dict):
                merged["layout"] = sanitize_layout(args["layout"])
            merged["name"] = slug
            (THEMES_DIR / f"{slug}.json").write_text(json.dumps(merged, indent=2))
            self.config.theme = slug
            self.config.save()
            self.chat_items.append(f"[{self.colors['ok']}]created & applied theme: {slug}[/]")
            return f"Created and applied theme '{slug}'."

        if fn_name == "create_plugin":
            slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(args.get("name", "custom-plugin"))).strip("-").lower() or "custom-plugin"
            code_str = str(args.get("code", "")).strip()
            if not code_str:
                return "Plugin code cannot be empty."
            try:
                ast.parse(code_str)
            except SyntaxError as syn_err:
                return f"SyntaxError in plugin code: {syn_err}"
            PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
            p_file = PLUGINS_DIR / f"{slug}.py"
            p_file.write_text(code_str + "\n")
            if slug in self.config.disabled_plugins:
                self.config.disabled_plugins.remove(slug)
                self.config.save()
            PLUGIN_REGISTRY.reload(self.config.disabled_plugins)
            if slug in PLUGIN_REGISTRY.errors:
                return f"Saved {p_file}, but failed to load: {PLUGIN_REGISTRY.errors[slug]}"
            t_names = [t.name for t in PLUGIN_REGISTRY.tools.values() if t.plugin == slug]
            c_names = [c.name for c in PLUGIN_REGISTRY.commands.values() if c.plugin == slug]
            self.chat_items.append(
                f"[{self.colors['ok']}]created & loaded plugin: {slug}[/] [dim](tools: {t_names or 'none'}, cmds: {c_names or 'none'})[/]"
            )
            return f"Created and hot-loaded plugin '{slug}' (tools={t_names}, commands={c_names})."

        if fn_name in PLUGIN_REGISTRY.tools:
            spec = PLUGIN_REGISTRY.tools[fn_name]
            try:
                res = spec.handler(args)
                if asyncio.iscoroutine(res):
                    res = await res
                self.chat_items.append(f"[dim]plugin:{spec.plugin}.{fn_name}[/]")
                return str(res)
            except Exception as exc:
                return f"Plugin tool '{fn_name}' error: {exc}"

        return f"Unknown tool: {fn_name}"

    # --------------------------------------------------------------------------
    # 6. AGENT & WORKFLOW STREAMING EXECUTION ENGINE
    # --------------------------------------------------------------------------
    async def dispatch_prompt(self, user_text: str) -> None:
        ok_prompt, prompt_hook_out = run_lifecycle_hooks("UserPromptSubmit", {"prompt": user_text})
        if not ok_prompt:
            self.chat_items.append(f"[{self.colors['err']}]Blocked by UserPromptSubmit hook: {prompt_hook_out}[/]")
            return
        if prompt_hook_out:
            user_text = f"{user_text}\n\n[Hook Context]\n{prompt_hook_out}"
        clean_text, inline_skills, attached_images = self.resolve_mentions(user_text)
        agents, workflows = load_agents_and_workflows()

        if self.config.active_mode == "workflow":
            wf = workflows.get(self.config.active_name)
            if not wf or not wf.steps:
                self.chat_items.append(
                    f"[{self.colors['err']}]Workflow '{self.config.active_name}' not found or empty.[/]"
                )
                return

            self.chat_items.append(
                f"[{self.colors['grad_start']}]running workflow:[/] [bold]{wf.name}[/] ({len(wf.steps)} steps)"
            )
            accumulated_context = f"Original User Goal:\n{clean_text}"

            for idx, step in enumerate(wf.steps, 1):
                agent = agents.get(step.agent) or agents.get("default") or AgentDef(
                    name=step.agent,
                    description="",
                    system_prompt="You are a helpful local assistant.",
                )
                prov, _, _, mod = self.resolve_endpoint(agent)
                self.chat_items.append(
                    f"\n[bold {self.colors['warn']}][step {idx}/{len(wf.steps)} -> {agent.name} ({prov}:{mod})][/] "
                    f"[dim]{step.instruction}[/]"
                )
                step_prompt = (
                    f"{accumulated_context}\n\n"
                    f"--- YOUR STEP ({idx}/{len(wf.steps)} as '{agent.name}') ---\n"
                    f"{step.instruction}"
                )
                step_messages = [
                    {"role": "system", "content": self.build_system_prompt(agent, inline_skills)},
                    {"role": "user", "content": step_prompt},
                ]
                step_output = await self.run_single_agent(agent, step_messages, label=agent.name)
                accumulated_context += f"\n\n--- Output from Step {idx} ({agent.name}) ---\n{step_output}"
        else:
            agent = agents.get(self.config.active_name) or agents.get("default") or AgentDef(
                name="default",
                description="",
                system_prompt="You are a concise local terminal coding assistant.",
            )
            sys_prompt = self.build_system_prompt(agent, inline_skills)
            if not self.messages:
                self.messages.append({"role": "system", "content": sys_prompt})
            else:
                self.messages[0] = {"role": "system", "content": sys_prompt}
            if clean_text.strip() or attached_images:
                user_content: Any = (
                    [{"type": "text", "text": clean_text}, *attached_images]
                    if attached_images
                    else clean_text
                )
                self.messages.append({"role": "user", "content": user_content})
            await self.run_single_agent(agent, self.messages, label=agent.name)
        ok_stop, stop_reason = run_lifecycle_hooks("Stop", {"session_id": self.session_id})
        if not ok_stop and self.config.active_mode == "agent":
            self.chat_items.append(f"[{self.colors['warn']}]Stop hook check failed — feeding output back to agent...[/]")
            self.messages.append({"role": "user", "content": f"Stop hook check failed:\n{stop_reason}\nPlease fix this before finishing."})
            agents, _ = load_agents_and_workflows()
            ag = agents.get(self.config.active_name) or AgentDef("default", "", "")
            await self.run_single_agent(ag, self.messages, label=ag.name)

    def _render_active_spinners(self) -> None:
        for entry in self._active_spinners:
            entry["tick"] = entry.get("tick", 0) + 1
        self._spinner_rendered_lines = len(self._active_spinners)
        if self.active_tab == "prompt":
            self._begin_sync_frame()
            try:
                self.render_prompt_view("", 0)
            finally:
                self._end_sync_frame(restore_cursor=True)

    async def _spinner_loop(self) -> None:
        try:
            while self._active_spinners:
                self._render_active_spinners()
                await asyncio.sleep(0.09)
        except asyncio.CancelledError:
            pass

    def _start_thinking_spinner(self, label: str = "", verb: str = "Thinking") -> int:
        self._spinner_seq += 1
        sid = self._spinner_seq
        self._active_spinners.append({"id": sid, "label": label, "verb": verb, "tick": sid - 1})
        self._render_active_spinners()
        if self._spinner_task is None or self._spinner_task.done():
            self._spinner_task = asyncio.create_task(self._spinner_loop())
        return sid

    async def _stop_thinking_spinner(self, sid: int) -> None:
        before = len(self._active_spinners)
        self._active_spinners = [s for s in self._active_spinners if s["id"] != sid]
        if len(self._active_spinners) != before:
            self._render_active_spinners()
        if not self._active_spinners and self._spinner_task and not self._spinner_task.done():
            self._spinner_task.cancel()
            try:
                await self._spinner_task
            except asyncio.CancelledError:
                pass
            self._spinner_task = None


    def _extract_text_tool_calls(self, raw_text: str) -> tuple[str, List[Dict[str, Any]]]:
        """Fallback parser for models like qwen2.5-coder:14b that emit JSON tool calls in message.content."""
        valid_names = {t["function"]["name"] for t in get_all_tool_definitions()}
        decoder = json.JSONDecoder()
        extracted: List[Dict[str, Any]] = []
        spans: List[tuple[int, int]] = []

        # Strip optional <tool_call>...</tool_call> tags when scanning
        scan_text = re.sub(r"</?tool_call>", "\n", raw_text)
        idx = 0
        n = len(scan_text)
        while idx < n:
            brace = scan_text.find("{", idx)
            if brace == -1:
                break
            try:
                obj, end_idx = decoder.raw_decode(scan_text, brace)
            except json.JSONDecodeError:
                idx = brace + 1
                continue

            if isinstance(obj, dict):
                fn_block = obj.get("function") if isinstance(obj.get("function"), dict) else obj
                fn_name = fn_block.get("name")
                if fn_name in valid_names and ("arguments" in fn_block or "parameters" in fn_block):
                    raw_args = fn_block.get("arguments", fn_block.get("parameters", {}))
                    args_str = raw_args if isinstance(raw_args, str) else json.dumps(raw_args or {})
                    extracted.append(
                        {
                            "id": f"call_text_{len(extracted)+1}",
                            "type": "function",
                            "function": {"name": fn_name, "arguments": args_str},
                        }
                    )
                    spans.append((brace, end_idx))
                    idx = end_idx
                    continue
            idx = brace + 1

        if not extracted:
            return raw_text, []

        # Remove extracted JSON objects and empty ```json``` fences from visible prose
        rebuilt: List[str] = []
        last = 0
        for s, e in spans:
            rebuilt.append(scan_text[last:s])
            last = e
        rebuilt.append(scan_text[last:])
        cleaned = "".join(rebuilt)
        cleaned = re.sub(r"```(?:json)?\s*```", "", cleaned, flags=re.IGNORECASE).strip()
        return cleaned, extracted

    async def _stream_completion(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        label: str,
        spinner_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        stream_payload = dict(payload, stream=True)
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_map: Dict[int, Dict[str, Any]] = {}
        in_think = False
        stream_slot_idx: Optional[int] = None
        last_stream_render = 0.0

        try:
          async with client.stream("POST", url, headers=headers, json=stream_payload) as resp:
              if resp.status_code != 200:
                  body = await resp.aread()
                  raise httpx.HTTPStatusError(
                      f"HTTP {resp.status_code}: {body.decode('utf-8', errors='replace')[:300]}",
                      request=resp.request,
                      response=resp,
                  )

              async for raw_line in resp.aiter_lines():
                  line = raw_line.strip()
                  if not line or not line.startswith("data:"):
                      continue
                  data_str = line[5:].strip()
                  if data_str == "[DONE]":
                      break
                  try:
                      chunk = json.loads(data_str)
                  except json.JSONDecodeError:
                      continue

                  usage = chunk.get("usage")
                  if usage:
                      self.prompt_tokens += usage.get("prompt_tokens", 0)
                      self.completion_tokens += usage.get("completion_tokens", 0)

                  choices = chunk.get("choices") or []
                  if not choices:
                      continue
                  delta = choices[0].get("delta") or {}

                  r_piece = delta.get("reasoning_content") or ""
                  if r_piece:
                      reasoning_parts.append(r_piece)

                  c_piece = delta.get("content") or ""
                  if c_piece:
                      content_parts.append(c_piece)
                      if "<think>" in c_piece:
                          in_think = True
                      if not in_think:
                          visible_text = "".join(content_parts)
                          stripped_vis = visible_text.lstrip()
                          looks_like_tool_json = (
                              stripped_vis.startswith("{")
                              or stripped_vis.startswith("```json")
                              or stripped_vis.startswith("<tool_call>")
                          )
                          if not looks_like_tool_json:
                              clean_vis = re.sub(r"<think>.*?</think>", "", visible_text, flags=re.DOTALL).strip()
                              if clean_vis:
                                  if spinner_id is not None:
                                      await self._stop_thinking_spinner(spinner_id)
                                      spinner_id = None
                                  md_text = clean_vis
                                  if md_text.count("```") % 2 == 1:
                                      md_text += "\n```"
                                  elif md_text.count("~~~") % 2 == 1:
                                      md_text += "\n~~~"
                                  live_block = Group(
                                      Text.from_markup(f"[{self.colors['grad_start']}]{label}:[/]"),
                                      Markdown(md_text),
                                  )
                                  if stream_slot_idx is None:
                                      stream_slot_idx = len(self.chat_items)
                                      self.chat_items.append(live_block)
                                  else:
                                      self.chat_items[stream_slot_idx] = live_block
                                  now = time.monotonic()
                                  if self.active_tab == "prompt" and (now - last_stream_render) >= 0.05:
                                      last_stream_render = now
                                      self._begin_sync_frame()
                                      try:
                                          self.render_prompt_view(getattr(self, "_live_buf", ""), getattr(self, "_live_cursor", 0))
                                      finally:
                                          self._end_sync_frame(restore_cursor=True)
                      if "</think>" in c_piece:
                          in_think = False

                  for tc in delta.get("tool_calls", []) or []:
                      idx = tc.get("index", 0)
                      slot = tool_map.setdefault(
                          idx,
                          {"id": tc.get("id") or f"call_{idx}", "type": "function", "function": {"name": "", "arguments": ""}},
                      )
                      if tc.get("id"):
                          slot["id"] = tc["id"]
                      fn = tc.get("function") or {}
                      if fn.get("name"):
                          slot["function"]["name"] += fn["name"]
                      if fn.get("arguments"):
                          slot["function"]["arguments"] += fn["arguments"]

        finally:
            if stream_slot_idx is not None and stream_slot_idx < len(self.chat_items):
                self.chat_items.pop(stream_slot_idx)
            partial_txt = "".join(content_parts).strip()
            if partial_txt:
                self._last_partial_assistant = partial_txt

        msg: Dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts),
        }
        if reasoning_parts:
            msg["reasoning_content"] = "".join(reasoning_parts)
        if tool_map:
            msg["tool_calls"] = [tool_map[k] for k in sorted(tool_map.keys())]
        return msg

    async def run_single_agent(
        self, agent: AgentDef, convo: List[Dict[str, Any]], label: str
    ) -> str:
        provider, base_url, api_key, model_name = self.resolve_endpoint(agent)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        final_assistant_text = ""

        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                for _ in range(16):
                    used_tok = self.prompt_tokens + self.completion_tokens
                    ctx_cap = max(2048, int(self.config.num_ctx))
                    pct_used = int((used_tok / ctx_cap) * 100)
                    if used_tok >= int(ctx_cap * 0.85) and used_tok > getattr(self, "_ctx_warned_at_tokens", 0) + 1024:
                        self._ctx_warned_at_tokens = used_tok
                        self.chat_items.append(
                            f"[bold {self.colors['warn']}]⚠ Context warning: {used_tok:,}/{ctx_cap:,} tokens ({pct_used}% of num_ctx) — run /compact or raise num_ctx.[/]"
                        )
                    if (
                        convo is self.messages
                        and len(convo) > 6
                        and used_tok >= int(ctx_cap * 0.85)
                        and used_tok > getattr(self, "_auto_compacted_at_tokens", 0) + 2048
                    ):
                        self._auto_compacted_at_tokens = used_tok
                        self.chat_items.append(
                            f"[{self.colors['warn']}]⟳ Context at {pct_used}% of num_ctx — auto-compacting session...[/]"
                        )
                        await self.compact_session()

                    spinner_id = self._start_thinking_spinner(label)

                    payload: Dict[str, Any] = {
                        "model": model_name,
                        "messages": convo,
                        "temperature": self.config.temperature,
                        "max_tokens": self.config.max_tokens,
                        "tools": get_all_tool_definitions(allow_docs_skills=self.config.auto_docs_skills),
                    }
                    if provider == "ollama":
                        payload["options"] = {"num_ctx": self.config.num_ctx}

                    try:
                        if self.config.stream:
                            msg = await self._stream_completion(
                                client, f"{base_url}/chat/completions", headers, payload, label, spinner_id
                            )
                        else:
                            resp = await client.post(
                                f"{base_url}/chat/completions",
                                headers=headers,
                                json=payload,
                            )
                            resp.raise_for_status()
                            data = resp.json()
                            usage = data.get("usage", {})
                            self.prompt_tokens += usage.get("prompt_tokens", 0)
                            self.completion_tokens += usage.get("completion_tokens", 0)
                            msg = data["choices"][0]["message"]
                    finally:
                        await self._stop_thinking_spinner(spinner_id)

                    raw = msg.get("content") or ""
                    reasoning = msg.get("reasoning_content") or ""

                    for pattern in (r"<think>(.*?)</think>", r"<\|channel>thought(.*?)<channel\|>"):
                        for t in re.findall(pattern, raw, flags=re.DOTALL):
                            if t.strip():
                                self.thoughts.append(f"[{label} | {model_name}]\n{t.strip()}")
                    if reasoning.strip():
                        self.thoughts.append(f"[{label} | {model_name}]\n{reasoning.strip()}")

                    clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
                    clean = re.sub(r"<\|channel>thought.*?<channel\|>", "", clean, flags=re.DOTALL)
                    clean = re.sub(r"<\|?channel\|?>|<\|turn\|?>", "", clean).strip()

                    tool_calls = msg.get("tool_calls", []) or []
                    if not tool_calls and clean:
                        clean, parsed_text_tcs = self._extract_text_tool_calls(clean)
                        if parsed_text_tcs:
                            tool_calls = parsed_text_tcs
                            msg = dict(msg)
                            msg["content"] = clean
                            msg["tool_calls"] = tool_calls

                    convo.append(msg)

                    if clean:
                        final_assistant_text = clean
                        self.chat_items.append(f"[{self.colors['grad_start']}]{label}:[/]")
                        self.chat_items.append(Markdown(clean))

                    if not tool_calls:
                        break

                    parsed_calls = []
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        fn_name = fn.get("name", "")
                        try:
                            args = json.loads(fn.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            args = {}
                        parsed_calls.append((tc, fn_name, args))

                    can_parallel = len(parsed_calls) > 1 and all(
                        fn_name in ("delegate_subagent", "read_file", "search_files", "list_files")
                        for _, fn_name, _ in parsed_calls
                    )
                    if can_parallel:
                        outputs = await asyncio.gather(
                            *(self.run_tool_call(fn_name, args, label) for _, fn_name, args in parsed_calls)
                        )
                    else:
                        outputs = []
                        for _, fn_name, args in parsed_calls:
                            if fn_name in ("create_agent", "create_workflow", "create_theme", "create_plugin"):
                                asset_k = fn_name.replace("create_", "")
                                g_sid = self._start_thinking_spinner(label=asset_k, verb="Generating")
                                try:
                                    outputs.append(await self.run_tool_call(fn_name, args, label))
                                finally:
                                    await self._stop_thinking_spinner(g_sid)
                            else:
                                outputs.append(await self.run_tool_call(fn_name, args, label))

                    for (tc, _, _), tool_output in zip(parsed_calls, outputs):
                        convo.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.get("id", "call_1"),
                                "content": str(tool_output),
                            }
                        )
        except Exception as exc:
            err_msg = f"error ({label} @ {base_url}): {exc}"
            self.chat_items.append(f"[{self.colors['err']}]{err_msg}[/]")
            return err_msg

        return final_assistant_text

    # --------------------------------------------------------------------------
    # --------------------------------------------------------------------------
    # ESC INTERRUPT & UNIFIED SPLIT-PANE UI HELPERS (OPENCODE / CLAUDE CODE STYLE)
    # --------------------------------------------------------------------------
    def _decode_raw_key(self, raw: bytes) -> str:
        if not raw:
            return ""
        if raw == b"\x03":
            return "CTRL_C"
        if raw == b"\x1b":
            return "ESC"
        if raw in (b"\r", b"\n"):
            return "ENTER"
        if raw in (b"\x7f", b"\x08"):
            return "BACKSPACE"
        if raw == b"\t":
            return "TAB"
        if raw == b"\x15":
            return "CTRL_U"
        if raw == b"\x01":
            return "HOME"
        if raw == b"\x05":
            return "END"
        seq = raw.decode("utf-8", errors="ignore")
        seq_map = {
            "\x1b[A": "UP",
            "\x1b[B": "DOWN",
            "\x1b[D": "LEFT",
            "\x1b[C": "RIGHT",
            "\x1bOD": "LEFT",
            "\x1bOC": "RIGHT",
            "\x1b[H": "HOME",
            "\x1b[1~": "HOME",
            "\x1b[F": "END",
            "\x1b[4~": "END",
            "\x1b[3~": "DEL",
            "\x1b\r": "SHIFT_ENTER",
            "\x1b\n": "SHIFT_ENTER",
            "\x1b[13;2u": "SHIFT_ENTER",
            "\x1b[27;2;13~": "SHIFT_ENTER",
            "\x1b[13;2~": "SHIFT_ENTER",
        }
        if seq in seq_map:
            return seq_map[seq]
        if seq.startswith("\x1b"):
            return ""
        return seq

    async def _watch_esc_interrupt(self, cancel_event: asyncio.Event, target_task: asyncio.Task) -> None:
        if os.name == "nt":
            while not cancel_event.is_set() and not target_task.done():
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch == "\x1b":
                        cancel_event.set()
                        target_task.cancel()
                        return
                await asyncio.sleep(0.03)
            return

        fd = sys.stdin.fileno()
        try:
            old = termios.tcgetattr(fd)
        except Exception:
            return

        def _set_raw_keep_opost() -> None:
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            attrs[1] |= (termios.OPOST | termios.ONLCR)
            termios.tcsetattr(fd, termios.TCSADRAIN, attrs)

        try:
            _set_raw_keep_opost()
            was_paused = False
            while not cancel_event.is_set() and not target_task.done():
                if self._pause_esc_watcher:
                    if not was_paused:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old)
                        was_paused = True
                    await asyncio.sleep(0.05)
                    continue
                elif was_paused:
                    _set_raw_keep_opost()
                    was_paused = False

                rlist, _, _ = select.select([fd], [], [], 0.04)
                if rlist:
                    raw = os.read(fd, 64)
                    if raw == b"\x1b":
                        r2, _, _ = select.select([fd], [], [], 0.025)
                        if r2:
                            raw += os.read(fd, 64)
                    key = self._decode_raw_key(raw)
                    if not key:
                        await asyncio.sleep(0.01)
                        continue

                    if key in ("ESC", "CTRL_C"):
                        cancel_event.set()
                        target_task.cancel()
                        return

                    if self.active_tab != "prompt":
                        if key == "LEFT":
                            self.switch_tab(-1)
                            cancel_event.set()
                            target_task.cancel()
                            return
                        elif key == "RIGHT":
                            self.switch_tab(1)
                            cancel_event.set()
                            target_task.cancel()
                            return
                        continue

                    prev_buf = self._live_buf
                    cur = self._live_cursor

                    if key == "LEFT":
                        if prev_buf == "":
                            self.switch_tab(-1)
                            cancel_event.set()
                            target_task.cancel()
                            return
                        if cur > 0:
                            self._live_cursor = cur - 1
                            self._redraw_input_line(self._live_buf, self._live_cursor)
                    elif key == "RIGHT":
                        if prev_buf == "":
                            self.switch_tab(1)
                            cancel_event.set()
                            target_task.cancel()
                            return
                        pred = self._slash_prediction(prev_buf) if cur == len(prev_buf) else ""
                        if pred:
                            self._live_buf = prev_buf + pred
                            self._live_cursor = len(self._live_buf)
                            self._update_input(prev_buf, self._live_buf, self._live_cursor)
                        elif cur < len(prev_buf):
                            self._live_cursor = cur + 1
                            self._redraw_input_line(self._live_buf, self._live_cursor)
                    elif key == "TAB":
                        pred = self._slash_prediction(prev_buf)
                        if pred:
                            self._live_buf = prev_buf + pred
                            self._live_cursor = len(self._live_buf)
                            self._update_input(prev_buf, self._live_buf, self._live_cursor)
                    elif key == "HOME":
                        self._live_cursor = 0
                        self._redraw_input_line(self._live_buf, self._live_cursor)
                    elif key == "END":
                        self._live_cursor = len(self._live_buf)
                        self._redraw_input_line(self._live_buf, self._live_cursor)
                    elif key == "CTRL_U":
                        self._live_buf = ""
                        self._live_cursor = 0
                        self._update_input(prev_buf, "", 0)
                    elif key == "BACKSPACE":
                        if cur > 0:
                            self._live_buf = prev_buf[: cur - 1] + prev_buf[cur:]
                            self._live_cursor = cur - 1
                            self._update_input(prev_buf, self._live_buf, self._live_cursor)
                    elif key == "DEL":
                        if cur < len(prev_buf):
                            self._live_buf = prev_buf[:cur] + prev_buf[cur + 1 :]
                            self._update_input(prev_buf, self._live_buf, self._live_cursor)
                    elif key in ("SHIFT_ENTER", "ALT_ENTER"):
                        self._live_buf = prev_buf[:cur] + "\n" + prev_buf[cur:]
                        self._live_cursor = cur + 1
                        self._update_input(prev_buf, self._live_buf, self._live_cursor)
                    elif key == "ENTER":
                        submitted = self._live_buf.strip()
                        self._live_buf = ""
                        self._live_cursor = 0
                        self._update_input(prev_buf, "", 0)
                        if submitted:
                            if submitted.lower().startswith("/nudge"):
                                self._nudge_hint = submitted[6:].strip()
                                self._nudge_requested = True
                                cancel_event.set()
                                target_task.cancel()
                                return
                            else:
                                self._queued_live_inputs.append(submitted)
                                self.chat_items.append(f"[dim]queued: {submitted}[/]")
                    else:
                        printable = "".join(ch for ch in key if ch.isprintable() or ch == "\n")
                        if printable:
                            self._live_buf = prev_buf[:cur] + printable + prev_buf[cur:]
                            self._live_cursor = cur + len(printable)
                            self._update_input(prev_buf, self._live_buf, self._live_cursor)

                await asyncio.sleep(0.015)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass

    async def _run_prompt_interruptible(self, user_text: str, is_continuation: bool = False) -> None:
        self._live_buf = ""
        self._live_cursor = 0
        self._queued_live_inputs: List[str] = []
        current_prompt: Optional[str] = user_text

        while current_prompt is not None:
            self._nudge_requested = False
            self._nudge_hint = ""
            self._last_partial_assistant = ""
            cancel_event = asyncio.Event()
            agent_task = asyncio.create_task(self.dispatch_prompt(current_prompt))
            watcher_task = asyncio.create_task(self._watch_esc_interrupt(cancel_event, agent_task))
            current_prompt = None

            try:
                await agent_task
            except asyncio.CancelledError:
                for sp in list(self._active_spinners):
                    await self._stop_thinking_spinner(sp["id"])
                sys.stdout.write("\r\033[K\n")
                sys.stdout.flush()

                if getattr(self, "_nudge_requested", False):
                    partial = getattr(self, "_last_partial_assistant", "").strip()
                    hint = getattr(self, "_nudge_hint", "").strip()
                    if partial:
                        clean_partial = re.sub(r"<think>.*?</think>", "", partial, flags=re.DOTALL).strip()
                        if clean_partial:
                            self.chat_items.append(
                                Group(
                                    Text.from_markup(f"[dim]{self.config.active_name} (paused):[/]"),
                                    Markdown(clean_partial),
                                )
                            )
                        self.messages.append({"role": "assistant", "content": partial})

                    self.chat_items.append(
                        f"[bold {self.colors['warn']}]⟳ Nudge — halting to second-guess before continuing...[/]"
                        + (f" [dim]({hint})[/]" if hint else "")
                    )
                    self.render_prompt_view(self._live_buf, self._live_cursor)

                    nudge_instruction = (
                        "STOP right where you are for a moment and critically second-guess your reasoning, "
                        "assumptions, and partial output so far. Check whether you missed an edge case, made an "
                        "incorrect assumption, or took a suboptimal path"
                        + (f" — specifically check: {hint}" if hint else "")
                        + ". Briefly state any correction or confirmation, then continue seamlessly from where you left off."
                    )
                    self.messages.append({"role": "user", "content": nudge_instruction})
                    current_prompt = ""
                else:
                    self.chat_items.append(
                        f"[bold {self.colors['warn']}]■ Interrupted by user (Esc)[/] [dim]— state preserved; enter follow-up to redirect.[/]"
                    )
            finally:
                cancel_event.set()
                if not watcher_task.done():
                    watcher_task.cancel()
                    try:
                        await watcher_task
                    except asyncio.CancelledError:
                        pass

            if current_prompt is None and self._queued_live_inputs:
                queued_cmd = self._queued_live_inputs.pop(0)
                if queued_cmd.startswith("/") and self.handle_slash_command(queued_cmd):
                    self.render_prompt_view(self._live_buf, self._live_cursor)
                else:
                    if self.chat_items:
                        self.chat_items.append(Rule(style="dim"))
                    self.chat_items.append(f"[{self.colors['grad_start']}]>[/] {queued_cmd}")
                    current_prompt = queued_cmd


    def _ui_render_split_frame(
        self,
        title: str,
        subtitle: str,
        left_rows: List[Tuple[str, bool, str]],
        right_blocks: List[Any],
        keycaps: List[Tuple[str, str]],
        status_banner: str = "",
        left_panel_override: Optional[Any] = None,
    ) -> None:
        self._begin_sync_frame()
        try:
            sys.stdout.write("\033[H\033[?25l")
            self.render_header()
            c = self.colors
            ly = self.layout
            w = max(40, self.console.width)
            h = max(14, self.console.height)
            hdr_rows = self._header_row_count()
            body_h = max(6, h - (hdr_rows + 4))
            split_mode = str(ly.get("split_mode", "horizontal")).lower()
            ratio = max(0.20, min(0.65, float(ly.get("split_ratio", 0.36))))
            box_type = self._box_style()

            max_items = max(3, body_h - 4)
            total = len(left_rows)
            selected_idx = next((i for i, r in enumerate(left_rows) if r[1]), 0)
            scroll = max(0, min(max(0, selected_idx - max_items // 2), max(0, total - max_items)))

            if split_mode == "vertical":
                pane_inner_w = max(16, w - (2 if box_type is None else 4))
            elif split_mode == "inspector_only":
                pane_inner_w = max(16, w - 28)
            else:
                calc_left_w = max(24, min(int(w * 0.65), int(w * ratio)))
                pane_inner_w = max(16, calc_left_w - (2 if box_type is None else 4))

            left_lines: List[Any] = []
            for idx in range(scroll, min(total, scroll + max_items)):
                label, is_sel, badge = left_rows[idx]
                badge_str = f"  [{badge}]" if badge else ""
                avail_w = max(6, pane_inner_w - 2 - len(badge_str))
                shown_label = (label[: avail_w - 1] + "…") if len(label) > avail_w else label
                line = Text(no_wrap=True)
                line.append("▶ " if is_sel else "  ", style=f"bold {c['accent']}" if is_sel else "dim")
                line.append(shown_label, style=f"bold {c['grad_start']}" if is_sel else "")
                if badge:
                    b_style = f"bold {c['ok']}" if badge in ("ACTIVE", "ON", "APPROVED", "AUTO") else "dim"
                    line.append(badge_str, style=b_style)
                left_lines.append(line)
            if not left_lines:
                left_lines.append(Text("  (empty)", style="dim"))

            right_renderables: List[Any] = []
            for b in right_blocks:
                if isinstance(b, str):
                    if b.startswith("#"):
                        right_renderables.append(Markdown(b))
                    else:
                        try:
                            right_renderables.append(Text.from_markup(b))
                        except Exception:
                            right_renderables.append(Text(b))
                else:
                    right_renderables.append(b)

            if split_mode == "inspector_only":
                sel_label = left_rows[selected_idx][0] if left_rows else "none"
                bar = Text()
                bar.append(f"◀ {title} [{selected_idx + 1}/{max(1, total)}]: ", style="dim")
                bar.append(sel_label, style=f"bold {c['accent']}")
                bar.append(" ▶", style="dim")
                combined = [bar, Rule(style="dim"), *right_renderables]
                if box_type is None:
                    self.console.print(Padding(Group(*combined), (0, 1)))
                else:
                    self.console.print(Panel(
                        Group(*combined),
                        title=f"[bold]{subtitle}[/bold]",
                        border_style=c["accent"],
                        box=box_type,
                        width=w,
                        height=body_h,
                    ))
            elif split_mode == "vertical":
                top_h = max(5, min(body_h - 5, int(body_h * ratio)))
                bot_h = max(5, body_h - top_h)
                if box_type is None:
                    self.console.print(Text(f"── {title} ({total}) ──", style=f"bold {c['accent']}"))
                    self.console.print(Group(*left_lines[: max(1, top_h - 2)]))
                    self.console.print(Rule(style="dim"))
                    self.console.print(Text(f"── {subtitle} ──", style="bold"))
                    self.console.print(Group(*right_renderables))
                else:
                    self.console.print(
                        Group(
                            Panel(Group(*left_lines[: max(1, top_h - 2)]), title=f"[bold]{title}[/bold] ({total})", border_style=c["accent"], box=box_type, width=w, height=top_h),
                            Panel(Group(*right_renderables), title=f"[bold]{subtitle}[/bold]", border_style="dim", box=box_type, width=w, height=bot_h),
                        )
                    )
            else:
                left_w = max(24, min(int(w * 0.65), int(w * ratio)))
                right_w = max(28, w - left_w - 1)
                grid = Table.grid(padding=(0, 0))
                grid.add_column(width=left_w)
                grid.add_column(width=1)
                grid.add_column(width=right_w)
                left_body = left_panel_override if left_panel_override is not None else Group(*left_lines)
                left_t = f"[bold]{title}[/bold] ({total})" if left_panel_override is None else f"[bold]{title}[/bold]"
                if box_type is None:
                    grid.add_row(
                        Padding(Group(Text(title if left_panel_override is not None else f"{title} ({total})", style=f"bold {c['accent']}"), Rule(style="dim"), left_body), (0, 1)),
                        "",
                        Padding(Group(Text(subtitle, style="bold"), Rule(style="dim"), *right_renderables), (0, 1)),
                    )
                else:
                    grid.add_row(
                        Panel(left_body, title=left_t, border_style=c["accent"], box=box_type, width=left_w, height=body_h),
                        "",
                        Panel(Group(*right_renderables), title=f"[bold]{subtitle}[/bold]", border_style="dim", box=box_type, width=right_w, height=body_h),
                    )
                self.console.print(grid)

            sys.stdout.write(f"\033[{h - 2};1H\033[K")
            sys.stdout.flush()
            self.console.print(Rule(style="dim"), end="")
            sys.stdout.write(f"\033[{h - 1};1H\033[K")
            sys.stdout.flush()
            if status_banner:
                ft = Text(status_banner, style=f"bold {c['ok']}")
            else:
                ft = Text("  ".join(f"[{k}] {desc}" for k, desc in keycaps), style="dim")
            ft.truncate(w - 2, overflow="ellipsis")
            self.console.print(Align.center(ft), end="", no_wrap=True)
            sys.stdout.write("\033[J")
        finally:
            self._end_sync_frame(restore_cursor=False)


    # 7. AGENTS, SKILLS & WORKFLOWS TAB
    # --------------------------------------------------------------------------
    def _prompt_provider_override(self) -> tuple[str, str, str, str]:
        choice = self.select_menu(
            "Agent Model / Provider Binding:",
            [
                "1. Inherit global default provider & model",
                "2. Ollama (local — e.g. deepseek-r1:7b, qwen2.5-coder:14b)",
                "3. llama.cpp (local server — http://localhost:8080/v1)",
                "4. API Endpoint (Gemini / OpenAI / OpenRouter / Custom)",
            ],
            header_fn=self.render_header,
        )
        self.clear()
        self.render_header()
        if choice <= 0:
            return "", "", "", ""
        elif choice == 1:
            prov = "ollama"
            url = input("Ollama Base URL [http://localhost:11434/v1]: ").strip() or "http://localhost:11434/v1"
            model = input("Ollama Model (e.g. deepseek-r1:7b): ").strip()
            key = ""
        elif choice == 2:
            prov = "llamacpp"
            url = input("llama.cpp Base URL [http://localhost:8080/v1]: ").strip() or "http://localhost:8080/v1"
            model = input("Model name [local-model]: ").strip() or "local-model"
            key = ""
        else:
            prov = "api"
            url = (
                input("API Base URL [https://generativelanguage.googleapis.com/v1beta/openai]: ").strip()
                or "https://generativelanguage.googleapis.com/v1beta/openai"
            )
            model = input("Model name (e.g. gemini-2.5-flash, gpt-4o): ").strip()
            key = input("API Key: ").strip()
        return prov, url.rstrip("/"), key, model

    def create_agent_wizard(self) -> None:
        self.clear()
        self.render_header()
        self.console.print("[bold]Create New Agent[/]\n")
        name = input("Agent name (e.g. deepseek-planner): ").strip().lower().replace(" ", "-")
        if not name:
            return
        desc = input("Short description: ").strip()
        sys_prompt = input("System prompt: ").strip()

        prov, base_url, api_key, model_ov = self._prompt_provider_override()

        avail_skills = list(load_skills().keys())
        self.console.print(f"[dim]Available skills: {', '.join(avail_skills) or 'none'}[/]")
        skills_raw = input("Attached skills (comma-separated, optional): ").strip()
        skills = [s.strip() for s in skills_raw.split(",") if s.strip() in avail_skills]

        ag = AgentDef(
            name=name,
            description=desc,
            system_prompt=sys_prompt,
            provider=prov,
            base_url=base_url,
            api_key=api_key,
            model=model_ov,
            skills=skills,
            type="agent",
        )
        (AGENTS_DIR / f"{name}.json").write_text(json.dumps(asdict(ag), indent=2))
        self.config.active_mode = "agent"
        self.config.active_name = name
        self.config.save()

    def edit_agent_endpoint_wizard(self, agent: AgentDef) -> None:
        prov, base_url, api_key, model_ov = self._prompt_provider_override()
        agent.provider = prov
        agent.base_url = base_url
        agent.api_key = api_key
        agent.model = model_ov
        (AGENTS_DIR / f"{agent.name}.json").write_text(json.dumps(asdict(agent), indent=2))

    def create_workflow_wizard(self) -> None:
        self.clear()
        self.render_header()
        self.console.print("[bold]Create New Multi-Agent Workflow[/]\n")
        name = input("Workflow name (e.g. r1-plan-gemini-code): ").strip().lower().replace(" ", "-")
        if not name:
            return
        desc = input("Short description: ").strip()
        agents, _ = load_agents_and_workflows()
        self.console.print("\n[dim]Available agents:[/]")
        for aname, ag in agents.items():
            prov, _, _, mod = self.resolve_endpoint(ag)
            self.console.print(f"  - {aname} ({prov}:{mod})")

        steps: List[WorkflowStep] = []
        step_num = 1
        while True:
            ag = input(f"\nStep {step_num} agent name (or Enter to finish): ").strip()
            if not ag:
                break
            if ag not in agents:
                self.console.print(
                    f"[{self.colors['err']}]Unknown agent '{ag}'. Pick from: {', '.join(agents.keys())}[/]"
                )
                continue
            instr = input(f"Step {step_num} instruction for '{ag}': ").strip()
            if not instr:
                instr = f"Execute step {step_num} on the provided context."
            steps.append(WorkflowStep(agent=ag, instruction=instr))
            step_num += 1

        if steps:
            wf = WorkflowDef(name=name, description=desc, steps=steps, type="workflow")
            (AGENTS_DIR / f"{name}.json").write_text(json.dumps(asdict(wf), indent=2))
            self.config.active_mode = "workflow"
            self.config.active_name = name
            self.config.save()

    def create_skill_wizard(self) -> None:
        self.clear()
        self.render_header()
        self.console.print("[bold]Create New Skill[/]\n")
        name = input("Skill name (e.g. python-typing): ").strip().lower().replace(" ", "-")
        if not name:
            return
        desc = input("Short description: ").strip()
        self.console.print("Format: [1] Claude SKILL.md folder (skills/<name>/SKILL.md) [2] Flat .md file: ")
        fmt_choice = input().strip()
        self.console.print("Enter skill instructions (type END on a new line to save):")
        lines: List[str] = []
        while True:
            line = input()
            if line.strip() == "END":
                break
            lines.append(line)
        if lines:
            body = "\n".join(lines).strip()
            fm = f"---\nname: {name}\ndescription: {desc or name}\n---\n\n"
            if fmt_choice == "1" or not fmt_choice:
                target_dir = Path.cwd() / "skills" / name
                target_dir.mkdir(parents=True, exist_ok=True)
                (target_dir / "SKILL.md").write_text(fm + body + "\n")
            else:
                SKILLS_DIR.mkdir(parents=True, exist_ok=True)
                (SKILLS_DIR / f"{name}.md").write_text(fm + body + "\n")
            if name not in self.config.active_skills:
                self.config.active_skills.append(name)
                self.config.save()

    
    def _prompt_in_pane_text(self, title: str, prompt_label: str, hint: str = "") -> Optional[str]:
        """Dedicated in-pane single-line input prompt on the left pane without breaking raw TUI."""
        buf = ""
        c = self.colors
        while True:
            left_content = [
                Text(f"▶ {title}", style=f"bold {c['accent']}"),
                Rule(style="dim"),
                Text(prompt_label, style="bold"),
                Text(f"> {buf}█", style=f"bold {c['grad_start']}"),
            ]
            if hint:
                left_content.append(Text(f"  ({hint})", style="dim"))
            left_content.append(Rule(style="dim"))
            left_content.append(Text("Enter: confirm   Esc / ^X: cancel", style="dim"))

            right_blocks = [
                f"[bold {c['grad_start']}]{title} Wizard[/]",
                Rule(style="dim"),
                f"[bold]Creation Target:[/] {title.replace('NEW ', '')}",
                "[dim]Type the name slug on the left pane and press Enter.[/]",
                f"[dim]After naming, the in-pane editor will open on the left to compose instructions.[/]",
                "",
                "[bold]Controls:[/]",
                "  [bold]Enter[/]    Confirm name & open editor",
                "  [bold]Esc / ^X[/] Cancel immediately and return",
            ]
            self._ui_render_split_frame(
                title=title,
                subtitle="Step 1: Identifier",
                left_rows=[],
                right_blocks=right_blocks,
                keycaps=[("Enter", "confirm"), ("Esc/^X", "cancel")],
                left_panel_override=Group(*left_content),
            )
            key = read_key()
            if key in ("ESC", "CTRL_X"):
                return None
            elif key == "ENTER":
                cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", buf).strip("-").lower()
                return cleaned if cleaned else None
            elif key == "BACKSPACE":
                buf = buf[:-1]
            elif len(key) == 1 and (key.isalnum() or key in ("-", "_", " ")):
                buf += key.replace(" ", "-").lower()

    def _run_in_pane_editor(
        self,
        title: str,
        filename: str,
        initial_lines: List[str],
        preview_renderer: Any,
        footer_hint: str = "Format: <agent>: <instruction>",
    ) -> Optional[List[str]]:
        """
        Runs a stylistically uniform in-pane editor dedicated entirely inside the left pane.
        - ^O: Save / WriteOut
        - ^X: Cut current line
        - ^V: Paste line / clipboard
        - Up / Down: Navigate lines
        - Enter: Insert new line
        - Backspace: Delete character / merge lines
        - Esc: Exit dialog (Save and Exit / Exit without saving / Cancel)
        """
        lines = list(initial_lines) if initial_lines else [""]
        cur_line = 0
        cut_buffer = ""
        c = self.colors

        while True:
            body_h = max(6, self.console.height - (self._header_row_count() + 4))
            max_edit_rows = max(3, body_h - 4)
            scroll_top = max(0, min(max(0, cur_line - max_edit_rows // 2), max(0, len(lines) - max_edit_rows)))

            left_content: List[Any] = []
            top_bar = Text()
            top_bar.append("  Editor (Thule)  ", style=f"bold black on {c['accent']}")
            top_bar.append(f" {filename} ", style="bold")
            left_content.append(top_bar)
            left_content.append(Rule(style="dim"))

            for l_idx in range(scroll_top, min(len(lines), scroll_top + max_edit_rows)):
                is_active = (l_idx == cur_line)
                txt_line = Text()
                txt_line.append(f"{l_idx + 1:2d} ", style=f"bold {c['accent']}" if is_active else "dim")
                txt_val = lines[l_idx]
                if is_active:
                    txt_line.append(txt_val, style=f"bold {c['grad_start']}")
                    txt_line.append("█", style=f"bold {c['ok']}")
                else:
                    txt_line.append(txt_val)
                left_content.append(txt_line)

            displayed_cnt = min(len(lines), scroll_top + max_edit_rows) - scroll_top
            for _ in range(max(0, max_edit_rows - displayed_cnt)):
                left_content.append(Text("~", style="dim"))

            left_content.append(Rule(style="dim"))
            left_content.append(Text(footer_hint[:38], style="dim"))

            sub_title, right_blocks = preview_renderer(lines)

            editor_keycaps = [
                ("^O", "save"),
                ("^X", "cut"),
                ("^V", "paste"),
                ("↑/↓", "line"),
                ("Enter", "newline"),
                ("Esc", "exit/cancel"),
            ]
            self._ui_render_split_frame(
                title=f"Editor: {filename}",
                subtitle=sub_title,
                left_rows=[],
                right_blocks=right_blocks,
                keycaps=editor_keycaps,
                left_panel_override=Group(*left_content),
            )

            key = read_key()
            if key == "ESC":
                # Prompt user: exit with saving, exit without saving, or cancel
                while True:
                    prompt_content = [
                        Text("▶ EXIT CONFIRMATION", style=f"bold {c['accent']}"),
                        Rule(style="dim"),
                        Text("Save changes before exiting?", style="bold"),
                        "",
                        Text("  [1] Save & Exit", style=f"bold {c['ok']}"),
                        Text("  [2] Exit without saving (Discard)", style=f"bold {c['err']}"),
                        Text("  [3] Cancel (Return to editor)", style=f"bold {c['warn']}"),
                        Rule(style="dim"),
                        Text("Choose: 1 (save), 2 (discard), 3 / Esc (cancel)", style="dim"),
                    ]
                    prompt_blocks = [
                        f"[bold {c['grad_start']}]Exit Editor: {filename}[/]",
                        Rule(style="dim"),
                        "Choose an exit option:",
                        f"  [{c['ok']}][1] / [y] / [Enter][/]  Save modifications and apply asset.",
                        f"  [{c['err']}][2] / [n] / [d][/]      Discard all changes and exit.",
                        f"  [{c['warn']}][3] / [c] / [Esc][/]    Cancel and resume editing.",
                    ]
                    self._ui_render_split_frame(
                        title=f"Exit: {filename}",
                        subtitle="Save or Discard?",
                        left_rows=[],
                        right_blocks=prompt_blocks,
                        keycaps=[("1/y", "save & exit"), ("2/n", "discard"), ("3/Esc", "cancel")],
                        left_panel_override=Group(*prompt_content),
                    )
                    act_key = read_key()
                    if act_key in ("1", "y", "Y", "s", "S", "ENTER"):
                        return lines
                    elif act_key in ("2", "n", "N", "d", "D"):
                        return None
                    elif act_key in ("3", "c", "C", "ESC", "CTRL_C"):
                        break
                continue

            elif key in ("CTRL_O", "F2"):
                return lines
            elif key in ("CTRL_X", "CTRL_K"):
                cut_buffer = lines.pop(cur_line) if len(lines) > 1 else lines[0]
                if not lines:
                    lines = [""]
                cur_line = min(cur_line, len(lines) - 1)
            elif key in ("CTRL_V", "CTRL_U") or key.startswith("PASTE:"):
                paste_text = ""
                if key.startswith("PASTE:"):
                    paste_text = key[6:]
                else:
                    for clip_cmd in (["wl-paste", "-n"], ["xclip", "-o", "-selection", "clipboard"], ["xsel", "-ob"], ["pbpaste"]):
                        try:
                            res = subprocess.run(clip_cmd, capture_output=True, text=True, timeout=0.5)
                            if res.returncode == 0 and res.stdout:
                                paste_text = res.stdout
                                break
                        except Exception:
                            pass
                    if not paste_text and cut_buffer:
                        paste_text = cut_buffer
                if paste_text:
                    clean_p = paste_text.replace("\r\n", "\n").replace("\r", "\n")
                    p_lines = clean_p.splitlines()
                    if p_lines:
                        lines[cur_line] += p_lines[0]
                        for extra in p_lines[1:]:
                            cur_line += 1
                            lines.insert(cur_line, extra)
            elif key == "UP":
                cur_line = max(0, cur_line - 1)
            elif key == "DOWN":
                cur_line = min(len(lines) - 1, cur_line + 1)
            elif key == "ENTER":
                cur_line += 1
                lines.insert(cur_line, "")
            elif key == "BACKSPACE":
                if lines[cur_line]:
                    lines[cur_line] = lines[cur_line][:-1]
                elif len(lines) > 1:
                    lines.pop(cur_line)
                    cur_line = max(0, cur_line - 1)
            elif len(key) == 1 and key.isprintable():
                lines[cur_line] += key

    def run_agents_tab(self) -> None:
        idx = 0
        filter_q = ""
        filtering = False
        status_msg = ""

        while self.active_tab == "agents":
            agents, workflows = load_agents_and_workflows()
            skills = load_skills()

            entries: List[Dict[str, Any]] = []
            for name, wf in workflows.items():
                is_act = self.config.active_mode == "workflow" and self.config.active_name == name
                entries.append({
                    "kind": "workflow",
                    "name": name,
                    "obj": wf,
                    "label": f"{'●' if is_act else '○'} {name}",
                    "badge": "ACTIVE" if is_act else "WORKFLOW",
                })
            for name, ag in agents.items():
                is_act = self.config.active_mode == "agent" and self.config.active_name == name
                entries.append({
                    "kind": "agent",
                    "name": name,
                    "obj": ag,
                    "label": f"{'●' if is_act else '○'} {name}",
                    "badge": "ACTIVE" if is_act else "AGENT",
                })
            for sname, scontent in skills.items():
                is_on = sname in self.config.active_skills
                entries.append({
                    "kind": "skill",
                    "name": sname,
                    "obj": scontent,
                    "label": f"{'✓' if is_on else '·'} @{sname}",
                    "badge": "ON" if is_on else "SKILL",
                })
            for pslug, pmeta in PLUGIN_REGISTRY.plugins.items():
                is_en = bool(pmeta.get("enabled", True))
                entries.append({
                    "kind": "plugin",
                    "name": pslug,
                    "obj": pmeta,
                    "label": f"{'⚡' if is_en else '·'} {pslug}",
                    "badge": "ON" if is_en else "OFF",
                })

            if filter_q:
                filtered = [e for e in entries if filter_q.lower() in e["name"].lower()]
                if filtered:
                    entries = filtered

            idx = idx % max(1, len(entries))
            sel = entries[idx]

            left_rows = [(e["label"], i == idx, e["badge"]) for i, e in enumerate(entries)]
            right_blocks: List[Any] = []

            if sel["kind"] == "agent":
                ag: AgentDef = sel["obj"]
                prov, b_url, _, mod = self.resolve_endpoint(ag)
                is_act = sel["badge"] == "ACTIVE"
                right_blocks.append(
                    f"[bold {self.colors['grad_start']}]{ag.name}[/]  "
                    f"[{'bold ' + self.colors['ok'] if is_act else 'dim'}][{sel['badge']}][/]"
                )
                right_blocks.append(f"[dim]{ag.description or 'No description provided.'}[/]")
                right_blocks.append(Rule(style="dim"))
                right_blocks.append(f"[bold]Binding:[/]   {prov} / [bold]{mod}[/]  [dim]({'override' if (ag.provider or ag.model) else 'inherited'})[/]")
                right_blocks.append(f"[bold]Endpoint:[/]  [dim]{b_url}[/]")
                right_blocks.append(f"[bold]Skills:[/]    {', '.join('@' + s for s in ag.skills) if ag.skills else '[dim]none[/]'}")
                right_blocks.append(f"[bold]Exec Mode:[/] {self.config.execution_mode.upper()}  [dim](Shift+Tab in Prompt)[/]")
                right_blocks.append("")
                right_blocks.append(f"[bold {self.colors['accent']}]System Instructions[/]")
                right_blocks.append(Panel(ag.system_prompt or "(empty)", box=ROUNDED, border_style="dim"))
            elif sel["kind"] == "workflow":
                wf: WorkflowDef = sel["obj"]
                is_act = sel["badge"] == "ACTIVE"
                right_blocks.append(
                    f"[bold {self.colors['grad_end']}]{wf.name}[/]  "
                    f"[{'bold ' + self.colors['ok'] if is_act else 'dim'}][{sel['badge']}][/]"
                )
                right_blocks.append(f"[dim]{wf.description}[/]")
                right_blocks.append(Rule(style="dim"))
                right_blocks.append(f"[bold {self.colors['accent']}]Pipeline Execution Stages ({len(wf.steps)})[/]")
                for s_i, st in enumerate(wf.steps, 1):
                    st_ag = agents.get(st.agent)
                    st_tag = f"{st_ag.provider or self.config.provider}:{st_ag.model or self.config.model}" if st_ag else "default"
                    right_blocks.append(f"  [bold {self.colors['grad_start']}]{s_i}. {st.agent}[/] [dim]({st_tag})[/]")
                    right_blocks.append(f"     [dim]{st.instruction}[/]")
            elif sel["kind"] == "skill":
                sname = sel["name"]
                scontent = sel["obj"]
                is_on = sname in self.config.active_skills
                right_blocks.append(
                    f"[bold {self.colors['grad_start']}]@{sname}[/]  "
                    f"[{'bold ' + self.colors['ok'] if is_on else 'dim'}][{'GLOBAL ACTIVE' if is_on else 'AVAILABLE'}][/]"
                )
                right_blocks.append(f"[dim]Injected into system prompt when toggled ON or referenced via @{sname}.[/]")
                right_blocks.append(Rule(style="dim"))
                right_blocks.append(Markdown(scontent))
            else:
                pslug = sel["name"]
                pmeta = sel["obj"]
                is_en = bool(pmeta.get("enabled", True))
                p_tools = [t.name for t in PLUGIN_REGISTRY.tools.values() if t.plugin == pslug]
                p_cmds = [c.name for c in PLUGIN_REGISTRY.commands.values() if c.plugin == pslug]
                right_blocks.append(
                    f"[bold {self.colors['grad_start']}]plugin:{pslug}[/]  "
                    f"[{'bold ' + self.colors['ok'] if is_en else 'dim'}][{'ENABLED' if is_en else 'DISABLED'}][/]"
                )
                right_blocks.append(f"[dim]{pmeta.get('description') or 'Custom Python plugin'}[/]")
                right_blocks.append(Rule(style="dim"))
                right_blocks.append(f"[bold]Path:[/]     [dim]{pmeta.get('path', '')}[/]")
                right_blocks.append(f"[bold]Tools:[/]    {', '.join(p_tools) if p_tools else '[dim]none[/]'}")
                right_blocks.append(f"[bold]Commands:[/] {', '.join(p_cmds) if p_cmds else '[dim]none[/]'}")
                if pslug in PLUGIN_REGISTRY.errors:
                    right_blocks.append(f"[{self.colors['err']}]Load Error: {PLUGIN_REGISTRY.errors[pslug]}[/]")
                elif Path(pmeta.get("path", "")).is_file():
                    right_blocks.append("")
                    right_blocks.append(Panel(Path(pmeta["path"]).read_text()[:1200], box=ROUNDED, border_style="dim"))

            sub = f"filter: /{filter_q}_" if filtering else (f"active: {self.config.active_mode}:{self.config.active_name} · {len(agents)} agents · {len(skills)} skills")
            self._ui_render_split_frame(
                "AGENTS, WORKFLOWS & SKILLS",
                sub,
                left_rows,
                right_blocks,
                [("↑/↓", "nav"), ("Enter", "activate/toggle"), ("e", "edit endpoint"), ("n", "new agent"), ("w", "workflow"), ("s", "skill"), ("/", "filter"), ("←/→", "tabs")],
                status_banner=status_msg,
            )
            status_msg = ""

            key = read_key()
            if key == "RESIZE":
                continue
            if filtering:
                if key in ("ESC", "ENTER"):
                    filtering = False
                    if key == "ESC":
                        filter_q = ""
                elif key == "BACKSPACE":
                    filter_q = filter_q[:-1]
                elif key.startswith("PASTE:"):
                    filter_q += key[6:].replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
                elif len(key) == 1 and key.isprintable():
                    filter_q += key
                continue

            if key == "LEFT":
                self.switch_tab(-1)
            elif key == "RIGHT":
                self.switch_tab(1)
            elif key == "UP":
                idx = (idx - 1) % len(entries)
            elif key == "DOWN":
                idx = (idx + 1) % len(entries)
            elif key == "/":
                filtering = True
                filter_q = ""
            elif key in ("w", "W"):
                w_name = self._prompt_in_pane_text("NEW WORKFLOW", "Workflow Slug Name:", "e.g. audit-and-fix")
                if not w_name:
                    status_msg = "Workflow creation cancelled"
                else:
                    template_lines = [
                        "planner: Analyze goal and author step-by-step technical plan",
                        "coder: Implement changes cleanly in the workspace",
                        "reviewer: Audit diffs for bugs, missing imports, and run tests",
                    ]
                    def workflow_preview(curr_lines: List[str]) -> tuple[str, List[Any]]:
                        avail_agents, _ = load_agents_and_workflows()
                        parsed_steps: List[tuple[str, str]] = []
                        for ln in curr_lines:
                            cl = ln.strip()
                            if not cl or cl.startswith("#"):
                                continue
                            if ":" in cl:
                                ag, inst = cl.split(":", 1)
                                parsed_steps.append((ag.strip(), inst.strip()))
                            else:
                                parsed_steps.append((cl, "Execute step on context."))
                        blocks: List[Any] = [
                            f"[bold {self.colors['grad_start']}]Workflow Live Preview & Validation[/]",
                            Rule(style="dim"),
                        ]
                        if not parsed_steps:
                            blocks.append("[dim]No steps defined yet. Type `<agent>: <instruction>` on the left.[/]")
                        else:
                            flow_str = " ➔ ".join(f"[bold {self.colors['grad_end']}]{ag}[/]" for ag, _ in parsed_steps)
                            blocks.append(f"[bold]Pipeline Flow:[/] {flow_str}")
                            blocks.append("")
                            for s_i, (ag, inst) in enumerate(parsed_steps, 1):
                                is_valid = ag in avail_agents
                                v_badge = f"[{self.colors['ok']}]✓ agent:{ag}[/]" if is_valid else f"[{self.colors['err']}]⚠ unknown agent '{ag}'[/]"
                                blocks.append(f"  [bold]{s_i}.[/] {v_badge}")
                                blocks.append(f"     [dim]{inst}[/]")
                        blocks.append(Rule(style="dim"))
                        blocks.append(f"[dim]Available agents: {', '.join(sorted(avail_agents.keys()))}[/]")
                        blocks.append(f"[dim]Press ^O or F2 to write/save. Press ^X or Esc to cancel.[/]")
                        return f"Pipeline Steps: {len(parsed_steps)}", blocks

                    res_lines = self._run_in_pane_editor(
                        title="NEW WORKFLOW",
                        filename=f"{w_name}.json",
                        initial_lines=template_lines,
                        preview_renderer=workflow_preview,
                        footer_hint="Format: <agent>: <instruction>",
                    )
                    if res_lines is None:
                        status_msg = "Workflow creation cancelled"
                    else:
                        steps: List[WorkflowStep] = []
                        for ln in res_lines:
                            cl = ln.strip()
                            if not cl or cl.startswith("#"):
                                continue
                            if ":" in cl:
                                ag, inst = cl.split(":", 1)
                                steps.append(WorkflowStep(agent=ag.strip(), instruction=inst.strip()))
                            else:
                                steps.append(WorkflowStep(agent=cl, instruction="Execute step on context."))
                        if not steps:
                            status_msg = "Workflow had no valid steps — not saved"
                        else:
                            wf = WorkflowDef(name=w_name, description=f"Workflow {w_name}", steps=steps, type="workflow")
                            (AGENTS_DIR / f"{w_name}.json").write_text(json.dumps(asdict(wf), indent=2))
                            self.config.active_mode = "workflow"
                            self.config.active_name = w_name
                            self.config.save()
                            status_msg = f"Created & activated workflow '{w_name}' ({len(steps)} steps)"

            elif key in ("n", "N"):
                a_name = self._prompt_in_pane_text("NEW AGENT", "Agent Slug Name:", "e.g. rust-expert")
                if not a_name:
                    status_msg = "Agent creation cancelled"
                else:
                    template_lines = [
                        f"You are a specialized {a_name} coding assistant.",
                        "Inspect code thoroughly with `read_file`, `list_files`, and `search_files`.",
                        "Apply clean, minimal implementations and verify edge cases.",
                    ]
                    def agent_preview(curr_lines: List[str]) -> tuple[str, List[Any]]:
                        pr_text = "\n".join(curr_lines).strip()
                        blocks = [
                            f"[bold {self.colors['grad_start']}]Agent Live Specification Preview[/]",
                            Rule(style="dim"),
                            f"[bold]Name:[/]        {a_name}",
                            f"[bold]Provider:[/]    {self.config.provider} [dim](inherited)[/]",
                            f"[bold]Model:[/]       {self.config.model} [dim](inherited)[/]",
                            "",
                            f"[bold {self.colors['accent']}]System Instructions ({len(pr_text)} chars)[/]",
                            Panel(pr_text or "(empty prompt)", box=ROUNDED, border_style="dim"),
                            Rule(style="dim"),
                            "[dim]Press ^O or F2 to write/save. Press ^X or Esc to cancel.[/]",
                        ]
                        return f"Agent: {a_name}", blocks

                    res_lines = self._run_in_pane_editor(
                        title="NEW AGENT",
                        filename=f"{a_name}.json",
                        initial_lines=template_lines,
                        preview_renderer=agent_preview,
                        footer_hint="Compose system instructions",
                    )
                    if res_lines is None:
                        status_msg = "Agent creation cancelled"
                    else:
                        sys_prompt = "\n".join(res_lines).strip()
                        new_ag = AgentDef(name=a_name, description=f"Specialized {a_name} assistant", system_prompt=sys_prompt)
                        (AGENTS_DIR / f"{a_name}.json").write_text(json.dumps(asdict(new_ag), indent=2))
                        self.config.active_mode = "agent"
                        self.config.active_name = a_name
                        self.config.save()
                        status_msg = f"Created & activated agent '{a_name}'"

            elif key in ("s", "S"):
                s_name = self._prompt_in_pane_text("NEW SKILL", "Skill Slug Name:", "e.g. clean-architecture")
                if not s_name:
                    status_msg = "Skill creation cancelled"
                else:
                    template_lines = [
                        f"# Skill: {s_name}",
                        "- Rule 1: Write modular, idiomatic code.",
                        "- Rule 2: Ensure explicit error handling and input validation.",
                        "- Rule 3: Verify edge cases with comprehensive tests.",
                    ]
                    def skill_preview(curr_lines: List[str]) -> tuple[str, List[Any]]:
                        sk_text = "\n".join(curr_lines).strip()
                        blocks = [
                            f"[bold {self.colors['grad_start']}]Skill Markdown Preview[/]",
                            Rule(style="dim"),
                            f"[bold]Skill Identifier:[/] @{s_name}",
                            f"[bold]Target File:[/]      skills/{s_name}/SKILL.md",
                            "",
                            Markdown(sk_text),
                            Rule(style="dim"),
                            "[dim]Press ^O or F2 to write/save. Press ^X or Esc to cancel.[/]",
                        ]
                        return f"Skill: @{s_name}", blocks

                    res_lines = self._run_in_pane_editor(
                        title="NEW SKILL",
                        filename=f"skills/{s_name}/SKILL.md",
                        initial_lines=template_lines,
                        preview_renderer=skill_preview,
                        footer_hint="Compose skill instructions in Markdown",
                    )
                    if res_lines is None:
                        status_msg = "Skill creation cancelled"
                    else:
                        body = "\n".join(res_lines).strip()
                        target_dir = Path.cwd() / "skills" / s_name
                        target_dir.mkdir(parents=True, exist_ok=True)
                        fm = f"---\nname: {s_name}\ndescription: {s_name} skill\n---\n\n"
                        (target_dir / "SKILL.md").write_text(fm + body + "\n")
                        if s_name not in self.config.active_skills:
                            self.config.active_skills.append(s_name)
                            self.config.save()
                        status_msg = f"Created & activated Claude skill '@{s_name}'"
            elif key in ("e", "E") and sel["kind"] == "agent":
                self.edit_agent_endpoint_wizard(sel["obj"])
                status_msg = f"Updated endpoint for {sel['name']}"
            elif key in ("ENTER", " "):
                if sel["kind"] in ("agent", "workflow"):
                    self.config.active_mode = sel["kind"]
                    self.config.active_name = sel["name"]
                    self.config.save()
                    self.messages.clear()
                    status_msg = f"Activated {sel['kind']}:{sel['name']}"
                elif sel["kind"] == "skill":
                    if sel["name"] in self.config.active_skills:
                        self.config.active_skills.remove(sel["name"])
                        status_msg = f"Disabled @{sel['name']}"
                    else:
                        self.config.active_skills.append(sel["name"])
                        status_msg = f"Enabled @{sel['name']}"
                    self.config.save()
                elif sel["kind"] == "plugin":
                    pslug = sel["name"]
                    if pslug in self.config.disabled_plugins:
                        self.config.disabled_plugins.remove(pslug)
                        status_msg = f"Enabled plugin {pslug}"
                    else:
                        self.config.disabled_plugins.append(pslug)
                        status_msg = f"Disabled plugin {pslug}"
                    self.config.save()
                    PLUGIN_REGISTRY.reload(self.config.disabled_plugins)

    # --------------------------------------------------------------------------
    # 8. ADVANCED TAB (Reasoning Traces, Context Budget & Active Prompt Inspector)
    # --------------------------------------------------------------------------
    def run_advanced_tab(self) -> None:
        view_idx = 0
        scroll = 0
        status_msg = ""
        views = [
            ("1 · Reasoning Traces", "THOUGHTS"),
            ("2 · Context & Tokens", "BUDGET"),
            ("3 · Active Prompt & Tools", "SCHEMA"),
            ("4 · GPU / CPU / RAM Usage", "SYSTEM"),
        ]

        while self.active_tab == "advanced":
            total_tok = self.prompt_tokens + self.completion_tokens
            ctx_limit = max(1, self.config.num_ctx)
            pct = min(100, int((total_tok / ctx_limit) * 100))

            left_rows = [(lbl, i == view_idx, bdg) for i, (lbl, bdg) in enumerate(views)]
            right_blocks: List[Any] = []

            if view_idx == 0:
                right_blocks.append(f"[bold {self.colors['grad_start']}]Captured Reasoning & Thought Blocks[/] [dim]({len(self.thoughts)} traces)[/]")
                right_blocks.append(Rule(style="dim"))
                if not self.thoughts:
                    right_blocks.append("[dim]No <think> or reasoning_content blocks captured in this session yet.[/]")
                    right_blocks.append("[dim]Models like DeepSeek-R1, Qwen3, and Gemma 4 populate traces automatically.[/]")
                else:
                    shown = list(reversed(self.thoughts))[scroll : scroll + 4]
                    for i, th in enumerate(shown, scroll + 1):
                        lines = th.splitlines()
                        hdr = lines[0] if lines else f"Trace #{i}"
                        body = "\n".join(lines[1:]) if len(lines) > 1 else ""
                        right_blocks.append(f"[bold {self.colors['grad_end']}]#{i} {hdr}[/]")
                        right_blocks.append(Panel(body[:900], box=ROUNDED, border_style="dim"))
            elif view_idx == 1:
                right_blocks.append(f"[bold {self.colors['grad_start']}]Context Window & Token Allocation[/]")
                right_blocks.append(Rule(style="dim"))
                bar_w = 32
                filled = min(bar_w, int((total_tok / ctx_limit) * bar_w))
                bar_str = ("█" * filled) + ("░" * (bar_w - filled))
                right_blocks.append(f"[bold]Window Usage:[/] [{self.colors['grad_start']}]{bar_str}[/]  [bold]{total_tok:,}[/] / {ctx_limit:,} tok ({pct}%)")
                right_blocks.append(f"[bold]Prompt Tokens:[/] {self.prompt_tokens:,}    [bold]Completion Tokens:[/] {self.completion_tokens:,}")
                right_blocks.append("")
                sys_est = len(self.messages[0]["content"]) // 4 if self.messages and self.messages[0].get("role") == "system" else 0
                tool_est = sum(len(str(m.get("content", ""))) // 4 for m in self.messages if m.get("role") == "tool")
                chat_est = sum(len(str(m.get("content", ""))) // 4 for m in self.messages if m.get("role") in ("user", "assistant"))
                for seg_name, est in (("System + Skills + AGENTS.md", sys_est), ("Conversation Turns", chat_est), ("Tool Read/Shell Outputs", tool_est)):
                    right_blocks.append(f"  [dim]•[/] {seg_name:<28} [bold]~{est:,} tok[/]")
                right_blocks.append("")
                right_blocks.append("[dim]Tip: Run /compact in Prompt tab to summarize older turns when ctx > 75%.[/]")
            elif view_idx == 2:
                agents, _ = load_agents_and_workflows()
                ag = agents.get(self.config.active_name) or AgentDef("default", "", "")
                sys_txt = self.build_system_prompt(ag, [])
                right_blocks.append(f"[bold {self.colors['grad_start']}]Active System Prompt & Registered Tools[/]")
                right_blocks.append(Rule(style="dim"))
                tool_names = [t["function"]["name"] for t in get_all_tool_definitions()]
                right_blocks.append(f"[bold]Tools ({len(tool_names)}):[/] [dim]{', '.join(tool_names)}[/]")
                right_blocks.append("")
                sys_lines = sys_txt.splitlines()[scroll : scroll + 16]
                right_blocks.append(Panel("\n".join(sys_lines), box=ROUNDED, border_style="dim"))
            else:
                # view_idx == 3: GPU / CPU / RAM Usage
                hw = get_system_hardware_info()
                right_blocks.append(f"[bold {self.colors['grad_start']}]System Hardware & Resource Telemetry[/]")
                right_blocks.append(Rule(style="dim"))

                # CPU
                cpu_pct = hw["cpu_percent"]
                c_filled = min(25, int(cpu_pct * 0.25))
                c_bar = ("█" * c_filled) + ("░" * (25 - c_filled))
                c_color = self.colors["err"] if cpu_pct > 85 else (self.colors["warn"] if cpu_pct > 60 else self.colors["ok"])
                right_blocks.append(f"[bold]CPU Utilization:[/] [{c_color}]{c_bar}[/] [bold]{cpu_pct:.1f}%[/]  [dim]({hw['cpu_count_logical']} Cores / {hw['cpu_count_physical']} Physical)[/]")
                right_blocks.append(f"[dim]Load Average: 1m={hw['load_avg'][0]:.2f}  5m={hw['load_avg'][1]:.2f}  15m={hw['load_avg'][2]:.2f}[/]")
                right_blocks.append("")

                # RAM & Swap
                ram_pct = hw["ram_percent"]
                r_filled = min(25, int(ram_pct * 0.25))
                r_bar = ("█" * r_filled) + ("░" * (25 - r_filled))
                r_color = self.colors["err"] if ram_pct > 85 else (self.colors["warn"] if ram_pct > 70 else self.colors["ok"])
                right_blocks.append(f"[bold]System RAM:[/]       [{r_color}]{r_bar}[/] [bold]{hw['ram_used_gb']:.1f} GB[/] / {hw['ram_total_gb']:.1f} GB ({ram_pct:.1f}%)")
                sw_pct = hw["swap_percent"]
                s_filled = min(25, int(sw_pct * 0.25))
                s_bar = ("█" * s_filled) + ("░" * (25 - s_filled))
                right_blocks.append(f"[bold]Swap Memory:[/]      [{self.colors['warn']}]{s_bar}[/] {hw['swap_used_gb']:.1f} GB / {hw['swap_total_gb']:.1f} GB ({sw_pct:.1f}%)")
                right_blocks.append("")

                # GPU
                right_blocks.append(f"[bold {self.colors['accent']}]GPU Acceleration & Dedicated VRAM[/]")
                if not hw["gpus"]:
                    right_blocks.append("[dim]No discrete NVIDIA/AMD GPU detected via sysfs/smi. Running CPU inference mode.[/]")
                else:
                    for g_i, gpu in enumerate(hw["gpus"], 1):
                        g_pct = gpu.get("gpu_pct", 0)
                        g_filled = min(25, int(g_pct * 0.25))
                        gpu_bar = ("█" * g_filled) + ("░" * (25 - g_filled))
                        v_used = gpu.get("vram_used_mb", 0)
                        v_total = max(1.0, gpu.get("vram_total_mb", 1))
                        v_pct = min(100.0, (v_used / v_total) * 100)
                        v_filled = min(25, int(v_pct * 0.25))
                        v_bar = ("█" * v_filled) + ("░" * (25 - v_filled))
                        right_blocks.append(f"[bold]GPU #{g_i}:[/] [bold {self.colors['grad_end']}]{gpu['name']}[/]")
                        right_blocks.append(f"  Activity:  [{self.colors['ok']}]{gpu_bar}[/] [bold]{g_pct}%[/]")
                        right_blocks.append(f"  VRAM:      [{self.colors['grad_start']}]{v_bar}[/] [bold]{v_used:,.0f} MB[/] / {v_total:,.0f} MB ({v_pct:.1f}%)")
                right_blocks.append("")

                # LLM Processes
                right_blocks.append(f"[bold {self.colors['grad_start']}]Active Local Inference Processes[/]")
                if not hw["llm_processes"]:
                    right_blocks.append("[dim]No local inference process (ollama, llama-server, vllm) running on localhost.[/]")
                else:
                    for lp in hw["llm_processes"]:
                        right_blocks.append(f"  • [bold]{lp['name']}[/] (PID: {lp['pid']}) — Memory: [bold]{lp['mem_mb']:.1f} MB[/]")

            self._ui_render_split_frame(
                "ADVANCED DIAGNOSTICS & CONTEXT",
                f"ctx: {total_tok:,}/{ctx_limit:,} ({pct}%) · {self.config.provider}/{self.config.model}",
                left_rows,
                right_blocks,
                [("1..4", "view"), ("↑/↓", "select"), ("j/k", "scroll"), ("y", "yank trace"), ("c", "clear traces"), ("←/→", "tabs")],
                status_banner=status_msg,
            )
            status_msg = ""

            key = read_key()
            if key == "RESIZE":
                continue
            if key == "LEFT":
                self.switch_tab(-1)
            elif key == "RIGHT":
                self.switch_tab(1)
            elif key in ("1", "2", "3", "4"):
                view_idx = int(key) - 1
                scroll = 0
            elif key == "UP":
                view_idx = (view_idx - 1) % len(views)
                scroll = 0
            elif key == "DOWN":
                view_idx = (view_idx + 1) % len(views)
                scroll = 0
            elif key == "ENTER":
                scroll = 0
            elif key in ("j", "J", "PGDN", "WHEEL_DOWN"):
                scroll += 1
            elif key in ("k", "K", "PGUP", "WHEEL_UP"):
                scroll = max(0, scroll - 1)
            elif key in ("y", "Y") and self.thoughts:
                ok, info = copy_to_clipboard(self.thoughts[-1])
                status_msg = f"Copied latest thought via {info}" if ok else f"Copy failed: {info}"
            elif key in ("c", "C"):
                self.thoughts.clear()
                scroll = 0
                status_msg = "Cleared reasoning traces"

    # --------------------------------------------------------------------------
    # 9. HISTORY TAB (Saved Sessions & Action Audit Inspector)
    # --------------------------------------------------------------------------
    def run_history_tab(self) -> None:
        mode = "sessions"
        idx = 0
        filter_q = ""
        filtering = False
        status_msg = ""

        while self.active_tab == "history":
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            sess_files = sorted(SESSIONS_DIR.glob("*.json"), reverse=True)[:60]
            sessions_data: List[Dict[str, Any]] = []
            q_low = filter_q.strip().lower()
            for sf in sess_files:
                try:
                    d = json.loads(sf.read_text())
                    d["_path"] = sf
                    if q_low:
                        hay = f"{d.get('id', '')} {d.get('title', '')} " + " ".join(
                            str(m.get("content", "")) for m in d.get("messages", [])
                        )
                        if q_low not in hay.lower():
                            continue
                    sessions_data.append(d)
                except Exception:
                    pass

            audit_items = list(reversed(get_history()[-40:]))

            left_rows: List[tuple[str, bool, str]] = []
            right_blocks: List[Any] = []

            if mode == "sessions":
                if not sessions_data:
                    left_rows.append(("(no saved sessions yet)", True, "EMPTY"))
                    right_blocks.append("[dim]Start chatting in the PROMPT tab — sessions auto-save after each turn.[/]")
                else:
                    idx = idx % len(sessions_data)
                    for i, sd in enumerate(sessions_data):
                        is_cur = sd.get("id") == self.session_id
                        left_rows.append((sd.get("title", "untitled"), i == idx, "ACTIVE" if is_cur else f"{len(sd.get('messages', []))}m"))

                    sel_s = sessions_data[idx]
                    msgs = sel_s.get("messages", [])
                    right_blocks.append(f"[bold {self.colors['grad_start']}]{sel_s.get('title', 'Session')}[/]")
                    right_blocks.append(
                        f"[dim]ID: {sel_s.get('id')} · Updated: {sel_s.get('updated', '')} · "
                        f"Tokens: {sel_s.get('prompt_tokens', 0) + sel_s.get('completion_tokens', 0):,}[/]"
                    )
                    right_blocks.append(Rule(style="dim"))
                    for m in [x for x in msgs if x.get("role") in ("user", "assistant") and x.get("content")][:8]:
                        r_lbl = ">" if m["role"] == "user" else "AI"
                        r_col = self.colors["grad_start"] if m["role"] == "user" else self.colors["grad_end"]
                        snippet = str(m["content"]).strip().splitlines()[0][:85]
                        right_blocks.append(f"[bold {r_col}]{r_lbl}[/] {snippet}")
            else:
                if not audit_items:
                    left_rows.append(("(no tool actions logged)", True, "EMPTY"))
                    right_blocks.append("[dim]File edits, writes, and shell executions are audited here.[/]")
                else:
                    idx = idx % len(audit_items)
                    for i, it in enumerate(audit_items):
                        st = it.get("status", "INFO")
                        left_rows.append((it.get("cmd", ""), i == idx, st))

                    sel_a = audit_items[idx]
                    st = sel_a.get("status", "")
                    st_col = self.colors["ok"] if st in ("APPROVED", "AUTO") else self.colors["err"]
                    right_blocks.append(f"[bold]{sel_a.get('cmd', '')}[/]  [bold {st_col}][{st}][/]")
                    right_blocks.append(f"[dim]Time: {sel_a.get('time', '')} · Agent: {sel_a.get('agent', 'default')}[/]")
                    right_blocks.append(Rule(style="dim"))
                    right_blocks.append(f"[bold]Always-allowed prefixes:[/] [dim]{', '.join(self.config.always_allow) or 'none'}[/]")
                    right_blocks.append(f"[bold]Session Checkpoints:[/] {len(self.checkpoints)} saved")
                    if sel_a.get("out"):
                        right_blocks.append("")
                        right_blocks.append(Panel(sel_a["out"][:900], box=ROUNDED, border_style="dim"))

            mode_badge = (
                f"filter: /{filter_q}_"
                if filtering
                else (
                    f"VIEW: SESSIONS{f' (filter: {filter_q})' if filter_q else ''} · '/' search"
                    if mode == "sessions"
                    else "VIEW: AUDIT LOG (press 's' for Sessions)"
                )
            )
            self._ui_render_split_frame(
                "SESSION & AUDIT HISTORY",
                mode_badge,
                left_rows,
                right_blocks,
                [("s/a", "sessions/audit"), ("/", "search"), ("↑/↓", "nav"), ("Enter", "resume"), ("e", "export .md"), ("u", "rewind cp"), ("←/→", "tabs")],
                status_banner=status_msg,
            )
            status_msg = ""

            key = read_key()
            if key == "RESIZE":
                continue
            if filtering:
                if key in ("ESC", "ENTER"):
                    filtering = False
                    if key == "ESC":
                        filter_q = ""
                elif key == "BACKSPACE":
                    filter_q = filter_q[:-1]
                    idx = 0
                elif key.startswith("PASTE:"):
                    filter_q += key[6:].replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
                    idx = 0
                elif len(key) == 1 and key.isprintable():
                    filter_q += key
                    idx = 0
                continue
            if key == "/" and mode == "sessions":
                filtering = True
                idx = 0
                continue
            if key == "RESIZE":
                continue
            if key == "LEFT":
                self.switch_tab(-1)
            elif key == "RIGHT":
                self.switch_tab(1)
            elif key in ("s", "S"):
                mode = "sessions"
                idx = 0
            elif key in ("a", "A"):
                mode = "audit"
                idx = 0
            elif key == "UP":
                idx = max(0, idx - 1)
            elif key == "DOWN":
                idx += 1
            elif key in ("u", "U"):
                self.rewind_checkpoint()
                self.active_tab = "prompt"
                return
            elif key in ("e", "E") and mode == "sessions" and sessions_data:
                sel_s = sessions_data[idx % len(sessions_data)]
                out_p = Path.cwd() / f"{sel_s.get('id', 'session')}.md"
                lines = [f"# {sel_s.get('title', 'Thule Session')}\n"]
                for m in sel_s.get("messages", []):
                    if m.get("role") in ("user", "assistant") and m.get("content"):
                        lines.append(f"## {m['role'].capitalize()}\n\n{m['content']}\n")
                out_p.write_text("\n".join(lines))
                status_msg = f"Exported to {out_p.name}"
            elif key == "ENTER" and mode == "sessions" and sessions_data:
                chosen = sessions_data[idx % len(sessions_data)]
                self.session_id = chosen.get("id", self.session_id)
                self.messages = chosen.get("messages", [])
                self.prompt_tokens = chosen.get("prompt_tokens", 0)
                self.completion_tokens = chosen.get("completion_tokens", 0)
                self.chat_items.clear()
                self.scroll_offset = 0
                for m in self.messages:
                    role = m.get("role")
                    content = (m.get("content") or "").strip()
                    if role == "user" and content:
                        if self.chat_items:
                            self.chat_items.append(Rule(style="dim"))
                        self.chat_items.append(f"[{self.colors['grad_start']}]>[/] {content}")
                    elif role == "assistant" and content:
                        self.chat_items.append(f"[{self.colors['grad_start']}]{self.config.active_name}:[/]")
                        self.chat_items.append(Markdown(content))
                self.active_tab = "prompt"
                return

    # --------------------------------------------------------------------------
    # 10. SETTINGS TAB (Unified Split-Pane Config & Endpoint Probe)
    # --------------------------------------------------------------------------
    def run_settings_tab(self) -> None:
        idx = 0
        probe_status = "idle (press 'p' to probe endpoint)"
        status_msg = ""

        while self.active_tab == "settings":
            themes = load_themes()
            theme_names = list(themes.keys())
            if self.config.theme not in theme_names:
                self.config.theme = "default"

            items: List[Dict[str, Any]] = [
                {"key": "theme", "label": "Visual Theme", "val": self.config.theme, "default": "default", "desc": f"Cycles color & gradient palettes from {THEMES_DIR}."},
                {"key": "provider", "label": "Default Provider", "val": self.config.provider, "default": "ollama", "desc": "Backend adapter (ollama, llamacpp, lmstudio, openrouter, gemini, openai)."},
                {"key": "base_url", "label": "Base URL", "val": self.config.base_url, "default": "http://localhost:11434/v1", "desc": "OpenAI-compatible /v1 endpoint URL (bridged to native /api/chat for Ollama)."},
                {"key": "model", "label": "Default Model", "val": self.config.model, "default": "qwen3:8b", "desc": "Model identifier sent on requests unless overridden by the active agent."},
                {"key": "api_key", "label": "API Key", "val": "***" if self.config.api_key else "(none)", "default": "(none)", "desc": "Bearer token for cloud APIs (leave empty for local Ollama / llama.cpp)."},
                {"key": "stream", "label": "Stream Responses", "val": "ON" if self.config.stream else "OFF", "default": "ON", "desc": "Stream tokens live to the terminal viewport as they are generated."},
                {"key": "auto_approve", "label": "Auto-Approve File Edits", "val": "ON" if self.config.auto_approve else "OFF", "default": "OFF", "desc": "Skip confirmation prompts for edit_file/write_file (shell commands always prompt)."},
                {"key": "auto_commit", "label": "Git Auto-Commit Edits", "val": "ON" if self.config.auto_commit else "OFF", "default": "OFF", "desc": "Automatically create a local git commit after each approved file edit."},
                {"key": "auto_docs_skills", "label": "AI Docs & Skills Lookup", "val": "ON" if self.config.auto_docs_skills else "OFF", "default": "ON", "desc": "Allow the AI to check back at documentation and Skills/ on demand during tasks."},
                {"key": "temperature", "label": "Temperature", "val": str(self.config.temperature), "default": "0.2", "desc": "Sampling temperature (0.0 = deterministic coding, higher = more creative)."},
                {"key": "num_ctx", "label": "Context Window (num_ctx)", "val": str(self.config.num_ctx), "default": "32768", "desc": "Context window size in tokens. Keep >= 32768 when reading multi-hundred-line files."},
                {"key": "max_tokens", "label": "Max Output Tokens", "val": str(self.config.max_tokens), "default": "4096", "desc": "Maximum completion tokens per assistant turn."},
                {"key": "command_timeout", "label": "Shell Command Timeout (s)", "val": f"{self.config.command_timeout}s", "default": "60s", "desc": "Maximum seconds a shell tool command may run before being terminated."},
                {"key": "connect", "label": "Launch Endpoint Wizard", "val": "/connect", "default": "wizard", "desc": "Re-open the interactive backend discovery and model picker screen."},
                {"key": "clear_allow", "label": "Reset Always-Allow List", "val": f"{len(self.config.always_allow)} rules", "default": "0 rules", "desc": "Clear persisted workspace tool/command prefix approvals."},
            ]

            idx = idx % len(items)
            cur = items[idx]

            left_rows = [(it["label"], i == idx, it["val"]) for i, it in enumerate(items)]
            right_blocks: List[Any] = [
                f"[bold {self.colors['grad_start']}]{cur['label']}[/]  [dim](config.{cur['key']})[/]",
                Rule(style="dim"),
                f"[bold]Current Value:[/]  [{self.colors['grad_end']}]{cur['val']}[/]",
                f"[bold]Default Value:[/]  [dim]{cur['default']}[/]",
                "",
                f"[bold {self.colors['accent']}]Description[/]",
                cur["desc"],
                "",
                Rule(style="dim"),
                f"[bold]Endpoint Probe:[/] {probe_status}",
                f"[bold]Config File:[/]    [dim]{CONFIG_FILE}[/]",
            ]

            self._ui_render_split_frame(
                "CONFIGURATION & RUNTIME SETTINGS",
                f"{self.config.provider} @ {self.config.base_url}",
                left_rows,
                right_blocks,
                [("↑/↓", "nav"), ("Space/Enter", "edit/toggle"), ("p", "probe endpoint"), ("←/→", "tabs")],
                status_banner=status_msg,
            )
            status_msg = ""

            key = read_key()
            if key == "RESIZE":
                continue
            if key == "LEFT":
                self.switch_tab(-1)
            elif key == "RIGHT":
                self.switch_tab(1)
            elif key == "UP":
                idx = (idx - 1) % len(items)
            elif key == "DOWN":
                idx = (idx + 1) % len(items)
            elif key in ("p", "P"):
                ok, mods, msg = self._fetch_models_for(self.config.base_url, self.config.api_key)
                probe_status = f"[{self.colors['ok'] if ok else self.colors['err']}]{msg}[/]"
                status_msg = msg
            elif key in ("ENTER", " "):
                k = cur["key"]
                if k == "theme":
                    self.config.theme = theme_names[(theme_names.index(self.config.theme) + 1) % len(theme_names)]
                elif k == "provider":
                    provs = ["ollama", "llamacpp", "lmstudio", "openrouter", "gemini", "openai"]
                    cur_i = provs.index(self.config.provider) if self.config.provider in provs else 0
                    self.config.provider = provs[(cur_i + 1) % len(provs)]
                elif k == "base_url":
                    val = self._prompt_line_field("Base URL", self.config.base_url)
                    if val:
                        self.config.base_url = val.rstrip("/")
                elif k == "model":
                    val = self._prompt_line_field("Model", self.config.model)
                    if val:
                        self.config.model = val
                elif k == "api_key":
                    self.config.api_key = self._prompt_line_field("API Key", self.config.api_key, mask=True)
                elif k == "stream":
                    self.config.stream = not self.config.stream
                elif k == "auto_approve":
                    self.config.auto_approve = not self.config.auto_approve
                elif k == "auto_commit":
                    self.config.auto_commit = not self.config.auto_commit
                elif k == "auto_docs_skills":
                    self.config.auto_docs_skills = not self.config.auto_docs_skills
                elif k == "temperature":
                    val = self._prompt_line_field("Temperature (0.0 - 2.0)", str(self.config.temperature))
                    try:
                        self.config.temperature = float(val)
                    except ValueError:
                        pass
                elif k == "num_ctx":
                    if key == " ":
                        ladder = [8192, 16384, 32768, 65536]
                        self.config.num_ctx = ladder[(ladder.index(self.config.num_ctx) + 1) % len(ladder)] if self.config.num_ctx in ladder else 32768
                    else:
                        val = self._prompt_line_field("Context Size (num_ctx)", str(self.config.num_ctx))
                        try:
                            self.config.num_ctx = int(val)
                        except ValueError:
                            pass
                elif k == "max_tokens":
                    val = self._prompt_line_field("Max Output Tokens", str(self.config.max_tokens))
                    try:
                        self.config.max_tokens = int(val)
                    except ValueError:
                        pass
                elif k == "command_timeout":
                    val = self._prompt_line_field("Shell Command Timeout (seconds)", str(self.config.command_timeout))
                    try:
                        self.config.command_timeout = max(5, int(val))
                    except ValueError:
                        pass
                elif k == "connect":
                    self.run_setup()
                elif k == "clear_allow":
                    self.config.always_allow.clear()
                    status_msg = "Cleared always-allow rules"
                self.config.save()

    def run_plugin_tab(self, tab_name: str) -> None:
        status_msg = ""
        while self.active_tab == tab_name:
            spec = PLUGIN_REGISTRY.tabs.get(tab_name)
            if spec is None:
                self.active_tab = "prompt"
                return

            left_rows: List[tuple[str, bool, str]] = []
            right_blocks: List[Any] = []
            subtitle = f"plugin:{spec.plugin}"
            keycaps: List[tuple[str, str]] = [("←/→", "tabs"), ("r", "reload plugin")]

            try:
                res = spec.render_fn(self) if spec.render_fn.__code__.co_argcount >= 1 else spec.render_fn()
                if isinstance(res, dict):
                    subtitle = str(res.get("subtitle", subtitle))
                    left_rows = list(res.get("left_rows") or [])
                    right_blocks = list(res.get("right_blocks") or [])
                    if res.get("keycaps"):
                        keycaps = list(res["keycaps"]) + [("←/→", "tabs")]
                elif isinstance(res, list):
                    left_rows = [(spec.title, True, "PLUGIN")]
                    right_blocks = res
                else:
                    left_rows = [(spec.title, True, "PLUGIN")]
                    right_blocks = [Markdown(str(res)) if isinstance(res, str) else res]
            except Exception as exc:
                left_rows = [(spec.title, True, "ERROR")]
                right_blocks = [f"[{self.colors['err']}]Plugin tab render error: {exc}[/]"]

            if not left_rows:
                left_rows = [(spec.title, True, "PLUGIN")]

            self._ui_render_split_frame(
                spec.title.upper(),
                subtitle,
                left_rows,
                right_blocks,
                keycaps,
                status_banner=status_msg,
            )
            status_msg = ""

            key = read_key()
            if key == "RESIZE":
                continue
            if key == "LEFT":
                self.switch_tab(-1)
            elif key == "RIGHT":
                self.switch_tab(1)
            elif key in ("r", "R") and spec.on_key is None:
                PLUGIN_REGISTRY.reload(self.config.disabled_plugins)
                status_msg = f"Reloaded plugin:{spec.plugin}"
            elif callable(spec.on_key):
                try:
                    ret = spec.on_key(key, self) if spec.on_key.__code__.co_argcount >= 2 else spec.on_key(key)
                    if isinstance(ret, str) and ret:
                        status_msg = ret
                except Exception as exc:
                    status_msg = f"Key handler error: {exc}"

    # --------------------------------------------------------------------------
    # MAIN LOOP
    # --------------------------------------------------------------------------
    def load_session_by_id(self, session_ref: str) -> bool:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(SESSIONS_DIR.glob("*.json"), reverse=True)
        if not files:
            return False
        target_file = files[0] if session_ref == "latest" else next(
            (f for f in files if f.stem == session_ref or session_ref in f.stem), None
        )
        if target_file is None or not target_file.is_file():
            return False
        chosen = json.loads(target_file.read_text())
        self.session_id = chosen.get("id", target_file.stem)
        self.messages = chosen.get("messages", [])
        self.prompt_tokens = chosen.get("prompt_tokens", 0)
        self.completion_tokens = chosen.get("completion_tokens", 0)
        self.chat_items.clear()
        self.scroll_offset = 0
        for m in self.messages:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role == "user" and content:
                if self.chat_items:
                    self.chat_items.append(Rule(style="dim"))
                self.chat_items.append(f"[{self.colors['grad_start']}]>[/] {content}")
            elif role == "assistant" and content:
                self.chat_items.append(f"[{self.colors['grad_start']}]{self.config.active_name}:[/]")
                self.chat_items.append(Markdown(content))
        return True

    async def run_headless(self, prompt_text: str) -> int:
        prev_stream = self.config.stream
        self.config.stream = False
        try:
            await self.dispatch_prompt(prompt_text)
            self.save_session()
            for m in reversed(self.messages):
                if m.get("role") == "assistant" and m.get("content"):
                    clean = re.sub(r"<think>.*?</think>", "", str(m["content"]), flags=re.DOTALL).strip()
                    print(clean)
                    return 0
            return 1
        except Exception as exc:
            print(f"thule error: {exc}", file=sys.stderr)
            return 1
        finally:
            self.config.stream = prev_stream

    def start(self, skip_setup: bool = False) -> None:
        global _TUI_ACTIVE
        _TUI_ACTIVE = True
        sys.stdout.write("\033[?1049h\033[?2004h\033[?1000h\033[?1006h\033[?25l\033[1;1H")
        sys.stdout.flush()
        try:
            if not skip_setup:
                self.run_setup()
            while True:
                if self.active_tab == "prompt":
                    self.run_prompt_tab()
                elif self.active_tab == "agents":
                    self.run_agents_tab()
                elif self.active_tab == "advanced":
                    self.run_advanced_tab()
                elif self.active_tab == "history":
                    self.run_history_tab()
                elif self.active_tab == "settings":
                    self.run_settings_tab()
                elif self.active_tab in PLUGIN_REGISTRY.tabs:
                    self.run_plugin_tab(self.active_tab)
                else:
                    self.active_tab = "prompt"
        except KeyboardInterrupt:
            pass
        finally:
            _restore_terminal_state()
            print("Exited Thule Local AI CLI.")


def run_doctor() -> int:
    print(f"Thule v{__version__} Diagnostics")
    print("=" * 42)
    ok_all = True
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        probe_f = BASE_DIR / f".doctor_{os.getpid()}.tmp"
        probe_f.write_text("ok")
        probe_f.unlink(missing_ok=True)
        print(f"[OK]   THULE_HOME writable: {BASE_DIR}")
    except Exception as exc:
        ok_all = False
        print(f"[FAIL] THULE_HOME not writable ({BASE_DIR}): {exc}")

    print(f"[OK]   Python: {sys.version.split()[0]}")
    for mod_name, req in (("rich", True), ("httpx", True), ("psutil", False)):
        try:
            __import__(mod_name)
            print(f"[OK]   Dependency '{mod_name}' installed")
        except ImportError:
            if req:
                ok_all = False
            print(f"[{'FAIL' if req else 'WARN'}] Dependency '{mod_name}' missing")

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
    ed_bin = shlex.split(editor)[0] if editor else "nano"
    print(f"[{'OK' if shutil.which(ed_bin) else 'WARN'}] $EDITOR: {editor}")
    print(f"[{'OK' if shutil.which('git') else 'WARN'}] git: {shutil.which('git') or 'not found'}")

    cfg = Config.load()
    print(f"[INFO] Active config: provider={cfg.provider} model={cfg.model} url={cfg.base_url} num_ctx={cfg.num_ctx}")
    cli = LocalAICLI()
    ok_cfg, mods_cfg, msg_cfg = cli._fetch_models_for(cfg.base_url, cfg.api_key)
    print(f"[{'OK' if ok_cfg else 'WARN'}] Configured endpoint ({cfg.base_url}): {msg_cfg}")
    if ok_cfg and mods_cfg:
        print(f"       Models: {', '.join(mods_cfg[:8])}{' ...' if len(mods_cfg) > 8 else ''}")

    for pid, pinfo in cli._probe_endpoints().items():
        if pinfo.get("online"):
            print(f"[OK]   Local probe {pid} ({pinfo.get('url')}): online ({len(pinfo.get('models') or [])} models)")

    PLUGIN_REGISTRY.reload(cfg.disabled_plugins)
    if PLUGIN_REGISTRY.errors:
        ok_all = False
        for slug, err in PLUGIN_REGISTRY.errors.items():
            print(f"[FAIL] Plugin '{slug}' load error: {err}")
    else:
        print(f"[OK]   Plugins loaded: {len(PLUGIN_REGISTRY.plugins)} (0 errors)")
    return 0 if ok_all else 1


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="thule",
        description="Thule Local AI CLI — minimalist terminal coding assistant & agent orchestrator.",
    )
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-p", "--prompt", help="Run a one-shot headless prompt without launching the TUI")
    p.add_argument("--doctor", action="store_true", help="Run environment, endpoint, and plugin diagnostics")
    p.add_argument("--provider", help="Override provider (ollama, llamacpp, lmstudio, openrouter, gemini, openai)")
    p.add_argument("--base-url", dest="base_url", help="Override OpenAI-compatible base URL")
    p.add_argument("--model", help="Override model identifier")
    p.add_argument("--agent", help="Activate a specific agent by name")
    p.add_argument("--workflow", help="Activate a specific workflow by name")
    p.add_argument("--mode", choices=["plan", "build"], help="Set execution mode (plan or build)")
    p.add_argument("--resume", metavar="SESSION_ID", help="Resume a saved session ID (or 'latest')")
    p.add_argument("--auto-approve", action="store_true", help="Auto-approve file edits")
    p.add_argument("--no-docs-skills", action="store_true", help="Disable AI on-demand docs & skills lookup tools")
    p.add_argument("--no-setup", action="store_true", help="Skip startup endpoint wizard and enter TUI directly")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.doctor:
        return run_doctor()

    cli = LocalAICLI()
    if args.provider:
        cli.config.provider = args.provider
        if not args.base_url:
            preset = next((pr for pr in PROVIDER_PRESETS if pr["id"] == args.provider), None)
            if preset:
                cli.config.base_url = preset["url"].rstrip("/")
    if args.base_url:
        cli.config.base_url = args.base_url.rstrip("/")
    if args.model:
        cli.config.model = args.model
    if args.agent or args.workflow:
        avail_agents, avail_workflows = load_agents_and_workflows()
        if args.agent:
            if args.agent not in avail_agents:
                print(
                    f"Warning: agent '{args.agent}' not found (available: {', '.join(sorted(avail_agents)) or 'none'}).",
                    file=sys.stderr,
                )
            cli.config.active_mode = "agent"
            cli.config.active_name = args.agent
        if args.workflow:
            if args.workflow not in avail_workflows:
                print(
                    f"Warning: workflow '{args.workflow}' not found (available: {', '.join(sorted(avail_workflows)) or 'none'}).",
                    file=sys.stderr,
                )
            cli.config.active_mode = "workflow"
            cli.config.active_name = args.workflow
    if args.mode:
        cli.config.execution_mode = args.mode
    if args.auto_approve:
        cli.config.auto_approve = True
    if args.no_docs_skills:
        cli.config.auto_docs_skills = False

    if args.resume and not cli.load_session_by_id(args.resume):
        print(f"Warning: session '{args.resume}' not found.", file=sys.stderr)

    piped_stdin = ""
    if not sys.stdin.isatty():
        try:
            piped_stdin = sys.stdin.read().strip()
        except Exception:
            piped_stdin = ""

    if args.prompt is not None or piped_stdin:
        full_prompt = "\n\n".join(x for x in (args.prompt, piped_stdin) if x).strip()
        return asyncio.run(cli.run_headless(full_prompt)) if full_prompt else 0

    skip_setup = bool(
        args.no_setup or args.resume or args.model or args.provider or args.base_url or args.agent or args.workflow
    )
    cli.start(skip_setup=skip_setup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
