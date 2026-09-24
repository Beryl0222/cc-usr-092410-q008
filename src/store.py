"""事件存储：仅追加、按聚合乐观并发、全局 event_id 幂等。

同一 event_id 只接受一次；追加同一聚合时必须带对期望版本号（expected_version
为该聚合当前版本，新聚合用 0）。版本冲突由调用方重新加载后重试，保证并发处置
不会产生重复动作，也不会把范围写到不该写的聚合上。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import replace
from typing import Callable, Iterable

from src.events import Event
from src.validator import validate_event


class ConcurrentModificationError(RuntimeError):
    """聚合版本已被其他写入推进，本次提交被拒绝。"""


class DuplicateEventError(RuntimeError):
    """event_id 已存在；相同内容的重复回传由此识别。"""


class EventStore:
    def __init__(self) -> None:
        self._streams: dict[str, list[Event]] = defaultdict(list)
        self._index: dict[str, Event] = {}
        self._lock = threading.RLock()

    # ---- 读取 -------------------------------------------------------------

    def stream(self, aggregate_id: str) -> list[Event]:
        with self._lock:
            return list(self._streams[aggregate_id])

    def version(self, aggregate_id: str) -> int:
        with self._lock:
            return len(self._streams[aggregate_id])

    def all_events(self) -> list[Event]:
        with self._lock:
            events: list[Event] = []
            for stream in self._streams.values():
                events.extend(stream)
            events.sort(key=lambda e: (e.occurred_at, e.event_id))
            return events

    # ---- 写入 -------------------------------------------------------------

    def append(self, event: Event, expected_version: int) -> Event:
        errors = validate_event(event.to_dict())
        if errors:
            raise ValueError("；".join(errors))
        with self._lock:
            if event.event_id in self._index:
                raise DuplicateEventError(event.event_id)
            current = len(self._streams[event.aggregate_id])
            if event.version != current + 1:
                raise ConcurrentModificationError(
                    f"{event.aggregate_id} 期望版本 {current + 1}，收到 {event.version}"
                )
            if expected_version != current:
                raise ConcurrentModificationError(
                    f"{event.aggregate_id} 当前版本 {current}，调用方基于 {expected_version}"
                )
            self._streams[event.aggregate_id].append(event)
            self._index[event.event_id] = event
            return event

    def append_many(self, events: Iterable[Event], expected_versions: dict[str, int] | None = None) -> list[Event]:
        """原子追加多个事件（可跨聚合）。任一校验/版本冲突则整体不写入。"""
        events = list(events)
        expected_versions = expected_versions or {}
        with self._lock:
            snapshot = {k: list(v) for k, v in self._streams.items()}
            index_copy = dict(self._index)
            seen_aggregates: set[str] = set()
            try:
                written: list[Event] = []
                for event in events:
                    errors = validate_event(event.to_dict())
                    if errors:
                        raise ValueError("；".join(errors))
                    if event.event_id in self._index:
                        raise DuplicateEventError(event.event_id)
                    current = len(self._streams[event.aggregate_id])
                    if event.aggregate_id not in seen_aggregates:
                        expected = expected_versions.get(event.aggregate_id)
                        if expected is not None and expected != current:
                            raise ConcurrentModificationError(
                                f"{event.aggregate_id} 当前版本 {current}，调用方基于 {expected}"
                            )
                        seen_aggregates.add(event.aggregate_id)
                    if event.version != current + 1:
                        raise ConcurrentModificationError(
                            f"{event.aggregate_id} 期望版本 {current + 1}，收到 {event.version}"
                        )
                    self._streams[event.aggregate_id].append(event)
                    self._index[event.event_id] = event
                    written.append(event)
                return written
            except Exception:
                self._streams.clear()
                self._streams.update(snapshot)
                self._index = index_copy
                raise

    def has_event(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._index

    def get_event(self, event_id: str) -> Event | None:
        with self._lock:
            return self._index.get(event_id)

    # ---- 并发辅助 ---------------------------------------------------------

    def transact(self, aggregate_id: str, fn: Callable[[list[Event], int], list[Event]]) -> list[Event]:
        """以乐观并发在单个聚合上执行 fn；版本冲突时重试，直至提交成功。

        fn 接收当前流与版本，返回待追加事件（version 可留待此处编号）。
        """
        while True:
            with self._lock:
                stream = list(self._streams[aggregate_id])
                version = len(stream)
            pending = fn(stream, version)
            pending = [
                replace(event, version=version + i + 1) for i, event in enumerate(pending)
            ]
            try:
                with self._lock:
                    if len(self._streams[aggregate_id]) != version:
                        continue
                    return self.append_many(
                        pending, expected_versions={aggregate_id: version}
                    )
            except ConcurrentModificationError:
                continue
