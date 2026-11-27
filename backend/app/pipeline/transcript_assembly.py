"""转写版本装配服务：为每个转写版本冻结装配稿，并逐行绑定说话人来源凭据。

与「字幕包装配器」的分工：
- SubtitlePackAssembler 负责字幕包条目、版本引用计数与重算标记（既有逻辑不变）；
- 本服务负责「装配稿」本身：装配时逐行（字幕行号/语音段）读取改标事件台账，
  冻结每行实际采用的标签、写入人、原标签与来源事件，形成不可变凭据；
- 字幕包通过 draft_id 关联到实际装配稿，逐份打开即可还原当时正文与写入人；
  重装一份就生成一份新装配稿，旧稿保留以便对照。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.persistence import JsonFileRepository
from app.pipeline.relabel_event_ledger import RelabelEvent, RelabelEventLedger


class AssemblyLine:
    """装配稿中的一行：沿字幕行号冻结实际采用的说话人来源。"""

    def __init__(
        self,
        line_no: int,
        segment_id: str,
        text: str,
        label: str,
        original_label: str,
        source: str,
        operator: str,
        source_event_id: int | None,
        receipt_id: str,
        confirmed: bool,
    ) -> None:
        self.line_no = line_no
        self.segment_id = segment_id
        self.text = text
        self.label = label
        self.original_label = original_label
        self.source = source
        self.operator = operator
        self.source_event_id = source_event_id
        self.receipt_id = receipt_id
        self.confirmed = confirmed

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_no": self.line_no,
            "segment_id": self.segment_id,
            "text": self.text,
            "label": self.label,
            "original_label": self.original_label,
            "source": self.source,
            "operator": self.operator,
            "source_event_id": self.source_event_id,
            "receipt_id": self.receipt_id,
            "confirmed": self.confirmed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AssemblyLine":
        return cls(
            line_no=int(data["line_no"]),
            segment_id=str(data["segment_id"]),
            text=str(data.get("text", "")),
            label=str(data.get("label", "")),
            original_label=str(data.get("original_label", "")),
            source=str(data.get("source", "")),
            operator=str(data.get("operator", "")),
            source_event_id=data.get("source_event_id"),
            receipt_id=str(data.get("receipt_id", "")),
            confirmed=bool(data.get("confirmed", False)),
        )


class AssemblyDraft:
    """一次装配冻结出的不可变稿件。"""

    def __init__(
        self,
        draft_id: str,
        version_id: str,
        task_id: str,
        media_id: str,
        created_at: str,
        lines: list[AssemblyLine],
    ) -> None:
        self.draft_id = draft_id
        self.version_id = version_id
        self.task_id = task_id
        self.media_id = media_id
        self.created_at = created_at
        self.lines = lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "version_id": self.version_id,
            "task_id": self.task_id,
            "media_id": self.media_id,
            "created_at": self.created_at,
            "lines": [line.to_dict() for line in self.lines],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AssemblyDraft":
        return cls(
            draft_id=str(data["draft_id"]),
            version_id=str(data["version_id"]),
            task_id=str(data.get("task_id", "")),
            media_id=str(data.get("media_id", "")),
            created_at=str(data["created_at"]),
            lines=[AssemblyLine.from_dict(raw) for raw in data.get("lines", [])],
        )

    def line_for_segment(self, segment_id: str) -> AssemblyLine | None:
        for line in self.lines:
            if line.segment_id == segment_id:
                return line
        return None

    def line_for_no(self, line_no: int) -> AssemblyLine | None:
        for line in self.lines:
            if line.line_no == line_no:
                return line
        return None


class TranscriptAssemblyService:
    """转写版本装配：读改标台账冻结装配稿，并维护字幕包→装配稿关联。"""

    def __init__(
        self,
        ledger: RelabelEventLedger,
        file_path: str | Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ledger = ledger
        self._repo = JsonFileRepository(file_path)
        self._clock = clock or datetime.now
        self._drafts: dict[str, AssemblyDraft] = {}
        self._pack_links: dict[str, str] = {}
        self._version_drafts: dict[str, list[str]] = {}
        self._load()

    @classmethod
    def companion_to_ledger(
        cls,
        ledger: RelabelEventLedger,
        clock: Callable[[], datetime] | None = None,
    ) -> "TranscriptAssemblyService":
        """在改标台账同目录构建装配稿服务，保证两份凭据落在同一存储位置。"""
        path = ledger.data_path.with_name("assembly_drafts.json")
        return cls(ledger, path, clock=clock)

    # ------------------------------------------------------------------ 持久化
    def _load(self) -> None:
        data = self._repo.load_raw()
        for raw in data.get("drafts", []):
            draft = AssemblyDraft.from_dict(raw)
            self._drafts[draft.draft_id] = draft
            self._version_drafts.setdefault(draft.version_id, []).append(draft.draft_id)
        for pack_id, draft_id in data.get("pack_links", {}).items():
            self._pack_links[pack_id] = draft_id

    def _persist(self) -> None:
        self._repo.save_raw({
            "drafts": [draft.to_dict() for draft in self._drafts.values()],
            "pack_links": dict(self._pack_links),
        })

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    # ------------------------------------------------------------------ 装配
    def assemble(
        self,
        version_id: str,
        task_id: str,
        media_id: str,
        line_specs: list[dict[str, Any]],
    ) -> AssemblyDraft:
        """按行规格装配一份新稿，逐行读取台账中实际生效的改标事件。

        line_specs: [{"line_no": 1, "segment_id": "SPCH-0001", "text": "..."}]
        凭据严格跟随实际正文：每行冻结当时生效事件的标签/写入人/原标签/来源。
        """
        lines: list[AssemblyLine] = []
        for spec in line_specs:
            segment_id = str(spec["segment_id"])
            line_no = int(spec.get("line_no", len(lines) + 1))
            text = str(spec.get("text", ""))
            event = self._ledger.effective_event(segment_id)
            lines.append(self._build_line(line_no, segment_id, text, event))

        sequence = len(self._version_drafts.get(version_id, [])) + 1
        draft_id = f"ASMB-{version_id}-{sequence:03d}"
        draft = AssemblyDraft(
            draft_id=draft_id,
            version_id=version_id,
            task_id=task_id,
            media_id=media_id,
            created_at=self._now(),
            lines=lines,
        )
        self._drafts[draft_id] = draft
        self._version_drafts.setdefault(version_id, []).append(draft_id)
        self._persist()
        return draft

    @staticmethod
    def _build_line(
        line_no: int,
        segment_id: str,
        text: str,
        event: RelabelEvent | None,
    ) -> AssemblyLine:
        if event is None:
            return AssemblyLine(
                line_no=line_no,
                segment_id=segment_id,
                text=text,
                label="",
                original_label="",
                source="",
                operator="",
                source_event_id=None,
                receipt_id="",
                confirmed=False,
            )
        return AssemblyLine(
            line_no=line_no,
            segment_id=segment_id,
            text=text,
            label=event.label,
            original_label=event.original_label,
            source=event.source,
            operator=event.operator,
            source_event_id=event.event_id,
            receipt_id=event.receipt_id,
            confirmed=event.confirmed,
        )

    # ------------------------------------------------------------------ 字幕包关联
    def link_pack(self, pack_id: str, draft_id: str) -> tuple[bool, str]:
        """把字幕包关联到实际装配稿。"""
        if draft_id not in self._drafts:
            return False, f"装配稿 {draft_id} 不存在"
        self._pack_links[pack_id] = draft_id
        self._persist()
        return True, f"字幕包 {pack_id} 已关联装配稿 {draft_id}"

    def draft_for_pack(self, pack_id: str) -> AssemblyDraft | None:
        draft_id = self._pack_links.get(pack_id)
        return self._drafts.get(draft_id) if draft_id else None

    # ------------------------------------------------------------------ 查询
    def get_draft(self, draft_id: str) -> AssemblyDraft | None:
        return self._drafts.get(draft_id)

    def first_draft_for_version(self, version_id: str) -> AssemblyDraft | None:
        draft_ids = self._version_drafts.get(version_id, [])
        return self._drafts.get(draft_ids[0]) if draft_ids else None

    def drafts_for_version(self, version_id: str) -> list[AssemblyDraft]:
        return [
            self._drafts[draft_id]
            for draft_id in self._version_drafts.get(version_id, [])
            if draft_id in self._drafts
        ]

    def line_provenance(self, pack_id: str, line_no: int) -> dict[str, Any] | None:
        """从字幕包还原某一行当时的写入人和原标签。"""
        draft = self.draft_for_pack(pack_id)
        if draft is None:
            return None
        line = draft.line_for_no(line_no)
        if line is None:
            return None
        return {
            "pack_id": pack_id,
            "draft_id": draft.draft_id,
            **line.to_dict(),
        }

    def compare_with_delivery(
        self, pack_id: str, delivered_lines: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """和已发字幕逐行对照：返回每行装配稿标签与交付标签是否一致。"""
        draft = self.draft_for_pack(pack_id)
        if draft is None:
            return []
        delivered_index = {
            int(item["line_no"]): str(item.get("label", "")) for item in delivered_lines
        }
        result = []
        for line in draft.lines:
            delivered_label = delivered_index.get(line.line_no)
            result.append({
                "line_no": line.line_no,
                "segment_id": line.segment_id,
                "draft_label": line.label,
                "delivered_label": delivered_label,
                "match": delivered_label == line.label,
                "operator": line.operator,
                "source": line.source,
            })
        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_drafts": len(self._drafts),
            "total_pack_links": len(self._pack_links),
            "drafts": [draft.to_dict() for draft in self._drafts.values()],
            "pack_links": dict(self._pack_links),
        }
