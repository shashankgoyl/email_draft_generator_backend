"""
LangGraph Workflow - Multi-Address Email Generation with Goal-Based Filtering
1. Fetch emails for multiple addresses and group into threads
2. Extract intent from conversation using LLM (automatic)
3. Filter threads by email goal using LLM (if goal provided)
4. Generate contextual emails from threads OR new emails from scratch

LLM: Groq (langchain-groq) — set GROQ_API_KEY in .env
"""

import os
import json
import re
from typing import Dict, Optional, List
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_groq import ChatGroq

from email_provider import fetch_threads, format_thread_for_context

load_dotenv()

# ── LLM SETUP (Groq) ─────────────────────────────────────────────────────────
# Free tier: https://console.groq.com → API Keys
# Models: llama-3.3-70b-versatile | llama3-8b-8192 | mixtral-8x7b-32768
llm = ChatGroq(
    model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
    api_key=os.getenv("GROQ_API_KEY"),
    temperature=0.7,
    max_tokens=4096,
)


# ── INTENT EXTRACTION ────────────────────────────────────────────────────────

def extract_intent_from_thread(thread: Dict, email_goal: Optional[str] = None) -> str:
    """Use LLM to extract reply intent from a conversation thread."""
    print("\n🎯 Extracting intent from conversation thread...")

    thread_summary = (
        f"Subject: {thread.get('subject', 'No Subject')}\n"
        f"Total Emails: {thread.get('email_count', 0)}\n"
        f"Participants: {', '.join(thread.get('participants', []))}\n"
        f"Recent Snippet: {thread.get('snippet', '')[:300]}\n"
    )

    if thread.get("emails"):
        for idx, email in enumerate(thread["emails"][-2:]):
            thread_summary += (
                f"\nEmail #{idx + 1}:\n"
                f"From: {email.get('from', 'Unknown')}\n"
                f"Date: {email.get('date', 'Unknown')}\n"
                f"Body: {email.get('body', '')[:400]}...\n"
            )

    system_msg = (
        "You are an expert email assistant. Analyse the conversation thread and "
        "return ONLY ONE WORD from: reply, follow_up, reminder, inquiry"
    )
    user_msg = f"Thread:\n{thread_summary}"
    if email_goal:
        user_msg += f"\n\nUser Goal: {email_goal}"
    user_msg += "\n\nReturn ONLY the intent word:"

    try:
        response = llm.invoke([SystemMessage(content=system_msg), HumanMessage(content=user_msg)])
        intent = response.content.strip().lower()
        valid = ["reply", "follow_up", "reminder", "inquiry"]
        if intent not in valid:
            for v in valid:
                if v in intent:
                    return v
            return "reply"
        print(f"✅ Intent: {intent}")
        return intent
    except Exception as e:
        print(f"❌ Intent extraction error: {e}")
        return "reply"


# ── MULTI-ADDRESS THREAD FETCHING ────────────────────────────────────────────

def get_threads_for_multiple_addresses(
    email_addresses: List[str],
    provider: str = "gmail",
    max_emails: int = 100,
) -> Dict:
    print(f"\n📧 Fetching threads for {len(email_addresses)} address(es)...")
    addresses_data = []

    for email_address in email_addresses:
        try:
            threads = fetch_threads(provider=provider, email_address=email_address, max_results=max_emails)
            total_emails = sum(t["email_count"] for t in threads)
            addresses_data.append({
                "email_address": email_address,
                "threads": threads,
                "total_emails": total_emails,
                "success": True,
            })
            print(f"✅ {email_address}: {len(threads)} threads ({total_emails} emails)")
        except Exception as e:
            print(f"❌ Error fetching threads for {email_address}: {e}")
            addresses_data.append({
                "email_address": email_address,
                "threads": [],
                "total_emails": 0,
                "success": False,
                "error": str(e),
            })

    return {"success": True, "addresses_data": addresses_data, "total_addresses": len(addresses_data)}


# ── GOAL-BASED THREAD FILTERING ──────────────────────────────────────────────

def filter_threads_by_goal(threads: List[Dict], email_goal: str) -> Dict:
    """Use LLM to find threads relevant to the email goal."""
    print(f"\n🎯 Filtering {len(threads)} threads by goal: {email_goal[:60]}...")

    if not threads:
        return {"success": True, "relevant_threads": [], "has_relevant_context": False,
                "message": "No threads — will generate new email."}

    thread_summaries = [
        {
            "index": i,
            "thread_id": t["thread_id"],
            "subject": t["subject"],
            "email_count": t["email_count"],
            "participants": ", ".join(t["participants"][:3]),
            "snippet": t["snippet"][:200],
        }
        for i, t in enumerate(threads)
    ]

    system_msg = (
        "You are an expert email assistant. Given a user's email goal and conversation threads, "
        "identify which threads are directly relevant.\n"
        "Return ONLY a JSON array of relevant thread indices (most relevant first), e.g. [2, 0]\n"
        "If none are relevant return []"
    )
    formatted = "\n".join(
        f"Index {s['index']}: Subject: \"{s['subject']}\" | "
        f"Emails: {s['email_count']} | Participants: {s['participants']} | "
        f"Snippet: {s['snippet']}"
        for s in thread_summaries
    )
    user_msg = f"Email Goal:\n{email_goal}\n\nThreads:\n{formatted}\n\nReturn ONLY the JSON array:"

    try:
        response = llm.invoke([SystemMessage(content=system_msg), HumanMessage(content=user_msg)])
        text = response.content.strip()
        match = re.search(r"\[[\d,\s]*\]", text)
        indices = json.loads(match.group()) if match else []
        relevant = [threads[i] for i in indices if 0 <= i < len(threads)]
        has_context = len(relevant) > 0
        print(f"{'✅ Found' if has_context else 'ℹ️  No'} relevant threads ({len(relevant)})")
        return {"success": True, "relevant_threads": relevant, "has_relevant_context": has_context,
                "total_relevant": len(relevant)}
    except Exception as e:
        print(f"❌ Filter error: {e}")
        return {"success": False, "relevant_threads": [], "has_relevant_context": False, "error": str(e)}


# ── WORKFLOW NODES ────────────────────────────────────────────────────────────

def node_fetch_threads(state: Dict) -> Dict:
    print("\n📧 NODE 1: FETCHING THREADS")
    email_address = state.get("email_address")
    if not email_address:
        state["threads"] = []
        state["error"] = "No email address provided"
        return state
    try:
        threads = fetch_threads(
            provider=state.get("provider", "gmail"),
            email_address=email_address,
            max_results=state.get("max_emails", 100),
        )
        state["threads"] = threads
        state["total_emails"] = sum(t["email_count"] for t in threads)
        print(f"✅ {len(threads)} threads fetched")
    except Exception as e:
        print(f"❌ {e}")
        state["threads"] = []
        state["error"] = str(e)
    return state


def node_prepare_context(state: Dict) -> Dict:
    print("\n🧵 NODE 2: PREPARING CONTEXT + EXTRACTING INTENT")
    threads = state.get("threads", [])
    selected_thread_id = state.get("selected_thread_id")

    selected_thread = next((t for t in threads if t["thread_id"] == selected_thread_id), None)
    if not selected_thread:
        state["error"] = "Selected thread not found"
        return state

    intent = extract_intent_from_thread(selected_thread, state.get("email_goal", ""))
    state["intent"] = intent
    state["thread_context"] = format_thread_for_context(selected_thread, state.get("selected_email_index"))
    state["selected_thread"] = selected_thread
    print(f"✅ Context ready | Intent: {intent}")
    return state


def node_generate_email(state: Dict) -> Dict:
    print("\n✍️ NODE 3: GENERATING EMAIL")
    thread_context = state.get("thread_context", "")
    intent = state.get("intent", "reply")
    tone = state.get("tone", "professional")
    email_goal = state.get("email_goal", "")

    if not thread_context:
        state["error"] = "No thread context"
        return state

    intent_instructions = {
        "reply": "Write a direct, responsive reply to the most recent email.",
        "follow_up": "Write a follow-up continuing the conversation with updates.",
        "reminder": "Write a polite reminder about pending items or unanswered questions.",
        "inquiry": "Write an email asking for information or clarification.",
    }

    system_msg = (
        f"You are a professional email writer.\n"
        f"TONE: {tone}\n"
        f"INTENT: {intent} — {intent_instructions.get(intent, '')}\n\n"
        "RULES:\n"
        f"- Write in a {tone} tone.\n"
        "- Do NOT invent facts, names, or company details.\n"
        "- Sound human, avoid robotic phrasing.\n"
        "- If the goal specifies a word count, match it (±5 words).\n"
        "- Otherwise aim for 100–160 words.\n\n"
        "FORMAT:\nSubject: [subject line]\n\n[email body]"
    )
    user_msg = (
        f"Conversation thread:\n{thread_context}\n\n"
        f"Email Goal: {email_goal or 'Use thread context and intent.'}\n\n"
        "Write the complete email (Subject + Body):"
    )

    try:
        response = llm.invoke([SystemMessage(content=system_msg), HumanMessage(content=user_msg)])
        raw = response.content.partition("</reasoning>")[2].strip() or response.content.strip()
        subject, email_body = _parse_subject_body(raw)
        state["generated_email"] = email_body
        state["subject"] = subject
        print(f"✅ Email generated | Subject: {subject}")
    except Exception as e:
        print(f"❌ Generation error: {e}")
        state["generated_email"] = ""
        state["subject"] = "Error"
        state["error"] = str(e)
    return state


def _parse_subject_body(text: str):
    """Extract Subject line and body from LLM output."""
    subject = "Email"
    body = text
    if "Subject:" in text:
        lines = text.split("\n")
        for idx, line in enumerate(lines):
            if line.strip().startswith("Subject:"):
                subject = line.split("Subject:", 1)[1].strip()
                remaining = lines[:idx] + lines[idx + 1:]
                while remaining and not remaining[0].strip():
                    remaining.pop(0)
                body = "\n".join(remaining).strip()
                break
    return subject, body


# ── BUILD WORKFLOW ────────────────────────────────────────────────────────────

def _build_workflow():
    wf = StateGraph(dict)
    wf.add_node("fetch_threads",    node_fetch_threads)
    wf.add_node("prepare_context",  node_prepare_context)
    wf.add_node("generate_email",   node_generate_email)
    wf.set_entry_point("fetch_threads")
    wf.add_edge("fetch_threads",   "prepare_context")
    wf.add_edge("prepare_context", "generate_email")
    wf.add_edge("generate_email",  END)
    return wf.compile()


email_workflow = _build_workflow()


# ── PUBLIC API FUNCTIONS ──────────────────────────────────────────────────────

def generate_email_from_thread(
    email_address: str,
    thread_id: str,
    intent: Optional[str] = None,
    selected_email_index: Optional[int] = None,
    email_goal: str = "",
    provider: str = "gmail",
    tone: str = "professional",
    max_emails: int = 100,
) -> Dict:
    print(f"\n🚀 Generating from thread | Address: {email_address} | Thread: {thread_id}")
    state = {
        "email_address": email_address,
        "provider": provider,
        "max_emails": max_emails,
        "selected_thread_id": thread_id,
        "selected_email_index": selected_email_index,
        "intent": intent,
        "tone": tone,
        "email_goal": email_goal,
        "threads": [],
        "thread_context": "",
        "generated_email": "",
        "subject": "",
        "error": None,
    }
    result = email_workflow.invoke(state)
    if result.get("error"):
        return {"success": False, "error": result["error"]}
    return {
        "success": True,
        "subject": result.get("subject", ""),
        "email": result.get("generated_email", ""),
        "thread_subject": result.get("selected_thread", {}).get("subject", ""),
        "thread_email_count": result.get("selected_thread", {}).get("email_count", 0),
        "intent": result.get("intent", "reply"),
    }


def generate_new_email(
    email_address: str,
    email_goal: str,
    tone: str = "professional",
) -> Dict:
    """Generate a brand-new email from scratch (no thread context)."""
    print(f"\n📝 Generating new email | To: {email_address} | Goal: {email_goal[:60]}")

    system_msg = (
        f"You are a professional email writer.\n"
        f"TONE: {tone}\n\n"
        "RULES:\n"
        f"- Write in a {tone} tone.\n"
        "- Do NOT invent personal info, company details, or facts.\n"
        "- Sound human and natural.\n"
        "- If the goal specifies a word count, match it (±5 words).\n"
        "- Otherwise aim for 100–160 words.\n\n"
        "FORMAT:\nSubject: [subject line]\n\n[email body]"
    )
    user_msg = (
        f"Write a new email to {email_address}.\n"
        f"Goal: {email_goal}\n"
        f"Tone: {tone}\n\n"
        "Write the complete email (Subject + Body):"
    )

    try:
        response = llm.invoke([SystemMessage(content=system_msg), HumanMessage(content=user_msg)])
        raw = response.content.partition("</reasoning>")[2].strip() or response.content.strip()
        subject, email_body = _parse_subject_body(raw)
        print(f"✅ New email generated | Subject: {subject}")
        return {"success": True, "subject": subject, "email": email_body, "is_new_email": True, "intent": "new"}
    except Exception as e:
        print(f"❌ Generation error: {e}")
        return {"success": False, "error": str(e), "is_new_email": True, "intent": "new"}
