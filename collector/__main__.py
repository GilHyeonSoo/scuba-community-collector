from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import PLATFORMS, ConfigStore, data_directory
from .engine import Engine
from .lock import InstanceLock
from .secrets import SecretStore
from .storage import Store


def parser():
    result = argparse.ArgumentParser(
        description="SCUBA Community Collector — Windows desktop / scheduled CLI"
    )
    result.add_argument("--home", type=Path, default=None, help="Isolated data directory")
    result.add_argument("--demo", action="store_true", help="Use isolated synthetic demo data")
    actions = result.add_mutually_exclusive_group()
    actions.add_argument("--once", action="store_true", help="Run enabled collectors once and exit")
    actions.add_argument(
        "--replay", action="store_true", help="Retry pending/failed raw batches without network"
    )
    actions.add_argument("--import-file", type=Path, help="Import authorized JSON/JSONL; requires --source")
    actions.add_argument("--export", type=Path, help="Export .csv or .jsonl")
    actions.add_argument("--list-sources", action="store_true", help="Print configured source IDs")
    result.add_argument("--source", help="Restrict to one source ID")
    result.add_argument("--platform", choices=list(PLATFORMS), help="Restrict collection/export by platform")
    result.add_argument("--search", default="", help="Title/body search for export")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    cli = bool(args.once or args.replay or args.import_file or args.export or args.list_sources)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    home = (args.home or data_directory()).expanduser().resolve()
    lock = None
    try:
        lock = InstanceLock(home)
        configs = ConfigStore(home)
        config = configs.read()
        if args.list_sources:
            print(json.dumps(config["sources"], ensure_ascii=False, indent=2))
            return 0
        store = Store(home / "demo" if args.demo and cli else home)
        store.recover_interrupted()
        store.purge(config["retention_days"])
        secrets = SecretStore(home)
        if args.demo and cli:
            from .demo import seed

            seed(Engine(store, config, secrets))
        if not cli:
            import tkinter as tk

            from .ui import App

            root = tk.Tk()
            application = App(root, configs, store, secrets)
            if args.demo:
                application.toggle_demo()
            root.mainloop()
            return 0

        def report(event):
            print(json.dumps(event, ensure_ascii=False), flush=True)

        engine = Engine(store, config, secrets, report)
        if args.export:
            if args.export.suffix.lower() not in (".csv", ".jsonl"):
                raise ValueError("내보내기 확장자는 .csv 또는 .jsonl이어야 합니다.")
            count = store.export(args.export, args.platform or "", args.search, args.source or "")
            print(json.dumps({"exported": count, "path": str(args.export)}, ensure_ascii=False))
            return 0
        if args.import_file:
            source = next((s for s in config["sources"] if s["id"] == args.source), None)
            if not source:
                raise ValueError(
                    "가져올 대상을 --source ID로 지정하세요. --list-sources로 확인할 수 있습니다."
                )
            engine.import_file(source, args.import_file)
            return 0
        if args.replay:
            statuses = engine.replay()
        elif args.demo:
            print(json.dumps({"demo": True, **store.stats()}, ensure_ascii=False))
            return 0
        else:
            ids = [
                s["id"]
                for s in config["sources"]
                if s["enabled"]
                and (not args.source or s["id"] == args.source)
                and (not args.platform or s["platform"] == args.platform)
            ]
            if not ids:
                raise ValueError(
                    "조건에 맞는 활성 수집 대상이 없습니다. 먼저 GUI에서 수집 대상을 추가하세요."
                )
            try:
                statuses = engine.run(ids)
            except KeyboardInterrupt:
                engine.stop.set()
                return 130
        return 1 if any(s in ("error", "blocked", "cancelled") for s in statuses) else 0
    except Exception as exc:
        from .models import CollectorError

        message = (
            str(exc)
            if isinstance(exc, (ValueError, RuntimeError, OSError, CollectorError))
            else f"{type(exc).__name__}: 실행 실패"
        )
        if cli:
            print(message, file=sys.stderr)
        else:
            try:
                import tkinter as tk
                from tkinter import messagebox

                error_root = tk.Tk()
                error_root.withdraw()
                messagebox.showerror("SCUBA Collector 실행 실패", message)
                error_root.destroy()
            except Exception:
                print(message, file=sys.stderr)
        return 1
    finally:
        if lock:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
