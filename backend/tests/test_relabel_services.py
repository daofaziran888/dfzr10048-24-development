"""服务层测试：改标事件/装配稿服务接入统一资源路由，且工作台人工改标写入台账。"""
from __future__ import annotations

from app.pipeline import traceability
from app.services.assembly_draft import AssemblyDraftService
from app.services.relabel_event import RelabelEventService
from app.services.speaker_label import SpeakerLabelService


class TestRelabelEventService:
    def test_create_machine_event_via_generic_entry(self):
        svc = RelabelEventService()
        entry, missing = svc.create_entry({
            "关联分段": "SEG-A",
            "说话人标签": "乙",
            "写入人": "转写引擎",
            "标注来源": "机器",
            "回执编号": "RCP-1",
        })
        assert not missing
        assert entry["说话人标签"] == "乙"
        assert entry["status"] == "待确认"
        assert entry["是否采纳"] == "是"

    def test_machine_event_requires_receipt(self):
        svc = RelabelEventService()
        _, missing = svc.create_entry({
            "关联分段": "SEG-A",
            "说话人标签": "乙",
            "写入人": "转写引擎",
            "标注来源": "机器",
        })
        assert "回执编号" in missing

    def test_manual_entry_is_confirmed(self):
        svc = RelabelEventService()
        entry, missing = svc.create_entry({
            "关联分段": "SEG-A",
            "说话人标签": "丙",
            "写入人": "编辑小张",
            "标注来源": "人工",
        })
        assert not missing
        assert entry["status"] == "已确认"
        assert entry["改标次数"] == "1"

    def test_late_machine_listed_as_not_applied(self):
        svc = RelabelEventService()
        svc.create_entry({
            "关联分段": "SEG-A", "说话人标签": "乙",
            "写入人": "转写引擎", "标注来源": "机器", "回执编号": "RCP-1",
        })
        svc.create_entry({
            "关联分段": "SEG-A", "说话人标签": "丙",
            "写入人": "编辑小张", "标注来源": "人工",
        })
        late, _ = svc.create_entry({
            "关联分段": "SEG-A", "说话人标签": "乙",
            "写入人": "转写引擎", "标注来源": "机器", "回执编号": "RCP-LATE",
        })
        assert late["是否采纳"] == "否"
        assert late["status"] == "未采纳"
        assert late["abnormal"] is True

    def test_list_filter_by_segment_and_operator(self):
        svc = RelabelEventService()
        svc.create_entry({
            "关联分段": "SEG-A", "说话人标签": "丙",
            "写入人": "编辑小张", "标注来源": "人工",
        })
        svc.create_entry({
            "关联分段": "SEG-B", "说话人标签": "丁",
            "写入人": "编辑小李", "标注来源": "人工",
        })
        items, total = svc.list_entries(filters={"关联分段": "SEG-A"})
        assert total == 1 and items[0]["关联分段"] == "SEG-A"

        own = svc.list_for_operator("编辑小李")
        assert len(own) == 1 and own[0]["说话人标签"] == "丁"

    def test_confirm_action_on_machine_event(self):
        svc = RelabelEventService()
        svc.create_entry({
            "关联分段": "SEG-A", "说话人标签": "乙",
            "写入人": "转写引擎", "标注来源": "机器", "回执编号": "RCP-1",
        })
        entry, _ = svc.run_action(1, "确认事件")
        assert entry["是否确认"] == "是"
        assert entry["status"] == "已确认"


class TestSpeakerLabelWritesLedger:
    def test_manual_override_persists_event(self):
        svc = SpeakerLabelService()
        ok, msg, entry = svc.manual_override(
            "SPKR-0002", "新说话人", "vox_new_001", operator="编辑小王")
        assert ok
        ledger = traceability.relabel_ledger()
        events = ledger.events_for_segment(entry["关联分段"])
        assert len(events) == 1
        assert events[0].label == "新说话人"
        assert events[0].operator == "编辑小王"
        assert events[0].source == "人工"


class TestAssemblyDraftService:
    def test_draft_manual_create_blocked(self):
        svc = AssemblyDraftService()
        _, missing = svc.create_entry({"装配稿编号": "X", "绑定版本": "v1"})
        assert missing  # 只能由装配接口生成

    def test_draft_listed_after_assembly(self):
        ledger = traceability.relabel_ledger()
        assembly = traceability.assembly_service()
        ledger.record_manual("SEG-A", "丙", "编辑小张")
        draft = assembly.assemble("VER-1", "T-1", "M-1", [
            {"line_no": 1, "segment_id": "SEG-A", "text": "正文"}])
        assembly.link_pack("PACK-1", draft.draft_id)

        svc = AssemblyDraftService()
        items, total = svc.list_entries()
        assert total == 1
        assert items[0]["装配稿编号"] == draft.draft_id
        assert items[0]["关联字幕包"] == "PACK-1"
        assert items[0]["装配行数"] == "1"

    def test_view_source_action(self):
        assembly = traceability.assembly_service()
        traceability.relabel_ledger().record_manual("SEG-A", "丙", "编辑小张")
        draft = assembly.assemble("VER-1", "T-1", "M-1", [
            {"line_no": 1, "segment_id": "SEG-A", "text": "正文"}])
        svc = AssemblyDraftService()
        items, _ = svc.list_entries()
        entry, msg = svc.run_action(items[0]["id"], "查看来源")
        assert entry is not None
        assert "来源" in msg
