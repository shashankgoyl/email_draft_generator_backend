"""schemas.py — All Pydantic request/response models.
Centralised here so api.py, admin_router.py and gmail_router.py stay clean."""
from pydantic import BaseModel, Field
from typing import Optional, List

# AUTH
class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

# ADMIN USER MANAGEMENT
class AddUserRequest(BaseModel):
    email: str = Field(..., description="User's email address")
    password: str = Field(..., min_length=6, description="Password (min 6 characters)")

class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=6, description="New password (min 6 characters)")

# EMAIL FETCHING
class FetchThreadsRequest(BaseModel):
    email_addresses: str = Field(..., description="Comma-separated email address(es)")
    email_goal: Optional[str] = Field(None, description="Optional goal to filter relevant threads")
    provider: Optional[str] = Field("gmail", description="Email provider: gmail or outlook")
    max_emails: Optional[int] = Field(100, description="Max emails to fetch per address")

# EMAIL GENERATION
class GenerateEmailRequest(BaseModel):
    email_address: str = Field(..., description="Email address to generate for")
    thread_id: Optional[str] = Field(None, description="Thread ID — None means new email from scratch")
    selected_email_index: Optional[int] = Field(None, description="Index of email to focus on (0-based)")
    email_goal: Optional[str] = Field("", description="User's goal for the email")
    provider: Optional[str] = Field("gmail", description="Email provider")
    tone: Optional[str] = Field("professional", description="Email tone")
    max_emails: Optional[int] = Field(100, description="Max emails to fetch")

class GenerateMultipleEmailsRequest(BaseModel):
    email_addresses: str = Field(..., description="Comma-separated email addresses")
    email_goal: str = Field(..., description="Email goal/purpose")
    tone: Optional[str] = Field("professional", description="Email tone")
    provider: Optional[str] = Field("gmail", description="Email provider")
    max_emails: Optional[int] = Field(100, description="Max emails to fetch")

# SESSION UPDATE
class UpdateSessionRequest(BaseModel):
    subject: Optional[str] = Field(None, description="Updated subject")
    email_body: Optional[str] = Field(None, description="Updated email body")
    email_goal: Optional[str] = Field(None, description="Updated email goal")
    tone: Optional[str] = Field(None, description="Updated tone")

# RESPONSES
class AddressThreadsResponse(BaseModel):
    email_address: str
    threads: list = []
    total_emails: int = 0
    relevant_threads: Optional[list] = None
    has_context: bool = True

class MultiAddressThreadsResponse(BaseModel):
    success: bool
    addresses_data: List[AddressThreadsResponse] = []
    email_goal: Optional[str] = None
    total_addresses: int = 0
    message: Optional[str] = None

class EmailResponse(BaseModel):
    success: bool
    email_address: str = ""
    subject: str = ""
    email: str = ""
    email_file_url: Optional[str] = None
    thread_subject: Optional[str] = None
    thread_email_count: int = 0
    is_new_email: bool = False
    intent: Optional[str] = None
    session_id: Optional[str] = None
    message: Optional[str] = None

class MultiEmailResponse(BaseModel):
    success: bool
    emails: List[EmailResponse] = []
    total_generated: int = 0
    message: Optional[str] = None

class HistoryResponse(BaseModel):
    success: bool
    sessions: List[dict] = []
    total: int = 0
    message: Optional[str] = None

class StatsResponse(BaseModel):
    success: bool
    stats: dict = {}
    message: Optional[str] = None

class UpdateResponse(BaseModel):
    success: bool
    message: str