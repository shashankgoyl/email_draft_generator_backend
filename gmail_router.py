"""
gmail_router.py — Gmail OAuth endpoints.

OAuth logic lives in email_provider.py.
This module only defines the routes that expose that logic via HTTP.
"""

from fastapi import APIRouter, HTTPException, Depends
from auth import get_current_user
from email_provider import get_gmail_auth_url, complete_gmail_auth

router = APIRouter(prefix="/gmail", tags=["Gmail OAuth"])

@router.get("/auth-url")
async def gmail_auth_url(current_user: dict = Depends(get_current_user)):
    """Step 1 — Get the Gmail OAuth authorisation URL.

    Open the returned URL in your browser, grant access, and Google will
    redirect to OAUTH_REDIRECT_URI with a `code` query parameter.
    That redirect is handled automatically by /gmail/auth-callback.
    If the redirect fails, copy the `code` and POST it to /gmail/complete-auth."""
    try:
        url = get_gmail_auth_url()
        return {
            "success": True,
            "auth_url": url,
            "instructions": (
                "Open auth_url in your browser and grant access. "
                "Google will redirect to /gmail/auth-callback automatically. "
                "If that fails, copy the code= param and POST to /gmail/complete-auth."
            ),
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate auth URL: {e}")

@router.get("/auth-callback")
async def gmail_auth_callback(code: str):
    """Step 2 (automatic) — Google redirects here after the user grants access.

    Exchanges the authorisation code for a token and saves token.json to the
    credentials volume so it persists across container restarts.

    Public endpoint — Google's redirect carries no auth header.
    OAUTH_REDIRECT_URI in docker-compose.yml must point here."""
    try:
        complete_gmail_auth(code)
        return {
            "success": True,
            "message": "Gmail authenticated. token.json saved — you can now fetch emails.",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OAuth callback failed: {e}")

@router.post("/complete-auth")
async def gmail_complete_auth(
    code: str,
    current_user: dict = Depends(get_current_user),
):
    """Step 2 (manual) — Supply the authorisation code yourself.

    Use this if the automatic redirect is not reachable (e.g. running locally
    without a public URL). Copy the code= parameter from the redirect URL and
    post it here."""
    try:
        complete_gmail_auth(code)
        return {"success": True, "message": "Gmail authenticated. token.json saved."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to complete auth: {e}")