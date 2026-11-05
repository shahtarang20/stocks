"""Job Spec v2 (ADR-0006 section 6), the subset accepted in U1. Unknown fields are refused everywhere.

Fields the ADR lists but later packages build (BYOC image/command, network, parallelism, privacy, pipelines)
are refused with ``not_supported_yet`` instead of silently ignored (L1).
"""

from __future__ import annotations

import decimal
import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_REF_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}@[0-9A-Za-z][0-9A-Za-z.\-+]{0,30}$")
_SOURCE_RE = re.compile(r"^(dataset|artifact):[A-Za-z0-9_\-./:]{1,200}$")
_NAME_RE = r"^[a-z][a-z0-9_]{0,31}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputRef(_Strict):
    name: str = Field(pattern=_NAME_RE)
    source: str

    @field_validator("source")
    @classmethod
    def _source(cls, v: str) -> str:
        if not _SOURCE_RE.fullmatch(v):
            raise ValueError("source must look like dataset:<id> or artifact:<job>/<name>")
        return v


class OutputRef(_Strict):
    name: str = Field(pattern=_NAME_RE)
    stream_previews: bool = False


class GpuRequest(_Strict):
    count: int | None = Field(default=None, ge=1, le=8)
    min_vram_gb: int | None = Field(default=None, ge=1, le=1024)
    min_compute_capability: str | None = Field(default=None, pattern=r"^\d{1,2}\.\d$")


class ResourceRequest(_Strict):
    gpu: GpuRequest = Field(default_factory=GpuRequest)
    nodes: int | None = Field(default=None, ge=1, le=8)  # U13: multi-node needs MULTI_NODE_ENABLED and approved hosts
    cpu: int | None = Field(default=None, ge=1, le=256)
    ram_gb: int | None = Field(default=None, ge=1, le=4096)
    disk_gb: int | None = Field(default=None, ge=1, le=100000)


class BillingHints(_Strict):
    """Hints only. Billing stays per GPU-second (P5)."""

    unit: str | None = Field(default=None, pattern=r"^[a-z][a-z_]{0,31}$")
    expected_units: int | None = Field(default=None, ge=1, le=10**9)
    max_cost: decimal.Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)


MAX_FANOUT_PARTS = 1000  # hard ceiling; the platform setting FANOUT_MAX_PARTS may only lower it


class SplitRange(_Strict):
    """Inclusive integer range cut into chunks of ``chunk`` (U4). ``from`` is the JSON key."""

    kind: Literal["range"]
    from_: int = Field(alias="from", ge=-(10**9), le=10**9)
    to: int = Field(ge=-(10**9), le=10**9)
    chunk: int = Field(ge=1, le=10**9)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _bounds(self) -> "SplitRange":
        if self.to < self.from_:
            raise ValueError("'to' must be >= 'from'")
        if -(-(self.to - self.from_ + 1) // self.chunk) > MAX_FANOUT_PARTS:
            raise ValueError(f"split would make more than {MAX_FANOUT_PARTS} parts")
        return self


class SplitItems(_Strict):
    kind: Literal["items"]
    chunk: int = Field(ge=1, le=100_000)


class SplitBytes(_Strict):
    kind: Literal["bytes"]
    input: str = Field(pattern=_NAME_RE)
    chunk_bytes: int = Field(ge=1, le=2**50)


class Parallelism(_Strict):
    """Fan-out (U4): one job split into parts. Only accepted on POST /v1/jobs/v2/fanout."""

    split: Annotated[Union[SplitRange, SplitItems, SplitBytes], Field(discriminator="kind")]
    max_parallel: int | None = Field(default=None, ge=1, le=200)
    straggler_multiple: float | None = Field(default=None, ge=1.5, le=20.0)
    max_part_attempts: int | None = Field(default=None, ge=1, le=5)


class VmSpec(_Strict):
    """U14 (Windows VM tier, default off): the customer-supplied Windows image and VM size. The platform ships no
    Windows image and no licence; ``licence_ack`` records that the customer holds the licence."""

    image: str  # a dataset the customer owns: dataset:<id>
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    vcpus: int = Field(default=4, ge=1, le=64)
    memory_gb: int = Field(default=16, ge=2, le=512)
    gpu_passthrough: bool = True
    licence_ack: bool
    command: list[str] | None = Field(default=None, max_length=32)  # run inside the customer's own VM (replaces params.vm_command)

    @field_validator("image")
    @classmethod
    def _image(cls, v: str) -> str:
        if not re.fullmatch(r"dataset:[A-Za-z0-9_\-./:]{1,200}", v):
            raise ValueError("image must look like dataset:<id>")
        return v

    @field_validator("command")
    @classmethod
    def _command(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and (not v or any(not isinstance(a, str) or not a or len(a) > 512 or "\x00" in a for a in v)):
            raise ValueError("command must be 1 to 32 non-empty strings of at most 512 characters")
        return v

    @field_validator("licence_ack")
    @classmethod
    def _ack(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("the customer must confirm they hold a licence for the Windows image")
        return v


class JobSpecV2(_Strict):
    version: Literal[2]
    kit: str
    mode: Literal["batch", "vm"] = "batch"
    os: Literal["linux", "windows"] | None = None  # U14: accepted only with mode "vm"
    vm: VmSpec | None = None  # U14: accepted only with mode "vm"
    inputs: list[InputRef] = Field(default_factory=list, max_length=32)
    outputs: list[OutputRef] = Field(default_factory=list, max_length=32)
    resources: ResourceRequest = Field(default_factory=ResourceRequest)
    params: dict[str, Any] = Field(default_factory=dict)
    billing: BillingHints = Field(default_factory=BillingHints)
    parallelism: Parallelism | None = None  # U4: validated here, honoured only by the fan-out route

    @model_validator(mode="after")
    def _vm_fields(self) -> "JobSpecV2":
        if self.mode == "vm":
            if self.os != "windows" or self.vm is None:
                raise ValueError("mode 'vm' needs os 'windows' and a 'vm' block")
            if self.parallelism is not None:
                raise ValueError("a VM job cannot be fanned out")
        elif self.os is not None or self.vm is not None:
            raise ValueError("'os' and 'vm' are accepted only with mode 'vm'")
        return self

    @field_validator("kit")
    @classmethod
    def _kit(cls, v: str) -> str:
        if not _REF_RE.fullmatch(v):
            raise ValueError("kit must be '<slug>@<exact version>'")
        return v

    def kit_slug_version(self) -> tuple[str, str]:
        slug, _, version = self.kit.partition("@")
        return slug, version


# Top-level keys the ADR defines that are not built yet; reported as not_supported_yet.
NOT_SUPPORTED_YET = ("image", "command", "network", "checkpoint", "retries", "privacy",
                     "verification", "budget", "deadline")
