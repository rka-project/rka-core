"""Research journal models."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)

# v2.0 canonical types
JournalType = Literal["note", "log", "directive"]
JournalCaptureMode = Literal["unknown", "raw_capture", "agent_restatement"]


def validate_capture(mode: JournalCaptureMode, source: str, original: str | None) -> None:
    """Validate explicit capture claims; never infer historical evidence."""
    if mode == "raw_capture" and not (original or "").strip():
        raise ValueError("raw_capture requires a non-blank verbatim_input")
    if mode == "agent_restatement" and source == "pi" and not (original or "").strip():
        raise ValueError("PI agent_restatement requires verbatim_input; do not invent missing originals")

# Legacy types accepted for backward compatibility (silently mapped to v2 types)
LegacyJournalType = Literal[
    "finding", "insight", "pi_instruction", "exploration",
    "idea", "observation", "hypothesis", "methodology", "summary",
]

# Combined type for input validation (accepts both old and new)
AnyJournalType = Literal[
    "finding", "insight", "pi_instruction", "exploration",
    "idea", "observation", "hypothesis", "methodology", "summary",
    "note", "log", "directive",
]

# Mapping from legacy types to v2 types
JOURNAL_TYPE_MAP: dict[str, str] = {
    "finding": "note",
    "insight": "note",
    "idea": "note",
    "observation": "note",
    "exploration": "note",
    "hypothesis": "note",
    "summary": "note",
    "methodology": "log",
    "pi_instruction": "directive",
    # v2 types map to themselves
    "note": "note",
    "log": "log",
    "directive": "directive",
}


def normalize_journal_type(raw_type: str) -> str:
    """Map any journal type (legacy or v2) to the canonical v2 type."""
    mapped = JOURNAL_TYPE_MAP.get(raw_type)
    if mapped is None:
        raise ValueError(f"Unknown journal type: {raw_type!r}")
    if mapped != raw_type:
        logger.debug("Mapped legacy journal type %r → %r", raw_type, mapped)
    return mapped


class JournalEntryCreate(BaseModel):
    """Create a new journal entry.

    extra="forbid": undeclared fields raise 422 instead of silently stripping.
    Mirrors the JournalEntryUpdate guard added by Bug A; closes the parallel
    CREATE-path silent-write hole identified by Mission C
    (mis_01KR43RX9KY11GAPTPPGK9XSDE).
    """

    model_config = ConfigDict(extra="forbid")

    content: str
    type: AnyJournalType = "note"
    summary: str | None = None
    # Defaults to `executor`, matching RecordNoteArgs on the MCP surface.
    # It defaulted to `pi` here, so any REST caller that omitted the field
    # silently claimed PI authorship — the strongest provenance signal in the
    # system, asserted by omission. 46 live entries claim source='pi' with no
    # verbatim record, 22 of them in real research projects, and none of them
    # came from a caller that meant to say it.
    #
    # Old callers remain explicitly unclassified. Explicit capture modes use
    # the same validation for REST, MCP, batch import and internal callers.
    source: Literal["brain", "executor", "pi", "web_ui", "llm"] = "executor"
    capture_mode: JournalCaptureMode = Field(
        default="unknown",
        description="unknown: no capture claim; raw_capture: preserve supplied text; "
        "agent_restatement: agent-rendered body, PI source requires exact original. "
        "Raw creation snapshots content only when verbatim_input is absent/null.",
    )
    phase: str | None = None
    verbatim_input: str | None = None
    related_decisions: list[str] | None = None
    related_literature: list[str] | None = None
    related_mission: str | None = None
    supersedes: str | None = None
    confidence: Literal["hypothesis", "tested", "verified", "superseded", "retracted"] = "hypothesis"
    importance: Literal["critical", "high", "normal", "low", "archived"] = "normal"
    status: Literal["draft", "active", "superseded", "retracted"] = "active"
    pinned: bool = False
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_type(self) -> JournalEntryCreate:
        self.type = normalize_journal_type(self.type)  # type: ignore[assignment]
        if self.capture_mode == "raw_capture" and self.verbatim_input is None:
            self.verbatim_input = self.content
        validate_capture(self.capture_mode, self.source, self.verbatim_input)
        return self


class JournalEntryUpdate(BaseModel):
    """Partial update for journal entry.

    extra="forbid": undeclared fields raise 422 instead of silently stripping.
    See mis_01KQJH9MB65AR0GSVPQBT8707X (silent-write-failure fix) for context.
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    type: AnyJournalType | None = None
    summary: str | None = None
    source: Literal["brain", "executor", "pi", "web_ui", "llm"] | None = Field(
        None, deprecated=True, description="Use correct_note_attribution; non-null updates are rejected.",
    )
    phase: str | None = None
    verbatim_input: str | None = Field(
        None, deprecated=True, description="Use correct_note_attribution; non-null updates are rejected.",
    )
    confidence: Literal["hypothesis", "tested", "verified", "superseded", "retracted"] | None = None
    importance: Literal["critical", "high", "normal", "low", "archived"] | None = None
    status: Literal["draft", "active", "superseded", "retracted"] | None = None
    pinned: bool | None = None
    related_decisions: list[str] | None = None
    related_literature: list[str] | None = None
    related_mission: str | None = None
    tags: list[str] | None = None

    @model_validator(mode="after")
    def _normalize_type(self) -> JournalEntryUpdate:
        if self.__dict__.get("source") is not None or self.__dict__.get("verbatim_input") is not None:
            raise ValueError("source/verbatim_input require correct_note_attribution with reason and revision")
        if self.type is not None:
            self.type = normalize_journal_type(self.type)  # type: ignore[assignment]
        return self


class JournalEntry(BaseModel):
    """Full journal entry from database."""

    id: str
    project_id: str
    type: str
    content: str
    summary: str | None = None
    source: str
    attribution_revision: int = 0
    capture_mode: JournalCaptureMode = "unknown"
    phase: str | None = None
    verbatim_input: str | None = None
    related_decisions: list[str] | None = None
    related_literature: list[str] | None = None
    related_mission: str | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    confidence: str
    importance: str
    status: str = "active"
    pinned: bool = False
    tags: list[str] = Field(default_factory=list)
    enrichment_status: Literal["pending", "ready", "failed"] = "ready"
    created_at: str | None = None
    updated_at: str | None = None


class JournalAttributionCorrection(BaseModel):
    """Full replacement of asserted attribution, not authenticated identity.

    Both source and verbatim_input are required; explicit null means the
    original is unknown. Reasons and quoted text are preserved byte-for-byte.
    """

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0, strict=True)
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    actor: Literal["brain", "executor", "pi", "llm", "web_ui", "system"]
    reason: str = Field(min_length=1, max_length=4000)
    source: Literal["brain", "executor", "pi", "llm", "web_ui"]
    verbatim_input: str | None
    capture_mode: JournalCaptureMode | None = Field(
        default=None, description="Omitted/null preserves the previous capture mode. "
        "Explicit mode changes are revision-guarded; raw_capture needs a supplied original.",
    )

    @model_validator(mode="after")
    def _validate_correction(self) -> JournalAttributionCorrection:
        if not self.reason.strip():
            raise ValueError("attribution correction requires a non-blank reason")
        if self.source == "pi" and not (self.verbatim_input or "").strip():
            raise ValueError("PI attribution correction requires verbatim_input; do not invent missing originals")
        if self.capture_mode is not None:
            validate_capture(self.capture_mode, self.source, self.verbatim_input)
        return self


class JournalAttributionRevision(BaseModel):
    """Immutable correction result. Retries return this event, not current state."""

    id: str
    project_id: str
    journal_id: str
    revision: int
    expected_revision: int
    request_id: str
    actor: str
    actor_basis: Literal["caller_asserted"]
    reason: str
    before_source: str
    before_verbatim_input: str | None
    before_capture_mode: JournalCaptureMode = "unknown"
    after_source: str
    after_verbatim_input: str | None
    after_capture_mode: JournalCaptureMode = "unknown"
    created_at: str
