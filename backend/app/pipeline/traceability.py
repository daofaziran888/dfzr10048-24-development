"""说话人改标可追溯组件的共享单例与装配工厂。

服务层、HTTP 路由和管线编排器都通过这里拿到同一份持久化台账/装配服务，
保证进程内只有一份权威数据，并且全部落盘、重启可读回。
测试需要隔离存储时，用 reset_traceability() 指向临时目录重建。
"""
from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.pipeline.relabel_event_ledger import RelabelEventLedger
from app.pipeline.transcript_assembly import TranscriptAssemblyService

_relabel_ledger: RelabelEventLedger | None = None
_assembly_service: TranscriptAssemblyService | None = None
_runtime_dir: Path = settings.runtime_dir


def _build() -> None:
    global _relabel_ledger, _assembly_service
    _runtime_dir.mkdir(parents=True, exist_ok=True)
    _relabel_ledger = RelabelEventLedger(_runtime_dir / "relabel_events.json")
    _assembly_service = TranscriptAssemblyService(
        _relabel_ledger, _runtime_dir / "assembly_drafts.json",
    )


def relabel_ledger() -> RelabelEventLedger:
    if _relabel_ledger is None:
        _build()
    assert _relabel_ledger is not None
    return _relabel_ledger


def assembly_service() -> TranscriptAssemblyService:
    if _assembly_service is None:
        _build()
    assert _assembly_service is not None
    return _assembly_service


def reset_traceability(runtime_dir: str | Path | None = None) -> None:
    """重置单例：传入目录则改用该目录（测试隔离用），否则用配置目录重建。"""
    global _runtime_dir, _relabel_ledger, _assembly_service
    if runtime_dir is not None:
        _runtime_dir = Path(runtime_dir)
    _relabel_ledger = None
    _assembly_service = None
