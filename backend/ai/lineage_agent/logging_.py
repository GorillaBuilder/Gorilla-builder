from __future__ import annotations

import time

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_external_log_callback = None


def set_log_callback(cb):
    global _external_log_callback
    _external_log_callback = cb


def log_agent(role: str, message: str, project_id: str = "") -> None:
    prefix = f"[{project_id[:8]}]" if project_id else "[AGENT]"
    ts = time.strftime("%H:%M:%S")
    c = {
        "agent":    "\033[94m",
        "llm":      "\033[90m",
        "system":   "\033[97m",
        "debugger": "\033[91m",
    }.get(role.lower(), "\033[94m")
    print(
        f"\033[90m{ts}\033[0m {prefix} {c}{role.upper()}\033[0m: "
        f"{message[:300]}{'...' if len(message) > 300 else ''}"
    )
    if _external_log_callback and project_id and role.lower() != "llm":
        try:
            _external_log_callback(project_id, role.lower(), message)
        except Exception:
            pass


def _render_token_limit_message() -> str:
    return (
        '<div style="display:flex;flex-direction:column;align-items:center;'
        'justify-content:center;padding:40px 30px;'
        'background:linear-gradient(135deg,rgba(15,23,42,0.9),rgba(30,10,50,0.8));'
        'border:1px solid rgba(217,70,239,0.3);border-radius:20px;'
        'text-align:center;max-width:400px;margin:20px auto;'
        'box-shadow:0 20px 60px rgba(0,0,0,0.5);">'
        '<h2 style="color:#fff;font-size:24px;font-weight:700;margin:0 0 12px;">'
        "Token Limit Reached</h2>"
        '<p style="color:#94a3b8;font-size:14px;line-height:1.6;margin:0 0 28px;'
        'max-width:280px;">Upgrade to Premium for unlimited access.</p>'
        '<a href="/pricing" style="background:linear-gradient(135deg,#d946ef,#a855f7);'
        'color:white;text-decoration:none;padding:14px 32px;border-radius:12px;'
        'font-size:14px;font-weight:600;">Upgrade to Premium</a></div>'
    )
