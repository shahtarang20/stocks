"""Enumerations used across models (stored as portable strings)."""

from __future__ import annotations

import enum


class UserStatus(str, enum.Enum):
    active = "active"
    suspended = "suspended"
    deleted = "deleted"


class Role(str, enum.Enum):
    guest = "guest"
    customer = "customer"
    provider = "provider"
    support_operator = "support_operator"
    security_operator = "security_operator"
    administrator = "administrator"
