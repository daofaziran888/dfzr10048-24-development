"""说话人改标事件台账：持久保存每一次改标，给转写版本装配提供说话人来源记录。

解决的争议场景：
- 处置字幕署名争议时，只能翻到最新说话人标注，缺少当时的人工改标凭据；
- 机器结果（回执）可能重送/迟到，人工结论一旦形成就不能被机器结果更改；
- 同一机器回执重送必须幂等，不能重复登记、不能增加改标次数。

核心规则：
1. 人工改标永远生效，写入即确认，并使该分段此前待确认的机器领取失效；
2. 机器事件在该分段尚无人工结论时可被「领取」为当前标签，但需确认；
   一旦该分段存在已生效人工结论，迟到机器结果只留痕、不采纳（applied=False），
   不改写当前标签、不增加改标次数；
3. 机器回执按 receipt_id 幂等：重送同一张回执直接返回原事件，不新增、不计数；
4. 改标次数只统计人工改标（已采纳的人工事件数）；
5. 全部事件落盘，重启后可完整读回。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.persistence import JsonFileRepository

MACHINE_SOURCE = "机器"
MANUAL_SOURCE = "人工"


class RelabelEvent:
    """一条说话人改标事件（不可变凭据；确认状态只在确认动作上变更）。"""

    def __init__(
        self,
        event_id: int,
        segment_id: str,
        line_no: int,
        label: str,
        original_label: str,
        source: str,
        operator: str,
        receipt_id: str,
        confirmed: bool,
        applied: bool,
        change_count: int,
        created_at: str,
        note: str = "",
    ) -> None:
        self.event_id = event_id
        self.segment_id = segment_id
        self.line_no = line_no
        self.label = label
        self.original_label = original_label
        self.source = source
        self.operator = operator
        self.receipt_id = receipt_id
        self.confirmed = confirmed
        self.applied = applied
        self.change_count = change_count
        self.created_at = created_at
        self.note = note

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "segment_id": self.segment_id,
            "line_no": self.line_no,
            "label": self.label,
            "original_label": self.original_label,
            "source": self.source,
            "operator": self.operator,
            "receipt_id": self.receipt_id,
            "confirmed": self.confirmed,
            "applied": self.applied,
            "change_count": self.change_count,
            "created_at": self.created_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RelabelEvent":
        return cls(
            event_id=int(data["event_id"]),
            segment_id=str(data["segment_id"]),
            line_no=int(data.get("line_no", 0)),
            label=str(data["label"]),
            original_label=str(data.get("original_label", "")),
            source=str(data["source"]),
            operator=str(data.get("operator", "")),
            receipt_id=str(data.get("receipt_id", "")),
            confirmed=bool(data.get("confirmed", False)),
            applied=bool(data.get("applied", True)),
            change_count=int(data.get("change_count", 0)),
            created_at=str(data["created_at"]),
            note=str(data.get("note", "")),
        )


class RelabelEventLedger:
    """改标事件台账：内存索引 + JSON 文件持久化。"""

    def __init__(
        self,
        file_path: str | Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = JsonFileRepository(file_path)
        self._clock = clock or datetime.now
        self._events: list[RelabelEvent] = []
        self._receipt_index: dict[str, int] = {}
        self._load()

    @property
    def data_path(self) -> Path:
        """台账落盘文件路径，供同目录的装配稿服务定位存储。"""
        return self._repo.file_path

    # ------------------------------------------------------------------ 持久化
    def _load(self) -> None:
        data = self._repo.load_raw()
        for raw in data.get("events", []):
            event = RelabelEvent.from_dict(raw)
            self._events.append(event)
            if event.receipt_id:
                self._receipt_index[event.receipt_id] = event.event_id
        self._events.sort(key=lambda item: item.event_id)

    def _persist(self) -> None:
        self._repo.save_raw({
            "events": [event.to_dict() for event in self._events],
        })

    def _next_event_id(self) -> int:
        return max((event.event_id for event in self._events), default=0) + 1

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    # ------------------------------------------------------------------ 查询
    def list_events(
        self, segment_id: str | None = None, operator: str | None = None,
    ) -> list[RelabelEvent]:
        result = list(self._events)
        if segment_id is not None:
            result = [event for event in result if event.segment_id == segment_id]
        if operator is not None:
            result = [event for event in result if event.operator == operator]
        return result

    def get_event(self, event_id: int) -> RelabelEvent | None:
        for event in self._events:
            if event.event_id == event_id:
                return event
        return None

    def find_by_receipt(self, receipt_id: str) -> RelabelEvent | None:
        if not receipt_id:
            return None
        event_id = self._receipt_index.get(receipt_id)
        return self.get_event(event_id) if event_id is not None else None

    def events_for_segment(self, segment_id: str) -> list[RelabelEvent]:
        return [event for event in self._events if event.segment_id == segment_id]

    def events_for_operator(self, operator: str) -> list[RelabelEvent]:
        return [event for event in self._events if event.operator == operator]

    def has_manual_conclusion(self, segment_id: str) -> bool:
        return any(
            event.segment_id == segment_id
            and event.source == MANUAL_SOURCE
            and event.applied
            for event in self._events
        )

    def manual_change_count(self, segment_id: str) -> int:
        """改标次数：只统计已采纳的人工改标。"""
        return sum(
            1 for event in self._events
            if event.segment_id == segment_id
            and event.source == MANUAL_SOURCE
            and event.applied
        )

    def effective_event(self, segment_id: str) -> RelabelEvent | None:
        """当前实际生效的改标事件：优先人工结论，否则取最近一次已采纳机器事件。"""
        candidates = [
            event for event in self._events
            if event.segment_id == segment_id and event.applied
        ]
        manual = [event for event in candidates if event.source == MANUAL_SOURCE]
        if manual:
            return manual[-1]
        return candidates[-1] if candidates else None

    def _current_label(self, segment_id: str) -> str:
        event = self.effective_event(segment_id)
        return event.label if event else ""

    # ------------------------------------------------------------------ 写入
    def record_machine(
        self,
        segment_id: str,
        label: str,
        operator: str,
        receipt_id: str,
        *,
        line_no: int = 0,
        confirmed: bool = False,
    ) -> tuple[RelabelEvent, bool, str]:
        """登记一条机器改标/回执。

        返回 (事件, 是否本次新登记, 说明)。重送回执幂等返回原事件；
        人工结论已生效后的迟到机器结果只留痕不采纳。
        """
        if not receipt_id:
            raise ValueError("机器改标事件必须携带回执编号 receipt_id")

        duplicate = self.find_by_receipt(receipt_id)
        if duplicate is not None:
            return duplicate, False, f"回执 {receipt_id} 已登记，重送幂等忽略，不新增改标"

        original_label = self._current_label(segment_id)
        if self.has_manual_conclusion(segment_id):
            # 迟到的机器结果不能更改人工结论：留痕但不采纳、不计数。
            event = RelabelEvent(
                event_id=self._next_event_id(),
                segment_id=segment_id,
                line_no=line_no,
                label=label,
                original_label=original_label,
                source=MACHINE_SOURCE,
                operator=operator,
                receipt_id=receipt_id,
                confirmed=False,
                applied=False,
                change_count=self.manual_change_count(segment_id),
                created_at=self._now(),
                note="人工结论已生效，迟到机器结果不予采纳",
            )
            self._events.append(event)
            self._receipt_index[receipt_id] = event.event_id
            self._persist()
            return event, True, "人工结论已生效，迟到机器结果仅留痕，未更改人工结论"

        event = RelabelEvent(
            event_id=self._next_event_id(),
            segment_id=segment_id,
            line_no=line_no,
            label=label,
            original_label=original_label,
            source=MACHINE_SOURCE,
            operator=operator,
            receipt_id=receipt_id,
            confirmed=confirmed,
            applied=True,
            change_count=self.manual_change_count(segment_id),
            created_at=self._now(),
            note="机器结果已领取，待人工确认" if not confirmed else "机器结果已确认",
        )
        self._events.append(event)
        self._receipt_index[receipt_id] = event.event_id
        self._persist()
        return event, True, "机器改标已登记"

    def record_manual(
        self,
        segment_id: str,
        label: str,
        operator: str,
        *,
        line_no: int = 0,
        original_label: str | None = None,
        receipt_id: str = "",
    ) -> tuple[RelabelEvent, str]:
        """登记人工改标：永远生效并确认，使待确认机器领取失效，改标次数 +1。"""
        if receipt_id and self.find_by_receipt(receipt_id) is not None:
            raise ValueError(f"回执 {receipt_id} 已存在，不能重复登记")

        previous_label = (
            original_label if original_label is not None else self._current_label(segment_id)
        )
        # 人工结论形成后，该分段此前待确认/已采纳的机器事件全部退为未采纳。
        for event in self._events:
            if (
                event.segment_id == segment_id
                and event.source == MACHINE_SOURCE
                and event.applied
            ):
                event.applied = False
                event.note = (event.note + "；" if event.note else "") + "被后续人工改标覆盖"

        change_count = self.manual_change_count(segment_id) + 1
        event = RelabelEvent(
            event_id=self._next_event_id(),
            segment_id=segment_id,
            line_no=line_no,
            label=label,
            original_label=previous_label,
            source=MANUAL_SOURCE,
            operator=operator,
            receipt_id=receipt_id,
            confirmed=True,
            applied=True,
            change_count=change_count,
            created_at=self._now(),
            note="人工改标即时生效",
        )
        self._events.append(event)
        if receipt_id:
            self._receipt_index[receipt_id] = event.event_id
        self._persist()
        return event, f"人工改标已记录，第 {change_count} 次改标"

    def confirm_receipt(self, receipt_id: str) -> tuple[RelabelEvent | None, str]:
        """确认一张机器回执对应的改标。人工结论已生效时确认不改变当前结论。"""
        event = self.find_by_receipt(receipt_id)
        if event is None:
            return None, f"回执 {receipt_id} 不存在"
        if event.source != MACHINE_SOURCE:
            return event, "该回执不是机器改标，无需确认"
        event.confirmed = True
        if not self.has_manual_conclusion(event.segment_id):
            event.applied = True
            event.note = "机器结果已确认"
        else:
            event.applied = False
            event.note = "确认时人工结论已生效，机器结果不采纳"
        self._persist()
        return event, f"回执 {receipt_id} 已确认"

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_events": len(self._events),
            "segments": sorted({event.segment_id for event in self._events}),
            "applied_machine": sum(
                1 for event in self._events
                if event.source == MACHINE_SOURCE and event.applied
            ),
            "manual": sum(
                1 for event in self._events if event.source == MANUAL_SOURCE
            ),
            "rejected_late_machine": sum(
                1 for event in self._events
                if event.source == MACHINE_SOURCE and not event.applied
            ),
        }
