from __future__ import annotations

import re
from typing import Any, Dict, List

from .config import TOTAL_BATCH_LIMIT, WRITE_BATCH_LIMIT
from .logging_ import log_agent
from .tools import AGENT_TOOL_DEFS

# ═══════════════════════════════════════════════════════════════════════════
#  XML repair
# ═══════════════════════════════════════════════════════════════════════════

def _repair_qwen3_xml(text: str) -> str:
    if text.count("<tool_call>") > text.count("</tool_call>"):
        text = text + "\n</tool_call>"

    open_funcs  = len(re.findall(r"<function=[^>]+>", text))
    close_funcs = text.count("</function>")
    if open_funcs > close_funcs:
        diff = open_funcs - close_funcs
        if "</tool_call>" in text:
            text = text.replace("</tool_call>", "</function>" * diff + "</tool_call>", 1)
        else:
            text = text + ("</function>" * diff)

    open_params  = len(re.findall(r"<parameter=[^>]+>", text))
    close_params = text.count("</parameter>")
    if open_params > close_params:
        diff = open_params - close_params
        if "</function>" in text:
            text = text.replace("</function>", "</parameter>" * diff + "</function>", 1)
        else:
            text = text + ("</parameter>" * diff)

    return text


# ═══════════════════════════════════════════════════════════════════════════
#  XML tool-call parser
# ═══════════════════════════════════════════════════════════════════════════

_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>([\s\S]*?)</tool_call>")
_FUNCTION_RE        = re.compile(r"<function=([^>\s]+)>([\s\S]*?)</function>")
_PARAMETER_RE       = re.compile(r"<parameter=([^>\s]+)>([\s\S]*?)</parameter>")

_TOOL_CATEGORY = {t["name"]: t.get("category", "exec") for t in AGENT_TOOL_DEFS}


def _strip_param_value(raw: str) -> str:
    if not raw:
        return raw
    if raw.startswith("\r\n"):
        raw = raw[2:]
    elif raw.startswith("\n"):
        raw = raw[1:]
    if raw.endswith("\r\n"):
        raw = raw[:-2]
    elif raw.endswith("\n"):
        raw = raw[:-1]
    return raw


def _parse_xml_functions(block_inner: str) -> List[Dict[str, Any]]:
    functions: List[Dict[str, Any]] = []
    for m in _FUNCTION_RE.finditer(block_inner):
        name   = m.group(1).strip()
        body   = m.group(2)
        params: Dict[str, str] = {}
        for pm in _PARAMETER_RE.finditer(body):
            pname = pm.group(1).strip()
            pval  = _strip_param_value(pm.group(2))
            params[pname] = pval
        functions.append({"name": name, "params": params})
    return functions


def _parse_response(raw_text: str) -> Dict[str, Any]:
    # In _parse_response, update the result dict initialization:
    result = {
        "thought":              "",
        "write_files":          [],
        "edit_files":           [],
        "read_calls":           [],
        "bash":                 "",
        "done":                 False,
        "message":              "",
        "extra_writes_dropped": 0,
        "user_action":          None,   # ← ADD THIS
        "spawn_subagent":       None,
    }

    if not raw_text:
        return result

    repaired = _repair_qwen3_xml(raw_text)
    blocks   = _TOOL_CALL_BLOCK_RE.findall(repaired)

    if blocks:
        first_pos = repaired.find("<tool_call>")
        result["thought"] = repaired[:first_pos].strip() if first_pos > 0 else ""

        bash_parts:    List[str] = []
        all_functions: List[Dict[str, Any]] = []
        for block_inner in blocks:
            all_functions.extend(_parse_xml_functions(block_inner))

        if len(all_functions) > TOTAL_BATCH_LIMIT:
            log_agent("agent", f"⚠ Capping {len(all_functions)} functions at {TOTAL_BATCH_LIMIT}")
            all_functions = all_functions[:TOTAL_BATCH_LIMIT]

        write_count = 0
        for fn in all_functions:
            n = fn["name"]
            p = fn["params"]

            if n == "write_file":
                if write_count < WRITE_BATCH_LIMIT:
                    result["write_files"].append({
                        "path":    p.get("path", "").strip(),
                        "content": p.get("content", ""),
                        "reason":  p.get("reason", "").strip(),
                    })
                    write_count += 1
                else:
                    result["extra_writes_dropped"] += 1

            elif n == "edit_file":
                if write_count < WRITE_BATCH_LIMIT:
                    result["edit_files"].append({
                        "path":    p.get("path", "").strip(),
                        "old_str": p.get("old_str", ""),
                        "new_str": p.get("new_str", ""),
                    })
                    write_count += 1
                else:
                    result["extra_writes_dropped"] += 1

            elif n == "run_bash":
                cmd = p.get("command", "").strip()
                if cmd:
                    bash_parts.append(cmd)

            elif n == "mark_done":
                result["done"]    = True
                result["message"] = p.get("summary", "Done.").strip()

            elif n == "connect_supabase":
                result["user_action"] = {
                    "type": "connect_supabase",
                    "reason": p.get("reason", "Database connection required.").strip(),
                }

            elif n == "set_env_vars":
                raw_vars = p.get("vars", "")
                var_names = [v.strip() for v in raw_vars.replace("\n", ",").split(",") if v.strip()]
                result["user_action"] = {
                    "type": "set_env_vars",
                    "vars": var_names,
                    "reason": p.get("reason", "Environment variables required.").strip(),
                }
            elif n in ("read_files", "list_dir", "grep_search", "glob_files",
                       "web_search", "web_fetch", "preview_screenshot"):
                result["read_calls"].append({"tool": n, "params": p})

            elif n == "spawn_subagent":
                result["spawn_subagent"] = {
                    "task":      p.get("task", "").strip(),
                    "max_turns": p.get("max_turns", "").strip(),
                }

            else:
                log_agent("agent", f"Unknown tool: {n}")

        result["bash"] = "\n\n".join(bash_parts)

        if not result["message"]:
            if result["thought"]:
                result["message"] = result["thought"].split("\n")[0][:30000]
            else:
                paths = (
                    [w["path"] for w in result["write_files"]] +
                    [e["path"] for e in result["edit_files"]]
                )
                if paths:
                    result["message"] = f"Working on {', '.join(paths)}"
                elif result["read_calls"]:
                    result["message"] = "Exploring the codebase..."
                elif result["bash"]:
                    result["message"] = ""

        return result

    if "GORILLA_DONE" in raw_text:
        result["done"] = True
        parts   = raw_text.split("GORILLA_DONE", 1)
        body    = parts[0]
        summary = parts[1].strip() if len(parts) > 1 else ""
        bash_blocks = re.findall(r"```(?:bash|sh|shell)?\n([\s\S]*?)```", body)
        result["bash"]    = "\n\n".join(b.strip() for b in bash_blocks if b.strip())
        result["thought"] = body[:body.find("```")].strip() if "```" in body else body.strip()
        result["message"] = summary or result["thought"].split("\n")[0][:30000] or "Done."
        return result

    bash_blocks = re.findall(r"```(?:bash|sh|shell)?\n([\s\S]*?)```", raw_text)
    if bash_blocks:
        result["bash"]    = "\n\n".join(b.strip() for b in bash_blocks if b.strip())
        first_pos         = raw_text.find("```")
        result["thought"] = raw_text[:first_pos].strip() if first_pos > 0 else ""
    else:
        result["thought"] = raw_text.strip()

    if result["thought"]:
        sentences = re.split(r"(?<=[.!?])\s+", result["thought"])
        result["message"] = " ".join(sentences[:3])[:30000]

    return result


# ═══════════════════════════════════════════════════════════════════════════
#  Shell safety
# ═══════════════════════════════════════════════════════════════════════════

_DANGEROUS = [
    r"\brm\s+-rf\s+/($|\s)",
    r"\bsudo\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r">\s*/dev/(sda|nvme|hda)",
    r"\bmkfs\b",
    r":\(\)\s*{\s*:\|:",
    r"\bdd\s+if=.*\s+of=/dev/",
]


def _is_safe(cmd: str) -> bool:
    return not any(re.search(p, cmd, re.IGNORECASE) for p in _DANGEROUS)
