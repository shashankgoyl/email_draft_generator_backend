"""Email Provider Module with Conversation Threading

PKCE verifier stored in SQLite — survives Render server restarts.
"""

import os, base64, hashlib, secrets, sqlite3, re
from typing import List, Dict, Optional
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

SCOPES             = ['https://www.googleapis.com/auth/gmail.readonly']
TOKEN_PATH         = os.getenv("GOOGLE_TOKEN_PATH",       "credentials/token.json")
CREDENTIALS_PATH   = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials/credentials.json")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI",      "http://localhost:8000/gmail/auth-callback")
DB_PATH            = os.getenv("DB_PATH", "./email_generator.db")

# Decode credentials.json from env var at startup
_creds_b64 = os.getenv("GOOGLE_CREDENTIALS_BASE64")
if _creds_b64 and not os.path.exists(CREDENTIALS_PATH):
    try:
        os.makedirs(os.path.dirname(CREDENTIALS_PATH), exist_ok=True)
        with open(CREDENTIALS_PATH, "w") as _f:
            _f.write(base64.b64decode(_creds_b64).decode("utf-8"))
        print(f"credentials.json decoded from env var")
    except Exception as _e:
        print(f"Failed to decode credentials: {_e}")

# PKCE store in SQLite
def _get_oauth_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS oauth_state (
        state TEXT PRIMARY KEY, code_verifier TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    conn.commit()
    return conn

def _store_verifier(state: str, code_verifier: str):
    conn = _get_oauth_conn()
    try:
        conn.execute("INSERT OR REPLACE INTO oauth_state VALUES (?,?,?)",
                     (state, code_verifier, datetime.utcnow().isoformat()))
        conn.commit()
        print(f"PKCE verifier stored for state: {state[:8]}")
    finally:
        conn.close()

def _retrieve_verifier(state: str) -> Optional[str]:
    conn = _get_oauth_conn()
    try:
        row = conn.execute("SELECT code_verifier FROM oauth_state WHERE state=?", (state,)).fetchone()
        if row:
            conn.execute("DELETE FROM oauth_state WHERE state=?", (state,))
            conn.commit()
            print(f"PKCE verifier retrieved for state: {state[:8]}")
            return row[0]
        print(f"No PKCE verifier found for state: {state[:8]}")
        return None
    finally:
        conn.close()

# Gmail OAuth
def get_gmail_auth_url() -> str:
    from google_auth_oauthlib.flow import Flow
    if not os.path.exists(CREDENTIALS_PATH):
        raise FileNotFoundError(f"credentials.json not found at {CREDENTIALS_PATH}. Set GOOGLE_CREDENTIALS_BASE64 env var.")

    code_verifier  = secrets.token_urlsafe(96)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")

    flow = Flow.from_client_secrets_file(CREDENTIALS_PATH, scopes=SCOPES, redirect_uri=OAUTH_REDIRECT_URI)
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent",
        code_challenge=code_challenge, code_challenge_method="S256",
    )
    _store_verifier(state, code_verifier)
    print(f"Auth URL generated | state: {state[:8]}")
    return auth_url

def complete_gmail_auth(code: str, state: str = None) -> bool:
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_secrets_file(CREDENTIALS_PATH, scopes=SCOPES,
                                         redirect_uri=OAUTH_REDIRECT_URI, state=state)
    code_verifier = _retrieve_verifier(state) if state else None
    if code_verifier:
        flow.fetch_token(code=code, code_verifier=code_verifier)
    else:
        print("No verifier — trying without PKCE")
        flow.fetch_token(code=code)
    creds = flow.credentials
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())
    print(f"Gmail token saved to {TOKEN_PATH}")
    return True

def get_gmail_service():
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
        else:
            raise RuntimeError("Gmail not authenticated. Call GET /gmail/auth-url to start OAuth flow.")
    return build('gmail', 'v1', credentials=creds)

def fetch_gmail_emails(email_address: str, max_results: int = 100) -> List[Dict]:
    try:
        service  = get_gmail_service()
        query    = f'(from:{email_address} OR to:{email_address})'
        results  = service.users().messages().list(userId='me', q=query, maxResults=max_results).execute()
        messages = results.get('messages', [])
        if not messages:
            return []
        emails = []
        for msg_ref in messages:
            data = extract_email_content(service, msg_ref['id'])
            if data:
                data['thread_id'] = msg_ref.get('threadId', msg_ref['id'])
                emails.append(data)
        print(f"Fetched {len(emails)} emails for {email_address}")
        return emails
    except Exception as e:
        print(f'Gmail fetch error: {e}')
        return []

def extract_email_content(service, message_id: str) -> Optional[Dict]:
    try:
        msg     = service.users().messages().get(userId='me', id=message_id, format='full').execute()
        headers = msg['payload'].get('headers', [])
        return {
            'id': message_id, 'subject': get_header(headers,'Subject'),
            'from': get_header(headers,'From'), 'to': get_header(headers,'To'),
            'date': get_header(headers,'Date'),
            'timestamp': parse_email_date(get_header(headers,'Date')),
            'body': get_message_body(msg['payload'])[:2000],
            'snippet': msg.get('snippet',''), 'message_id': get_header(headers,'Message-ID'),
        }
    except Exception as e:
        print(f'Error fetching email {message_id}: {e}')
        return None

def get_header(headers, name):
    for h in headers:
        if h['name'].lower() == name.lower(): return h['value']
    return ''

def get_message_body(payload):
    if 'body' in payload and payload['body'].get('data'):
        return base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')
    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/plain' and 'data' in part['body']:
                return base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
    return ''

def parse_email_date(date_str):
    try:
        from email.utils import parsedate_to_datetime
        return int(parsedate_to_datetime(date_str).timestamp())
    except: return 0

def group_emails_into_threads(emails):
    if not emails: return []
    threads_dict = defaultdict(list)
    for e in emails: threads_dict[e.get('thread_id', e['id'])].append(e)
    threads = []
    for tid, es in threads_dict.items():
        es.sort(key=lambda x: x.get('timestamp',0))
        threads.append({
            'thread_id': tid, 'subject': clean_subject(es[0]['subject']),
            'email_count': len(es), 'participants': get_unique_participants(es),
            'first_date': es[0]['date'], 'last_date': es[-1]['date'],
            'first_timestamp': es[0].get('timestamp',0), 'last_timestamp': es[-1].get('timestamp',0),
            'snippet': es[-1]['snippet'], 'emails': es,
        })
    threads.sort(key=lambda x: x['last_timestamp'], reverse=True)
    return threads

def clean_subject(subject):
    prefixes = ['Re:', 'RE:', 'Fwd:', 'FWD:', 'Fw:']
    s = subject.strip()
    while any(s.startswith(p) for p in prefixes):
        for p in prefixes:
            if s.startswith(p): s = s[len(p):].strip(); break
    return s or subject

def get_unique_participants(emails):
    p = set()
    for e in emails:
        a = extract_email_address(e['from'])
        if a: p.add(a)
        for part in e['to'].split(','):
            a2 = extract_email_address(part.strip())
            if a2: p.add(a2)
    return sorted(p)

def extract_email_address(s):
    m = re.search(r'<(.+?)>', s)
    if m: return m.group(1)
    if '@' in s: return s.strip()
    return None

def fetch_emails(provider, email_address, max_results=100):
    if provider.lower() == 'gmail': return fetch_gmail_emails(email_address, max_results)
    raise ValueError(f"Unsupported provider: {provider}")

def fetch_threads(provider, email_address, max_results=100):
    return group_emails_into_threads(fetch_emails(provider, email_address, max_results))

def format_thread_for_context(thread, selected_email_index=None):
    ctx = f"=== CONVERSATION THREAD ===\nSubject: {thread['subject']}\n"
    ctx += f"Participants: {', '.join(thread['participants'])}\nTotal Emails: {thread['email_count']}\n\n"
    ctx += "--- EMAIL HISTORY ---\n\n"
    for i, email in enumerate(thread['emails']):
        ctx += f"Email #{i+1}"
        if selected_email_index is not None and i == selected_email_index:
            ctx += " [SELECTED EMAIL]"
        ctx += f":\nFrom: {email['from']}\nTo: {email['to']}\nDate: {email['date']}\n"
        ctx += f"Body: {email['body'][:500]}...\n" + "-"*60 + "\n\n"
    return ctx
