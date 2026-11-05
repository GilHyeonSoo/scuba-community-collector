from __future__ import annotations

import csv
import gzip
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import RawBatch, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
 id TEXT PRIMARY KEY, native_id TEXT NOT NULL, platform TEXT NOT NULL,
 content_type TEXT NOT NULL, title TEXT, body TEXT NOT NULL, author TEXT,
 created_at TEXT, source_url TEXT NOT NULL, parent_id TEXT, extra_metadata TEXT NOT NULL,
 first_seen_at TEXT NOT NULL, fetched_at TEXT NOT NULL, fingerprint TEXT NOT NULL, raw_path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_platform_time ON records(platform, created_at DESC);
CREATE TABLE IF NOT EXISTS record_sources (
 record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE, source_id TEXT NOT NULL,
 PRIMARY KEY (record_id, source_id)
);
CREATE INDEX IF NOT EXISTS record_sources_source ON record_sources(source_id);
CREATE TABLE IF NOT EXISTS checkpoints (source_id TEXT PRIMARY KEY, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS host_cooldowns (host TEXT PRIMARY KEY, until_epoch REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_name TEXT NOT NULL, platform TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
 inserted INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL DEFAULT 0,
 unchanged INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS raw_batches (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
 platform TEXT NOT NULL, path TEXT NOT NULL UNIQUE, loaded_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT ''
);
PRAGMA user_version = 1;
"""


class Store:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = self.home / "collector.sqlite3"
        self.raw_root = self.home / "raw"
        self.raw_root.mkdir(exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError("새 버전의 프로그램으로 만든 DB입니다. 프로그램을 업데이트하세요.")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover_interrupted(self):
        with self.connection() as db:
            db.execute(
                "UPDATE runs SET status='interrupted',finished_at=?,message=? WHERE status='running'",
                (utcnow(), "이전 실행이 중단되었습니다. 다시 수집하거나 원본 재처리를 실행하세요."),
            )

    def start_run(self, source):
        run_id = uuid.uuid4().hex
        with self.connection() as db:
            db.execute(
                "INSERT INTO runs(id,source_id,source_name,platform,started_at,status) VALUES(?,?,?,?,?,'running')",
                (run_id, source["id"], source["name"], source["platform"], utcnow()),
            )
        return run_id

    def finish_run(self, run_id, status, message="", state=None, source_id=None):
        with self.connection() as db:
            if state is not None:
                db.execute(
                    "INSERT INTO checkpoints VALUES(?,?) ON CONFLICT(source_id) DO UPDATE SET state=excluded.state",
                    (source_id, json.dumps(state)),
                )
            db.execute(
                "UPDATE runs SET status=?,message=?,finished_at=? WHERE id=?",
                (status, message, utcnow(), run_id),
            )

    def checkpoint(self, source_id):
        with self.connection() as db:
            row = db.execute("SELECT state FROM checkpoints WHERE source_id=?", (source_id,)).fetchone()
            return json.loads(row[0]) if row else {}

    def reset_checkpoint(self, source_id):
        with self.connection() as db:
            db.execute("DELETE FROM checkpoints WHERE source_id=?", (source_id,))

    def cooldown(self, host):
        with self.connection() as db:
            row = db.execute("SELECT until_epoch FROM host_cooldowns WHERE host=?", (host,)).fetchone()
            return row[0] if row else 0

    def defer_host(self, host, until):
        with self.connection() as db:
            db.execute(
                "INSERT INTO host_cooldowns VALUES(?,?) ON CONFLICT(host) DO UPDATE SET until_epoch=max(until_epoch,excluded.until_epoch)",
                (host, until),
            )

    def stage(self, run_id, source, batch: RawBatch):
        raw_id = uuid.uuid4().hex
        relative = Path("raw") / source["platform"] / source["id"] / (raw_id + ".json.gz")
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        envelope = {
            "schema_version": 1,
            "source": source,
            "kind": batch.kind,
            "payload": batch.payload,
            "context": batch.context,
            "loaded_at": utcnow(),
        }
        temporary = path.with_suffix(".tmp")
        with gzip.open(temporary, "wt", encoding="utf-8") as stream:
            json.dump(envelope, stream, ensure_ascii=False)
        temporary.replace(path)
        with self.connection() as db:
            db.execute(
                "INSERT INTO raw_batches(id,run_id,source_id,platform,path,loaded_at) VALUES(?,?,?,?,?,?)",
                (raw_id, run_id, source["id"], source["platform"], relative.as_posix(), utcnow()),
            )
        return raw_id, relative.as_posix()

    def commit_batch(self, raw_id, raw_path, source_id, run_id, records):
        counts = {"inserted": 0, "updated": 0, "unchanged": 0}
        now = utcnow()
        with self.connection() as db:
            for record in records:
                values = asdict(record)
                fingerprint = hashlib.sha256(
                    json.dumps(values, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                old = db.execute("SELECT fingerprint FROM records WHERE id=?", (record.id,)).fetchone()
                key = "inserted" if old is None else "unchanged" if old[0] == fingerprint else "updated"
                counts[key] += 1
                values["extra_metadata"] = json.dumps(values["extra_metadata"], ensure_ascii=False)
                columns = list(values)
                db.execute(
                    "INSERT INTO records("
                    + ",".join(columns)
                    + ",first_seen_at,fetched_at,fingerprint,raw_path) VALUES("
                    + ",".join("?" for _ in range(len(columns) + 4))
                    + ") ON CONFLICT(id) DO UPDATE SET "
                    + ",".join(f"{col}=excluded.{col}" for col in columns if col != "id")
                    + ",fetched_at=excluded.fetched_at,fingerprint=excluded.fingerprint,raw_path=excluded.raw_path",
                    [values[col] for col in columns] + [now, now, fingerprint, raw_path],
                )
                db.execute("INSERT OR IGNORE INTO record_sources VALUES(?,?)", (record.id, source_id))
            db.execute("UPDATE raw_batches SET status='done',error='' WHERE id=?", (raw_id,))
            db.execute(
                "UPDATE runs SET inserted=inserted+?,updated=updated+?,unchanged=unchanged+? WHERE id=?",
                (counts["inserted"], counts["updated"], counts["unchanged"], run_id),
            )
        return counts

    def fail_batch(self, raw_id, message):
        with self.connection() as db:
            db.execute("UPDATE raw_batches SET status='error',error=? WHERE id=?", (message, raw_id))

    def pending(self):
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM raw_batches WHERE status IN ('pending','error') ORDER BY loaded_at"
                )
            ]

    def load_raw(self, relative):
        path = (self.home / relative).resolve()
        if not path.is_relative_to(self.raw_root.resolve()):
            raise ValueError("잘못된 원본 경로입니다.")
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            value = json.load(stream)
        if value.get("schema_version") != 1:
            raise ValueError("지원하지 않는 원본 스키마입니다.")
        return value["source"], RawBatch(value["kind"], value["payload"], value.get("context", {}))

    @staticmethod
    def query_parts(platform="", search="", source_id=""):
        terms, args = [], []
        if platform:
            terms.append("r.platform=?")
            args.append(platform)
        if source_id:
            terms.append("EXISTS(SELECT 1 FROM record_sources s WHERE s.record_id=r.id AND s.source_id=?)")
            args.append(source_id)
        if search:
            terms.append("(r.title LIKE ? ESCAPE '\\' OR r.body LIKE ? ESCAPE '\\')")
            pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args.extend([pattern, pattern])
        return (" WHERE " + " AND ".join(terms) if terms else ""), args

    def records(self, platform="", search="", source_id="", limit=500, offset=0):
        where, args = self.query_parts(platform, search, source_id)
        with self.connection() as db:
            count = db.execute("SELECT count(*) FROM records r" + where, args).fetchone()[0]
            rows = db.execute(
                "SELECT r.* FROM records r"
                + where
                + " ORDER BY coalesce(r.created_at,r.first_seen_at) DESC,r.id LIMIT ? OFFSET ?",
                args + [limit, offset],
            )
            return [self.decode(row) for row in rows], count

    @staticmethod
    def decode(row):
        result = dict(row)
        result["extra_metadata"] = json.loads(result["extra_metadata"])
        result.pop("fingerprint", None)
        return result

    def stats(self):
        with self.connection() as db:
            result = dict(db.execute("SELECT platform,count(*) FROM records GROUP BY platform"))
            result["total"] = sum(result.values())
            result["pending"] = db.execute(
                "SELECT count(*) FROM raw_batches WHERE status!='done'"
            ).fetchone()[0]
            return result

    def runs(self, limit=100):
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM runs ORDER BY started_at DESC,rowid DESC LIMIT ?", (limit,)
                )
            ]

    def export(self, destination, platform="", search="", source_id=""):
        where, args = self.query_parts(platform, search, source_id)
        fields = [
            "id",
            "native_id",
            "platform",
            "content_type",
            "title",
            "body",
            "author",
            "created_at",
            "source_url",
            "parent_id",
            "extra_metadata",
            "first_seen_at",
            "fetched_at",
        ]
        csv_mode = Path(destination).suffix.lower() == ".csv"
        count = 0
        # A cursor streams the entire result set, independently of the UI page size.
        with (
            self.connection() as db,
            open(destination, "w", encoding="utf-8-sig" if csv_mode else "utf-8", newline="") as stream,
        ):
            writer = csv.DictWriter(stream, fieldnames=fields) if csv_mode else None
            if writer:
                writer.writeheader()
            for row in db.execute("SELECT r.* FROM records r" + where + " ORDER BY r.id", args):
                record = self.decode(row)
                record.pop("raw_path", None)
                if writer:
                    record["extra_metadata"] = json.dumps(record["extra_metadata"], ensure_ascii=False)
                    writer.writerow(
                        {
                            key: (
                                "'" + value
                                if isinstance(value, str)
                                and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r"))
                                else value
                            )
                            for key, value in record.items()
                        }
                    )
                else:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
        return count

    def purge(self, days=30):
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
        reddit_cutoff = (now - timedelta(days=min(days, 2))).isoformat(timespec="seconds")
        with self.connection() as db:
            db.execute(
                "DELETE FROM records WHERE fetched_at < ? OR (platform='reddit' AND fetched_at < ?)",
                (cutoff, reddit_cutoff),
            )
            raw_rows = db.execute(
                "SELECT id,path FROM raw_batches WHERE loaded_at < ? OR (platform='reddit' AND loaded_at < ?)",
                (cutoff, reddit_cutoff),
            ).fetchall()
            for row in raw_rows:
                path = (self.home / row["path"]).resolve()
                if path.is_relative_to(self.raw_root.resolve()):
                    path.unlink(missing_ok=True)
                    db.execute("DELETE FROM raw_batches WHERE id=?", (row["id"],))
        # Data retained by a newer upsert can reference only a newer raw batch.
        return len(raw_rows)
