"""BYOC job spec (U8, docs/architecture/U8-byoc-design.md). Strict: unknown fields are refused.

There is deliberately no field for privileged mode, host mounts, devices, capabilities, host namespaces or the
Docker socket. What cannot be asked for cannot be granted.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.job_spec_v2 import BillingHints, InputRef, ResourceRequest

IMAGE_DIGEST_RE = re.compile(r"^(?P<ref>[a-z0-9][a-z0-9.\-]{0,126}(?::[0-9]{1,5})?(?:/[a-z0-9][a-z0-9._\-]{0,127}){1,8})@(?P<digest>sha256:[0-9a-f]{64})$")
_NAME_RE = r"^[a-z][a-z0-9_]{0,31}$"
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_EGRESS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}:[0-9]{1,5}$")
MAX_ARGV_ITEMS = 32
MAX_ARG_CHARS = 512
MAX_ARGV_TOTAL = 8192
MAX_ENV_ITEMS = 32
MAX_ENV_VALUE = 1024
MAX_EGRESS = 8


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_argv(items: list[str]) -> list[str]:
    if len(items) > MAX_ARGV_ITEMS:
        raise ValueError(f"at most {MAX_ARGV_ITEMS} items")
    total = 0
    for it in items:
        if len(it) > MAX_ARG_CHARS:
            raise ValueError(f"each item at most {MAX_ARG_CHARS} characters")
        if "\x00" in it:
            raise ValueError("NUL is not allowed")
        total += len(it)
    if total > MAX_ARGV_TOTAL:
        raise ValueError(f"total length at most {MAX_ARGV_TOTAL}")
    return items


class ByocOutput(_Strict):
    name: str = Field(pattern=_NAME_RE)
    path: str = Field(min_length=1, max_length=255)  # relative to /out

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if v.startswith("/") or ".." in v.split("/") or "\x00" in v or "\\" in v or "//" in v:
            raise ValueError("path must be relative, without '..'")
        return v


class ByocNetwork(_Strict):
    egress: list[str] = Field(default_factory=list, max_length=MAX_EGRESS)

    @field_validator("egress")
    @classmethod
    def _egress(cls, v: list[str]) -> list[str]:
        # Same rules as a v1 job's network_policy (B5): public DNS names only, no IP literal / local or internal name.
        # (`$` in _EGRESS_RE would also have accepted a trailing newline; fullmatch inside the validator does not.)
        from app.services.network_policy import NetworkPolicyError, validate_egress_entry

        out: set[str] = set()
        for e in v:
            if not isinstance(e, str) or not _EGRESS_RE.fullmatch(e):
                raise ValueError("egress entries must be exact host:port")
            try:
                out.add(validate_egress_entry(e))
            except NetworkPolicyError as exc:
                raise ValueError(str(exc)) from exc
        return sorted(out)


class ByocSpec(_Strict):
    version: Literal[2]
    mode: Literal["byoc"]
    image: str = Field(max_length=300)
    command: list[str] = Field(default_factory=list)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict, max_length=MAX_ENV_ITEMS)
    isolation: Literal["gvisor", "microvm"] = "gvisor"
    inputs: list[InputRef] = Field(default_factory=list, max_length=32)
    outputs: list[ByocOutput] = Field(default_factory=list, max_length=32)
    resources: ResourceRequest = Field(default_factory=ResourceRequest)
    network: ByocNetwork = Field(default_factory=ByocNetwork)
    max_runtime_seconds: int | None = Field(default=None, ge=1, le=10**7)
    billing: BillingHints = Field(default_factory=BillingHints)

    @field_validator("image")
    @classmethod
    def _image(cls, v: str) -> str:
        if not IMAGE_DIGEST_RE.fullmatch(v):
            raise ValueError("image must be <registry>/<path>@sha256:<64 hex digits> (a tag is not accepted)")
        return v

    @field_validator("command", "args")
    @classmethod
    def _argv(cls, v: list[str]) -> list[str]:
        return _check_argv(v)

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        for k, val in v.items():
            if not _ENV_NAME_RE.fullmatch(k):
                raise ValueError(f"bad environment name {k!r}")
            if len(val) > MAX_ENV_VALUE or "\x00" in val or "\n" in val or "\r" in val:
                raise ValueError(f"bad environment value for {k}")
        return v

    @property
    def digest(self) -> str:
        return IMAGE_DIGEST_RE.match(self.image).group("digest")  # type: ignore[union-attr]

    @property
    def registry_path(self) -> str:
        return IMAGE_DIGEST_RE.match(self.image).group("ref")  # type: ignore[union-attr]
