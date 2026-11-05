"""Cross-process lock: GUI and Task Scheduler must not advance the same cursor."""

from __future__ import annotations

import os
from pathlib import Path


class InstanceLock:
    def __init__(self, home: Path):
        Path(home).mkdir(parents=True, exist_ok=True)
        self.stream = open(Path(home) / "collector.lock", "a+b")
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            self.stream.close()
            raise RuntimeError(
                "같은 저장소를 사용하는 프로그램이 이미 실행 중입니다. 기존 창/예약 작업을 종료하세요."
            ) from exc

    def close(self):
        if self.stream.closed:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
