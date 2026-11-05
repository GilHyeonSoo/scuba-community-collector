"""Synthetic demo. Its database and raw archives live in an isolated subdirectory."""

from __future__ import annotations

from .config import new_source


def seed(engine):
    examples = [
        (
            "reddit",
            "다이빙 로그를 한곳에 모으고 싶어요",
            "종이 로그와 기기 기록을 함께 관리하고 싶습니다.",
            "post",
        ),
        ("google_play", None, "장비별 사용 횟수와 정비일 알림이 있으면 좋겠습니다.", "review"),
        ("app_store", "버디 찾기 기능 제안", "일정과 자격 수준이 맞는 버디를 찾기 어렵습니다.", "review"),
        (
            "dcinside",
            "잠수풀 예약 정보가 흩어져 있어요",
            "수심, 가격, 운영시간을 한번에 비교하고 싶습니다.",
            "post",
        ),
        (
            "naver_cafe",
            "주말 프리다이빙 모임",
            "[검색 요약 예시] 활동 지역과 일정이 맞는 모임을 찾습니다.",
            "post",
        ),
        ("scubaboard", None, "기기마다 내보내는 로그 형식이 달라 통합에 시간이 걸립니다.", "comment"),
    ]
    import json
    from pathlib import Path
    from tempfile import TemporaryDirectory

    for index, (platform, title, body, kind) in enumerate(examples):
        source = new_source(platform)
        source.update(id="demo_" + platform, name="[가상 시연] " + platform, target="synthetic")
        row = {
            "native_id": f"sample_{index}",
            "platform": platform,
            "title": title,
            "body": "[가상 시연 데이터 · 실제 사용자 발언 아님]\n" + body,
            "author": "demo-user",
            "created_at": "2026-11-05T00:00:00+00:00" if platform != "naver_cafe" else None,
            "content_type": kind,
            "source_url": "",
            "extra_metadata": {"synthetic": True},
        }
        if kind == "review":
            row["extra_metadata"]["rating"] = index % 5 + 1
        if platform == "naver_cafe":
            row["extra_metadata"]["content_scope"] = "search_snippet"
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "demo.json"
            path.write_text(json.dumps([row], ensure_ascii=False), encoding="utf-8")
            engine.import_file(source, path)
