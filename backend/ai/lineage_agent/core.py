from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import (
    DEEP_THINK_MODEL,
    MODE_INSTRUCTIONS,
    VISION_MODEL,
    WRITE_BATCH_LIMIT,
    _matches_deep_think_trigger,
)
from .context import _filter_observation
from .history import _append_history
from .llm import _call_llm, _pick_model
from .logging_ import log_agent
from .parsing import _is_safe, _parse_response
from .prompts import (
    DEBUG_ADDON,
    SUPABASE_ADDON,
    SYSTEM_PROMPT_BODY,
    _build_skills_block,
    expand_prompt,
    generate_plan,
)
from .token_substitution import TokenSubstitution
from .tools import _format_tools_for_prompt

# ═══════════════════════════════════════════════════════════════════════════
#  LineageAgent v18.1
# ═══════════════════════════════════════════════════════════════════════════

class LineageAgent:
    def __init__(self, project_id: str):
        self.project_id         = project_id
        self.total_tokens       = 0
        self.token_sub          = TokenSubstitution()
        self.messages:          List[Dict[str, Any]] = []
        self._system_prompt_set = False
        self._plan_injected     = False
        self._prompt_expanded   = False
        self._gorilla_proxy_url: str  = ""
        self._has_supabase:      bool = False
        # Once a request contains the DEEP_THINK_TRIGGER phrase, every turn
        # of this agent instance routes to DEEP_THINK_MODEL. Sticky so
        # multi-turn tool loops don't drop back to the base model.
        self._deep_think:        bool = False
        self._mode_instruction_injected: bool = False

    def _ensure_system_prompt(
        self,
        gorilla_proxy_url: str,
        has_supabase: bool,
        is_debug: bool,
        agent_skills: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self._system_prompt_set and has_supabase == self._has_supabase:
            return

        prompt = SYSTEM_PROMPT_BODY.replace(
            "{GORILLA_PROXY}",
            gorilla_proxy_url or "https://your-proxy.ngrok-free.dev",
        )

        # explicitly declare the supabase state for the agent's logic rules
        if has_supabase:
            prompt += "\n" + SUPABASE_ADDON
            prompt += "\n\n**CONTEXT STATE:** `has_supabase` is currently True."
        else:
            prompt += "\n\n**CONTEXT STATE:** `has_supabase` is currently False. (No database is connected)."

        if is_debug:
            prompt += "\n" + DEBUG_ADDON

        prompt += "\n\n" + _format_tools_for_prompt()

        skills_block = _build_skills_block(agent_skills)
        if skills_block:
            prompt += skills_block
            log_agent(
                "agent",
                f"Skills injected: {[k for k, v in (agent_skills or {}).items() if v]}",
                self.project_id,
            )

        if self._system_prompt_set:
            log_agent("agent", f"System prompt rebuilt — has_supabase changed to {has_supabase}", self.project_id)
            if self.messages and self.messages[0].get("role") == "system":
                self.messages[0] = {"role": "system", "content": prompt}
            else:
                self.messages.insert(0, {"role": "system", "content": prompt})
        else:
            self.messages = [{"role": "system", "content": prompt}]

        self._has_supabase      = has_supabase
        self._system_prompt_set = True

    def _extract_prompt_image(self, file_tree: Dict[str, str]) -> Optional[str]:
        raw = file_tree.get(".gorilla/prompt_image.b64") or file_tree.get("prompt_image.b64")
        if not raw:
            return None
        stripped = raw.strip()
        return stripped if stripped.startswith("data:") else f"data:image/jpeg;base64,{stripped}"

    def _append_observation(
        self,
        write_paths:      List[str],
        edit_paths:       List[str],
        read_observation: str,
        bash_observation: str,
    ) -> None:
        parts: List[str] = []
        if write_paths:
            parts.append("Files written: " + ", ".join(write_paths))
        if edit_paths:
            parts.append("Files edited: " + ", ".join(edit_paths))
        if read_observation:
            parts.append(read_observation[:10000])
        if bash_observation:
            filtered = _filter_observation(bash_observation)[:8000]
            parts.append("Bash output:\n" + filtered)
        if not parts:
            parts.append("(no output)")

        self.messages.append({
            "role":    "user",
            "content": "OBSERVATION:\n" + "\n\n".join(parts),
        })

    async def run(
        self,
        user_request: str,
        file_tree: Dict[str, str],
        chat_history: Optional[list] = None,
        gorilla_proxy_url: str = "",
        has_supabase: bool = False,
        is_debug: bool = False,
        error_context: str = "",
        image_b64: Optional[str] = None,
        previous_command_output: Optional[str] = None,
        agent_skills: Optional[Dict[str, Any]] = None,
        think_mode: str = "normal",
        screenshot_b64: Optional[str] = None,
    ) -> Dict[str, Any]:

        if gorilla_proxy_url:
            self._gorilla_proxy_url = gorilla_proxy_url

        self._ensure_system_prompt(gorilla_proxy_url, has_supabase, is_debug, agent_skills)

        compressed  = self.token_sub.compress_tree(file_tree)
        clean_paths = sorted(p for p in compressed if not p.endswith(".b64"))
        tree_str    = "\n".join(f"  {p}" for p in clean_paths)

        pkg_snippet = ""
        if "package.json" in compressed:
            pkg = compressed["package.json"]
            if len(pkg) < 3000:
                pkg_snippet = f"\n\npackage.json:\n{pkg}"

        prompt_image_b64: Optional[str] = None
        if not previous_command_output:
            prompt_image_b64 = self._extract_prompt_image(file_tree)
            if prompt_image_b64:
                log_agent("agent", "prompt_image.b64 found", self.project_id)

        if previous_command_output:
            filtered = _filter_observation(previous_command_output)
            obs_text = f"OBSERVATION:\n{filtered[:12000]}"
            if screenshot_b64:
                img_url = (
                    screenshot_b64 if screenshot_b64.startswith("data:")
                    else f"data:image/png;base64,{screenshot_b64}"
                )
                self.messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text",      "text": obs_text},
                        {"type": "image_url", "image_url": {"url": img_url}},
                    ],
                })
            else:
                self.messages.append({"role": "user", "content": obs_text})
        else:
            effective_request = user_request
            plan_text         = ""

            if not is_debug and user_request:
                if not self._prompt_expanded:
                    effective_request = await expand_prompt(
                        user_request,
                        has_supabase=has_supabase,
                        image_b64=prompt_image_b64,
                        gorilla_proxy_url=self._gorilla_proxy_url,
                    )
                    self._prompt_expanded = True

                if not self._plan_injected and bool(file_tree):
                    plan = await generate_plan(
                        effective_request,
                        tree_str,
                        has_supabase=has_supabase,
                        image_b64=prompt_image_b64,
                        gorilla_proxy_url=self._gorilla_proxy_url,
                    )
                    if plan:
                        plan_text = (
                            "\n\nHere's a plan — follow it step by step. Each item "
                            "= one turn. Use parallel reads on explore turns; "
                            "respect the 3-write batch limit on implementation turns:\n"
                            + plan
                        )
                        self._plan_injected = True

            parts = [f"Project files in /home/user/app:\n{tree_str}{pkg_snippet}"]

            mode_instruction = ""
            if not self._mode_instruction_injected and not is_debug:
                mode_instruction = MODE_INSTRUCTIONS.get(think_mode, "")
                self._mode_instruction_injected = True

            if is_debug and error_context:
                parts.append(f"\nError to fix:\n{error_context}")
            elif effective_request:
                parts.append(f"\nTask:\n{mode_instruction}{effective_request}{plan_text}")

            if chat_history:
                recent = chat_history[-6:]
                history_text = "\n".join(
                    f"{m.get('role','user').upper()}: {m.get('content','')[:200]}"
                    for m in recent if m.get("content")
                )
                if history_text:
                    parts.append(f"\nRecent conversation:\n{history_text}")

            user_text        = "\n".join(parts)
            first_turn_image = image_b64 or prompt_image_b64

            if first_turn_image:
                img_url = (
                    first_turn_image if first_turn_image.startswith("data:")
                    else f"data:image/jpeg;base64,{first_turn_image}"
                )
                self.messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text",      "text": user_text},
                        {"type": "image_url", "image_url": {"url": img_url}},
                    ],
                })
            else:
                self.messages.append({"role": "user", "content": user_text})

        # ─── DEEP-THINK OVERRIDE ─────────────────────────────────────
        # Detect the trigger phrase in this turn's user_request OR in any
        # earlier message of this agent (sticky). Once latched, every turn
        # — including vision and post-image turns — routes to DEEP_THINK_MODEL.
        if not self._deep_think and (
            think_mode == "deep" or _matches_deep_think_trigger(user_request or "")
        ):
            self._deep_think = True
            log_agent(
                "agent",
                f"DEEP-THINK mode ({'explicit' if think_mode == 'deep' else 'legacy trigger'}) "
                f"→ routing to {DEEP_THINK_MODEL}",
                self.project_id,
            )

        first_turn_has_image = bool(
            (image_b64 or prompt_image_b64) and not previous_command_output
        )
        observation_has_screenshot = bool(screenshot_b64 and previous_command_output)

        if self._deep_think:
            # Override: use the deep-think model for every turn, including
            # vision turns. The override takes precedence over the smart
            # picker and the vision-model branch — the user asked for
            # deliberate, deep reasoning, so we honour it everywhere.
            model = DEEP_THINK_MODEL
        elif first_turn_has_image or observation_has_screenshot:
            model = VISION_MODEL
        else:
            turn_index = max(0, len(self.messages) // 2 - 1)
            model = _pick_model(
                turn=turn_index,
                previous_output=previous_command_output,
                user_request=user_request,
                is_debug=is_debug,
            )

        log_agent(
            "agent",
            f"v18.1 model={model.split('/')[-1]} step={len(self.messages) // 2} "
            f"write_limit={WRITE_BATCH_LIMIT} supabase={has_supabase}",
            self.project_id,
        )

        try:
            raw_text, turn_tokens = await _call_llm(
                self.messages,
                model=model,
                temperature=0.6,
            )
            # ─── BILLING FIX: track per-turn delta cleanly ───
            self.total_tokens += turn_tokens
        except Exception as e:
            log_agent("agent", f"LLM error: {e}", self.project_id)
            return {
                "message":     f"AI error: {str(e)[:150]}",
                "write_files": [],
                "edit_files":  [],
                "read_calls":  [],
                "commands":    [],
                "done":        True,
                "tokens":      self.total_tokens,
                "turn_tokens": 0,
            }

        self.messages.append({"role": "assistant", "content": raw_text or ""})

        parsed = _parse_response(raw_text)

        if parsed["thought"]:
            log_agent("agent", f"THOUGHT: {parsed['thought'][:300]}", self.project_id)

        if parsed["extra_writes_dropped"] > 0:
            log_agent(
                "agent",
                f"⚠ Dropped {parsed['extra_writes_dropped']} extra writes (limit = {WRITE_BATCH_LIMIT})",
                self.project_id,
            )

        safe_bash = ""
        if parsed["bash"]:
            bash_content = self.token_sub.expand(parsed["bash"])
            if _is_safe(bash_content):
                safe_bash = bash_content
            else:
                log_agent("agent", "Blocked dangerous command", self.project_id)

        for wf in parsed["write_files"]:
            wf["content"] = self.token_sub.expand(wf["content"])
        for ef in parsed["edit_files"]:
            ef["old_str"] = self.token_sub.expand(ef["old_str"])
            ef["new_str"] = self.token_sub.expand(ef["new_str"])

        done   = parsed["done"]
        n_writes = len(parsed["write_files"])
        n_edits  = len(parsed["edit_files"])
        n_reads  = len(parsed["read_calls"])
        log_agent(
            "agent",
            f"{'DONE' if done else f'writes={n_writes} edits={n_edits} reads={n_reads} bash={bool(safe_bash)}'} "
            f"turn={turn_tokens}tok total={self.total_tokens}tok",
            self.project_id,
        )

        return {
            "message":      parsed["message"],
            "write_files":  parsed["write_files"],
            "edit_files":   parsed["edit_files"],
            "read_calls":   parsed["read_calls"],
            "commands":     [safe_bash] if safe_bash else [],
            "done":         done,
            "tokens":       self.total_tokens,
            "turn_tokens":  turn_tokens,
            "user_action":  parsed.get("user_action"),   # ← ADD THIS
            "spawn_subagent": parsed.get("spawn_subagent"),
            "_parsed":      parsed,
        }

    def record_tool_results(
        self,
        parsed:            Dict[str, Any],
        write_observation: str = "",
        read_observation:  str = "",
        bash_observation:  str = "",
    ) -> None:
        write_paths = [wf.get("path", "") for wf in parsed.get("write_files", []) if wf.get("path")]
        edit_paths  = [ef.get("path", "") for ef in parsed.get("edit_files",  []) if ef.get("path")]
        if write_observation and not bash_observation and not read_observation:
            bash_observation = write_observation
        self._append_observation(write_paths, edit_paths, read_observation, bash_observation)


# ---------------------------------------------------------------------------
# Legacy shim
# ---------------------------------------------------------------------------
class Agent:
    def __init__(self, timeout_s: float = 120.0):
        self.timeout_s = timeout_s

    def remember(self, project_id: str, role: str, text: str) -> None:
        _append_history(project_id, role, text)
