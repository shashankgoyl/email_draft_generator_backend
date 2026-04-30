"""
gmail_router.py — Gmail OAuth endpoints.

OAuth logic lives in email_provider.py.
This router exposes it over HTTP.

Flow:
  GET  /gmail/auth-url       → returns URL for user to open (PKCE verifier stored server-side)
  GET  /gmail/auth-callback  → Google redirects here with code + state (uses Request to accept all params)
  POST /gmail/complete-auth  → manual fallback if redirect fails
"""

from fastapi import APIRouter, HTTPException, Depends, Request
from auth import get_current_user
from email_provider import get_gmail_auth_url, complete_gmail_auth

router = APIRouter(prefix="/gmail", tags=["Gmail OAuth"])


@router.get("/auth-url")
async def gmail_auth_url(current_user: dict = Depends(get_current_user)):
    """Step 1 — Get the Gmail OAuth authorisation URL.

    Opens the Google consent screen. After the user approves, Google
    redirects to OAUTH_REDIRECT_URI (/gmail/auth-callback) with code + state.
    """
    try:
        url = get_gmail_auth_url()
        return {
            "success": True,
            "auth_url": url,
            "instructions": (
                "Open auth_url in your browser and grant Gmail access. "
                "Google will redirect to /gmail/auth-callback automatically. "
                "If the redirect fails, copy the code= value from the URL "
                "and POST it to /gmail/complete-auth."
            ),
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate auth URL: {e}")


@router.get("/auth-callback")
async def gmail_auth_callback(request: Request):
    """Step 2 (automatic) — Google redirects here after user grants access.

    Uses Request object instead of individual query params because Google
    sends extra fields (state, iss, scope) alongside code — FastAPI would
    reject the request with 422 if we declared only `code: str`.

    The `state` parameter is forwarded to complete_gmail_auth() so it can
    retrieve the matching PKCE code_verifier from the in-memory store.
    """
    params = request.query_params

    error = params.get("error")
    if error:
        error_desc = params.get("error_description", "No description")
        raise HTTPException(
            status_code=400,
            detail=f"Google OAuth error: {error} — {error_desc}"
        )

    code  = params.get("code")
    state = params.get("state")

    if not code:
        received = dict(params)
        raise HTTPException(
            status_code=400,
            detail=f"No authorization code in callback. Received params: {received}"
        )

    try:
        complete_gmail_auth(code=code, state=state)
        return {
            "success": True,
            "message": (
                "✅ Gmail authenticated successfully! "
                "token.json has been saved. You can now fetch email threads."
            ),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OAuth callback failed: {e}")


@router.delete("/token")
async def delete_gmail_token(current_user: dict = Depends(get_current_user)):
    """Delete the stored Gmail token.json — forces re-authentication."""
    from email_provider import TOKEN_PATH
    import os
    if os.path.exists(TOKEN_PATH):
        os.remove(TOKEN_PATH)
        return {"success": True, "message": "token.json deleted. Re-run Gmail OAuth to reconnect."}
    return {"success": False, "message": "No token.json found — Gmail was not authenticated."}

@router.post("/complete-auth")
async def gmail_complete_auth(
    code: str,
    state: str = None,
    current_user: dict = Depends(get_current_user),
):
    """Step 2 (manual fallback) — Supply the authorisation code yourself.

    Use this if the automatic redirect is not reachable.
    Copy the code= value from the redirect URL and POST it here.
    Optionally include the state= value too so the PKCE verifier is used.
    """
    try:
        complete_gmail_auth(code=code, state=state)
        return {
            "success": True,
            "message": "✅ Gmail authenticated. token.json saved.",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to complete auth: {e}")


@router.get("/status")
async def gmail_status(current_user: dict = Depends(get_current_user)):
    """Check whether Gmail is currently authenticated (token.json exists and is valid)."""
    import os
    from email_provider import TOKEN_PATH, SCOPES

    if not os.path.exists(TOKEN_PATH):
        return {"authenticated": False, "message": "token.json not found — run the OAuth flow."}

    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        if creds.valid:
            return {"authenticated": True, "message": "Gmail is authenticated and token is valid."}
        elif creds.expired and creds.refresh_token:
            return {"authenticated": True, "message": "Gmail token is expired but can be refreshed automatically."}
        else:
            return {"authenticated": False, "message": "Gmail token is invalid — re-run the OAuth flow."}
    except Exception as e:
        return {"authenticated": False, "message": f"Error reading token: {e}"}
