"""
Standalone Design Tool routes — extracted from app.py.

Pure mechanical code move. Shared state (supabase client, DB helpers,
templates, token helpers, etc.) is accessed via `import app` at call time —
app.py imports this router only near the bottom of the file, after all of
those globals/helpers have already been defined, to avoid circular imports.
"""
import os
import json
import asyncio
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, Response

from backend.design.design import (
    generate_design, edit_design,
    design_to_html, get_hosted_html, generate_slug, to_figma_clipboard,
)

import app

router = APIRouter()


@router.get("/design", response_class=HTMLResponse)
async def design_dashboard(request: Request):
    user = app.get_current_user(request)
    used, limit = app.get_token_usage_and_limit(user["id"])
    user["tokens"] = {"used": used, "limit": limit, "remaining": max(0, limit - used)}

    try:
        # OPTIMIZED: Only fetching lightweight list data, skipping massive JSON blobs
        res = app.supabase.table("designs").select("id, name, updated_at, created_at, hosted_slug").eq("owner_id", user["id"]).order("updated_at", desc=True).execute()
        designs = res.data if res and res.data else []
    except Exception:
        designs = []

    return app.templates.TemplateResponse("design/dashboard.html", {
        "request": request,
        "user": user,
        "designs": designs,
    })


# ── CREATE ───────────────────────────────────────────────────────────────

@router.post("/api/design/create")
async def create_design(request: Request):
    user = app.get_current_user(request)
    payload = await request.json()
    name = payload.get("name", "Untitled Design")
    try:
        res = app.supabase.table("designs").insert({
            "owner_id": user["id"],
            "name": name,
            "figma_json": None,
            "tokens": None,
        }).execute()
        return JSONResponse({"id": res.data[0]["id"]})
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ── EDITOR ───────────────────────────────────────────────────────────────

@router.get("/design/editor/{design_id}", response_class=HTMLResponse)
async def design_editor(request: Request, design_id: str):
    user = app.get_current_user(request)
    try:
        # OPTIMIZED: Explicit column selection, avoiding hosted_html if not strictly needed here
        res = app.supabase.table("designs").select("id, name, figma_json, tokens, chat_history, updated_at").eq("id", design_id).eq("owner_id", user["id"]).single().execute()
        design = res.data
    except Exception:
        raise HTTPException(404, "Design not found")

    user_data = app.db_select_one("users", {"id": user["id"]}, "gorilla_api_key") or {}
    return app.templates.TemplateResponse("design/editor.html", {
        "request": request,
        "user": user,
        "design": design,
        "gorilla_api_key": user_data.get("gorilla_api_key", ""),
    })


# ── VIEWER ───────────────────────────────────────────────────────────────

@router.get("/design/viewer/{design_id}", response_class=HTMLResponse)
async def design_viewer(request: Request, design_id: str):
    user = app.get_current_user_safe(request)
    try:
        # OPTIMIZED: Only grab the fields needed to view
        res = app.supabase.table("designs").select("id, name, figma_json, hosted_html").eq("id", design_id).single().execute()
        design = res.data
    except Exception:
        raise HTTPException(404, "Design not found")
    return app.templates.TemplateResponse("design/viewer.html", {
        "request": request,
        "user": user,
        "design": design,
    })


# ── HOSTED PAGE (public, no auth) ────────────────────────────────────────

@router.get("/design/hosted/{slug}", response_class=HTMLResponse)
async def design_hosted(slug: str):
    try:
        # Already optimized: Only selects hosted_html
        res = app.supabase.table("designs").select("hosted_html").eq("hosted_slug", slug).single().execute()
        design = res.data
    except Exception:
        raise HTTPException(404, "Page not found")
    if not design or not design.get("hosted_html"):
        raise HTTPException(404, "Page not published yet")
    return HTMLResponse(content=design["hosted_html"])


# ── SAVE ─────────────────────────────────────────────────────────────────

@router.post("/api/design/{design_id}/save")
async def save_design(request: Request, design_id: str):
    user = app.get_current_user(request)
    payload = await request.json()
    try:
        app.supabase.table("designs").update({
            "figma_json": payload.get("figma_json"),
            "tokens": payload.get("tokens"),
            "updated_at": "now()",
        }).eq("id", design_id).eq("owner_id", user["id"]).execute()
        return JSONResponse({"ok": True})
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ── GENERATE — the main design creation endpoint ──────────────────────────

@router.post("/api/design/{design_id}/generate")
async def generate_design_endpoint(request: Request, design_id: str, background_tasks: BackgroundTasks):
    """
    Generate a complete design from a brief.
    Returns immediately with status, polls /api/design/{id}/status for result.
    Avoids ngrok 30s timeout.
    """
    user = app.get_current_user(request)
    try:
        await asyncio.to_thread(app.enforce_token_limit_or_raise, user["id"])
    except HTTPException as e:
        if e.status_code == 402:
            raise HTTPException(402, "Token limit reached")
        raise

    payload = await request.json()
    brief = payload.get("brief", "").strip()
    if not brief:
        raise HTTPException(400, "Brief required")

    # Mark as generating
    app.supabase.table("designs").update({
        "chat_history": [{"role": "user", "content": brief}],
        "name": brief[:40].split(".")[0].strip().title() or "Generating...",
        "updated_at": "now()",
    }).eq("id", design_id).eq("owner_id", user["id"]).execute()

    async def _generate_bg(design_id: str, brief: str, user_id: str):
        import asyncio
        def _save(data: dict):
            app.supabase.table("designs").update(data).eq("id", design_id).eq("owner_id", user_id).execute()
        def _charge():
            app.add_monthly_tokens(user_id, 3000)
        try:
            result = await generate_design(brief)
            figma_json = result["figma_json"]
            tokens = result["tokens"]
            html = result["html"]
            name = result["name"] or brief[:40].strip().title() or "Untitled"
            figma_json["_raw_html"] = html
            # Run sync Supabase call in thread to avoid blocking event loop
            await asyncio.to_thread(_save, {
                "figma_json": figma_json,
                "tokens": tokens,
                "name": name,
                "hosted_html": html,
                "chat_history": [
                    {"role": "user", "content": brief},
                    {"role": "assistant", "content": f'Generated "{name}"'},
                ],
                "updated_at": "now()",
            })
            await asyncio.to_thread(_charge)
        except Exception as e:
            import traceback; traceback.print_exc()
            try:
                await asyncio.to_thread(_save, {
                    "chat_history": [
                        {"role": "user", "content": brief},
                        {"role": "assistant", "content": f"Error: {str(e)[:200]}"},
                    ],
                    "updated_at": "now()",
                })
            except Exception:
                pass

    import asyncio
    asyncio.create_task(_generate_bg(design_id, brief, user["id"]))
    return JSONResponse({"status": "generating", "design_id": design_id})


@router.get("/api/design/{design_id}/status")
async def design_status(request: Request, design_id: str):
    """Poll this after /generate to get the result."""
    user = app.get_current_user(request)
    try:
        res = app.supabase.table("designs").select("figma_json, tokens, name, chat_history").eq("id", design_id).eq("owner_id", user["id"]).single().execute()
        d = res.data
    except Exception:
        raise HTTPException(404, "Design not found")

    if d.get("figma_json"):
        return JSONResponse({"status": "done", "figma_json": d["figma_json"], "tokens": d.get("tokens", {}), "name": d.get("name", "")})

    # Check if error in chat history
    history = d.get("chat_history") or []
    last = history[-1] if history else {}
    if last.get("content", "").startswith("Error:"):
        return JSONResponse({"status": "error", "detail": last["content"]})

    return JSONResponse({"status": "generating"})


# Keep old endpoint name working too
@router.post("/api/design/{design_id}/setup-tokens")
async def setup_tokens_compat(request: Request, design_id: str):
    """Compat shim — routes old setup-tokens calls to generate."""
    payload = await request.json()
    brand = payload.get("brand", payload.get("brief", ""))
    request._body = json.dumps({"brief": brand}).encode()
    return await generate_design_endpoint(request, design_id)


# ── EDIT — surgical search/replace on existing design ────────────────────

@router.post("/api/design/{design_id}/edit")
async def edit_design_endpoint(request: Request, design_id: str):
    """
    Edit an existing design via surgical ops.
    No full rewrites — just targeted changes.
    """
    user = app.get_current_user(request)
    try:
        await asyncio.to_thread(app.enforce_token_limit_or_raise, user["id"])
    except HTTPException as e:
        if e.status_code == 402:
            raise HTTPException(402, "Token limit reached")
        raise

    payload = await request.json()
    instruction = payload.get("instruction", "").strip()
    if not instruction:
        raise HTTPException(400, "Instruction required")

    try:
        # OPTIMIZED: Fetches everything needed in ONE database call instead of two
        res = app.supabase.table("designs").select("figma_json, tokens, chat_history").eq("id", design_id).eq("owner_id", user["id"]).single().execute()
        design = res.data
    except Exception:
        raise HTTPException(404, "Design not found")

    tree = design.get("figma_json")
    if not tree:
        raise HTTPException(400, "No design to edit yet. Generate one first.")

    try:
        existing_html = tree.get("_raw_html", "")
        result = await edit_design(existing_html or "", instruction)

        updated_tree = result["figma_json"]
        tokens = result["tokens"]
        html = result["html"]
        narration = result["narration"]

        # Store raw HTML back
        updated_tree["_raw_html"] = html

        # OPTIMIZED: Use the history we already fetched above
        history = design.get("chat_history") or []
        history.append({"role": "user", "content": instruction})
        history.append({"role": "assistant", "content": narration})
        if len(history) > 100:
            history = history[-100:]

        app.supabase.table("designs").update({
            "figma_json": updated_tree,
            "tokens": tokens,
            "hosted_html": html,
            "chat_history": history,
            "updated_at": "now()",
        }).eq("id", design_id).eq("owner_id", user["id"]).execute()

        app.add_monthly_tokens(user["id"], 500)

        return JSONResponse({
            "figma_json": updated_tree,
            "tokens": tokens,
            "narration": narration,
            "html": html,
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(500, detail=str(e))


# Keep old agent endpoint working too
@router.post("/api/design/{design_id}/agent")
async def agent_compat(request: Request, design_id: str):
    """Compat shim — routes old agent calls to edit."""
    payload = await request.json()
    instruction = payload.get("instruction", payload.get("message", ""))
    request._body = json.dumps({"instruction": instruction}).encode()
    return await edit_design_endpoint(request, design_id)


# ── IMAGE GENERATION ─────────────────────────────────────────────────────

@router.post("/api/design/{design_id}/image")
async def generate_image_endpoint(request: Request, design_id: str):
    """Generate an image and embed it as a fill on a specific node."""
    user = app.get_current_user(request)
    payload = await request.json()
    node_id = payload.get("node_id", "")
    prompt = payload.get("prompt", "")
    if not node_id or not prompt:
        raise HTTPException(400, "node_id and prompt required")

    try:
        res = app.supabase.table("designs").select("figma_json").eq("id", design_id).eq("owner_id", user["id"]).single().execute()
        tree = res.data.get("figma_json") or {}
    except Exception:
        raise HTTPException(404, "Design not found")

    user_data = app.db_select_one("users", {"id": user["id"]}, "gorilla_api_key") or {}
    api_key = user_data.get("gorilla_api_key", "")

    updated_tree, success = await generate_image_fill(node_id, prompt, tree, api_key)

    if success:
        app.supabase.table("designs").update({
            "figma_json": updated_tree,
            "updated_at": "now()",
        }).eq("id", design_id).eq("owner_id", user["id"]).execute()
        app.add_monthly_tokens(user["id"], 8000)

    return JSONResponse({"ok": success, "figma_json": updated_tree if success else tree})


# ── EXPORT: FIGMA JSON download ───────────────────────────────────────────

@router.post("/api/design/{design_id}/export/figma")
async def export_figma(request: Request, design_id: str):
    user = app.get_current_user(request)
    try:
        res = app.supabase.table("designs").select("figma_json, name").eq("id", design_id).eq("owner_id", user["id"]).single().execute()
        design = res.data
    except Exception:
        raise HTTPException(404, "Design not found")
    return JSONResponse({"payload": design.get("figma_json"), "name": design.get("name")})


# ── EXPORT: HTML download ─────────────────────────────────────────────────

@router.post("/api/design/{design_id}/export/html")
async def export_html(request: Request, design_id: str):
    user = app.get_current_user(request)
    try:
        res = app.supabase.table("designs").select("figma_json").eq("id", design_id).eq("owner_id", user["id"]).single().execute()
        tree = res.data.get("figma_json") or {}
    except Exception:
        raise HTTPException(404, "Design not found")
    html = design_to_html(tree)
    return Response(content=html, media_type="text/html")


# ── EXPORT: HOST as public page ───────────────────────────────────────────

@router.post("/api/design/{design_id}/export/host")
async def export_host(request: Request, design_id: str):
    user = app.get_current_user(request)
    try:
        # OPTIMIZED: Stop pulling chat history and tokens here
        res = app.supabase.table("designs").select("figma_json, name, hosted_slug, hosted_html").eq("id", design_id).eq("owner_id", user["id"]).single().execute()
        design = res.data
    except Exception:
        raise HTTPException(404, "Design not found")

    tree = design.get("figma_json") or {}
    name = design.get("name", "design")
    slug = design.get("hosted_slug") or generate_slug(name)
    site_url = os.getenv("SITE_URL", "")

    # Use stored HTML if available (new HTML-first format)
    raw_html = tree.get("_raw_html") or design.get("hosted_html") or ""
    if raw_html:
        html = get_hosted_html(raw_html, design_id=design_id, site_url=site_url)
    else:
        html = get_hosted_html(design_to_html(tree), design_id=design_id, site_url=site_url)

    app.supabase.table("designs").update({
        "hosted_slug": slug,
        "hosted_html": html,
        "updated_at": "now()",
    }).eq("id", design_id).execute()

    return JSONResponse({"url": f"{site_url}/design/hosted/{slug}", "slug": slug})


# ── EXPORT: Figma clipboard (paste directly into Figma) ───────────────────

@router.post("/api/design/{design_id}/export/figma-clipboard")
async def export_figma_clipboard(request: Request, design_id: str):
    user = app.get_current_user(request)
    try:
        res = app.supabase.table("designs").select("figma_json, name").eq("id", design_id).eq("owner_id", user["id"]).single().execute()
        design = res.data
    except Exception:
        raise HTTPException(404, "Design not found")
    tree = design.get("figma_json") or {}
    # Returns the HTML string that Figma reads from clipboard
    clipboard_html = to_figma_clipboard(tree)
    return Response(content=clipboard_html, media_type="text/plain")

# ── DELETE ────────────────────────────────────────────────────────────────

@router.post("/api/design/{design_id}/delete")
async def delete_design(request: Request, design_id: str):
    user = app.get_current_user(request)
    try:
        app.supabase.table("designs").delete().eq("id", design_id).eq("owner_id", user["id"]).execute()
        return JSONResponse({"ok": True})
    except Exception as e:
        raise HTTPException(500, detail=str(e))
