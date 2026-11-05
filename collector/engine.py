from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .adapters import COLLECTORS
from .http import HostGate, HttpClient
from .models import AccessBlocked, Cancelled, RawBatch
from .normalize import normalize


class Engine:
    def __init__(self, store, config, secrets, callback=None):
        self.store = store
        self.config = config
        self.secrets = secrets
        self.callback = callback or (lambda event: None)
        self.stop = threading.Event()
        self.gate = HostGate(store)
        self.reddit_lock = threading.Lock()

    def emit(self, **event):
        self.callback(event)

    def message(self, exc):
        # Avoid persisting a library exception that might contain credentials/headers.
        from .models import CollectorError

        return (
            str(exc)[:500]
            if isinstance(exc, (CollectorError, ValueError))
            else (f"{type(exc).__name__}: 처리 실패. 설정·원본 및 프로그램 버전을 확인하세요.")
        )

    def transform(self, run_id, source, batch):
        raw_id, path = self.store.stage(run_id, source, batch)
        try:
            records = normalize(batch, source, self.config)
            counts = self.store.commit_batch(raw_id, path, source["id"], run_id, records)
            self.emit(kind="batch", source=source["name"], **counts)
        except Exception as exc:
            self.store.fail_batch(raw_id, self.message(exc))
            raise

    def run_source(self, source):
        run_id = self.store.start_run(source)
        self.emit(kind="start", source=source["name"])
        client = HttpClient(self.stop, self.config["request_delay"], self.gate)
        status = "success"
        message = ""
        reddit_locked = False
        try:
            if self.stop.is_set():
                raise Cancelled("수집이 중지되었습니다.")
            if source["platform"] == "reddit":
                # PRAW rate accounting belongs to one instance. Serialize sources that
                # share the same OAuth client rather than multiplying its request budget.
                while not self.reddit_lock.acquire(timeout=0.2):
                    if self.stop.is_set():
                        raise Cancelled("수집이 중지되었습니다.")
                reddit_locked = True
            adapter = COLLECTORS[source["platform"]](
                source, self.store.checkpoint(source["id"]), self.secrets, client, self.stop
            )
            for batch in adapter.collect():
                self.transform(run_id, source, batch)
            if self.stop.is_set():
                raise Cancelled("수집이 중지되었습니다.")
            status = "partial" if adapter.partial else "success"
            message = adapter.message
            self.store.finish_run(run_id, status, message, adapter.state, source["id"])
        except Exception as exc:
            status = (
                "cancelled"
                if isinstance(exc, Cancelled)
                else "blocked"
                if isinstance(exc, AccessBlocked)
                else "error"
            )
            message = self.message(exc)
            self.store.finish_run(run_id, status, message)
        finally:
            client.close()
            if reddit_locked:
                self.reddit_lock.release()
        self.emit(kind="finish", source=source["name"], status=status, message=message)
        return status

    def run(self, source_ids=None):
        sources = [
            source
            for source in self.config["sources"]
            if source["enabled"] and (source_ids is None or source["id"] in source_ids)
        ]
        if not sources:
            self.emit(kind="notice", message="활성화된 수집 대상이 없습니다. 수집 대상 탭에서 추가하세요.")
            return []
        self.store.purge(self.config["retention_days"])
        with ThreadPoolExecutor(max_workers=self.config["workers"], thread_name_prefix="collector") as pool:
            futures = [pool.submit(self.run_source, source) for source in sources]
            try:
                statuses = [future.result() for future in as_completed(futures)]
            except KeyboardInterrupt:
                self.stop.set()
                for future in futures:
                    future.cancel()
                raise
        return statuses

    def replay(self):
        results = []
        for row in self.store.pending():
            if self.stop.is_set():
                break
            try:
                source, batch = self.store.load_raw(row["path"])
                records = normalize(batch, source, self.config)
                self.store.commit_batch(row["id"], row["path"], row["source_id"], row["run_id"], records)
                self.emit(kind="notice", message=f"원본 재처리 완료: {source['name']}")
                results.append("success")
            except Exception as exc:
                self.store.fail_batch(row["id"], self.message(exc))
                self.emit(kind="notice", message=self.message(exc))
                results.append("error")
        return results

    def import_file(self, source, path):
        path = Path(path)
        if path.stat().st_size > 50 * 1024 * 1024:
            raise ValueError("가져오기 파일은 50MB 이하여야 합니다. 큰 파일은 나누어 가져오세요.")
        if path.suffix.lower() == ".jsonl":
            payload = [
                json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()
            ]
        else:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise ValueError("JSON 배열 또는 한 줄에 객체 하나인 JSONL 파일이 필요합니다.")
        run_id = self.store.start_run(source)
        try:
            for offset in range(0, len(payload), 200):
                if self.stop.is_set():
                    raise Cancelled("가져오기가 중지되었습니다.")
                self.transform(run_id, source, RawBatch("import", payload[offset : offset + 200]))
            self.store.finish_run(run_id, "success", f"권한 확보 데이터 파일 가져오기: {len(payload)}건")
        except Exception as exc:
            self.store.finish_run(run_id, "error", self.message(exc))
            raise
        return len(payload)
