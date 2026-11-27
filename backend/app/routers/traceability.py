"""说话人改标追溯接口：装配稿、字幕包来源与逐行凭据。

路由层保持轻薄，只做参数转发，业务判断全部落在 services / pipeline：
- POST /api/traceability/relabel/machine      登记机器回执（重送幂等、迟到拒收）
- POST /api/traceability/relabel/manual       登记人工改标（即时生效）
- POST /api/traceability/relabel/confirm      确认机器回执
- GET  /api/traceability/relabel/events       按分段/写入人查询改标事件
- POST /api/traceability/assemble             为转写版本装配一份新稿
- POST /api/traceability/packs/link           字幕包关联实际装配稿
- GET  /api/traceability/packs/{pack_id}/draft        取字幕包实际装配稿
- GET  /api/traceability/packs/{pack_id}/lines/{line_no}  逐行还原写入人与原标签
- POST /api/traceability/packs/{pack_id}/compare       与已发字幕逐行对照
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.pipeline.traceability import assembly_service, relabel_ledger

router = APIRouter(prefix="/api/traceability", tags=["说话人改标追溯"])


class MachineRelabelPayload(BaseModel):
    segment_id: str
    label: str
    operator: str
    receipt_id: str
    line_no: int = 0
    confirmed: bool = False


class ManualRelabelPayload(BaseModel):
    segment_id: str
    label: str
    operator: str
    line_no: int = 0
    original_label: str | None = None
    receipt_id: str = ""


class ConfirmPayload(BaseModel):
    receipt_id: str


class AssemblyLineSpec(BaseModel):
    line_no: int
    segment_id: str
    text: str = ""


class AssemblePayload(BaseModel):
    version_id: str
    task_id: str
    media_id: str
    lines: list[AssemblyLineSpec] = Field(default_factory=list)


class LinkPackPayload(BaseModel):
    pack_id: str
    draft_id: str


class DeliveredLine(BaseModel):
    line_no: int
    label: str


class ComparePayload(BaseModel):
    delivered_lines: list[DeliveredLine] = Field(default_factory=list)


@router.post("/relabel/machine")
def record_machine(payload: MachineRelabelPayload) -> dict[str, Any]:
    event, created, message = relabel_ledger().record_machine(
        segment_id=payload.segment_id,
        label=payload.label,
        operator=payload.operator,
        receipt_id=payload.receipt_id,
        line_no=payload.line_no,
        confirmed=payload.confirmed,
    )
    return {
        "ok": True,
        "created": created,
        "message": message,
        "event": event.to_dict(),
    }


@router.post("/relabel/manual")
def record_manual(payload: ManualRelabelPayload) -> dict[str, Any]:
    event, message = relabel_ledger().record_manual(
        segment_id=payload.segment_id,
        label=payload.label,
        operator=payload.operator,
        line_no=payload.line_no,
        original_label=payload.original_label,
        receipt_id=payload.receipt_id,
    )
    return {"ok": True, "message": message, "event": event.to_dict()}


@router.post("/relabel/confirm")
def confirm_receipt(payload: ConfirmPayload) -> dict[str, Any]:
    event, message = relabel_ledger().confirm_receipt(payload.receipt_id)
    if event is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "event": event.to_dict()}


@router.get("/relabel/events")
def list_relabel_events(
    segment_id: str | None = None, operator: str | None = None,
) -> dict[str, Any]:
    events = relabel_ledger().list_events(segment_id=segment_id, operator=operator)
    return {
        "items": [event.to_dict() for event in events],
        "total": len(events),
    }


@router.post("/assemble")
def assemble(payload: AssemblePayload) -> dict[str, Any]:
    draft = assembly_service().assemble(
        version_id=payload.version_id,
        task_id=payload.task_id,
        media_id=payload.media_id,
        line_specs=[item.model_dump() for item in payload.lines],
    )
    return {"ok": True, "draft": draft.to_dict()}


@router.post("/packs/link")
def link_pack(payload: LinkPackPayload) -> dict[str, Any]:
    ok, message = assembly_service().link_pack(payload.pack_id, payload.draft_id)
    if not ok:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message}


@router.get("/packs/{pack_id}/draft")
def get_pack_draft(pack_id: str) -> dict[str, Any]:
    draft = assembly_service().draft_for_pack(pack_id)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"字幕包 {pack_id} 尚未关联装配稿")
    return {"ok": True, "draft": draft.to_dict()}


@router.get("/packs/{pack_id}/lines/{line_no}")
def get_line_provenance(pack_id: str, line_no: int) -> dict[str, Any]:
    provenance = assembly_service().line_provenance(pack_id, line_no)
    if provenance is None:
        raise HTTPException(
            status_code=404,
            detail=f"字幕包 {pack_id} 第 {line_no} 行来源凭据不存在",
        )
    return {"ok": True, "provenance": provenance}


@router.post("/packs/{pack_id}/compare")
def compare_with_delivery(pack_id: str, payload: ComparePayload) -> dict[str, Any]:
    if assembly_service().draft_for_pack(pack_id) is None:
        raise HTTPException(status_code=404, detail=f"字幕包 {pack_id} 尚未关联装配稿")
    comparison = assembly_service().compare_with_delivery(
        pack_id, [item.model_dump() for item in payload.delivered_lines],
    )
    return {
        "ok": True,
        "items": comparison,
        "all_match": all(item["match"] for item in comparison),
    }
