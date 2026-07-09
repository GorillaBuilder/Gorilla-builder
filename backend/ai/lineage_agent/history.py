from __future__ import annotations

from typing import Dict

# ---------------------------------------------------------------------------
# Legacy shims
# ---------------------------------------------------------------------------
_HISTORY: Dict[str, list] = {}
HISTORY_CAP = 100


def _norm_role(r: str) -> str:
    return "user" if (r or "").strip().lower() in ("user", "you") else "assistant"


def _append_history(project_id: str, role: str, content: str) -> None:
    if not project_id or not content:
        return
    _HISTORY.setdefault(project_id, []).append(
        {"role": _norm_role(role), "content": content.strip()}
    )
    if len(_HISTORY[project_id]) > HISTORY_CAP:
        _HISTORY[project_id] = _HISTORY[project_id][-HISTORY_CAP:]


def _get_history(project_id: str, max_items: int = 20) -> list:
    return list(_HISTORY.get(project_id, []))[-max_items:]


def clear_history(project_id: str) -> None:
    _HISTORY.pop(project_id, None)
