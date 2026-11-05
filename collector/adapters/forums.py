from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from ..models import CollectorError, RawBatch
from .base import Collector


class DcinsideCollector(Collector):
    def collect(self):
        source = self.source
        prefix = {"board": "/board", "mgallery": "/mgallery/board", "mini": "/mini/board"}[
            source["gallery_type"]
        ]
        base = "https://gall.dcinside.com" + prefix
        previous = int(self.state.get("last_seen") or 0)
        head = int(self.state.get("scan_head") or 0)
        page = self.state.get("page", 1)
        below = int(self.state.get("below") or 2**63)
        collected = 0
        for _ in range(source["pages"]):
            self.check()
            url = base + "/lists/?" + urlencode({"id": source["target"], "page": page})
            html = self.http.request("GET", url, robots=True).text
            soup = BeautifulSoup(html, "html.parser")
            rows = soup.select("tr.ub-content")
            if not rows:
                raise CollectorError(
                    "디시 게시글 목록을 찾지 못했습니다. 갤러리 유형·ID 또는 HTML 구조를 확인하세요."
                )
            candidates = []
            for row in rows:
                if row.get("data-type") in ("icon_notice", "icon_survey"):
                    continue
                anchor = row.select_one(".gall_tit a[href*='view']")
                if not anchor:
                    continue
                query = parse_qs(urlparse(anchor.get("href", "")).query)
                if query.get("id", [source["target"]])[0] != source["target"]:
                    continue
                number = query.get("no", [""])[0]
                if number.isdigit():
                    candidates.append(int(number))
            candidates = sorted(set(candidates), reverse=True)
            if not candidates:
                raise CollectorError("디시 게시글 ID를 찾지 못했습니다.")
            head = max(head, candidates[0])
            for number in candidates:
                self.check()
                if number >= below:
                    continue
                if number <= previous:
                    self.end_scan(str(head))
                    self.message = "기존 수집 지점까지 게시글 수집 완료. 댓글은 공식 제공 경로가 없어 파일 가져오기로 지원합니다."
                    return
                if collected >= source["limit"]:
                    self.pause_scan(str(head), page=page, below=below)
                    return
                post_url = base + "/view/?" + urlencode({"id": source["target"], "no": number})
                body = self.http.request("GET", post_url, headers={"Referer": url}, robots=True).text
                yield RawBatch("dcinside", body, {"post_id": number, "url": post_url})
                below = number
                collected += 1
            page += 1
            if not soup.select_one(".bottom_paging_box a, .page_next"):
                self.end_scan(str(head))
                return
        self.pause_scan(str(head), page=page, below=below)


def safe_scuba_link(base, href):
    url = urljoin(base, href)
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ("scubaboard.com", "www.scubaboard.com")
        or parsed.query
        or not parsed.path.startswith("/community/")
    ):
        return None
    return parsed._replace(fragment="").geturl()


class ScubaBoardCollector(Collector):
    def collect(self):
        # A durable bounded queue prevents long threads from starving other sources.
        queue = list(self.state.get("queue", [self.source["target"]]))
        visited = set(self.state.get("visited", []))
        prior = dict(self.state.get("thread_latest", {}))
        latest = dict(prior)
        resume = dict(self.state.get("thread_resume", {}))
        pages = 0
        count = 0
        while queue and pages < self.source["pages"] and count < self.source["limit"]:
            self.check()
            url = queue.pop(0)
            if url in visited:
                continue
            html = self.http.request("GET", url, robots=True).text
            pages += 1
            visited.add(url)
            soup = BeautifulSoup(html, "html.parser")
            thread_match = re.search(r"/threads/[^/]*\.(\d+)(?:/|$)", urlparse(url).path)
            if not thread_match:
                links = []
                for anchor in soup.select(".structItem-title a[href*='/threads/']"):
                    link = safe_scuba_link(url, anchor.get("href", ""))
                    if link and link not in visited and link not in links:
                        links.append(link)
                if not links:
                    raise CollectorError(
                        "ScubaBoard 스레드 목록을 찾지 못했습니다. 게시판/스레드 URL을 확인하세요."
                    )
                queue = links + queue
            else:
                thread_id = thread_match.group(1)
                page_match = re.search(r"/page-(\d+)", urlparse(url).path)
                page = int(page_match.group(1)) if page_match else 1
                messages = soup.select("article.message[data-content], article.message[id^='js-post-']")
                ids = [
                    int(re.search(r"post-(\d+)", tag.get("data-content", "") or tag.get("id", "")).group(1))
                    for tag in messages
                    if re.search(r"post-(\d+)", tag.get("data-content", "") or tag.get("id", ""))
                ]
                yield RawBatch("scubaboard", html, {"thread_id": thread_id, "url": url, "page": page})
                count += len(ids)
                latest[thread_id] = max([latest.get(thread_id, 0)] + ids)
                saved = resume.get(thread_id)
                saved_match = re.search(r"/page-(\d+)", saved or "")
                saved_page = int(saved_match.group(1)) if saved_match else 1
                if page >= saved_page:
                    resume[thread_id] = url
                # Resume at the last page previously read, then walk new next pages.
                # Jumping straight to today's last page could skip a burst of replies.
                if prior.get(thread_id) and page == 1:
                    if saved and saved != url:
                        if saved not in visited and saved not in queue:
                            queue.insert(0, saved)
                        continue
            next_link = soup.select_one(".pageNav-jump--next")
            if next_link:
                link = safe_scuba_link(url, next_link.get("href", ""))
                if link and link not in visited and link not in queue:
                    queue.append(link)
            if len(visited) > 5000 or len(queue) > 5000:
                raise CollectorError(
                    "ScubaBoard 탐색 범위가 5,000페이지를 초과했습니다. 더 좁은 게시판/스레드를 지정하세요."
                )
        if queue:
            self.partial = True
            self.message = (
                "페이지/건수 한도에 도달했습니다. 다음 실행에서 대기 중인 페이지를 이어서 처리합니다."
            )
            self.state = {
                "queue": queue,
                "visited": sorted(visited),
                "thread_latest": latest,
                "thread_resume": resume,
            }
        else:
            self.state = {"thread_latest": latest, "thread_resume": resume}
            self.message = (
                "공개 페이지 수집 완료. 이후 첫 페이지와 마지막 수집 페이지부터 새 댓글을 갱신합니다."
            )
