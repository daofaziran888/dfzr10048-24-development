"""转写装配稿领域服务：把持久化装配稿接入统一资源路由（只读为主）。

装配稿由专用追溯接口（assemble/link/line-provenance）产生，
通用资源接口只负责列表、明细与「查看来源」动作，避免在通用登记入口
绕过改标台账直接写入装配凭据。
"""
from __future__ import annotations

from typing import Any

from app.pipeline.traceability import assembly_service
from app.services.base import BaseModuleService
from app.store import store


class AssemblyDraftService(BaseModuleService):
    MODULE = "assembly_draft"
    ENTITY = "装配稿"
    OPERATION_TERM = "登记"
    LIST_FIELDS = [
        "装配稿编号", "绑定版本", "关联任务", "关联媒体源",
        "装配行数", "生成时间", "关联字幕包",
    ]
    REQUIRED_FIELDS = ["装配稿编号", "绑定版本"]
    STATUS_ORDER = ["已冻结", "查看来源"]
    ACTION_RULES = {"查看来源": "查看来源"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self._hydrated = False

    def _hydrate(self) -> None:
        if self._hydrated:
            return
        rows = store.rows(self.MODULE)
        rows.clear()
        links = self._pack_link_index()
        for index, draft in enumerate(assembly_service().snapshot()["drafts"], start=1):
            linked_packs = [
                pack_id for pack_id, draft_id in links.items()
                if draft_id == draft["draft_id"]
            ]
            rows.append({
                "id": index,
                "装配稿编号": draft["draft_id"],
                "绑定版本": draft["version_id"],
                "关联任务": draft["task_id"],
                "关联媒体源": draft["media_id"],
                "装配行数": str(len(draft["lines"])),
                "生成时间": draft["created_at"],
                "关联字幕包": "、".join(linked_packs) or "-",
                "status": "已冻结",
                "pending": False,
                "abnormal": False,
            })
        self._hydrated = True

    @staticmethod
    def _pack_link_index() -> dict[str, str]:
        return dict(assembly_service().snapshot()["pack_links"])

    def list_entries(
        self, *, filters: dict[str, str] | None = None, keyword: str | None = None,
        status: str | None = None, page: int = 1, size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        self._hydrated = False  # 装配稿/关联可能在追溯接口中新增，每次重新镜像
        self._hydrate()
        return super().list_entries(
            filters=filters, keyword=keyword, status=status, page=page, size=size)

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        self._hydrate()
        rows = store.rows(self.MODULE)
        for row in rows:
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        # 装配稿只能由装配接口读改标台账生成，禁止手工登记无凭据稿件。
        return None, ["装配稿仅可通过追溯装配接口生成"]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action != "查看来源":
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        return entry, f"{self.ENTITY}来源凭据可通过追溯接口逐行查看"

    def draft_detail(self, draft_id: str) -> dict[str, Any] | None:
        draft = assembly_service().get_draft(draft_id)
        return draft.to_dict() if draft else None
