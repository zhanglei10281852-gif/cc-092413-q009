from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.seismic.schemas import (
    ComputeRequest,
    EventCreate,
    EventPatch,
    ObservationCreate,
    PolicyUpdateRequest,
    SequenceMergeRequest,
    SequenceSplitRequest,
    TaskComplete,
    WindowAdvanceRequest,
)
from app.seismic.sequences import SequenceService
from app.seismic.service import SeismicService

router = APIRouter(prefix="/api/seismic", tags=["地震科学计算"])


def service() -> SeismicService:
    return SeismicService()


def sequence_service() -> SequenceService:
    return SequenceService()


@router.post("/events", status_code=201)
def create_event(payload: EventCreate):
    try:
        return service().create_event(payload.model_dump(), actor=payload.source)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409, detail="external_id 已存在") from exc
        raise


@router.get("/events/{event_id}")
def get_event(event_id: int, include_observations: bool = Query(True)):
    value = service().get_event(event_id, include_observations)
    if value is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    return value


@router.patch("/events/{event_id}")
def patch_event(event_id: int, payload: EventPatch):
    try:
        return service().patch_event(event_id, payload.model_dump(exclude_unset=True), actor="operator")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="事件不存在") from exc


@router.post("/events/{event_id}/observations", status_code=201)
def add_observation(event_id: int, payload: ObservationCreate):
    try:
        return service().add_observation(event_id, payload.model_dump(), actor="station")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="事件不存在") from exc


@router.post("/events/{event_id}/computations", status_code=202)
def enqueue(event_id: int, payload: ComputeRequest):
    try:
        return service().enqueue_computation(event_id, payload.model_version, payload.grid_step_km, payload.radius_km, payload.requested_by)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="事件不存在") from exc


@router.post("/computations/claim")
def claim(worker_id: str = Query(..., min_length=1)):
    task = service().claim_task(worker_id)
    return {"task": task}


@router.post("/computations/{task_id}/calculate")
def calculate(task_id: int, worker_id: str = Query(..., min_length=1)):
    try:
        return service().calculate_task(task_id, worker_id)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail="任务不属于该工作者或不存在") from exc


@router.get("/computations/{task_id}")
def get_computation(task_id: int):
    current = service()
    row = current.connection.execute("SELECT * FROM seismic_computations WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return dict(row)


# ---- 余震序列 ----


def _raise_sequence_error(exc: Exception) -> HTTPException:
    message = str(exc)
    if message.startswith("sequence_not_found"):
        return HTTPException(status_code=404, detail="序列不存在")
    if message.startswith("events_not_in_sequence"):
        return HTTPException(status_code=409, detail="部分事件不属于该序列")
    if message.startswith("event_not_found"):
        return HTTPException(status_code=404, detail="事件不存在")
    if "not_active" in message:
        return HTTPException(status_code=409, detail="序列已终结或已合并，无法再调整")
    return HTTPException(status_code=400, detail=message)


@router.get("/sequences")
def list_sequences(status_filter: str | None = Query(default=None, alias="status")):
    return sequence_service().list_sequences(status_filter)


@router.get("/sequences/recover")
def recover_sequences():
    return sequence_service().recover()


@router.get("/sequences/{sequence_id}")
def get_sequence(sequence_id: int):
    view = sequence_service().get_sequence(sequence_id)
    if view is None:
        raise HTTPException(status_code=404, detail="序列不存在")
    return view


@router.get("/sequences/{sequence_id}/operations")
def get_sequence_operations(sequence_id: int):
    if sequence_service().get_sequence(sequence_id) is None:
        raise HTTPException(status_code=404, detail="序列不存在")
    return sequence_service().list_operations(sequence_id)


@router.get("/sequences/{sequence_id}/alerts")
def get_sequence_alerts(sequence_id: int):
    if sequence_service().get_sequence(sequence_id) is None:
        raise HTTPException(status_code=404, detail="序列不存在")
    return sequence_service().list_alerts(sequence_id)


@router.post("/sequences/{sequence_id}/split", status_code=201)
def split_sequence(sequence_id: int, payload: SequenceSplitRequest):
    try:
        return sequence_service().split_sequence(
            sequence_id,
            payload.event_ids,
            actor="operator",
            name=payload.name,
            reason=payload.reason,
            new_mainshock_event_id=payload.new_mainshock_event_id,
        )
    except KeyError as exc:
        raise _raise_sequence_error(exc) from exc
    except ValueError as exc:
        raise _raise_sequence_error(exc) from exc


@router.post("/sequences/{sequence_id}/merge", status_code=201)
def merge_sequences(sequence_id: int, payload: SequenceMergeRequest):
    try:
        return sequence_service().merge_sequences(sequence_id, payload.source_sequence_ids, actor="operator", reason=payload.reason)
    except KeyError as exc:
        raise _raise_sequence_error(exc) from exc
    except ValueError as exc:
        raise _raise_sequence_error(exc) from exc


@router.patch("/sequences/{sequence_id}/policy")
def update_sequence_policy(sequence_id: int, payload: PolicyUpdateRequest):
    try:
        return sequence_service().update_policy(
            sequence_id,
            enabled=payload.enabled,
            suppression_window_seconds=payload.suppression_window_seconds,
            alert_aftershocks=payload.alert_aftershocks,
        )
    except KeyError as exc:
        raise _raise_sequence_error(exc) from exc


@router.post("/sequences/{sequence_id}/windows/advance")
def advance_windows(sequence_id: int, payload: WindowAdvanceRequest):
    try:
        return sequence_service().advance_windows(
            sequence_id,
            payload.window_seconds,
            payload.slide_seconds,
            now=payload.as_of,
        )
    except KeyError as exc:
        raise _raise_sequence_error(exc) from exc
    except ValueError as exc:
        raise _raise_sequence_error(exc) from exc


@router.get("/sequences/{sequence_id}/windows")
def get_sequence_windows(sequence_id: int):
    if sequence_service().get_sequence(sequence_id) is None:
        raise HTTPException(status_code=404, detail="序列不存在")
    return sequence_service().get_windows(sequence_id)
