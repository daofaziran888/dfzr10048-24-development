"""改标事件台账测试：人工结论优先、回执幂等、迟到机器结果拒收、改标次数与持久化。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.pipeline.relabel_event_ledger import (
    MANUAL_SOURCE,
    MACHINE_SOURCE,
    RelabelEventLedger,
)


class FixedClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def tick(self, seconds: int = 1) -> None:
        self.current += timedelta(seconds=seconds)


@pytest.fixture()
def ledger(tmp_path):
    clock = FixedClock(datetime(2026, 11, 27, 10, 0, 0))
    return RelabelEventLedger(tmp_path / "relabel.json", clock=clock), clock


class TestMachineReceipt:
    def test_machine_label_claimed_before_manual(self, ledger):
        book, clock = ledger
        event, created, msg = book.record_machine(
            "SEG-A", "乙", "转写引擎", "RCP-1",
        )
        assert created
        assert event.applied is True
        assert event.confirmed is False
        assert book.effective_event("SEG-A").label == "乙"

    def test_receipt_redeliver_is_idempotent(self, ledger):
        book, clock = ledger
        first, created1, _ = book.record_machine("SEG-A", "乙", "转写引擎", "RCP-1")
        clock.tick()
        second, created2, msg = book.record_machine("SEG-A", "乙", "转写引擎", "RCP-1")
        assert created1 is True
        assert created2 is False
        assert first.event_id == second.event_id
        assert len(book.list_events(segment_id="SEG-A")) == 1
        assert "幂等" in msg

    def test_machine_requires_receipt(self, ledger):
        book, _ = ledger
        with pytest.raises(ValueError):
            book.record_machine("SEG-A", "乙", "转写引擎", "")


class TestManualWins:
    def test_manual_overrides_unconfirmed_machine(self, ledger):
        book, clock = ledger
        machine_event, _, _ = book.record_machine("SEG-A", "乙", "转写引擎", "RCP-1")
        clock.tick()
        manual_event, msg = book.record_manual("SEG-A", "丙", "编辑小张")
        assert manual_event.applied is True
        assert manual_event.confirmed is True
        assert manual_event.change_count == 1
        # 待确认机器领取被人工结论覆盖
        assert book.get_event(machine_event.event_id).applied is False
        assert book.effective_event("SEG-A").label == "丙"
        assert book.effective_event("SEG-A").source == MANUAL_SOURCE

    def test_late_machine_cannot_change_manual_conclusion(self, ledger):
        book, clock = ledger
        book.record_machine("SEG-A", "乙", "转写引擎", "RCP-1")
        book.record_manual("SEG-A", "丙", "编辑小张")
        clock.tick()
        late_event, created, msg = book.record_machine(
            "SEG-A", "乙", "转写引擎", "RCP-OLD-LATE",
        )
        assert created is True
        assert late_event.applied is False  # 留痕但不采纳
        assert book.effective_event("SEG-A").label == "丙"
        assert "不予采纳" in msg or "未更改人工结论" in msg

    def test_late_machine_redeliver_does_not_increase_change_count(self, ledger):
        book, _ = ledger
        book.record_machine("SEG-A", "乙", "转写引擎", "RCP-1")
        book.record_manual("SEG-A", "丙", "编辑小张")
        count_after_manual = book.manual_change_count("SEG-A")
        # 乙的旧回执重送
        late, _, _ = book.record_machine("SEG-A", "乙", "转写引擎", "RCP-2")
        # 再重送一次（同回执幂等）
        again, created_again, _ = book.record_machine("SEG-A", "乙", "转写引擎", "RCP-2")
        assert late.applied is False
        assert created_again is False
        assert book.manual_change_count("SEG-A") == count_after_manual == 1

    def test_second_manual_increments_count(self, ledger):
        book, _ = ledger
        book.record_manual("SEG-A", "乙", "编辑小张")
        second, msg = book.record_manual("SEG-A", "丙", "编辑小张")
        assert second.change_count == 2
        assert book.manual_change_count("SEG-A") == 2
        assert second.original_label == "乙"

    def test_confirm_receipt_after_manual_keeps_manual(self, ledger):
        book, _ = ledger
        book.record_machine("SEG-A", "乙", "转写引擎", "RCP-1")
        book.record_manual("SEG-A", "丙", "编辑小张")
        event, msg = book.confirm_receipt("RCP-1")
        assert event.confirmed is True
        assert event.applied is False
        assert book.effective_event("SEG-A").label == "丙"

    def test_confirm_machine_receipt_without_manual(self, ledger):
        book, _ = ledger
        event, _, _ = book.record_machine("SEG-A", "乙", "转写引擎", "RCP-1")
        confirmed, msg = book.confirm_receipt("RCP-1")
        assert confirmed.applied is True
        assert confirmed.confirmed is True


class TestOperatorIsolation:
    def test_operator_reads_own_sources(self, ledger):
        book, _ = ledger
        book.record_manual("SEG-A", "丙", "编辑小张")
        book.record_manual("SEG-B", "丁", "编辑小李")
        zhang = book.events_for_operator("编辑小张")
        li = book.events_for_operator("编辑小李")
        assert len(zhang) == 1 and zhang[0].segment_id == "SEG-A"
        assert len(li) == 1 and li[0].segment_id == "SEG-B"


class TestPersistence:
    def test_events_read_back_after_restart(self, tmp_path):
        path = tmp_path / "relabel.json"
        clock = FixedClock(datetime(2026, 11, 27, 10, 0, 0))
        first = RelabelEventLedger(path, clock=clock)
        first.record_machine("SEG-A", "乙", "转写引擎", "RCP-1")
        first.record_manual("SEG-A", "丙", "编辑小张")
        first.record_machine("SEG-A", "乙", "转写引擎", "RCP-LATE")

        # 新实例模拟重启读回
        second = RelabelEventLedger(path)
        assert len(second.list_events()) == 3
        assert second.effective_event("SEG-A").label == "丙"
        assert second.manual_change_count("SEG-A") == 1
        # 重启后回执幂等仍然成立
        _, created, _ = second.record_machine("SEG-A", "乙", "转写引擎", "RCP-1")
        assert created is False
        late = second.find_by_receipt("RCP-LATE")
        assert late.applied is False

    def test_empty_state_when_file_missing(self, tmp_path):
        book = RelabelEventLedger(tmp_path / "missing.json")
        assert book.list_events() == []
        assert book.effective_event("NOPE") is None
