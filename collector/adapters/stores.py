from __future__ import annotations

from ..models import CollectorError, RawBatch
from ..normalize import play_response
from .base import Collector


class AppStoreCollector(Collector):
    def collect(self):
        previous, head = self.begin_scan()
        page = self.state.get("page", 1)
        seen = 0
        for _ in range(self.source["pages"]):
            self.check()
            url = (
                f"https://itunes.apple.com/{self.source['country']}/rss/customerreviews/"
                f"page={page}/id={self.source['target']}/sortBy=mostRecent/json"
            )
            raw = self.http.json(url)
            if not isinstance(raw, dict) or not isinstance(raw.get("feed"), dict):
                raise CollectorError("App Store RSS 응답 형식이 변경되었습니다.")
            entries = raw["feed"].get("entry", [])
            if isinstance(entries, dict):
                entries = [entries]
            ids = [str(item["id"]["label"]) for item in entries if "im:rating" in item]
            head = head or (ids[0] if ids else None)
            yield RawBatch("app_store", raw, {"page": page, "url": url})
            seen += len(ids)
            # RSS offers only a rolling window (commonly up to 10 pages).
            if not ids or previous in ids or page >= 10:
                self.end_scan(head)
                self.message = "App Store RSS 제공 범위 완료. 과거 전체 리뷰 수집을 보장하지 않습니다."
                return
            page += 1
            if seen >= self.source["limit"]:
                break
        self.pause_scan(head, page=page)


class GooglePlayCollector(Collector):
    def collect(self):
        from google_play_scraper.constants.request import Formats

        previous, head = self.begin_scan()
        token = self.state.get("token")
        seen = 0
        for _ in range(self.source["pages"]):
            self.check()
            count = min(200, self.source["limit"] - seen)
            url = Formats.Reviews.build(lang=self.source["language"], country=self.source["country"])
            data = Formats.Reviews.build_body(self.source["target"], 2, count, "null", "null", token)
            raw = self.http.request(
                "POST", url, headers={"Content-Type": "application/x-www-form-urlencoded"}, data=data
            ).text
            # Use the library's RPC schema with our bounded transport. The library's public
            # reviews() swallows network errors; those must surface as failures here.
            items, next_token = play_response(raw)
            ids = [item[0] for item in items]
            head = head or (ids[0] if ids else None)
            yield RawBatch("google_play", raw, {"url": url})
            seen += len(ids)
            if not ids or not next_token or previous in ids:
                self.end_scan(head)
                self.message = (
                    "Google Play 제공 범위 완료. 국가·언어와 공개 인터페이스에 따라 범위가 달라집니다."
                )
                return
            if next_token == token:
                raise CollectorError(
                    "Google Play 페이지 토큰이 반복되었습니다. 체크포인트를 초기화해 다시 시도하세요."
                )
            token = next_token
            if seen >= self.source["limit"]:
                break
        self.pause_scan(head, token=token)
