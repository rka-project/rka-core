"""Build and validate the public RKA Core capability manifest."""

from __future__ import annotations

from rka import __version__
from rka.contracts import (
    AGENTIC_UNSUPPORTED,
    CORE,
    CORE_LEGACY,
    mcp_operation_disposition,
)
from rka.mcp.operations_schema import (
    DEPRECATED_OPERATIONS,
    OPERATIONS_SCHEMA,
    WRITER_COMPATIBILITY_OPERATIONS,
    operation_maturity,
)
from rka.models.capabilities import (
    CapabilityNegotiationError,
    CapabilityRequirementIssue,
    CoreCapabilities,
    CoreContractIdentity,
    EmbeddingCapability,
    InterfaceCapabilities,
    McpInterfaceCapability,
    RestInterfaceCapability,
)


CAPABILITY_SCHEMA_VERSION = "rka.core-capabilities/v1"
CORE_CONTRACT = "rka-core/v1"
REST_CONTRACT = "rka-rest/v1"
MCP_CONTRACT = "rka-mcp/v1"
SUPPORTED_CORE_CONTRACTS = (CORE_CONTRACT,)
SUPPORTED_CAPABILITIES = ("rest", "mcp", "embedding")


def _operation_counts() -> dict[str, int]:
    """Return mutually exclusive discovery counts.

    ``operation_maturity`` remains the historical usage/readiness signal.
    Explicit deprecation is an orthogonal contract signal and takes
    precedence in this summary so the three counts add up to the full
    operation registry.
    """

    counts = {
        "stable": 0,
        "preview": 0,
        "deprecated": 0,
        "supported": 0,
        "supported_stable": 0,
        "supported_preview": 0,
        "unsupported": 0,
        "legacy": 0,
    }
    for operation in OPERATIONS_SCHEMA:
        if operation in DEPRECATED_OPERATIONS:
            counts["deprecated"] += 1
        else:
            counts[operation_maturity(operation)] += 1
        disposition = mcp_operation_disposition(
            operation,
            writer_operations=WRITER_COMPATIBILITY_OPERATIONS,
        )
        if disposition == CORE:
            maturity = operation_maturity(operation)
            counts["supported"] += 1
            counts[f"supported_{maturity}"] += 1
        elif disposition == AGENTIC_UNSUPPORTED:
            counts["unsupported"] += 1
        elif disposition == CORE_LEGACY:
            counts["legacy"] += 1
    return counts


def build_core_capabilities(
    *, embedding_available: bool, embedding_reason: str | None,
    embedding_index_status: str | None = None,
) -> CoreCapabilities:
    """Build the versioned discovery document without touching storage."""

    counts = _operation_counts()
    embedding = EmbeddingCapability(
        available=embedding_available,
        reason_unavailable=embedding_reason,
        search_mode="hybrid" if embedding_available else "lexical",
        index_status=embedding_index_status,
        warning=(
            "Partial semantic coverage: only verified current vectors are searchable; "
            "records without vectors remain keyword-searchable. Backfill still needs attention."
            if embedding_index_status == "partial" else None
        ),
    )
    available = ["rest", "mcp"]
    if embedding.available:
        available.append("embedding")

    return CoreCapabilities(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        core=CoreContractIdentity(
            version=__version__,
            contract=CORE_CONTRACT,
            supported_contracts=list(SUPPORTED_CORE_CONTRACTS),
        ),
        interfaces=InterfaceCapabilities(
            rest=RestInterfaceCapability(contract=REST_CONTRACT),
            mcp=McpInterfaceCapability(
                contract=MCP_CONTRACT,
                default_operation_count=counts["supported_stable"],
                usage_stable_operation_count=counts["stable"],
                usage_preview_operation_count=counts["preview"],
                deprecated_operation_count=counts["deprecated"],
                supported_operation_count=counts["supported"],
                supported_usage_stable_operation_count=counts["supported_stable"],
                supported_usage_preview_operation_count=counts["supported_preview"],
                unsupported_operation_count=counts["unsupported"],
                legacy_operation_count=counts["legacy"],
            ),
        ),
        supported_capabilities=list(SUPPORTED_CAPABILITIES),
        available_capabilities=available,
        embedding=embedding,
    )


def validate_capability_requirements(
    document: CoreCapabilities,
    *,
    required_contract: str | None,
    required_capabilities: list[str] | None,
) -> CapabilityNegotiationError | None:
    """Return an actionable error when a client's requirements cannot be met."""

    requirements = list(dict.fromkeys(required_capabilities or []))
    issues: list[CapabilityRequirementIssue] = []

    if required_contract and required_contract not in document.core.supported_contracts:
        issues.append(
            CapabilityRequirementIssue(
                requirement=required_contract,
                reason="The requested Core contract is not implemented by this server.",
                status="unsupported",
            )
        )

    supported = set(document.supported_capabilities)
    available = set(document.available_capabilities)
    for capability in requirements:
        if capability not in supported:
            issues.append(
                CapabilityRequirementIssue(
                    requirement=capability,
                    reason="Unknown capability name for this discovery contract.",
                    status="unsupported",
                )
            )
        elif capability not in available:
            reason = (
                document.embedding.reason_unavailable
                if capability == "embedding"
                else "The capability is supported but unavailable in this runtime."
            )
            issues.append(
                CapabilityRequirementIssue(
                    requirement=capability,
                    reason=reason or "The capability is unavailable.",
                    status="unavailable",
                )
            )

    if not issues:
        return None

    capability_issue_requirements = {issue.requirement for issue in issues} & set(requirements)
    contract_only = bool(required_contract) and not capability_issue_requirements
    error_code = (
        "unsupported_core_contract" if contract_only else "unsupported_capability_combination"
    )
    return CapabilityNegotiationError(
        error=error_code,
        message="This RKA Core runtime cannot satisfy the requested contract/capabilities.",
        core_version=document.core.version,
        requested_contract=required_contract,
        required_capabilities=requirements,
        supported_contracts=document.core.supported_contracts,
        supported_capabilities=document.supported_capabilities,
        issues=issues,
        hint=(
            f"Use a client compatible with {CORE_CONTRACT}; remove unsupported "
            "requirements or enable the unavailable runtime capability, then retry."
        ),
    )
