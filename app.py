from __future__ import annotations

import os
import json
import time
import uuid
import asyncio
import secrets
import mimetypes
import traceback
import random
import string
import re
import urllib.parse
import subprocess
import tempfile
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Set

import httpx
import resend
from fastapi import FastAPI, Request, HTTPException, Form, BackgroundTasks
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
    FileResponse,
    JSONResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from dotenv import load_dotenv
from supabase import create_client, Client



# Load Environment Variables
load_dotenv()

from backend.e2b_sandbox import E2BSandboxManager
from backend.ai.lineage_agent import (
    set_log_callback as lineage_set_log,
    _render_token_limit_message,
    _append_history,
    _get_history,
    clear_history,
)


# ==========================================================================
# CONSTANTS & PATHS
# ==========================================================================
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# 1. Frontend Paths
FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")
if not os.path.isdir(FRONTEND_DIR):
    # Fallback: If running from inside 'backend' folder, go up one level
    ROOT_DIR = os.path.dirname(ROOT_DIR)
    FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")

FRONTEND_TEMPLATES_DIR = os.path.join(FRONTEND_DIR, "templates")
FRONTEND_STYLES_DIR = os.path.join(FRONTEND_DIR, "styles")

# 2. Boilerplate Path (Critical for Project Creation)
# We check multiple common locations to prevent "directory not found" errors
possible_bp_paths = [
    os.path.join(ROOT_DIR, "backend", "boilerplate"),
    os.path.join(ROOT_DIR, "boilerplate"),
    os.path.join(os.getcwd(), "backend", "boilerplate"),
]

BOILERPLATE_DIR = None
for path in possible_bp_paths:
    if os.path.isdir(path):
        BOILERPLATE_DIR = path
        break

# If still not found, default to backend/boilerplate and warn
if not BOILERPLATE_DIR:
    BOILERPLATE_DIR = os.path.join(ROOT_DIR, "backend", "boilerplate")
    print(f"⚠️ WARNING: Boilerplate directory not found. Expected at: {BOILERPLATE_DIR}")

# 3. Dev Mode & Limits
DEV_MODE = os.getenv("DEV_MODE", "1") == "1"
DEFAULT_TOKEN_LIMIT = int(os.getenv("MONTHLY_TOKEN_LIMIT", "500000"))

# 4. Google Auth Keys
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "https://precious-lorianne-walnutlabs-bc5e943d.koyeb.app/auth/google/callback")

# ==========================================================================
# CONFIGURATION: RESEND & SUPABASE (CRITICAL FIX)
# ==========================================================================

# 1. Resend (Email)
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

# 2. Supabase (Database)
SUPABASE_URL = os.getenv("SUPABASE_URL")
# [CRITICAL] We MUST use the SERVICE_ROLE_KEY to bypass RLS policies in the backend
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("CRITICAL ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env to allow project creation.")

# Initialize Supabase with Admin Privileges
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==========================================================================
# GLOBAL STATE
# ==========================================================================

# In-Memory OTP Store (for Signup/Login verification)
PENDING_SIGNUPS = {}

# Runtime Management State
_BOOTING_PROJECTS: Set[str] = set()
_LAST_ACCESS: Dict[str, float] = {} 
SHUTDOWN_TIMEOUT_SECONDS = 600 # 10 Minutes

# GLOBAL STATE
PENDING_SIGNUPS = {}
_BOOTING_PROJECTS: Set[str] = set()
_LAST_ACCESS: Dict[str, float] = {}
SHUTDOWN_TIMEOUT_SECONDS = 600
_AGENT_PAUSE_EVENTS: Dict[str, asyncio.Event] = {}
_AGENT_PAUSE_DATA:   Dict[str, Any]           = {}
_sandbox_manager = None  # ← ADD THIS LINE

# Serializes concurrent boot attempts per project (frontend tab-click racing
# the agent's own ensure_running was occasionally double-creating sandboxes).
_SANDBOX_BOOT_LOCKS: Dict[str, asyncio.Lock] = {}

def _sandbox_boot_lock(project_id: str) -> asyncio.Lock:
    # Safe under asyncio's single-threaded event loop: this function has no
    # `await` between the get and the set, so no other coroutine can run in
    # between and create a second lock for the same project_id.
    return _SANDBOX_BOOT_LOCKS.setdefault(project_id, asyncio.Lock())

# ==========================================================================
# APP INITIALIZATION & LIFECYCLE
# ==========================================================================
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="Gorilla Backend",
    docs_url="/api/docs",       # <--- MOVES the Swagger UI to /api/docs
    redoc_url="/api/redoc",     # <--- MOVES ReDoc to /api/redoc
    openapi_url="/api/openapi.json" # <--- MOVES the JSON schema
)

# Session secret MUST be stable across restarts/deploys, or every deploy logs
# everyone out and the SSE route starts 403→redirect looping for old cookies.
# Prefer AUTH_SECRET_KEY; fall back to a deterministic derivation so a missing
# env var degrades gracefully instead of randomly.
_auth_secret = os.getenv("AUTH_SECRET_KEY")
if not _auth_secret:
    _auth_secret = hashlib.sha256(
        f"gorilla-session-v1::{SUPABASE_KEY}".encode()
    ).hexdigest()
    print("⚠️ AUTH_SECRET_KEY not set — using a derived stable fallback. "
          "Set AUTH_SECRET_KEY in Koyeb env vars for production.")

app.add_middleware(
    SessionMiddleware,
    secret_key=_auth_secret,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_permissive_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = "frame-src *; frame-ancestors *; child-src *;"
    response.headers["Cross-Origin-Embedder-Policy"] = "unsafe-none"
    response.headers["Cross-Origin-Opener-Policy"] = "unsafe-none"
    response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    return response

if os.path.isdir(FRONTEND_STYLES_DIR):
    app.mount("/styles", StaticFiles(directory=FRONTEND_STYLES_DIR), name="styles")

templates = Jinja2Templates(directory=FRONTEND_TEMPLATES_DIR)


# ==========================================================================
# EXCEPTION HANDLERS
# ==========================================================================
@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc):
    if exc.status_code == 404:
        # Check if template exists, fallback if not
        if os.path.exists(os.path.join(FRONTEND_TEMPLATES_DIR, "404.html")):
             return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
        return HTMLResponse("<h1>404 - Not Found</h1>", status_code=404)
    
    # For other errors, return JSON
    return JSONResponse({"detail": str(exc.detail)}, status_code=exc.status_code)

@app.exception_handler(403)
async def custom_403_handler(request: Request, __):
    return RedirectResponse(url="/", status_code=303)
# ==========================================================================
# DATABASE HELPERS (Safe Access)
# ==========================================================================
def db_select_one(table: str, match: dict, select="*"):
    """Safe wrapper to fetch a single row."""
    if not supabase: return None
    try:
        q = supabase.table(table).select(select)
        for k, v in match.items(): q = q.eq(k, v)
        res = q.maybe_single().execute()
        return res.data if res else None
    except Exception: return None

# ==========================================================================
# DB HELPERS (Integrity Guard)
# ==========================================================================
import posixpath
 
def _sanitize_file_path(raw: str) -> str:
    """Prevent path traversal. Raises 400 on suspicious input."""
    clean = posixpath.normpath(raw.replace("\\", "/")).lstrip("/")
    if ".." in clean.split("/"):
        raise HTTPException(status_code=400, detail="Invalid file path.")
    if clean.startswith("/") or "\x00" in clean:
        raise HTTPException(status_code=400, detail="Invalid file path.")
    return clean

def db_upsert(table: str, data: Dict[str, Any], on_conflict: str = "id"):
    """
    Enhanced upsert that blocks binary/giant files from entering the DB.
    """
    path = data.get("path", "")
    content = data.get("content", "")

    # 1. 🛑 THE LOCKFILE BLOCKER
    # Blocks the Agent or Boilerplate from saving giant lockfiles
    if path and any(x in path for x in ["package-lock.json", "yarn.lock", "pnpm-lock.yaml"]):
        print(f"⏩ Skipping {path} (Handled by WebContainer runtime)")
        return None

    # 2. 🛑 BINARY FILTER
    # Prevents binary data from being stored in the text column
    if path and path.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.ico', '.pdf', '.zip')):
        print(f"⏩ Skipping binary file: {path}")
        return None

    # 3. 🛑 INTEGRITY CHECK
    # Ensure we aren't saving empty strings as files
    if table == "files" and not content:
        print(f"⚠️ Warning: Attempting to save empty file content for {path}")

    try:
        return supabase.table(table).upsert(data, on_conflict=on_conflict).execute()
    except Exception as e:
        print(f"❌ DB Upsert Error for {path}: {e}")
        return None

def db_delete(table: str, filters: dict) -> None:
    """Delete rows matching filters. Used by the sandbox manager to remove
    files that the agent deleted in the sandbox (rm commands)."""
    try:
        q = supabase.table(table).delete()
        for k, v in filters.items():
            q = q.eq(k, v)
        q.execute()
    except Exception as e:
        print(f"⚠️ db_delete({table}) failed: {e}")
 
 
def db_upsert_batch(table: str, rows: list, on_conflict: str = "") -> None:
    """Batch upsert — one HTTP request for many rows (loophole 21)."""
    if not rows:
        return
    try:
        q = supabase.table(table).upsert(rows, on_conflict=on_conflict)
        q.execute()
    except Exception as e:
        # Fallback to per-row if the batch fails (e.g. one bad row)
        print(f"⚠️ batch upsert failed, falling back to per-row: {e}")
        for row in rows:
            try:
                supabase.table(table).upsert(row, on_conflict=on_conflict).execute()
            except Exception as e2:
                print(f"⚠️ per-row upsert failed for {row.get('path')}: {e2}")
 
 
def db_list_file_paths(project_id: str) -> set:
    """Return every file path the DB currently has for this project.
    Used to detect which files the agent deleted."""
    try:
        res = supabase.table("files").select("path").eq("project_id", project_id).execute()
        return {row["path"] for row in (res.data or []) if row.get("path")}
    except Exception as e:
        print(f"⚠️ db_list_file_paths failed: {e}")
        return set()
 
 
def emit_file_deleted(project_id: str, path: str) -> None:
    """SSE event so the frontend file tree can remove the node."""
    progress_bus.emit(project_id, {"type": "file_deleted", "path": path})

# ==========================================================================
# TOKEN MANAGEMENT LOGIC
# ==========================================================================
def get_token_usage_and_limit(user_id: str) -> Tuple[int, int]:
    """Fetches used tokens and total limit from DB."""
    user = db_select_one("users", {"id": user_id}, "tokens_used, tokens_limit")
    if not user:
        return 0, DEFAULT_TOKEN_LIMIT
    
    used = int(user.get("tokens_used") or 0)
    limit = int(user.get("tokens_limit") or DEFAULT_TOKEN_LIMIT)
    return used, limit

def add_monthly_tokens(user_id: str, tokens_to_add: int) -> int:
    """Adds tokens used. If user missing, creates them automatically."""
    if tokens_to_add <= 0:
        used, _ = get_token_usage_and_limit(user_id)
        return used
    
    try:
        current_used, current_limit = get_token_usage_and_limit(user_id)
        new_total = current_used + int(tokens_to_add)
        
        db_upsert(
            "users",
            {
                "id": user_id, 
                "tokens_used": new_total,
                # Preserve limit if exists, else default
                "updated_at": "now()"
            }, 
            on_conflict="id"
        )
        return new_total
    except Exception as e:
        print(f"Token Update Error: {e}")
        return 0

def _maybe_emit_token_warning(project_id: str, user_id: str) -> None:
    """Emit a save-your-work nudge at 500k and 800k tokens used."""
    try:
        used, limit = get_token_usage_and_limit(user_id)
        pct = used / limit if limit else 0
        if 0.98 <= pct:
            return
        if 0.80 <= pct < 0.82:
            emit_log(project_id, "assistant", (
                "⚠️ **You've used 80% of your token budget.** Now is a good time to push your code to GitHub "
                "using the **GitHub button** in the top-right of the editor, or hit **Deploy** to save your work "
                "before you run out."
            ))
        elif 0.50 <= pct < 0.52:
            emit_log(project_id, "assistant", (
                "💡 **Halfway through your token budget.** Your project is saved automatically, but if you want "
                "a permanent copy push it to GitHub now using the **GitHub** button top-right."
            ))
    except Exception as e:
        print(f"⚠️ token warning check failed: {e}")

def enforce_token_limit_or_raise(user_id: str) -> Tuple[int, int]:
    """Checks usage against the user's specific limit."""
    used, limit = get_token_usage_and_limit(user_id)
    remaining = max(0, limit - used)
    
    if used >= limit:
        raise HTTPException(
            status_code=402,
            detail=f"Token limit reached ({limit}). Used={used}. Please upgrade or top-up.",
        )
    return used, remaining

def set_user_plan_and_limit(user_id: str, plan: str, limit: int):
    """Updates user plan and token limit (for upgrades)."""
    # Force direct update
    db_upsert(
        "users",
        {
            "id": user_id,
            "plan": plan,
            "tokens_limit": limit,
            "updated_at": "now()"
        },
        on_conflict="id"
    )

def decrease_tokens_used(user_id: str, amount: int):
    """'Top up' by reducing the 'used' counter (simulates adding balance)."""
    used, _ = get_token_usage_and_limit(user_id)
    new_used = max(0, used - amount)
    
    db_upsert(
        "users",
        {"id": user_id, "tokens_used": new_used, "updated_at": "now()"},
        on_conflict="id"
    )

# ==========================================================================
# AUTHENTICATION & USER HELPERS
# ==========================================================================
_DEV_NAMESPACE = uuid.UUID("2b48c7cc-51c8-4b50-a5c6-2c4ac3f26cb1")

def _stable_user_id_for_email(email: str) -> str:
    """Generates a consistent UUIDv5 based on email for Dev Mode."""
    e = (email or "").strip().lower()
    if not e: 
        return str(uuid.uuid4())
    return str(uuid.uuid5(_DEV_NAMESPACE, e))

def ensure_public_user(user_id: str, email: str) -> None:
    """Ensures the user exists in the public.users table WITHOUT overwriting existing data."""
    try:
        # --- FIX: Check existence first! ---
        existing = db_select_one("users", {"id": user_id}, "id")
        if existing:
            return # User exists, do NOT touch their plan/limits
            
        # Default new users to free plan and default limit
        db_upsert(
            "users", 
            {"id": user_id, "email": email, "plan": "free", "tokens_limit": DEFAULT_TOKEN_LIMIT}, 
            on_conflict="id"
        )
    except Exception:
        pass

def get_current_user(request: Request) -> Dict[str, Any]:
    user = request.session.get("user")
 
    # 1. No user in session? 403 — the exception handler will redirect home.
    if not user or not user.get("id"):
        raise HTTPException(status_code=403, detail="Invalid user session.")
 
    # 2. KILLSWITCH: catch old dev@local cookies and destroy them
    if user.get("email") == "dev@local":
        request.session.clear()
        raise HTTPException(status_code=403, detail="Invalid user session.")
 
    # 3. Valid, real user — ensure public record exists
    ensure_public_user(user["id"], user.get("email") or "unknown@local")
    return user

def _require_project_owner(user: Dict[str, Any], project_id: str) -> None:
    """Verifies that the current user owns the project."""
    res = db_select_one("projects", {"id": project_id}, "id, owner_id")
    
    if not res:
        raise HTTPException(status_code=404, detail="Project not found")
        
    if res.get("owner_id") != user["id"]:
        # We use 403 to signal "You aren't allowed here" 
        # JavaScript will catch this and trigger the redirect.
        raise HTTPException(status_code=403, detail="Unauthorized Access")

# --- RESEND EMAIL LOGIC ---

import resend # Ensure you have this imported

# Make sure you assign the API key to the resend library somewhere in your environment setup:
# resend.api_key = RESEND_API_KEY

def send_otp_email(to_email: str, code: str):
    if not RESEND_API_KEY:
        print(f"⚠️ Resend Key missing. Code for {to_email}: {code}")
        return
    
    try:
        # 1. Set up the parameters
        params = {
            "from": "Gor://a Auth Verification <auth@gorillabuilder.dev>", # Use your verified domain
            "to": [to_email],
            "subject": "Your Verification Code for Gor://a Builder",
            "html": f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Verification Email</title>
            </head>
            <body style="margin: 0; padding: 0; background-color: #0b1020; font-family: Monospace, sans-serif;">
                <div style="width: 100%; padding: 40px 0; background-color: #0b1020;">
                    <div style="max-width: 420px; margin: 0 auto; background-color: #0f1530; padding: 40px; border-radius: 18px; color: #ffffff;">
                        <h1 style="margin: 0 0 10px; font-size: 24px; font-weight: 400; letter-spacing: -0.3px;">Welcome to Gor://a</h1>
                        <p style="margin: 0 0 30px; opacity: 0.7; font-size: 14px; line-height: 1.5;">
                            Start building AI apps faster. Use the code below to verify your account.
                        </p>
                        <div style="background-color: #141a3a; padding: 20px; border-radius: 10px; text-align: center; margin-bottom: 30px;">
                            <span style="display: block; font-size: 12px; opacity: 0.5; margin-bottom: 5px; text-transform: uppercase; letter-spacing: 1px;">Verification Code</span>
                            <strong style="font-size: 32px; color: #3b6cff; letter-spacing: 5px;">{code}</strong>
                        </div>
                        <p style="font-size: 12px; opacity: 0.4; text-align: center; margin-top: 40px;">
                            If you didn't request this, you can safely ignore this email.
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """,
        }

        # 2. Actually trigger the email send via the Resend API
        email = resend.Emails.send(params)
        print(f"✅ OTP email sent to {to_email}")

    except Exception as e:
        print(f"❌ Resend Error: {e}")

# ==========================================================================
# FIGMA OAUTH INTEGRATION
# ==========================================================================
import secrets
import httpx
import urllib.parse

FIGMA_CLIENT_ID = os.getenv("FIGMA_CLIENT_ID")
FIGMA_CLIENT_SECRET = os.getenv("FIGMA_CLIENT_SECRET")
# e.g., https://app.gorillabuilder.dev/auth/figma/callback (must match Figma exact)
FIGMA_REDIRECT_URI = os.getenv("FIGMA_REDIRECT_URI")

# ==========================================================================
# PUBLIC ROUTES (Templates & Redirects)
# ==========================================================================

# Define pages that don't require authentication (except root which has logic)
PUBLIC_PAGES = {
    "/login": "auth/login.html",
    "/signup": "auth/signup.html",
    "/forgot-password": "auth/forgot_password.html",
    "/checkout/tokens": "freemium/checkout/tokens.html",
    "/checkout/premium": "freemium/checkout/premium.html",
    "/privacy-policy": "legal/privacy-policy.html",
    "/terms-of-service": "legal/terms-of-service.html"
}
# In app.py
from fastapi.staticfiles import StaticFiles # Make sure this is imported

app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

# 1. ROOT ROUTE REDIRECT LOGIC
@app.get("/")
async def root_redirect(request: Request):
    """
    Redirects based on auth status:
    - Logged in -> /dashboard
    - Not logged in (or dev/local) -> /signup
    """
    user = get_current_user_safe(request) # Helper function to get user without raising error
    
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    
    # Default landing is now the signup page
    return RedirectResponse("/signup", status_code=303)

# 2. GENERATE HANDLERS FOR OTHER PUBLIC PAGES
for route, template_name in PUBLIC_PAGES.items():
    # Skip creating a handler for root since we defined it manually above
    if route == "/": continue

    def make_handler(t_name):
        async def handler(request: Request):
            # Pass common variables like 'step' for signup flow
            return templates.TemplateResponse(t_name, {"request": request, "step": "initial"})
        return handler
        
    app.get(route, response_class=HTMLResponse)(make_handler(template_name))

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    p = os.path.join(FRONTEND_DIR, "assets", "favicon.png")
    if os.path.exists(p): 
        return FileResponse(p)
    raise HTTPException(status_code=404)


# ==========================================================================
# 📚 DOCUMENTATION ROUTES
# ==========================================================================

@app.get("/docs/{page}", response_class=HTMLResponse)
async def docs_page(request: Request, page: str):
    
    # The complete 44-page Gor://a Builder Master Architecture
    valid_pages = [
        # Part 1: Getting Started
        "intro", "quickstart", "dashboard", "tiers", "account-settings",

        # Part 2: Agent Skills
        "skills-overview", "visual-prefs", "ui-frameworks", "code-style", "agent-personality", "golden-rules",

        # Part 3: Designing & Prompting
        "perfect-prompt", "image-references", "figma-overview", "figma-import", "github-import", "ideas-chips",

        # Part 4: In-Browser IDE
        "editor-interface", "webcontainers", "auto-healing", "sub-agents", "manual-editing", "file-tree", "export-zip",
        
        # Part 5: Full-Stack & Databases
        "backend-overview", "supabase-link", "db-provisioning", "sql-migrations", "db-healing",
        
        # Part 6: Built-In Integrations
        "auth-gateway", "ai-integrations", "image-gen", "voice-stt", "bg-removal",
        
        # Part 7: Deployment Pipeline
        "deploy-prep", "github-link", "vercel-opt", "github-push", "vercel-deploy",
        
        # Part 8: Economy & Tokens
        "api-tokens", "error-402", "spin-wheel", "monke-negotiator", "enterprise-tier"
    ]
    
    if page not in valid_pages:
        return RedirectResponse("/docs/intro")
        
    # Safely grab the user so the docs can render the golden "PRO" theme if applicable
    user = get_current_user_safe(request)
        
    return templates.TemplateResponse(
        f"docs/{page}.html", 
        {
            "request": request, 
            "page": page,
            "user": user  # Injects user state into base.html
        }
    )

@app.get("/docs", response_class=HTMLResponse)
async def docs_root():
    return RedirectResponse("/docs/intro")


# ==========================================================================
# AUTHENTICATION ROUTES (Consolidated & Secured)
# ==========================================================================
import os
import random
import string
import time
import secrets
import httpx
from fastapi import APIRouter, Request, Form, BackgroundTasks, HTTPException, Response
from fastapi.responses import RedirectResponse

# --- OAuth Environment Variables ---
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
GITHUB_APPAUTH_CLIENT_ID = os.getenv("GITHUB_APPAUTH_CLIENT_ID")
GITHUB_APPAUTH_CLIENT_SECRET = os.getenv("GITHUB_APPAUTH_CLIENT_SECRET")
GITHUB_REDIRECT_URI = os.getenv("GITHUB_REDIRECT_URI")

# Global memory for storing OTPs during signup flow
PENDING_SIGNUPS = {}

def get_current_user_safe(request: Request):
    """
    Helper to safely check for a user session without raising an exception.
    Used for the root redirect logic.
    """
    try:
        # Check Supabase cookie first
        token = request.cookies.get("sb_access_token")
        if token:
             user = supabase.auth.get_user(token)
             if user: return user
        
        # Fallback to session (for dev/google/github auth) if used
        if "user" in request.session:
            return request.session["user"]
            
    except:
        pass
    return None

def _ensure_gorilla_api_key(user_id: str):
    """
    PHASE 1: AI PROXY GATEWAY
    Checks if a user has a gb_live_ API key. If not, generates and saves one.
    """
    try:
        user_data = db_select_one("users", {"id": user_id}, "gorilla_api_key")
        if not user_data or not user_data.get("gorilla_api_key"):
            # Generate a secure 48-character hex string (total key length ~56 chars)
            new_key = f"gb_live_{secrets.token_hex(24)}"
            supabase.table("users").update({"gorilla_api_key": new_key}).eq("id", user_id).execute()
            print(f"🔑 Generated new Gorilla API Key for user: {user_id}")
    except Exception as e:
        print(f"⚠️ Failed to generate gorilla_api_key for {user_id}: {e}")


# --------------------------------------------------------------------------
# 1. SIGNUP FLOW (Secure)
# --------------------------------------------------------------------------


# ==========================================================================
# PROJECT ROUTES (RLS-COMPLIANT)
# ==========================================================================
import io
import zipfile
import urllib.parse
import re
import os
import asyncio
import traceback
import json
import time
import mimetypes
import tempfile
import subprocess
import base64 # Added for handling image data
import httpx # Needed for the background task API call
from typing import Dict, Any, List, Optional
from fastapi import Request, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, Response, JSONResponse

# --- NEW: BACKGROUND TASK FOR SNAPSHOTS ---
async def generate_project_snapshot(project_id: str, prompt: str, user_api_key: str):
    try:
        proxy_base = os.getenv("FILE_API_BASE_URL", "").rstrip("/")
        if not proxy_base:
            print(f"FILE_API_BASE_URL not set; skipping snapshot for {project_id}")
            return
        payload = {"prompt": f"Professional web UI dashboard preview: {prompt}", "samples": 1}
        headers = {
            "Authorization": f"Bearer {user_api_key}",
            "Content-Type": "application/json",
            "ngrok-skip-browser-warning": "true",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{proxy_base}/api/v1/images/generations",
                json=payload, headers=headers, timeout=60.0,
            )
            if resp.status_code != 200:
                print(f"Proxy Error ({resp.status_code}): {resp.text}")
                return
            data = resp.json()
            snapshot_b64_data = None
            if isinstance(data, list) and data:
                snapshot_b64_data = data[0].get("base64")
            elif isinstance(data, dict):
                if "data" in data and data["data"]:
                    snapshot_b64_data = data["data"][0].get("b64_json") or data["data"][0].get("url")
                elif "base64" in data:
                    snapshot_b64_data = data["base64"]
            if snapshot_b64_data:
                if not snapshot_b64_data.startswith("data:image"):
                    snapshot_b64_data = f"data:image/jpeg;base64,{snapshot_b64_data}"
                supabase.table("projects").update(
                    {"snapshot_b64": snapshot_b64_data}
                ).eq("id", project_id).execute()
                print(f"Snapshot saved for {project_id}")
    except Exception as e:
        print(f"Snapshot task crashed: {e}")

# ==========================================================================
# DASHBOARD & WORKSPACE
# ==========================================================================

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)

    # Fetch latest Plan, Agent Skills, and Last Spin Date from DB
    has_skills = False
    last_spin_date = None
    try:
        res = supabase.table("users").select("plan, agent_skills, last_spin_date").eq("id", user["id"]).single().execute()
        if res and res.data:
            user["plan"] = res.data.get("plan", "free")
            last_spin_date = res.data.get("last_spin_date")
            if res.data.get("agent_skills"):
                has_skills = True
    except Exception:
        user["plan"] = "free"

    # Token Data
    used, limit = get_token_usage_and_limit(user["id"])
    user["tokens"] = {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used)
    }

    # Project Data
    try:
        # select("*") automatically pulls the new snapshot_b64 column
        res = (
            supabase.table("projects")
            .select("*")
            .eq("owner_id", user["id"])
            .order("updated_at", desc=True)
            .execute()
        )
        projects = res.data if res and res.data else []
    except Exception:
        projects = []

    return templates.TemplateResponse(
        "dashboard/dashboard.html",
        {
            "request": request,
            "projects": projects,
            "user": user,
            "has_skills": has_skills,
            "last_spin_date": last_spin_date
        }
    )

# 1. CREATE PAGE (Stash prompt in session & detect Figma)
@app.get("/projects/createit", response_class=HTMLResponse)
async def project_create_page(request: Request, prompt: Optional[str] = None):
    user = get_current_user(request)
    
    is_figma_link = False
    
    if prompt:
        request.session["stashed_prompt"] = prompt
        # Detect if the initial prompt is a figma URL so the template knows which animation to show
        if "figma.com" in prompt:
            is_figma_link = True
        
    return templates.TemplateResponse(
        "projects/project-create.html", 
        {
            "request": request, 
            "user": user, 
            "initial_prompt": prompt,
            "is_figma_link": is_figma_link # 🛑 Passes the flag to HTML!
        }
    )

# 2. CREATE ACTION (Backend Insert)
from backend.figma_import import fetch_and_compress_figma, compile_figma_to_react
from backend.github_import import (
    fetch_github_repo_files, get_github_login, repo_owner_matches, _parse_github_repo_url,
)
import re
import urllib.parse
import asyncio
import os


@app.post("/api/github/preview-import")
async def preview_github_import(request: Request):
    """
    Lightweight preview for the dashboard's GitHub-import modal: fetches the
    repo's file list (no project created yet) and reports whether it belongs
    to the user's own connected GitHub account, so the frontend can show a
    "push edits back to this repo" option before the user approves.
    """
    user = get_current_user(request)
    payload = await request.json()
    repo_url = (payload.get("github_url") or "").strip()
    if not repo_url:
        return JSONResponse({"ok": False, "error": "Missing GitHub repo URL."}, status_code=400)

    try:
        files = await fetch_github_repo_files(repo_url)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    user_data = db_select_one("users", {"id": user["id"]}, "github_access_token") or {}
    github_token = user_data.get("github_access_token")
    login = await get_github_login(github_token) if github_token else None
    owned_by_user = repo_owner_matches(repo_url, login)

    paths = sorted(files.keys())
    return JSONResponse({
        "ok": True,
        "file_count": len(paths),
        "paths": paths[:500],
        "owned_by_user": owned_by_user,
        "github_connected": bool(github_token),
    })

@app.post("/projects/create")
async def create_project(
    request: Request,
    background_tasks: BackgroundTasks,
    prompt: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
    description: str = Form(""),
    xmode: Optional[str] = Form(None),
    think_mode: Optional[str] = Form(None),
    image_base64: Optional[str] = Form(None),
    figma_url: Optional[str] = Form(None),
    use_supabase: Optional[str] = Form(None),
    github_url: Optional[str] = Form(None),
    github_allow_push: Optional[str] = Form(None),
):
    user = get_current_user(request)

    # Explicit think-mode selector ("fast" | "normal" | "deep") from the
    # dashboard's mode dropdown — replaces the old approach of prepending a
    # magic instruction string into the visible prompt text. (`xmode` is an
    # unrelated legacy field from the confirmation form's submit button —
    # not used for this.)
    think_mode = (think_mode or "normal").lower()
    if think_mode not in ("fast", "normal", "deep"):
        think_mode = "normal"
 
    def check_project_limit():
        res = supabase.table("projects").select("id", count="exact").eq("owner_id", user["id"]).execute()
        count = res.count if hasattr(res, "count") and res.count is not None else len(res.data)
        if count >= 3:
            return supabase.table("projects").select("*").eq("owner_id", user["id"]).order("created_at", desc=True).execute().data
        return None
 
    # --- 1. FREE TIER LIMIT ---
    if user.get("plan") != "premium":
        try:
            projects_data = await asyncio.to_thread(check_project_limit)
            if projects_data is not None:
                return templates.TemplateResponse("dashboard.html", {
                    "request": request, "user": user, "projects": projects_data,
                    "error": "Free Limit Reached (3/3). Upgrade to Pro.",
                })
        except Exception as e:
            print(f"Project limit check failed: {e}")
 
    # --- 2. PROMPT STASHING ---
    if prompt and not name:
        if figma_url:
            request.session["stashed_figma_url"] = figma_url
        if github_url:
            request.session["stashed_github_url"] = github_url
        is_figma_link = bool(figma_url or (prompt and "figma.com" in prompt))
        return templates.TemplateResponse("projects/project-create.html", {
            "request": request, "user": user,
            "initial_prompt": prompt, "stashed_image": image_base64,
            "is_figma_link": is_figma_link, "think_mode": think_mode,
        })

    # A GitHub-only import may have no typed prompt at all — allow that path
    # to reach the confirmation screen too (mirrors the figma_url branch
    # above, just without requiring `prompt`).
    if github_url and not name and not prompt:
        request.session["stashed_github_url"] = github_url
        return templates.TemplateResponse("projects/project-create.html", {
            "request": request, "user": user,
            "initial_prompt": None, "stashed_image": image_base64,
            "is_figma_link": False, "think_mode": think_mode,
        })

    final_prompt = prompt or request.session.pop("stashed_prompt", None)
    final_figma_url = figma_url or request.session.pop("stashed_figma_url", None)
    final_github_url = github_url or request.session.pop("stashed_github_url", None)
    project_name = name or "Untitled Project"
    final_image = image_base64
    final_figma_json = None
    final_github_files = None

    # --- 3. FIGMA INTERCEPTOR ---
    potential_url = final_figma_url or ""
    if not potential_url and final_prompt and "figma.com/" in final_prompt:
        match = re.search(r"(https://[^\\s^?]*figma\\.com/[^\\s]*)", final_prompt)
        if match:
            potential_url = match.group(0)

    if potential_url:
        try:
            def get_figma_token():
                return supabase.table("users").select("figma_access_token").eq("id", user["id"]).single().execute()
            user_data = await asyncio.to_thread(get_figma_token)
            figma_token = user_data.data.get("figma_access_token") if user_data.data else None
            if not figma_token:
                return RedirectResponse("/dashboard?error=figma_not_linked", status_code=303)
            final_figma_json, figma_img_b64 = await fetch_and_compress_figma(potential_url, figma_token)
            if figma_img_b64:
                final_image = figma_img_b64
            if "figma.com" in final_prompt:
                final_prompt = (
                    "Build a pixel-perfect React and Tailwind replica of the design structure. "
                    "I have provided the exact layout rules, spacing, typography, and hex colors "
                    "in the `.gorilla/figma.json` file. Read that file and implement it exactly."
                )
        except Exception as e:
            return RedirectResponse(f"/dashboard?error={urllib.parse.quote(str(e))}", status_code=303)

    # --- 3B. GITHUB IMPORT INTERCEPTOR ---
    final_github_repo_url_to_link = None
    if final_github_url:
        try:
            final_github_files = await fetch_github_repo_files(final_github_url)
            # Do NOT auto-trigger an agent turn for a repo-only import (no
            # typed prompt) — the old "summarize the codebase, then wait for
            # my next instruction" prompt had no clean way to actually stop:
            # the agent loop only exits cleanly via mark_done, so a model
            # that just replied with text and no tool call tripped the
            # no-progress circuit breaker after a few turns. Just seed the
            # files and land in the editor with the file tree visible —
            # nothing to fix, nothing to summarize via a burned turn.
            # If the user DID type their own prompt alongside the repo URL,
            # leave it untouched — don't discard their actual instructions.
            if not final_prompt:
                final_prompt = None

            # Re-verify ownership server-side — never trust the client's
            # github_allow_push flag on its own, since it decides whether
            # this project gets permission to push commits back to a real
            # GitHub repo.
            if github_allow_push == "true":
                gh_user_data = db_select_one(
                    "users", {"id": user["id"]}, "github_access_token"
                ) or {}
                gh_token = gh_user_data.get("github_access_token")
                gh_login = await get_github_login(gh_token) if gh_token else None
                if repo_owner_matches(final_github_url, gh_login):
                    owner, repo_name_parsed, _branch = _parse_github_repo_url(final_github_url)
                    final_github_repo_url_to_link = f"https://github.com/{owner}/{repo_name_parsed}"
        except Exception as e:
            return RedirectResponse(f"/dashboard?error={urllib.parse.quote(str(e))}", status_code=303)

    # BUG 5 FIX: fetch api_key + supabase token OUTSIDE the closure so they\'re captured
    user_keys_for_env = db_select_one(
        "users", {"id": user["id"]},
        "gorilla_api_key, supabase_access_token",
    ) or {}
    gorilla_api_key_for_env = user_keys_for_env.get("gorilla_api_key", "") or ""
    supa_mgmt_token = user_keys_for_env.get("supabase_access_token", "") or ""
 
    # --- 4. HEAVY LIFT ---
    def _heavy_lift_create():
        compiled_react_code = None
        figma_tokens_used = 0
 
        # 4A. SUPABASE PROVISIONING
        supabase_env_content = ""
        project_ref_to_save = None
        if use_supabase == "true" and supa_mgmt_token:
            try:
                headers = {"Authorization": f"Bearer {supa_mgmt_token}", "Content-Type": "application/json"}
                with httpx.Client(timeout=20.0) as client:
                    orgs_res = client.get("https://api.supabase.com/v1/organizations", headers=headers)
                    orgs = orgs_res.json() if orgs_res.status_code == 200 else []
                    org_id = orgs[0]["id"] if isinstance(orgs, list) and orgs else None
                    if not org_id:
                        new_org = client.post(
                            "https://api.supabase.com/v1/organizations",
                            headers=headers, json={"name": "Gorilla Apps"},
                        ).json()
                        org_id = new_org.get("id")
                    if org_id:
                        db_pass = secrets.token_urlsafe(16)
                        safe_db_name = re.sub(r"[^a-zA-Z0-9 ]", "", project_name)[:32].strip() or "Gorilla App"
                        proj_res = client.post(
                            "https://api.supabase.com/v1/projects", headers=headers,
                            json={
                                "organization_id": org_id, "name": safe_db_name,
                                "db_pass": db_pass, "region": "us-east-1", "plan": "free",
                            },
                        )
                        if proj_res.status_code == 201:
                            project_ref_to_save = proj_res.json().get("id")
                            supa_url = f"https://{project_ref_to_save}.supabase.co"
                            supa_anon_key = ""
                            for _ in range(10):
                                keys_res = client.get(
                                    f"https://api.supabase.com/v1/projects/{project_ref_to_save}/api-keys",
                                    headers=headers,
                                )
                                if keys_res.status_code == 200:
                                    keys = keys_res.json()
                                    if isinstance(keys, list):
                                        anon_obj = next(
                                            (k for k in keys if k.get("name") in ["anon", "publishable"]),
                                            None,
                                        )
                                        if anon_obj and anon_obj.get("api_key"):
                                            supa_anon_key = anon_obj.get("api_key")
                                            break
                                time.sleep(1.5)
                            # REAL newlines, not escaped
                            supabase_env_content = (
                                f"\\nVITE_SUPABASE_URL={supa_url}\\n"
                                f"VITE_SUPABASE_ANON_KEY={supa_anon_key}\\n"
                            )
            except Exception as e:
                print(f"Supabase provisioning failed: {e}")
 
        # 4B. GEMINI FIGMA COMPILER
        if final_figma_json:
            or_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("GORILLA_API_KEY")
            if or_key:
                try:
                    compiled_react_code, figma_tokens_used = asyncio.run(
                        compile_figma_to_react(final_figma_json, or_key)
                    )
                    if figma_tokens_used:
                        add_monthly_tokens(user["id"], figma_tokens_used)
                except Exception as e:
                    print(f"Gemini Compiler failed: {e}")
 
        # CHAT HISTORY SEED
        initial_history = []
        if compiled_react_code:
            initial_history.append({
                "role": "system",
                "content": "A Figma design was imported. A pre-compiler has already converted the design into the starting React code located in src/App.tsx.",
            })
            initial_history.append({
                "role": "assistant",
                "content": "✨ I compiled your Figma design into React. What state would you like to add?",
            })
            if final_prompt and "figma.com" not in final_prompt:
                initial_history.append({"role": "user", "content": final_prompt})
        elif final_figma_json:
            initial_history.append({
                "role": "system",
                "content": f"FIGMA DESIGN DATA:\\n{final_figma_json}",
            })
 
        gorilla_auth_id = str(uuid.uuid4())
 
        res = supabase.table("projects").insert({
            "owner_id": user["id"],
            "name": project_name,
            "gorilla_auth_id": gorilla_auth_id,
            "supabase_project_ref": project_ref_to_save,
            "description": description or (final_prompt[:200] if final_prompt else ""),
            "prompt_image": final_image,
            "snapshot_b64": final_image,
            "chat_history": initial_history,
        }).execute()
 
        if not res.data:
            raise Exception("DB Insert Failed - Check Service Role Key")
        pid = res.data[0]["id"]
 
        clean_name = re.sub(r"[^a-z0-9-]", "-", project_name.lower()).strip("-") or "app"
        supabase.table("projects").update({"subdomain": f"{clean_name}-{pid}"}).eq("id", pid).execute()
 
        bp_dir = globals().get("BOILERPLATE_DIR")
        if not bp_dir or not os.path.isdir(bp_dir):
            bp_dir = os.path.join(ROOT_DIR, "backend", "boilerplate")
 
        if os.path.isdir(bp_dir):
            files_to_insert = []
            # BUG 5 FIX: api_key is the captured var, real newlines in .env
            files_to_insert.append({
                "project_id": pid,
                "path": ".env",
                "content": (
                    f"VITE_GORILLA_AUTH_ID={gorilla_auth_id}\\n"
                    f"GORILLA_API_KEY={gorilla_api_key_for_env}"
                    f"{supabase_env_content}"
                ),
            })
 
            for root, dirs, files in os.walk(bp_dir):
                dirs[:] = [d for d in dirs if d not in ["node_modules", ".git", "dist", "build"]]
                for file in files:
                    if file.startswith("."):
                        continue
                    abs_path = os.path.join(root, file)
                    rel_path = os.path.relpath(abs_path, bp_dir).replace("\\\\", "/")
                    try:
                        with open(abs_path, "r", encoding="utf-8") as f:
                            files_to_insert.append({
                                "project_id": pid, "path": rel_path, "content": f.read(),
                            })
                    except Exception:
                        continue
 
            if files_to_insert and compiled_react_code:
                for f in files_to_insert:
                    if f["path"] in ["src/App.tsx", "src/App.jsx"]:
                        f["content"] = compiled_react_code
                        break
 
            if files_to_insert:
                try:
                    supabase.table("files").insert(files_to_insert).execute()
                except Exception:
                    for f in files_to_insert:
                        try:
                            supabase.table("files").upsert(f, on_conflict="project_id,path").execute()
                        except Exception:
                            pass
 
        if final_image:
            try:
                supabase.table("files").insert({
                    "project_id": pid, "path": ".gorilla/prompt_image.b64", "content": final_image,
                }).execute()
            except Exception:
                pass
 
        if final_figma_json:
            try:
                supabase.table("files").insert({
                    "project_id": pid, "path": ".gorilla/figma.json", "content": final_figma_json,
                }).execute()
            except Exception:
                pass

        if final_github_files:
            try:
                github_rows = [
                    {"project_id": pid, "path": gh_path, "content": gh_content}
                    for gh_path, gh_content in final_github_files.items()
                ]
                db_upsert_batch("files", github_rows, on_conflict="project_id,path")
            except Exception as e:
                print(f"GitHub import file insert failed: {e}")

            if final_github_repo_url_to_link:
                try:
                    supabase.table("projects").update(
                        {"github_repo_url": final_github_repo_url_to_link}
                    ).eq("id", pid).execute()
                except Exception as e:
                    print(f"GitHub repo link failed: {e}")

        return pid
 
    # --- 5. EXECUTION ---
    try:
        pid = await asyncio.to_thread(_heavy_lift_create)
 
        if final_prompt and not final_figma_json and not final_image and not final_github_files:
            if gorilla_api_key_for_env.startswith("gb_live_"):
                background_tasks.add_task(
                    generate_project_snapshot, pid, final_prompt, gorilla_api_key_for_env,
                )
 
        # BUG 4 FIX: build URL directly. No query_params list.
        # After
        if final_prompt:
            request.session["editor_prompt"] = final_prompt
            request.session["editor_think_mode"] = think_mode
        return RedirectResponse(f"/projects/{pid}/editor", status_code=303)
 
    except Exception as e:
        print(f"Create Error: {e}")
        return RedirectResponse("/dashboard?error=creation_failed", status_code=303)


# 3. EDITOR PAGE
@app.get("/projects/{project_id}/editor", response_class=HTMLResponse)
async def project_editor(request: Request, project_id: str, file: str = "index.html", prompt: Optional[str] = None):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    if not prompt:
        prompt = request.session.pop("editor_prompt", None)
    initial_think_mode = request.session.pop("editor_think_mode", "normal")

    # 1. Fetch User Data (API Keys & Integrations)
    user_data = db_select_one("users", {"id": user["id"]}, "gorilla_api_key, github_access_token, supabase_access_token")
    api_key = user_data.get("gorilla_api_key", "") if user_data else ""
    has_github = bool(user_data and user_data.get("github_access_token"))
    has_supabase = bool(user_data and user_data.get("supabase_access_token"))
    
    # 2. Fetch Project Data
    try:
        res = supabase.table("projects").select("*").eq("id", project_id).single().execute()
        project = res.data
    except Exception:
        project = {} 
    
    # 3. Token Check
    used, limit = get_token_usage_and_limit(user["id"])
    user["tokens"] = {"used": used, "limit": limit}
    
    chat_history = project.get("chat_history", []) if project else []
    
    # 4. DB Agent Resume Logic
    project_has_db = bool(project and project.get("supabase_project_ref"))
    last_user_msg = next((msg.get("content", "").lower() for msg in reversed(chat_history) if msg.get("role") == "user"), "")
    
    resume_db_agent = project_has_db or ("database" in last_user_msg) or ("supabase" in last_user_msg)

    return templates.TemplateResponse(
        "projects/project-editor.html",
        {
            "prompt": prompt,
            "request": request, 
            "project_id": project_id, 
            "project": project, 
            "project_name": project.get("name", "Untitled Project") if project else "Untitled Project",
            "file": file, 
            "user": user,
            "initial_prompt": prompt,
            "initial_think_mode": initial_think_mode,
            "has_github": has_github,
            "has_supabase": has_supabase,
            "project_has_db": project_has_db,
            "resume_db_agent": resume_db_agent,
            "gorilla_api_key": api_key,
            "chat_history": json.dumps(chat_history) 
        }
    )

# 5. PREVIEW PAGE
@app.get("/projects/{project_id}/preview", response_class=HTMLResponse)
async def project_preview(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
 
    # BUG 7 FIX: actually fetch the project
    try:
        res = supabase.table("projects").select("*").eq("id", project_id).single().execute()
        project = res.data if res else None
    except Exception:
        project = None
 
    project_name = project.get("name", "Untitled Project") if project else "Untitled Project"
 
    return templates.TemplateResponse(
        "projects/project-preview.html",
        {
            "request": request,
            "project_id": project_id,
            "project_name": project_name,
            "user": user,
        },
    )

# 6. SETTINGS PAGE
@app.get("/projects/{project_id}/settings", response_class=HTMLResponse)
async def project_settings(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    try:
        res = supabase.table("projects").select("*").eq("id", project_id).single().execute()
        project = res.data if res else None
    except Exception:
        project = None
        
    return templates.TemplateResponse(
        "projects/project-settings.html",
        {"request": request, "project_id": project_id, "project": project, "project_name": project.get("name", "Untitled Project") if project else "Untitled Project", "user": user}
    )
from fastapi import FastAPI, UploadFile, File # <-- Added File here
from typing import Optional
@app.post("/projects/{project_id}/settings")
async def project_settings_save(
    request: Request, 
    project_id: str, 
    name: str = Form(...), 
    description: str = Form(""),
    snapshot: Optional[UploadFile] = File(None) # Added the file catcher
):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    update_data = {
        "name": name, 
        "description": description
    }

    # If the user uploaded a new image, process it into Base64
    if snapshot and snapshot.filename:
        file_bytes = await snapshot.read()
        mime_type, _ = mimetypes.guess_type(snapshot.filename)
        
        if mime_type and mime_type.startswith('image/'):
            encoded_str = base64.b64encode(file_bytes).decode('utf-8')
            update_data["snapshot_b64"] = f"data:{mime_type};base64,{encoded_str}"
    
    supabase.table("projects").update(update_data).eq("id", project_id).execute()
    
    return RedirectResponse(f"/projects/{project_id}/settings", status_code=303)


# 7. EXPORT TO ZIP
@app.get("/api/project/{project_id}/export")
async def project_export(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    try:
        user_record = db_select_one("users", {"id": user["id"]}, "plan")
        current_plan = user_record.get("plan") if user_record else "free"
    except:
        current_plan = "free"

    res = (
        supabase.table("files")
        .select("path,content")
        .eq("project_id", project_id)
        .execute()
    )
    files = res.data if res and res.data else []

    if not files:
        raise HTTPException(status_code=404, detail="No files found in this project.")

    zip_buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in files:
                path = file.get("path", "unknown.txt").strip("/")
                content = file.get("content") or ""
                zf.writestr(path, content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create ZIP: {e}")

    zip_buffer.seek(0)
    filename = f"gorilla_project_{project_id[:8]}.zip"
    track_event(project_id, "export", metadata={"file_count": len(files)})
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Cache-Control": "no-cache"
        }
    )

# ==========================================================================
# ANALYTICS HELPERS
# ==========================================================================

def track_event(project_id: str, event_type: str, **kwargs):
    """Non-blocking analytics write. Never raises — analytics must never break features."""
    try:
        supabase.table("app_analytics").insert({
            "project_id":    project_id,
            "event_type":    event_type,
            "user_email":    kwargs.get("user_email"),
            "user_provider": kwargs.get("user_provider"),
            "metadata":      kwargs.get("metadata") or {},
        }).execute()
    except Exception as e:
        err_str = str(e)
        if "42501" in err_str or "row-level security" in err_str.lower():
            print(f"⚠️ analytics RLS policy blocks insert for event '{event_type}'. "
                  f"Fix: grant INSERT on app_analytics to the service role, or disable RLS for the service key.")
        else:
            print(f"⚠️ analytics track_event failed ({event_type}): {e}")

# ==========================================================================
# SANDBOX ROUTES AND HELPERS
# ==========================================================================

def _build_sandbox_env(project_id: str, user_id: str) -> dict:
    user_data = db_select_one(
        "users", {"id": user_id},
        "gorilla_api_key, supabase_access_token",
    ) or {}
    api_key = user_data.get("gorilla_api_key", "")
    supa_mgmt = user_data.get("supabase_access_token", "")
 
    proj = db_select_one(
        "projects", {"id": project_id},
        "gorilla_auth_id, supabase_project_ref",
    ) or {}
    auth_id = proj.get("gorilla_auth_id", "")
    project_ref = proj.get("supabase_project_ref") or ""
 
    supa_url, supa_anon = "", ""
    env_file = db_select_one("files", {"project_id": project_id, "path": ".env"})
    if env_file and env_file.get("content"):
        # Real newline character — \\n in Python source = newline at runtime
        for line in env_file["content"].split("\\n"):
            line = line.strip()
            if line.startswith("VITE_SUPABASE_URL="):
                supa_url = line.split("=", 1)[1].strip()
            elif line.startswith("VITE_SUPABASE_ANON_KEY="):
                supa_anon = line.split("=", 1)[1].strip()
 
    return {
        "GORILLA_API_KEY":        api_key,
        "VITE_GORILLA_AUTH_ID":   auth_id,
        "VITE_SUPABASE_URL":      supa_url,
        "VITE_SUPABASE_ANON_KEY": supa_anon,
        "SUPABASE_MGMT_TOKEN":    supa_mgmt,
        "SUPABASE_PROJECT_REF":   project_ref,
    }
 
def _init_sandbox_manager():
    global _sandbox_manager
    _sandbox_manager = E2BSandboxManager(
        db_upsert_fn=db_upsert,
        db_delete_fn=db_delete,
        db_upsert_batch_fn=db_upsert_batch,
        add_tokens_fn=add_monthly_tokens,
        emit_log_fn=emit_log,
        emit_status_fn=emit_status,
        emit_file_changed_fn=emit_file_changed,
        emit_file_deleted_fn=emit_file_deleted,
        fetch_files_fn=_fetch_file_tree,
        list_db_paths_fn=db_list_file_paths,
        progress_bus=progress_bus,
    )
 
# At the very bottom of app.py (replacing the old set_log_callback calls):

@app.post("/api/project/{project_id}/sandbox/start")
async def start_sandbox(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    # ← ADD THIS GUARD
    if not _sandbox_manager:
        return JSONResponse({"detail": "Sandbox manager not initialized"}, status_code=503)

    # Serialize boots per project — a second concurrent request waits here,
    # then sees is_running() == True and returns the same sandbox.
    async with _sandbox_boot_lock(project_id):
        if _sandbox_manager.is_running(project_id):
            url = _sandbox_manager.get_preview_url(project_id)
            if url:
                progress_bus.emit(project_id, {"type": "sandbox_url", "url": url})
            return JSONResponse({"status": "running", "url": url or ""})

        try:
            # Sync Supabase call — off the event loop so SSE keeps flowing.
            await asyncio.to_thread(enforce_token_limit_or_raise, user["id"])
        except HTTPException as e:
            if e.status_code == 402:
                emit_log(project_id, "assistant", (
                    "⚠️ **Your token limit has been reached.** The sandbox can't start until you upgrade or top up. "
                    "Visit [Pricing](/pricing) to continue building."
                ))
                return JSONResponse({
                    "detail": "Token limit reached. Please upgrade your plan or top up tokens to continue.",
                    "upgrade_url": "/pricing"
                }, status_code=402)
            raise

        env_vars = await asyncio.to_thread(_build_sandbox_env, project_id, user["id"])
        try:
            await _sandbox_manager.ensure_running(project_id, env_vars, user["id"])
            url = await _sandbox_manager.start_dev_server(project_id)
            await asyncio.to_thread(track_event, project_id, "sandbox_boot")
            return JSONResponse({"status": "ok", "url": url or ""})
        except Exception as e:
            print(f"Sandbox boot error: {e}")
            return JSONResponse({"detail": str(e)}, status_code=500)
 
 
@app.post("/api/project/{project_id}/sandbox/stop")
async def stop_sandbox(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    await _sandbox_manager.kill(project_id)
    return JSONResponse({"status": "stopped"})

# ==========================================================================
# ANALYTICS ROUTE
# ==========================================================================

from collections import defaultdict, Counter

@app.get("/api/project/{project_id}/analytics")
async def get_project_analytics(request: Request, project_id: str, days: int = 30):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    try:
        # Single efficient query — filtered by project + time window, capped at 500 rows
        res = (
            supabase.table("app_analytics")
            .select("event_type, user_email, user_provider, metadata, created_at")
            .eq("project_id", project_id)
            .gte("created_at", since)
            .order("created_at", desc=True)
            .limit(500)
            .execute()
        )
        events = res.data or []
    except Exception as e:
        return JSONResponse({"error": str(e), "events": [], "totals": {}, "unique_users": 0,
                             "daily_logins": {}, "daily_tokens": {}, "providers": {}})

    # — Aggregate in Python (avoids multiple DB round-trips) —
    daily_logins  = defaultdict(int)
    daily_agents  = defaultdict(int)
    daily_tokens  = defaultdict(int)
    daily_errors  = defaultdict(int)
    providers     = Counter()
    event_types   = Counter()
    login_emails  = set()

    for ev in events:
        day  = ev["created_at"][:10]
        etype = ev.get("event_type", "")
        event_types[etype] += 1

        if etype == "login":
            daily_logins[day] += 1
            if ev.get("user_provider"):
                providers[ev["user_provider"]] += 1
            if ev.get("user_email"):
                login_emails.add(ev["user_email"])

        elif etype == "agent_run":
            daily_agents[day] += 1
            toks = (ev.get("metadata") or {}).get("tokens", 0)
            daily_tokens[day] += int(toks or 0)

        elif etype == "error_fix":
            daily_errors[day] += 1

    # Recent feed — last 30 events, human-readable
    recent = [
        {
            "type":     ev.get("event_type"),
            "email":    ev.get("user_email"),
            "provider": ev.get("user_provider"),
            "metadata": ev.get("metadata") or {},
            "ts":       ev.get("created_at"),
        }
        for ev in events[:30]
    ]

    return JSONResponse({
        "totals":       dict(event_types),
        "unique_users": len(login_emails),
        "daily_logins": dict(daily_logins),
        "daily_agents": dict(daily_agents),
        "daily_tokens": dict(daily_tokens),
        "daily_errors": dict(daily_errors),
        "providers":    dict(providers),
        "recent":       recent,
    })

# ==========================================================================
# REUSABLE AGENT LOOP - Streamlined Version
# ==========================================================================
#
# No plan rendering - agents work freely
# Only assistant message shown to user
# All activity logged to terminal
# Minimal, clean UI experience
#

import asyncio
import os
import re
import httpx

# Global tracking for active AI fixes
active_ai_fixes = set()
_AGENT_TASKS = set()
# Per-project active-run guard — prevents two concurrent agent/start calls
# (double-click, duplicate tab, retry-storm) from driving the same sandbox
# session at once, which corrupts file-write ordering and turn state.
_AGENT_TASKS_BY_PROJECT: Dict[str, asyncio.Task] = {}


async def run_agent_loop(
    project_id: str,
    prompt: str,
    user_id: str,
    agent_type: str = "fast",
    history: list = None,
    skip_planner: bool = False,
    is_system_task: bool = False,
    think_mode: str = "normal",
):
    try:
        await asyncio.sleep(0.2)

        # ---- Load project state ----
        proj = (await asyncio.to_thread(
            db_select_one,
            "projects", {"id": project_id},
            "name, chat_history, supabase_project_ref, gorilla_auth_id",
        )) or {}
        project_name = proj.get("name", "Gorilla App")
        project_ref = proj.get("supabase_project_ref")
        has_supabase = bool(project_ref)  # ← add this line here
        db_history = proj.get("chat_history") or []

        # Append user message to chat history and persist
        if not skip_planner:
            if is_system_task:
                db_history.append({"role": "system", "content": f"SYSTEM: {prompt[:500]}"})
            else:
                db_history.append({"role": "user", "content": prompt})
            if len(db_history) > 100:
                db_history = db_history[-100:]
            # ★ FIX: off-load the sync supabase call to a thread so the
            # event loop can keep emitting SSE events in the meantime.
            await asyncio.to_thread(
                lambda: supabase.table("projects").update(
                    {"chat_history": db_history}
                ).eq("id", project_id).execute()
            )

        user_data = (await asyncio.to_thread(
            db_select_one,
            "users", {"id": user_id},
            "gorilla_api_key, supabase_access_token, agent_skills",
        )) or {}
        supa_token = user_data.get("supabase_access_token")
        agent_skills = user_data.get("agent_skills")  # ← add this line
        is_db_request = (agent_type == "supabase")
    
        # ---- MID-CHAT SUPABASE PROVISIONING (unchanged from old code) ----
        if is_db_request and not project_ref and supa_token:
            emit_status(project_id, "Provisioning Remote Database...")
            emit_log(project_id, "system", "Spinning up PostgreSQL instance...")
            try:
                import secrets as _secrets
                headers = {"Authorization": f"Bearer {supa_token}", "Content-Type": "application/json"}
                async with httpx.AsyncClient() as client:
                    orgs = (await client.get(
                        "https://api.supabase.com/v1/organizations", headers=headers
                    )).json()
                    if isinstance(orgs, dict) and "message" in orgs:
                        raise Exception(f"Supabase: {orgs.get('message')}")
                    org_id = orgs[0]["id"] if isinstance(orgs, list) and orgs else None
                    if not org_id:
                        new_org = (await client.post(
                            "https://api.supabase.com/v1/organizations",
                            headers=headers, json={"name": "Gorilla Apps"},
                        )).json()
                        org_id = new_org.get("id")
                    if not org_id:
                        raise Exception("No Supabase organization")
                    db_pass = _secrets.token_urlsafe(16)
                    safe_name = re.sub(r"[^a-zA-Z0-9 ]", "", project_name)[:32].strip() or "Gorilla App"
                    proj_res = await client.post(
                        "https://api.supabase.com/v1/projects",
                        headers=headers,
                        json={
                            "organization_id": org_id, "name": safe_name,
                            "db_pass": db_pass, "region": "us-east-1", "plan": "free",
                        },
                    )
                    if proj_res.status_code == 201:
                        project_ref = proj_res.json().get("id")
                        supabase.table("projects").update(
                            {"supabase_project_ref": project_ref}
                        ).eq("id", project_id).execute()
                        has_supabase = True

                        supa_anon_key = ""
                        for i in range(45):
                            if i and i % 5 == 0:
                                emit_log(project_id, "system",
                                    f"Still waiting for database to come online ({i * 4}s)...")
                            s = await client.get(
                                f"https://api.supabase.com/v1/projects/{project_ref}",
                                headers=headers,
                            )
                            if s.status_code == 200 and s.json().get("status") in ["ACTIVE_HEALTHY", "ACTIVE"]:
                                break
                            await asyncio.sleep(4)
                        else:
                            emit_log(project_id, "system",
                                "Database is taking longer than expected to come online; continuing anyway.")
                        for i in range(10):
                            if i and i % 3 == 0:
                                emit_log(project_id, "system", "Waiting for database API keys...")
                            k = await client.get(
                                f"https://api.supabase.com/v1/projects/{project_ref}/api-keys",
                                headers=headers,
                            )
                            if k.status_code == 200:
                                kl = k.json()
                                if isinstance(kl, list):
                                    anon = next(
                                        (x for x in kl if x.get("name") in ["anon", "publishable"]),
                                        None,
                                    )
                                    if anon and anon.get("api_key"):
                                        supa_anon_key = anon["api_key"]
                                        break
                            await asyncio.sleep(3)
                        if not supa_anon_key:
                            emit_log(project_id, "system",
                                "Could not retrieve database API key yet; it will sync on your next message.")

                        if supa_anon_key:
                            supa_url = f"https://{project_ref}.supabase.co"
                            env_content = (
                                f"VITE_GORILLA_AUTH_ID={proj.get('gorilla_auth_id','')}\n"
                                f"GORILLA_API_KEY={user_data.get('gorilla_api_key','')}\n"
                                f"VITE_SUPABASE_URL={supa_url}\n"
                                f"VITE_SUPABASE_ANON_KEY={supa_anon_key}"
                            )
                            db_upsert(
                                "files",
                                {"project_id": project_id, "path": ".env", "content": env_content},
                                on_conflict="project_id,path",
                            )
                            emit_log(project_id, "system", f"Database ready: {project_ref}")
                    else:
                        emit_log(project_id, "system", f"Provisioning failed: {proj_res.text[:200]}")
            except Exception as e:
                emit_log(project_id, "system", f"DB provisioning error: {e}")

        # ---- Build env + contextual prompt ----
        env_vars = await asyncio.to_thread(_build_sandbox_env, project_id, user_id)
        gorilla_proxy = os.getenv("FILE_API_BASE_URL", "https://your-proxy.ngrok-free.dev")

        contextual_prompt = prompt
        if len(db_history) > 1 and not skip_planner:
            past = db_history[-7:-1]
            history_text = "\n".join([
                f"{m.get('role', 'user').upper()}: {m.get('content', '')[:300]}"
                for m in past
            ])
            contextual_prompt = (
                f"--- PREVIOUS CONVERSATION ---\n{history_text}\n"
                f"\n--- CURRENT REQUEST ---\n{prompt}"
            )

        # Image attachment (first-turn only, read from Supabase files)
        file_tree = await _fetch_file_tree(project_id)
        image_b64 = file_tree.get(".gorilla/prompt_image.b64")

        # ---- Callback to persist each assistant message as it arrives ----
        def on_assistant_message(msg: str):
            # Note: this runs inside the sandbox manager which may already
            # be on a worker thread, so a direct .execute() here is fine.
            # Keeping it synchronous avoids awaiting from a sync callback.
            try:
                db_history.append({"role": "assistant", "content": msg})
                if len(db_history) > 100:
                    db_history[:] = db_history[-100:]
                supabase.table("projects").update(
                    {"chat_history": db_history}
                ).eq("id", project_id).execute()
            except Exception as e:
                print(f"Persist assistant message failed: {e}")

        # ---- Hand off EVERYTHING to the sandbox manager ----
        # It handles: boot → multi-turn agent loop → single end-of-turn sync
        #             → delete detection → dev server restart → URL emit
        emit_status(project_id, "Preparing Sandbox...")
        emit_progress(project_id, "Preparing Environment...", 5)

        result = await _sandbox_manager.run_agent_turn(
            project_id=project_id,
            user_request=contextual_prompt,
            user_id=user_id,
            env_vars=env_vars,
            chat_history=db_history,
            gorilla_proxy_url=gorilla_proxy,
            has_supabase=has_supabase,
            is_debug=skip_planner,
            error_context=prompt if skip_planner else "",
            image_b64=image_b64 if not skip_planner else None,
            on_assistant_message=on_assistant_message,
            agent_skills=agent_skills,  # ← add this line
            think_mode=think_mode,
        )

        # NOTE: do NOT bill tokens here. _sandbox_manager is constructed with
        # add_tokens_fn=add_monthly_tokens (see `_sandbox_manager = ...` /
        # SandboxManager init below) — every turn, reviewer pass, and
        # spawn_subagent call already bills incrementally in real time
        # *inside* run_agent_turn as it happens. `result["tokens"]` is just
        # the cumulative total of everything already billed; charging it
        # again here double-bills every turn (and compounds further on any
        # turn that ran a reviewer pass or a sub-agent, since those are
        # billed incrementally too before being folded into this total).
        total_tokens = result.get("tokens", 0)
        if total_tokens and user_id:
            await asyncio.to_thread(_maybe_emit_token_warning, project_id, user_id)

        await asyncio.to_thread(track_event, project_id, "agent_run",
                                metadata={"tokens": total_tokens})

        # Process user action (pause/resume flow)
        user_action = result.get("user_action")
        if user_action:
            action_type = user_action["type"]

            if action_type == "connect_supabase":
                # Emit SSE event — frontend renders the button
                progress_bus.emit(project_id, {
                    "type":   "user_input_required",
                    "action": "connect_supabase",
                    "reason": user_action.get("reason", ""),
                })

            elif action_type == "set_env_vars":
                progress_bus.emit(project_id, {
                    "type":   "user_input_required",
                    "action": "set_env_vars",
                    "vars":   user_action.get("vars", []),
                    "reason": user_action.get("reason", ""),
                })

            # Create a pause event and block until /agent/resume is called
            ev = asyncio.Event()
            _AGENT_PAUSE_EVENTS[project_id] = ev
            emit_status(project_id, "Waiting for your input…")

            try:
                await asyncio.wait_for(ev.wait(), timeout=300)  # 5 min timeout
            except asyncio.TimeoutError:
                emit_status(project_id, "Timed out waiting for input.")
                _AGENT_PAUSE_EVENTS.pop(project_id, None)
                _AGENT_PAUSE_DATA.pop(project_id, None)
                return

            resume_data = _AGENT_PAUSE_DATA.pop(project_id, {})
            _AGENT_PAUSE_EVENTS.pop(project_id, None)

            # Re-fetch updated state and continue the loop with an observation
            emit_status(project_id, "Resuming…")
            
            denied = bool(resume_data.get("data", {}).get("denied"))

            if action_type == "connect_supabase" and denied:
                continuation_prompt = (
                    "The user declined to connect Supabase. Do NOT ask again this "
                    "session. Continue with the rest of the task using only what's "
                    "possible without a database — if the feature they asked for "
                    "strictly requires one, briefly explain that limitation instead "
                    "of blocking on it."
                )
            elif action_type == "connect_supabase":
                # Reload supabase status and continue
                user_data_fresh = db_select_one("users", {"id": user_id}, "supabase_access_token") or {}
                has_supabase_now = bool(user_data_fresh.get("supabase_access_token"))
                continuation_prompt = (
                    "The user has now connected their Supabase account. "
                    f"has_supabase is now {'true' if has_supabase_now else 'still false — they may have cancelled'}. "
                    "Continue with the original task."
                )
            elif action_type == "set_env_vars" and denied:
                continuation_prompt = (
                    "The user chose to skip providing those environment variables. "
                    "Do NOT ask again this session. Continue with the rest of the "
                    "task — if the feature strictly requires those keys, briefly "
                    "explain that limitation instead of blocking on it."
                )
            elif action_type == "set_env_vars":
                submitted = resume_data.get("data", {}).get("vars", {})
                var_list  = ", ".join(submitted.keys()) if submitted else "none"
                continuation_prompt = (
                    f"The user has provided the requested environment variables: {var_list}. "
                    "They have been written to .env. Continue with the original task."
                )
            else:
                continuation_prompt = "The user has completed the requested action. Continue."

            # Re-run the loop from this project's current state as a follow-up turn
            asyncio.create_task(run_agent_loop(
                project_id=project_id,
                prompt=continuation_prompt,
                user_id=user_id,
                agent_type=agent_type,
                skip_planner=True,
                is_system_task=True,
            ))
            return   # current invocation ends; the task above carries on

        # Check for fatal errors if no user action triggered
        if not result.get("ok"):
            emit_status(project_id, "Fatal Error")
            return

        emit_status(project_id, "Done")
        emit_progress(project_id, "Ready", 100)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"⚠️ run_agent_loop EXCEPTION: {e}")
        print(tb)
        emit_status(project_id, "Fatal Error")
        # Actually surface the error message AND traceback to the chat UI
        # so you can see what broke without digging through terminal logs.
        emit_log(project_id, "system", f"❌ {type(e).__name__}: {e}")
        # Split traceback into last few lines so the UI doesn't choke
        tb_short = "\n".join(tb.strip().split("\n")[-8:])
        emit_log(project_id, "system", f"Traceback:\n{tb_short}")

# ==========================================================================
# AUTO-FIXING LOG ENDPOINT
# ==========================================================================

@app.post("/api/project/{project_id}/log")
async def log_browser_event(
    project_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    global active_ai_fixes
    try:
        form = await request.form()
        message = form.get("message", "")
        level = form.get("level", "INFO")
        print(f"[{level}] Browser Event: {message[:100]}...")
 
        msg_lc = message.lower()
        if not ("error" in msg_lc or "failed" in msg_lc or "syntax error" in msg_lc):
            return JSONResponse({"status": "ok"})
 
        # Backend mutex
        if project_id in active_ai_fixes:
            print(f"[{project_id}] AI already fixing — ignoring duplicate")
            return JSONResponse({"status": "ignored"})
 
        # BUG 9 FIX: don't burn tokens if sandbox is gone — preview won't update anyway
        if not (_sandbox_manager and _sandbox_manager.is_running(project_id)):
            print(f"[{project_id}] Sandbox not running — skipping auto-fix")
            return JSONResponse({"status": "skipped_no_sandbox"})
 
        active_ai_fixes.add(project_id)
        emit_log(project_id, "system", "Browser error detected. Analyzing...")
 
        owner_id = None
        try:
            proj = db_select_one("projects", {"id": project_id}, "owner_id")
            if proj:
                owner_id = proj.get("owner_id")
        except Exception as db_err:
            print(f"DB fetch error in /log: {db_err}")
 
        if not owner_id:
            active_ai_fixes.discard(project_id)
            return JSONResponse(
                {"status": "error", "detail": "Owner not found"}, status_code=404
            )
 
        async def run_and_unlock(*args, **kwargs):
            try:
                await run_agent_loop(*args, **kwargs)
            finally:
                active_ai_fixes.discard(project_id)
 
        # BUG 6 FIX: is_xmode doesn't exist — use skip_planner
        background_tasks.add_task(
            run_and_unlock,
            project_id=project_id,
            prompt=message,
            user_id=owner_id,
            skip_planner=True,
        )
 
    except Exception as e:
        print(f"Logging error: {e}")
        active_ai_fixes.discard(project_id)
        import traceback
        traceback.print_exc()
 
    return JSONResponse({"status": "ok"})
# ==========================================================================
# DEPLOYMENT ROUTES (VERCEL & GITHUB)
# ==========================================================================

# 1. Dedicated Route to securely LINK GitHub without logging out the active Google user
@app.get("/auth/github/link")
async def link_github_account(request: Request):
    user = get_current_user(request)
    if not GITHUB_CLIENT_ID or not GITHUB_REDIRECT_URI:
        raise HTTPException(500, "GitHub Auth config missing.")
    
    # We pass 'link' in state so the callback knows we are just attaching a token
    scope = "user:email repo"
    auth_url = f"https://github.com/login/oauth/authorize?client_id={GITHUB_CLIENT_ID}&redirect_uri={GITHUB_REDIRECT_URI}&scope={scope}&state=link_{user['id']}"
    return RedirectResponse(auth_url)

# 2. Render Deploy Wizard
@app.get("/projects/{project_id}/deploy", response_class=HTMLResponse)
async def project_deploy_page(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    try:
        res = supabase.table("projects").select("*").eq("id", project_id).single().execute()
        project = res.data
    except Exception:
        project = {}
        
    user_data = db_select_one("users", {"id": user["id"]}, "github_access_token, gorilla_api_key")
    has_github = bool(user_data and user_data.get("github_access_token"))
    api_key = user_data.get("gorilla_api_key", "")

    return templates.TemplateResponse(
        "projects/deploy.html",
        {
            "request": request,
            "project_id": project_id,
            "project": project,
            "has_github": has_github,
            "gorilla_api_key": api_key, # <--- Passing the key to the template
            "user": user
        }
    )

# 3. Optimize Codebase for Vercel
import json
from fastapi import Request, HTTPException

# ==========================================================================
# DEPLOY OPTIMIZE FIX
# ==========================================================================
@app.post("/api/project/{project_id}/deploy-optimize")
async def optimize_for_vercel(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    try:
        user_data = db_select_one("users", {"id": user["id"]}, "gorilla_api_key")
        api_key = user_data.get("gorilla_api_key", "") if user_data else ""

        # FETCH THE UNIQUE AUTH ID FOR THIS PROJECT
        project_data = db_select_one("projects", {"id": project_id}, "gorilla_auth_id")
        auth_id = project_data.get("gorilla_auth_id", "") if project_data else ""

        # ⚡ FETCH SUPABASE KEYS FROM VFS .ENV FILE ⚡
        env_file = db_select_one("files", {"project_id": project_id, "path": ".env"})
        supa_url = ""
        supa_key = ""
        if env_file and env_file.get("content"):
            for line in env_file.get("content", "").split("\n"):
                if line.startswith("VITE_SUPABASE_URL="): 
                    supa_url = line.split("=", 1)[1].strip()
                if line.startswith("VITE_SUPABASE_ANON_KEY="): 
                    supa_key = line.split("=", 1)[1].strip()

        vercel_json_content = {
            "version": 2,
            "builds": [
                {"src": "server.js", "use": "@vercel/node"},
                {"src": "package.json", "use": "@vercel/static-build"}
            ],
            "rewrites": [
                {"source": "/api/(.*)", "destination": "/server.js"},
                {"source": "/(.*)", "destination": "/index.html"}
            ],
            "env": {
                "GORILLA_API_KEY": api_key,
                "VITE_GORILLA_AUTH_ID": auth_id,
                "VITE_SUPABASE_URL": supa_url,         # <--- INJECTED FOR VERCEL
                "VITE_SUPABASE_ANON_KEY": supa_key     # <--- INJECTED FOR VERCEL
            }
        }
        
        # PROPERLY UPSERT INTO THE FILES TABLE
        db_upsert(
            "files", 
            {
                "project_id": project_id, 
                "path": "vercel.json", 
                "content": json.dumps(vercel_json_content, indent=2)
            }, 
            on_conflict="project_id,path"
        )

        optimization_prompt = """
        We are deploying this full-stack application to Vercel Serverless. You MUST perform this exactly:
        Overwrite `server.js` completely. Do NOT call `app.listen(...)` at the bottom. Vercel requires you to EXPORT the express app instead. End the file with `export default app;` or `module.exports = app;`.
        Do NOT create or modify `vercel.json` (it has already been handled).   
        """
        
        await run_agent_loop(
            project_id=project_id,
            prompt=optimization_prompt,
            user_id=user["id"],
            agent_type="fast",
            skip_planner=True,
            is_system_task=True
        )
        
        try:
            supabase.table("projects").update({"vercel_optimized": True}).eq("id", project_id).execute()
        except Exception:
            pass

        return {"status": "ok", "detail": "Optimized for Vercel"}
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, detail=str(e))

# ==========================================================================
# DEPLOY PUSH (Ships the optimized code to GitHub to trigger Vercel)
# ==========================================================================
@app.post("/api/project/{project_id}/deploy-push")
async def push_for_deployment(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    try:
        user_data = db_select_one("users", {"id": user["id"]}, "github_access_token")
        if not user_data or not user_data.get("github_access_token"):
            return JSONResponse({"detail": "GitHub account not connected. Please link GitHub in settings."}, status_code=400)
        
        token = user_data["github_access_token"]
        
        res = supabase.table("projects").select("*").eq("id", project_id).single().execute()
        project = res.data
        
        # Sanitize to replace spaces and special characters with hyphens (required by GitHub)
        raw_name = project.get("name", "gorilla-project")
        repo_name = re.sub(r'[^a-z0-9-]', '-', raw_name.lower()).strip('-')
        
        if not repo_name:
            repo_name = f"gorilla-project-{project_id[:6]}"
            
        files_res = supabase.table("files").select("path,content").eq("project_id", project_id).execute()
        files = getattr(files_res, "data", [])
        if not files and isinstance(files_res, list): files = files_res

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}
        
        async with httpx.AsyncClient() as client:
            # A. Create or Find Repository
            repo_res = await client.post(
                "https://api.github.com/user/repos", 
                json={"name": repo_name, "private": True, "auto_init": True}, 
                headers=headers
            )
            if repo_res.status_code not in [201, 422]:
                return JSONResponse({"detail": f"GitHub Repo Creation Failed: {repo_res.text}"}, status_code=500)
            
            repo_data = repo_res.json()
            full_name = repo_data.get("full_name")
            
            # If repo already exists (422)
            if repo_res.status_code == 422:
                user_info_res = await client.get("https://api.github.com/user", headers=headers)
                if user_info_res.status_code == 200:
                    login = user_info_res.json().get("login")
                    full_name = f"{login}/{repo_name}"
                else:
                    return JSONResponse({"detail": "Failed to fetch GitHub username for existing repo."}, status_code=500)
                
                # SAFETY NET: Check if the existing repo is completely empty!
                branch_res = await client.get(f"https://api.github.com/repos/{full_name}/branches/main", headers=headers)
                if branch_res.status_code == 404:
                    import base64
                    readme_content = base64.b64encode(b"# Init\n").decode("utf-8")
                    await client.put(
                        f"https://api.github.com/repos/{full_name}/contents/README.md",
                        json={"message": "Initialize empty repository", "content": readme_content},
                        headers=headers
                    )

            # B. Bulk Create Blobs & Tree
            tree = []
            for f in files:
                path = f.get("path", "")
                if path.startswith(".gorilla/"): continue
                
                # GitHub API rejects strictly empty strings for inline blobs. Give it a single space.
                content = f.get("content")
                if not content:
                    content = " "
                    
                tree.append({
                    "path": path.lstrip("/"),
                    "mode": "100644",
                    "type": "blob",
                    "content": content
                })
            
            if len(tree) == 0:
                tree.append({
                    "path": "README.md",
                    "mode": "100644",
                    "type": "blob",
                    "content": f"# {project.get('name', 'Gorilla Project')}\n\nAuto-generated by Gor://a Builder."
                })
                
            tree_res = await client.post(f"https://api.github.com/repos/{full_name}/git/trees", json={"tree": tree}, headers=headers)
            
            if tree_res.status_code != 201:
                return JSONResponse({"detail": f"Git Tree Error: {tree_res.text}"}, status_code=500)
                
            tree_sha = tree_res.json()["sha"]
            
            # C. Create Commit
            commit_res = await client.post(
                f"https://api.github.com/repos/{full_name}/git/commits", 
                json={"message": "Git Commit via Gor://a Builder", "tree": tree_sha}, 
                headers=headers
            )
            if commit_res.status_code != 201:
                return JSONResponse({"detail": f"Commit Error: {commit_res.text}"}, status_code=500)
            
            commit_sha = commit_res.json()["sha"]
            
            # D. Update Reference (Create or Update Main Branch)
            ref_res = await client.post(f"https://api.github.com/repos/{full_name}/git/refs", json={"ref": "refs/heads/main", "sha": commit_sha}, headers=headers)
            if ref_res.status_code == 422: # Reference already exists, force update it
                await client.patch(f"https://api.github.com/repos/{full_name}/git/refs/heads/main", json={"sha": commit_sha, "force": True}, headers=headers)
            
            repo_url = f"https://github.com/{full_name}"
            
            # E. Save to DB
            supabase.table("projects").update({"github_repo_url": repo_url}).eq("id", project_id).execute()
            
            return JSONResponse({
                "status": "ok", 
                "detail": "Code successfully pushed to GitHub.",
                "repo_url": repo_url,
                "full_name": full_name
            })
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"detail": str(e)}, status_code=500)

# 4. Push to GitHub

@app.post("/projects/{project_id}/github/publish")
async def publish_to_github(request: Request, project_id: str):
    try:
        user = get_current_user(request)
        
        proj_check = supabase.table("projects").select("owner_id").eq("id", project_id).maybe_single().execute()
        if not proj_check or not proj_check.data:
            return JSONResponse({"detail": "Project not found."}, status_code=404)
        if proj_check.data["owner_id"] != user["id"]:
            return JSONResponse({"detail": "Unauthorized"}, status_code=403)
        
        user_data = db_select_one("users", {"id": user["id"]}, "github_access_token")
        if not user_data or not user_data.get("github_access_token"):
            return JSONResponse({
                "detail": (
                    "GitHub account not connected. Click the GitHub button in the top-right "
                    "of the editor to connect your account — no token pasting required. "
                    "Once connected, click the button again to push your code."
                ),
                "connect_url": "/auth/github/link"
            }, status_code=400)
        
        token = user_data["github_access_token"]
        
        res = supabase.table("projects").select("*").eq("id", project_id).single().execute()
        project = res.data

        # If this project is linked to an existing repo (e.g. imported via
        # "Import a Codebase" with push-back enabled), push straight to THAT
        # repo instead of creating/finding a new one named after the Gorilla
        # project — those are two different repos and silently picking the
        # wrong one would fork the user's history.
        linked_repo_url = (project.get("github_repo_url") or "").strip()
        linked_full_name = None
        if linked_repo_url:
            m = re.search(r"github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", linked_repo_url)
            if m:
                linked_full_name = f"{m.group(1)}/{m.group(2)}"

        # --- Otherwise: make repo name match the Gorilla project name ---
        raw_name = project.get("name", "gorilla-project")
        # Sanitize to replace spaces and special characters with hyphens (required by GitHub)
        repo_name = re.sub(r'[^a-z0-9-]', '-', raw_name.lower()).strip('-')

        # Fallback just in case the name was completely invalid characters
        if not repo_name:
            repo_name = f"gorilla-project-{project_id[:6]}"

        files_res = supabase.table("files").select("path,content").eq("project_id", project_id).execute()
        files = getattr(files_res, "data", [])
        if not files and isinstance(files_res, list): files = files_res

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}

        async with httpx.AsyncClient() as client:
            if linked_full_name:
                # B'. Verify the linked repo still exists/is reachable, then
                # skip straight to pushing — no create-or-find step needed.
                check_res = await client.get(f"https://api.github.com/repos/{linked_full_name}", headers=headers)
                if check_res.status_code != 200:
                    return JSONResponse({
                        "detail": f"Linked repo {linked_full_name} is no longer reachable: {check_res.text}"
                    }, status_code=400)
                full_name = linked_full_name
            else:
                # A. Create or Find Repository
                repo_res = await client.post(
                    "https://api.github.com/user/repos",
                    json={"name": repo_name, "private": True, "auto_init": True},
                    headers=headers
                )
                if repo_res.status_code not in [201, 422]:
                    return JSONResponse({"detail": f"GitHub Repo Creation Failed: {repo_res.text}"}, status_code=500)

                repo_data = repo_res.json()
                full_name = repo_data.get("full_name")

                # If repo already exists (422)
                if repo_res.status_code == 422:
                    user_info_res = await client.get("https://api.github.com/user", headers=headers)
                    if user_info_res.status_code == 200:
                        login = user_info_res.json().get("login")
                        full_name = f"{login}/{repo_name}"
                    else:
                        return JSONResponse({"detail": "Failed to fetch GitHub username for existing repo."}, status_code=500)

                    # SAFETY NET: Check if the existing repo is completely empty!
                    branch_res = await client.get(f"https://api.github.com/repos/{full_name}/branches/main", headers=headers)
                    if branch_res.status_code == 404:
                        # The repo exists but is empty! Initialize it manually via the Contents API
                        readme_content = base64.b64encode(b"# Init\n").decode("utf-8")
                        await client.put(
                            f"https://api.github.com/repos/{full_name}/contents/README.md",
                            json={"message": "Initialize empty repository", "content": readme_content},
                            headers=headers
                        )

            # B. Bulk Create Blobs & Tree
            tree = []
            for f in files:
                path = f.get("path", "")
                if path.startswith(".gorilla/"): continue
                
                # GitHub API rejects strictly empty strings for inline blobs. Give it a single space.
                content = f.get("content")
                if not content:
                    content = " "
                    
                tree.append({
                    "path": path.lstrip("/"),
                    "mode": "100644",
                    "type": "blob",
                    "content": content
                })
            
            # If the tree is completely empty, inject a default README so the commit always succeeds.
            if len(tree) == 0:
                tree.append({
                    "path": "README.md",
                    "mode": "100644",
                    "type": "blob",
                    "content": f"# {project.get('name', 'Gorilla Project')}\n\nAuto-generated by Gor://a Builder."
                })
                
            tree_res = await client.post(f"https://api.github.com/repos/{full_name}/git/trees", json={"tree": tree}, headers=headers)
            
            if tree_res.status_code != 201:
                return JSONResponse({"detail": f"Git Tree Error: {tree_res.text}"}, status_code=500)
                
            tree_sha = tree_res.json()["sha"]
            
            # C. Create Commit
            commit_res = await client.post(f"https://api.github.com/repos/{full_name}/git/commits", json={"message": "Publish via Gor://a Builder", "tree": tree_sha}, headers=headers)
            if commit_res.status_code != 201:
                return JSONResponse({"detail": f"Commit Error: {commit_res.text}"}, status_code=500)
            commit_sha = commit_res.json()["sha"]
            
            # D. Update Reference (Create or Update Main Branch)
            ref_res = await client.post(f"https://api.github.com/repos/{full_name}/git/refs", json={"ref": "refs/heads/main", "sha": commit_sha}, headers=headers)
            if ref_res.status_code == 422: # Reference already exists, force update it
                await client.patch(f"https://api.github.com/repos/{full_name}/git/refs/heads/main", json={"sha": commit_sha, "force": True}, headers=headers)
            
            repo_url = f"https://github.com/{full_name}"
            
            # E. Save to DB
            supabase.table("projects").update({"github_repo_url": repo_url}).eq("id", project_id).execute()
            
            return JSONResponse({"status": "ok", "repo_url": repo_url})
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"detail": str(e)}, status_code=500)


# ==========================================================================
# FILE API ROUTES
# ==========================================================================

LOCKFILE_PATTERNS = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "node_modules", ".git"
]

@app.get("/api/project/{project_id}/files")
async def get_project_files(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    res = (
        supabase.table("files")
        .select("path,content")
        .eq("project_id", project_id)
        .execute()
    )
    if asyncio.iscoroutine(res): res = await res
    rows = getattr(res, "data", [])
    if not rows and isinstance(res, list): rows = res

    clean_rows = []
    for r in rows:
        path = r.get("path", "")
        content = r.get("content", "")

        # Skip lockfiles / node_modules / git
        if any(x in path for x in LOCKFILE_PATTERNS):
            continue

        # For binary assets: swap Storage URL → empty string so the
        # editor doesn't try to render a URL as code. The sandbox has
        # the real file; the frontend should use the URL only for <img>.
        if _is_binary_path(path) and (content or "").startswith("http"):
            clean_rows.append({
                "path": path,
                "content": "",           # editor gets blank — it's a binary
                "asset_url": content,    # frontend can use this for previews
                "is_binary": True,
            })
        else:
            clean_rows.append({"path": path, "content": content})

    return {"files": clean_rows}


@app.get("/api/project/{project_id}/file")
async def get_file_content(request: Request, project_id: str, path: str):
    # Block lockfiles
    if any(x in path for x in LOCKFILE_PATTERNS):
        return JSONResponse({
            "content": "// Lockfiles are hidden by the system.",
            "is_binary": False,
        })

    try:
        res = (
            supabase.table("files")
            .select("content")
            .eq("project_id", project_id)
            .eq("path", path)
            .execute()
        )
        if asyncio.iscoroutine(res): res = await res
        content = ""
        if res.data and len(res.data) > 0:
            content = res.data[0].get("content", "")

        # Binary asset — return the Storage URL separately
        # so the frontend can render <img src={asset_url} /> directly
        if _is_binary_path(path) and content.startswith("http"):
            return JSONResponse({
                "content": "",
                "asset_url": content,
                "is_binary": True,
            })

        return JSONResponse({"content": content, "is_binary": False})

    except Exception as e:
        return JSONResponse({"content": f"// Error loading file: {e}", "is_binary": False})

import os
import re
import base64
import mimetypes
from fastapi import Request, HTTPException

# ---------------------------------------------------------------------------
# Binary extension detection
# ---------------------------------------------------------------------------
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".ico", ".bmp", ".tiff", ".woff", ".woff2", ".ttf",
    ".eot", ".otf", ".mp3", ".mp4", ".wav", ".ogg",
    ".pdf", ".zip", ".tar", ".gz",
}

def _is_binary_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in BINARY_EXTENSIONS


async def _upload_asset_to_storage(
    project_id: str,
    rel_path: str,
    file_bytes: bytes,
) -> str:
    """
    Upload binary bytes to Supabase Storage.
    Returns the public URL.
    """
    mime_type, _ = mimetypes.guess_type(rel_path)
    mime_type = mime_type or "application/octet-stream"
    storage_path = f"{project_id}/{rel_path}"

    supabase.storage.from_("project-assets").upload(
        storage_path,
        file_bytes,
        {"content-type": mime_type, "upsert": True},
    )
    return supabase.storage.from_("project-assets").get_public_url(storage_path)


async def _write_binary_to_sandbox(
    project_id: str,
    rel_path: str,
    file_bytes: bytes,
) -> None:
    """
    Write real binary bytes to a live sandbox via base64 pipe.
    Safe for any binary format — avoids heredoc mangling.
    """
    if not (_sandbox_manager and _sandbox_manager.is_running(project_id)):
        return
    APP_DIR = "/home/user/app"
    full_path = f"{APP_DIR}/{rel_path}"
    dirp = "/".join(full_path.split("/")[:-1])
    b64 = base64.b64encode(file_bytes).decode()
    try:
        await _sandbox_manager._execute_commands_streaming(project_id, [
            f"mkdir -p '{dirp}' && printf '%s' '{b64}' | base64 -d > '{full_path}'"
        ])
    except Exception as e:
        print(f"⚠️ Sandbox binary write failed for {rel_path}: {e}")


# ---------------------------------------------------------------------------
# /save route
# ---------------------------------------------------------------------------
@app.post("/api/project/{project_id}/save")
async def save_file(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
 
    form_data = await request.form()
    file_path  = form_data.get("file")
    content_obj = form_data.get("content")
 
    if not file_path or content_obj is None:
        raise HTTPException(status_code=400, detail="Missing file path or content")
 
    rel_path = _sanitize_file_path(str(file_path))  # ✅ sanitized — no path traversal
 
    # ── Binary file (PNG, font, etc.) ───────────────────────────────────────
    if hasattr(content_obj, "filename") and _is_binary_path(rel_path):
        file_bytes = await content_obj.read()
        try:
            public_url = await _upload_asset_to_storage(project_id, rel_path, file_bytes)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Storage upload failed: {e}")
        db_upsert(
            "files",
            {"project_id": project_id, "path": rel_path, "content": public_url},
            on_conflict="project_id,path",
        )
        await _write_binary_to_sandbox(project_id, rel_path, file_bytes)
 
    # ── Text file ────────────────────────────────────────────────────────────
    else:
        if hasattr(content_obj, "filename"):
            file_bytes = await content_obj.read()
            try:
                final_content = file_bytes.decode("utf-8")
            except UnicodeDecodeError:
                mime_type, _ = mimetypes.guess_type(rel_path)
                b64 = base64.b64encode(file_bytes).decode("utf-8")
                final_content = f"data:{mime_type or 'application/octet-stream'};base64,{b64}"
        else:
            final_content = str(content_obj)
 
        db_upsert(
            "files",
            {"project_id": project_id, "path": rel_path, "content": final_content},
            on_conflict="project_id,path",
        )
        if _sandbox_manager and _sandbox_manager.is_running(project_id):
            try:
                await _sandbox_manager.write_file(project_id, rel_path, final_content)
            except Exception as e:
                print(f"⚠️ Sandbox text write mirror failed: {e}")
 
    supabase.table("projects").update({"updated_at": "now()"}).eq("id", project_id).execute()
    return {"success": True}

@app.post("/api/project/{project_id}/delete")
async def delete_project(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    try:
        # Kill live sandbox first
        if _sandbox_manager and _sandbox_manager.is_running(project_id):
            try:
                await _sandbox_manager.kill(project_id)
            except Exception as e:
                print(f"Sandbox kill during delete failed: {e}")

        # Pull all file paths so we can remove binary assets from Storage
        files_res = supabase.table("files").select("path, content").eq("project_id", project_id).execute()
        rows = getattr(files_res, "data", []) or []

        # Delete any Storage objects that belong to this project
        storage_paths = [
            f"{project_id}/{r['path']}"
            for r in rows
            if _is_binary_path(r.get("path", ""))
            and (r.get("content") or "").startswith("http")
        ]
        if storage_paths:
            try:
                supabase.storage.from_("project-assets").remove(storage_paths)
            except Exception as e:
                print(f"Storage cleanup warning: {e}")

        # Delete DB rows
        supabase.table("files").delete().eq("project_id", project_id).execute()
        supabase.table("projects").delete().eq("id", project_id).execute()

        return JSONResponse({"status": "success", "detail": "Project deleted."})
    except Exception as e:
        print(f"Project deletion error: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete project.")


@app.get("/api/project/{project_id}/tokens")
async def check_tokens(request: Request, project_id: str):
    user = get_current_user(request)
    used, limit = get_token_usage_and_limit(user["id"])
    return {"used": used, "limit": limit}


# ==========================================================================
# STATIC FILE SERVING & WEBCONTAINER SUPPORT
# ==========================================================================
import mimetypes

def _guess_media_type(path: str) -> str:
    mt, _ = mimetypes.guess_type(path)
    if mt: return mt
    if path.endswith(".js"): return "application/javascript"
    if path.endswith(".css"): return "text/css"
    if path.endswith(".html"): return "text/html"
    if path.endswith(".json"): return "application/json"
    return "text/plain"


@app.get("/app/{project_id}/{path:path}")
async def serve_project_file(request: Request, project_id: str, path: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    if not path or path.endswith("/"): 
        path = (path or "") + "index.html"
        
    res = (
        supabase.table("files")
        .select("content")
        .eq("project_id", project_id)
        .eq("path", path)
        .maybe_single()
        .execute()
    )
    if asyncio.iscoroutine(res): res = await res

    row = res.data if res else None
    
    if not row:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
        
    return Response(
        content=row.get("content", ""), 
        media_type=_guess_media_type(path)
    )


# ==========================================================================
# EVENT BUS & UTILS
# ==========================================================================
import json

class _ProgressBus:
    """Sequenced, replayable, thread-safe event bus.

    Fixes two production failure modes:

    1. LOST EVENTS ON RECONNECT — every event gets a monotonically increasing
       sequence id. Reconnecting SSE clients send back the last id they saw
       (?last_id=N) and we replay everything after it from a 10-minute buffer.
       Brand-new subscribers (no last_id) replay only the last RECENT_WINDOW_S
       seconds, which closes the connect-vs-first-emit race on cold loads
       without duplicating old history.

    2. CROSS-THREAD CORRUPTION — the E2B sandbox manager and to_thread-offloaded
       DB code call emit_* from WORKER THREADS. asyncio.Queue is not thread-safe;
       put_nowait from the wrong thread can silently drop events or lose waiter
       wakeups. We bind the main loop at startup and marshal cross-thread emits
       with call_soon_threadsafe.

    Still in-memory ⇒ SINGLE PROCESS ONLY. Run exactly one uvicorn worker and
    pin Koyeb to one instance (scale min = max = 1). Multi-instance needs Redis
    pub/sub instead.
    """

    RECENT_WINDOW_S = 15.0     # replay window for brand-new subscribers
    BUFFER_TTL_S    = 600.0    # how far back a reconnecting client can resume
    BUFFER_CAP      = 500      # per-project buffered events

    def __init__(self):
        self._queues: Dict[str, List[asyncio.Queue]] = {}
        self._buffer: Dict[str, List[tuple]] = {}   # (seq, ts, event)
        # Seed from wall clock so ids stay monotonic across restarts — a client
        # resuming with a pre-restart id can never mask post-restart events.
        self._next_seq: int = int(time.time() * 1000)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Call once at startup with the main event loop."""
        self._loop = loop

    def subscribe(self, project_id: str, last_id: Optional[int] = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues.setdefault(project_id, []).append(q)

        now = time.time()
        buf = self._buffer.get(project_id, [])
        if last_id is not None:
            # Reconnect: replay everything the client missed.
            replay = [(s, ev) for (s, ts, ev) in buf if s > last_id]
        else:
            # Fresh connect: only very recent events (race-closing, no dupes).
            replay = [(s, ev) for (s, ts, ev) in buf
                      if (now - ts) <= self.RECENT_WINDOW_S]
        for item in replay:
            try:
                q.put_nowait(item)
            except Exception:
                pass
        return q

    def unsubscribe(self, project_id: str, q: asyncio.Queue) -> None:
        subs = self._queues.get(project_id)
        if subs and q in subs:
            subs.remove(q)
        if subs is not None and not subs:
            self._queues.pop(project_id, None)

    def emit(self, project_id: str, event: Dict[str, Any]) -> None:
        self._next_seq += 1
        seq = self._next_seq
        item = (seq, event)

        # Buffer immediately (list append is fine under the GIL).
        buf = self._buffer.setdefault(project_id, [])
        buf.append((seq, time.time(), event))
        if len(buf) > self.BUFFER_CAP:
            del buf[: len(buf) - self.BUFFER_CAP]
        cutoff = time.time() - self.BUFFER_TTL_S
        while buf and buf[0][1] < cutoff:
            buf.pop(0)

        def _deliver():
            for q in list(self._queues.get(project_id, [])):
                try:
                    q.put_nowait(item)
                except Exception:
                    pass

        # Deliver on the MAIN loop, always. If we're on a worker thread
        # (sandbox-manager callbacks, asyncio.to_thread DB code), marshal
        # over safely instead of poking asyncio.Queue cross-thread.
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is not None and (self._loop is None or running is self._loop):
            _deliver()
        elif self._loop is not None:
            self._loop.call_soon_threadsafe(_deliver)
        else:
            _deliver()  # startup edge case: no loop bound yet

progress_bus = _ProgressBus()

def emit_log(pid: str, role: str, text: str) -> None:
    """Emits log to UI via SSE and archives non-UI roles to virtual FS."""
    progress_bus.emit(pid, {"type": "log", "role": role, "text": text})
 
    if role not in ["user", "assistant", "system"] and pid:
        try:
            existing = db_select_one("files", {"project_id": pid, "path": ".gorilla/thoughts.json"})
            logs = []
            if existing and existing.get("content"):
                try:
                    logs = json.loads(existing.get("content"))
                except Exception:
                    pass
            logs.append({"role": role, "text": text, "ts": time.time()})
            if len(logs) > 100:
                logs = logs[-100:]
            db_upsert(
                "files",
                {"project_id": pid, "path": ".gorilla/thoughts.json", "content": json.dumps(logs)},
                on_conflict="project_id,path",
            )
        except Exception:
            pass

def emit_status(pid: str, text: str) -> None:
    progress_bus.emit(pid, {"type": "status", "text": text})

def emit_phase(pid: str, value: str) -> None:
    progress_bus.emit(pid, {"type": "phase", "value": value})

def emit_progress(pid: str, text: str, pct: float) -> None:
    progress_bus.emit(pid, {"type": "progress", "text": text, "pct": pct})

def emit_file_changed(pid: str, path: str) -> None:
    progress_bus.emit(pid, {"type": "file_changed", "path": path})

def emit_token_update(pid: str, used: int) -> None:
    progress_bus.emit(pid, {"type": "token_usage", "used": used})

def filtered_log_callback(project_id: str, role: str, text: str) -> None:
    """Bridge from lineage_agent\'s log_agent() to the SSE event bus."""
    role_lc = (role or "").strip().lower()
    if role_lc in ("llm", "internal"):
        return
    try:
        emit_log(project_id, role_lc, text)
    except Exception as e:
        print(f"filtered_log_callback failed: {e}")


# ==========================================================================
# GATEKEEPER: LINTING
# ==========================================================================
import tempfile
import subprocess
import os

def lint_code_with_esbuild(content: str, filename: str) -> str | None:
    if not filename.startswith("static/") or not filename.endswith(".js"):
        return None

    try:
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False, mode='w', encoding='utf-8') as tmp:
            tmp.write(content)
            tmp.write_path = tmp.name

        result = subprocess.run(
            ["npx", "esbuild", tmp.name, "--loader=jsx", "--format=esm", "--log-level=error"],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        os.remove(tmp.name)

        if result.returncode != 0:
            return result.stderr.strip()
            
        return None
    except Exception as e:
        print(f"⚠️ Linter failed to run: {e}")
        return None


# ==========================================================================
# AI AGENT WORKFLOW (TRIGGER)
# ==========================================================================
async def _fetch_file_tree(project_id: str) -> Dict[str, str]:
    """
    Fetches the project files but strictly filters out massive lockfiles 
    to prevent AI context bloat and truncation errors.
    """
    try:
        query = supabase.table("files").select("path,content").eq("project_id", project_id)
        # Sync HTTP call — off-load so it can't freeze SSE/keep-alives while
        # pulling a large file tree.
        res = await asyncio.to_thread(query.execute)
            
        rows = getattr(res, "data", [])
        if not rows and isinstance(res, list):
            rows = res
            
        # 🛑 SHARK FILTER: Exclude files that cause 'Expected , or }' errors
        filtered_tree = {}
        for r in rows:
            path = r.get("path", "")
            if not path: continue
            
            # Skip giant cloggers that the AI doesn't need to read
            if any(x in path for x in ["package-lock.json", "yarn.lock", "node_modules", ".git"]):
                continue
                
            filtered_tree[path] = (r.get("content") or "")
            
        return filtered_tree
        
    except Exception as e:
        print(f"⚠️ Fetch Error: {e}")
        return {}

@app.post("/api/project/{project_id}/agent/start")
async def agent_start(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    existing = _AGENT_TASKS_BY_PROJECT.get(project_id)
    if existing and not existing.done():
        return {"started": False, "error": "Agent is already running for this project."}

    try:
        await asyncio.to_thread(enforce_token_limit_or_raise, user["id"])
    except HTTPException as e:
        if e.status_code == 402:
            emit_log(project_id, "assistant", _render_token_limit_message())
            return {"started": False}
        raise

    form_data = await request.form()
    prompt = str(form_data.get("prompt", ""))
    image_base64 = form_data.get("image_base64")
    skip_planner = str(form_data.get("skip_planner", "false")).lower() == "true"

    # Explicit mode selector (replaces the old "prepend a magic instruction
    # string to the visible prompt" hack) — see lineage_agent.MODE_INSTRUCTIONS.
    think_mode = str(form_data.get("think_mode", "normal")).lower()
    if think_mode not in ("fast", "normal", "deep"):
        think_mode = "normal"

    # is_db_request still matters — it routes to mid-chat Supabase
    # provisioning when the project doesn\'t have supabase_project_ref yet.
    is_db_request = str(form_data.get("is_db_request", "false")).lower() == "true"
    agent_type = "supabase" if is_db_request else "fast"
 
    if image_base64:
        try:
            db_upsert(
                "files",
                {
                    "project_id": project_id,
                    "path": ".gorilla/prompt_image.b64",
                    "content": image_base64,
                },
                on_conflict="project_id,path",
            )
        except Exception as err:
            print(f"Mid-chat image save failed: {err}")
 
    if not skip_planner:
        emit_status(project_id, "Agent received prompt")
        emit_log(project_id, "user", prompt)
 
    task = asyncio.create_task(
        run_agent_loop(
            project_id=project_id,
            prompt=prompt,
            user_id=user["id"],
            agent_type=agent_type,
            skip_planner=skip_planner,
            is_system_task=False,
            think_mode=think_mode,
        )
    )
    _AGENT_TASKS.add(task)
    task.add_done_callback(_AGENT_TASKS.discard)
    _AGENT_TASKS_BY_PROJECT[project_id] = task
    task.add_done_callback(
        lambda t: _AGENT_TASKS_BY_PROJECT.pop(project_id, None)
        if _AGENT_TASKS_BY_PROJECT.get(project_id) is t else None
    )

    # Also surface exceptions that the task raises — without this, any
    # error inside run_agent_loop that isn\'t caught by its own try/except
    # would be swallowed silently.
    def _log_task_exception(t: asyncio.Task):
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            import traceback
            print(f"⚠️ agent task crashed: {exc}")
            traceback.print_exception(type(exc), exc, exc.__traceback__)
    task.add_done_callback(_log_task_exception)
    progress_bus.emit(project_id, {"type": "agent_queued"})  # ADD THIS LINE
    return {"started": True}

@app.post("/api/project/{project_id}/agent/resume")
async def agent_resume(request: Request, project_id: str):
    """
    Called by the frontend after the user completes a user_action
    (connected Supabase or submitted env vars). Unblocks the paused agent loop.
    """
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    payload = await request.json()
    action_type = payload.get("type")       # "connect_supabase" | "set_env_vars"
    data        = payload.get("data", {})   # {"vars": {"KEY": "val", ...}} for env

    # If env vars were submitted, persist them into .env before resuming
    if action_type == "set_env_vars" and data.get("vars"):
        env_vars = data["vars"]
        try:
            existing = db_select_one("files", {"project_id": project_id, "path": ".env"})
            current_content = (existing or {}).get("content", "") or ""
            # Append new vars, overwriting any existing keys with same name
            existing_lines = {
                line.split("=", 1)[0]: line
                for line in current_content.splitlines()
                if "=" in line
            }
            for k, v in env_vars.items():
                existing_lines[k.strip()] = f"{k.strip()}={v.strip()}"
            new_content = "\n".join(existing_lines.values())
            db_upsert(
                "files",
                {"project_id": project_id, "path": ".env", "content": new_content},
                on_conflict="project_id,path",
            )
            # Mirror to live sandbox if running
            if _sandbox_manager and _sandbox_manager.is_running(project_id):
                await _sandbox_manager.write_file(project_id, ".env", new_content)
        except Exception as e:
            print(f"⚠️ env var injection failed: {e}")

    # If Supabase was connected, update project has_supabase flag for the agent
    if action_type == "connect_supabase":
        # The user's supabase_access_token is now set (OAuth callback already ran).
        # The agent loop will re-read it on its next turn.
        pass

    # Unblock the waiting agent loop
    ev = _AGENT_PAUSE_EVENTS.get(project_id)
    if ev:
        _AGENT_PAUSE_DATA[project_id] = {"type": action_type, "data": data}
        ev.set()
        return JSONResponse({"status": "resumed"})

    return JSONResponse({"status": "no_paused_agent"}, status_code=404)

@app.get("/api/project/{project_id}/events")
async def agent_events(request: Request, project_id: str):
    if not DEV_MODE:
        try:
            user = get_current_user(request)
            _require_project_owner(user, project_id)
        except HTTPException:
            # Return a PLAIN 401 — never the 303 redirect. EventSource can't
            # follow redirects to HTML; it just error-loops with no signal.
            # The frontend auth probe detects this 401 and sends the user
            # to /login instead of retrying forever.
            return Response(status_code=401, media_type="text/plain",
                            content="unauthorized")

    # Resume cursor: our client passes ?last_id on manual reconnects; native
    # EventSource auto-reconnect sends the Last-Event-ID header. Honor both.
    last_id: Optional[int] = None
    raw_last = request.query_params.get("last_id") or request.headers.get("last-event-id")
    if raw_last:
        try:
            last_id = int(raw_last)
        except (TypeError, ValueError):
            last_id = None

    async def _gen():
        q = progress_bus.subscribe(project_id, last_id=last_id)
        try:
            yield ":" + " " * 2048 + "\n\n"       # prime proxy buffers, force flush
            yield "retry: 2000\n\n"
            yield f"data: {json.dumps({'type': 'sse_open'})}\n\n"
            while True:
                try:
                    seq, ev = await asyncio.wait_for(q.get(), timeout=10)
                    yield f"id: {seq}\ndata: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ka\n\n"              # heartbeat every 10s
        finally:
            progress_bus.unsubscribe(project_id, q)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            # no-transform tells intermediaries not to buffer/compress the stream
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

from fastapi.responses import HTMLResponse
@app.get("/projects/{project_id}/game", response_class=HTMLResponse)
async def project_game(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    return templates.TemplateResponse("projects/game.html", {"request": request, "project_id": project_id, "user": user})

@app.post("/api/project/{project_id}/agent/ping")
async def agent_ping(request: Request, project_id: str):
    emit_log(project_id, "system", "🔥 Pong from backend")
    return {"ok": True}

import time
@app.get("/health")
async def health():
    return {"ok": True, "ts": int(time.time()), "dev_mode": DEV_MODE}

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

# ==========================================================================
# THE MONKE NEGOTIATOR ROUTES
# ==========================================================================

class NegotiationResult(BaseModel):
    agreed_price: float

@app.get("/pricing/negotiate", response_class=HTMLResponse)
async def serve_negotiator(request: Request):
    """Serves the monkey.html UI with FULL user data fetched from the DB."""
    session_user = get_current_user(request) 
    if not session_user:
        return RedirectResponse(url="/auth/login")
        
    user_id = session_user["id"]
    
    # 🛑 THE FIX: We MUST fetch the API key and live stats from the database!
    try:
        res = supabase.table("users").select("*").eq("id", user_id).single().execute()
        if not res or not res.data:
            return RedirectResponse(url="/auth/login")
            
        db_user = res.data
        
        # Calculate live tokens for the UI
        used, limit = get_token_usage_and_limit(user_id) 
        db_user["tokens"] = {"remaining": max(0, limit - used)}
        
    except Exception as e:
        print(f"Error fetching user for negotiation: {e}")
        return RedirectResponse(url="/dashboard")

    # Now db_user absolutely contains 'gorilla_api_key' and 'tokens'
    return templates.TemplateResponse("freemium/monkey.html", {
        "request": request, 
        "user": db_user
    })

@app.post("/api/pricing/save-negotiation")
async def save_negotiation(request: Request, data: NegotiationResult):
    session_user = get_current_user(request)
    if not session_user:
        raise HTTPException(status_code=401, detail="Not logged in")
 
    final_price = data.agreed_price
 
    # ✅ Floor AND ceiling — can't be spoofed below $0.67 or above $99.99
    final_price = max(0.67, min(99.99, float(final_price)))
    # ✅ Round to 2dp — no floating point artifacts in DB
    final_price = round(final_price, 2)
 
    try:
        supabase.table("users").update({
            "first_month_price": str(final_price)
        }).eq("id", session_user["id"]).execute()
 
        print(f"💰 User {session_user['id']} negotiated to ${final_price}")
        return JSONResponse({"status": "success", "checkout_url": "/checkout/premium"})
    except Exception as e:
        raise HTTPException(status_code=500, detail="Database error while saving price.")


@app.on_event("startup")
async def start_background_tasks():
    # Bind the main loop so worker-thread emits can be marshalled safely.
    progress_bus.bind_loop(asyncio.get_running_loop())
    asyncio.create_task(_cleanup_expired_otps())
 
async def _cleanup_expired_otps():
    """Purge abandoned OTP sessions every 5 minutes to prevent memory growth."""
    while True:
        await asyncio.sleep(300)
        cutoff  = time.time() - 600
        expired = [e for e, r in list(PENDING_SIGNUPS.items()) if r.get("ts", 0) < cutoff]
        for email in expired:
            PENDING_SIGNUPS.pop(email, None)
        if expired:
            print(f"🧹 Cleaned {len(expired)} expired OTP session(s)")


# ==========================================================================
# ROUTER REGISTRATION — split-out route modules (backend/routers/*)
# These are imported here (near the bottom of the file) rather than at the
# top, because each router does `import app` and reaches back into this
# module for shared state (supabase client, DB helpers, templates, token
# helpers, etc.) at call time. Importing them before those globals/helpers
# are defined above would leave the routers with missing attributes; doing
# the import here — after everything they need already exists on this
# module — avoids a circular-import failure.
# ==========================================================================

from backend.routers.auth import router as auth_router
from backend.routers.design import router as design_router
from backend.routers.gateway import router as gateway_router

app.include_router(auth_router)
app.include_router(design_router)
app.include_router(gateway_router)

# ==========================================================================
# STARTUP WIRING — ORDER MATTERS
# All of these must be defined ABOVE this block:
#   - filtered_log_callback (PATCH 1)
#   - emit_log, emit_status, emit_file_changed, emit_file_deleted
#   - progress_bus, _fetch_file_tree
#   - db_upsert, db_delete, db_upsert_batch, db_list_file_paths
#   - add_monthly_tokens
# ==========================================================================

lineage_set_log(filtered_log_callback)
_init_sandbox_manager()
 
 
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)