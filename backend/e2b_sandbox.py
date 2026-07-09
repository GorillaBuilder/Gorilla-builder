"""
E2B Sandbox Manager v18.1 — Claude Code-style tool execution
=============================================================

v18.1 patch notes (from v18):
  - BILLING FIX: bill on turn_tokens (per-call delta) instead of subtracting
    cumulative-then-cumulative. Reviewer pass no longer double-bills the
    entire agent history. Compatible with v18.1 lineage_agent's new
    turn_tokens field; falls back to delta math if it's missing.
  - RESTART LOOP FIX: pre-warm now sets _dev_server_up immediately after
    `npm install` completes (not after full health check). Plus the
    restart-blocker uses live pgrep to detect a running dev server even
    when the flag is stale.
  - REVIEWER FIX: skip the reviewer fix-turn when the agent already called
    mark_done — completed builds don't need a second pass that rewrites
    files mid-sync.

Performance comparison for dev server verification (unchanged from v18):
                        v15            v18
  cold startup          ~30-45s        ~3-8s (pre-warmed: <1s)
  warm verify           ~10-15s        <2s
  health check loop     12 × 2000ms    ~10 × 200ms (early-exit)
"""

from __future__ import annotations

import os
import re
import io
import time
import base64
import tarfile
import asyncio
import hashlib
import json
import shlex
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Callable, Set, Tuple

try:
    from e2b import Sandbox
except ImportError:
    Sandbox = None
    print("⚠️ e2b package not installed. Run: pip install e2b")

try:
    import httpx
except ImportError:
    httpx = None
    print("⚠️ httpx not installed; web_search/web_fetch tools will degrade.")

from backend.ai.lineage_agent import LineageAgent, log_agent, review_output

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
E2B_API_KEY      = os.getenv("E2B_API_KEY", "")
SANDBOX_TEMPLATE = os.getenv("E2B_TEMPLATE", "base")
IDLE_TIMEOUT_S   = 900
BILLING_TOKENS_PER_HOUR = 50_000
BILLING_TICK_S   = 1
APP_DIR          = "/home/user/app"
MAX_COMMANDS_PER_TURN   = 1600
MAX_TURNS_PER_REQUEST   = 120
SYNC_MARKER      = "/tmp/.gorilla_sync_marker"
FILE_READ_SENTINEL    = "═══GORILLA_FILE_BOUNDARY_9f8c═══"
FILE_CONTENT_SENTINEL = "═══GORILLA_CONTENT_START_9f8c═══"

DEFAULT_PREVIEW_PORT = 8080
DEFAULT_SERVER_PORT  = 3000

# Web tool config
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# Dev server fast-verify tunables
DEV_READY_TIMEOUT_S      = 5.0
DEV_POLL_INTERVAL_S      = 0.2
DEV_LOG_TAIL_LINES       = 40

_EDIT_KEYWORDS = frozenset([
    "fix", "change", "update", "edit", "debug", "adjust", "tweak",
    "modify", "rename", "move", "delete", "remove", "add a", "add the",
])


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def _strip_app_prefix(p: str) -> str:
    p = p.strip()
    if p.startswith(APP_DIR + "/"):
        return p[len(APP_DIR) + 1:]
    app_no_slash = APP_DIR.lstrip("/") + "/"
    if p.startswith(app_no_slash):
        return p[len(app_no_slash):]
    cleaned = re.sub(r"^.*?/app/", "", p)
    if cleaned != p:
        return cleaned
    return p


def _resolve_app_path(p: str) -> str:
    """Resolve an agent-supplied path to an absolute path strictly confined to
    APP_DIR. Prevents `../` traversal and absolute paths (e.g. `/etc/passwd`)
    from escaping the project sandbox, regardless of how the LLM formats the
    path (with/without an `/app` prefix, with leading slash, etc.)."""
    rel = _strip_app_prefix(p).lstrip("/")
    full = os.path.normpath(f"{APP_DIR}/{rel}")
    if full != APP_DIR and not full.startswith(APP_DIR + "/"):
        # Traversal attempted to escape APP_DIR — clamp back to the root.
        full = APP_DIR
    return full


def _resolve_app_rel(p: str) -> str:
    """Same confinement as _resolve_app_path but returns the path relative
    to APP_DIR (no leading slash), for display/storage purposes."""
    full = _resolve_app_path(p)
    if full == APP_DIR:
        return ""
    return full[len(APP_DIR) + 1:]


def _is_binary_path(p: str) -> bool:
    BINARY_EXTS = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
        ".svg", ".woff", ".woff2", ".ttf", ".eot", ".otf",
        ".mp3", ".mp4", ".wav", ".ogg", ".pdf", ".zip",
    }
    ext = os.path.splitext(p)[1].lower()
    return ext in BINARY_EXTS


# ---------------------------------------------------------------------------
# Activity classifier
# ---------------------------------------------------------------------------
def classify_command(cmd: str) -> Dict[str, str]:
    c   = cmd.strip()
    low = c.lower()

    if c.startswith("write_file:"):
        path = c[len("write_file:"):]
        return {"verb": "edit", "target": path, "short": f"Write {path}"}
    if c.startswith("edit_file:"):
        path = c[len("edit_file:"):]
        return {"verb": "edit", "target": path, "short": f"Edit {path}"}
    if c.startswith("read_files:"):
        paths = c[len("read_files:"):]
        return {"verb": "read", "target": paths, "short": f"Read {paths[:60]}"}
    if c.startswith("list_dir:"):
        path = c[len("list_dir:"):]
        return {"verb": "scan", "target": path, "short": f"List {path}"}
    if c.startswith("grep_search:"):
        pat = c[len("grep_search:"):]
        return {"verb": "scan", "target": "", "short": f"Grep {pat[:50]}"}
    if c.startswith("glob_files:"):
        pat = c[len("glob_files:"):]
        return {"verb": "scan", "target": "", "short": f"Glob {pat[:50]}"}
    if c.startswith("web_search:"):
        q = c[len("web_search:"):]
        return {"verb": "fetch", "target": "", "short": f"Web search: {q[:50]}"}
    if c.startswith("web_fetch:"):
        u = c[len("web_fetch:"):]
        return {"verb": "fetch", "target": "", "short": f"Fetch {u[:60]}"}
    if c.startswith("run_bash:"):
        snippet = c[len("run_bash:"):].strip()[:60]
        return {"verb": "execute", "target": "", "short": snippet or "Run command"}
    if c == "mark_done":
        return {"verb": "done", "target": "", "short": "Task complete"}

    m = re.match(r"cat\s+>>?\s+['\"]?([^\s'\"<]+)['\"]?\s+<<", c)
    if m:
        return {"verb": "edit", "target": m.group(1), "short": f"Edit {m.group(1)}"}
    if low.startswith("mkdir"):
        m = re.search(r"mkdir\s+(?:-p\s+)?['\"]?([^\s'\"]+)", c)
        return {"verb": "create", "target": m.group(1) if m else "",
                "short": f"Create dir {m.group(1) if m else ''}"}
    if low.startswith("rm"):
        m = re.search(r"rm\s+(?:-\S+\s+)*['\"]?([^\s'\"]+)", c)
        return {"verb": "delete", "target": m.group(1) if m else "",
                "short": f"Delete {m.group(1) if m else ''}"}
    if low.startswith("npm install") or low.startswith("npm i "):
        return {"verb": "install", "target": "", "short": "Install dependencies"}
    if low.startswith("npm run"):
        m = re.search(r"npm\s+run\s+(\S+)", c)
        return {"verb": "execute", "target": m.group(1) if m else "",
                "short": f"Run {m.group(1) if m else 'script'}"}
    if low.startswith("curl"):
        if "supabase.com" in c and "database/query" in c:
            return {"verb": "database", "target": "migration", "short": "Run SQL migration"}
        if "localhost" in c or "127.0.0.1" in c:
            return {"verb": "fetch", "target": "", "short": "Checking dev server"}
        m = re.search(r"curl\s+(?:-\S+\s+)*['\"]?(https?://[^\s'\"]+)", c)
        url = m.group(1)[:60] if m else ""
        return {"verb": "fetch", "target": "", "short": f"Fetch {url}" if url else "Run curl command"}
    if low.startswith("cat ") or low.startswith("tail ") or low.startswith("head "):
        m = re.match(r"\S+\s+(?:-\S+\s+)*['\"]?([^\s'\"]+)", c)
        return {"verb": "read", "target": m.group(1) if m else "",
                "short": f"Read {m.group(1) if m else 'file'}"}
    if low.startswith("grep ") or low.startswith("find ") or low.startswith("rg "):
        return {"verb": "scan", "target": "", "short": "Search files"}
    if low.startswith("python"):
        return {"verb": "execute", "target": "python", "short": "Run Python script"}
    if low.startswith("sed "):
        return {"verb": "edit", "target": "", "short": "Edit file"}
    first = c.split()[0] if c.split() else "run"
    return {"verb": "execute", "target": "", "short": f"Execute {first}"}


@dataclass
class SandboxSession:
    project_id:  str
    sandbox:     Any
    sandbox_id:  str
    owner_id:    str
    preview_port: int = DEFAULT_PREVIEW_PORT
    url:         Optional[str] = None
    created_at:  float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    last_bill_at:  float = field(default_factory=time.time)
    total_billed_tokens: int = 0
    deps_installed: bool = False
    content_hashes: Dict[str, str] = field(default_factory=dict)
    _billing_task: Optional[asyncio.Task] = field(default=None, repr=False)
    agent: Optional[Any] = field(default=None, repr=False)
    _cached_tree:    Dict[str, str] = field(default_factory=dict, repr=False)
    _tree_cached_at: float          = field(default=0.0,          repr=False)
    _has_files_api:  bool           = field(default=False,        repr=False)
    _agent_written_paths: Set[str]  = field(default_factory=set,  repr=False)
    _dev_server_up:  bool           = field(default=False,        repr=False)
    _has_ripgrep:    Optional[bool] = field(default=None,         repr=False)
    # How many spawn_subagent runs are currently in flight for this session.
    # Multiple sub-agents may run concurrently (siblings) — the depth limit
    # (a sub-agent cannot itself spawn another sub-agent) is enforced inside
    # _exec_spawn_subagent's own turn loop instead, by simply never
    # dispatching a nested spawn_subagents request there.
    _active_subagent_count: int     = field(default=0,            repr=False)
    # Base64 PNG from the most recent preview_screenshot call this turn, if
    # any. Consumed (and cleared) by _do_run_agent_turn after each turn's
    # read calls execute, then threaded into the next agent.run(screenshot_b64=...).
    _last_screenshot_b64: Optional[str] = field(default=None,     repr=False)


class E2BSandboxManager:
    def __init__(
        self,
        db_upsert_fn:       Callable,
        db_delete_fn:       Callable,
        db_upsert_batch_fn: Optional[Callable],
        add_tokens_fn:      Callable,
        emit_log_fn:        Callable,
        emit_status_fn:     Callable,
        emit_file_changed_fn:  Callable,
        emit_file_deleted_fn:  Callable,
        fetch_files_fn:     Callable,
        list_db_paths_fn:   Callable,
        progress_bus:       Any = None,
    ):
        self._sessions:      Dict[str, SandboxSession]  = {}
        self._db_upsert      = db_upsert_fn
        self._db_delete      = db_delete_fn
        self._db_upsert_batch = db_upsert_batch_fn
        self._add_tokens     = add_tokens_fn
        self._emit_log       = emit_log_fn
        self._emit_status    = emit_status_fn
        self._emit_file_changed = emit_file_changed_fn
        self._emit_file_deleted = emit_file_deleted_fn
        self._fetch_files    = fetch_files_fn
        self._list_db_paths  = list_db_paths_fn
        self._progress_bus   = progress_bus
        self._idle_monitor_task: Optional[asyncio.Task] = None
        self._boot_locks:  Dict[str, asyncio.Lock] = {}
        self._turn_locks:  Dict[str, asyncio.Lock] = {}
        self._activity_counter: Dict[str, int] = {}

    # ─── BILLING HELPER (v18.1) ──────────────────────────────────────────
    def _extract_turn_tokens(
        self,
        result: Dict[str, Any],
        cumulative_before: int,
    ) -> Tuple[int, int]:
        """
        Return (turn_delta, new_cumulative).

        v18.1 lineage_agent returns "turn_tokens" — the per-call delta. Use it.
        Fall back to subtracting cumulative-then-cumulative for older agents.
        """
        new_cumulative = result.get("tokens", 0) or 0
        if "turn_tokens" in result:
            return int(result["turn_tokens"] or 0), new_cumulative
        # Legacy path: compute delta from cumulative
        delta = max(0, new_cumulative - cumulative_before)
        return delta, new_cumulative

    # -----------------------------------------------------------
    # Emit helpers
    # -----------------------------------------------------------
    def _emit(self, project_id: str, event: Dict[str, Any]) -> None:
        if self._progress_bus:
            self._progress_bus.emit(project_id, event)

    def _next_activity_id(self, project_id: str) -> str:
        self._activity_counter[project_id] = self._activity_counter.get(project_id, 0) + 1
        return f"act_{self._activity_counter[project_id]}"

    def _emit_activity_start(self, project_id, activity_id, verb, target, short):
        self._emit(project_id, {
            "type": "activity_start", "id": activity_id,
            "verb": verb, "target": target, "short": short,
        })

    def _emit_activity_chunk(self, project_id, activity_id, stream, text):
        if text:
            self._emit(project_id, {
                "type": "activity_chunk", "id": activity_id,
                "stream": stream, "text": text,
            })

    def _emit_activity_end(self, project_id, activity_id, exit_code):
        self._emit(project_id, {
            "type": "activity_end", "id": activity_id,
            "exit_code": exit_code, "ok": exit_code == 0,
        })

    def _emit_narration(self, project_id, text):
        if text:
            self._emit(project_id, {"type": "narration", "text": text})

    # -----------------------------------------------------------
    # Port detection
    # -----------------------------------------------------------
    @staticmethod
    def _detect_preview_port(file_tree: Dict[str, str]) -> int:
        for path in ("vite.config.ts", "vite.config.js", "vite.config.mjs"):
            if path in file_tree and file_tree[path]:
                m = re.search(r"port\s*:\s*(\d+)", file_tree[path])
                if m:
                    try:
                        return int(m.group(1))
                    except ValueError:
                        pass
        pkg = file_tree.get("package.json", "")
        m = re.search(r"--port[= ]+(\d+)", pkg)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        return DEFAULT_PREVIEW_PORT

    def _sandbox_url_for_port(self, sbx, port: int) -> str:
        try:
            host = sbx.get_host(port)
            return f"https://{host}"
        except Exception:
            return f"https://{sbx.sandbox_id}-{port}.e2b.dev"

    # -----------------------------------------------------------
    # Boot-time tar upload
    # -----------------------------------------------------------
    @staticmethod
    def _build_tar_base64(file_tree: Dict[str, str]) -> Tuple[str, int, Dict[str, str]]:
        buf    = io.BytesIO()
        hashes: Dict[str, str] = {}
        count  = 0
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for path, content in file_tree.items():
                if not path or content is None:
                    continue
                if any(x in path for x in ["package-lock.json", "yarn.lock", "node_modules/", ".git/"]):
                    continue
                data = content.encode("utf-8", errors="replace")
                if len(data) > 1_000_000:
                    continue
                ti       = tarfile.TarInfo(name=path)
                ti.size  = len(data)
                ti.mtime = int(time.time())
                tf.addfile(ti, io.BytesIO(data))
                hashes[path] = hashlib.md5(data).hexdigest()
                count += 1
        raw = buf.getvalue()
        if len(raw) > 5 * 1024 * 1024:
            return "", 0, {}
        return base64.b64encode(raw).decode("ascii"), count, hashes

    def _upload_files_fast(self, sbx, file_tree: Dict[str, str]) -> Tuple[int, Dict[str, str]]:
        b64, count, hashes = self._build_tar_base64(file_tree)
        if not b64:
            return self._upload_files_slow(sbx, file_tree)
        try:
            sbx.commands.run("rm -f /tmp/bundle.b64 && touch /tmp/bundle.b64", timeout=5)
            for i in range(0, len(b64), 500_000):
                chunk = b64[i:i + 500_000]
                meta  = base64.b64encode(chunk.encode()).decode()
                sbx.commands.run(f"echo '{meta}' | base64 -d >> /tmp/bundle.b64", timeout=10)
            result = sbx.commands.run(
                f"mkdir -p {APP_DIR} && cat /tmp/bundle.b64 | base64 -d | "
                f"tar -xzf - -C {APP_DIR} && rm -f /tmp/bundle.b64",
                timeout=60,
            )
            if result.exit_code != 0:
                return self._upload_files_slow(sbx, file_tree)
            return count, hashes
        except Exception as e:
            print(f"⚠️ Batched upload failed: {e}; falling back")
            return self._upload_files_slow(sbx, file_tree)

    @staticmethod
    def _upload_files_slow(sbx, file_tree: Dict[str, str]) -> Tuple[int, Dict[str, str]]:
        hashes:       Dict[str, str] = {}
        count         = 0
        dirs_created: Set[str]       = set()
        for path, content in file_tree.items():
            if not path or content is None:
                continue
            if any(x in path for x in ["package-lock.json", "yarn.lock", "node_modules/", ".git/"]):
                continue
            full = f"{APP_DIR}/{path}"
            dirp = "/".join(full.split("/")[:-1])
            if dirp and dirp not in dirs_created:
                sbx.commands.run(f"mkdir -p '{dirp}'", timeout=5)
                dirs_created.add(dirp)
            safe = content.replace("GORILLA_EOF", "GORILLA__EOF")
            sbx.commands.run(f"cat > '{full}' << 'GORILLA_EOF'\n{safe}\nGORILLA_EOF", timeout=15)
            hashes[path] = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()
            count += 1
        return count, hashes

    # -----------------------------------------------------------
    # Native file write
    # -----------------------------------------------------------
    def _write_one_file_sync(self, sbx, rel_path: str, content: str) -> bool:
        full = _resolve_app_path(rel_path)
        dirp = "/".join(full.split("/")[:-1])

        try:
            sbx.commands.run(f"mkdir -p '{dirp}'", timeout=5)
            sbx.files.write(full, content)
            return True
        except AttributeError:
            pass
        except Exception as e:
            log_agent("agent", f"files.write failed for {rel_path}: {e}")

        try:
            sbx.commands.run(f"mkdir -p '{dirp}'", timeout=5)
            safe = content.replace("GORILLA_EOF", "GORILLA__EOF")
            result = sbx.commands.run(
                f"cat > '{full}' << 'GORILLA_EOF'\n{safe}\nGORILLA_EOF",
                timeout=20,
            )
            return (result.exit_code == 0)
        except Exception as e:
            log_agent("agent", f"heredoc fallback failed for {rel_path}: {e}")
            return False

    async def _write_files_parallel(
        self,
        project_id: str,
        write_files: List[Dict[str, str]],
    ) -> Tuple[List[str], str]:
        session = self._sessions.get(project_id)
        if not session or not write_files:
            return [], ""

        sbx = session.sandbox

        async def _write_one(wf: Dict[str, str]) -> Tuple[str, bool]:
            rel  = _resolve_app_rel(wf.get("path", "").strip())
            body = wf.get("content", "")

            if not rel:
                return rel, False

            activity_id = self._next_activity_id(project_id)
            self._emit_activity_start(
                project_id, activity_id,
                "edit", rel, f"Write {rel}",
            )
            ok = await asyncio.to_thread(self._write_one_file_sync, sbx, rel, body)
            self._emit_activity_end(project_id, activity_id, 0 if ok else 1)

            if ok:
                h = hashlib.md5(body.encode("utf-8", errors="replace")).hexdigest()
                session.content_hashes[rel] = h
                session._agent_written_paths.add(rel)
                log_agent("agent", f"wrote {rel}", project_id)
            else:
                log_agent("agent", f"FAILED to write {rel}", project_id)

            return rel, ok

        results  = await asyncio.gather(*[_write_one(wf) for wf in write_files])
        written  = [p for p, ok in results if ok]
        failed   = [p for p, ok in results if not ok]

        session._tree_cached_at = 0.0

        lines = []
        if written:
            lines.append(f"Wrote {len(written)} file(s): {', '.join(written)}")
        if failed:
            lines.append(f"FAILED to write: {', '.join(failed)}")
        obs = "\n".join(lines)

        return written, obs

    # -----------------------------------------------------------
    # edit_file (server-side str_replace)
    # -----------------------------------------------------------
    def _edit_one_file_sync(
        self, sbx, rel_path: str, old_str: str, new_str: str,
    ) -> Tuple[bool, str]:
        full = f"{APP_DIR}/{rel_path}"

        try:
            if hasattr(sbx, "files") and hasattr(sbx.files, "read"):
                current = sbx.files.read(full)
                if isinstance(current, bytes):
                    current = current.decode("utf-8", errors="replace")
            else:
                r = sbx.commands.run(f"cat '{full}'", timeout=10)
                if r.exit_code != 0:
                    return False, f"File not found: {rel_path}"
                current = r.stdout or ""
        except Exception as e:
            return False, f"Read failed for {rel_path}: {e}"

        occurrences = current.count(old_str)
        if occurrences == 0:
            return False, (
                f"edit_file: old_str not found in {rel_path}. "
                f"Re-read the file with read_files and copy exact bytes."
            )
        if occurrences > 1:
            return False, (
                f"edit_file: old_str matches {occurrences} times in {rel_path}. "
                f"Make the snippet unique by including more surrounding context."
            )

        new_content = current.replace(old_str, new_str, 1)
        ok = self._write_one_file_sync(sbx, rel_path, new_content)
        return ok, ("" if ok else f"Write failed for {rel_path}")

    async def _apply_edits_parallel(
        self,
        project_id: str,
        edits: List[Dict[str, str]],
    ) -> Tuple[List[str], str]:
        session = self._sessions.get(project_id)
        if not session or not edits:
            return [], ""

        sbx = session.sandbox

        async def _apply_one(ef: Dict[str, str]) -> Tuple[str, bool, str]:
            rel     = ef.get("path", "").strip()
            old_str = ef.get("old_str", "")
            new_str = ef.get("new_str", "")
            if not rel or not old_str:
                return rel, False, "Missing path or old_str"

            activity_id = self._next_activity_id(project_id)
            self._emit_activity_start(
                project_id, activity_id,
                "edit", rel, f"Edit {rel}",
            )
            ok, msg = await asyncio.to_thread(
                self._edit_one_file_sync, sbx, rel, old_str, new_str,
            )
            self._emit_activity_end(project_id, activity_id, 0 if ok else 1)

            if ok:
                try:
                    if hasattr(sbx, "files") and hasattr(sbx.files, "read"):
                        new_content = sbx.files.read(f"{APP_DIR}/{rel}")
                        if isinstance(new_content, bytes):
                            new_content = new_content.decode("utf-8", errors="replace")
                    else:
                        r = sbx.commands.run(f"cat '{APP_DIR}/{rel}'", timeout=5)
                        new_content = r.stdout or ""
                    session.content_hashes[rel] = hashlib.md5(
                        new_content.encode("utf-8", errors="replace")
                    ).hexdigest()
                    session._agent_written_paths.add(rel)
                except Exception:
                    pass
                log_agent("agent", f"edited {rel}", project_id)
                return rel, True, ""
            else:
                log_agent("agent", f"edit FAILED {rel}: {msg}", project_id)
                return rel, False, msg

        results = await asyncio.gather(*[_apply_one(ef) for ef in edits])
        edited  = [r[0] for r in results if r[1]]
        errors  = [(r[0], r[2]) for r in results if not r[1]]

        session._tree_cached_at = 0.0

        lines = []
        if edited:
            lines.append(f"Edited {len(edited)} file(s): {', '.join(edited)}")
        for path, msg in errors:
            lines.append(f"EDIT FAILED [{path}]: {msg}")
        obs = "\n".join(lines)

        return edited, obs

    # -----------------------------------------------------------
    # ripgrep availability + install
    # -----------------------------------------------------------
    async def _ensure_ripgrep(self, session: SandboxSession) -> bool:
        if session._has_ripgrep is not None:
            return session._has_ripgrep
        try:
            r = await asyncio.to_thread(
                session.sandbox.commands.run, "which rg", timeout=5,
            )
            if r.exit_code == 0 and (r.stdout or "").strip():
                session._has_ripgrep = True
                return True
        except Exception:
            pass

        try:
            r = await asyncio.to_thread(
                session.sandbox.commands.run,
                "apt-get install -y ripgrep > /dev/null 2>&1 && which rg",
                timeout=30,
            )
            session._has_ripgrep = (r.exit_code == 0 and bool((r.stdout or "").strip()))
        except Exception:
            session._has_ripgrep = False

        log_agent("agent", f"ripgrep available: {session._has_ripgrep}")
        return session._has_ripgrep

    # -----------------------------------------------------------
    # Tool executors
    # -----------------------------------------------------------
    async def _exec_read_files(
        self, session: SandboxSession, project_id: str, params: Dict[str, str],
    ) -> str:
        raw = params.get("paths", "")
        paths = [p.strip() for p in re.split(r"[,\n]+", raw) if p.strip()][:10]
        if not paths:
            return "read_files: no paths provided."

        activity_id = self._next_activity_id(project_id)
        self._emit_activity_start(
            project_id, activity_id,
            "read", ",".join(paths[:3]) + ("..." if len(paths) > 3 else ""),
            f"Read {len(paths)} file(s)",
        )

        sbx = session.sandbox

        def _read_one(rel: str) -> Tuple[str, str]:
            full = _resolve_app_path(rel)
            rel = _resolve_app_rel(rel)
            try:
                if hasattr(sbx, "files") and hasattr(sbx.files, "read"):
                    content = sbx.files.read(full)
                    if isinstance(content, bytes):
                        content = content.decode("utf-8", errors="replace")
                    return rel, content
                r = sbx.commands.run(f"cat '{full}' 2>/dev/null", timeout=10)
                if r.exit_code != 0:
                    return rel, f"[error: file not found]"
                return rel, (r.stdout or "")
            except Exception as e:
                return rel, f"[error: {e}]"

        results = await asyncio.gather(*[asyncio.to_thread(_read_one, p) for p in paths])

        parts = []
        for rel, content in results:
            snippet = content[:30_000]
            truncated = " [TRUNCATED]" if len(content) > 30_000 else ""
            parts.append(f"━━━ {rel}{truncated} ━━━\n{snippet}")

        self._emit_activity_end(project_id, activity_id, 0)
        return "FILES READ:\n" + "\n\n".join(parts)

    async def _exec_list_dir(
        self, session: SandboxSession, project_id: str, params: Dict[str, str],
    ) -> str:
        path = params.get("path", ".").strip() or "."
        full = _resolve_app_path(path) if path != "." else APP_DIR
        rel  = _resolve_app_rel(path) if path != "." else ""

        activity_id = self._next_activity_id(project_id)
        self._emit_activity_start(
            project_id, activity_id, "scan", path, f"List {path}",
        )

        cmd = (
            f"cd {shlex.quote(full)} 2>/dev/null && "
            f"ls -lhA --time-style=+ 2>/dev/null | "
            f"awk '{{if (NF >= 7) printf \"%s  %s  %s\\n\", $1, $5, $NF}}' | "
            f"grep -v node_modules | grep -v '^total'"
        )
        try:
            r = await asyncio.to_thread(session.sandbox.commands.run, cmd, timeout=10)
            out = (r.stdout or "").strip() or "(empty)"
        except Exception as e:
            out = f"[error: {e}]"

        self._emit_activity_end(project_id, activity_id, 0)
        return f"DIRECTORY LISTING ({rel or '.'}):\n{out[:6000]}"

    async def _exec_grep_search(
        self, session: SandboxSession, project_id: str, params: Dict[str, str],
    ) -> str:
        pattern = params.get("pattern", "").strip()
        if not pattern:
            return "grep_search: no pattern provided."
        subpath = params.get("path", "").strip()
        glob    = params.get("file_glob", "").strip()

        activity_id = self._next_activity_id(project_id)
        self._emit_activity_start(
            project_id, activity_id, "scan", "",
            f"Grep '{pattern[:40]}'",
        )

        has_rg = await self._ensure_ripgrep(session)

        search_root = _resolve_app_path(subpath) if subpath else APP_DIR
        search_root = search_root.rstrip("/") or APP_DIR

        if has_rg:
            cmd_parts = [
                "rg", "--no-heading", "-n", "--max-count", "100",
                "--max-columns", "300",
                "-g", "!node_modules", "-g", "!.git", "-g", "!dist",
            ]
            if glob:
                cmd_parts.extend(["-g", glob])
            cmd_parts.extend([shlex.quote(pattern), shlex.quote(search_root)])
            cmd = " ".join(cmd_parts) + " 2>/dev/null | head -100"
        else:
            include = f"--include='{glob}' " if glob else ""
            cmd = (
                f"grep -r -n {include}"
                f"--exclude-dir=node_modules --exclude-dir=.git --exclude-dir=dist "
                f"{shlex.quote(pattern)} {shlex.quote(search_root)} "
                f"2>/dev/null | head -100"
            )

        try:
            r = await asyncio.to_thread(session.sandbox.commands.run, cmd, timeout=15)
            raw = (r.stdout or "").strip()
        except Exception as e:
            raw = f"[error: {e}]"

        cleaned = raw.replace(f"{APP_DIR}/", "")

        self._emit_activity_end(project_id, activity_id, 0)
        if not cleaned:
            return f"GREP '{pattern}': no matches."
        return f"GREP '{pattern}':\n{cleaned[:6000]}"

    async def _exec_glob_files(
        self, session: SandboxSession, project_id: str, params: Dict[str, str],
    ) -> str:
        pattern = params.get("pattern", "").strip()
        if not pattern:
            return "glob_files: no pattern provided."

        activity_id = self._next_activity_id(project_id)
        self._emit_activity_start(
            project_id, activity_id, "scan", "",
            f"Glob {pattern[:40]}",
        )

        cmd = (
            f"cd {APP_DIR} && "
            f"find . -type f -not -path '*/node_modules/*' "
            f"-not -path '*/.git/*' -not -path '*/dist/*' "
            f"2>/dev/null | grep -E '{_glob_to_regex(pattern)}' | head -200"
        )
        try:
            r = await asyncio.to_thread(session.sandbox.commands.run, cmd, timeout=10)
            raw = (r.stdout or "").strip()
        except Exception as e:
            raw = f"[error: {e}]"

        cleaned = re.sub(r"^\./", "", raw, flags=re.MULTILINE)

        self._emit_activity_end(project_id, activity_id, 0)
        if not cleaned:
            return f"GLOB '{pattern}': no matches."
        return f"GLOB '{pattern}':\n{cleaned[:4000]}"

    _PLAYWRIGHT_SHOT_SCRIPT = (
        "import sys\n"
        "from playwright.sync_api import sync_playwright\n"
        "url = sys.argv[1]\n"
        "out = sys.argv[2]\n"
        "with sync_playwright() as p:\n"
        "    browser = p.chromium.launch(args=['--no-sandbox'])\n"
        "    page = browser.new_page(viewport={'width': 1280, 'height': 800})\n"
        "    page.goto(url, timeout=8000, wait_until='load')\n"
        "    page.screenshot(path=out)\n"
        "    browser.close()\n"
    )

    async def _exec_preview_screenshot(
        self, session: SandboxSession, project_id: str, params: Dict[str, str],
    ) -> str:
        """
        Launch headless Chromium (Playwright) INSIDE the sandbox, navigate to
        the dev server, and capture a screenshot. The base64 PNG is stashed on
        the session (session._last_screenshot_b64) rather than returned inline
        as text — tool observations are plain text, but a screenshot needs to
        reach the LLM as an actual image on the NEXT turn. _do_run_agent_turn
        reads session._last_screenshot_b64 after this tool runs and passes it
        into the next agent.run(...) call as screenshot_b64=... .

        On any failure (playwright missing because the template hasn't been
        rebuilt yet, dev server not responding, etc.) this returns a clear
        plain-text error observation instead of raising.
        """
        path = params.get("path", "/").strip() or "/"
        if not path.startswith("/"):
            path = "/" + path

        activity_id = self._next_activity_id(project_id)
        self._emit_activity_start(
            project_id, activity_id, "screenshot", path, "Screenshot preview",
        )

        url = f"http://localhost:{session.preview_port}{path}"
        remote_out = "/tmp/gorilla_preview.png"
        script_path = "/tmp/gorilla_preview_shot.py"

        try:
            safe_script = self._PLAYWRIGHT_SHOT_SCRIPT.replace("GORILLA_EOF", "GORILLA__EOF")
            write_result = await asyncio.to_thread(
                session.sandbox.commands.run,
                f"cat > '{script_path}' << 'GORILLA_EOF'\n{safe_script}\nGORILLA_EOF",
                timeout=10,
            )
            if write_result.exit_code != 0:
                self._emit_activity_end(project_id, activity_id, 1)
                return (
                    "preview_screenshot failed: could not write screenshot script "
                    "into the sandbox — the dev server may not be running, or this "
                    "sandbox template needs a rebuild with playwright installed."
                )

            run_result = await asyncio.to_thread(
                session.sandbox.commands.run,
                f"cd {APP_DIR} && python3 '{script_path}' {shlex.quote(url)} {shlex.quote(remote_out)}",
                timeout=20,
            )
            if run_result.exit_code != 0:
                stderr = (run_result.stderr or run_result.stdout or "").strip()[:500]
                self._emit_activity_end(project_id, activity_id, 1)
                return (
                    f"preview_screenshot failed: {stderr or 'unknown error'} — "
                    "the dev server may not be running, or this sandbox template "
                    "needs a rebuild with playwright installed."
                )

            sbx = session.sandbox
            if hasattr(sbx, "files") and hasattr(sbx.files, "read"):
                raw = await asyncio.to_thread(sbx.files.read, remote_out)
                if isinstance(raw, str):
                    raw = raw.encode("latin-1", errors="ignore")
            else:
                b64_result = await asyncio.to_thread(
                    session.sandbox.commands.run,
                    f"base64 -w0 '{remote_out}'",
                    timeout=10,
                )
                if b64_result.exit_code != 0 or not (b64_result.stdout or "").strip():
                    self._emit_activity_end(project_id, activity_id, 1)
                    return (
                        "preview_screenshot failed: could not read the screenshot "
                        "back out of the sandbox — the dev server may not be "
                        "running, or this sandbox template needs a rebuild with "
                        "playwright installed."
                    )
                session._last_screenshot_b64 = (b64_result.stdout or "").strip()
                self._emit_activity_end(project_id, activity_id, 0)
                return f"PREVIEW SCREENSHOT of {path}: captured (image attached on next turn)."

            b64 = base64.b64encode(raw).decode("ascii")
            session._last_screenshot_b64 = b64
            self._emit_activity_end(project_id, activity_id, 0)
            return f"PREVIEW SCREENSHOT of {path}: captured (image attached on next turn)."
        except Exception as e:
            self._emit_activity_end(project_id, activity_id, 1)
            return (
                f"preview_screenshot failed: {e} — the dev server may not be "
                "running, or this sandbox template needs a rebuild with "
                "playwright installed."
            )

    async def _exec_web_search(
        self, project_id: str, params: Dict[str, str],
    ) -> str:
        query = params.get("query", "").strip()
        if not query:
            return "web_search: no query provided."

        activity_id = self._next_activity_id(project_id)
        self._emit_activity_start(
            project_id, activity_id, "fetch", "",
            f"Web search: {query[:50]}",
        )

        result_text = ""
        if not httpx:
            result_text = "web_search: httpx not installed in backend."
        elif SERPER_API_KEY:
            try:
                async with httpx.AsyncClient(timeout=12.0) as client:
                    r = await client.post(
                        "https://google.serper.dev/search",
                        headers={
                            "X-API-KEY": SERPER_API_KEY,
                            "Content-Type": "application/json",
                        },
                        json={"q": query, "num": 5},
                    )
                    data = r.json()
                results = data.get("organic", [])[:5]
                if results:
                    lines = []
                    for item in results:
                        title = item.get("title", "").strip()
                        url   = item.get("link", "").strip()
                        snip  = item.get("snippet", "").strip()
                        lines.append(f"• {title}\n  {url}\n  {snip[:200]}")
                    result_text = "\n\n".join(lines)
                else:
                    result_text = "web_search: no results."
            except Exception as e:
                result_text = f"web_search failed: {e}"
        elif TAVILY_API_KEY:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r = await client.post(
                        "https://api.tavily.com/search",
                        json={
                            "api_key": TAVILY_API_KEY,
                            "query": query,
                            "max_results": 5,
                        },
                    )
                    data = r.json()
                results = data.get("results", [])[:5]
                if results:
                    lines = []
                    for item in results:
                        title = item.get("title", "").strip()
                        url   = item.get("url", "").strip()
                        snip  = item.get("content", "").strip()
                        lines.append(f"• {title}\n  {url}\n  {snip[:200]}")
                    result_text = "\n\n".join(lines)
                else:
                    result_text = "web_search: no results."
            except Exception as e:
                result_text = f"web_search failed: {e}"
        else:
            result_text = (
                "web_search: no search API key configured. "
                "Set SERPER_API_KEY or TAVILY_API_KEY env var."
            )

        self._emit_activity_end(project_id, activity_id, 0)
        return f"WEB SEARCH '{query}':\n{result_text[:4000]}"

    async def _exec_web_fetch(
        self, project_id: str, params: Dict[str, str],
    ) -> str:
        url = params.get("url", "").strip()
        if not url or not url.startswith(("http://", "https://")):
            return "web_fetch: invalid URL (must start with https://)."

        activity_id = self._next_activity_id(project_id)
        self._emit_activity_start(
            project_id, activity_id, "fetch", url, f"Fetch {url[:60]}",
        )

        result_text = ""
        if not httpx:
            result_text = "web_fetch: httpx not installed in backend."
        else:
            try:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                    r = await client.get(
                        url,
                        headers={
                            "User-Agent": "Mozilla/5.0 GorillaBuilder/1.0",
                            "Accept": "text/html,application/xhtml+xml,text/plain",
                        },
                    )
                    text = r.text
                text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
                text = re.sub(r"<style[\s\S]*?</style>",   " ", text, flags=re.I)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                result_text = text[:12_000]
            except Exception as e:
                result_text = f"web_fetch failed: {e}"

        self._emit_activity_end(project_id, activity_id, 0)
        return f"WEB FETCH {url}:\n{result_text}"

    async def _execute_read_calls_parallel(
        self,
        project_id: str,
        read_calls: List[Dict[str, Any]],
    ) -> str:
        session = self._sessions.get(project_id)
        if not session or not read_calls:
            return ""

        tasks = []
        for call in read_calls:
            tool   = call.get("tool", "")
            params = call.get("params", {})
            if tool == "read_files":
                tasks.append(self._exec_read_files(session, project_id, params))
            elif tool == "list_dir":
                tasks.append(self._exec_list_dir(session, project_id, params))
            elif tool == "grep_search":
                tasks.append(self._exec_grep_search(session, project_id, params))
            elif tool == "glob_files":
                tasks.append(self._exec_glob_files(session, project_id, params))
            elif tool == "web_search":
                tasks.append(self._exec_web_search(project_id, params))
            elif tool == "web_fetch":
                tasks.append(self._exec_web_fetch(project_id, params))
            elif tool == "preview_screenshot":
                tasks.append(self._exec_preview_screenshot(session, project_id, params))
            else:
                tasks.append(asyncio.sleep(0, result=f"[unknown read tool: {tool}]"))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        out_parts = []
        for r in results:
            if isinstance(r, Exception):
                out_parts.append(f"[tool error: {r}]")
            else:
                out_parts.append(str(r))
        return "\n\n".join(out_parts)

    # -----------------------------------------------------------
    # spawn_subagent — bounded, depth-limited delegation
    # -----------------------------------------------------------
    async def _exec_spawn_subagent(
        self,
        project_id: str,
        session: SandboxSession,
        task: str,
        max_turns_raw: str,
    ) -> Tuple[str, int]:
        """
        Run a fresh, throwaway LineageAgent against the SAME sandbox/file tree
        for a bounded number of turns, then collapse everything it did into a
        short text summary for the parent turn's observation.

        Returns (summary_text, turn_tokens_delta) so the caller can bill the
        parent turn for the sub-agent's token usage.

        Multiple sub-agents may run concurrently as siblings (the caller
        launches several of these with asyncio.gather). The depth limit — a
        sub-agent cannot itself spawn another sub-agent — is enforced below,
        inside this function's own turn loop, by simply never dispatching
        any spawn_subagents request the sub-agent's own turns produce.
        """
        task = (task or "").strip()
        if not task:
            return ("spawn_subagent: no task provided.", 0)

        try:
            max_turns = int(str(max_turns_raw).strip() or 5)
        except (TypeError, ValueError):
            max_turns = 5
        max_turns = max(1, min(max_turns, 8))

        activity_id = self._next_activity_id(project_id)
        self._emit_activity_start(
            project_id, activity_id, "delegate", task[:80],
            f"Delegating: {task[:60]}",
        )

        session._active_subagent_count += 1
        sub_agent = LineageAgent(project_id)
        turn_tokens_total = 0
        touched_paths: List[str] = []
        final_message = ""
        turns_run = 0

        try:
            previous_output: Optional[str] = None
            for turn in range(max_turns):
                turns_run = turn + 1
                result = await sub_agent.run(
                    user_request=task,
                    file_tree=session._cached_tree if turn == 0 else {},
                    previous_command_output=previous_output,
                )

                turn_delta = int(result.get("turn_tokens", 0) or 0)
                turn_tokens_total += turn_delta

                # Depth limit (max 1 level): a sub-agent's own turns are never
                # dispatched to _exec_spawn_subagent, no matter what it asks for.
                if result.get("spawn_subagents"):
                    previous_output = (
                        "OBSERVATION:\nspawn_subagent is not available inside a "
                        "sub-agent (depth limit of 1). Do the investigation "
                        "yourself with read/write/bash tools instead."
                    )
                    continue

                write_files = result.get("write_files", [])
                edit_files  = result.get("edit_files",  [])
                read_calls  = result.get("read_calls",  [])
                commands    = result.get("commands",    [])
                parsed_internal = result.get("_parsed", {})

                read_obs = ""
                if read_calls:
                    read_obs = await self._execute_read_calls_parallel(project_id, read_calls)

                write_obs_parts: List[str] = []
                if write_files:
                    wp, wobs = await self._write_files_parallel(project_id, write_files)
                    touched_paths.extend(wp)
                    if wobs:
                        write_obs_parts.append(wobs)
                if edit_files:
                    ep, eobs = await self._apply_edits_parallel(project_id, edit_files)
                    touched_paths.extend(ep)
                    if eobs:
                        write_obs_parts.append(eobs)
                write_obs = "\n\n".join(write_obs_parts)

                bash_obs = ""
                if commands:
                    cmd_results = await self._execute_commands_streaming(project_id, commands)
                    output_parts: List[str] = []
                    for r in cmd_results:
                        stdout    = (r.get("stdout") or "").strip()
                        stderr    = (r.get("stderr") or "").strip()
                        exit_code = r.get("exit_code", 0)
                        if stdout:
                            output_parts.append(stdout[:2000])
                        if stderr:
                            output_parts.append(f"STDERR: {stderr[:1000]}")
                        if exit_code != 0:
                            output_parts.append(f"[exit code: {exit_code}]")
                    bash_obs = "\n".join(output_parts)[:4000] if output_parts else "Command ran successfully with no output."

                if parsed_internal:
                    sub_agent.record_tool_results(
                        parsed_internal,
                        write_observation=write_obs,
                        read_observation=read_obs,
                        bash_observation=bash_obs,
                    )

                msg = result.get("message", "")
                if msg:
                    final_message = msg

                if result.get("done", False):
                    break

                obs_parts = [p for p in (read_obs, write_obs, bash_obs) if p]
                previous_output = "\n\n".join(obs_parts) if obs_parts else "(no output)"

            session._tree_cached_at = 0.0  # sub-agent may have changed files
        except Exception as e:
            log_agent("agent", f"spawn_subagent error: {e}", project_id)
            final_message = final_message or f"Sub-agent errored: {e}"
        finally:
            session._active_subagent_count = max(0, session._active_subagent_count - 1)

        summary_lines = [f"Sub-agent task: {task}"]
        summary_lines.append(f"Ran {turns_run} turn(s).")
        if touched_paths:
            uniq = sorted(set(touched_paths))
            summary_lines.append(f"Files touched: {', '.join(uniq[:30])}")
        else:
            summary_lines.append("No files were changed.")
        summary_lines.append(f"Sub-agent's final message: {final_message or '(none)'}")
        summary = "\n".join(summary_lines)

        self._emit_activity_end(project_id, activity_id, 0)
        return summary, turn_tokens_total

    # -----------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------
    async def ensure_running(
        self, project_id: str, env_vars: Dict[str, str], owner_id: str
    ) -> SandboxSession:
        if project_id in self._sessions:
            s = self._sessions[project_id]
            s.last_activity = time.time()
            return s
        self._boot_locks.setdefault(project_id, asyncio.Lock())
        async with self._boot_locks[project_id]:
            if project_id in self._sessions:
                s = self._sessions[project_id]
                s.last_activity = time.time()
                return s
            files = await self._fetch_files(project_id)
            return await self._do_boot(project_id, files, env_vars, owner_id)

    async def _do_boot(self, project_id, file_tree, env_vars, owner_id):
        if not Sandbox:
            raise RuntimeError("E2B SDK not installed")
        if not E2B_API_KEY:
            raise RuntimeError("E2B_API_KEY not set")

        self._emit_log(project_id, "sandbox", f"Booting sandbox ({SANDBOX_TEMPLATE})...")
        self._emit_status(project_id, "Booting Sandbox...")

        def _boot_sync():
            sbx = Sandbox(template=SANDBOX_TEMPLATE, api_key=E2B_API_KEY, timeout=3600)
            sbx.commands.run(f"mkdir -p {APP_DIR}", timeout=5)
            return sbx

        try:
            sbx = await asyncio.to_thread(_boot_sync)
        except Exception as e:
            self._emit_log(project_id, "sandbox", f"Boot failed: {e}")
            raise

        self._emit_log(project_id, "sandbox", "Uploading project files...")
        file_count, hashes = await asyncio.to_thread(self._upload_files_fast, sbx, file_tree)
        self._emit_log(project_id, "sandbox", f"Mounted {file_count} text files")

        binary_count = await self._restore_binary_files(sbx, project_id, file_tree)
        if binary_count:
            self._emit_log(project_id, "sandbox", f"Restored {binary_count} binary assets")

        if env_vars:
            try:
                env_lines   = "\n".join(f"{k}={v}" for k, v in env_vars.items() if v)
                shell_lines = "\n".join(f'export {k}="{v}"' for k, v in env_vars.items() if v)
                await asyncio.to_thread(
                    sbx.commands.run,
                    f"cat > {APP_DIR}/.env << 'GORILLA_EOF'\n{env_lines}\nGORILLA_EOF",
                )
                await asyncio.to_thread(
                    sbx.commands.run,
                    f"cat > {APP_DIR}/.gorilla_env << 'GORILLA_EOF'\n{shell_lines}\nGORILLA_EOF",
                )
                await asyncio.to_thread(
                    sbx.commands.run,
                    f"set -a && source {APP_DIR}/.env && set +a",
                )
            except Exception as e:
                self._emit_log(project_id, "sandbox", f"env write warning: {e}")

        inject_error_reporter = (
            r"grep -q '__gorilla_errors' " + APP_DIR + r"/index.html 2>/dev/null || "
            r"sed -i 's|</head>|<script>"
            r"(function(){"
            r"function send(d){try{fetch(\"/api/__gorilla_errors\",{method:\"POST\","
            r"headers:{\"Content-Type\":\"application/json\"},body:JSON.stringify(d)})}catch(e){}}"
            r"window.onerror=function(m,s,l,c,e){"
            r"send({message:m,source:s+\":\"+l,type:\"error\",stack:e&&e.stack?e.stack.slice(0,300):\"\"});"
            r"return false;};"
            r"window.addEventListener(\"unhandledrejection\",function(e){"
            r"send({message:e.reason&&e.reason.message?e.reason.message:String(e.reason),"
            r"type:\"unhandledrejection\"});});"
            r"window.addEventListener(\"error\",function(e){"
            r"if(e.target&&e.target!==window){"
            r"send({message:\"Resource failed: \"+(e.target.src||e.target.href),type:\"resource\"});}},true);"
            r"})();"
            r"<\/script><\/head>|' " + APP_DIR + r"/index.html"
        )
        try:
            await asyncio.to_thread(sbx.commands.run, inject_error_reporter, timeout=5)
            log_agent("agent", "Injected browser error reporter into index.html", project_id)
        except Exception as e:
            log_agent("agent", f"Error reporter inject skipped: {e}", project_id)

        has_files_api = hasattr(sbx, "files") and hasattr(getattr(sbx, "files", None), "write")

        preview_port = self._detect_preview_port(file_tree)
        session = SandboxSession(
            project_id=project_id, sandbox=sbx, sandbox_id=sbx.sandbox_id,
            owner_id=owner_id, preview_port=preview_port,
            url=self._sandbox_url_for_port(sbx, preview_port),
            deps_installed=True, content_hashes=hashes,
            _has_files_api=has_files_api,
        )
        self._sessions[project_id] = session

        session._billing_task = asyncio.create_task(self._billing_loop(project_id))
        if not self._idle_monitor_task or self._idle_monitor_task.done():
            self._idle_monitor_task = asyncio.create_task(self._idle_monitor())

        self._emit_log(project_id, "sandbox", f"Sandbox ready: {session.url}")
        self._emit_status(project_id, "Session Started..")
        self._emit(project_id, {"type": "sandbox_url", "url": session.url})

        # NOTE: no auto-boot here anymore. This used to fire-and-forget
        # `npm install && npm run dev` on every sandbox unconditionally,
        # which assumed every project is the Node/Vite boilerplate — wrong
        # for imported non-Node codebases (and wasted an install+boot cycle
        # even when nothing about the project changed since last session).
        # Starting the app (whatever stack it actually is) and getting it
        # reachable on the preview port is now the agent's job, driven by
        # the system prompt — see prompts.py's Environment section.

        return session

    async def _is_dev_process_alive(self, session: SandboxSession) -> bool:
        """
        Live check: is anything actually bound to the dev server ports right
        now (8080 frontend / 3000 backend)?

        This used to check `pgrep -f 'npm run dev'` for the exact parent
        process name, but `npm run dev` commonly execs into (or spawns) a
        `vite`/`node` child that keeps the ports held even after the parent
        npm process itself has exited — pgrep then reports DOWN, the guard
        doesn't block the next restart, and the real `npm run dev` collides
        with the still-listening child (EADDRINUSE), producing a genuine
        failure the agent has no way to explain except by restarting again.
        Checking the ports directly is what actually matters, regardless of
        which process holds them. `/dev/tcp` is a bash builtin — no extra
        packages required in the sandbox image.
        """
        try:
            r = await asyncio.to_thread(
                session.sandbox.commands.run,
                "bash -c '"
                "(echo > /dev/tcp/127.0.0.1/8080) 2>/dev/null && echo UP && exit; "
                "(echo > /dev/tcp/127.0.0.1/3000) 2>/dev/null && echo UP && exit; "
                "echo DOWN'",
                timeout=3,
            )
            return "UP" in (r.stdout or "")
        except Exception:
            return False

    async def _restore_binary_files(self, sbx, project_id: str, file_tree: Dict[str, str]) -> int:
        count = 0
        for path, content in file_tree.items():
            if not _is_binary_path(path):
                continue
            if not content or not content.startswith("http"):
                continue
            full_path = f"{APP_DIR}/{path}"
            dirp      = "/".join(full_path.split("/")[:-1])
            try:
                result = await asyncio.to_thread(
                    sbx.commands.run,
                    f"mkdir -p '{dirp}' && curl -sL --max-time 30 '{content}' -o '{full_path}' && echo OK",
                    timeout=35,
                )
                if "OK" in (result.stdout or ""):
                    count += 1
                    log_agent("agent", f"Restored binary: {path}", project_id)
                else:
                    log_agent("agent", f"Binary restore may have failed: {path}", project_id)
            except Exception as e:
                log_agent("agent", f"Failed to restore binary {path}: {e}", project_id)
        return count

    async def kill(self, project_id: str) -> None:
        session = self._sessions.pop(project_id, None)
        if not session:
            self._boot_locks.pop(project_id, None)
            self._turn_locks.pop(project_id, None)
            return
        now     = time.time()
        elapsed = (now - session.last_bill_at) / 3600.0
        if elapsed > 0.01:
            prorated = int(BILLING_TOKENS_PER_HOUR * elapsed)
            if prorated > 0:
                try:
                    self._add_tokens(session.owner_id, prorated)
                except Exception as e:
                    print(f"⚠️ Billing error: {e}")
        if session._billing_task and not session._billing_task.done():
            session._billing_task.cancel()
        try:
            await asyncio.to_thread(session.sandbox.kill)
        except Exception as e:
            print(f"⚠️ Kill error: {e}")
        self._boot_locks.pop(project_id, None)
        self._turn_locks.pop(project_id, None)
        self._emit_status(project_id, "Sandbox Offline")

    def is_running(self, project_id: str) -> bool:
        return project_id in self._sessions

    def get_session(self, project_id: str) -> Optional[SandboxSession]:
        s = self._sessions.get(project_id)
        if s:
            s.last_activity = time.time()
        return s

    def get_preview_url(self, project_id: str) -> Optional[str]:
        s = self._sessions.get(project_id)
        return s.url if s else None

    # -----------------------------------------------------------
    # Console error polling
    # -----------------------------------------------------------
    async def _poll_console_errors(self, project_id: str) -> str:
        session = self._sessions.get(project_id)
        if not session:
            return ""
        try:
            result = await asyncio.to_thread(
                session.sandbox.commands.run,
                "curl -s http://localhost:3000/api/__gorilla_errors 2>/dev/null",
                timeout=5,
            )
            raw = (result.stdout or "").strip()
            if not raw or raw in ("[]", ""):
                return ""
            errors = json.loads(raw)
            if not errors:
                return ""
            lines = []
            for e in errors[:10]:
                msg   = e.get("message", str(e))
                src   = e.get("source", "")
                etype = e.get("type", "error")
                stack = e.get("stack", "")
                entry = f"  [{etype}] {msg}"
                if src:
                    entry += f" @ {src}"
                if stack:
                    entry += f"\n    {stack[:200]}"
                lines.append(entry)
            return "BROWSER CONSOLE ERRORS (fix these):\n" + "\n".join(lines)
        except Exception:
            return ""

    # -----------------------------------------------------------
    # SUB-5-SECOND dev server verification
    # -----------------------------------------------------------
    async def _fast_verify_dev_server(
        self, project_id: str, session: SandboxSession,
    ) -> Tuple[bool, bool, str]:
        start = time.monotonic()
        deadline = start + DEV_READY_TIMEOUT_S
        sbx = session.sandbox

        fe_ok = False
        api_ok = False

        def probe(port: int) -> bool:
            try:
                r = sbx.commands.run(
                    f"curl -s -o /dev/null -m 1 -w '%{{http_code}}' http://localhost:{port}",
                    timeout=2,
                )
                code = (r.stdout or "").strip()
                return code in ("200", "204", "301", "302", "304")
            except Exception:
                return False

        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            fe_result, api_result = await asyncio.gather(
                asyncio.to_thread(probe, session.preview_port),
                asyncio.to_thread(probe, DEFAULT_SERVER_PORT),
            )
            if not fe_ok:
                fe_ok = fe_result
            if not api_ok:
                api_ok = api_result
            if fe_ok and api_ok:
                break
            await asyncio.sleep(DEV_POLL_INTERVAL_S)

        elapsed = time.monotonic() - start

        try:
            r = await asyncio.to_thread(
                sbx.commands.run,
                f"tail -{DEV_LOG_TAIL_LINES} /tmp/dev.log 2>/dev/null",
                timeout=3,
            )
            log_tail = (r.stdout or "").strip()
        except Exception:
            log_tail = ""

        log_agent(
            "agent",
            f"FAST verify done in {elapsed:.2f}s fe={fe_ok} api={api_ok} ({attempts} probes)",
            project_id,
        )

        if fe_ok and api_ok:
            session._dev_server_up = True

        return fe_ok, api_ok, log_tail

# -----------------------------------------------------------
    # Agent turn — v18 tool-call native loop
    # -----------------------------------------------------------
    async def run_agent_turn(
        self, project_id, user_request, user_id, env_vars,
        chat_history=None, gorilla_proxy_url="", has_supabase=False,
        is_debug=False, error_context="", image_b64=None,
        on_assistant_message=None, agent_skills=None, think_mode="normal"
    ) -> Dict[str, Any]:
        self._turn_locks.setdefault(project_id, asyncio.Lock())
        async with self._turn_locks[project_id]:
            return await self._do_run_agent_turn(
                project_id, user_request, user_id, env_vars,
                chat_history, gorilla_proxy_url, has_supabase,
                is_debug, error_context, image_b64, on_assistant_message,
                agent_skills, think_mode,
            )

    async def _do_run_agent_turn(
        self, project_id, user_request, user_id, env_vars,
        chat_history, gorilla_proxy_url, has_supabase, is_debug,
        error_context, image_b64, on_assistant_message, agent_skills=None,
        think_mode="normal",
    ) -> Dict[str, Any]:
        try:
            session = await self.ensure_running(project_id, env_vars, user_id)
        except Exception as e:
            self._emit_log(project_id, "system", f"Sandbox boot failed: {e}")
            self._emit_status(project_id, "Fatal Error")
            return {"ok": False, "error": str(e), "commands": [],
                    "tokens": 0, "final_message": "", "turns": 0}

        try:
            await asyncio.to_thread(session.sandbox.commands.run, f"touch {SYNC_MARKER}")
        except Exception:
            pass

        if session.agent is None:
            session.agent = LineageAgent(project_id)
        agent = session.agent

        all_commands:   List[str] = []
        final_message             = ""
        total_tokens              = 0
        turn_count                = 0
        previous_output:  Optional[str] = None
        last_raw_output           = ""
        agent_marked_done         = False
        consecutive_no_action     = 0
        CIRCUIT_BREAKER_LIMIT     = 4
        # Base64 screenshot captured by preview_screenshot this turn, if any —
        # threaded into the NEXT agent.run() call so the vision model can see it.
        screenshot_b64_for_next_turn: Optional[str] = None

        now = time.time()
        if (now - session._tree_cached_at) > 30 or not session._cached_tree:
            session._cached_tree    = await self._read_tree_from_sandbox(project_id)
            session._tree_cached_at = time.time()
        tree = session._cached_tree

        for turn in range(MAX_TURNS_PER_REQUEST):
            turn_count = turn + 1
            log_agent("agent", f"Turn {turn_count}/{MAX_TURNS_PER_REQUEST}", project_id)

            result = await agent.run(
                user_request=user_request,
                file_tree=tree if turn == 0 else {},
                chat_history=chat_history if turn == 0 else None,
                gorilla_proxy_url=gorilla_proxy_url,
                has_supabase=has_supabase,
                is_debug=is_debug,
                error_context=error_context if turn == 0 else "",
                image_b64=image_b64 if turn == 0 else None,
                previous_command_output=previous_output,
                agent_skills=agent_skills if turn == 0 else None,
                think_mode=think_mode,
                screenshot_b64=screenshot_b64_for_next_turn,
            )
            screenshot_b64_for_next_turn = None

            # ─── BILLING FIX: use turn_tokens delta directly ─────────────
            turn_delta, total_tokens = self._extract_turn_tokens(result, total_tokens)
            if turn_delta > 0:
                try:
                    self._add_tokens(session.owner_id, turn_delta)
                    self._emit(project_id, {"type": "token_usage", "tokens": turn_delta})
                except Exception:
                    pass

            # ─── CRITICAL FIX: INTERCEPT USER ACTIONS IMMEDIATELY ─────────────
            if result.get("user_action"):
                log_agent("agent", f"Pausing loop for user action: {result['user_action']['type']}", project_id)
                self._emit_status(project_id, "Waiting for your input…")
                # Sync whatever files might have been touched before returning
                synced, deleted = await self._sync_once(project_id)
                return {
                    "ok": True, "commands": all_commands,
                    "tokens": total_tokens,
                    "final_message": result.get("message", "Waiting for input..."),
                    "turns": turn_count, "synced_files": synced,
                    "deleted_files": deleted, "preview_url": session.url if session else None,
                    "user_action": result["user_action"], # <-- THIS EXITS THE LOOP TO APP.PY
                }

            # Save plan as .gorilla/todo.md on first turn
            if turn == 0 and getattr(agent, "_plan_injected", False):
                try:
                    first_user_msg = agent.messages[1].get("content", "") if len(agent.messages) > 1 else ""
                    if isinstance(first_user_msg, str) and "follow it step by step" in first_user_msg:
                        plan_start = first_user_msg.find("# Task:")
                        if plan_start == -1:
                            plan_start = first_user_msg.find("- [ ]")
                        if plan_start > 0:
                            plan_content = first_user_msg[plan_start:]
                            for marker in ["\nRecent conversation:", "\nProject files"]:
                                idx = plan_content.find(marker)
                                if idx > 0:
                                    plan_content = plan_content[:idx]
                            await asyncio.to_thread(
                                session.sandbox.commands.run,
                                f"mkdir -p {APP_DIR}/.gorilla && "
                                f"cat > {APP_DIR}/.gorilla/todo.md << 'GORILLA_EOF'\n"
                                f"{plan_content.strip()}\nGORILLA_EOF",
                                timeout=5,
                            )
                            log_agent("agent", "Saved plan to .gorilla/todo.md", project_id)
                except Exception as e:
                    log_agent("agent", f"Failed to save todo.md: {e}", project_id)

            msg = result.get("message", "")
            if msg:
                final_message = msg
                self._emit_narration(project_id, msg)
                if on_assistant_message:
                    try:
                        on_assistant_message(msg)
                    except Exception:
                        pass

            write_files: List[Dict[str, str]] = result.get("write_files", [])
            edit_files:  List[Dict[str, str]] = result.get("edit_files",  [])
            read_calls:  List[Dict[str, Any]] = result.get("read_calls",  [])
            commands:    List[str]            = result.get("commands", [])
            parsed_internal                   = result.get("_parsed", {})

            # ── Phase 0: spawn_subagent — one or more independent delegations
            #    requested in the same turn run CONCURRENTLY (siblings), not
            #    serially. Each gets its own fresh LineageAgent + activity card.
            subagent_reqs = result.get("spawn_subagents") or []
            if subagent_reqs:
                for req in subagent_reqs:
                    all_commands.append(f"spawn_subagent:{req.get('task','')[:80]}")
                sub_results = await asyncio.gather(*[
                    self._exec_spawn_subagent(
                        project_id, session,
                        req.get("task", ""),
                        req.get("max_turns", ""),
                    )
                    for req in subagent_reqs
                ])
                sub_summaries: List[str] = []
                for i, (sub_summary, sub_turn_tokens) in enumerate(sub_results):
                    sub_summaries.append(
                        f"--- Sub-agent {i + 1}/{len(sub_results)} ---\n{sub_summary}"
                        if len(sub_results) > 1 else sub_summary
                    )
                    if sub_turn_tokens > 0:
                        try:
                            self._add_tokens(session.owner_id, sub_turn_tokens)
                            self._emit(project_id, {"type": "token_usage", "tokens": sub_turn_tokens})
                            total_tokens += sub_turn_tokens
                        except Exception:
                            pass
                combined_summary = "\n\n".join(sub_summaries)
                if parsed_internal:
                    agent.record_tool_results(
                        parsed_internal,
                        write_observation="",
                        read_observation=combined_summary,
                        bash_observation="",
                    )
                previous_output = combined_summary
                consecutive_no_action = 0
                continue

            # ── Phase 1: parallel reads ───────────────────────────────────
            read_obs = ""
            if read_calls:
                for c in read_calls:
                    all_commands.append(f"{c.get('tool','read')}:{json.dumps(c.get('params',{}))[:80]}")
                read_obs = await self._execute_read_calls_parallel(project_id, read_calls)
                if session._last_screenshot_b64:
                    screenshot_b64_for_next_turn = session._last_screenshot_b64
                    session._last_screenshot_b64 = None

            # ── Phase 2: parallel writes ──────────────────────────────────
            write_obs_parts: List[str] = []
            written_paths:   List[str] = []
            edited_paths:    List[str] = []

            if write_files:
                wp, wobs = await self._write_files_parallel(project_id, write_files)
                written_paths.extend(wp)
                if wobs: write_obs_parts.append(wobs)
                all_commands.extend(f"write_file:{wf['path']}" for wf in write_files)

            if edit_files:
                ep, eobs = await self._apply_edits_parallel(project_id, edit_files)
                edited_paths.extend(ep)
                if eobs: write_obs_parts.append(eobs)
                all_commands.extend(f"edit_file:{ef['path']}" for ef in edit_files)

            tsx_paths = [
                p for p in (written_paths + edited_paths)
                if p.endswith((".ts", ".tsx"))
            ]
            if tsx_paths:
                lint_obs = await self._lint_paths(project_id, tsx_paths)
                if lint_obs:
                    write_obs_parts.append(lint_obs)

            write_obs = "\n\n".join(write_obs_parts)

            # ── Phase 3: bash ─────────────────────────────────────────────
            bash_obs = ""

            if result.get("done", False) and not commands:
                fe_ok, api_ok, _ = await self._fast_verify_dev_server(project_id, session)
                if not fe_ok and not api_ok:
                    log_agent("agent", "mark_done blocked — both ports down, injecting observation", project_id)
                    previous_output = (
                        "OBSERVATION:\nmark_done blocked — both dev server ports (:8080 and :3000) "
                        "are not responding. Fix the error causing the crash, restart the dev server "
                        "with: cd /home/user/app && npm run dev > /tmp/dev.log 2>&1 </dev/null & disown, "
                        "then verify both ports return 200 before calling mark_done."
                    )
                    if parsed_internal:
                        agent.record_tool_results(parsed_internal, write_observation=write_obs, read_observation=read_obs, bash_observation=previous_output)
                    continue
                agent_marked_done = True
                if parsed_internal:
                    agent.record_tool_results(
                        parsed_internal,
                        write_observation=write_obs,
                        read_observation=read_obs,
                        bash_observation="",
                    )
                break

            if commands:
                # ─── RESTART-LOOP FIX (v18.1): block restart even if flag stale ───
                fixed: List[str] = []
                _dev_alive_cached: Optional[bool] = None

                for cmd in commands:
                    original = cmd
                    stripped = cmd.strip()

                    is_dev_restart = (
                        "npm run dev" in stripped
                        or re.search(r"pkill.*(vite|npm run dev|node server)", stripped)
                    )

                    if is_dev_restart:
                        if _dev_alive_cached is None:
                            _dev_alive_cached = (
                                session._dev_server_up
                                or await self._is_dev_process_alive(session)
                            )
                            if _dev_alive_cached:
                                session._dev_server_up = True

                        if _dev_alive_cached:
                            # Phrased as a plain observation, not "you are blocked" — the
                            # agent tends to narrate injected tool output back to the user
                            # verbatim-ish, and language like "skipping restart" reads as
                            # the agent being stopped by something rather than just noticing
                            # the server is already up and moving on.
                            cmd = (
                                "echo 'dev server already running on :8080 and :3000, no "
                                "restart needed. If a page still is not responding, the "
                                "issue is a code error, not the process — check /tmp/dev.log "
                                "for the actual stack trace and fix that.'"
                            )
                            log_agent("agent",
                                f"Blocked restart dance (process alive): {original[:80]}",
                                project_id)
                            fixed.append(cmd)
                            continue

                    if re.search(r"pkill\s+-f\s+['\"]?(vite|npm run dev|node server)", stripped):
                        cmd = re.sub(
                            r"pkill\s+-f\s+['\"]?(?:vite|npm run dev|node server)[^;&|]*"
                            r"(?:\s*\|\|\s*true)?",
                            "true",
                            cmd,
                        )
                        log_agent("agent", "Stripped pkill from agent bash", project_id)

                    if re.search(r"\bnpm run dev\b", cmd) and "disown" not in cmd:
                        if re.search(r"npm run dev\s*$", cmd.strip()):
                            cmd = cmd.rstrip() + " > /tmp/dev.log 2>&1 </dev/null & disown"
                        elif re.search(r"npm run dev\s*>", cmd) and "&" not in cmd:
                            cmd = cmd.rstrip() + " </dev/null & disown"
                        elif re.search(r"npm run dev\b.*&\s*$", cmd) and "disown" not in cmd:
                            cmd = cmd.rstrip() + " ; disown 2>/dev/null || true"
                        log_agent("agent", "Detached npm run dev (nohup+disown)", project_id)

                    fixed.append(cmd)
                commands = fixed

                all_commands.extend(commands)
                cmd_results = await self._execute_commands_streaming(project_id, commands)

                output_parts: List[str] = []
                for r in cmd_results:
                    stdout    = (r.get("stdout") or "").strip()
                    stderr    = (r.get("stderr") or "").strip()
                    exit_code = r.get("exit_code", 0)
                    if stdout:
                        output_parts.append(stdout[:3000])
                    if stderr:
                        output_parts.append(f"STDERR: {stderr[:1500]}")
                    if exit_code != 0:
                        output_parts.append(f"[exit code: {exit_code}]")

                bash_obs = (
                    "\n".join(output_parts)[:6000]
                    if output_parts
                    else "Command ran successfully with no output."
                )

                touched_dev = any(
                    "npm run dev" in cmd or
                    re.search(r"curl.*localhost:(8080|3000)", cmd)
                    for cmd in commands
                )
                if touched_dev:
                    bash_obs = await self._post_dev_server_checks_fast(
                        project_id, session, bash_obs
                    )

                console_errs = await self._poll_console_errors(project_id)
                if console_errs:
                    bash_obs += f"\n\n{console_errs}"
                    log_agent("agent", f"Console errors: {console_errs[:150]}", project_id)

                last_raw_output = bash_obs

            elif not (write_files or edit_files or read_calls):
                consecutive_no_action += 1
                if consecutive_no_action >= CIRCUIT_BREAKER_LIMIT:
                    self._emit_narration(
                        project_id,
                        "I've been stuck for a few turns without making progress — this usually means "
                        "the sandbox environment is degraded. Your files are safe and saved. "
                        "Try refreshing the page, or send your message again to start fresh."
                    )
                    self._emit_status(project_id, "Fatal Error")
                    log_agent("agent", f"Circuit breaker tripped after {consecutive_no_action} no-action turns", project_id)
                    break
                previous_output = (
                    "OBSERVATION:\n"
                    "No actions detected. Use read_files/grep_search to explore, "
                    "write_file/edit_file to modify code, run_bash for commands, "
                    "or mark_done if finished."
                )
                continue
            else:
                consecutive_no_action = 0

            if parsed_internal:
                agent.record_tool_results(
                    parsed_internal,
                    write_observation=write_obs,
                    read_observation=read_obs,
                    bash_observation=bash_obs,
                )

            obs_parts: List[str] = []
            if read_obs:
                obs_parts.append(read_obs)
            if write_obs:
                obs_parts.append(write_obs)
            if bash_obs:
                obs_parts.append(bash_obs)
            if not obs_parts:
                obs_parts.append("Files written successfully.")

            previous_output = "\n\n".join(obs_parts)

            if result.get("done", False):
                agent_marked_done = True
                break

        # -----------------------------------------------------------
        # Reviewer — v18.1: skip when agent marked done cleanly
        # -----------------------------------------------------------
        request_lower  = (user_request or "").lower()
        is_edit_request = any(kw in request_lower for kw in _EDIT_KEYWORDS)
        should_review = (
            not is_debug
            and turn_count > 3
            and not is_edit_request
            and not agent_marked_done
        )

        if should_review:
            try:
                current_tree = await self._read_tree_from_sandbox(project_id)
                session._cached_tree    = current_tree
                session._tree_cached_at = time.time()
                tree_summary = "\n".join(
                    f"  {p}" for p in sorted(current_tree.keys()) if not p.endswith(".b64")
                )
                review_fixes = await review_output(tree_summary, last_raw_output)
                if review_fixes:
                    self._emit_narration(project_id, "Reviewing output for issues...")
                    fix_result = await agent.run(
                        user_request=f"Code review found issues. Fix them:\n{review_fixes}",
                        file_tree={},
                        gorilla_proxy_url=gorilla_proxy_url,
                        has_supabase=has_supabase,
                        is_debug=True,
                        error_context=review_fixes,
                    )
                    fix_delta, total_tokens = self._extract_turn_tokens(fix_result, total_tokens)
                    if fix_delta > 0:
                        try:
                            self._add_tokens(session.owner_id, fix_delta)
                            self._emit(project_id, {"type": "token_usage", "tokens": fix_delta})
                        except Exception:
                            pass

                    fix_wf = fix_result.get("write_files", [])
                    fix_ef = fix_result.get("edit_files",  [])
                    fix_cmds = fix_result.get("commands",  [])
                    if fix_wf:
                        await self._write_files_parallel(project_id, fix_wf)
                    if fix_ef:
                        await self._apply_edits_parallel(project_id, fix_ef)
                    if fix_cmds:
                        all_commands.extend(fix_cmds)
                        await self._execute_commands_streaming(project_id, fix_cmds)
            except Exception as e:
                log_agent("agent", f"Reviewer error: {e}", project_id)
        elif agent_marked_done and turn_count > 3:
            log_agent("agent", "Skipping reviewer — agent marked done cleanly", project_id)

        # -----------------------------------------------------------
        # Pre-sync reset (preserved fix from v15)
        # -----------------------------------------------------------
        session = self._sessions.get(project_id)
        if session and session._agent_written_paths:
            try:
                await asyncio.to_thread(
                    session.sandbox.commands.run,
                    f"touch -t 197001010000 {SYNC_MARKER}",
                    timeout=5,
                )
                for p in session._agent_written_paths:
                    session.content_hashes.pop(p, None)
                log_agent(
                    "agent",
                    f"Pre-sync: reset marker + evicted {len(session._agent_written_paths)} hashes",
                    project_id,
                )
            except Exception as e:
                log_agent("agent", f"Pre-sync reset failed (non-fatal): {e}", project_id)

        # -----------------------------------------------------------
        # Sync
        # -----------------------------------------------------------
        self._emit_status(project_id, "Syncing to database...")
        synced, deleted = await self._sync_once(project_id)

        if session:
            session._agent_written_paths.clear()
            session._tree_cached_at = 0.0

        self._emit_log(project_id, "sync", f"Synced {synced} changed, removed {deleted} deleted")

        url = session.url if session else None
        self._emit(project_id, {"type": "sandbox_url", "url": url})

        return {
            "ok": True, "commands": all_commands,
            "tokens": total_tokens,
            "final_message": final_message or "Done.",
            "turns": turn_count, "synced_files": synced,
            "deleted_files": deleted, "preview_url": url,
            "user_action": None, # <-- Safely default back to None
        }

    # -----------------------------------------------------------
    # FAST post-dev-server check
    # -----------------------------------------------------------
    async def _post_dev_server_checks_fast(
        self, project_id: str, session: SandboxSession, current_obs: str
    ) -> str:
        obs = current_obs

        try:
            health_task = self._fast_verify_dev_server(project_id, session)

            async def _scan_errors():
                try:
                    r = await asyncio.to_thread(
                        session.sandbox.commands.run,
                        "grep -i -E 'error|failed|Cannot find|could not be resolved|SyntaxError' "
                        "/tmp/dev.log 2>/dev/null | grep -v 'node_modules' | head -30",
                        timeout=5,
                    )
                    return (r.stdout or "").strip()
                except Exception:
                    return ""

            async def _inject_drain():
                inject_cmd = (
                    f"grep -q '__gorilla_errors' {APP_DIR}/server.js 2>/dev/null || "
                    f"cat >> {APP_DIR}/server.js << 'GORILLA_EOF'\n"
                    f"// Gorilla browser error tunnel\n"
                    f"const _gErrs = [];\n"
                    f"app.post('/api/__gorilla_errors', (req, res) => {{\n"
                    f"  if (req.body) {{ _gErrs.push(req.body); if (_gErrs.length > 50) _gErrs.shift(); }}\n"
                    f"  res.json({{ok: true}});\n"
                    f"}});\n"
                    f"app.get('/api/__gorilla_errors', (req, res) => {{\n"
                    f"  res.json(_gErrs.splice(0));\n"
                    f"}});\n"
                    f"GORILLA_EOF"
                )
                try:
                    await asyncio.to_thread(session.sandbox.commands.run, inject_cmd, timeout=5)
                except Exception:
                    pass

            (fe_ok, api_ok, log_tail), vite_errors, _ = await asyncio.gather(
                health_task,
                _scan_errors(),
                _inject_drain(),
            )

            if vite_errors:
                obs += f"\n\nVITE COMPILE ERRORS:\n{vite_errors}"

            if not fe_ok and not api_ok:
                obs += (
                    f"\n\nWARNING: Neither port responded within "
                    f"{DEV_READY_TIMEOUT_S}s. Dev server may have crashed. "
                    f"Recent log:\n{log_tail[:1500]}"
                )
            elif not fe_ok:
                obs += (
                    f"\n\nWARNING: Frontend port :{session.preview_port} didn't "
                    f"respond. Check vite.config and index.html.\n"
                    f"Recent log:\n{log_tail[:1000]}"
                )
            elif not api_ok:
                obs += (
                    f"\n\nNOTE: Express :3000 didn't respond — server.js may have "
                    f"crashed or has no root route. Recent log:\n{log_tail[:1000]}"
                )
            else:
                obs += f"\n\n[health: both ports OK]"

            log_agent(
                "agent",
                f"Fast check done. fe={fe_ok} api={api_ok} errs={bool(vite_errors)}",
                project_id,
            )
        except Exception as e:
            log_agent("agent", f"Fast check failed: {e}", project_id)

        return obs

    # -----------------------------------------------------------
    # Linter helper
    # -----------------------------------------------------------
    async def _lint_paths(self, project_id: str, paths: List[str]) -> str:
        session = self._sessions.get(project_id)
        if not session:
            return ""
        written_set = set(paths)
        try:
            lint_result = await asyncio.to_thread(
                session.sandbox.commands.run,
                f"cd {APP_DIR} && npx tsc --noEmit 2>&1 | head -40",
                timeout=20,
            )
            lint_out = (lint_result.stdout or "").strip()
            if not lint_out:
                return ""
            relevant = [
                line for line in lint_out.splitlines()
                if any(p.replace("/", os.sep) in line or p in line for p in written_set)
                and ("error TS" in line or "Error" in line)
            ]
            if not relevant:
                return ""
            return "LINT ERRORS:\n" + "\n".join(relevant[:20])
        except Exception:
            return ""

    # -----------------------------------------------------------
    # Streaming executor
    # -----------------------------------------------------------
    async def _execute_commands_streaming(self, project_id, commands):
        session = self._sessions.get(project_id)
        if not session:
            return [{"command": "N/A", "stdout": "", "stderr": "Sandbox not running", "exit_code": -1}]

        session.last_activity = time.time()
        results: List[Dict[str, Any]] = []

        for cmd in commands[:MAX_COMMANDS_PER_TURN]:
            if not cmd or not cmd.strip():
                continue
            if any(cmd.startswith(p) for p in (
                "write_file:", "edit_file:", "read_files:", "list_dir:",
                "grep_search:", "glob_files:", "web_search:", "web_fetch:",
            )):
                continue

            classification = classify_command(cmd)
            activity_id    = self._next_activity_id(project_id)
            self._emit_activity_start(
                project_id, activity_id,
                classification["verb"], classification["target"],
                classification["short"],
            )

            effective = (
                cmd if (cmd.startswith("cd ") or cmd.startswith("/"))
                else f"cd {APP_DIR} && source .gorilla_env 2>/dev/null; {cmd}"
            )

            stdout_buf: List[str] = []
            stderr_buf: List[str] = []

            def on_stdout(line: str):
                if not line:
                    return
                stdout_buf.append(line)
                clean = line.rstrip("\n")[:400]
                if clean.strip():
                    self._emit_activity_chunk(project_id, activity_id, "stdout", clean)

            def on_stderr(line: str):
                if not line:
                    return
                stderr_buf.append(line)
                clean = line.rstrip("\n")[:400]
                if clean.strip():
                    self._emit_activity_chunk(project_id, activity_id, "stderr", clean)

            try:
                exit_code = await asyncio.to_thread(
                    self._run_command_with_streaming,
                    session.sandbox, effective, on_stdout, on_stderr,
                )
            except Exception as e:
                err = str(e)[:200]
                stderr_buf.append(err)
                self._emit_activity_chunk(project_id, activity_id, "stderr", err)
                exit_code = -1

            self._emit_activity_end(project_id, activity_id, exit_code)
            results.append({
                "command": cmd, "stdout": "".join(stdout_buf),
                "stderr":  "".join(stderr_buf), "exit_code": exit_code,
            })

        try:
            await asyncio.to_thread(session.sandbox.commands.run, "sync && true")
        except Exception:
            pass
        session.last_activity = time.time()
        return results

    @staticmethod
    def _run_command_with_streaming(sandbox, cmd, on_stdout, on_stderr):
        if re.search(r"\b(npm|pnpm|yarn|bun)\s+(install|i|ci|add)\b", cmd):
            tmo = 180
        elif "npm run build" in cmd or "tsc " in cmd:
            tmo = 90
        else:
            tmo = 30
        try:
            result = sandbox.commands.run(cmd, on_stdout=on_stdout, on_stderr=on_stderr, timeout=tmo)
            return getattr(result, "exit_code", 0)
        except TypeError:
            pass
        try:
            result = sandbox.commands.run(cmd, timeout=tmo)
            if result.stdout:
                for line in result.stdout.splitlines(keepends=True):
                    on_stdout(line)
            if result.stderr:
                for line in result.stderr.splitlines(keepends=True):
                    on_stderr(line)
            return getattr(result, "exit_code", 0)
        except Exception as e:
            on_stderr(str(e))
            return -1

    # -----------------------------------------------------------
    # Batched tree read
    # -----------------------------------------------------------
    async def _read_tree_from_sandbox(self, project_id: str) -> Dict[str, str]:
        session = self._sessions.get(project_id)
        if not session:
            return {}

        dump_cmd = (
            f"find {APP_DIR} -type f "
            f"-not -path '*/node_modules/*' -not -path '*/.git/*' "
            f"-not -path '*/dist/*' -not -name 'package-lock.json' "
            f"-not -name '*.lock' -size -500k -print0 | "
            f'xargs -0 -I {{}} sh -c \''
            f'echo "{FILE_READ_SENTINEL}{{}}"; '
            f'echo "{FILE_CONTENT_SENTINEL}"; '
            f'cat "{{}}" 2>/dev/null; echo ""\''
        )

        try:
            result = await asyncio.to_thread(session.sandbox.commands.run, dump_cmd)
            output = result.stdout or ""
        except Exception:
            return await self._read_tree_slow(project_id)

        if not output.strip():
            return {}

        tree: Dict[str, str] = {}
        for chunk in output.split(FILE_READ_SENTINEL)[1:]:
            if FILE_CONTENT_SENTINEL not in chunk:
                continue
            header, _, body = chunk.partition(FILE_CONTENT_SENTINEL)
            path            = _strip_app_prefix(header)
            if not path:
                continue
            content = body[:-1] if body.endswith("\n") else body
            if "\x00" in content[:1000]:
                continue
            tree[path] = content
        return tree

    async def _read_tree_slow(self, project_id: str) -> Dict[str, str]:
        session = self._sessions.get(project_id)
        if not session:
            return {}
        try:
            listing = await asyncio.to_thread(
                session.sandbox.commands.run,
                f"find {APP_DIR} -type f -not -path '*/node_modules/*' "
                f"-not -path '*/.git/*' -not -path '*/dist/*' "
                f"-not -name 'package-lock.json' -not -name '*.lock' -size -500k",
            )
        except Exception:
            return {}
        if not listing.stdout:
            return {}

        paths = [
            _strip_app_prefix(p)
            for p in listing.stdout.strip().split("\n") if p.strip()
        ]

        tree: Dict[str, str] = {}
        for rel in paths[:400]:
            if not rel:
                continue
            try:
                r = await asyncio.to_thread(
                    session.sandbox.commands.run,
                    f"cat '{APP_DIR}/{rel}' 2>/dev/null"
                )
                content = r.stdout or ""
                if "\x00" in content[:1000]:
                    continue
                tree[rel] = content
            except Exception:
                continue
        return tree

    # -----------------------------------------------------------
    # Batched sync (preserved fix from v15)
    # -----------------------------------------------------------
    async def _sync_once(self, project_id: str) -> Tuple[int, int]:
        session = self._sessions.get(project_id)
        if not session:
            return (0, 0)
        try:
            await asyncio.to_thread(session.sandbox.commands.run, "sync")
        except Exception:
            pass

        changed_dump_cmd = (
            f"find {APP_DIR} -type f -newer {SYNC_MARKER} "
            f"-not -path '*/node_modules/*' -not -path '*/.git/*' "
            f"-not -path '*/dist/*' -not -name 'package-lock.json' "
            f"-not -name '*.lock' -size -500k -print0 | "
            f'xargs -0 -I {{}} sh -c \''
            f'echo "{FILE_READ_SENTINEL}{{}}"; '
            f'echo "{FILE_CONTENT_SENTINEL}"; '
            f'cat "{{}}" 2>/dev/null; echo ""\''
        )

        try:
            result = await asyncio.to_thread(session.sandbox.commands.run, changed_dump_cmd)
            dump   = result.stdout or ""
        except Exception as e:
            self._emit_log(project_id, "sync", f"Sync failed: {e}")
            return (0, 0)

        changed_files: Dict[str, str] = {}
        if dump.strip():
            for chunk in dump.split(FILE_READ_SENTINEL)[1:]:
                if FILE_CONTENT_SENTINEL not in chunk:
                    continue
                header, _, body = chunk.partition(FILE_CONTENT_SENTINEL)
                path            = _strip_app_prefix(header)
                if not path:
                    continue
                content = body[:-1] if body.endswith("\n") else body
                if "\x00" in content[:1000]:
                    continue
                if len(content) > 500_000:
                    continue
                changed_files[path] = content

        rows:                  List[Dict[str, Any]] = []
        current_sandbox_paths: Set[str]             = set(changed_files.keys())

        for rel, content in changed_files.items():
            h = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()
            if session.content_hashes.get(rel) == h:
                continue
            session.content_hashes[rel] = h
            rows.append({"project_id": project_id, "path": rel, "content": content})

        try:
            all_listing = await asyncio.to_thread(
                session.sandbox.commands.run,
                f"find {APP_DIR} -type f -not -path '*/node_modules/*' "
                f"-not -path '*/.git/*' -not -path '*/dist/*' "
                f"-not -name 'package-lock.json' -not -name '*.lock'",
            )
            if all_listing.stdout:
                for p in all_listing.stdout.strip().split("\n"):
                    stripped = _strip_app_prefix(p)
                    if stripped:
                        current_sandbox_paths.add(stripped)
        except Exception:
            pass

        if rows:
            try:
                if self._db_upsert_batch:
                    self._db_upsert_batch("files", rows, on_conflict="project_id,path")
                else:
                    for row in rows:
                        self._db_upsert("files", row, on_conflict="project_id,path")
                for row in rows:
                    self._emit_file_changed(project_id, row["path"])
            except Exception as e:
                self._emit_log(project_id, "sync", f"Upsert error: {e}")

        deleted_count = 0
        try:
            db_paths  = set(self._list_db_paths(project_id) or [])
            to_delete = db_paths - current_sandbox_paths - {".env", ".gorilla_env"}
            for p in to_delete:
                try:
                    self._db_delete("files", {"project_id": project_id, "path": p})
                    self._emit_file_deleted(project_id, p)
                    session.content_hashes.pop(p, None)
                    deleted_count += 1
                except Exception:
                    pass
        except Exception:
            pass

        try:
            await asyncio.to_thread(session.sandbox.commands.run, f"touch {SYNC_MARKER}")
        except Exception:
            pass

        return (len(rows), deleted_count)

    # -----------------------------------------------------------
    # Dev server (for /sandbox/start endpoint)
    # -----------------------------------------------------------
    async def start_dev_server(self, project_id: str) -> Tuple[Optional[str], Optional[str]]:
        session = self._sessions.get(project_id)
        if not session:
            return (None, None)
        session.last_activity = time.time()

        fe_ok, api_ok, _ = await self._fast_verify_dev_server(project_id, session)
        if fe_ok:
            self._emit(project_id, {"type": "sandbox_url", "url": session.url})
            return (session.url, None)

        try:
            await asyncio.to_thread(session.sandbox.commands.run,
                "pkill -f vite || true; pkill -f 'npm run dev' || true; pkill -f 'node server' || true")
            await asyncio.sleep(1)
        except Exception as e:
            self._emit_log(project_id, "system", f"Dev server restart (kill step) failed: {e}")

        try:
            await asyncio.to_thread(session.sandbox.commands.run,
                f"cd {APP_DIR} && nohup npm run dev > /tmp/dev.log 2>&1 &")
        except Exception as e:
            self._emit_log(project_id, "system", f"Dev server restart (spawn step) failed: {e}")

        fe_ok, api_ok, _ = await self._fast_verify_dev_server(project_id, session)
        session._dev_server_up = bool(fe_ok or api_ok)
        if not session._dev_server_up:
            self._emit_log(project_id, "system", "Dev server did not come back up after restart.")

        self._emit(project_id, {"type": "sandbox_url", "url": session.url})
        return (session.url, None)

    # -----------------------------------------------------------
    # File write/delete for editor bridge
    # -----------------------------------------------------------
    async def write_file(self, project_id: str, rel_path: str, content: str) -> bool:
        session = self._sessions.get(project_id)
        if not session:
            return False
        try:
            ok = await asyncio.to_thread(self._write_one_file_sync, session.sandbox, rel_path, content)
            if ok:
                session.content_hashes[rel_path] = hashlib.md5(
                    content.encode("utf-8", errors="replace")
                ).hexdigest()
                session._tree_cached_at = 0.0
                session.last_activity   = time.time()
            return ok
        except Exception as e:
            self._emit_log(project_id, "system", f"Write failed for {rel_path}: {e}")
            return False

    async def delete_file(self, project_id: str, rel_path: str) -> bool:
        session = self._sessions.get(project_id)
        if not session:
            return False
        full = _resolve_app_path(rel_path)
        if full == APP_DIR:
            # Refuse to no-op-delete the app root itself.
            return False
        try:
            await asyncio.to_thread(session.sandbox.commands.run, f"rm -f '{full}'")
            session.content_hashes.pop(rel_path, None)
            session._tree_cached_at = 0.0
            session.last_activity   = time.time()
            return True
        except Exception as e:
            self._emit_log(project_id, "system", f"Delete failed for {rel_path}: {e}")
            return False

    # -----------------------------------------------------------
    # Billing + idle monitor
    # -----------------------------------------------------------
    async def _billing_loop(self, project_id: str) -> None:
        accumulated = 0
        try:
            while True:
                await asyncio.sleep(BILLING_TICK_S)
                session = self._sessions.get(project_id)
                if not session:
                    return
                now     = time.time()
                elapsed = (now - session.last_bill_at) / 3600.0
                if elapsed <= 0:
                    continue
                prorated = int(BILLING_TOKENS_PER_HOUR * elapsed)
                if prorated <= 0:
                    continue
                accumulated              += prorated
                session.total_billed_tokens += prorated
                session.last_bill_at     = now
                if accumulated >= int(BILLING_TOKENS_PER_HOUR / 360):
                    try:
                        self._add_tokens(session.owner_id, accumulated)
                        accumulated = 0
                    except Exception as e:
                        print(f"⚠️ Billing error: {e}")
        except asyncio.CancelledError:
            if accumulated > 0:
                session = self._sessions.get(project_id)
                if session:
                    try:
                        self._add_tokens(session.owner_id, accumulated)
                    except Exception:
                        pass

    async def _idle_monitor(self) -> None:
        while True:
            try:
                now = time.time()
                for pid in [
                    p for p, s in list(self._sessions.items())
                    if (now - s.last_activity) > IDLE_TIMEOUT_S
                ]:
                    print(f"💤 Idle kill: {pid}")
                    try:
                        await self._sync_once(pid)
                    except Exception:
                        pass
                    await self.kill(pid)
            except Exception as e:
                print(f"Idle monitor error: {e}")
            await asyncio.sleep(30)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _glob_to_regex(pattern: str) -> str:
    p = pattern.strip().lstrip("./")
    def brace_repl(m):
        inner = m.group(1)
        alts  = inner.split(",")
        return "(" + "|".join(re.escape(a.strip()) for a in alts) + ")"
    p = re.sub(r"\{([^{}]+)\}", brace_repl, p)

    p = p.replace("**", "\x00DOUBLE\x00")
    p = re.sub(r"([.+^${}\[\]\\])", r"\\\1", p)
    p = p.replace("*", "[^/]*")
    p = p.replace("?", "[^/]")
    p = p.replace("\x00DOUBLE\x00", ".*")
    return p + "$"


sandbox_manager: Optional[E2BSandboxManager] = None