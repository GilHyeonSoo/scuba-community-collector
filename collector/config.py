from __future__ import annotations

import copy
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

PLATFORMS = {
    "reddit": "Reddit",
    "google_play": "Google Play",
    "app_store": "App Store",
    "dcinside": "디시인사이드",
    "naver_cafe": "네이버 카페",
    "scubaboard": "ScubaBoard",
}


def data_directory() -> Path:
    override = os.environ.get("SCUBA_COLLECTOR_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SCUBACollector"
    return Path.home() / ".scuba-collector"


def defaults() -> dict:
    return {
        "schema_version": 1,
        "interval_minutes": 60,
        "workers": 3,
        "request_delay": 3.0,
        "retention_days": 30,
        "hash_authors": True,
        "author_salt": secrets.token_hex(32),
        "sources": [],
    }


def new_source(platform="reddit") -> dict:
    return {
        "id": secrets.token_hex(6),
        "name": "새 수집 대상",
        "platform": platform,
        "target": "",
        "enabled": True,
        "limit": 100,
        "pages": 3,
        "country": "kr",
        "language": "ko",
        "comments": False,
        "comment_limit": 100,
        "gallery_type": "mgallery",
        "cafe_url": "",
    }


def validate(config: dict) -> None:
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError("지원하지 않는 설정 형식입니다.")
    for key, low, high in (
        ("interval_minutes", 1, 10080),
        ("workers", 1, 6),
        ("request_delay", 1, 60),
        ("retention_days", 1, 3650),
    ):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not low <= value <= high:
            raise ValueError(f"{key}: {low}~{high} 범위의 숫자가 필요합니다.")
    if not isinstance(config.get("workers"), int) or not isinstance(config.get("retention_days"), int):
        raise ValueError("workers, retention_days는 정수여야 합니다.")
    if not isinstance(config.get("hash_authors"), bool):
        raise ValueError("hash_authors는 true/false여야 합니다.")
    if not re.fullmatch(r"[a-f0-9]{64}", config.get("author_salt", "")):
        raise ValueError("author_salt는 64자리 16진수여야 합니다.")
    if not isinstance(config.get("sources"), list):
        raise ValueError("sources는 배열이어야 합니다.")
    ids = set()
    for source in config["sources"]:
        sid = source.get("id", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", sid) or sid in ids:
            raise ValueError("수집 대상 ID는 고유한 영문·숫자·밑줄이어야 합니다.")
        ids.add(sid)
        platform = source.get("platform")
        if platform not in PLATFORMS:
            raise ValueError("지원하지 않는 플랫폼입니다.")
        if not isinstance(source.get("name"), str) or not source["name"].strip():
            raise ValueError("수집 대상 이름을 입력하세요.")
        if not isinstance(source.get("enabled"), bool) or not isinstance(source.get("comments"), bool):
            raise ValueError("enabled, comments는 true/false여야 합니다.")
        for key, maximum in (("limit", 2000), ("pages", 20), ("comment_limit", 1000)):
            value = source.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{key}: 1~{maximum}의 정수가 필요합니다.")
        target = source.get("target", "").strip()
        if not target:
            raise ValueError("수집 대상을 입력하세요.")
        if platform == "reddit" and not re.fullmatch(r"[A-Za-z0-9_]{2,50}", target):
            raise ValueError("Reddit은 r/를 제외한 서브레딧 이름을 입력하세요.")
        if platform == "dcinside" and (
            not re.fullmatch(r"[A-Za-z0-9_]+", target)
            or source.get("gallery_type") not in ("board", "mgallery", "mini")
        ):
            raise ValueError("디시 갤러리 ID와 유형(board/mgallery/mini)을 확인하세요.")
        if platform == "app_store" and not target.isdigit():
            raise ValueError("App Store 앱 ID는 숫자여야 합니다.")
        if platform == "google_play" and not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", target):
            raise ValueError("Google Play 패키지 ID를 입력하세요. 예: com.example.app")
        if platform in ("app_store", "google_play"):
            if not re.fullmatch(r"[a-z]{2}", source.get("country", "")):
                raise ValueError("국가는 kr/us와 같은 소문자 2자리 코드입니다.")
            if not re.fullmatch(r"[a-z]{2,3}(?:[-_][A-Za-z]{2,4})?", source.get("language", "")):
                raise ValueError("언어 코드를 확인하세요. 예: ko/en")
        if platform == "scubaboard":
            url = urlparse(target)
            if (
                url.scheme != "https"
                or url.hostname not in ("scubaboard.com", "www.scubaboard.com")
                or url.username
                or url.port not in (None, 443)
                or not url.path.startswith("/community/")
                or url.query
                or url.fragment
            ):
                raise ValueError(
                    "ScubaBoard의 https://scubaboard.com/community/ 게시판/스레드 URL을 입력하세요."
                )


class ConfigStore:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.path = self.home / "settings.json"
        self.home.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict:
        if not self.path.exists():
            value = defaults()
            self.save(value)
            return value
        value = json.loads(self.path.read_text(encoding="utf-8-sig"))
        validate(value)
        return value

    def save(self, config: dict) -> None:
        validate(config)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def copy(self):
        return copy.deepcopy(self.read())
