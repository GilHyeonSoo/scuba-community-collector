from __future__ import annotations

from ..models import AccessBlocked, CollectorError, RawBatch
from .base import Collector


def extract(obj, comment=False):
    keys = (
        ("name", "body", "created_utc", "permalink", "parent_id", "score")
        if comment
        else (
            "name",
            "title",
            "selftext",
            "created_utc",
            "permalink",
            "score",
            "upvote_ratio",
            "num_comments",
        )
    )
    result = {key: getattr(obj, key, None) for key in keys}
    result["author"] = str(obj.author) if obj.author else None
    return result


class RedditCollector(Collector):
    def collect(self):
        import praw
        import prawcore

        credentials = {
            key: self.secrets.get(key)
            for key in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT")
        }
        if not all(credentials.values()):
            raise CollectorError("인증 설정에서 Reddit client ID·secret·User-Agent를 입력하세요.")
        self.http.gate.wait("oauth.reddit.com", self.http.delay, self.stop)
        reddit = praw.Reddit(
            client_id=credentials["REDDIT_CLIENT_ID"],
            client_secret=credentials["REDDIT_CLIENT_SECRET"],
            user_agent=credentials["REDDIT_USER_AGENT"],
            requestor_kwargs={"timeout": 25},
            check_for_updates=False,
            ratelimit_seconds=0,
        )
        previous, head = self.begin_scan()
        after = self.state.get("after")
        count = 0
        last = None
        exhausted = True
        try:
            listing = reddit.subreddit(self.source["target"]).new(
                limit=self.source["limit"] + 1, params={"after": after} if after else None
            )
            for post in listing:
                self.check()
                if count >= self.source["limit"]:
                    exhausted = False
                    break
                head = head or post.name
                yield RawBatch("reddit", [extract(post)])
                count += 1
                last = post.name
                if self.source.get("comments"):
                    # Bound both additional API expansion and normalized comments.
                    post.comments.replace_more(limit=2)
                    comments = []
                    for comment in post.comments.list():
                        self.check()
                        if hasattr(comment, "body"):
                            comments.append(extract(comment, True))
                        if len(comments) >= self.source["comment_limit"]:
                            break
                    if comments:
                        yield RawBatch("reddit", comments)
                if post.name == previous:
                    break
            if exhausted:
                self.end_scan(head)
                self.message = "최신 글 및 경계 게시글 갱신 완료. Reddit 목록/API 제공 범위 내 수집입니다."
            else:
                self.pause_scan(head, after=last)
        except prawcore.TooManyRequests as exc:
            try:
                seconds = max(1, int(exc.response.headers.get("Retry-After", "900")))
            except (AttributeError, TypeError, ValueError):
                seconds = 900
            self.http.gate.defer("oauth.reddit.com", seconds)
            raise AccessBlocked(f"Reddit 요청 제한. 최소 {seconds}초 뒤 다시 실행하세요.") from exc
        except (prawcore.Forbidden, prawcore.Unauthorized) as exc:
            raise AccessBlocked("Reddit 접근 거절/요청 제한: API 승인·자격증명·할당량을 확인하세요.") from exc
        except prawcore.PrawcoreException as exc:
            raise CollectorError("Reddit API 요청 실패. 연결 상태와 API 설정을 확인하세요.") from exc
        finally:
            reddit.close()
