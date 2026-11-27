"""pytest 全局夹具：把改标追溯持久化目录隔离到每个用例的临时目录。

既有的工作台服务（如 SpeakerLabelService.manual_override）现在也会写改标台账，
若不隔离会把凭据落到 backend/runtime_data。这里对所有测试统一重定向，
保证全量测试不污染真实运行期目录，用例之间也不共享状态。
"""
from __future__ import annotations

import pytest

from app.pipeline import traceability


@pytest.fixture(autouse=True)
def isolate_traceability(tmp_path):
    traceability.reset_traceability(tmp_path)
    yield
    traceability.reset_traceability()
