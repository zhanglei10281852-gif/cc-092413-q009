from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class EventCreate(BaseModel):
    external_id: str = Field(..., min_length=1, max_length=80)
    origin_time: str = Field(..., min_length=20, max_length=40)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    depth_km: float = Field(..., ge=0, le=800)
    magnitude: float = Field(..., ge=-1, le=10)
    magnitude_type: str = Field(default="ML", min_length=1, max_length=12)
    source: str = Field(default="manual", min_length=1, max_length=40)


class EventPatch(BaseModel):
    depth_km: float | None = Field(default=None, ge=0, le=800)
    magnitude: float | None = Field(default=None, ge=-1, le=10)
    magnitude_type: str | None = Field(default=None, min_length=1, max_length=12)
    status: str | None = Field(default=None, pattern="^(draft|review|published|archived)$")
    reason: str = Field(default="", max_length=300)


class ObservationCreate(BaseModel):
    station_code: str = Field(..., min_length=2, max_length=32)
    channel: str = Field(..., min_length=2, max_length=16)
    observed_at: str = Field(..., min_length=20, max_length=40)
    pga: float | None = Field(default=None, ge=0, le=100)
    pgv: float | None = Field(default=None, ge=0, le=500)
    distance_km: float = Field(..., ge=0, le=2000)
    quality_hint: str = Field(default="raw", max_length=24)

    @field_validator("station_code", "channel")
    @classmethod
    def strip_codes(cls, value: str) -> str:
        return value.strip().upper()


class ComputeRequest(BaseModel):
    model_version: str = Field(default="gmpe-2026.1", min_length=1, max_length=40)
    grid_step_km: float = Field(default=10, gt=0, le=100)
    radius_km: float = Field(default=100, gt=0, le=1000)
    requested_by: str = Field(default="system", max_length=80)


class TaskComplete(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=80)
    result: dict = Field(default_factory=dict)


class SequenceSplitRequest(BaseModel):
    event_ids: list[int] = Field(..., min_length=1)
    name: str | None = Field(default=None, max_length=120)
    new_mainshock_event_id: int | None = None
    reason: str = Field(default="", max_length=300)


class SequenceMergeRequest(BaseModel):
    source_sequence_ids: list[int] = Field(..., min_length=1)
    reason: str = Field(default="", max_length=300)


class PolicyUpdateRequest(BaseModel):
    enabled: bool | None = None
    alert_aftershocks: bool | None = None
    suppression_window_seconds: int | None = Field(default=None, ge=0, le=30 * 24 * 3600)


class WindowAdvanceRequest(BaseModel):
    window_seconds: int = Field(default=3600, ge=60, le=30 * 24 * 3600)
    slide_seconds: int = Field(default=300, ge=60, le=24 * 3600)
    as_of: datetime | None = None

    @field_validator("slide_seconds")
    @classmethod
    def slide_within_window(cls, value: int, info):
        window = info.data.get("window_seconds")
        if window is not None and value > window:
            raise ValueError("slide_seconds 不能大于 window_seconds")
        return value

