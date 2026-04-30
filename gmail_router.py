"""
gmail_router.py — Gmail OAuth endpoints.
"""

from fastapi import APIRouter, HTTPException, Depends, Request
from auth import get_current_user
from email_provider import get_gmail_auth_url, complete_gmail_auth

router = APIRouter(prefix="/gmail", tags=["Gmail OAuth"])


@router.get("/auth-url")
async def gmail_auth_url(current_user: dict = Depends(get_current_user)):
    """Step 1 — Get the Gmail OAuth authorisation URL."""
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
async def gmail_auth_callback(request: Request):
    """Step 2 (automatic) — Google redirects here after user grants access.
    Uses Request object to handle all query params Google sends (code, state, scope, iss)."""
    code  = request.query_params.get("code")
    error = request.query_params.get("error")

    if error:
        raise HTTPException(status_code=400, detail=f"Google OAuth error: {error}")

    if not code:
        raise HTTPException(
            status_code=400,
            detail="No authorization code in callback. Params received: " + str(dict(request.query_params))
        )

    try:
        complete_gmail_auth(code)
        return {
            "success": True,
            "message": "Gmail authenticated! token.json saved — you can now fetch emails.",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OAuth callback failed: {e}")


@router.post("/complete-auth")
async def gmail_complete_auth(
    code: str,
    current_user: dict = Depends(get_current_user),
):
    """Step 2 (manual) — Supply the authorisation code yourself if redirect fails."""
    try:
        complete_gmail_auth(code)
        return {"success": True, "message": "Gmail authenticated. token.json saved."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to complete auth: {e}")