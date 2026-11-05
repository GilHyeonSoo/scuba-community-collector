from __future__ import annotations

import threading
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from .models import AccessBlocked, Cancelled, CollectorError, RateLimited


class HostGate:
    """Shared across workers, so two sources never double the per-host rate."""

    def __init__(self, store=None):
        self.lock = threading.Lock()
        self.next_at = {}
        self.store = store
        self.cooldowns = {}

    def defer(self, host, seconds):
        until = time.time() + seconds
        self.cooldowns[host] = max(self.cooldowns.get(host, 0), until)
        if self.store:
            self.store.defer_host(host, until)

    def wait(self, host, delay, stop):
        until = self.store.cooldown(host) if self.store else self.cooldowns.get(host, 0)
        if until > time.time():
            raise RateLimited(
                f"{host} 요청 제한 대기 중입니다. 약 {int(until - time.time()) + 1}초 후 다시 실행하세요."
            )
        with self.lock:
            now = time.monotonic()
            due = max(now, self.next_at.get(host, now))
            self.next_at[host] = due + delay
        if stop.wait(max(0, due - time.monotonic())):
            raise Cancelled("사용자가 수집을 중지했습니다.")


class HttpClient:
    def __init__(self, stop, delay=3, gate=None):
        self.stop = stop
        self.delay = delay
        self.gate = gate or HostGate()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "SCUBACollector/1.0 (local community research)"
        self.robots = {}

    def close(self):
        self.session.close()

    def request(self, method, url, *, headers=None, data=None, robots=False):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.username or parsed.port not in (None, 443):
            raise CollectorError("HTTPS 주소만 지원합니다.")
        if robots:
            self.check_robots(url)
        for attempt in range(3):
            if self.stop.is_set():
                raise Cancelled("사용자가 수집을 중지했습니다.")
            self.gate.wait(parsed.netloc, self.delay, self.stop)
            try:
                response = self.session.request(
                    method, url, headers=headers, data=data, timeout=(8, 25), allow_redirects=False
                )
            except requests.RequestException as exc:
                if attempt < 2:
                    if self.stop.wait(2 ** (attempt + 1)):
                        raise Cancelled("수집이 중지되었습니다.") from exc
                    continue
                raise CollectorError("네트워크 연결/시간 초과: 대상 주소와 연결 상태를 확인하세요.") from exc
            if response.status_code == 429:
                retry = response.headers.get("Retry-After", "서버 지정 시간")[:80]
                try:
                    seconds = max(1, int(retry))
                except ValueError:
                    try:
                        seconds = max(1, parsedate_to_datetime(retry).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        seconds = 900
                self.gate.defer(parsed.netloc, seconds)
                raise RateLimited(f"HTTP 429 요청 제한. Retry-After: {retry}. 다음 실행 전에 대기하세요.")
            if response.status_code in (401, 403):
                raise AccessBlocked(
                    f"HTTP {response.status_code}: 로그인·API 승인·접근 권한 또는 봇 차단을 확인하세요."
                )
            if response.status_code in (301, 302, 303, 307, 308):
                # Redirects can be login gates or can leak API credentials to a different host.
                raise AccessBlocked(
                    "주소가 리디렉션되었습니다. 브라우저에서 최종 공개 URL을 확인해 설정을 수정하세요."
                )
            if 500 <= response.status_code < 600 and attempt < 2:
                if self.stop.wait(2 ** (attempt + 1)):
                    raise Cancelled("수집이 중지되었습니다.")
                continue
            if response.status_code >= 400:
                raise CollectorError(f"HTTP {response.status_code}: 대상 삭제 또는 주소·설정을 확인하세요.")
            if len(response.content) > 15 * 1024 * 1024:
                raise CollectorError("응답이 15MB를 초과했습니다.")
            probe = response.text[:5000].lower()
            if any(
                marker in probe
                for marker in (
                    "/cdn-cgi/challenge-platform",
                    "cf-chl-",
                    "<title>just a moment",
                    "<title>captcha",
                )
            ):
                raise AccessBlocked("캡차/봇 방어 화면이 감지되어 수집을 중지했습니다.")
            return response
        raise CollectorError("요청 재시도 횟수를 초과했습니다.")

    def json(self, url, **kwargs):
        try:
            return self.request("GET", url, **kwargs).json()
        except requests.exceptions.JSONDecodeError as exc:
            raise CollectorError("JSON 대신 다른 응답이 반환되었습니다. API/접근 권한을 확인하세요.") from exc

    def check_robots(self, url):
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self.robots:
            robots_url = urljoin(base, "/robots.txt")
            self.gate.wait(parsed.netloc, self.delay, self.stop)
            try:
                response = self.session.get(robots_url, timeout=(8, 15), allow_redirects=False)
            except requests.RequestException as exc:
                raise AccessBlocked("robots.txt를 확인할 수 없어 공개 HTML 수집을 보류했습니다.") from exc
            parser = RobotFileParser(robots_url)
            if response.status_code == 404:
                parser.parse([])
            elif response.status_code != 200:
                raise AccessBlocked(f"robots.txt HTTP {response.status_code}: 공개 HTML 수집을 보류했습니다.")
            else:
                parser.parse(response.text.splitlines())
            self.robots[base] = parser
        parser = self.robots[base]
        agent = self.session.headers["User-Agent"]
        if not parser.can_fetch(agent, url):
            raise AccessBlocked(
                "robots.txt에서 이 경로의 수집을 허용하지 않습니다. 허가받은 파일 가져오기를 사용하세요."
            )
        crawl_delay = parser.crawl_delay(agent)
        if crawl_delay:
            self.delay = max(self.delay, crawl_delay)
