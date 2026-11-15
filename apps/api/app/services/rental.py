"""Container rental helpers (docs/architecture/RENTAL-container-rental-design.md).

Pure rules (templates, registries, egress allowlist, relative paths) and the two database checks a rental request needs (the target
machine, and the input files). Money is NOT touched here: a rental is billed by the existing session settlement.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

TEMPLATE_FILE = Path(__file__).resolve().parent.parent / "data" / "rental_templates.json"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
IMAGE_RE = re.compile(r"^[a-zA-Z0-9][\w./:-]*@sha256:[0-9a-f]{64}$")
_PART_RE = re.compile(r"^[A-Za-z0-9._+=@ -]{1,100}$")
MAX_PATH_CHARS = 240
MAX_COMMAND_ARGS = 32
MAX_COMMAND_ARG_CHARS = 512
DEFAULT_MEMORY_MB = 16384
DEFAULT_CPUS = 4
MIN_MEMORY_MB = 1024
EXPORT_MIN_INTERVAL_SECONDS = 60


class RentalError(Exception):
    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.code = code
        self.status = status


# --------------------------------------------------------------------------- settings as lists
def _csv(value: object) -> list[str]:
    return [p.strip().lower() for p in str(value or "").split(",") if p.strip()]


def allowed_registries() -> set[str]:
    return set(_csv(getattr(settings, "RENTAL_ALLOWED_REGISTRIES", "")))


def egress_allowlist() -> set[str]:
    return set(_csv(getattr(settings, "RENTAL_EGRESS_ALLOWLIST", "")))


def rental_enabled() -> bool:
    return bool(getattr(settings, "CONTAINER_RENTAL_ENABLED", False))


# --------------------------------------------------------------------------- images
def registry_of(image: str) -> str:
    """The registry an image reference points at: the first path part when there is a '/' and that part looks like a host
    (contains '.' or ':', or is ``localhost``), else Docker Hub. Lower case."""
    name = (image or "").split("@", 1)[0]
    head, sep, _rest = name.partition("/")
    if sep and ("." in head or ":" in head or head.lower() == "localhost"):
        return head.lower()
    return "docker.io"


def registry_allowed(image: str) -> bool:
    reg = registry_of(image)
    if reg == "localhost" or reg.startswith("localhost:"):
        return False  # never, whatever the list says
    return reg in allowed_registries()


# --------------------------------------------------------------------------- templates
def _valid_template(raw: Any) -> dict | None:
    """One template entry, or None when it is malformed (a bad entry is dropped, never half-used)."""
    if not isinstance(raw, dict):
        return None
    slug = raw.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        return None
    if raw.get("mode") != "tcp":
        return None
    port = raw.get("tcp_port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return None
    command = raw.get("command")  # null or absent: the image's own ENTRYPOINT starts the app, no override is sent
    if command is not None and not (
        isinstance(command, list) and 1 <= len(command) <= MAX_COMMAND_ARGS
        and all(isinstance(c, str) and c and "\x00" not in c and len(c) <= MAX_COMMAND_ARG_CHARS for c in command)
    ):
        return None
    egress = raw.get("default_egress") or []
    if not (isinstance(egress, list) and all(isinstance(e, str) for e in egress)):
        return None
    digest = raw.get("digest")
    if digest is not None and not (isinstance(digest, str) and IMAGE_RE.fullmatch(digest)):
        return None  # a malformed digest is treated like a missing one: the template stays hidden
    libs = raw.get("libraries") or []
    if not (isinstance(libs, list) and all(isinstance(x, str) for x in libs)):
        return None
    return {
        "slug": slug,
        "name": str(raw.get("name") or slug)[:120],
        "description": str(raw.get("description") or "")[:600],
        "gpu_required": bool(raw.get("gpu_required", True)),
        "mode": "tcp",
        "tcp_port": port,
        "command": list(command) if command is not None else None,
        "default_egress": sorted(set(egress)),
        "libraries": [x[:80] for x in libs][:60],
        "digest": digest,
    }


@lru_cache(maxsize=8)
def _load_templates_file(path: str, mtime_ns: int) -> tuple[dict, ...]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    items = raw.get("templates") if isinstance(raw, dict) else raw  # {"templates": [...]} or a bare list
    if not isinstance(items, list):
        return ()
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        t = _valid_template(item)
        if t is not None and t["slug"] not in seen:
            seen.add(t["slug"])
            out.append(t)
    return tuple(out)


def all_templates() -> list[dict]:
    """Every well-formed template, including ones with no digest yet."""
    try:
        mtime = TEMPLATE_FILE.stat().st_mtime_ns
    except OSError:
        return []
    return [dict(t) for t in _load_templates_file(str(TEMPLATE_FILE), mtime)]


def available_templates() -> list[dict]:
    """Templates that can be rented now: they have a pinned digest on an allowed registry (checked on every call, because
    the allowed list is a setting)."""
    return [t for t in all_templates() if t["digest"] and registry_allowed(t["digest"])]


def get_template(slug: str) -> dict | None:
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        return None
    return next((t for t in available_templates() if t["slug"] == slug), None)


def public_template(t: dict) -> dict:
    """What the public list shows: never the digest."""
    return {k: t[k] for k in ("slug", "name", "description", "gpu_required", "mode", "default_egress", "libraries")}


# --------------------------------------------------------------------------- paths and commands
def clean_relpath(path: object, *, forbid_results: bool = False) -> str:
    """A clean relative POSIX path: no leading '/', no '..', no empty or '.' part, parts of at most 100 characters from a safe
    set, at most 240 characters in all. ``forbid_results`` also refuses a path that starts with ``results/`` (inputs may not
    be staged where the export looks)."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS or "\\" in path or "\x00" in path:
        raise RentalError("invalid_path")
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", "..") or not _PART_RE.fullmatch(part) or part != part.strip():
            raise RentalError("invalid_path")
    if forbid_results and parts[0] == "results":
        raise RentalError("invalid_path")
    return path


def clean_command(command: object) -> list[str] | None:
    if command is None:
        return None
    if not (isinstance(command, list) and 1 <= len(command) <= MAX_COMMAND_ARGS
            and all(isinstance(c, str) and c and "\x00" not in c and len(c) <= MAX_COMMAND_ARG_CHARS for c in command)):
        raise RentalError("invalid_command")
    return list(command)


def paths_conflict(paths: list[str]) -> bool:
    """True if two paths are equal or one is a directory prefix of the other (a file cannot also be a directory)."""
    seen = sorted(paths)
    for i, p in enumerate(seen):
        if i and (p == seen[i - 1] or p.startswith(seen[i - 1] + "/")):
            return True
    return False


def relay_misconfigured() -> bool:
    """True when the API runs several workers but the session relay is the in-memory one. A browser and a host can then land on
    DIFFERENT workers and never meet, so a rental would start and silently never connect. ``WEB_CONCURRENCY`` is set by the server
    entrypoint; unset (development, tests) means one worker."""
    try:
        workers = int(os.environ.get("WEB_CONCURRENCY") or 1)
    except ValueError:
        workers = 1
    backend = (getattr(settings, "SESSIONS_RELAY_BACKEND", "memory") or "memory").strip().lower()
    return workers > 1 and backend != "redis"


# --------------------------------------------------------------------------- database checks
async def check_target_machine(db: AsyncSession, machine_id: uuid.UUID, *, require_flag: bool, needs_egress: bool = False):
    """The machine a customer picked. Raises ``machine_not_available_for_rental`` unless it is usable now, and
    ``machine_cannot_filter_network`` when the rental asks for internet sites and this host has no network-filter proxy configured
    (it could not start the container without widening the network, so it is refused up front)."""
    from app.models.provider import ProviderMachine
    from app.services.capability_flags import host_capabilities
    from app.services.provider import blocked_provider_ids

    machine = await db.get(ProviderMachine, machine_id)
    if machine is None or getattr(machine, "is_banned", False) or getattr(machine, "status", "") == "deregistered":
        raise RentalError("machine_not_available_for_rental")
    if getattr(machine, "availability", "online") != "online":
        raise RentalError("machine_not_available_for_rental")
    if await blocked_provider_ids(db, {machine.provider_id}):
        raise RentalError("machine_not_available_for_rental")
    flags = host_capabilities(machine).flags
    if require_flag and "container_rental" not in flags:
        raise RentalError("machine_not_available_for_rental")
    if needs_egress and "egress_proxy" not in flags:
        raise RentalError("machine_cannot_filter_network")
    return machine


async def check_inputs(db: AsyncSession, customer_id: uuid.UUID, inputs: list) -> list[dict]:
    """Validate the requested input files and return the stored form (file id, path, size, hash)."""
    from app.models.data_plane import OBJECT_READY, DataObject
    from app.services import data_plane as dp

    if not inputs:
        return []
    if len(inputs) > int(getattr(settings, "RENTAL_MAX_INPUTS", 16)):
        raise RentalError("too_many_inputs")
    paths: list[str] = []
    out: list[dict] = []
    seen_ids: set[uuid.UUID] = set()
    for item in inputs:
        path = clean_relpath(item.path, forbid_results=True)
        paths.append(path)
        if item.file_id in seen_ids:
            raise RentalError("input_paths_conflict")  # the same file twice: ask for it once
        seen_ids.add(item.file_id)
    if paths_conflict(paths):
        raise RentalError("input_paths_conflict")
    try:
        dp.require_enabled()
    except dp.DataPlaneError as exc:
        raise RentalError(exc.code, exc.http_status) from exc
    for item, path in zip(inputs, paths):
        try:
            f = await dp.get_owned_file(db, customer_id, item.file_id)
        except dp.DataPlaneError:
            raise RentalError("input_file_not_found", 404) from None
        obj = await db.get(DataObject, f.object_id) if f.object_id else None
        if obj is None or obj.status != OBJECT_READY:
            raise RentalError("input_file_not_found", 404)
        if await dp._is_private_job_file(db, customer_id, f.name):  # private-tier results never go to a rental host
            raise RentalError("input_file_not_found", 404)
        out.append({"file_id": str(f.id), "path": path, "size_bytes": int(f.size_bytes), "sha256": f.sha256})
    return out


__all__ = [
    "RentalError", "allowed_registries", "egress_allowlist", "rental_enabled", "registry_of", "registry_allowed",
    "all_templates", "available_templates", "get_template", "public_template", "clean_relpath", "clean_command",
    "paths_conflict", "check_target_machine", "check_inputs", "DEFAULT_MEMORY_MB", "DEFAULT_CPUS", "MIN_MEMORY_MB",
    "EXPORT_MIN_INTERVAL_SECONDS", "SLUG_RE", "IMAGE_RE",
]
