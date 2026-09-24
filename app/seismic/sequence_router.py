from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.seismic.schemas import AlertPolicyRequest, SequenceMergeRequest, SequenceSplitRequest
from app.seismic.sequences import SequenceService

router = APIRouter(prefix="/api/seismic", tags=["余震序列关联与统计"])


def service() -> SequenceService:
    return SequenceService()


@router.get("/sequences")
def list_sequences(status: str | None = Query(default=None, pattern="^(active|merged|closed)$")):
    return {"sequences": service().list_sequences(status)}


@router.get("/sequences/{sequence_id}")
def get_sequence(sequence_id: int):
    value = service().get_sequence(sequence_id)
    if value is None:
        raise HTTPException(status_code=404, detail="序列不存在")
    return value


@router.get("/sequences/{sequence_id}/ops")
def list_ops(sequence_id: int):
    if service().get_sequence(sequence_id) is None:
        raise HTTPException(status_code=404, detail="序列不存在")
    return {"ops": service().list_ops(sequence_id)}


@router.post("/sequences/{sequence_id}/split", status_code=201)
def split_sequence(sequence_id: int, payload: SequenceSplitRequest):
    try:
        return service().split_sequence(sequence_id, payload.event_ids, payload.actor, payload.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="序列不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sequences/{sequence_id}/merge")
def merge_sequences(sequence_id: int, payload: SequenceMergeRequest):
    try:
        return service().merge_sequences(sequence_id, payload.source_sequence_ids, payload.actor, payload.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="序列不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sequences/{sequence_id}/windows/roll")
def roll_windows(sequence_id: int, as_of: str | None = Query(default=None, min_length=20, max_length=40)):
    try:
        windows = service().roll_windows(sequence_id, as_of)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="序列不存在") from exc
    return {"windows": windows}


@router.post("/windows/roll-all")
def roll_all():
    return service().roll_all()


@router.put("/sequences/{sequence_id}/alert-policy")
def upsert_policy(sequence_id: int, payload: AlertPolicyRequest):
    try:
        return service().upsert_policy(sequence_id, payload.min_magnitude, payload.suppression_seconds)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="序列不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sequences/{sequence_id}/alerts")
def list_alerts(sequence_id: int):
    if service().get_sequence(sequence_id) is None:
        raise HTTPException(status_code=404, detail="序列不存在")
    return {"alerts": service().list_alerts(sequence_id)}


@router.get("/events/{event_id}/attribution")
def event_attribution(event_id: int):
    value = service().get_attribution(event_id)
    if value is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    return value
