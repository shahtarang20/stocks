"""Auth-related request/response schemas."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)


class RegisterResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    # Returned only in dev/test so flows are testable without real email.
    email_verification_token: str | None = None


class VerifyEmailRequest(BaseModel):
    token: str


class MessageResponse(BaseModel):
    message: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetRequestResponse(BaseModel):
    message: str
    # Returned only in dev/test.
    password_reset_token: str | None = None


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=256)


class CurrentUser(BaseModel):
    id: uuid.UUID
    email: str
    email_verified: bool
    status: str
    roles: list[str]


class SessionInfo(BaseModel):
    id: uuid.UUID
    created_at: dt.datetime
    expires_at: dt.datetime
    revoked_at: dt.datetime | None
