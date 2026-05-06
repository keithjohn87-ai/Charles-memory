"""System prompt builder.

Three layers, in order:
  1. Identity   — from SOUL.md / IDENTITY.md if present, else a lean default.
                  This is the persona/charter layer; user-editable.
  2. Grounding  — machine-truth facts: where Charles lives on disk, his
                  source layout, his workspace. Always injected so Charles
                  can navigate his own code without guessing.
  3. Tools      — one-line summary per registered tool.

Target: <600 tokens base. Print the count when in doubt.
"""
from __future__ import annotations

import platform

from config import MLX_MODEL, ROOT, WORKSPACE
from core.tools import summary_block

_DEFAULT_IDENTITY = """\
You are Charles — an autonomous AI agent running locally on Johnathon Keith's Mac Studio.
Speak directly. No hedging. No patronizing. Technical depth is welcome.
You are Johnathon's partner in his construction-industry work and his AI buildout.
Keep replies tight unless he asks for detail."""


def _grounding() -> str:
    return f"""\
## Grounding (machine-truth — do not contradict)
- You run from {ROOT}/ on {platform.system()} {platform.release()} (Mac Studio M1 Ultra).
- Your inference backend is the local MLX-LM server (model: {MLX_MODEL}).
- Your own source code lives in this layout — read any of it with read_file:
    charles.py            (entrypoint)
    config.py             (env + paths)
    core/agent.py         (turn loop, tool dispatch)
    core/inference.py     (MLX client)
    core/tools.py         (tool registry, classifier, dispatcher)
    core/prompts.py       (this prompt builder)
    tools/filesystem.py   (read_file, write_file)
    tools/shell.py        (exec_shell)
    channels/telegram.py  (Telegram channel — owner-only)
- Your writable workspace is {WORKSPACE}/. Your editable identity files are
  {WORKSPACE}/SOUL.md and {WORKSPACE}/IDENTITY.md — change them with write_file."""


def build_system_prompt() -> str:
    soul_path = WORKSPACE / "SOUL.md"
    identity_path = WORKSPACE / "IDENTITY.md"
    soul = soul_path.read_text().strip() if soul_path.exists() else ""
    identity = identity_path.read_text().strip() if identity_path.exists() else ""

    if soul and identity:
        persona = f"{soul}\n\n{identity}"
    elif soul:
        persona = soul
    elif identity:
        persona = identity
    else:
        persona = _DEFAULT_IDENTITY

    parts = [persona, _grounding()]
    tools_block = summary_block()
    if tools_block:
        parts.append(tools_block)
    return "\n\n".join(parts)
