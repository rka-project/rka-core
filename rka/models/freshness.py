"""Audited freshness review inputs shared by adapters."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ResolveStaleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str
    verdict: Literal["current", "historical", "retired", "superseded", "retracted", "dismissed"]
    resolution: str = Field(min_length=1)
    resolved_by: Literal["brain", "executor", "pi"]
    journal_id: str | None = None

    @field_validator("resolution")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("resolution must not be blank")
        return value
