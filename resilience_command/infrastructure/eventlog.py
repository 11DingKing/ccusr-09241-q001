"""JSONL 事件日志：每条记录一行，追加写，重启后全量重放。"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Iterable


class JsonlEventLog:
    """事件日志端口实现。

    ``path`` 为 ``None`` 时仅保存在内存（测试与干跑演练用）；
    否则追加写入磁盘文件，服务重启后从同一文件重放恢复。
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self._records.append(json.loads(line))

    def next_seq(self) -> int:
        return len(self._records) + 1

    def append(self, record: dict[str, Any]) -> int:
        with self._lock:
            seq = len(self._records) + 1
            envelope = {"seq": seq, **record}
            self._records.append(envelope)
            if self._path:
                directory = os.path.dirname(os.path.abspath(self._path))
                os.makedirs(directory, exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(envelope, ensure_ascii=False, sort_keys=True) + "\n"
                    )
            return seq

    def records(self) -> Iterable[dict[str, Any]]:
        return list(self._records)
