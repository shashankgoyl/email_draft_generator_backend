"""email_router.py — Email generation, history, and stats routes.
No file_storage — email body is stored directly as TEXT in SQLite."""

import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from auth import get_current_user
from schemas import (
    FetchThreadsRequest, GenerateEmailRequest, GenerateMultipleEmailsRequest,
    UpdateSessionRequest, EmailResponse, MultiEmailResponse,
    MultiAddressThreadsResponse, AddressThreadsResponse,
    HistoryResponse, StatsResponse, UpdateResponse,
)
from graph import (
    get_threads_for_multiple_addresses,
    filter_threads_by_goal,
    generate_email_from_thread,
    generate_new_email,
)
from database import (
    save_generation, update_session, get_all_sessions,
    get_session_by_id, delete_session, clear_all_history, get_stats,
)

load_dotenv()

TIMEZONE = os.getenv("TIMEZONE", "UTC")
router = APIRouter()


def _session_id() -> str:
    return f"session_{datetime.now(timezone.utc).strftime('%d%m%Y_%H%M%S%f')}"


# ── FETCH THREADS ─────────────────────────────────────────────────────────────

@router.post("/fetch-threads", response_model=MultiAddressThreadsResponse, tags=["Email"])
async def fetch_threads_endpoint(
    request: FetchThreadsRequest,
    current_user: dict = Depends(get_current_user),
):
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


# ── GENERATE EMAIL ────────────────────────────────────────────────────────────

@router.post("/generate-email", response_model=EmailResponse, tags=["Email"])
async def generate_email_endpoint(
    request: GenerateEmailRequest,
    current_user: dict = Depends(get_current_user),
):
    try:
        if request.provider.lower() not in ("gmail", "outlook"):
            raise HTTPException(status_code=400, detail="Provider must be 'gmail' or 'outlook'")

        is_new = request.thread_id is None

        result = (
            generate_new_email(request.email_address, request.email_goal, request.tone)
            if is_new
            else generate_email_from_thread(
                email_address=request.email_address,
                thread_id=request.thread_id,
                selected_email_index=request.selected_email_index,
                email_goal=request.email_goal,
                provider=request.provider,
                tone=request.tone,
                max_emails=request.max_emails,
            )
        )

        if not result.get("success"):
            return EmailResponse(success=False, email_address=request.email_address,
                                 message=result.get("error", "Failed"))

        intent = result.get("intent", "new" if is_new else "reply")
        body   = result.get("email", "")
        sid    = _session_id()

        saved_id = save_generation({
            "email_address":        request.email_address,
            "thread_subject":       result.get("thread_subject", "New Email"),
            "intent":               intent,
            "subject":              result.get("subject", ""),
            "email_body":           body,           # stored as TEXT in SQLite
            "tone":                 request.tone,
            "selected_email_index": request.selected_email_index,
            "email_goal":           request.email_goal,
            "thread_email_count":   result.get("thread_email_count", 0),
            "is_new_email":         is_new,
        }, session_id=sid)

        return EmailResponse(
            success=True,
            email_address=request.email_address,
            subject=result.get("subject", ""),
            email=body,
            thread_subject=result.get("thread_subject"),
            thread_email_count=result.get("thread_email_count", 0),
            is_new_email=is_new,
            intent=intent,
            session_id=saved_id,
            message=f"Email generated (Intent: {intent})",
        )
    except HTTPException:
        raise
    except Exception as e:
        return EmailResponse(success=False, email_address=request.email_address, message=str(e))


# ── GENERATE MULTIPLE ─────────────────────────────────────────────────────────

@router.post("/generate-multiple", response_model=MultiEmailResponse, tags=["Email"])
async def generate_multiple_emails_endpoint(
    request: GenerateMultipleEmailsRequest,
    current_user: dict = Depends(get_current_user),
):
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
                    email_addresses=[addr], provider=request.provider, max_emails=request.max_emails
                )
                threads = (
                    threads_res.get("addresses_data", [{}])[0].get("threads", [])
                    if threads_res.get("success") else []
                )

                if not threads:
                    res = generate_new_email(addr, request.email_goal, request.tone)
                else:
                    filtered = filter_threads_by_goal(threads=threads, email_goal=request.email_goal)
                    if filtered.get("success") and filtered.get("has_relevant_context"):
                        res = generate_email_from_thread(
                            email_address=addr,
                            thread_id=filtered["relevant_threads"][0]["thread_id"],
                            email_goal=request.email_goal,
                            provider=request.provider,
                            tone=request.tone,
                            max_emails=request.max_emails,
                        )
                    else:
                        res = generate_new_email(addr, request.email_goal, request.tone)

                if not res.get("success"):
                    generated.append(EmailResponse(success=False, email_address=addr, message=res.get("error")))
                    continue

                is_new = res.get("is_new_email", False)
                intent = res.get("intent", "new" if is_new else "reply")
                body   = res.get("email", "")
                sid    = _session_id()

                saved_id = save_generation({
                    "email_address":      addr,
                    "thread_subject":     res.get("thread_subject", "New Email"),
                    "intent":             intent,
                    "subject":            res.get("subject", ""),
                    "email_body":         body,
                    "tone":               request.tone,
                    "email_goal":         request.email_goal,
                    "thread_email_count": res.get("thread_email_count", 0),
                    "is_new_email":       is_new,
                }, session_id=sid)

                generated.append(EmailResponse(
                    success=True, email_address=addr,
                    subject=res.get("subject", ""), email=body,
                    thread_subject=res.get("thread_subject"),
                    thread_email_count=res.get("thread_email_count", 0),
                    is_new_email=is_new, intent=intent, session_id=saved_id,
                ))
            except Exception as e:
                generated.append(EmailResponse(success=False, email_address=addr, message=str(e)))

        ok = sum(1 for e in generated if e.success)
        return MultiEmailResponse(success=True, emails=generated, total_generated=ok,
                                  message=f"Generated {ok} emails")
    except HTTPException:
        raise
    except Exception as e:
        return MultiEmailResponse(success=False, message=str(e))


# ── HISTORY ───────────────────────────────────────────────────────────────────

@router.put("/history/{session_id}", response_model=UpdateResponse, tags=["History"])
async def update_session_endpoint(
    session_id: str,
    request: UpdateSessionRequest,
    current_user: dict = Depends(get_current_user),
):
    if not get_session_by_id(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    data = {k: v for k, v in {
        "subject":    request.subject,
        "email_body": request.email_body,
        "email_goal": request.email_goal,
        "tone":       request.tone,
    }.items() if v is not None}
    ok = update_session(session_id, data)
    return UpdateResponse(success=ok, message="Updated" if ok else "Failed")


@router.get("/history", response_model=HistoryResponse, tags=["History"])
async def get_history(limit: int = 50, current_user: dict = Depends(get_current_user)):
    try:
        sessions = get_all_sessions(limit=limit)
        return HistoryResponse(success=True, sessions=sessions, total=len(sessions))
    except Exception as e:
        return HistoryResponse(success=False, message=str(e))


@router.get("/history/{session_id}", tags=["History"])
async def get_history_by_id(session_id: str, current_user: dict = Depends(get_current_user)):
    session = get_session_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True, "session": session}


@router.delete("/history/{session_id}", tags=["History"])
async def delete_history_item(session_id: str, current_user: dict = Depends(get_current_user)):
    if not delete_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True, "message": "Deleted"}


@router.post("/history/clear", tags=["History"])
async def clear_history(current_user: dict = Depends(get_current_user)):
    clear_all_history()
    return {"success": True, "message": "All history cleared"}


# ── STATS ─────────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=StatsResponse, tags=["General"])
async def get_statistics(current_user: dict = Depends(get_current_user)):
    try:
        return StatsResponse(success=True, stats=get_stats())
    except Exception as e:
        return StatsResponse(success=False, message=str(e))
