"""只追加记录存储。

事件与决策以 JSONL 形式顺序写入单一日志文件，重启后全量重放即可恢复。
写入采用行级原子追加；存储是唯一的可变基础设施，便于替换为其它实现。
"""

from __future__ import annotations

import json
import os
from typing import Any


class EventStore:
    """JSONL 追加日志。"""

    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        # 单行写入 + flush/fsync，保证崩溃后记录完整或不存在
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read_all(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        records: list[dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def reset(self) -> None:
        if os.path.exists(self.path):
            os.remove(self.path)


class InMemoryStore:
    """测试与稳定重放自检使用的内存存储。"""

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    def append(self, record: dict[str, Any]) -> None:
        self._records.append(json.loads(json.dumps(record, ensure_ascii=False)))

    def read_all(self) -> list[dict[str, Any]]:
        return list(self._records)

    def reset(self) -> None:
        self._records.clear()
