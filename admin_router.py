"""admin_router.py — Admin user management endpoints.
Protected by X-Admin-Key header (set ADMIN_SECRET_KEY in .env).

All DB operations go through manage_users.py which uses the shared
connection pool from database.py — no raw connections, no duplication."""

import os
from fastapi import APIRouter, HTTPException, Depends, Header
from dotenv import load_dotenv

from schemas import AddUserRequest, ResetPasswordRequest
from manage_users import add_user, delete_user, list_users, reset_password

load_dotenv()

router = APIRouter(prefix="/admin", tags=["Admin"])

def _verify_admin_key(x_admin_key: str = Header(...)):
    """Validate the X-Admin-Key header."""
    if x_admin_key != os.getenv("ADMIN_SECRET_KEY", "change-this-admin-secret"):
        raise HTTPException(status_code=403, detail="Invalid admin key")

@router.post("/users")
async def admin_add_user(
    request: AddUserRequest,
    _=Depends(_verify_admin_key),
):
    """Add a new user.
    Requires header: X-Admin-Key: <ADMIN_SECRET_KEY>"""
    try:
        created = add_user(request.email, request.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not created:
        raise HTTPException(status_code=409, detail=f"User '{request.email}' already exists")
    return {"success": True, "message": f"User '{request.email}' created successfully"}

@router.delete("/users/{email}")
async def admin_delete_user(
    email: str,
    _=Depends(_verify_admin_key),
):
    """Delete a user by email.
    Requires header: X-Admin-Key: <ADMIN_SECRET_KEY>"""
    deleted = delete_user(email)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"User '{email}' not found")
    return {"success": True, "message": f"User '{email}' deleted successfully"}

@router.post("/users/{email}/reset-password")
async def admin_reset_password(
    email: str,
    request: ResetPasswordRequest,
    _=Depends(_verify_admin_key),
):
    """Reset a user's password.
    Requires header: X-Admin-Key: <ADMIN_SECRET_KEY>"""
    try:
        updated = reset_password(email, request.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not updated:
        raise HTTPException(status_code=404, detail=f"User '{email}' not found")
    return {"success": True, "message": f"Password reset for '{email}'"}

@router.get("/users")
async def admin_list_users(_=Depends(_verify_admin_key)):
    """List all users (no password hashes).
    Requires header: X-Admin-Key: <ADMIN_SECRET_KEY>"""
    users = list_users()
    return {"success": True, "users": users, "total": len(users)}