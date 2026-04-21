"""Email Provider Module with Conversation Threading
Supports: Gmail (Outlook can be added later)

CHANGE (Docker OAuth fix):
  get_gmail_service() no longer tries to open a browser (which fails in Docker).
  Instead:
    - Call get_gmail_auth_url() → returns the URL the user must open in their browser.
    - After the user pastes the authorisation code, call complete_gmail_auth(code).
    - Both steps are exposed as API endpoints (/gmail/auth-url, /gmail/auth-callback)."""

import os
import base64
import re
from typing import List, Dict, Optional
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

# Paths can be overridden via env vars (useful in Docker)
TOKEN_PATH       = os.getenv("GOOGLE_TOKEN_PATH",       "credentials/token.json")
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials/credentials.json")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI",    "http://localhost:8000/gmail/auth-callback")


# GMAIL OAUTH HELPERS  (CHANGED: web flow instead of local-server flow)
def get_gmail_auth_url() -> str:
    """Generate the Google OAuth authorisation URL.

    The user visits this URL in their browser, grants access, and Google
    redirects them to OAUTH_REDIRECT_URI with an authorisation code.
    The /gmail/auth-callback endpoint then exchanges the code for a token.

    Returns:
        The authorisation URL the user must open."""
    from google_auth_oauthlib.flow import Flow

    if not os.path.exists(CREDENTIALS_PATH):
        raise FileNotFoundError(
            f"Gmail credentials.json not found at {CREDENTIALS_PATH}. "
            "Mount it into the container via the credentials/ volume."
        )

    flow = Flow.from_client_secrets_file(
        CREDENTIALS_PATH,
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return auth_url

def complete_gmail_auth(code: str) -> bool:
    """CHANGE: Exchange the authorisation code for credentials and save token.json.

    Args:
        code: The authorisation code returned by Google after user consent.

    Returns:
        True if the token was saved successfully."""
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(
        CREDENTIALS_PATH,
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials

    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w") as token_file:
        token_file.write(creds.to_json())

    print(f"✅ Gmail token saved to {TOKEN_PATH}")
    return True

# GMAIL PROVIDER
def get_gmail_service():
    """Initialize Gmail API service using the stored token.json.

    CHANGE: Removed the run_local_server() call that tried to open a browser.
    If the token is missing or invalid, raises RuntimeError with instructions
    to call GET /gmail/auth-url and complete the OAuth flow via the API."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = None

    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Token exists but expired — refresh it silently
            creds.refresh(Request())
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
            print("✅ Gmail token refreshed")
        else:
            # CHANGE: raise instead of launching a local browser
            raise RuntimeError(
                "Gmail is not authenticated. "
                "Call GET /gmail/auth-url to get the authorisation URL, "
                "open it in your browser, then visit the redirect URL shown "
                "to complete authentication."
            )

    return build('gmail', 'v1', credentials=creds)

def fetch_gmail_emails(email_address: str, max_results: int = 100) -> List[Dict]:
    """Fetch emails sent to or from a specific email address

    Args:
        email_address: Email address to search for
        max_results: Maximum number of emails to fetch (50-100)

    Returns:
        List of email dictionaries with subject, sender, date, body, thread_id"""
    try:
        service = get_gmail_service()

        # Search for emails from OR to this address
        query = f'(from:{email_address} OR to:{email_address})'

        results = service.users().messages().list(
            userId='me',
            q=query,
            maxResults=max_results
        ).execute()

        messages = results.get('messages', [])

        if not messages:
            print(f"⚠️ No emails found for {email_address}")
            return []

        emails = []
        for msg_ref in messages:
            email_data = extract_email_content(service, msg_ref['id'])
            if email_data:
                # Add thread_id from the message reference
                email_data['thread_id'] = msg_ref.get('threadId', msg_ref['id'])
                emails.append(email_data)

        print(f"✅ Fetched {len(emails)} emails for {email_address}")
        return emails

    except Exception as error:
        print(f'❌ Gmail fetch error: {error}')
        return []

def extract_email_content(service, message_id: str) -> Optional[Dict]:
    """Extract content from a Gmail message"""
    try:
        message = service.users().messages().get(
            userId='me',
            id=message_id,
            format='full'
        ).execute()

        headers = message['payload'].get('headers', [])

        # Extract headers
        subject = get_header(headers, 'Subject')
        sender = get_header(headers, 'From')
        date = get_header(headers, 'Date')
        to = get_header(headers, 'To')
        message_id_header = get_header(headers, 'Message-ID')

        # Extract body
        body = get_message_body(message['payload'])

        # Parse date to timestamp for sorting
        timestamp = parse_email_date(date)

        return {
            'id': message_id,
            'subject': subject,
            'from': sender,
            'to': to,
            'date': date,
            'timestamp': timestamp,
            'body': body[:2000],  # Limit to 2000 chars
            'snippet': message.get('snippet', ''),
            'message_id': message_id_header
        }

    except Exception as e:
        print(f'⚠️ Error fetching email {message_id}: {e}')
        return None

def get_header(headers: List[Dict], name: str) -> str:
    """Get header value by name"""
    for header in headers:
        if header['name'].lower() == name.lower():
            return header['value']
    return ''

def get_message_body(payload: Dict) -> str:
    """Extract message body from payload"""
    if 'body' in payload and payload['body'].get('data'):
        return base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')

    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/plain' and 'data' in part['body']:
                return base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')

    return ''

def parse_email_date(date_str: str) -> int:
    """Parse email date string to Unix timestamp"""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        return int(dt.timestamp())
    except:
        return 0

# CONVERSATION THREADING
def group_emails_into_threads(emails: List[Dict]) -> List[Dict]:
    """Group emails into conversation threads

    Args:
        emails: List of email dictionaries

    Returns:
        List of thread dictionaries with emails sorted by date"""
    if not emails:
        return []

    # Group by thread_id
    threads_dict = defaultdict(list)

    for email in emails:
        thread_id = email.get('thread_id', email['id'])
        threads_dict[thread_id].append(email)

    # Convert to list of threads
    threads = []
    for thread_id, thread_emails in threads_dict.items():
        # Sort emails in thread by timestamp
        thread_emails.sort(key=lambda x: x.get('timestamp', 0))

        # Get thread metadata
        first_email = thread_emails[0]
        last_email = thread_emails[-1]

        # Clean subject (remove Re:, Fwd:, etc.)
        subject = clean_subject(first_email['subject'])

        thread = {
            'thread_id': thread_id,
            'subject': subject,
            'email_count': len(thread_emails),
            'participants': get_unique_participants(thread_emails),
            'first_date': first_email['date'],
            'last_date': last_email['date'],
            'first_timestamp': first_email.get('timestamp', 0),
            'last_timestamp': last_email.get('timestamp', 0),
            'snippet': last_email['snippet'],
            'emails': thread_emails
        }

        threads.append(thread)

    # Sort threads by last activity (most recent first)
    threads.sort(key=lambda x: x['last_timestamp'], reverse=True)

    print(f"📊 Grouped {len(emails)} emails into {len(threads)} conversation threads")

    return threads

def clean_subject(subject: str) -> str:
    """Remove Re:, Fwd:, etc. from subject"""
    prefixes = ['Re:', 'RE:', 'Fwd:', 'FWD:', 'Fw:']
    cleaned = subject.strip()

    while any(cleaned.startswith(prefix) for prefix in prefixes):
        for prefix in prefixes:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                break

    return cleaned or subject

def get_unique_participants(emails: List[Dict]) -> List[str]:
    """Extract unique email addresses from conversation"""
    participants = set()

    for email in emails:
        # Extract email from "Name <email@domain.com>" format
        from_email = extract_email_address(email['from'])
        to_emails = email['to'].split(',')

        if from_email:
            participants.add(from_email)

        for to_email in to_emails:
            addr = extract_email_address(to_email.strip())
            if addr:
                participants.add(addr)

    return sorted(list(participants))

def extract_email_address(email_str: str) -> Optional[str]:
    """Extract email address from 'Name <email@domain.com>' format"""
    import re
    match = re.search(r'<(.+?)>', email_str)
    if match:
        return match.group(1)
    # If no angle brackets, assume it's just the email
    if '@' in email_str:
        return email_str.strip()
    return None

# UNIFIED EMAIL PROVIDER
def fetch_emails(provider: str, email_address: str, max_results: int = 100) -> List[Dict]:
    """Unified function to fetch emails from any provider

    Args:
        provider: 'gmail' or 'outlook'
        email_address: Email address to search for
        max_results: Maximum number of emails to fetch (50-100)

    Returns:
        List of email dictionaries"""
    if provider.lower() == 'gmail':
        return fetch_gmail_emails(email_address, max_results)
    else:
        raise ValueError(f"Unsupported email provider: {provider}")

def fetch_threads(provider: str, email_address: str, max_results: int = 100) -> List[Dict]:
    """    Fetch emails and group them into conversation threads

    Args:
        provider: 'gmail' or 'outlook'
        email_address: Email address to search for
        max_results: Maximum number of emails to fetch

    Returns:
        List of thread dictionaries"""
    emails = fetch_emails(provider, email_address, max_results)
    threads = group_emails_into_threads(emails)
    return threads

def format_thread_for_context(thread: Dict, selected_email_index: Optional[int] = None) -> str:
    """ Format a conversation thread for LLM context

    Args:
        thread: Thread dictionary
        selected_email_index: Index of specific email to focus on (0-based)

    Returns:
        Formatted string for LLM context """
    context = f"=== CONVERSATION THREAD ===\n"
    context += f"Subject: {thread['subject']}\n"
    context += f"Participants: {', '.join(thread['participants'])}\n"
    context += f"Total Emails: {thread['email_count']}\n\n"

    context += "--- EMAIL HISTORY ---\n\n"

    for i, email in enumerate(thread['emails']):
        is_selected = (selected_email_index is not None and i == selected_email_index)

        context += f"Email #{i + 1}"
        if is_selected:
            context += " [SELECTED EMAIL - FOCUS CONTEXT]"
        context += ":\n"
        context += f"From: {email['from']}\n"
        context += f"To: {email['to']}\n"
        context += f"Date: {email['date']}\n"
        context += f"Body: {email['body'][:500]}...\n"
        context += "-" * 60 + "\n\n"

    return context