"""链路11/12 端到端测试：字幕署名争议场景 A 全流程与重启读回。"""
from __future__ import annotations

from app.pipeline.integration import PipelineOrchestrator
from app.pipeline.relabel_event_ledger import RelabelEventLedger
from app.pipeline.transcript_assembly import TranscriptAssemblyService


def _build_orchestrator(tmp_path):
    ledger = RelabelEventLedger(tmp_path / "relabel.json")
    assembly = TranscriptAssemblyService(ledger, tmp_path / "assembly.json")
    return PipelineOrchestrator(relabel_ledger=ledger, assembly_service=assembly), ledger, assembly


class TestScenarioARelabelDispute:
    def test_full_dispute_flow(self, tmp_path):
        orch, ledger, assembly = _build_orchestrator(tmp_path)
        result = orch.link11_speaker_relabel_traceability(
            segment_id="SEG-A",
            machine_label="乙",
            machine_receipt="RCP-YI",
            manual_label="丙",
            operator="编辑小张",
            version_id="VER-9",
            pack_id="PACK-FIRST",
            line_text="争议行正文",
        )
        assert result["ok"]

        # 首包关联首份装配稿
        assert result["first_pack_draft_id"] == result["first_draft"]["draft_id"]
        # 首份装配实际采用已领取的机器标签乙，凭据跟随实际正文
        first_line = result["first_draft"]["lines"][0]
        assert first_line["label"] == "乙"
        assert first_line["source"] == "机器"
        assert first_line["receipt_id"] == "RCP-YI"

        # 人工更正为丙
        assert result["manual_event"]["label"] == "丙"
        assert result["manual_event"]["source"] == "人工"
        assert result["manual_event"]["original_label"] == "乙"

        # 迟到的乙回执：留痕但不采纳，人工结论仍是丙
        assert result["late_event"]["applied"] is False
        assert ledger.effective_event("SEG-A").label == "丙"

        # 旧回执重送幂等，且改标次数仍为 1
        assert result["late_redeliver_idempotent"] is True
        assert result["manual_change_count"] == 1

        # 重装的第二份装配稿采用丙，与首稿不同
        second_line = result["second_draft"]["lines"][0]
        assert second_line["label"] == "丙"
        assert second_line["operator"] == "编辑小张"
        assert result["second_draft"]["draft_id"] != result["first_draft"]["draft_id"]

        # 从字幕包取首份装配第 A 段（行1）来源
        provenance = result["first_line_provenance"]
        assert provenance["label"] == "乙"
        assert provenance["text"] == "争议行正文"

    def test_each_pack_opens_its_own_adopted_events(self, tmp_path):
        orch, ledger, assembly = _build_orchestrator(tmp_path)
        # 首包在人工确认前装配（乙）
        ledger.record_machine("SEG-A", "乙", "转写引擎", "RCP-1")
        first = assembly.assemble("VER-1", "T-1", "M-1", [
            {"line_no": 1, "segment_id": "SEG-A", "text": "正文"}])
        assembly.link_pack("PACK-1", first.draft_id)

        # 人工改丙后重装第二包
        ledger.record_manual("SEG-A", "丙", "编辑小张")
        ledger.record_machine("SEG-A", "乙", "转写引擎", "RCP-LATE")
        second = assembly.assemble("VER-1", "T-1", "M-1", [
            {"line_no": 1, "segment_id": "SEG-A", "text": "正文"}])
        assembly.link_pack("PACK-2", second.draft_id)

        assert assembly.line_provenance("PACK-1", 1)["label"] == "乙"
        assert assembly.line_provenance("PACK-2", 1)["label"] == "丙"
        # 两个字幕包各自打开实际采用的改标事件
        assert assembly.draft_for_pack("PACK-1").draft_id != assembly.draft_for_pack("PACK-2").draft_id


class TestRestartReadback:
    def test_link12_readback_and_line_compare(self, tmp_path):
        orch, ledger, assembly = _build_orchestrator(tmp_path)
        ledger.record_machine("SEG-A", "乙", "转写引擎", "RCP-1")
        ledger.record_manual("SEG-A", "丙", "编辑小张")
        ledger.record_machine("SEG-A", "乙", "转写引擎", "RCP-LATE")
        draft = assembly.assemble("VER-1", "T-1", "M-1", [
            {"line_no": 1, "segment_id": "SEG-A", "text": "正文"}])
        assembly.link_pack("PACK-1", draft.draft_id)

        # 模拟重启：全新实例从持久化文件读回
        restarted_ledger = RelabelEventLedger(tmp_path / "relabel.json")
        restarted_assembly = TranscriptAssemblyService(
            restarted_ledger, tmp_path / "assembly.json")
        restarted_orch = PipelineOrchestrator(
            relabel_ledger=restarted_ledger, assembly_service=restarted_assembly)

        result = restarted_orch.link12_relabel_restart_readback(
            restarted_ledger, restarted_assembly, "PACK-1",
            delivered_lines=[{"line_no": 1, "label": "丙"}],
            operator="编辑小张",
        )
        assert result["ok"]
        assert result["readback_events"] == 3
        assert result["all_match"] is True
        # B（编辑小张）重启后仍读取自己的改标来源
        assert len(result["operator_events"]) == 1
        assert result["operator_events"][0]["label"] == "丙"

    def test_readback_detects_delivery_mismatch(self, tmp_path):
        _, ledger, assembly = _build_orchestrator(tmp_path)
        ledger.record_manual("SEG-A", "丙", "编辑小张")
        draft = assembly.assemble("VER-1", "T-1", "M-1", [
            {"line_no": 1, "segment_id": "SEG-A", "text": "正文"}])
        assembly.link_pack("PACK-1", draft.draft_id)

        restarted_ledger = RelabelEventLedger(tmp_path / "relabel.json")
        restarted_assembly = TranscriptAssemblyService(
            restarted_ledger, tmp_path / "assembly.json")
        comparison = restarted_assembly.compare_with_delivery(
            "PACK-1", [{"line_no": 1, "label": "旧标签乙"}])
        assert comparison[0]["match"] is False
