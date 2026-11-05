from __future__ import annotations

import copy

from ..models import Cancelled


class Collector:
    def __init__(self, source, state, secrets, http, stop):
        self.source = source
        self.state = copy.deepcopy(state)
        self.secrets = secrets
        self.http = http
        self.stop = stop
        self.partial = False
        self.message = ""

    def check(self):
        if self.stop.is_set():
            raise Cancelled("사용자가 수집을 중지했습니다.")

    def begin_scan(self):
        return self.state.get("last_seen"), self.state.get("scan_head")

    def end_scan(self, head):
        self.state = {"last_seen": head} if head else {}

    def pause_scan(self, head, **cursor):
        self.partial = True
        self.message = "회차 한도에 도달했습니다. 다음 실행에서 이어서 수집합니다."
        self.state.update(scan_head=head, **cursor)
