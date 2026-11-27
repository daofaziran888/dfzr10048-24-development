"""可追溯数据的文件持久化基座。

平台业务数据原本只在内存（app.store），重启即丢失；字幕署名争议要求改标事件、
转写装配稿与字幕包来源记录在服务重启后仍能读回。这里提供一个只依赖标准库的
小型 JSON 文件仓库：

- 写入走「临时文件 + os.replace」原子替换，避免半写文件损坏历史凭据；
- 读取容错：文件缺失返回空态，损坏文件抛错由上层决定，不静默吞掉凭据；
- 子类只负责把领域对象和 dict 互相转换。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class JsonFileRepository:
    """以单个 JSON 文件承载一份可追溯数据。"""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)

    def exists(self) -> bool:
        return self.file_path.exists()

    def load_raw(self) -> dict[str, Any]:
        """读回原始字典；文件不存在时返回空态。"""
        if not self.file_path.exists():
            return {}
        with self.file_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"持久化文件 {self.file_path} 顶层结构必须是对象")
        return data

    def save_raw(self, data: dict[str, Any]) -> None:
        """原子写入：先写同目录临时文件，再 os.replace 覆盖目标。"""
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.file_path.with_name(f".{self.file_path.name}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.file_path)
