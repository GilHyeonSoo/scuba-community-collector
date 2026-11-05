from __future__ import annotations

import copy
import csv
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from collector.adapters.base import Collector
from collector.adapters.forums import DcinsideCollector, ScubaBoardCollector
from collector.adapters.naver import NaverCollector
from collector.adapters.reddit import RedditCollector
from collector.adapters.stores import AppStoreCollector, GooglePlayCollector
from collector.config import ConfigStore, defaults, new_source, validate
from collector.engine import Engine
from collector.http import HostGate, HttpClient
from collector.lock import InstanceLock
from collector.models import AccessBlocked, CollectorError, RateLimited, RawBatch
from collector.normalize import normalize, play_response, utc
from collector.secrets import SecretStore, _dpapi
from collector.storage import Store

FIXTURES = Path(__file__).parent / "fixtures"


def make_source(platform="reddit", target="scuba"):
    value = new_source(platform)
    value.update(id="test_" + platform, name="Test " + platform, target=target)
    return value


def reddit_item(native="t3_123", body="hello"):
    return {
        "name": native,
        "selftext": body,
        "title": "Dive",
        "author": "example",
        "created_utc": 1793836800,
        "permalink": "/r/scuba/comments/123",
        "score": 3,
    }


def play_rpc(native="review-1", token=None):
    item = [
        native,
        ["Diver", [None, None, None, [None, None, "avatar"]]],
        4,
        None,
        "Equipment reminder",
        [1793836800, 0],
        7,
        None,
        None,
        None,
        "1.2",
    ]
    # Representative RPC envelope, using the package's own response extraction regex.
    inner = [[item], [None, token], None]
    outer = [["wrb.fr", "oCPfdb", json.dumps(inner), None, None, None, "generic"]]
    return ")]}'\n\n" + json.dumps(outer, separators=(",", ":")) + "\n"


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = Store(self.home)
        self.config = defaults()
        self.source = make_source()
        self.config["sources"] = [self.source]
        self.secrets = SecretStore(self.home)
        self.engine = Engine(self.store, self.config, self.secrets)

    def tearDown(self):
        self.temp.cleanup()

    def stage(self, batch, source=None):
        source = source or self.source
        run = self.store.start_run(source)
        self.engine.transform(run, source, batch)
        self.store.finish_run(run, "success")
        return run

    def test_raw_first_idempotent_upsert_and_changes(self):
        self.stage(RawBatch("reddit", [reddit_item()]))
        self.stage(RawBatch("reddit", [reddit_item()]))
        rows, count = self.store.records()
        self.assertEqual(count, 1)
        self.assertTrue((self.home / rows[0]["raw_path"]).is_file())
        self.assertEqual(self.store.runs()[0]["unchanged"], 1)
        self.stage(RawBatch("reddit", [reddit_item(body="updated")]))
        self.assertEqual(self.store.runs()[0]["updated"], 1)
        self.assertEqual(self.store.records()[0][0]["body"], "updated")

    def test_failed_transform_retains_raw_and_replay_is_atomic(self):
        batch = RawBatch("reddit", [reddit_item(), {"title": "broken"}])
        run = self.store.start_run(self.source)
        with self.assertRaises(KeyError):
            self.engine.transform(run, self.source, batch)
        self.assertEqual(self.store.stats()["total"], 0)
        self.assertEqual(len(self.store.pending()), 1)
        original_normalize = normalize

        def repaired(batch, source, config):
            batch.payload = [batch.payload[0]]
            return original_normalize(batch, source, config)

        with patch("collector.engine.normalize", repaired):
            self.assertEqual(self.engine.replay(), ["success"])
        self.assertEqual(self.store.stats()["pending"], 0)
        self.assertEqual(self.store.stats()["total"], 1)

    def test_source_error_isolated_and_checkpoint_not_advanced(self):
        good = make_source("app_store", "1234")

        class Broken(Collector):
            def collect(adapter):
                adapter.state = {"last_seen": "must-not-save"}
                yield RawBatch("reddit", [reddit_item()])
                raise CollectorError("synthetic failure")

        class Good(Collector):
            def collect(adapter):
                adapter.state = {"last_seen": "saved"}
                return iter([])

        self.config["sources"].append(good)
        with patch.dict("collector.engine.COLLECTORS", reddit=Broken, app_store=Good):
            self.assertCountEqual(self.engine.run(), ["error", "success"])
        self.assertEqual(self.store.checkpoint(self.source["id"]), {})
        self.assertEqual(self.store.checkpoint(good["id"]), {"last_seen": "saved"})

    def test_partial_checkpoint_saved_on_success_only(self):
        class Partial(Collector):
            def collect(adapter):
                yield RawBatch("reddit", [reddit_item()])
                adapter.pause_scan("t3_123", after="t3_123")

        with patch.dict("collector.engine.COLLECTORS", reddit=Partial):
            self.assertEqual(self.engine.run(), ["partial"])
        self.assertEqual(self.store.checkpoint(self.source["id"])["after"], "t3_123")

    def test_author_hash_deterministic_platform_scoped(self):
        batch = RawBatch("reddit", [reddit_item()])
        first = normalize(batch, self.source, self.config)[0]
        second = normalize(batch, self.source, self.config)[0]
        self.assertEqual(first.author, second.author)
        self.assertTrue(first.author.startswith("sha256:"))
        self.assertNotIn("example", first.author)

    def test_scope_avoids_native_id_collision(self):
        html = (FIXTURES / "dc_post.html").read_text()
        a = make_source("dcinside", "a")
        b = make_source("dcinside", "b")
        batch = RawBatch("dcinside", html, {"post_id": 10, "url": "https://gall.dcinside.com"})
        self.assertNotEqual(normalize(batch, a, self.config)[0].id, normalize(batch, b, self.config)[0].id)

    def test_shared_record_keeps_multiple_source_links(self):
        other = copy.deepcopy(self.source)
        other["id"] = "other_source"
        self.stage(RawBatch("reddit", [reddit_item()]))
        self.stage(RawBatch("reddit", [reddit_item()]), other)
        self.assertEqual(self.store.stats()["total"], 1)
        self.assertEqual(self.store.records(source_id=other["id"])[1], 1)
        self.assertEqual(self.store.records(source_id=self.source["id"])[1], 1)

    def test_cancelled_source_never_calls_adapter(self):
        self.engine.stop.set()
        with patch.dict("collector.engine.COLLECTORS", reddit=Mock()) as collectors:
            self.assertEqual(self.engine.run_source(self.source), "cancelled")
            collectors["reddit"].assert_not_called()

    def test_export_all_pages_csv_formula_protection(self):
        payload = [reddit_item(f"t3_{i}", body="=SUM(A1:A2)") for i in range(205)]
        self.stage(RawBatch("reddit", payload))
        target = self.home / "export.csv"
        self.assertEqual(self.store.export(target), 205)
        with target.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["body"], "'=SUM(A1:A2)")
        target = self.home / "export.jsonl"
        self.assertEqual(self.store.export(target), 205)
        self.assertEqual(json.loads(target.read_text().splitlines()[0])["body"], "=SUM(A1:A2)")

    def test_search_treats_percent_as_literal(self):
        self.stage(RawBatch("reddit", [reddit_item(body="100% useful"), reddit_item("t3_other", "ordinary")]))
        self.assertEqual(self.store.records(search="%")[1], 1)
        self.assertEqual(self.store.records(platform="app_store")[1], 0)

    def test_import_jsonl_export_roundtrip_preserves_ids_and_hash(self):
        self.stage(RawBatch("reddit", [reddit_item()]))
        target = self.home / "export.jsonl"
        self.store.export(target)
        self.assertEqual(self.engine.import_file(self.source, target), 1)
        self.assertEqual(self.store.stats()["total"], 1)
        self.assertEqual(self.store.records()[0][0]["id"], "reddit_t3_123")

    def test_import_rejects_wrong_platform(self):
        path = self.home / "import.json"
        path.write_text(json.dumps([{"native_id": "1", "platform": "naver_cafe"}]))
        with self.assertRaises(CollectorError):
            self.engine.import_file(self.source, path)
        self.assertEqual(self.store.stats()["total"], 0)
        self.assertEqual(self.store.stats()["pending"], 1)

    def test_retention_cleans_raw_and_rows(self):
        self.stage(RawBatch("reddit", [reddit_item()]))
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        raw = self.store.records()[0][0]["raw_path"]
        with self.store.connection() as db:
            db.execute("UPDATE records SET fetched_at=?", (old,))
            db.execute("UPDATE raw_batches SET loaded_at=?", (old,))
        self.store.purge(30)
        self.assertEqual(self.store.stats()["total"], 0)
        self.assertFalse((self.home / raw).exists())

    def test_interrupted_run_recovery(self):
        self.store.start_run(self.source)
        self.store.recover_interrupted()
        self.assertEqual(self.store.runs()[0]["status"], "interrupted")

    def test_single_instance_lock(self):
        first = InstanceLock(self.home)
        try:
            with self.assertRaises(RuntimeError):
                InstanceLock(self.home)
        finally:
            first.close()
        second = InstanceLock(self.home)
        second.close()

    def test_config_rejects_duplicate_ids_and_unsafe_target(self):
        self.config["sources"].append(copy.deepcopy(self.source))
        with self.assertRaises(ValueError):
            validate(self.config)
        self.config["sources"] = [make_source("scubaboard", "https://example.com/community/threads/test.1/")]
        with self.assertRaises(ValueError):
            validate(self.config)

    def test_config_roundtrip_and_bad_json_not_overwritten(self):
        settings = ConfigStore(self.home)
        settings.save(self.config)
        self.assertEqual(settings.read(), self.config)
        settings.path.write_text("broken")
        with self.assertRaises(ValueError):
            settings.read()
        self.assertEqual(settings.path.read_text(), "broken")

    @unittest.skipUnless(__import__("os").name == "nt", "Windows DPAPI integration")
    def test_windows_dpapi_roundtrip(self):
        secret = b"synthetic-secret"
        encrypted = _dpapi(secret)
        self.assertNotIn(secret, encrypted)
        self.assertEqual(_dpapi(encrypted, True), secret)
        self.secrets.save({"NAVER_CLIENT_SECRET": "example"})
        self.assertEqual(SecretStore(self.home).get("NAVER_CLIENT_SECRET"), "example")


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.config = defaults()

    def test_timezone_conversion_and_unknown_time(self):
        self.assertEqual(utc("2026.11.05 09:30:00", 9), "2026-11-05T00:30:00+00:00")
        self.assertEqual(utc("2026-11-05T09:30:00+09:00"), "2026-11-05T00:30:00+00:00")
        self.assertIsNone(utc(None))
        with self.assertRaises(CollectorError):
            utc("changed date format")

    def test_dc_html_cleanup(self):
        batch = RawBatch(
            "dcinside",
            (FIXTURES / "dc_post.html").read_text(),
            {"post_id": 42, "url": "https://gall.dcinside.com"},
        )
        row = normalize(batch, make_source("dcinside"), self.config)[0]
        self.assertEqual(row.created_at, "2026-11-05T00:30:00+00:00")
        self.assertNotIn("alert", row.body)
        self.assertIn("첫 다이빙", row.body)

    def test_scuba_posts_comments(self):
        batch = RawBatch(
            "scubaboard", (FIXTURES / "scuba_thread.html").read_text(), {"thread_id": "12", "page": 1}
        )
        rows = normalize(batch, make_source("scubaboard"), self.config)
        self.assertEqual([row.content_type for row in rows], ["post", "comment"])
        self.assertEqual(rows[1].id, "scubaboard_502")
        self.assertIsNone(rows[1].title)

    def test_naver_snippet_not_fabricated_as_full_body_or_date(self):
        batch = RawBatch(
            "naver_cafe",
            {
                "items": [
                    {
                        "title": "<b>Dive</b>",
                        "description": "A <b>snippet</b>",
                        "link": "https://cafe.naver.com/sample/42",
                        "cafeurl": "https://cafe.naver.com/sample",
                    }
                ]
            },
        )
        row = normalize(batch, make_source("naver_cafe"), self.config)[0]
        self.assertEqual(row.body, "A snippet")
        self.assertIsNone(row.created_at)
        self.assertEqual(row.extra_metadata["content_scope"], "search_snippet")

    def test_google_rpc_raw_timestamp_is_utc(self):
        raw = play_rpc(token="next-token")
        items, token = play_response(raw)
        self.assertEqual(token, "next-token")
        self.assertEqual(items[0][0], "review-1")
        row = normalize(
            RawBatch("google_play", raw), make_source("google_play", "com.example.app"), self.config
        )[0]
        self.assertEqual(row.extra_metadata["rating"], 4)
        self.assertEqual(row.created_at, utc(1793836800))

    def test_google_changed_schema_is_not_silent_empty_success(self):
        with self.assertRaises(CollectorError):
            play_response("<html>unexpected response</html>")

    def test_reddit_deleted_content_is_scrubbed(self):
        row = normalize(RawBatch("reddit", [reddit_item(body="[deleted]")]), make_source(), self.config)[0]
        self.assertEqual(row.body, "")
        self.assertIsNone(row.title)
        self.assertIsNone(row.author)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.client = HttpClient(self.stop, delay=1)
        self.client.gate.wait = Mock()
        self.client.session.request = Mock()

    def tearDown(self):
        self.client.close()

    def response(self, status=200, body="OK", headers=None):
        result = Mock(status_code=status, text=body, content=body.encode(), headers=headers or {})
        return result

    def test_429_cooldown_is_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            self.client.gate = HostGate(store)
            self.client.session.request.return_value = self.response(429, headers={"Retry-After": "120"})
            with self.assertRaises(RateLimited):
                self.client.request("GET", "https://example.com/data")
            gate = HostGate(Store(Path(temporary)))
            with self.assertRaises(RateLimited):
                gate.wait("example.com", 1, self.stop)
            self.assertEqual(self.client.session.request.call_count, 1)

    def test_403_and_challenge_stop_without_retry(self):
        for result in (
            self.response(403),
            self.response(200, "<script src='/cdn-cgi/challenge-platform/test'></script>"),
        ):
            self.client.session.request.reset_mock()
            self.client.session.request.return_value = result
            with self.assertRaises(AccessBlocked):
                self.client.request("GET", "https://example.com/data")
            self.assertEqual(self.client.session.request.call_count, 1)

    def test_transient_server_failure_retries(self):
        self.client.stop = Mock()
        self.client.stop.is_set.return_value = False
        self.client.stop.wait.return_value = False
        self.client.session.request.side_effect = [self.response(503), self.response(200)]
        self.assertEqual(self.client.request("GET", "https://example.com/data").status_code, 200)
        self.assertEqual(self.client.session.request.call_count, 2)

    def test_redirect_does_not_forward_credentials(self):
        self.client.session.request.return_value = self.response(
            302, headers={"Location": "https://other.example.com"}
        )
        with self.assertRaises(AccessBlocked):
            self.client.request("GET", "https://example.com/data", headers={"X-Secret": "example"})
        self.assertFalse(self.client.session.request.call_args.kwargs["allow_redirects"])

    def test_robots_disallow_is_enforced(self):
        self.client.session.get = Mock(return_value=self.response(200, "User-agent: *\nDisallow: /private/"))
        with self.assertRaises(AccessBlocked):
            self.client.request("GET", "https://example.com/private/page", robots=True)
        self.client.session.request.assert_not_called()


class CursorTests(unittest.TestCase):
    def make(self, klass, source, state=None):
        return klass(source, state or {}, Mock(), Mock(), threading.Event())

    def test_google_continuation_survives_budget(self):
        source = make_source("google_play", "com.example.app")
        source.update(limit=1, pages=1)
        adapter = self.make(GooglePlayCollector, source)
        adapter.http.request.return_value.text = play_rpc(token="continue")
        self.assertEqual(len(list(adapter.collect())), 1)
        self.assertTrue(adapter.partial)
        self.assertEqual(adapter.state["token"], "continue")
        next_adapter = self.make(GooglePlayCollector, source, adapter.state)
        next_adapter.http.request.return_value.text = play_rpc("older", None)
        list(next_adapter.collect())
        self.assertFalse(next_adapter.partial)
        self.assertEqual(next_adapter.state, {"last_seen": "review-1"})

    def test_app_store_resume_page(self):
        source = make_source("app_store", "123")
        source.update(limit=1, pages=1)
        adapter = self.make(AppStoreCollector, source)
        adapter.http.json.return_value = {
            "feed": {"entry": [{"id": {"label": "1"}, "im:rating": {"label": "4"}}]}
        }
        list(adapter.collect())
        self.assertEqual(adapter.state["page"], 2)
        self.assertTrue(adapter.partial)
        # Apple returns HTTP 200 with no reviews for an incorrectly cased sort path.
        self.assertIn("sortBy=mostRecent/json", adapter.http.json.call_args.args[0])

    def test_naver_query_cursor_and_missing_date(self):
        source = make_source("naver_cafe", "다이빙")
        source.update(limit=1, pages=1)
        adapter = self.make(NaverCollector, source)
        adapter.secrets.get.return_value = "synthetic-key"
        adapter.http.json.return_value = {
            "total": 3,
            "items": [{"link": "https://cafe.naver.com/demo/3", "description": "snippet"}],
        }
        list(adapter.collect())
        self.assertEqual(adapter.state["start"], 2)
        self.assertTrue(adapter.partial)
        self.assertIn("sort=date", adapter.http.json.call_args.args[0])

    def test_reddit_after_cursor_and_boundary(self):
        from types import SimpleNamespace

        def post(native):
            return SimpleNamespace(**{**reddit_item(native), "num_comments": 0, "upvote_ratio": 1})

        source = make_source()
        source.update(limit=1)
        client = Mock()
        client.subreddit.return_value.new.side_effect = [
            iter([post("t3_new"), post("t3_old")]),
            iter([post("t3_old")]),
        ]
        with patch("praw.Reddit", return_value=client):
            adapter = self.make(RedditCollector, source, {"last_seen": "t3_old"})
            adapter.secrets.get.return_value = "synthetic-key"
            list(adapter.collect())
            self.assertEqual(adapter.state["after"], "t3_new")
            self.assertTrue(adapter.partial)
            resumed = self.make(RedditCollector, source, adapter.state)
            resumed.secrets.get.return_value = "synthetic-key"
            list(resumed.collect())
            self.assertEqual(resumed.state, {"last_seen": "t3_new"})
            self.assertFalse(resumed.partial)
            self.assertEqual(
                client.subreddit.return_value.new.call_args.kwargs["params"], {"after": "t3_new"}
            )

    def test_dc_budget_resumes_same_page_below_last_id(self):
        source = make_source("dcinside", "scuba")
        source.update(limit=1, pages=1)
        adapter = self.make(DcinsideCollector, source)
        listing = "".join(
            f'<tr class="ub-content"><td class="gall_tit"><a href="/mgallery/board/view/?id=scuba&no={i}">Title</a></td></tr>'
            for i in (10, 9)
        )
        listing += '<div class="bottom_paging_box"><a>next</a></div>'
        adapter.http.request.side_effect = [Mock(text=listing), Mock(text="post")]
        list(adapter.collect())
        self.assertEqual(adapter.state["page"], 1)
        self.assertEqual(adapter.state["below"], 10)

    def test_scuba_refresh_resumes_old_last_page_without_skipping_new_pages(self):
        source = make_source("scubaboard", "https://scubaboard.com/community/threads/dive.12/")
        source.update(pages=1)
        saved = source["target"] + "page-3"
        adapter = self.make(
            ScubaBoardCollector, source, {"thread_latest": {"12": 502}, "thread_resume": {"12": saved}}
        )
        adapter.http.request.return_value.text = (FIXTURES / "scuba_thread.html").read_text()
        list(adapter.collect())
        self.assertTrue(adapter.partial)
        self.assertEqual(adapter.state["queue"], [saved])


if __name__ == "__main__":
    unittest.main()
