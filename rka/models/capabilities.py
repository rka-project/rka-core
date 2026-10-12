"""Public models for RKA Core capability and contract discovery."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EmbeddingCapability(BaseModel):
    """Runtime availability of the optional embedding backend."""

    available: bool
    reason_unavailable: str | None = None
    search_mode: Literal["hybrid", "lexical"] = "lexical"
    index_status: Literal["ready", "partial", "reindexing", "failed", "unavailable"] | None = None
    warning: str | None = None


class CoreContractIdentity(BaseModel):
    """Product identity and public-contract compatibility information."""

    name: Literal["rka-core"] = "rka-core"
    version: str
    contract: str
    supported_contracts: list[str]


class RestInterfaceCapability(BaseModel):
    """Stable REST discovery entrypoint."""

    status: Literal["stable"] = "stable"
    contract: str
    discovery: str = "/openapi.json"


class McpInterfaceCapability(BaseModel):
    """Stable MCP dispatch interface and operation-discovery summary."""

    status: Literal["stable"] = "stable"
    contract: str
    discovery: str = "rka_describe"
    operation_maturity_basis: Literal["usage-readiness"] = "usage-readiness"
    default_operation_count: int = Field(
        ge=0,
        description="Core-owned operations listed by default in rka_describe.",
    )
    usage_stable_operation_count: int = Field(
        ge=0,
        description=(
            "Usage-stable count across all non-deprecated operations, including "
            "unsupported and legacy compatibility branches."
        ),
    )
    usage_preview_operation_count: int = Field(
        ge=0,
        description=(
            "Historical usage-preview count across all non-deprecated operations; "
            "use supported_usage_preview_operation_count for the Core-owned count."
        ),
    )
    deprecated_operation_count: int = Field(
        ge=0,
        description="Frozen Writer compatibility operation count.",
    )
    supported_operation_count: int = Field(
        ge=0,
        description="Core-owned typed operations, stable and preview combined.",
    )
    supported_usage_stable_operation_count: int = Field(
        ge=0,
        description="Core-owned operations currently marked usage-stable.",
    )
    supported_usage_preview_operation_count: int = Field(
        ge=0,
        description="Core-owned operations currently marked usage-preview.",
    )
    unsupported_operation_count: int = Field(
        ge=0,
        description="Still-exposed operations owned by the shelved Agentic layer.",
    )
    legacy_operation_count: int = Field(
        ge=0,
        description="Still-exposed Core legacy operations outside rka-mcp/v1.",
    )


class InterfaceCapabilities(BaseModel):
    rest: RestInterfaceCapability
    mcp: McpInterfaceCapability


class CoreCapabilities(BaseModel):
    """Versioned, additive discovery document returned by Core."""

    schema_version: Literal["rka.core-capabilities/v1"] = "rka.core-capabilities/v1"
    core: CoreContractIdentity
    interfaces: InterfaceCapabilities
    supported_capabilities: list[str]
    available_capabilities: list[str]
    embedding: EmbeddingCapability


class CapabilityRequirementIssue(BaseModel):
    """One unsatisfied capability or contract requirement."""

    requirement: str
    reason: str
    status: Literal["unsupported", "unavailable"]


class CapabilityNegotiationError(BaseModel):
    """Actionable response for an unsupported client requirement set."""

    error: Literal["unsupported_core_contract", "unsupported_capability_combination"]
    message: str
    core_version: str
    requested_contract: str | None = None
    required_capabilities: list[str]
    supported_contracts: list[str]
    supported_capabilities: list[str]
    issues: list[CapabilityRequirementIssue]
    hint: str
