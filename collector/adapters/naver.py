from __future__ import annotations

from urllib.parse import urlencode

from ..models import CollectorError, RawBatch
from .base import Collector


class NaverCollector(Collector):
    def collect(self):
        client_id = self.secrets.get("NAVER_CLIENT_ID")
        secret = self.secrets.get("NAVER_CLIENT_SECRET")
        if not client_id or not secret:
            raise CollectorError("인증 설정에서 네이버 검색 API client ID와 secret을 입력하세요.")
        previous, head = self.begin_scan()
        start = self.state.get("start", 1)
        seen = 0
        for _ in range(self.source["pages"]):
            self.check()
            display = min(100, self.source["limit"] - seen, 1001 - start)
            url = "https://openapi.naver.com/v1/search/cafearticle.json?" + urlencode(
                {"query": self.source["target"], "display": display, "start": start, "sort": "date"}
            )
            raw = self.http.json(
                url, headers={"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": secret}
            )
            if not isinstance(raw.get("items"), list):
                raise CollectorError("네이버 API 오류 또는 응답 구조 변경을 확인하세요.")
            ids = [item["link"] for item in raw["items"]]
            head = head or (ids[0] if ids else None)
            yield RawBatch("naver_cafe", raw, {"start": start})
            seen += len(ids)
            if not ids or previous in ids or start + len(ids) > min(raw.get("total", 0), 1000):
                self.end_scan(head)
                self.message = (
                    "공개 카페 검색 요약 수집 완료(본문·작성자·작성일 미제공, 검색 결과 최대 1,000위)."
                )
                return
            start += len(ids)
            if seen >= self.source["limit"]:
                break
        self.pause_scan(head, start=start)
