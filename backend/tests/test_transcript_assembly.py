"""转写装配测试：装配稿逐行凭据、字幕包关联、重装冻结与逐行对照。"""
from __future__ import annotations

import pytest

from app.pipeline.relabel_event_ledger import RelabelEventLedger
from app.pipeline.transcript_assembly import TranscriptAssemblyService


@pytest.fixture()
def context(tmp_path):
    ledger = RelabelEventLedger(tmp_path / "relabel.json")
    assembly = TranscriptAssemblyService(ledger, tmp_path / "assembly.json")
    return ledger, assembly


LINES = [
    {"line_no": 1, "segment_id": "SEG-A", "text": "第一行正文"},
    {"line_no": 2, "segment_id": "SEG-B", "text": "第二行正文"},
]


class TestAssemblyProvenance:
    def test_first_assembly_uses_claimed_machine_label(self, context):
        ledger, assembly = context
        ledger.record_machine("SEG-A", "乙", "转写引擎", "RCP-1")
        draft = assembly.assemble("VER-1", "TASK-1", "MEDI-1", LINES)
        line_a = draft.line_for_no(1)
        # 首份装配采用已领取（尚未确认）的机器标签乙，凭据跟随实际正文
        assert line_a.label == "乙"
        assert line_a.source == "机器"
        assert line_a.operator == "转写引擎"
        assert line_a.receipt_id == "RCP-1"
        assert line_a.confirmed is False

    def test_reassemble_after_manual_uses_manual_label(self, context):
        ledger, assembly = context
        ledger.record_machine("SEG-A", "乙", "转写引擎", "RCP-1")
        first = assembly.assemble("VER-1", "TASK-1", "MEDI-1", LINES)
        ledger.record_manual("SEG-A", "丙", "编辑小张")
        second = assembly.assemble("VER-1", "TASK-1", "MEDI-1", LINES)

        assert first.draft_id != second.draft_id
        # 旧稿冻结在乙，新稿采用丙
        assert first.line_for_no(1).label == "乙"
        assert second.line_for_no(1).label == "丙"
        assert second.line_for_no(1).operator == "编辑小张"
        assert second.line_for_no(1).original_label == "乙"
        # 同版本两份装配稿都可枚举
        drafts = assembly.drafts_for_version("VER-1")
        assert [d.draft_id for d in drafts] == [first.draft_id, second.draft_id]
        assert assembly.first_draft_for_version("VER-1").draft_id == first.draft_id

    def test_pack_links_to_actual_draft(self, context):
        ledger, assembly = context
        ledger.record_manual("SEG-A", "丙", "编辑小张")
        draft = assembly.assemble("VER-1", "TASK-1", "MEDI-1", LINES)
        ok, msg = assembly.link_pack("PACK-1", draft.draft_id)
        assert ok
        assert assembly.draft_for_pack("PACK-1").draft_id == draft.draft_id

    def test_link_unknown_draft_rejected(self, context):
        _, assembly = context
        ok, msg = assembly.link_pack("PACK-X", "ASMB-NOPE")
        assert not ok
        assert "不存在" in msg

    def test_line_provenance_restores_writer_and_original(self, context):
        ledger, assembly = context
        ledger.record_machine("SEG-A", "乙", "转写引擎", "RCP-1")
        ledger.record_manual("SEG-A", "丙", "编辑小张")
        draft = assembly.assemble("VER-1", "TASK-1", "MEDI-1", LINES)
        assembly.link_pack("PACK-1", draft.draft_id)
        provenance = assembly.line_provenance("PACK-1", 1)
        assert provenance["label"] == "丙"
        assert provenance["original_label"] == "乙"
        assert provenance["operator"] == "编辑小张"
        assert provenance["segment_id"] == "SEG-A"

    def test_compare_with_delivery_line_by_line(self, context):
        ledger, assembly = context
        ledger.record_manual("SEG-A", "丙", "编辑小张")
        ledger.record_machine("SEG-B", "丁", "转写引擎", "RCP-9", confirmed=True)
        draft = assembly.assemble("VER-1", "TASK-1", "MEDI-1", LINES)
        assembly.link_pack("PACK-1", draft.draft_id)

        delivered = [
            {"line_no": 1, "label": "丙"},
            {"line_no": 2, "label": "错误标签"},
        ]
        comparison = assembly.compare_with_delivery("PACK-1", delivered)
        assert comparison[0]["match"] is True
        assert comparison[1]["match"] is False
        assert comparison[1]["draft_label"] == "丁"

    def test_persistence_read_back_after_restart(self, tmp_path):
        relabel_path = tmp_path / "relabel.json"
        assembly_path = tmp_path / "assembly.json"
        ledger = RelabelEventLedger(relabel_path)
        assembly = TranscriptAssemblyService(ledger, assembly_path)
        ledger.record_machine("SEG-A", "乙", "转写引擎", "RCP-1")
        ledger.record_manual("SEG-A", "丙", "编辑小张")
        ledger.record_machine("SEG-A", "乙", "转写引擎", "RCP-LATE")
        draft = assembly.assemble("VER-1", "TASK-1", "MEDI-1", LINES)
        assembly.link_pack("PACK-1", draft.draft_id)

        # 重启：两个新实例从文件读回
        ledger2 = RelabelEventLedger(relabel_path)
        assembly2 = TranscriptAssemblyService(ledger2, assembly_path)
        read_back = assembly2.draft_for_pack("PACK-1")
        assert read_back is not None
        assert read_back.line_for_no(1).label == "丙"
        assert len(ledger2.list_events()) == 3
        provenance = assembly2.line_provenance("PACK-1", 1)
        assert provenance["operator"] == "编辑小张"
        assert provenance["original_label"] == "乙"
