"""仅追加的领域事件存储。

- 事件一经写入不可修改、不可删除；状态只能由后继事件改变。
- 每个聚合拥有独立的单调版本号，写入时用 expected_version 做乐观并发控制。
- event_id 全局唯一；离线回传等外部输入可再带幂等键，相同键只接受一次。
"""

import threading
from collections import defaultdict

from src.validator import validate_event


class ConcurrencyConflict(RuntimeError):
    """聚合当前版本与调用方预期版本不一致。"""


class DuplicateEvent(RuntimeError):
    """event_id 或幂等键已被接收过。"""


class EventStore:
    def __init__(self) -> None:
        self._events: list[dict] = []
        self._by_aggregate: dict[str, list[dict]] = defaultdict(list)
        self._event_ids: set[str] = set()
        self._idempotency_keys: dict[str, str] = {}
        self._seq = 0
        self._lock = threading.RLock()

    def append(self, record: dict, expected_version: int | None = None, idempotency_key: str | None = None) -> dict:
        """追加一条事件。

        expected_version 为调用方读取到的该聚合最新版本；若期间有新事件写入则抛
        ConcurrencyConflict，调用方必须重新读取后再决策。版本号由存储统一分配。
        """
        with self._lock:
            return self._append(record, expected_version, idempotency_key)

    def _append(self, record: dict, expected_version: int | None, idempotency_key: str | None) -> dict:
        event_id = record.get("event_id")
        if event_id in self._event_ids:
            raise DuplicateEvent(f"事件已存在：{event_id}")
        if idempotency_key is not None and idempotency_key in self._idempotency_keys:
            raise DuplicateEvent(f"幂等键已处理：{idempotency_key}")

        aggregate_id = record["aggregate_id"]
        current = len(self._by_aggregate[aggregate_id])
        if expected_version is not None and current != expected_version:
            raise ConcurrencyConflict(
                f"聚合 {aggregate_id} 版本冲突：期望 {expected_version}，实际 {current}"
            )

        stored = dict(record)
        if not stored.get("event_id"):
            self._seq += 1
            stored["event_id"] = f"evt-{self._seq:06d}"
        stored["version"] = current + 1

        errors = validate_event(stored)
        if errors:
            # 校验失败：除自增编号外不产生任何状态变化
            raise ValueError("；".join(errors))

        self._events.append(stored)
        self._by_aggregate[aggregate_id].append(stored)
        self._event_ids.add(stored["event_id"])
        if idempotency_key is not None:
            self._idempotency_keys[idempotency_key] = stored["event_id"]
        return stored

    def append_many(self, records: list[tuple[dict, int | None, str | None]]) -> list[dict]:
        """成批追加：任一记录不合法则整批不写入。"""
        with self._lock:
            snapshot = (
                list(self._events),
                {k: list(v) for k, v in self._by_aggregate.items()},
                set(self._event_ids),
                dict(self._idempotency_keys),
                self._seq,
            )
            try:
                return [self._append(record, expected_version, key) for record, expected_version, key in records]
            except Exception:
                old_events, old_by_agg, old_ids, old_keys, old_seq = snapshot
                self._events = old_events
                self._by_aggregate = defaultdict(list, old_by_agg)
                self._event_ids = old_ids
                self._idempotency_keys = old_keys
                self._seq = old_seq
                raise

    def version_of(self, aggregate_id: str) -> int:
        with self._lock:
            return len(self._by_aggregate[aggregate_id])

    def events_for(self, aggregate_id: str) -> list[dict]:
        with self._lock:
            return list(self._by_aggregate[aggregate_id])

    def all_events(self) -> list[dict]:
        with self._lock:
            return list(self._events)
