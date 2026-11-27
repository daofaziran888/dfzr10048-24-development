"""改标事件领域服务：把持久化改标台账接入统一资源路由。

平台约定「状态流转只允许在 services 里改，路由层不做业务判断」，
因此机器回执重送、迟到拒收、人工改标生效等判断都落在本服务，
路由只做参数转发。台账是权威持久化存储，内存 store 仅镜像列表字段供通用筛选。
"""
from __future__ import annotations

from typing import Any

from app.pipeline.relabel_event_ledger import MANUAL_SOURCE, MACHINE_SOURCE
from app.pipeline.traceability import relabel_ledger
from app.services.base import BaseModuleService
from app.store import store


class RelabelEventService(BaseModuleService):
    MODULE = "relabel_event"
    ENTITY = "改标事件"
    OPERATION_TERM = "登记"
    LIST_FIELDS = [
        "事件编号", "关联分段", "字幕行号", "说话人标签", "原标签",
        "标注来源", "写入人", "回执编号", "是否确认", "是否采纳", "改标次数", "发生时间",
    ]
    REQUIRED_FIELDS = ["关联分段", "说话人标签", "写入人", "标注来源"]
    STATUS_ORDER = ["已登记", "待确认", "已确认", "未采纳"]
    ACTION_RULES = {"提交确认": "待确认", "确认事件": "已确认", "标记未采纳": "未采纳"}
    NEGATIVE_ACTIONS = ["标记未采纳"]

    def __init__(self) -> None:
        self._hydrated = False

    def _hydrate(self) -> None:
        """把台账镜像进内存表，供通用列表/筛选复用。"""
        if self._hydrated:
            return
        rows = store.rows(self.MODULE)
        rows.clear()
        for event in relabel_ledger().list_events():
            rows.append(self._to_row(event))
        self._hydrated = True

    @staticmethod
    def _status(event: Any) -> str:
        if not event.applied:
            return "未采纳"
        if event.confirmed:
            return "已确认"
        if event.source == MACHINE_SOURCE:
            return "待确认"
        return "已确认"

    def _to_row(self, event: Any) -> dict[str, Any]:
        status = self._status(event)
        return {
            "id": event.event_id,
            "事件编号": f"RLBL-{event.event_id:05d}",
            "关联分段": event.segment_id,
            "字幕行号": str(event.line_no),
            "说话人标签": event.label,
            "原标签": event.original_label,
            "标注来源": event.source,
            "写入人": event.operator,
            "回执编号": event.receipt_id,
            "是否确认": "是" if event.confirmed else "否",
            "是否采纳": "是" if event.applied else "否",
            "改标次数": str(event.change_count),
            "发生时间": event.created_at,
            "备注": event.note,
            "status": status,
            "pending": status == "待确认",
            "abnormal": not event.applied,
        }

    def list_entries(
        self, *, filters: dict[str, str] | None = None, keyword: str | None = None,
        status: str | None = None, page: int = 1, size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        self._hydrate()
        return super().list_entries(
            filters=filters, keyword=keyword, status=status, page=page, size=size)

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        event = relabel_ledger().get_event(entry_id)
        return self._to_row(event) if event else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing

        source = str(values.get("标注来源", "")).strip()
        segment_id = str(values["关联分段"]).strip()
        label = str(values["说话人标签"]).strip()
        operator = str(values["写入人"]).strip()
        line_no = int(values.get("字幕行号") or 0)
        receipt_id = str(values.get("回执编号") or "").strip()

        if source == MANUAL_SOURCE:
            event, _ = relabel_ledger().record_manual(
                segment_id=segment_id, label=label, operator=operator,
                line_no=line_no,
                original_label=str(values.get("原标签") or "").strip() or None,
                receipt_id=receipt_id,
            )
        elif source == MACHINE_SOURCE:
            if not receipt_id:
                return None, ["回执编号"]
            event, _, _ = relabel_ledger().record_machine(
                segment_id=segment_id, label=label, operator=operator,
                receipt_id=receipt_id, line_no=line_no,
                confirmed=str(values.get("是否确认", "")).strip() == "是",
            )
        else:
            return None, ["标注来源"]

        self._hydrated = False
        return self._to_row(event), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        event = relabel_ledger().get_event(entry_id)
        if event is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "确认事件":
            if event.source != MACHINE_SOURCE:
                return self._to_row(event), "人工改标即时生效，无需确认"
            updated, message = relabel_ledger().confirm_receipt(event.receipt_id)
            self._hydrated = False
            return (self._to_row(updated), message) if updated else (None, message)
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        # 「提交确认/标记未采纳」只影响展示状态，权威结论以台账 applied/confirmed 为准。
        return self._to_row(event), f"{self.ENTITY}已{action}"

    # --------------------------------------------------------------- 场景化查询
    def list_for_segment(self, segment_id: str) -> list[dict[str, Any]]:
        return [self._to_row(event) for event in relabel_ledger().events_for_segment(segment_id)]

    def list_for_operator(self, operator: str) -> list[dict[str, Any]]:
        return [self._to_row(event) for event in relabel_ledger().events_for_operator(operator)]

    def effective_for_segment(self, segment_id: str) -> dict[str, Any] | None:
        event = relabel_ledger().effective_event(segment_id)
        return self._to_row(event) if event else None
