"""api.py — FastAPI application entry point.

Modules:
  schemas.py        — Pydantic models
  admin_router.py   — Admin user management
  gmail_router.py   — Gmail OAuth
  auth.py           — JWT logic
  manage_users.py   — User DB operations
  database.py       — SQLite DB operations (no external DB needed)
  graph.py          — LLM logic (Groq)
  email_provider.py — Gmail fetching + OAuth logic
"""

import os
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
import uvicorn
from dotenv import load_dotenv

from auth import authenticate_user, get_current_user
from database import (
    save_generation,
    update_session,
    get_all_sessions,
    get_session_by_id,
    delete_session,
    clear_all_history,
    get_stats,
)
from graph import (
    get_threads_for_multiple_addresses,
    filter_threads_by_goal,
    generate_email_from_thread,
    generate_new_email,
)
from schemas import (
    TokenResponse,
    FetchThreadsRequest,
    GenerateEmailRequest,
    GenerateMultipleEmailsRequest,
    UpdateSessionRequest,
    AddressThreadsResponse,
    MultiAddressThreadsResponse,
    EmailResponse,
    MultiEmailResponse,
    HistoryResponse,
    StatsResponse,
    UpdateResponse,
)
from admin_router import router as admin_router
from gmail_router import router as gmail_router

load_dotenv()

TIMEZONE = os.getenv("TIMEZONE", "UTC")


def _now_session_id() -> str:
    return f"session_{datetime.now(timezone.utc).strftime('%d%m%Y_%H%M%S%f')}"


# ── APP SETUP ────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Email Draft Generator",
    description="AI-powered contextual email generation with Gmail context.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin_router)
app.include_router(gmail_router)


# ── PUBLIC ENDPOINTS ─────────────────────────────────────────────────────────

@app.get("/", tags=["General"])
async def root():
    return {
        "service": "Email Draft Generator",
        "version": "2.0.0",
        "status": "running",
        "database": "SQLite (built-in, no external service)",
        "llm": f"Groq — {os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')}",
        "endpoints": {
            "login":            "POST /login",
            "health":           "GET  /health",
            "fetch_threads":    "POST /fetch-threads",
            "generate_email":   "POST /generate-email",
            "generate_multiple":"POST /generate-multiple",
            "history":          "GET  /history",
            "stats":            "GET  /stats",
            "admin_users":      "POST/GET/DELETE /admin/users",
            "gmail_auth":       "GET  /gmail/auth-url",
        },
    }


@app.get("/health", tags=["General"])
async def health_check():
    try:
        stats = get_stats()
        return {
            "status": "healthy",
            "llm": {"model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"), "provider": "Groq"},
            "database": {"type": "SQLite", "status": "connected", "stats": stats},
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "unhealthy", "error": str(e)})


@app.post("/login", response_model=TokenResponse, tags=["Auth"])
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """Login with email + password. Returns JWT Bearer token."""
    token = authenticate_user(form_data.username, form_data.password)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenResponse(access_token=token)


# ── EMAIL ENDPOINTS ───────────────────────────────────────────────────────────

@app.post("/fetch-threads", response_model=MultiAddressThreadsResponse, tags=["Email"])
async def fetch_threads_endpoint(
    request: FetchThreadsRequest,
    current_user: dict = Depends(get_current_user),
):
    """Fetch Gmail threads for one or more email addresses."""
    try:
        addresses = [a.strip() for a in request.email_addresses.split(",") if a.strip()]
        if not addresses:
            raise HTTPException(status_code=400, detail="At least one email address required")
        if request.provider.lower() not in ("gmail", "outlook"):
            raise HTTPException(status_code=400, detail="Provider must be 'gmail' or 'outlook'")

        result = get_threads_for_multiple_addresses(
            email_addresses=addresses,
            provider=request.provider,
            max_emails=request.max_emails,
        )
        if not result.get("success"):
            return MultiAddressThreadsResponse(success=False, message=result.get("error", "Failed"))

        addresses_data = []
        for addr in result.get("addresses_data", []):
            threads = addr["threads"]
            ad = AddressThreadsResponse(
                email_address=addr["email_address"],
                threads=threads,
                total_emails=addr["total_emails"],
                has_context=len(threads) > 0,
            )
            if request.email_goal and threads:
                filtered = filter_threads_by_goal(threads=threads, email_goal=request.email_goal)
                if filtered.get("success"):
                    relevant = filtered.get("relevant_threads", [])
                    ad.relevant_threads = relevant
                    ad.has_context = filtered.get("has_relevant_context", bool(relevant))
            addresses_data.append(ad)

        return MultiAddressThreadsResponse(
            success=True,
            addresses_data=addresses_data,
            email_goal=request.email_goal,
            total_addresses=len(addresses_data),
            message="Threads fetched successfully",
        )
    except HTTPException:
        raise
    except Exception as e:
        return MultiAddressThreadsResponse(success=False, message=f"Server error: {e}")


@app.post("/generate-email", response_model=EmailResponse, tags=["Email"])
async def generate_email_endpoint(
    request: GenerateEmailRequest,
    current_user: dict = Depends(get_current_user),
):
    """Generate an email. thread_id=None means new email from scratch."""
    try:
        if request.provider.lower() not in ("gmail", "outlook"):
            raise HTTPException(status_code=400, detail="Provider must be 'gmail' or 'outlook'")

        is_new = request.thread_id is None

        if is_new:
            result = generate_new_email(
                email_address=request.email_address,
                email_goal=request.email_goal,
                tone=request.tone,
            )
        else:
            result = generate_email_from_thread(
                email_address=request.email_address,
                thread_id=request.thread_id,
                selected_email_index=request.selected_email_index,
                email_goal=request.email_goal,
                provider=request.provider,
                tone=request.tone,
                max_emails=request.max_emails,
            )

        if not result.get("success"):
            return EmailResponse(
                success=False,
                email_address=request.email_address,
                message=result.get("error", "Generation failed"),
            )

        intent    = result.get("intent", "new" if is_new else "reply")
        email_body = result.get("email", "")
        session_id = _now_session_id()

        saved_id = save_generation(
            session_data={
                "email_address":        request.email_address,
                "thread_subject":       result.get("thread_subject", "New Email"),
                "intent":               intent,
                "subject":              result.get("subject", ""),
                "email_body":           email_body,          # stored directly in SQLite
                "tone":                 request.tone,
                "selected_email_index": request.selected_email_index,
                "email_goal":           request.email_goal,
                "thread_email_count":   result.get("thread_email_count", 0),
                "is_new_email":         is_new,
            },
            session_id=session_id,
        )

        return EmailResponse(
            success=True,
            email_address=request.email_address,
            subject=result.get("subject", ""),
            email=email_body,
            thread_subject=result.get("thread_subject"),
            thread_email_count=result.get("thread_email_count", 0),
            is_new_email=is_new,
            intent=intent,
            session_id=saved_id,
            message=f"Email generated successfully (Intent: {intent})",
        )
    except HTTPException:
        raise
    except Exception as e:
        return EmailResponse(success=False, email_address=request.email_address, message=f"Server error: {e}")


@app.post("/generate-multiple", response_model=MultiEmailResponse, tags=["Email"])
async def generate_multiple_emails_endpoint(
    request: GenerateMultipleEmailsRequest,
    current_user: dict = Depends(get_current_user),
):
    """Generate emails for multiple addresses based on a shared goal."""
    try:
        addresses = [a.strip() for a in request.email_addresses.split(",") if a.strip()]
        if not addresses:
            raise HTTPException(status_code=400, detail="At least one email address required")
        if not request.email_goal:
            raise HTTPException(status_code=400, detail="Email goal required")

        generated = []
        for addr in addresses:
            try:
                threads_res = get_threads_for_multiple_addresses(
                    email_addresses=[addr],
                    provider=request.provider,
                    max_emails=request.max_emails,
                )
                threads = threads_res.get("addresses_data", [{}])[0].get("threads", []) if threads_res.get("success") else []

                if not threads:
                    email_result = generate_new_email(addr, request.email_goal, request.tone)
                else:
                    filtered = filter_threads_by_goal(threads=threads, email_goal=request.email_goal)
                    if filtered.get("success") and filtered.get("has_relevant_context"):
                        email_result = generate_email_from_thread(
                            email_address=addr,
                            thread_id=filtered["relevant_threads"][0]["thread_id"],
                            email_goal=request.email_goal,
                            provider=request.provider,
                            tone=request.tone,
                            max_emails=request.max_emails,
                        )
                    else:
                        email_result = generate_new_email(addr, request.email_goal, request.tone)

                if not email_result.get("success"):
                    generated.append(EmailResponse(success=False, email_address=addr, message=email_result.get("error")))
                    continue

                is_new    = email_result.get("is_new_email", False)
                intent    = email_result.get("intent", "new" if is_new else "reply")
                body      = email_result.get("email", "")
                sid       = _now_session_id()

                saved_id = save_generation({
                    "email_address":      addr,
                    "thread_subject":     email_result.get("thread_subject", "New Email"),
                    "intent":             intent,
                    "subject":            email_result.get("subject", ""),
                    "email_body":         body,
                    "tone":               request.tone,
                    "email_goal":         request.email_goal,
                    "thread_email_count": email_result.get("thread_email_count", 0),
                    "is_new_email":       is_new,
                }, session_id=sid)

                generated.append(EmailResponse(
                    success=True,
                    email_address=addr,
                    subject=email_result.get("subject", ""),
                    email=body,
                    thread_subject=email_result.get("thread_subject"),
                    thread_email_count=email_result.get("thread_email_count", 0),
                    is_new_email=is_new,
                    intent=intent,
                    session_id=saved_id,
                ))
            except Exception as e:
                generated.append(EmailResponse(success=False, email_address=addr, message=str(e)))

        ok = sum(1 for e in generated if e.success)
        return MultiEmailResponse(success=True, emails=generated, total_generated=ok,
                                  message=f"Generated {ok} emails")
    except HTTPException:
        raise
    except Exception as e:
        return MultiEmailResponse(success=False, message=f"Server error: {e}")


# ── HISTORY ENDPOINTS ─────────────────────────────────────────────────────────

@app.get("/history", response_model=HistoryResponse, tags=["History"])
async def get_history(limit: int = 50, current_user: dict = Depends(get_current_user)):
    try:
        sessions = get_all_sessions(limit=limit)
        return HistoryResponse(success=True, sessions=sessions, total=len(sessions))
    except Exception as e:
        return HistoryResponse(success=False, message=str(e))


@app.get("/history/{session_id}", tags=["History"])
async def get_history_by_id(session_id: str, current_user: dict = Depends(get_current_user)):
    session = get_session_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True, "session": session}


@app.put("/history/{session_id}", response_model=UpdateResponse, tags=["History"])
async def update_session_endpoint(
    session_id: str,
    request: UpdateSessionRequest,
    current_user: dict = Depends(get_current_user),
):
    if not get_session_by_id(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    update_data = {k: v for k, v in {
        "subject":    request.subject,
        "email_body": request.email_body,
        "email_goal": request.email_goal,
        "tone":       request.tone,
    }.items() if v is not None}
    ok = update_session(session_id, update_data)
    return UpdateResponse(success=ok, message="Updated" if ok else "Failed")


@app.delete("/history/{session_id}", tags=["History"])
async def delete_history_item(session_id: str, current_user: dict = Depends(get_current_user)):
    if not delete_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True, "message": "Deleted"}


@app.post("/history/clear", tags=["History"])
async def clear_history_endpoint(current_user: dict = Depends(get_current_user)):
    clear_all_history()
    return {"success": True, "message": "All history cleared"}


@app.get("/stats", response_model=StatsResponse, tags=["General"])
async def get_statistics(current_user: dict = Depends(get_current_user)):
    try:
        return StatsResponse(success=True, stats=get_stats())
    except Exception as e:
        return StatsResponse(success=False, message=str(e))


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print(f"\n{'='*60}")
    print("  EMAIL DRAFT GENERATOR v2.0")
    print(f"{'='*60}")
    print(f"  API:      http://localhost:{port}")
    print(f"  Docs:     http://localhost:{port}/docs")
    print(f"  LLM:      Groq — {os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')}")
    print(f"  Database: SQLite — {os.getenv('DB_PATH', './email_generator.db')}")
    print(f"{'='*60}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
