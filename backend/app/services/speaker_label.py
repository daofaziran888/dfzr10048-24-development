"""说话人标注写入领域服务：为语音段标注说话人并记录声纹特征值与改标次数，产出说话人标注数据集供转写版本消费。"""
from __future__ import annotations

from typing import Any

from app.pipeline.traceability import relabel_ledger
from app.services.base import BaseModuleService
from app.store import store


class SpeakerLabelService(BaseModuleService):
    MODULE = "speaker_label"
    ENTITY = "标注条目"
    OPERATION_TERM = "写入"
    LIST_FIELDS = ["标注编号", "关联分段", "说话人标识", "声纹特征值", "标注来源", "标注置信度", "改标次数", "标注时间"]
    REQUIRED_FIELDS = ["标注编号", "关联分段", "说话人标识"]
    STATUS_ORDER = ["机器标注", "待人工确认", "已确认", "存在分歧"]
    ACTION_RULES = {"提交确认": "待人工确认", "确认标注": "已确认", "登记分歧": "存在分歧"}
    NEGATIVE_ACTIONS = []

    def manual_override(
        self, label_id: str, new_speaker: str, voiceprint: str,
        operator: str = "人工编辑",
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """人工改标覆盖机器标注，记录改标次数，并持久化改标事件凭据。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("标注编号") == label_id:
                old_count = int(r.get("改标次数", "0"))
                original_label = str(r.get("说话人标识", ""))
                segment_id = str(r.get("关联分段", label_id))
                r["说话人标识"] = new_speaker
                r["声纹特征值"] = voiceprint
                r["标注来源"] = "人工"
                r["标注置信度"] = "0.99"
                r["改标次数"] = str(old_count + 1)
                r["status"] = "已确认"
                r["pending"] = False
                # 同一次人工改标同时写入持久化台账，重启后仍可还原写入人与原标签。
                relabel_ledger().record_manual(
                    segment_id=segment_id,
                    label=new_speaker,
                    operator=operator,
                    original_label=original_label,
                )
                return True, f"标注 {label_id} 已人工改标第 {old_count + 1} 次", dict(r)
        return False, f"标注 {label_id} 不存在", None

    def get_change_history(self, label_id: str) -> int:
        """获取指定标注的改标次数。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("标注编号") == label_id:
                return int(r.get("改标次数", "0"))
        return 0

    def get_by_segment(self, segment_id: str) -> list[dict[str, Any]]:
        """获取指定语音段的所有说话人标注。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("关联分段") == segment_id]

    def get_unconfirmed(self) -> list[dict[str, Any]]:
        """获取所有待人工确认的标注条目。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("status") == "待人工确认"]