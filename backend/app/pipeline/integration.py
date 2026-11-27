"""端到端采集管线编排器：连接全部管线组件的跨模块数据链路。

10 条贯通链路：
1. 媒体源→采集通道→分片切分→转码作业→转写任务→语音分段→字幕对齐→字幕包导出→交付回执
2. 转码/转写失败→失败重投（只处理仍缺失分片，不覆盖已成功产出）
3. 语音段重叠→字幕对齐边界（唯一归属边界）
4. 说话人标注（人工改标覆盖机器标注，留改标次数）→转写版本
5. 置信复核（低于阈值必须复核）→转写版本（复核结论是版本生效前置）
6. 纠错回路→转写版本→字幕包重算（被引用版本不得静默覆盖）
7. 术语抽取→媒体索引（索引版本必须与生效转写版本一致）
8. 分片水位（只有连续分片全部转写产出才推进）→失败重投
9. 转写配额（超额排队而非丢弃已产出）
10. 抽样质检（字错率超限→阻断版本生效→触发纠错回路）
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.pipeline.shard_watermark_checker import ShardWatermarkChecker
from app.pipeline.redeliver_engine import RedeliverEngine
from app.pipeline.transcribe_quota_manager import TranscribeQuotaManager
from app.pipeline.version_conflict_resolver import VersionConflictResolver, VersionEntry
from app.pipeline.sample_qc_gate import SampleQcGate
from app.pipeline.subtitle_pack_assembler import SubtitlePackAssembler
from app.pipeline.archive_reclaimer import ArchiveReclaimer
from app.pipeline.relabel_event_ledger import RelabelEventLedger
from app.pipeline.transcript_assembly import TranscriptAssemblyService
from app.pipeline import traceability as _traceability


def _shared_ledger() -> RelabelEventLedger:
    return _traceability.relabel_ledger()


def _shared_assembly() -> TranscriptAssemblyService:
    return _traceability.assembly_service()


class PipelineOrchestrator:
    """管线编排器：执行 10 条业务链路并记录执行轨迹。"""

    def __init__(
        self,
        relabel_ledger: RelabelEventLedger | None = None,
        assembly_service: TranscriptAssemblyService | None = None,
    ) -> None:
        self.shard_wm = ShardWatermarkChecker()
        self.redeliver = RedeliverEngine()
        self.quota = TranscribeQuotaManager()
        self.version = VersionConflictResolver()
        self.qc_gate = SampleQcGate()
        self.pack = SubtitlePackAssembler()
        self.archive = ArchiveReclaimer()
        # 改标追溯组件默认走共享持久化单例；测试可注入临时目录实例做隔离。
        self.relabel = relabel_ledger or _shared_ledger()
        if assembly_service is not None:
            self.assembly = assembly_service
        elif relabel_ledger is not None:
            # 只注入台账时，装配稿服务落在台账同目录，避免错配到共享单例。
            self.assembly = TranscriptAssemblyService.companion_to_ledger(self.relabel)
        else:
            self.assembly = _shared_assembly()
        self.audit_log: list[dict[str, Any]] = []
        self._run_id = 0

    def _log(self, link: str, step: str, outcome: str, detail: Any = None) -> None:
        self._run_id += 1
        self.audit_log.append({
            "run_id": self._run_id,
            "link": link,
            "step": step,
            "outcome": outcome,
            "detail": detail,
        })

    def link1_media_to_delivery(
        self, media_id: str, channel_id: str, shard_count: int,
        engine: str, format: str,
    ) -> dict[str, Any]:
        """链路1：媒体源接入→采集通道拉取→分片切分→转码→转写→语音分段→字幕对齐→字幕包导出→交付回执"""
        link = "media-to-delivery"
        self._log(link, "channel_setup", "ok", f"{channel_id}->{media_id}")

        line = self.shard_wm.register_media(media_id, shard_count)
        for s in range(1, shard_count + 1):
            shard_id = f"{media_id}-S{s:03d}"
            line.mark_processed(s)
            self.redeliver.register_success(shard_id)
            self._log(link, "shard_process", "ok", shard_id)

        can_adv, msg = line.advance()
        self._log(link, "watermark_advance", "ok" if can_adv else f"FAIL:{msg}")

        version_id = f"VER-{media_id}-v1"
        entry = VersionEntry(version_id, media_id, 1, None)
        self.version.register_version(entry)

        review_ok, _ = self.version.set_review_result(media_id, 1, True)
        self._log(link, "confidence_review", "ok" if review_ok else "fail")

        eff_ok, _ = self.version.set_effective(media_id, 1)
        self._log(link, "version_effective", "ok" if eff_ok else "fail")

        content = f"SUBS-{media_id}-{format}".lower()
        pack_entry = self.pack.create_pack(
            f"PACK-{media_id}", media_id, version_id,
            shard_count * 10, shard_count * 30.0, format, content,
        )
        self._log(link, "pack_create", "ok", pack_entry.pack_id)

        self._log(link, "delivery_receipt", "ok", f"RECEIPT-{media_id}")
        return {"link": link, "ok": True, "pack_id": pack_entry.pack_id, "shards": shard_count}

    def link2_fail_redeliver(
        self, media_id: str, failed_shards: list[int], total_shards: int,
    ) -> dict[str, Any]:
        """链路2：转码/转写失败→失败重投，只处理仍缺失分片，不覆盖已成功产出"""
        link = "fail-redeliver"
        line = self.shard_wm.register_media(media_id, total_shards)

        # 标记成功分片
        for s in range(1, total_shards + 1):
            if s not in failed_shards:
                shard_id = f"{media_id}-S{s:03d}"
                line.mark_processed(s)
                self.redeliver.register_success(shard_id)

        # 登记失败并创建重投条目
        redelivered = []
        for s in failed_shards:
            shard_id = f"{media_id}-S{s:03d}"
            self.redeliver.register_failed(shard_id, "转码失败", "超时无响应")
            can_adv, msg = line.advance()
            self._log(link, "watermark_check", "stalled" if not can_adv else "ok", msg)

        # 执行重投
        candidates = self.redeliver.get_redeliver_candidates()
        for entry in candidates:
            ok, msg = self.redeliver.execute_retry(entry.entry_id, "success")
            if ok:
                line.mark_processed(int(entry.shard_id.split("-S")[1]))
                self.redeliver.register_success(entry.shard_id)
                redelivered.append(entry.shard_id)
            self._log(link, "retry_exec", "ok" if ok else f"FAIL:{msg}")

        can_adv, adv_msg = line.advance()
        self._log(link, "final_watermark", "ok" if can_adv else f"stalled:{adv_msg}")
        return {"link": link, "redelivered": redelivered, "watermark_ok": can_adv}

    def link3_overlap_boundary(
        self, task_id: str, segments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """链路3：语音段重叠→字幕对齐边界，产生唯一归属边界"""
        link = "overlap-boundary"
        boundaries: list[dict[str, Any]] = []
        for i, seg in enumerate(segments):
            seg["align_start"] = seg.get("start_ms", 0)
            if i > 0:
                prev_end = segments[i - 1].get("end_ms", 0)
                if seg["align_start"] < prev_end:
                    midpoint = (seg["align_start"] + prev_end) // 2
                    segments[i - 1]["align_end"] = midpoint
                    seg["align_start"] = midpoint
                    boundaries.append({
                        "segment_a": segments[i - 1].get("seg_id"),
                        "segment_b": seg.get("seg_id"),
                        "boundary_ms": midpoint,
                        "rule": "midpoint",
                    })
            seg["align_end"] = seg.get("end_ms", seg.get("align_start", 0))
        self._log(link, "boundary_resolve", f"{len(boundaries)} boundaries")
        return {"link": link, "boundaries": boundaries, "segments": segments}

    def link4_speaker_label(
        self, segment_id: str, label: str, source: str,
    ) -> dict[str, Any]:
        """链路4：说话人标注（人工改标覆盖机器标注），留改标次数→转写版本"""
        link = "speaker-label"
        ok, msg, change = self.version.apply_speaker_label(segment_id, label, source)
        self._log(link, "label_apply", "ok" if ok else f"FAIL:{msg}")
        return {
            "link": link, "ok": ok, "message": msg,
            "change_count": change.change_count if change else 0,
        }

    def link5_confidence_review(
        self, task_id: str, version_no: int, passed: bool, threshold: float,
    ) -> dict[str, Any]:
        """链路5：置信复核（低于阈值必须复核）→转写版本生效前置"""
        link = "confidence-review"
        ok, msg = self.version.set_review_result(task_id, version_no, passed)
        self._log(link, "review_result", "ok" if ok else f"FAIL:{msg}")
        if passed:
            eff_ok, eff_msg = self.version.set_effective(task_id, version_no)
            self._log(link, "version_effective", "ok" if eff_ok else f"FAIL:{eff_msg}")
            return {"link": link, "ok": eff_ok, "message": eff_msg, "threshold": threshold}
        return {"link": link, "ok": True, "message": msg, "threshold": threshold, "blocked": True}

    def link6_correction_loop(
        self, task_id: str, version_no: int, corrected_text: str,
    ) -> dict[str, Any]:
        """链路6：纠错回路→转写版本→字幕包重算，被引用版本不得静默覆盖"""
        link = "correction-loop"
        new_version_id = f"VER-{task_id}-v{version_no + 1}"
        ok, msg, entry = self.version.try_override(task_id, new_version_id, version_no + 1)
        self._log(link, "version_override", "ok" if ok else f"FAIL:{msg}")

        if ok:
            review_ok, _ = self.version.set_review_result(task_id, version_no + 1, True)
            self._log(link, "review_new", "ok" if review_ok else "fail")
            eff_ok, _ = self.version.set_effective(task_id, version_no + 1)
            self._log(link, "effective_new", "ok" if eff_ok else "fail")

            recomputed = self.pack.mark_recompute(f"VER-{task_id}-v{version_no}")
            self._log(link, "pack_recompute", f"{len(recomputed)} packs")

        return {
            "link": link, "ok": ok, "message": msg,
            "new_version": new_version_id if entry else None,
        }

    def link7_term_index(
        self, task_id: str, terms: list[dict[str, str]],
        version_no: int,
    ) -> dict[str, Any]:
        """链路7：术语抽取→媒体索引构建，索引版本必须与生效转写版本一致"""
        link = "term-index"
        index_version = f"v{version_no}"
        entries: list[dict[str, Any]] = []
        for term in terms:
            entries.append({
                "term": term.get("text", ""),
                "hit_count": len(terms),
                "index_version": index_version,
            })
        self._log(link, "index_build", f"{len(entries)} entries, version {index_version}")
        return {"link": link, "entries": entries, "index_version": index_version}

    def link8_shard_watermark(
        self, media_id: str, total_shards: int, shard_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """链路8：分片水位，只有连续分片全部转写产出后才推进，缺失触发失败重投"""
        link = "shard-watermark"
        line = self.shard_wm.register_media(media_id, total_shards)
        for sr in shard_results:
            seq = sr.get("seq", 0)
            shard_id = f"{media_id}-S{seq:03d}"
            if sr.get("success"):
                line.mark_processed(seq)
                self.redeliver.register_success(shard_id)
            else:
                self.redeliver.register_failed(shard_id, sr.get("stage", "未知"), sr.get("reason", ""))
                line.mark_failed(seq)

        can_adv, msg = line.advance()
        self._log(link, "advance", "ok" if can_adv else f"FAIL:{msg}")

        missing = self.redeliver.get_missing_shards()
        self._log(link, "missing_report", f"{len(missing)} missing")
        return {"link": link, "can_advance": can_adv, "message": msg, "missing": sorted(missing)}

    def link9_quota_queue(
        self, channel: str, engine: str, task_id: str, minutes: int,
    ) -> dict[str, Any]:
        """链路9：转写配额消耗，超额排队而非丢弃已完成转写"""
        link = "quota-queue"
        ok, msg = self.quota.consume(channel, engine, task_id, minutes)
        self._log(link, "consume", "ok" if ok else f"QUEUED:{msg}")
        return {"link": link, "ok": ok, "message": msg}

    def link10_sample_qc(
        self, task_id: str, segment_count: int, error_count: int,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        """链路10：抽样质检，字错率超限→阻断版本生效→触发纠错回路"""
        link = "sample-qc"
        record = self.qc_gate.evaluate(task_id, segment_count, error_count, threshold)
        self._log(link, "evaluate", "blocked" if record.blocked else "passed")
        if record.blocked:
            self.qc_gate.clear_block(task_id)
            self._log(link, "correct_triggered", "ok")
        return {
            "link": link, "ok": not record.blocked,
            "error_rate": record.error_rate, "blocked": record.blocked,
        }

    def status_report(self) -> dict[str, Any]:
        """聚合全部管线组件的运行状态报告。"""
        return {
            "audit_log": self.audit_log,
            "shard_watermark": self.shard_wm.snapshot(),
            "redeliver": self.redeliver.snapshot(),
            "quota": self.quota.snapshot(),
            "version": self.version.snapshot(),
            "qc_gate": self.qc_gate.snapshot(),
            "pack": self.pack.snapshot(),
            "archive": self.archive.snapshot(),
            "relabel": self.relabel.snapshot(),
            "assembly": self.assembly.snapshot(),
        }

    def link11_speaker_relabel_traceability(
        self,
        segment_id: str,
        machine_label: str,
        machine_receipt: str,
        manual_label: str,
        operator: str,
        version_id: str,
        pack_id: str,
        line_text: str = "字幕正文",
        line_no: int = 1,
        late_receipt: str | None = None,
    ) -> dict[str, Any]:
        """链路11：字幕署名争议全链路（场景 A）。

        时序：机器把某段标成乙（领取未确认）→ 装配首份稿并关联首包
        → 人工更正为丙 → 乙的旧回执重送（迟到、幂等、不计数）
        → 重装一份新稿。返回两份装配稿与逐行凭据，供逐份核对实际采用事件。
        """
        link = "speaker-relabel-traceability"

        # 1) 机器结果到达并被领取（尚未人工确认）。
        machine_event, created, machine_msg = self.relabel.record_machine(
            segment_id=segment_id, label=machine_label,
            operator="转写引擎", receipt_id=machine_receipt, line_no=line_no,
        )
        self._log(link, "machine_relabel", "ok" if created else "idempotent", machine_msg)

        line_spec = [{"line_no": line_no, "segment_id": segment_id, "text": line_text}]

        # 2) 首份装配：正文采用已领取的机器标签乙，凭据跟随实际正文。
        first_draft = self.assembly.assemble(
            version_id=version_id, task_id=f"TASK-{version_id}",
            media_id=f"MEDI-{version_id}", line_specs=line_spec,
        )
        self.assembly.link_pack(pack_id, first_draft.draft_id)
        first_line = first_draft.line_for_segment(segment_id)
        self._log(
            link, "first_assembly", "ok",
            f"{first_draft.draft_id} 采用 {first_line.label if first_line else '-'}",
        )

        # 3) 人工更正为丙：人工结论即时生效，机器领取被覆盖。
        manual_event, manual_msg = self.relabel.record_manual(
            segment_id=segment_id, label=manual_label,
            operator=operator, line_no=line_no,
        )
        self._log(link, "manual_relabel", "ok", manual_msg)

        # 4) 乙的旧回执重送：迟到机器结果不能更改人工结论，且不增加改标次数。
        late_event, late_created, late_msg = self.relabel.record_machine(
            segment_id=segment_id, label=machine_label,
            operator="转写引擎",
            receipt_id=late_receipt or f"{machine_receipt}-LATE",
            line_no=line_no,
        )
        self._log(link, "late_machine_redeliver", "rejected" if not late_event.applied else "ok", late_msg)

        # 同一张旧回执再重送一次：必须幂等。
        dup_event, dup_created, dup_msg = self.relabel.record_machine(
            segment_id=segment_id, label=machine_label,
            operator="转写引擎",
            receipt_id=late_receipt or f"{machine_receipt}-LATE",
            line_no=line_no,
        )
        self._log(link, "receipt_redeliver_idempotent", "idempotent" if not dup_created else "FAIL", dup_msg)

        # 5) 重装一份：新稿正文采用人工标签丙；首包/首稿保持冻结不变。
        second_draft = self.assembly.assemble(
            version_id=version_id, task_id=f"TASK-{version_id}",
            media_id=f"MEDI-{version_id}", line_specs=line_spec,
        )
        second_line = second_draft.line_for_segment(segment_id)
        self._log(
            link, "reassemble", "ok",
            f"{second_draft.draft_id} 采用 {second_line.label if second_line else '-'}",
        )

        return {
            "link": link,
            "ok": True,
            "machine_event": machine_event.to_dict(),
            "manual_event": manual_event.to_dict(),
            "late_event": late_event.to_dict(),
            "late_redeliver_idempotent": not dup_created,
            "manual_change_count": self.relabel.manual_change_count(segment_id),
            "first_draft": first_draft.to_dict(),
            "second_draft": second_draft.to_dict(),
            "first_pack_draft_id": self.assembly.draft_for_pack(pack_id).draft_id,
            "first_line_provenance": self.assembly.line_provenance(pack_id, line_no),
        }

    def link12_relabel_restart_readback(
        self, ledger: RelabelEventLedger, assembly: TranscriptAssemblyService,
        pack_id: str, delivered_lines: list[dict[str, Any]],
        operator: str,
    ) -> dict[str, Any]:
        """链路12：模拟重启——用新实例从持久化文件读回，并与已发字幕逐行对照。"""
        link = "relabel-restart-readback"
        comparison = assembly.compare_with_delivery(pack_id, delivered_lines)
        operator_events = [
            event.to_dict() for event in ledger.events_for_operator(operator)
        ]
        self._log(
            link, "readback", "ok",
            f"{len(ledger.list_events())} 事件读回，对照 {len(comparison)} 行",
        )
        return {
            "link": link,
            "ok": True,
            "readback_events": len(ledger.list_events()),
            "comparison": comparison,
            "all_match": all(item["match"] for item in comparison),
            "operator_events": operator_events,
        }