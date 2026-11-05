from __future__ import annotations

import copy
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from .config import PLATFORMS, new_source, validate
from .engine import Engine
from .secrets import KEYS
from .storage import Store

STATUS = {
    "success": "완료",
    "partial": "이어받기 대기",
    "error": "오류",
    "blocked": "접근 제한",
    "cancelled": "중지",
    "running": "수집 중",
    "interrupted": "이전 실행 중단",
}
HINTS = {
    "reddit": "대상: 서브레딧 이름(r/ 제외). 승인된 Reddit API 키가 필요합니다.\n댓글 옵션은 글당 최대 설정 건수·추가 확장 2회로 제한됩니다.",
    "google_play": "대상: Google Play 패키지 ID (예: com.example.app). 국가·언어를 지정하세요.\n공개 비공식 인터페이스이며 전체 리뷰 제공을 보장하지 않습니다.",
    "app_store": "대상: App Store 숫자 앱 ID. 국가별 RSS의 최근 리뷰를 수집합니다.\nRSS가 제공하는 최근 페이지 범위(통상 최대 10페이지)만 조회합니다.",
    "dcinside": "대상: 갤러리 ID. 일반=board / 마이너=mgallery / 미니=mini.\n공개 게시글 본문을 수집하며 robots.txt·접근 제한 시 중지합니다. 댓글은 파일 가져오기 지원.",
    "naver_cafe": "대상: 카페 검색어. 네이버 검색 API 키가 필요합니다. 카페 URL 필터는 선택 사항입니다.\n검색 요약만 제공됩니다. 본문·댓글은 권한 확보 JSON/JSONL 파일로 가져오세요.",
    "scubaboard": "대상: https://scubaboard.com/community/forums/... 또는 /threads/... URL.\n공개 글·답글을 수집하며 robots.txt·Cloudflare/캡차 발생 시 중지합니다.",
}


def open_folder(path):
    if os.name == "nt":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class SourceDialog(tk.Toplevel):
    def __init__(self, parent, source, commit):
        super().__init__(parent)
        self.title("수집 대상 설정")
        self.resizable(False, False)
        self.source = copy.deepcopy(source)
        self.commit = commit
        self.transient(parent)
        self.vars = {}
        form = ttk.Frame(self, padding=24)
        form.pack(fill="both", expand=True)
        labels = [
            ("name", "이름"),
            ("platform", "플랫폼"),
            ("target", "수집 대상"),
            ("country", "국가 코드"),
            ("language", "언어 코드"),
            ("limit", "회차당 건수 한도"),
            ("pages", "회차당 페이지 한도"),
            ("gallery_type", "디시 갤러리 유형"),
            ("cafe_url", "네이버 카페 URL 필터"),
            ("comment_limit", "Reddit 글당 댓글 한도"),
        ]
        for row, (key, label) in enumerate(labels):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 16))
            variable = tk.StringVar(value=str(source.get(key, "")))
            self.vars[key] = variable
            if key == "platform":
                widget = ttk.Combobox(
                    form, textvariable=variable, values=list(PLATFORMS), state="readonly", width=48
                )
                widget.bind("<<ComboboxSelected>>", self.hint)
            elif key == "gallery_type":
                widget = ttk.Combobox(
                    form,
                    textvariable=variable,
                    values=["board", "mgallery", "mini"],
                    state="readonly",
                    width=48,
                )
            else:
                widget = ttk.Entry(form, textvariable=variable, width=51)
            widget.grid(row=row, column=1, sticky="ew", pady=5)
        self.enabled = tk.BooleanVar(value=source["enabled"])
        self.comments = tk.BooleanVar(value=source["comments"])
        ttk.Checkbutton(form, text="수집 활성화", variable=self.enabled).grid(
            row=10, column=1, sticky="w", pady=5
        )
        ttk.Checkbutton(form, text="Reddit 댓글 함께 수집", variable=self.comments).grid(
            row=11, column=1, sticky="w", pady=5
        )
        self.help = ttk.Label(form, wraplength=620, foreground="#526176")
        self.help.grid(row=12, column=0, columnspan=2, sticky="w", pady=16)
        ttk.Label(
            form, text="페이지 단위 수집은 건수 한도를 소폭 초과할 수 있습니다.", foreground="#526176"
        ).grid(row=13, column=0, columnspan=2, sticky="w")
        buttons = ttk.Frame(form)
        buttons.grid(row=14, column=0, columnspan=2, sticky="e", pady=(20, 0))
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(buttons, text="저장", command=self.save).pack(side="left", padx=4)
        self.hint()
        self.bind("<Escape>", lambda event: self.destroy())
        self.grab_set()

    def hint(self, *_):
        self.help.configure(text=HINTS[self.vars["platform"].get()])

    def save(self):
        value = dict(self.source)
        value.update({key: variable.get().strip() for key, variable in self.vars.items()})
        try:
            for key in ("limit", "pages", "comment_limit"):
                value[key] = int(value[key])
            value.update(enabled=self.enabled.get(), comments=self.comments.get())
            self.commit(value)
        except (ValueError, OSError) as exc:
            messagebox.showerror("입력 확인", str(exc), parent=self)
            return
        self.destroy()


class App:
    def __init__(self, root, config_store, store, secrets):
        self.root = root
        self.config_store = config_store
        self.config = config_store.read()
        self.live_store = store
        self.store = store
        self.secrets = secrets
        self.events = queue.Queue()
        self.thread = None
        self.engine = None
        self.schedule = False
        self.next_run = None
        self.demo_mode = False
        self.closing = False
        self.offset = 0
        self.visible = {}
        self.root.title("SCUBA Collector · 커뮤니티 통합 수집기")
        self.root.geometry("1240x850")
        self.root.minsize(1040, 720)
        self.root.configure(background="#f1f5f9")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.style()
        self.build()
        self.refresh()
        self.root.after(150, self.poll)

    def style(self):
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        font = "Malgun Gothic" if os.name == "nt" else "Helvetica"
        self.root.option_add("*Font", (font, 10))
        style.configure("TFrame", background="#f1f5f9")
        style.configure("TLabel", background="#f1f5f9", foreground="#16263b", font=(font, 10))
        style.configure("TButton", padding=(12, 8), font=(font, 10))
        style.configure("Primary.TButton", background="#087f8c", foreground="white")
        style.map("Primary.TButton", background=[("active", "#096976"), ("disabled", "#a1b1b5")])
        style.configure(
            "Treeview", rowheight=32, fieldbackground="white", background="white", font=(font, 10)
        )
        style.configure("Treeview.Heading", font=(font, 10, "bold"), padding=8)
        style.map("Treeview", background=[("selected", "#d2edf0")], foreground=[("selected", "#12303d")])
        style.configure("TNotebook", background="#f1f5f9", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(22, 11), font=(font, 10))
        style.configure("Card.TFrame", background="white")
        style.configure("Card.TLabel", background="white")
        style.configure("Value.TLabel", background="white", foreground="#087f8c", font=(font, 25, "bold"))

    def build(self):
        header = tk.Frame(self.root, bg="#12283d", height=92)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header, text="SCUBA  /  COMMUNITY COLLECTOR", bg="#12283d", fg="white", font=("Arial", 19, "bold")
        ).pack(anchor="w", padx=26, pady=(18, 3))
        tk.Label(
            header, text="플랫폼별 수집 → 원본 보관 → 통합 정규화 → 검색·내보내기", bg="#12283d", fg="#9ec4d2"
        ).pack(anchor="w", padx=27)
        body = ttk.Frame(self.root, padding=(24, 18))
        body.pack(fill="both", expand=True)
        cards = ttk.Frame(body)
        cards.pack(fill="x", pady=(0, 15))
        self.card_vars = {}
        for key, label in (
            ("total", "통합 데이터"),
            ("sources", "활성 수집 대상"),
            ("pending", "재처리 대기 원본"),
            ("mode", "저장 모드"),
        ):
            card = ttk.Frame(cards, style="Card.TFrame", padding=(18, 10))
            card.pack(side="left", fill="both", expand=True, padx=(0, 10))
            ttk.Label(card, text=label, style="Card.TLabel").pack(anchor="w")
            self.card_vars[key] = tk.StringVar(value="0")
            ttk.Label(card, textvariable=self.card_vars[key], style="Value.TLabel").pack(
                anchor="w", pady=(3, 0)
            )
        toolbar = ttk.Frame(body)
        toolbar.pack(fill="x", pady=(0, 14))
        self.run_button = ttk.Button(toolbar, text="▶  지금 수집", style="Primary.TButton", command=self.run)
        self.run_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(toolbar, text="■  중지", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 8))
        self.schedule_button = ttk.Button(toolbar, text="예약 수집 켜기", command=self.toggle_schedule)
        self.schedule_button.pack(side="left", padx=(0, 8))
        ttk.Button(toolbar, text="내보내기", command=self.export).pack(side="right")
        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill="both", expand=True)
        self.data_tab = ttk.Frame(self.notebook, padding=12)
        self.source_tab = ttk.Frame(self.notebook, padding=12)
        self.run_tab = ttk.Frame(self.notebook, padding=12)
        self.help_tab = ttk.Frame(self.notebook, padding=18)
        for frame, label in (
            (self.data_tab, "통합 데이터"),
            (self.source_tab, "수집 대상"),
            (self.run_tab, "실행 기록"),
            (self.help_tab, "설정·도움말"),
        ):
            self.notebook.add(frame, text=label)
        self.build_data()
        self.build_sources()
        self.build_runs()
        self.build_help()
        self.status = tk.StringVar(value="준비 완료 · 수집 대상을 추가해 시작하세요.")
        ttk.Label(body, textvariable=self.status, foreground="#526176", wraplength=1120).pack(
            anchor="w", pady=(12, 0)
        )

    def tree(self, parent, columns, heights=10):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(
            frame,
            columns=[column[0] for column in columns],
            show="headings",
            height=heights,
            selectmode="browse",
        )
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for key, label, width in columns:
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=55, stretch=True)
        return tree

    def build_data(self):
        filters = ttk.Frame(self.data_tab)
        filters.pack(fill="x", pady=(0, 10))
        self.platform = tk.StringVar(value="전체 플랫폼")
        choice = ttk.Combobox(
            filters,
            textvariable=self.platform,
            values=["전체 플랫폼"] + list(PLATFORMS.values()),
            state="readonly",
            width=18,
        )
        choice.pack(side="left", padx=(0, 8))
        choice.bind("<<ComboboxSelected>>", lambda event: self.search())
        self.search_var = tk.StringVar()
        entry = ttk.Entry(filters, textvariable=self.search_var)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        entry.bind("<Return>", lambda event: self.search())
        ttk.Button(filters, text="검색", command=self.search).pack(side="left")
        ttk.Button(filters, text="새로고침", command=self.refresh).pack(side="left", padx=(8, 0))
        pane = ttk.Panedwindow(self.data_tab, orient="vertical")
        pane.pack(fill="both", expand=True)
        upper = ttk.Frame(pane)
        lower = ttk.Frame(pane)
        pane.add(upper, weight=3)
        pane.add(lower, weight=2)
        self.data_tree = self.tree(
            upper,
            [
                ("platform", "플랫폼", 120),
                ("type", "유형 / 범위", 125),
                ("date", "작성일 (UTC)", 150),
                ("rating", "평점", 55),
                ("title", "제목 / 본문", 490),
            ],
        )
        self.data_tree.bind("<<TreeviewSelect>>", self.show_record)
        pager = ttk.Frame(upper)
        pager.pack(fill="x", pady=6)
        self.page_label = tk.StringVar()
        ttk.Label(pager, textvariable=self.page_label).pack(side="left")
        ttk.Button(pager, text="다음 →", command=lambda: self.page(200)).pack(side="right")
        ttk.Button(pager, text="← 이전", command=lambda: self.page(-200)).pack(side="right", padx=5)
        detail_bar = ttk.Frame(lower)
        detail_bar.pack(fill="x", pady=(7, 3))
        ttk.Label(detail_bar, text="선택한 데이터", font=("Arial", 11, "bold")).pack(side="left")
        ttk.Button(detail_bar, text="원문 열기", command=self.open_record).pack(side="right")
        self.detail = tk.Text(
            lower,
            wrap="word",
            height=8,
            relief="flat",
            padx=12,
            pady=8,
            bg="white",
            fg="#24374b",
            state="disabled",
        )
        detail_scroll = ttk.Scrollbar(lower, command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_scroll.set)
        self.detail.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")

    def build_sources(self):
        bar = ttk.Frame(self.source_tab)
        bar.pack(fill="x", pady=(0, 10))
        for label, command in (
            ("+ 대상 추가", self.add_source),
            ("수정", self.edit_source),
            ("삭제", self.delete_source),
            ("선택 대상 수집", self.run_selected),
            ("체크포인트 초기화", self.reset_source),
            ("파일 가져오기", self.import_file),
        ):
            ttk.Button(bar, text=label, command=command).pack(side="left", padx=(0, 6))
        ttk.Label(
            self.source_tab,
            text="스토어 2개를 개별 모듈로 분리해 총 6개 어댑터를 제공합니다. API 키는 설정·도움말에서 입력하세요.",
            foreground="#526176",
        ).pack(anchor="w", pady=(0, 8))
        self.source_tree = self.tree(
            self.source_tab,
            [
                ("enabled", "활성", 55),
                ("name", "이름", 160),
                ("platform", "플랫폼", 110),
                ("target", "수집 대상", 390),
                ("state", "최근 실행", 130),
            ],
        )
        self.source_tree.bind("<Double-1>", lambda event: self.edit_source())

    def build_runs(self):
        self.run_tree = self.tree(
            self.run_tab,
            [
                ("started", "시작 (현지 시각)", 160),
                ("source", "수집 대상", 155),
                ("status", "결과", 100),
                ("counts", "신규 / 변경 / 동일", 145),
                ("message", "설명", 480),
            ],
        )
        self.run_tree.bind("<<TreeviewSelect>>", self.show_run)
        self.run_message = tk.StringVar(value="실행을 선택하면 상세 사유를 확인할 수 있습니다.")
        ttk.Label(self.run_tab, textvariable=self.run_message, wraplength=1080, foreground="#526176").pack(
            anchor="w", pady=10
        )
        self.log = tk.Text(
            self.run_tab,
            height=6,
            bg="#12283d",
            fg="#c6e4ec",
            wrap="word",
            relief="flat",
            padx=12,
            pady=10,
            state="disabled",
        )
        self.log.pack(fill="x")

    def build_help(self):
        bar = ttk.Frame(self.help_tab)
        bar.pack(fill="x", pady=(0, 12))
        for label, command in (
            ("API 인증 설정", self.credentials),
            ("운영 설정", self.settings),
            ("원본 재처리", self.replay),
            ("저장 폴더 열기", lambda: open_folder(self.store.home)),
            ("가상 시연 모드 전환", self.toggle_demo),
        ):
            ttk.Button(bar, text=label, command=command).pack(side="left", padx=(0, 8))
        self.help_text = tk.Text(
            self.help_tab, wrap="word", bg="white", fg="#24374b", relief="flat", padx=20, pady=16
        )
        self.help_text.pack(fill="both", expand=True)
        self.help_text.insert(
            "1.0",
            "시작하기\n\n"
            "1. 수집 대상 탭에서 플랫폼과 대상 ID/URL을 추가합니다.\n"
            "2. Reddit·네이버는 API 인증 설정을 입력합니다. Windows에서는 현재 사용자 전용 DPAPI로 암호화합니다.\n"
            "3. 지금 수집을 누르고 실행 기록에서 결과를 확인합니다. 오류가 난 플랫폼과 다른 대상은 독립적으로 실행됩니다.\n"
            "4. 통합 데이터에서 플랫폼·키워드로 검색한 뒤 CSV 또는 JSONL로 내보냅니다. 현재 필터의 모든 결과가 저장됩니다.\n\n"
            "수집 범위\n\n"
            "• Reddit: 승인된 공식 API, 게시글·선택적 댓글(건수·확장 한도 적용). 최근 경계 글을 갱신합니다.\n"
            "• Google Play: 국가·언어별 공개 리뷰, 비공식 인터페이스. 제공량과 구조가 바뀔 수 있습니다.\n"
            "• App Store: 국가별 RSS 최근 리뷰 범위. 전체 과거 이력을 보장하지 않습니다.\n"
            "• 디시인사이드: 공개 갤러리 게시글 본문. 댓글은 권한 확보 파일로 가져옵니다.\n"
            "• 네이버 카페: 공식 검색 API의 검색 요약. 본문·댓글은 권한 확보 파일로 가져옵니다.\n"
            "• ScubaBoard: 공개 게시판/스레드 글·답글. 페이지별 수집 한도를 적용합니다.\n\n"
            "운영 안내\n\n"
            "• 예약 수집은 이 프로그램이 열려 있고 PC가 깨어 있는 동안 동작합니다. Windows 작업 스케줄러는 README를 참고하세요.\n"
            "• 일시적 네트워크/서버 오류는 최대 3회 시도합니다. 429는 Retry-After까지 대기 상태를 보존합니다.\n"
            "• 공개 HTML은 robots.txt를 확인합니다. 로그인·캡차·접근 거절 시 해당 수집을 중단합니다.\n"
            "• 원본은 raw 폴더, 정규화 데이터·실행 기록은 SQLite에 저장합니다. 실패 원본은 재처리가 가능합니다.\n"
            "• 기본 보관 기간은 30일, Reddit은 최대 48시간입니다. 정리는 수집 시작/프로그램 시작 시 실행됩니다.\n"
            "• 작성자는 기본적으로 플랫폼별 HMAC으로 가명 처리됩니다. 원본에는 실제 작성자 정보가 남습니다.\n"
            "• JSON/JSONL 가져오기는 선택한 대상의 플랫폼과 일치해야 합니다. 예제는 examples 폴더를 참고하세요.\n"
            "• 가상 시연은 별도 demo 폴더의 합성 데이터입니다. 실제 고객 발언이나 수집 결과로 사용하지 마세요.\n\n"
            f"실제 저장 위치\n{self.live_store.home}\n",
        )
        self.help_text.configure(state="disabled")

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def idle_required(self):
        if self.busy():
            messagebox.showinfo(
                "수집 진행 중", "현재 작업이 끝나거나 중지된 뒤 실행하세요.", parent=self.root
            )
            return False
        return True

    def selected_source(self):
        selected = self.source_tree.selection()
        source = next((s for s in self.config["sources"] if selected and s["id"] == selected[0]), None)
        if source is None:
            messagebox.showinfo("대상 선택", "수집 대상 목록에서 한 항목을 선택하세요.", parent=self.root)
        return source

    def persist_source(self, value):
        config = copy.deepcopy(self.config)
        previous = next((s for s in config["sources"] if s["id"] == value["id"]), None)
        config["sources"] = [value if s["id"] == value["id"] else s for s in config["sources"]]
        if previous is None:
            config["sources"].append(value)
        validate(config)
        self.config_store.save(config)
        self.config = config
        if previous and any(
            previous.get(k) != value.get(k)
            for k in ("target", "platform", "country", "language", "gallery_type", "cafe_url")
        ):
            self.live_store.reset_checkpoint(value["id"])
        self.refresh()

    def add_source(self):
        if self.idle_required():
            SourceDialog(self.root, new_source(), self.persist_source)

    def edit_source(self):
        if self.idle_required():
            source = self.selected_source()
            if source:
                SourceDialog(self.root, source, self.persist_source)

    def delete_source(self):
        if not self.idle_required():
            return
        source = self.selected_source()
        if source and messagebox.askyesno(
            "대상 삭제",
            f"'{source['name']}' 설정을 삭제할까요?\n이미 수집된 데이터는 보관합니다.",
            parent=self.root,
        ):
            config = copy.deepcopy(self.config)
            config["sources"] = [s for s in config["sources"] if s["id"] != source["id"]]
            self.config_store.save(config)
            self.config = config
            self.live_store.reset_checkpoint(source["id"])
            self.refresh()

    def reset_source(self):
        if not self.idle_required():
            return
        source = self.selected_source()
        if source and messagebox.askyesno(
            "체크포인트 초기화",
            "다음 실행에서 최신 지점부터 다시 탐색합니다. 저장된 데이터의 중복은 방지됩니다. 계속할까요?",
            parent=self.root,
        ):
            self.live_store.reset_checkpoint(source["id"])
            self.status.set("체크포인트를 초기화했습니다.")

    def launch(self, operation):
        if not self.idle_required():
            return
        self.engine = Engine(self.store, copy.deepcopy(self.config), self.secrets, self.events.put)
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

        def work():
            try:
                operation(self.engine)
            except Exception as exc:
                self.events.put({"kind": "notice", "message": self.engine.message(exc)})
            finally:
                self.events.put({"kind": "done"})

        self.thread = threading.Thread(target=work, name="desktop-worker", daemon=True)
        self.thread.start()

    def run(self):
        if self.demo_mode:
            messagebox.showinfo(
                "가상 시연 모드", "설정·도움말에서 실제 저장 모드로 전환한 뒤 수집하세요.", parent=self.root
            )
            return
        self.launch(lambda engine: engine.run())

    def run_selected(self):
        if self.demo_mode:
            self.run()
            return
        source = self.selected_source()
        if source:
            self.launch(lambda engine: engine.run([source["id"]]))

    def stop(self):
        if self.engine:
            self.engine.stop.set()
            self.status.set("중지 요청됨 · 진행 중인 네트워크 요청의 제한 시간까지 기다릴 수 있습니다.")

    def toggle_schedule(self):
        if self.demo_mode:
            return
        self.schedule = not self.schedule
        self.next_run = time.monotonic() + self.config["interval_minutes"] * 60 if self.schedule else None
        self.schedule_button.configure(text="예약 수집 끄기" if self.schedule else "예약 수집 켜기")
        self.status.set(f"예약 수집 {'켜짐 · 프로그램을 열어 두세요.' if self.schedule else '꺼짐'}")

    def filters(self):
        platform = next((key for key, label in PLATFORMS.items() if label == self.platform.get()), "")
        return {"platform": platform, "search": self.search_var.get().strip()}

    def search(self):
        self.offset = 0
        self.refresh_data()

    def page(self, delta):
        self.offset = max(0, self.offset + delta)
        self.refresh_data()

    def refresh_data(self):
        rows, total = self.store.records(**self.filters(), limit=200, offset=self.offset)
        if not rows and self.offset:
            self.offset = max(0, (max(1, total) - 1) // 200 * 200)
            rows, total = self.store.records(**self.filters(), limit=200, offset=self.offset)
        self.visible = {row["id"]: row for row in rows}
        self.data_tree.delete(*self.data_tree.get_children())
        for row in rows:
            snippet = row["extra_metadata"].get("content_scope") == "search_snippet"
            kind = (
                "검색 요약"
                if snippet
                else {"post": "게시글", "comment": "댓글", "review": "리뷰"}.get(row["content_type"], "")
            )
            self.data_tree.insert(
                "",
                "end",
                iid=row["id"],
                values=(
                    PLATFORMS[row["platform"]],
                    kind,
                    (row["created_at"] or "미제공")[:19].replace("T", " "),
                    row["extra_metadata"].get("rating", "—"),
                    (row["title"] or row["body"]).replace("\n", " ")[:140],
                ),
            )
        self.page_label.set(
            f"{total:,}건 · {self.offset + 1 if total else 0:,}–{min(self.offset + 200, total):,} 표시 / 내보내기는 전체 검색 결과"
        )
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert(
            "1.0",
            "목록에서 데이터를 선택하면 본문과 메타데이터가 표시됩니다."
            if total
            else "아직 데이터가 없습니다. 수집 대상을 추가하거나 설정·도움말에서 가상 시연 모드를 열어보세요.",
        )
        self.detail.configure(state="disabled")

    def show_record(self, *_):
        selected = self.data_tree.selection()
        row = self.visible.get(selected[0]) if selected else None
        if not row:
            return
        value = (
            f"{row['title'] or '(제목 없음)'}\n\n{row['body']}\n\n"
            f"ID: {row['id']}\n작성자: {row['author'] or '미제공'}\n작성일(UTC): {row['created_at'] or '미제공'}\n"
            f"출처: {row['source_url'] or '파일/가상 시연'}\n최종 수집: {row['fetched_at']}\n"
            f"메타데이터: {json.dumps(row['extra_metadata'], ensure_ascii=False, indent=2)}"
        )
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", value)
        self.detail.configure(state="disabled")

    def open_record(self):
        selected = self.data_tree.selection()
        row = self.visible.get(selected[0]) if selected else None
        if row and row["source_url"].startswith("https://"):
            webbrowser.open(row["source_url"])

    def refresh(self):
        stats = self.store.stats()
        for key in ("total", "pending"):
            self.card_vars[key].set(f"{stats[key]:,}")
        self.card_vars["sources"].set(str(sum(s["enabled"] for s in self.config["sources"])))
        self.card_vars["mode"].set("가상 시연" if self.demo_mode else "로컬 저장")
        self.refresh_data()
        runs = self.store.runs()
        self.run_rows = {row["id"]: row for row in runs}
        self.run_tree.delete(*self.run_tree.get_children())
        latest = {}
        for row in runs:
            latest.setdefault(row["source_id"], STATUS.get(row["status"], row["status"]))
            date = datetime.fromisoformat(row["started_at"]).astimezone().strftime("%m-%d %H:%M:%S")
            self.run_tree.insert(
                "",
                "end",
                iid=row["id"],
                values=(
                    date,
                    row["source_name"],
                    STATUS.get(row["status"], row["status"]),
                    f"{row['inserted']} / {row['updated']} / {row['unchanged']}",
                    row["message"],
                ),
            )
        selection = self.source_tree.selection()
        self.source_tree.delete(*self.source_tree.get_children())
        for source in self.config["sources"]:
            self.source_tree.insert(
                "",
                "end",
                iid=source["id"],
                values=(
                    "ON" if source["enabled"] else "OFF",
                    source["name"],
                    PLATFORMS[source["platform"]],
                    source["target"],
                    latest.get(source["id"], "미실행"),
                ),
            )
        if selection and self.source_tree.exists(selection[0]):
            self.source_tree.selection_set(selection[0])

    def show_run(self, *_):
        selected = self.run_tree.selection()
        if selected and selected[0] in self.run_rows:
            self.run_message.set(self.run_rows[selected[0]]["message"] or "진행 중인 실행입니다.")

    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", datetime.now().strftime("%H:%M:%S ") + text + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 300:
            self.log.delete("1.0", "2.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def poll(self):
        dirty = False
        try:
            while True:
                event = self.events.get_nowait()
                kind = event["kind"]
                if kind == "start":
                    text = f"{event['source']} 수집 시작"
                elif kind == "batch":
                    text = f"{event['source']} · 신규 {event['inserted']} / 변경 {event['updated']} / 동일 {event['unchanged']}"
                elif kind == "finish":
                    text = f"{event['source']} · {STATUS.get(event['status'], event['status'])} · {event['message']}"
                    dirty = True
                elif kind == "done":
                    self.stop_button.configure(state="disabled")
                    self.run_button.configure(state="normal")
                    self.next_run = (
                        time.monotonic() + self.config["interval_minutes"] * 60 if self.schedule else None
                    )
                    dirty = True
                    continue
                else:
                    text = event.get("message", "")
                self.status.set(text)
                self.append_log(text)
        except queue.Empty:
            pass
        if dirty:
            self.refresh()
        if self.closing and not self.busy():
            self.root.destroy()
            return
        if self.schedule and self.next_run and not self.busy() and time.monotonic() >= self.next_run:
            self.next_run = None
            self.run()
        self.root.after(200, self.poll)

    def export(self):
        destination = filedialog.asksaveasfilename(
            parent=self.root,
            title="현재 검색 결과 전체 내보내기",
            defaultextension=".csv",
            initialfile="scuba_demo" if self.demo_mode else "scuba_data",
            filetypes=[("Excel CSV (UTF-8)", "*.csv"), ("JSON Lines", "*.jsonl")],
        )
        if not destination:
            return
        try:
            count = self.store.export(destination, **self.filters())
            self.status.set(f"{count:,}건 내보내기 완료 · {destination}")
        except OSError as exc:
            messagebox.showerror("저장 실패", str(exc), parent=self.root)

    def import_file(self):
        if not self.idle_required() or self.demo_mode:
            return
        source = self.selected_source()
        if source:
            path = filedialog.askopenfilename(
                parent=self.root, filetypes=[("JSON / JSONL", "*.json *.jsonl")]
            )
            if path:
                self.launch(lambda engine: engine.import_file(source, path))

    def replay(self):
        self.launch(lambda engine: engine.replay())

    def toggle_demo(self):
        if not self.idle_required():
            return
        self.schedule = False
        self.next_run = None
        self.schedule_button.configure(text="예약 수집 켜기")
        self.demo_mode = not self.demo_mode
        if self.demo_mode:
            self.store = Store(self.live_store.home / "demo")
            if not self.store.stats()["total"]:
                from .demo import seed

                seed(Engine(self.store, self.config, self.secrets))
        else:
            self.store = self.live_store
        self.offset = 0
        self.root.title(
            "SCUBA Collector · " + ("가상 시연 데이터" if self.demo_mode else "커뮤니티 통합 수집기")
        )
        self.status.set(
            "가상 시연 모드 · 합성 데이터이며 실제 고객 발언이 아닙니다."
            if self.demo_mode
            else "실제 로컬 저장 모드로 전환했습니다."
        )
        self.refresh()

    def credentials(self):
        if not self.idle_required():
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("API 인증 설정")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Windows: 현재 사용자 DPAPI 암호화 저장 · 다른 OS: 이번 실행 동안만 유지\n환경 변수로 지정한 값이 우선합니다. 키는 내보내기나 실행 기록에 포함하지 않습니다.",
            wraplength=650,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 16))
        values = {}
        for row, key in enumerate(KEYS, 1):
            ttk.Label(frame, text=key).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=7)
            values[key] = tk.StringVar(value=self.secrets.get(key))
            ttk.Entry(frame, textvariable=values[key], width=57, show="•" if "SECRET" in key else "").grid(
                row=row, column=1, pady=7
            )
        ttk.Label(
            frame,
            text="Reddit User-Agent 예: windows:scuba.collector:v1.0 (by /u/내계정)\nReddit은 API 사용 승인과 해당 용도의 접근 권한이 필요합니다.",
            foreground="#526176",
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=14)

        def save():
            try:
                self.secrets.save({key: value.get() for key, value in values.items()})
                dialog.destroy()
                self.status.set("API 인증 설정이 저장되었습니다.")
            except OSError as exc:
                messagebox.showerror("인증 저장 실패", str(exc), parent=dialog)

        ttk.Button(frame, text="저장", command=save).grid(row=8, column=1, sticky="e")
        dialog.grab_set()

    def settings(self):
        if not self.idle_required():
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("운영 설정")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=24)
        frame.pack()
        values = {}
        for row, (key, label) in enumerate(
            (
                ("interval_minutes", "예약 간격 (분)"),
                ("workers", "동시 수집 대상 수 (1~6)"),
                ("request_delay", "같은 호스트 요청 간격 (초, 최소 1)"),
                ("retention_days", "데이터 보관 기간 (일)"),
            )
        ):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=8, padx=(0, 18))
            values[key] = tk.StringVar(value=str(self.config[key]))
            ttk.Entry(frame, textvariable=values[key], width=14).grid(row=row, column=1)
        hashed = tk.BooleanVar(value=self.config["hash_authors"])
        ttk.Checkbutton(frame, text="작성자 HMAC 가명 처리 (원본 파일은 별도 보관)", variable=hashed).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=12
        )
        ttk.Label(
            frame,
            text="보관 기간이 지난 데이터는 시작/수집 시 정리됩니다. Reddit은 최대 48시간입니다.\n가명 처리 설정은 이후 처리되는 데이터에 적용됩니다.",
            wraplength=530,
            foreground="#526176",
        ).grid(row=6, column=0, columnspan=2, pady=10)

        def save():
            try:
                config = copy.deepcopy(self.config)
                for key, value in values.items():
                    config[key] = float(value.get()) if key == "request_delay" else int(value.get())
                config["hash_authors"] = hashed.get()
                self.config_store.save(config)
                self.config = config
                if self.schedule:
                    self.next_run = time.monotonic() + config["interval_minutes"] * 60
                dialog.destroy()
                self.status.set("운영 설정이 저장되었습니다.")
            except (ValueError, OSError) as exc:
                messagebox.showerror("입력 확인", str(exc), parent=dialog)

        ttk.Button(frame, text="저장", command=save).grid(row=7, column=1, sticky="e")
        dialog.grab_set()

    def close(self):
        self.schedule = False
        if self.busy():
            self.closing = True
            self.stop()
            self.status.set("수집 중지 후 프로그램을 종료합니다. 잠시 기다려 주세요.")
        else:
            self.root.destroy()
