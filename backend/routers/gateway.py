"""
AI / Auth Gateway API routes (/api/v1/*) — extracted from app.py.

Pure mechanical code move. Shared state (supabase client, DB helpers,
templates, token helpers, etc.) is accessed via `import app` at call time —
app.py imports this router only near the bottom of the file, after all of
those globals/helpers have already been defined, to avoid circular imports.
"""
import os
import json
import math
import urllib.parse
import httpx
from fastapi import APIRouter, Request, HTTPException, Depends, File, UploadFile, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

import app

router = APIRouter()

# --- OAuth Environment Variables (App Auth Gateway) ---
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GITHUB_APPAUTH_CLIENT_ID = os.getenv("GITHUB_APPAUTH_CLIENT_ID")
GITHUB_APPAUTH_CLIENT_SECRET = os.getenv("GITHUB_APPAUTH_CLIENT_SECRET")

# ==========================================================================
# APP AUTH GATEWAY (For Generated Apps)
# ==========================================================================

@router.get("/api/v1/app-auth/login", response_class=HTMLResponse)
async def app_auth_login_page(request: Request, auth_id: str, return_url: str = ""):
    """Renders the Hosted Login Page for the generated app."""
    proj = app.db_select_one("projects", {"gorilla_auth_id": auth_id}, "name")
    if not proj:
        return HTMLResponse("<h1>Invalid App Auth ID</h1>", status_code=404)

    request.session["app_auth_pending"] = {"auth_id": auth_id, "return_url": return_url}

    return app.templates.TemplateResponse("auth/appauth.html", {
        "request": request,
        "project_name": proj.get("name", "this app"),
        "auth_id": auth_id,
        "step": "login"
    })

@router.get("/api/v1/app-auth/{auth_id}/google")
async def app_auth_google_init(request: Request, auth_id: str):
    scope = "openid email profile"
    site_url = os.getenv('SITE_URL')
    redirect_uri = urllib.parse.quote(f"{site_url}/api/v1/app-auth/google/callback")
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?response_type=code&client_id={GOOGLE_CLIENT_ID}&redirect_uri={redirect_uri}&scope={scope}&state={auth_id}"
    return RedirectResponse(auth_url)

@router.get("/api/v1/app-auth/{auth_id}/github")
async def app_auth_github_init(request: Request, auth_id: str):
    scope = "user:email"
    site_url = os.getenv('SITE_URL')
    redirect_uri = urllib.parse.quote(f"{site_url}/api/v1/app-auth/github/callback")
    auth_url = f"https://github.com/login/oauth/authorize?client_id={GITHUB_APPAUTH_CLIENT_ID}&redirect_uri={redirect_uri}&scope={scope}&state={auth_id}"
    return RedirectResponse(auth_url)

@router.get("/api/v1/app-auth/google/callback")
async def app_auth_google_callback(request: Request, code: str, state: str):
    site_url = os.getenv('SITE_URL')
    async with httpx.AsyncClient() as client:
        res = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": f"{site_url}/api/v1/app-auth/google/callback",
            "grant_type": "authorization_code",
        })
        tokens = res.json()
        access_token = tokens.get("access_token")

        user_res = await client.get(
            "https://www.googleapis.com/oauth2/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        google_user = user_res.json()

    user_payload = {
        "id": google_user.get("id"),
        "email": google_user.get("email"),
        "name": google_user.get("name"),
        "avatar": google_user.get("picture"),
        "provider": "google"
    }

# Track app login via analytics
    try:
        proj = app.db_select_one("projects", {"gorilla_auth_id": state}, "id")
        if proj:
            app.track_event(
                proj["id"], "login",
                user_email=user_payload.get("email"),
                user_provider="google",
                metadata={"name": user_payload.get("name")},
            )
    except Exception:
        pass

    return app.templates.TemplateResponse("auth/appauth.html", {
        "request": request,
        "step": "success",
        "user_data": json.dumps(user_payload)
    })

@router.get("/api/v1/app-auth/github/callback")
async def app_auth_github_callback(request: Request, code: str, state: str):
    site_url = os.getenv('SITE_URL')
    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": GITHUB_APPAUTH_CLIENT_ID,
                "client_secret": GITHUB_APPAUTH_CLIENT_SECRET,
                "code": code,
                "redirect_uri": f"{site_url}/api/v1/app-auth/github/callback"
            },
            headers={"Accept": "application/json"}
        )
        tokens = res.json()
        access_token = tokens.get("access_token")

        user_res = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        github_user = user_res.json()

    user_payload = {
        "id": str(github_user.get("id")),
        "email": github_user.get("email"),
        "name": github_user.get("name") or github_user.get("login"),
        "avatar": github_user.get("avatar_url"),
        "provider": "github"
    }

# Track app login via analytics
    try:
        proj = app.db_select_one("projects", {"gorilla_auth_id": state}, "id")
        if proj:
            app.track_event(
                proj["id"], "login",
                user_email=user_payload.get("email"),
                user_provider="github",
                metadata={"name": user_payload.get("name")},
            )
    except Exception:
        pass

    return app.templates.TemplateResponse("auth/appauth.html", {
        "request": request,
        "step": "success",
        "user_data": json.dumps(user_payload)
    })


# ==========================================================================
# THE GORILLA AI PROXY GATEWAY
# ==========================================================================

# Add these missing OpenRouter variables!
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_SITE_URL = "https://gorillabuilder.dev"
SITE_NAME = os.getenv("SITE_NAME", "Gorilla Builder")

security = HTTPBearer()

# --- Proxy Environment Variables ---
FIREWORKS_API_KEY = os.getenv("FIREWORKS_API_KEY")
REMBG_API_KEY = os.getenv("REMBG_API_KEY", "") # If your RemBG has a key
REMBG_API_URL = os.getenv("REMBG_API_URL", "http://localhost:5000/api/remove")

def _deduct_proxy_tokens(user_id: str, cost: float, feature: str):
    """Helper to safely deduct tokens for API Gateway usage."""
    if cost <= 0: return
    try:
        tokens_to_add = math.ceil(cost) # Round up fractional tokens

        # 1. Fetch current tokens_used from the users table
        res = app.supabase.table("users").select("tokens_used").eq("id", user_id).single().execute()
        current_used = res.data.get("tokens_used", 0) if res.data and res.data.get("tokens_used") else 0

        # 2. Add the cost and update the database
        new_total = current_used + tokens_to_add
        app.supabase.table("users").update({"tokens_used": new_total}).eq("id", user_id).execute()

        print(f"💰 Deducted {tokens_to_add} tokens for {feature} (User: {user_id})")
    except Exception as e:
        print(f"⚠️ Failed to deduct {cost} tokens for {user_id}: {e}")

async def verify_gorilla_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    api_key = credentials.credentials
    print(f"🔑 key received: {api_key[:20]}...")

    if not api_key.startswith("gb_live_"):
        print(f"🔑 REJECTED: bad format")
        raise HTTPException(status_code=401, detail="Invalid API Key format. Must start with 'gb_live_'")

    res = app.supabase.table("users").select("id, plan").eq("gorilla_api_key", api_key).single().execute()
    print(f"🔑 DB lookup: {res.data}")

    if not res or not res.data:
        print(f"🔑 REJECTED: key not found in DB")
        raise HTTPException(status_code=401, detail="Invalid API Key. Unauthorized.")

    user = res.data
    used, limit = app.get_token_usage_and_limit(user["id"])
    print(f"🔑 tokens: used={used} limit={limit}")

    if used >= limit:
        raise HTTPException(status_code=402, detail="Token limit reached.")

    print(f"🔑 APPROVED: user={user['id']}")
    return {"user_id": user["id"], "plan": user.get("plan")}

# --- 1. LLM CHAT (OpenRouter / 0.5 tokens per 1 API token) ---
@router.post("/api/v1/chat/completions")
async def proxy_chat_completions(request: Request, auth=Depends(verify_gorilla_key)):
    user_id = auth["user_id"]
    payload = await request.json()

    # Force the model to OpenRouter's massive 120b model as requested
    payload["model"] = "qwen/qwen3.5-flash-02-23" # Replace with your exact OpenRouter model string

    # Ask OpenRouter to send usage stats back even if it's a stream
    if "stream_options" not in payload:
        payload["stream_options"] = {"include_usage": True}

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_SITE_URL,
        "X-Title": SITE_NAME
    }

    # Handle Streaming Responses
    is_stream = payload.get("stream", False)

    if is_stream:
        async def stream_generator():
            total_tokens = 0
            async with httpx.AsyncClient() as client:
                async with client.stream("POST", OPENROUTER_URL, json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        yield f"data: {json.dumps({'error': 'Upstream provider error'})}\n\n"
                        return

                    async for chunk in resp.aiter_text():
                        yield chunk
                        # OpenRouter includes {"usage": {"total_tokens": X}} in the final SSE chunk
                        if '"usage":' in chunk and '"total_tokens":' in chunk:
                            try:
                                # Quick and dirty parse of the usage chunk
                                parts = chunk.split('"total_tokens":')
                                if len(parts) > 1:
                                    token_val = parts[1].split(',')[0].split('}')[0].strip()
                                    total_tokens = int(token_val)
                            except: pass

            # Bill the user after the stream closes (0.5 tokens per 1 API token)
            if total_tokens > 0:
                _deduct_proxy_tokens(user_id, total_tokens * 0.3, "chat_stream")

        return StreamingResponse(stream_generator(), media_type="text/event-stream")

    # Handle Standard Non-Streaming Responses
    else:
        async with httpx.AsyncClient() as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)

            data = resp.json()
            total_tokens = data.get("usage", {}).get("total_tokens", 0)

            # Bill the user (0.5 tokens per 1 API token)
            _deduct_proxy_tokens(user_id, total_tokens * 0.3, "chat")

            return JSONResponse(data)


@router.post("/api/v1/images/generations")
async def proxy_image_generations(request: Request, auth=Depends(verify_gorilla_key)):
    user_id = auth["user_id"]
    print(f"🖼️ IMAGE GEN START — user_id={user_id}")

    payload = await request.json()
    prompt = payload.get("prompt", "")
    print(f"🖼️ prompt={prompt[:100]}")
    print(f"🖼️ OPENROUTER_API_KEY={'SET len='+str(len(OPENROUTER_API_KEY)) if OPENROUTER_API_KEY else 'EMPTY/NONE'}")

    openrouter_payload = {
        "model": "black-forest-labs/flux.2-klein-4b",
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image"],
        "stream": False,
    }
    print(f"🖼️ sending payload={openrouter_payload}")

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_SITE_URL,
        "X-Title": SITE_NAME,
    }

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=openrouter_payload,
            headers=headers,
        )

    print(f"🖼️ OpenRouter status={resp.status_code}")
    print(f"🖼️ OpenRouter body={resp.text[:500]}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Image gen error {resp.status_code}: {resp.text[:200]}")

    result = resp.json()
    images = []
    choices = result.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        print(f"🖼️ message keys={list(msg.keys())}")
        for img in msg.get("images", []):
            url = img.get("image_url", {}).get("url") or img.get("url", "")
            if url:
                images.append({"url": url})
        if not images and msg.get("content", "").startswith("data:image"):
            images.append({"url": msg["content"]})

    print(f"🖼️ returning {len(images)} images")
    _deduct_proxy_tokens(user_id, 8000, "image_gen")
    return JSONResponse({"data": images})

@router.post("/api/v1/audio/transcriptions")
async def proxy_audio_transcriptions(
    file: UploadFile = File(...),
    model: str = Form("accounts/fireworks/models/whisper-v3-turbo"),
    auth=Depends(verify_gorilla_key)
):
    user_id = auth["user_id"]
    file_bytes = await file.read()

    # Heuristic Duration Calculation:
    # A standard mp3/m4a voice memo is roughly 1MB per minute.
    # We use file size to estimate minutes (minimum 1 minute)
    file_size_mb = len(file_bytes) / (1024 * 1024)
    estimated_minutes = max(1, math.ceil(file_size_mb))
    cost = estimated_minutes * 100

    headers = {"Authorization": f"Bearer {FIREWORKS_API_KEY}"}
    files_payload = {"file": (file.filename, file_bytes, file.content_type)}
    data_payload = {"model": "accounts/fireworks/models/whisper-v3-turbo"}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.fireworks.ai/inference/v1/audio/transcriptions",
            files=files_payload,
            data=data_payload,
            headers=headers
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)

        # Bill 100 tokens per estimated minute
        _deduct_proxy_tokens(user_id, cost, "stt_whisper")

        return JSONResponse(resp.json())


# --- 4. BACKGROUND REMOVAL (RemBG / 0 tokens / Free Forever) ---
@router.post("/api/v1/images/remove-background")
async def proxy_remove_background(file: UploadFile = File(...), auth=Depends(verify_gorilla_key)):
    # Verify key, but we don't bill them for this!
    file_bytes = await file.read()

    headers = {}
    if REMBG_API_KEY:
        headers["x-api-key"] = REMBG_API_KEY

    files_payload = {"file": (file.filename, file_bytes, file.content_type)}

    async with httpx.AsyncClient() as client:
        resp = await client.post(REMBG_API_URL, files=files_payload, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=f"RemBG Error: {resp.text}")

        # Returns the raw PNG image file directly to the frontend
        return Response(content=resp.content, media_type="image/png")

@router.post("/api/v1/chat/completions/bargain")
async def proxy_chat_completions_bargain(request: Request, auth=Depends(verify_gorilla_key)):
    user_id = auth["user_id"]
    payload = await request.json()

    # Force the model to OpenRouter's massive 120b model as requested
    payload["model"] = "deepseek/deepseek-v4-flash:nitro" # Replace with your exact OpenRouter model string

    # Ask OpenRouter to send usage stats back even if it's a stream
    if "stream_options" not in payload:
        payload["stream_options"] = {"include_usage": True}

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_SITE_URL,
        "X-Title": SITE_NAME
    }

    # Handle Streaming Responses
    is_stream = payload.get("stream", False)

    if is_stream:
        async def stream_generator():
            total_tokens = 0
            async with httpx.AsyncClient() as client:
                async with client.stream("POST", OPENROUTER_URL, json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        yield f"data: {json.dumps({'error': 'Upstream provider error'})}\n\n"
                        return

                    async for chunk in resp.aiter_text():
                        yield chunk
                        # OpenRouter includes {"usage": {"total_tokens": X}} in the final SSE chunk
                        if '"usage":' in chunk and '"total_tokens":' in chunk:
                            try:
                                # Quick and dirty parse of the usage chunk
                                parts = chunk.split('"total_tokens":')
                                if len(parts) > 1:
                                    token_val = parts[1].split(',')[0].split('}')[0].strip()
                                    total_tokens = int(token_val)
                            except: pass

            # Bill the user after the stream closes (0.5 tokens per 1 API token)
            if total_tokens > 0:
                _deduct_proxy_tokens(user_id, total_tokens * 0.3, "chat_stream")

        return StreamingResponse(stream_generator(), media_type="text/event-stream")

    # Handle Standard Non-Streaming Responses
    else:
        async with httpx.AsyncClient() as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)

            data = resp.json()
            total_tokens = data.get("usage", {}).get("total_tokens", 0)

            # Bill the user (0.5 tokens per 1 API token)
            _deduct_proxy_tokens(user_id, total_tokens * 0.3, "chat")

            return JSONResponse(data)
