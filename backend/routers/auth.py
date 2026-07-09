"""
Auth / Billing / Settings routes — extracted from app.py.

This is a pure mechanical code move. All shared state (supabase client, DB
helpers, templates, token helpers, user helpers, etc.) is imported from
`app` at call time via module-level `import app` so we avoid circular
imports: app.py imports this router only near the bottom of the file,
after all of those globals/helpers have already been defined.
"""
import os
import random
import string
import time
import secrets
import urllib.parse
import httpx
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Form, BackgroundTasks, HTTPException, Response
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse

import app

router = APIRouter()

# --- OAuth Environment Variables ---
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
GITHUB_APPAUTH_CLIENT_ID = os.getenv("GITHUB_APPAUTH_CLIENT_ID")
GITHUB_APPAUTH_CLIENT_SECRET = os.getenv("GITHUB_APPAUTH_CLIENT_SECRET")
GITHUB_REDIRECT_URI = os.getenv("GITHUB_REDIRECT_URI")

FIGMA_CLIENT_ID = os.getenv("FIGMA_CLIENT_ID")
FIGMA_CLIENT_SECRET = os.getenv("FIGMA_CLIENT_SECRET")
# e.g., https://app.gorillabuilder.dev/auth/figma/callback (must match Figma exact)
FIGMA_REDIRECT_URI = os.getenv("FIGMA_REDIRECT_URI")

SUPABASE_MGMT_CLIENT_ID = os.getenv("SUPABASE_MGMT_CLIENT_ID")
SUPABASE_MGMT_CLIENT_SECRET = os.getenv("SUPABASE_MGMT_CLIENT_SECRET")
SUPABASE_MGMT_REDIRECT_URI = os.getenv("SUPABASE_MGMT_REDIRECT_URI")


@router.get("/auth/figma")
async def figma_login(request: Request):
    """Initiates the Figma OAuth flow."""
    user = app.get_current_user(request)

    state = secrets.token_urlsafe(16)
    request.session["figma_oauth_state"] = state

    # 🛑 THE FIX: Changed scope=file_read to scope=file_content:read
    url = f"https://www.figma.com/oauth?client_id={FIGMA_CLIENT_ID}&redirect_uri={urllib.parse.quote(FIGMA_REDIRECT_URI)}&scope=file_content:read&state={state}&response_type=code"

    return RedirectResponse(url)

@router.get("/auth/figma/callback")
async def figma_callback(request: Request, code: str, state: str):
    """Catches the code from Figma, trades it for a token, and saves it to DB."""
    try:
        user = app.get_current_user(request)

        # Verify the state matches what we sent (CSRF protection)
        saved_state = request.session.pop("figma_oauth_state", None)
        if not saved_state or state != saved_state:
            return RedirectResponse("/dashboard?error=figma_invalid_state", status_code=303)

        # 🛑 THE FIX: Changed from www.figma.com to api.figma.com/v1/
        async with httpx.AsyncClient() as client:
            res = await client.post("https://api.figma.com/v1/oauth/token", data={
                "client_id": FIGMA_CLIENT_ID,
                "client_secret": FIGMA_CLIENT_SECRET,
                "redirect_uri": FIGMA_REDIRECT_URI,
                "code": code,
                "grant_type": "authorization_code"
            })

            if res.status_code != 200:
                print(f"⚠️ Figma OAuth Error: {res.text}")
                return RedirectResponse("/dashboard?error=figma_token_exchange_failed", status_code=303)

            tokens = res.json()
            access_token = tokens.get("access_token")
            refresh_token = tokens.get("refresh_token")

            if access_token:
                # Save the tokens to the user's record in Supabase
                app.supabase.table("users").update({
                    "figma_access_token": access_token,
                    "figma_refresh_token": refresh_token
                }).eq("id", user["id"]).execute()

        return RedirectResponse("/dashboard?success=figma_linked", status_code=303)

    except Exception as e:
        print(f"⚠️ Figma Auth Callback crashed: {e}")
        return RedirectResponse("/dashboard?error=figma_auth_crash", status_code=303)


def _ensure_gorilla_api_key(user_id: str):
    """
    PHASE 1: AI PROXY GATEWAY
    Checks if a user has a gb_live_ API key. If not, generates and saves one.
    """
    try:
        user_data = app.db_select_one("users", {"id": user_id}, "gorilla_api_key")
        if not user_data or not user_data.get("gorilla_api_key"):
            # Generate a secure 48-character hex string (total key length ~56 chars)
            new_key = f"gb_live_{secrets.token_hex(24)}"
            app.supabase.table("users").update({"gorilla_api_key": new_key}).eq("id", user_id).execute()
            print(f"🔑 Generated new Gorilla API Key for user: {user_id}")
    except Exception as e:
        print(f"⚠️ Failed to generate gorilla_api_key for {user_id}: {e}")


# --------------------------------------------------------------------------
# 1. SIGNUP FLOW (Secure)
# --------------------------------------------------------------------------

@router.post("/auth/signup")
async def auth_signup_init(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    password: str = Form(...)
):
    email = (email or "").strip().lower()

    # [SECURITY] Check if user already exists
    try:
        # Admin check is most reliable. If not available, use a safe alternative.
        existing_users = app.supabase.auth.admin.list_users()
        user_exists = any(u.email == email for u in existing_users)

        if user_exists:
            # REDIRECT TO LOGIN if account exists
            return app.templates.TemplateResponse(
                "auth/login.html",
                {
                    "request": request,
                    "error": "Account exists. Please log in here.",
                    "email_prefill": email # Optional: pass back to template if supported
                }
            )
    except Exception as e:
        print(f"⚠️ User existence check warning: {e}")
        pass # Fail open or closed depending on policy, passing allows flow to continue

    # Proceed with OTP generation
    otp = "".join(random.choices(string.digits, k=6))

    app.PENDING_SIGNUPS[email] = {
        "password": password,
        "otp": otp,
        "ts": time.time()
    }

    # --- FIX: SEND ACTUAL EMAIL VIA BACKGROUND TASK ---
    background_tasks.add_task(app.send_otp_email, email, otp)

    return app.templates.TemplateResponse(
        "auth/signup.html",
        {
            "request": request,
            "step": "verify",
            "email": email
        }
    )

@router.post("/auth/verify")
async def auth_verify_otp(
    request: Request,
    email: str = Form(...),
    code: str = Form(...)
):
    email  = email.strip().lower()
    record = app.PENDING_SIGNUPS.get(email)

    if not record:
        return app.templates.TemplateResponse("auth/signup.html", {
            "request": request, "step": "initial",
            "error": "Session expired. Please start over."
        })

    # ✅ Expire after 10 minutes
    if time.time() - record.get("ts", 0) > 600:
        del app.PENDING_SIGNUPS[email]
        return app.templates.TemplateResponse("auth/signup.html", {
            "request": request, "step": "initial",
            "error": "Code expired. Please start over."
        })

    # ✅ Max 5 attempts
    record["attempts"] = record.get("attempts", 0) + 1
    if record["attempts"] > 5:
        del app.PENDING_SIGNUPS[email]
        return app.templates.TemplateResponse("auth/signup.html", {
            "request": request, "step": "initial",
            "error": "Too many attempts. Please start over."
        })

    if record["otp"] != code:
        remaining = max(0, 5 - record["attempts"])
        return app.templates.TemplateResponse("auth/signup.html", {
            "request": request, "step": "verify", "email": email,
            "error": f"Invalid code. {remaining} attempt{'s' if remaining != 1 else ''} remaining."
        })

    try:
        password = record["password"]

        try:
            app.supabase.auth.admin.create_user({
                "email": email,
                "password": password,
                "email_confirm": True
            })
        except Exception:
            return app.templates.TemplateResponse("auth/login.html", {
                "request": request,
                "error": "Account exists. Please log in."
            })

        res = app.supabase.auth.sign_in_with_password({"email": email, "password": password})
        if not res.session:
            raise Exception("Account created, but auto-login failed.")

        app.ensure_public_user(res.user.id, email)
        _ensure_gorilla_api_key(res.user.id)

        if email in app.PENDING_SIGNUPS:
            del app.PENDING_SIGNUPS[email]

        request.session["user"] = {"id": res.user.id, "email": email}
        response = RedirectResponse("/dashboard", status_code=303)
        response.set_cookie(
            key="sb_access_token",
            value=res.session.access_token,
            max_age=86400, httponly=True, samesite="lax"
        )
        return response

    except Exception as e:
        print(f"Verify Error: {e}")
        return app.templates.TemplateResponse("auth/signup.html", {
            "request": request, "step": "verify", "email": email,
            "error": "System error. Try again."
        })

# --------------------------------------------------------------------------
# 2. LOGIN FLOW (Smart Redirect)
# --------------------------------------------------------------------------

@router.post("/auth/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        # 1. Attempt Real Authentication against Supabase
        res = app.supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })

        # If we get here, the password IS correct.
        if not res.session:
            raise Exception("Auth failed (No session)")

        # FIX: Force session set to avoid dev@local fallback
        request.session["user"] = {"id": res.user.id, "email": email}
        # FIX: Ensure user is synced
        app.ensure_public_user(res.user.id, email)

        # 🚨 AI PROXY: Ensure they have a Master Key
        _ensure_gorilla_api_key(res.user.id)

        # 2. Success: Set Cookie & Redirect
        response = RedirectResponse("/dashboard", status_code=303)
        response.set_cookie(
            key="sb_access_token",
            value=res.session.access_token,
            max_age=86400,
            httponly=True,
            samesite="lax"
        )
        return response

    except Exception as e:
        print(f"❌ Login Failed for {email}: {e}")

        # 3. Security Analysis: Determine why it failed
        error_msg = "Invalid email or password."

        try:
            # Check if user exists but uses Google/GitHub Auth (Passwordless)
            users = app.supabase.auth.admin.list_users()
            target_user = next((u for u in users if u.email == email), None)

            if target_user:
                identities = getattr(target_user, "identities", [])
                providers = [i.provider for i in identities]

                # If they only have OAuth and no password set
                if "google" in providers and "email" not in providers:
                    error_msg = "This account uses Google Login. Please click 'Continue with Google'."
                elif "github" in providers and "email" not in providers:
                    error_msg = "This account uses GitHub Login. Please click 'Continue with GitHub'."
                elif "google" in providers or "github" in providers:
                    error_msg = "Invalid password. Try logging in with your connected OAuth provider."
        except:
            pass # Keep generic error if admin check fails

        # 4. STRICT FAILURE: Return to login page with error
        return app.templates.TemplateResponse("auth/login.html", {
            "request": request,
            "error": error_msg,
            "email_prefill": email
        })

@router.get("/auth/logout")
async def logout(request: Request):
    # Redirect to signup on logout
    response = RedirectResponse("/signup", status_code=303)
    response.delete_cookie("sb_access_token")
    request.session.clear()
    try:
        app.supabase.auth.sign_out()
    except: pass
    return response

# --------------------------------------------------------------------------
# 3. FORGOT PASSWORD
# --------------------------------------------------------------------------

@router.post("/auth/forgot-password")
async def forgot_password_action(request: Request, email: str = Form(...)):
    try:
        app.supabase.auth.reset_password_email(email, options={
            "redirect_to": f"{str(request.base_url).rstrip('/')}/auth/reset-callback"
        })
        return app.templates.TemplateResponse("auth/login.html", {
            "request": request,
            "error": "Reset link sent. Check your email."
        })
    except Exception as e:
        return app.templates.TemplateResponse("auth/forgot_password.html", {"request": request, "error": f"Error: {e}"})

# --------------------------------------------------------------------------
# 4. GOOGLE OAUTH
# --------------------------------------------------------------------------

@router.get("/auth/google")
async def auth_google(request: Request):
    if not GOOGLE_CLIENT_ID or not GOOGLE_REDIRECT_URI:
        raise HTTPException(500, "Google Auth config missing.")
    scope = "openid email profile"
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?response_type=code&client_id={GOOGLE_CLIENT_ID}&redirect_uri={GOOGLE_REDIRECT_URI}&scope={scope}"
    return RedirectResponse(auth_url)

@router.get("/auth/google/callback")
async def auth_google_callback(request: Request, code: str):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(500, "Google Auth config missing.")

    async with httpx.AsyncClient() as client:
        res = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        })

        if res.status_code != 200:
             raise HTTPException(400, "Google Login Failed")

        tokens = res.json()
        access_token = tokens.get("access_token")

        user_res = await client.get(
            "https://www.googleapis.com/oauth2/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        user_data = user_res.json()
        email = user_data.get("email")

        if not email:
            raise HTTPException(400, "No email from Google")

        user_id = app._stable_user_id_for_email(email)
        app.ensure_public_user(user_id, email)

        # 🚨 AI PROXY: Ensure they have a Master Key
        _ensure_gorilla_api_key(user_id)

        request.session["user"] = {"id": user_id, "email": email}

        return RedirectResponse("/dashboard", status_code=303)

# --------------------------------------------------------------------------
# 5. GITHUB OAUTH (Crucial for Vercel Deployment pipeline)
# --------------------------------------------------------------------------

@router.get("/auth/github")
async def auth_github(request: Request):
    if not GITHUB_CLIENT_ID or not GITHUB_REDIRECT_URI:
        raise HTTPException(500, "GitHub Auth config missing.")

    # Scope 'repo' is REQUIRED to push code on the user's behalf
    scope = "user:email repo"
    auth_url = f"https://github.com/login/oauth/authorize?client_id={GITHUB_CLIENT_ID}&redirect_uri={GITHUB_REDIRECT_URI}&scope={scope}"
    return RedirectResponse(auth_url)

@router.get("/auth/github/callback")
async def auth_github_callback(request: Request, code: str):
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise HTTPException(500, "GitHub Auth config missing.")

    async with httpx.AsyncClient() as client:
        # 1. Exchange code for GitHub Access Token
        res = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": GITHUB_REDIRECT_URI
            },
            headers={"Accept": "application/json"}
        )

        if res.status_code != 200:
            raise HTTPException(400, "GitHub Login Failed")

        tokens = res.json()
        access_token = tokens.get("access_token")

        if not access_token:
            error_msg = tokens.get("error_description", "Failed to retrieve GitHub access token")
            raise HTTPException(400, error_msg)

        # 2. Get User Profile Data
        user_res = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github.v3+json"
            }
        )
        user_data = user_res.json()
        github_username = user_data.get("login", "unknown_github_user")

        # (email resolution logic unchanged — steps 3 & 4 from your original)
        email = user_data.get("email")
        if not email:
            emails_res = await client.get(
                "https://api.github.com/user/emails",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github.v3+json"
                }
            )
            if emails_res.status_code == 200:
                emails_data = emails_res.json()
                if isinstance(emails_data, list):
                    for em in emails_data:
                        if isinstance(em, dict) and em.get("primary") and em.get("verified"):
                            email = em.get("email")
                            break
                    if not email:
                        for em in emails_data:
                            if isinstance(em, dict) and em.get("verified"):
                                email = em.get("email")
                                break
                    if not email and len(emails_data) > 0 and isinstance(emails_data[0], dict):
                        email = emails_data[0].get("email")

        if not email:
            email = f"{github_username}@noreply.github.com"

        # ── ACCOUNT LINKING ──────────────────────────────────────────────
        # If the user is already logged in (e.g. via Google), attach the
        # GitHub token to their existing account instead of making a new one.
        existing_session = request.session.get("user")
        if existing_session and existing_session.get("id"):
            user_id = existing_session["id"]
            # Don't overwrite their session email — keep the Google one.
        else:
            # Fresh login via GitHub: resolve by GitHub email as before.
            user_id = app._stable_user_id_for_email(email)
            app.ensure_public_user(user_id, email)
            _ensure_gorilla_api_key(user_id)
            request.session["user"] = {"id": user_id, "email": email}
        # ─────────────────────────────────────────────────────────────────

        # 5. Save the GitHub access token under whichever user_id we resolved
        try:
            app.supabase.table("users").update(
                {"github_access_token": access_token}
            ).eq("id", user_id).execute()
        except Exception as e:
            print(f"⚠️ Failed to save github_access_token for user {user_id}: {e}")

        is_popup = request.session.pop("github_oauth_popup", False)
        if is_popup:
            return HTMLResponse("""<!DOCTYPE html><html><head><title>Connected</title></head>
        <body><script>
        window.opener && window.opener.postMessage({type:'github_linked',ok:true},'*');
        window.close();
        </script><p>GitHub connected. You can close this window.</p></body></html>""")
        return RedirectResponse("/dashboard", status_code=303)

# ==========================================================================
# BILLING ROUTES (Mock Payment Processing)
# ==========================================================================
@router.post("/billing/process-premium")
async def process_premium(request: Request):
    """Simulate upgrading to Premium (5M tokens/mo)."""
    user = app.get_current_user(request)

    # Update Plan to premium and set limit to 5,000,000
    app.set_user_plan_and_limit(user["id"], "premium", 5000000)

    # Redirect to dashboard with success param?
    return RedirectResponse("/dashboard", status_code=303)

@router.post("/billing/process-tokens")
async def process_tokens(request: Request, amount: int = Form(...)):
    """Simulate buying one-time token top-up."""
    user = app.get_current_user(request)

    # "Top up" logic: We decrease 'tokens_used' by the purchased amount
    app.decrease_tokens_used(user["id"], amount)

    return RedirectResponse("/dashboard", status_code=303)

@router.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request):
    user = app.get_current_user_safe(request)
    db_user = None
    if user:
        db_user = app.db_select_one("users", {"id": user["id"]}, "plan, first_month_price")
    return app.templates.TemplateResponse("freemium/pricing.html", {
        "request": request,
        "user": user,
        "db_user": db_user,
    })
# ==========================================================================
# SUPABASE MANAGEMENT OAUTH (Phase 1)
# ==========================================================================

@router.get("/auth/supabase/link")
async def link_supabase_account(request: Request):
    """Initiates the Supabase Management API OAuth flow."""
    user = app.get_current_user(request)
    if not SUPABASE_MGMT_CLIENT_ID or not SUPABASE_MGMT_REDIRECT_URI:
        raise HTTPException(500, "Supabase Management Auth config missing.")

    state = secrets.token_urlsafe(16)
    request.session["supabase_oauth_state"] = state

    # Send them to Supabase to authorize Gorilla Builder
    auth_url = f"https://api.supabase.com/v1/oauth/authorize?client_id={SUPABASE_MGMT_CLIENT_ID}&response_type=code&redirect_uri={urllib.parse.quote(SUPABASE_MGMT_REDIRECT_URI)}&state={state}"
    return RedirectResponse(auth_url)

@router.get("/auth/supabase/callback")
async def auth_supabase_callback(request: Request, code: str, state: str):
    """Exchanges the auth code for Management Tokens and saves them to the DB."""
    try:
        user = app.get_current_user(request)
        saved_state = request.session.pop("supabase_oauth_state", None)

        if not saved_state or state != saved_state:
            return RedirectResponse("/dashboard?error=supabase_invalid_state", status_code=303)

        async with httpx.AsyncClient() as client:
            res = await client.post(
                "https://api.supabase.com/v1/oauth/token",
                data={
                    "client_id": SUPABASE_MGMT_CLIENT_ID,
                    "client_secret": SUPABASE_MGMT_CLIENT_SECRET,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": SUPABASE_MGMT_REDIRECT_URI
                },
                headers={"Accept": "application/json"}
            )

            # 🛑 THE FIX: Allow 201 Created in addition to 200 OK
            if res.status_code not in [200, 201]:
                print(f"⚠️ Supabase OAuth Error ({res.status_code}): {res.text}")
                return RedirectResponse("/dashboard?error=supabase_token_exchange_failed", status_code=303)

            tokens = res.json()
            access_token = tokens.get("access_token")
            refresh_token = tokens.get("refresh_token")

            if access_token:
                # Save the management tokens directly to the user's profile
                app.supabase.table("users").update({
                    "supabase_access_token": access_token,
                    "supabase_refresh_token": refresh_token
                }).eq("id", user["id"]).execute()
                print(f"✅ Supabase tokens successfully saved for user {user['id']}")

        # Replace only the final return in auth_supabase_callback:
        is_popup = request.session.pop("supabase_oauth_popup", False)
        if is_popup:
            return HTMLResponse("""<!DOCTYPE html><html><head><title>Connected</title></head>
        <body><script>
        window.opener && window.opener.postMessage({type:'supabase_linked',ok:true},'*');
        window.close();
        </script><p>Supabase connected. You can close this window.</p></body></html>""")
        return RedirectResponse("/dashboard?success=supabase_linked", status_code=303)

    except Exception as e:
        print(f"⚠️ Supabase Auth Callback crashed: {e}")
        return RedirectResponse("/dashboard?error=supabase_auth_crash", status_code=303)

# ==========================================================================
# PopUp Oauth
# ==========================================================================

@router.get("/auth/supabase/link-popup")
async def link_supabase_popup(request: Request):
    user = app.get_current_user(request)

    # If already linked, close the popup immediately
    user_data = app.db_select_one("users", {"id": user["id"]}, "supabase_access_token")
    if user_data and user_data.get("supabase_access_token"):
        return HTMLResponse("""<!DOCTYPE html><html><body><script>
window.opener && window.opener.postMessage({type:'supabase_linked',ok:true},'*');
window.close();
</script><p>Already connected.</p></body></html>""")

    if not SUPABASE_MGMT_CLIENT_ID or not SUPABASE_MGMT_REDIRECT_URI:
        raise HTTPException(500, "Supabase Management Auth config missing.")
    state = secrets.token_urlsafe(16)
    request.session["supabase_oauth_state"] = state
    request.session["supabase_oauth_popup"] = True
    auth_url = (
        f"https://api.supabase.com/v1/oauth/authorize"
        f"?client_id={SUPABASE_MGMT_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={urllib.parse.quote(SUPABASE_MGMT_REDIRECT_URI)}"
        f"&state={state}"
    )
    return RedirectResponse(auth_url)


@router.get("/auth/github/link-popup")
async def link_github_popup(request: Request):
    user = app.get_current_user(request)

    # If already linked, close the popup immediately
    user_data = app.db_select_one("users", {"id": user["id"]}, "github_access_token")
    if user_data and user_data.get("github_access_token"):
        return HTMLResponse("""<!DOCTYPE html><html><body><script>
window.opener && window.opener.postMessage({type:'github_linked',ok:true},'*');
window.close();
</script><p>Already connected.</p></body></html>""")

    if not GITHUB_CLIENT_ID or not GITHUB_REDIRECT_URI:
        raise HTTPException(500, "GitHub Auth config missing.")
    request.session["github_oauth_popup"] = True
    scope = "user:email repo"
    auth_url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&redirect_uri={urllib.parse.quote(GITHUB_REDIRECT_URI)}"
        f"&scope={scope}"
        f"&state=link_{user['id']}"
    )
    return RedirectResponse(auth_url)

@router.post("/api/tokens/spin")
async def spin_wheel(request: Request):
    user = app.get_current_user(request)
    payload = await request.json()

    try:
        wager = int(payload.get("wager", 0))
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid wager amount.")

    if wager < 0 or wager > 500_000:
        raise HTTPException(400, "Wager must be between 0 and 500,000.")

    # Fetch state from DB first
    user_data = app.supabase.table("users").select(
        "last_spin_date, tokens_used, tokens_limit"
    ).eq("id", user["id"]).single().execute().data or {}

    used  = int(user_data.get("tokens_used")  or 0)
    limit = int(user_data.get("tokens_limit") or app.DEFAULT_TOKEN_LIMIT)
    remaining = max(0, limit - used)

    if wager > remaining:
        raise HTTPException(400, "You do not have enough credits for this wager.")

    # ✅ UTC date — consistent regardless of server timezone
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if user_data.get("last_spin_date") == today_str:
        raise HTTPException(400, "You have already used your daily spin.")

    rand = random.random()
    if rand < 0.50:
        multiplier = 0.0
    elif rand < 0.80:
        multiplier = 1.5
    else:
        multiplier = 2.0

    if multiplier == 0.0:
        net_change = -wager
    else:
        net_change = int(wager * multiplier) - wager

    # ✅ Floor at 0 — tokens_used can NEVER go negative (infinite token exploit fixed)
    new_used = max(0, used - net_change)

    app.supabase.table("users").update({
        "last_spin_date": today_str,
        "tokens_used":    new_used,
    }).eq("id", user["id"]).execute()

    return {"status": "success", "multiplier": multiplier, "net_change": net_change}

# ==========================================================================
# SETTINGS & AGENT SKILLS ROUTES
# ==========================================================================

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = app.get_current_user(request)

    try:
        res = app.supabase.table("users").select(
            "plan, email, gorilla_api_key, github_access_token, figma_access_token, supabase_access_token"
        ).eq("id", user["id"]).single().execute()

        if res and res.data:
            db_user = res.data

            raw_plan = db_user.get("plan") or "free"
            user["plan"] = str(raw_plan).lower().strip()

            user["email"] = db_user.get("email") or user.get("email", "")
            user["gorilla_api_key"] = db_user.get("gorilla_api_key") or ""
            user["has_github"] = bool(db_user.get("github_access_token"))
            user["has_figma"] = bool(db_user.get("figma_access_token"))
            user["has_supabase"] = bool(db_user.get("supabase_access_token"))
        else:
            user["plan"] = "free"
            user["gorilla_api_key"] = ""
            user["has_github"] = False
            user["has_figma"] = False
            user["has_supabase"] = False

    except Exception as e:
        print(f"Error loading settings: {e}")
        user["plan"] = "free"
        user["gorilla_api_key"] = ""

    success_msg = request.query_params.get("success")
    error_msg = request.query_params.get("error")

    return app.templates.TemplateResponse("dashboard/settings.html", {
        "request": request,
        "user": user,
        "success": success_msg,
        "error": error_msg
    })


# 3. REGENERATE API KEY ROUTE
@router.post("/api/user/regenerate-key")
async def regenerate_api_key(request: Request):
    user = app.get_current_user(request)
    new_key = f"gb_live_{secrets.token_hex(24)}"
    try:
        app.supabase.table("users").update({"gorilla_api_key": new_key}).eq("id", user["id"]).execute()
        return RedirectResponse("/settings?success=API+Key+regenerated+successfully", status_code=303)
    except Exception as e:
        return RedirectResponse("/settings?error=Failed+to+regenerate+key", status_code=303)


@router.get("/settings/skills", response_class=HTMLResponse)
async def agent_skills_page(request: Request):
    user = app.get_current_user(request)
    api_key = ""
    try:
        res = app.supabase.table("users").select("plan, gorilla_api_key").eq("id", user["id"]).single().execute()
        if res and res.data:
            user["plan"] = res.data.get("plan", "free")
            api_key = res.data.get("gorilla_api_key", "")
        else:
            user["plan"] = "free"
    except Exception:
        user["plan"] = "free"

    return app.templates.TemplateResponse(
        "dashboard/agentskills.html",
        {
            "request": request,
            "user": user,
            "gorilla_api_key": api_key
        }
    )

@router.post("/api/user/skills")
async def save_agent_skills(request: Request):
    user = app.get_current_user(request)
    try:
        payload = await request.json()
        app.supabase.table("users").update({"agent_skills": payload}).eq("id", user["id"]).execute()
        return JSONResponse({"status": "success", "message": "Skills saved successfully"})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"detail": f"Failed to save skills: {str(e)}"}, status_code=500)
