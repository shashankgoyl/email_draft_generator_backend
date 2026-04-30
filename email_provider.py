"""Email Provider Module with Conversation Threading
Supports: Gmail (Outlook can be added later)

OAuth flow (no browser on server):
  1. Call get_gmail_auth_url() → returns URL for user to open in browser
  2. Google redirects to OAUTH_REDIRECT_URI with code + state
  3. Call complete_gmail_auth(code, state) → exchanges code for token using PKCE verifier

PKCE fix: code_verifier is generated during auth URL creation and stored in
_oauth_state_store keyed by `state`. The callback retrieves it by state to
complete the token exchange — this prevents the 'Missing code verifier' error.
"""

import os
import base64
import hashlib
import secrets
import re
from typing import List, Dict, Optional
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

TOKEN_PATH         = os.getenv("GOOGLE_TOKEN_PATH",       "credentials/token.json")
CREDENTIALS_PATH   = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials/credentials.json")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI",      "http://localhost:8000/gmail/auth-callback")

# In-memory store: state → code_verifier
# Populated in get_gmail_auth_url(), consumed in complete_gmail_auth()
_oauth_state_store: dict = {}


# ── STARTUP: decode credentials.json from env var if file doesn't exist ──────
# This handles Render/cloud where you can't commit credentials.json
_creds_b64 = os.getenv("GOOGLE_CREDENTIALS_BASE64")
if _creds_b64 and not os.path.exists(CREDENTIALS_PATH):
    try:
        os.makedirs(os.path.dirname(CREDENTIALS_PATH), exist_ok=True)
        with open(CREDENTIALS_PATH, "w") as _f:
            _f.write(base64.b64decode(_creds_b64).decode("utf-8"))
        print(f"✅ credentials.json decoded from GOOGLE_CREDENTIALS_BASE64 → {CREDENTIALS_PATH}")
    except Exception as _e:
        print(f"❌ Failed to decode credentials from env var: {_e}")


# ── GMAIL OAUTH ───────────────────────────────────────────────────────────────

def get_gmail_auth_url() -> str:
    """Generate the Google OAuth authorisation URL with PKCE.

    Generates a code_verifier + code_challenge pair (PKCE / S256).
    The verifier is stored in _oauth_state_store keyed by the OAuth state
    parameter so complete_gmail_auth() can retrieve it.

    Returns:
        The authorisation URL the user must open in their browser.
    """
    from google_auth_oauthlib.flow import Flow

    if not os.path.exists(CREDENTIALS_PATH):
        raise FileNotFoundError(
            f"credentials.json not found at {CREDENTIALS_PATH}. "
            "Set GOOGLE_CREDENTIALS_BASE64 env var or place the file manually."
        )

    # PKCE — generate verifier and challenge
    code_verifier  = secrets.token_urlsafe(96)   # 128-char URL-safe string
    code_challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )

    flow = Flow.from_client_secrets_file(
        CREDENTIALS_PATH,
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
    )

    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        code_challenge=code_challenge,
        code_challenge_method="S256",
    )

    # Store verifier so the callback can use it
    _oauth_state_store[state] = code_verifier
    print(f"✅ Gmail auth URL generated | state prefix: {state[:8]}…")

    return auth_url


def complete_gmail_auth(code: str, state: str = None) -> bool:
    """Exchange the authorisation code for a token and save token.json.

    Uses the PKCE code_verifier stored during get_gmail_auth_url().
    If state is provided, retrieves the matching verifier from the store.
    Falls back to no-verifier exchange if state is missing (manual flow).

    Args:
        code:  The authorisation code from Google's redirect.
        state: The OAuth state parameter from Google's redirect.

    Returns:
        True if token was saved successfully.
    """
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(
        CREDENTIALS_PATH,
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
        state=state,
    )

    # Retrieve and remove the verifier for this state
    code_verifier = _oauth_state_store.pop(state, None) if state else None

    if code_verifier:
        print(f"✅ PKCE verifier found for state {state[:8]}… — using it")
        flow.fetch_token(code=code, code_verifier=code_verifier)
    else:
        print("⚠️  No PKCE verifier found — attempting exchange without it")
        flow.fetch_token(code=code)

    creds = flow.credentials
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())

    print(f"✅ Gmail token saved → {TOKEN_PATH}")
    return True


# ── GMAIL SERVICE ─────────────────────────────────────────────────────────────

def get_gmail_service():
    """Initialise Gmail API service using the stored token.json.

    If the token is missing or expired (and can't be refreshed), raises
    RuntimeError with instructions to redo the OAuth flow via the API."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = None

    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
            print("✅ Gmail token refreshed")
        else:
            raise RuntimeError(
                "Gmail is not authenticated. "
                "Call GET /gmail/auth-url to start the OAuth flow, "
                "open the URL in your browser, and approve access."
            )

    return build('gmail', 'v1', credentials=creds)


# ── EMAIL FETCHING ────────────────────────────────────────────────────────────

def fetch_gmail_emails(email_address: str, max_results: int = 100) -> List[Dict]:
    """Fetch emails sent to or from a specific email address.

    Args:
        email_address: Email address to search for.
        max_results:   Maximum number of emails to fetch.

    Returns:
        List of email dicts with subject, sender, date, body, thread_id.
    """
    try:
        service = get_gmail_service()
        query   = f'(from:{email_address} OR to:{email_address})'

        results  = service.users().messages().list(
            userId='me', q=query, maxResults=max_results
        ).execute()
        messages = results.get('messages', [])

        if not messages:
            print(f"⚠️  No emails found for {email_address}")
            return []

        emails = []
        for msg_ref in messages:
            email_data = extract_email_content(service, msg_ref['id'])
            if email_data:
                email_data['thread_id'] = msg_ref.get('threadId', msg_ref['id'])
                emails.append(email_data)

        print(f"✅ Fetched {len(emails)} emails for {email_address}")
        return emails

    except Exception as error:
        print(f'❌ Gmail fetch error: {error}')
        return []


def extract_email_content(service, message_id: str) -> Optional[Dict]:
    """Extract content from a single Gmail message."""
    try:
        message = service.users().messages().get(
            userId='me', id=message_id, format='full'
        ).execute()

        headers = message['payload'].get('headers', [])

        subject          = get_header(headers, 'Subject')
        sender           = get_header(headers, 'From')
        date             = get_header(headers, 'Date')
        to               = get_header(headers, 'To')
        message_id_hdr   = get_header(headers, 'Message-ID')
        body             = get_message_body(message['payload'])
        timestamp        = parse_email_date(date)

        return {
            'id':         message_id,
            'subject':    subject,
            'from':       sender,
            'to':         to,
            'date':       date,
            'timestamp':  timestamp,
            'body':       body[:2000],
            'snippet':    message.get('snippet', ''),
            'message_id': message_id_hdr,
        }
    except Exception as e:
        print(f'⚠️  Error fetching email {message_id}: {e}')
        return None


def get_header(headers: List[Dict], name: str) -> str:
    for header in headers:
        if header['name'].lower() == name.lower():
            return header['value']
    return ''


def get_message_body(payload: Dict) -> str:
    if 'body' in payload and payload['body'].get('data'):
        return base64.urlsafe_b64decode(
            payload['body']['data']
        ).decode('utf-8', errors='ignore')

    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/plain' and 'data' in part['body']:
                return base64.urlsafe_b64decode(
                    part['body']['data']
                ).decode('utf-8', errors='ignore')
    return ''


def parse_email_date(date_str: str) -> int:
    try:
        from email.utils import parsedate_to_datetime
        return int(parsedate_to_datetime(date_str).timestamp())
    except Exception:
        return 0


# ── CONVERSATION THREADING ────────────────────────────────────────────────────

def group_emails_into_threads(emails: List[Dict]) -> List[Dict]:
    """Group a flat list of emails into conversation threads."""
    if not emails:
        return []

    threads_dict = defaultdict(list)
    for email in emails:
        threads_dict[email.get('thread_id', email['id'])].append(email)

    threads = []
    for thread_id, thread_emails in threads_dict.items():
        thread_emails.sort(key=lambda x: x.get('timestamp', 0))
        first = thread_emails[0]
        last  = thread_emails[-1]

        threads.append({
            'thread_id':       thread_id,
            'subject':         clean_subject(first['subject']),
            'email_count':     len(thread_emails),
            'participants':    get_unique_participants(thread_emails),
            'first_date':      first['date'],
            'last_date':       last['date'],
            'first_timestamp': first.get('timestamp', 0),
            'last_timestamp':  last.get('timestamp', 0),
            'snippet':         last['snippet'],
            'emails':          thread_emails,
        })

    threads.sort(key=lambda x: x['last_timestamp'], reverse=True)
    print(f"📊 Grouped {len(emails)} emails into {len(threads)} threads")
    return threads


def clean_subject(subject: str) -> str:
    prefixes = ['Re:', 'RE:', 'Fwd:', 'FWD:', 'Fw:']
    cleaned  = subject.strip()
    while any(cleaned.startswith(p) for p in prefixes):
        for p in prefixes:
            if cleaned.startswith(p):
                cleaned = cleaned[len(p):].strip()
                break
    return cleaned or subject


def get_unique_participants(emails: List[Dict]) -> List[str]:
    participants = set()
    for email in emails:
        from_addr = extract_email_address(email['from'])
        if from_addr:
            participants.add(from_addr)
        for to_part in email['to'].split(','):
            addr = extract_email_address(to_part.strip())
            if addr:
                participants.add(addr)
    return sorted(participants)


def extract_email_address(email_str: str) -> Optional[str]:
    match = re.search(r'<(.+?)>', email_str)
    if match:
        return match.group(1)
    if '@' in email_str:
        return email_str.strip()
    return None


# ── UNIFIED PROVIDER API ──────────────────────────────────────────────────────

def fetch_emails(provider: str, email_address: str, max_results: int = 100) -> List[Dict]:
    if provider.lower() == 'gmail':
        return fetch_gmail_emails(email_address, max_results)
    raise ValueError(f"Unsupported provider: {provider}")


def fetch_threads(provider: str, email_address: str, max_results: int = 100) -> List[Dict]:
    emails  = fetch_emails(provider, email_address, max_results)
    threads = group_emails_into_threads(emails)
    return threads


def format_thread_for_context(thread: Dict, selected_email_index: Optional[int] = None) -> str:
    """Format a thread as a string for LLM context."""
    ctx  = f"=== CONVERSATION THREAD ===\n"
    ctx += f"Subject: {thread['subject']}\n"
    ctx += f"Participants: {', '.join(thread['participants'])}\n"
    ctx += f"Total Emails: {thread['email_count']}\n\n"
    ctx += "--- EMAIL HISTORY ---\n\n"

    for i, email in enumerate(thread['emails']):
        is_selected = (selected_email_index is not None and i == selected_email_index)
        ctx += f"Email #{i + 1}"
        if is_selected:
            ctx += " [SELECTED EMAIL — FOCUS CONTEXT]"
        ctx += f":\nFrom: {email['from']}\nTo: {email['to']}\nDate: {email['date']}\n"
        ctx += f"Body: {email['body'][:500]}...\n"
        ctx += "-" * 60 + "\n\n"

    return ctx
