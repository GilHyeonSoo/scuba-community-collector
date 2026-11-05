from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from .models import CollectorError, RawBatch, Record


def clean(value) -> str:
    soup = BeautifulSoup(str(value or ""), "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for tag in soup.find_all("br"):
        tag.replace_with("\n")
    return re.sub(r"[ \t]+", " ", soup.get_text(" ", strip=True)).strip()


def text(node) -> str:
    return clean(str(node)) if node else ""


def utc(value, naive_offset=0) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, timezone.utc)
    else:
        value = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            dt = None
            for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(value, fmt)
                    break
                except ValueError:
                    pass
            if dt is None:
                raise CollectorError(f"작성 시각 형식이 변경되었습니다: {value[:60]}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=naive_offset)))
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def play_response(raw: str) -> tuple[list, str | None]:
    import json

    from google_play_scraper.constants.regex import Regex

    try:
        match = Regex.REVIEWS.findall(raw)
        if not match:
            raise ValueError("RPC result missing")
        outer = json.loads(match[0])
        result = json.loads(outer[0][2])
        if not isinstance(result, list):
            raise ValueError("RPC result not a list")
        items = result[0] or []
        token = None
        if len(result) > 1:
            try:
                token = result[-2][-1]
            except (TypeError, IndexError):
                pass
        if not isinstance(token, str):
            token = None
        return items, token
    except (ValueError, IndexError, TypeError) as exc:
        raise CollectorError("Google Play 응답 구조가 변경되었거나 요청이 거절되었습니다.") from exc


def _reddit(batch, source):
    for item in batch.payload:
        kind = "comment" if item.get("name", "").startswith("t1_") else "post"
        native = item["name"]
        body = item.get("body", item.get("selftext", ""))
        deleted = body in ("[deleted]", "[removed]")
        yield Record(
            f"reddit_{native}",
            native,
            "reddit",
            kind,
            None if kind == "comment" or deleted else item.get("title"),
            "" if deleted else body,
            None if deleted else item.get("author"),
            utc(item.get("created_utc")),
            urljoin("https://www.reddit.com", item.get("permalink", "")),
            f"reddit_{item['parent_id']}" if item.get("parent_id") else None,
            {
                "score": item.get("score"),
                "upvote_ratio": item.get("upvote_ratio"),
                "subreddit": source["target"],
                "deleted_or_removed": deleted,
            },
        )


def _app_store(batch, source):
    entries = batch.payload.get("feed", {}).get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]
    for entry in entries:
        if "im:rating" not in entry:
            continue
        native = str(entry["id"]["label"])
        app = source["target"]
        yield Record(
            f"app_store_{app}_{native}",
            native,
            "app_store",
            "review",
            entry.get("title", {}).get("label"),
            entry.get("content", {}).get("label", ""),
            entry.get("author", {}).get("name", {}).get("label"),
            utc(entry.get("updated", {}).get("label")),
            f"https://apps.apple.com/{source['country']}/app/id{app}",
            extra_metadata={
                "rating": int(entry["im:rating"]["label"]),
                "app_version": entry.get("im:version", {}).get("label"),
                "app_id": app,
                "country": source["country"],
            },
        )


def _google_play(batch, source):
    from google_play_scraper.constants.element import ElementSpecs

    items, _ = play_response(batch.payload)
    for item in items:
        obj = {
            key: spec.extract_content(item)
            for key, spec in ElementSpecs.Review.items()
            if key not in ("at", "repliedAt")
        }
        native = obj.get("reviewId")
        if not native:
            raise CollectorError("Google Play reviewId가 누락되었습니다.")
        app = source["target"]
        yield Record(
            f"google_play_{app}_{native}",
            native,
            "google_play",
            "review",
            None,
            obj.get("content") or "",
            obj.get("userName"),
            utc(item[5][0]),
            f"https://play.google.com/store/apps/details?id={app}&reviewId={native}",
            extra_metadata={
                "rating": obj.get("score"),
                "app_version": obj.get("appVersion"),
                "thumbs_up_count": obj.get("thumbsUpCount"),
                "app_id": app,
                "country": source["country"],
                "language": source["language"],
            },
        )


def _naver(batch, source):
    for item in batch.payload.get("items", []):
        if source.get("cafe_url") and item.get("cafeurl", "").rstrip("/") != source["cafe_url"].rstrip("/"):
            continue
        link = item["link"]
        parsed = urlparse(link)
        query = parse_qs(parsed.query)
        if query.get("clubid") and query.get("articleid"):
            native = query["clubid"][0] + "_" + query["articleid"][0]
        elif parsed.hostname in ("cafe.naver.com", "m.cafe.naver.com") and re.fullmatch(
            r"/[^/]+/\d+/?", parsed.path
        ):
            native = parsed.path.strip("/").replace("/", "_")
        else:
            native = hashlib.sha256(link.encode()).hexdigest()[:32]
        yield Record(
            f"naver_cafe_{native}",
            native,
            "naver_cafe",
            "post",
            item.get("title"),
            item.get("description", ""),
            None,
            None,
            link,
            extra_metadata={
                "content_scope": "search_snippet",
                "cafename": item.get("cafename"),
                "cafeurl": item.get("cafeurl"),
                "query": source["target"],
                "created_at_unavailable": True,
            },
        )


def _dcinside(batch, source):
    soup = BeautifulSoup(batch.payload, "html.parser")
    content = soup.select_one(".write_div, .thum-txt, .write_view")
    if content is None:
        raise CollectorError("디시 본문을 찾지 못했습니다. 삭제·접근 제한 또는 HTML 변경을 확인하세요.")
    native = str(batch.context["post_id"])
    gallery = source["target"]
    date = soup.select_one(".gall_date, .date")
    author = soup.select_one(".gall_writer, .gallview_head .nick, .gallview-tit-box .nick")
    when = date.get("title") or date.get_text(strip=True) if date else None
    yield Record(
        f"dcinside_{gallery}_{native}",
        native,
        "dcinside",
        "post",
        text(soup.select_one(".title_subject, .tit")),
        text(content),
        author.get("data-nick") or text(author) if author else None,
        utc(when, 9),
        batch.context["url"],
        extra_metadata={
            "gallery": gallery,
            "gallery_type": source["gallery_type"],
            "recommend_count": text(soup.select_one(".up_num, .gall_recommend")),
            "content_scope": "post_only",
        },
    )


def _scubaboard(batch, source):
    soup = BeautifulSoup(batch.payload, "html.parser")
    messages = soup.select("article.message[data-content], article.message[id^='js-post-']")
    if not messages:
        raise CollectorError("ScubaBoard 게시글을 찾지 못했습니다. 접근 제한 또는 HTML 변경을 확인하세요.")
    title = text(soup.select_one("h1.p-title-value"))
    thread = batch.context["thread_id"]
    for message in messages:
        raw_id = message.get("data-content", "") or message.get("id", "")
        match = re.search(r"(?:post-|js-post-)(\d+)", raw_id)
        content = message.select_one(".message-body .bbWrapper, .message-body")
        if not match or not content:
            raise CollectorError("ScubaBoard 메시지 구조가 변경되었습니다.")
        native = match.group(1)
        date = message.select_one("time[datetime]")
        is_first = bool(message.select_one("[itemprop='position'][content='1']")) or (
            batch.context.get("page", 1) == 1 and message is messages[0]
        )
        yield Record(
            f"scubaboard_{native}",
            native,
            "scubaboard",
            "post" if is_first else "comment",
            title if is_first else None,
            text(content),
            message.get("data-author") or text(message.select_one(".username")),
            utc(date.get("datetime")) if date else None,
            f"https://scubaboard.com/community/posts/{native}/",
            None if is_first else f"scubaboard_thread_{thread}",
            {"thread_id": thread, "thread_title": title, "parent_scope": "thread"},
        )


def _import(batch, source):
    for item in batch.payload:
        platform = item.get("platform", source["platform"])
        if platform != source["platform"]:
            raise CollectorError("가져오기 파일의 platform과 선택한 수집 대상이 다릅니다.")
        native = str(item.get("native_id", "")).strip()
        if not native:
            raise CollectorError("가져오기에는 native_id가 필요합니다.")
        # Full canonical IDs survive a normalized export/import round trip.
        identifier = item.get("id") or f"{platform}_{source['target']}_{native}"
        if not isinstance(identifier, str) or not identifier.startswith(platform + "_"):
            raise CollectorError("가져오기 id는 platform_으로 시작해야 합니다.")
        yield Record(
            identifier,
            native,
            platform,
            item.get("content_type", "post"),
            item.get("title"),
            item.get("body", ""),
            item.get("author"),
            utc(item.get("created_at")),
            item.get("source_url", ""),
            item.get("parent_id"),
            {
                **item.get("extra_metadata", {}),
                "imported": True,
                "content_scope": item.get("extra_metadata", {}).get("content_scope", "authorized_import"),
            },
        )


TRANSFORMS = {
    "reddit": _reddit,
    "app_store": _app_store,
    "google_play": _google_play,
    "naver_cafe": _naver,
    "dcinside": _dcinside,
    "scubaboard": _scubaboard,
    "import": _import,
}


def normalize(batch: RawBatch, source: dict, config: dict) -> list[Record]:
    if batch.kind not in TRANSFORMS:
        raise CollectorError("알 수 없는 원본 형식입니다.")
    result = list(TRANSFORMS[batch.kind](batch, source))
    for record in result:
        if record.content_type not in ("post", "comment", "review"):
            raise CollectorError("content_type은 post/comment/review여야 합니다.")
        record.body = clean(record.body) if batch.kind != "reddit" else record.body.strip()
        record.title = clean(record.title) if record.title is not None else None
        if record.author in ("[deleted]", "", None):
            record.author = None
        elif config["hash_authors"]:
            # Idempotent for records exported by this installation.
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(record.author)):
                message = (record.platform + ":" + str(record.author)).encode()
                record.author = (
                    "sha256:"
                    + hmac.new(bytes.fromhex(config["author_salt"]), message, hashlib.sha256).hexdigest()
                )
    return result
