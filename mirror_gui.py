#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mirror_gui.py —— 文件夹镜像同步 · 图形配置界面

把 mirror_sync.py 的全部命令行参数做成可视化配置，支持多组任务、
一键启动/停止、预演、立即同步、实时日志。纯 tkinter，零第三方依赖。

启动：双击「启动图形界面.bat」，或 python mirror_gui.py
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# --------------------------------------------------------------------------- #
# 常量与路径
# --------------------------------------------------------------------------- #

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "mirror_sync.py"
CONFIG_PATH = HERE / "gui_config.json"
LOG_DIR = HERE / "logs"
ERROR_LOG = LOG_DIR / "gui_error.log"

TITLE = "文件夹镜像同步 · 控制台"
APP_VERSION = "1.0"

# Windows：不弹出黑窗口
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 配色（适配浅色主题）
BG = "#eef1f5"
CARD = "#ffffff"
BORDER = "#d5dae1"
TEXT = "#1f2328"
MUTED = "#6b7280"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
OK = "#15803d"
WARN = "#b45309"
DANGER = "#dc2626"
SOFT = "#f3f5f8"

FONT = "Microsoft YaHei UI"
MONO = "Consolas"

DEFAULT_TASK = {
    "name": "新建任务",
    "src": "",
    "dst": "",
    "delete": False,
    "poll": False,
    "no_default_exclude": False,
    "autostart": False,
    "debounce": 1.5,
    "max_delay": 10.0,
    "interval": 600.0,
    "excludes": [],
}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def python_exe() -> str:
    """返回一个能正常输出的 Python 解释器路径（避开 pythonw 无控制台的问题）。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


def sanitize(name: str) -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in name).strip()
    return (out or "task")[:40]


def human_dur(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# --------------------------------------------------------------------------- #
# 主界面
# --------------------------------------------------------------------------- #

class MirrorGUI(tk.Tk):

    def __init__(self, silent: bool = False) -> None:
        super().__init__()
        self.silent = silent
        self._apply_dpi()

        self.tasks: list[dict] = []
        self.current_id: str | None = None
        self.procs: dict[str, dict] = {}      # task_id -> {proc, started, mode, token}
        self.q: queue.Queue = queue.Queue()
        self._syncing = False                 # 抑制表单回写
        self._stopping: set[str] = set()      # 被主动停止的任务，避免误报退出码
        self._seq = 0
        self._run_seq = 0                     # 每次启动子进程的令牌，防止新旧输出串台
        # 自检模式用独立的临时配置，绝不碰用户真实的 gui_config.json
        self.cfg_path = (HERE / "_selftest_config.json") if silent else CONFIG_PATH

        self.title(TITLE)
        self._fit_screen(1120, 800)
        self.configure(bg=BG)

        self._init_vars()
        self._setup_style()
        self._build_ui()
        self._bind_events()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._load_config()
        self._refresh_list()
        if self.tasks:
            self._select(self.tasks[0]["id"])

        self.after(80, self._pump)
        self.after(500, self._tick)

        if not silent:
            self._log_sys(f"界面就绪。主程序：{SCRIPT.name}")
            if not SCRIPT.exists():
                self._log_err(f"找不到 {SCRIPT}，请确认 mirror_gui.py 与它在同一目录。")

        # 自动启动勾选的任务
        for t in self.tasks:
            if t.get("autostart"):
                self.after(600, lambda tid=t["id"]: self._start_task(tid))

    # ------------------------------------------------------------------ #
    # 高分屏
    # ------------------------------------------------------------------ #
    def _apply_dpi(self) -> None:
        """开启 DPI 感知，并按系统缩放比例同步调整 tk 的字体换算。

        开启后窗口坐标与字体都以「物理像素」计，因此布局尺寸也要按同一比例放大，
        否则在 150%/200% 缩放的屏幕上界面会显得又小又挤。
        """
        self._dpi = 1.0
        if os.name != "nt":
            return
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:                             # noqa: BLE001
            pass
        try:
            self._dpi = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
        except Exception:                             # noqa: BLE001
            self._dpi = 1.0
        if self._dpi > 1.01:
            try:
                self.tk.call("tk", "scaling", self._dpi * 1.3333)
            except tk.TclError:
                pass

    def px(self, n: int) -> int:
        """把按 96 DPI 设计的像素值换算成当前缩放下的实际像素。"""
        return int(round(n * getattr(self, "_dpi", 1.0)))

    def _fit_screen(self, want_w: int, want_h: int) -> None:
        """按屏幕大小自适应窗口（入参按 96 DPI 逻辑像素），并居中偏上摆放。"""
        try:
            sw = self.winfo_screenwidth()      # 注意：Tk 给的是逻辑尺寸
            sh = self.winfo_screenheight()
        except tk.TclError:
            self.geometry(f"{self.px(want_w)}x{self.px(want_h)}")
            return

        # 先在逻辑坐标系里做约束，再统一换算成 geometry 需要的物理像素
        w_log = max(min(want_w, sw - 24), 860)
        h_log = max(min(want_h, sh - 70), 600)
        w, h = self.px(w_log), self.px(h_log)

        sw_p, sh_p = self.px(sw), self.px(sh)
        x = max((sw_p - w) // 2, 0)
        y = max((sh_p - h) // 3, 0)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(self.px(900), self.px(640))

    # ------------------------------------------------------------------ #
    # 变量
    # ------------------------------------------------------------------ #
    def _init_vars(self) -> None:
        self.v_name = tk.StringVar()
        self.v_src = tk.StringVar()
        self.v_dst = tk.StringVar()
        self.v_delete = tk.BooleanVar()
        self.v_poll = tk.BooleanVar()
        self.v_nodefault = tk.BooleanVar()
        self.v_autostart = tk.BooleanVar()
        self.v_debounce = tk.StringVar()
        self.v_maxdelay = tk.StringVar()
        self.v_interval = tk.StringVar()
        self.v_cur_only = tk.BooleanVar(value=True)
        self.v_status = tk.StringVar(value="未选择任务")

    # ------------------------------------------------------------------ #
    # 样式
    # ------------------------------------------------------------------ #
    def _setup_style(self) -> None:
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass

        st.configure(".", background=BG, foreground=TEXT, font=(FONT, 10))
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("Bar.TFrame", background=CARD)

        st.configure("TLabel", background=CARD, foreground=TEXT)
        st.configure("Bar.TLabel", background=CARD, foreground=TEXT)
        st.configure("Bg.TLabel", background=BG, foreground=TEXT)
        st.configure("Title.TLabel", background=BG, foreground=TEXT,
                     font=(FONT, 16, "bold"))
        st.configure("Sub.TLabel", background=BG, foreground=MUTED,
                     font=(FONT, 9))
        st.configure("Head.TLabel", background=CARD, foreground=TEXT,
                     font=(FONT, 10, "bold"))
        st.configure("Muted.TLabel", background=CARD, foreground=MUTED,
                     font=(FONT, 9))
        st.configure("Card.TLabel", background=CARD, foreground=TEXT)

        st.configure("TCheckbutton", background=CARD, foreground=TEXT,
                     focuscolor=CARD)
        st.map("TCheckbutton", background=[("active", CARD)])
        st.configure("Bar.TCheckbutton", background=CARD, foreground=TEXT,
                     focuscolor=CARD)
        st.map("Bar.TCheckbutton", background=[("active", CARD)])

        st.configure("TLabelframe", background=CARD, bordercolor=BORDER,
                     relief="solid", borderwidth=1)
        st.configure("TLabelframe.Label", background=CARD, foreground=ACCENT,
                     font=(FONT, 10, "bold"))

        st.configure("TEntry", fieldbackground="#ffffff", bordercolor=BORDER,
                     insertcolor=TEXT, padding=self.px(4))
        st.map("TEntry", bordercolor=[("focus", ACCENT)])

        st.configure("TButton", padding=(self.px(12), self.px(6)), background=SOFT,
                     bordercolor=BORDER, focuscolor=BG, font=(FONT, 9))
        st.map("TButton", background=[("active", "#e3e7ee")],
               bordercolor=[("active", "#b9c1cc")])

        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                     borderwidth=0, padding=(self.px(18), self.px(7)), font=(FONT, 10, "bold"))
        st.map("Accent.TButton",
               background=[("active", ACCENT_DARK), ("disabled", "#a8bcea")],
               foreground=[("disabled", "#ffffff")])

        st.configure("Danger.TButton", background="#fdeaea", foreground=DANGER,
                     bordercolor="#f3c6c6", padding=(self.px(14), self.px(6)), font=(FONT, 9))
        st.map("Danger.TButton",
               background=[("active", "#f8d7d7"), ("disabled", "#f2f2f2")],
               foreground=[("disabled", "#b0b0b0")])

        st.configure("Treeview", background=CARD, fieldbackground=CARD,
                     foreground=TEXT, rowheight=self.px(30), borderwidth=0,
                     font=(FONT, 10))
        st.configure("Treeview.Heading", background=SOFT, foreground=MUTED,
                     relief="flat", font=(FONT, 9), padding=(self.px(6), self.px(4)))
        st.map("Treeview.Heading", background=[("active", "#e8ecf2")])
        st.map("Treeview", background=[("selected", "#dbeafe")],
               foreground=[("selected", TEXT)])

        st.configure("Vertical.TScrollbar", background="#e6e9ee",
                     troughcolor=CARD, bordercolor=CARD, arrowcolor=MUTED)

    # ------------------------------------------------------------------ #
    # 界面搭建
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = ttk.Frame(self, style="TFrame")
        root.pack(fill="both", expand=True, padx=self.px(14), pady=self.px(12))
        root.rowconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)

        self._build_header(root)

        pv = ttk.Panedwindow(root, orient="vertical")
        pv.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

        upper = ttk.Panedwindow(pv, orient="horizontal")
        pv.add(upper, weight=3)
        pv.add(self._build_log_card(pv), weight=2)

        upper.add(self._build_task_card(upper), weight=0)
        upper.add(self._build_form_card(upper), weight=1)

        self._build_actionbar(root)

        # 初始分割位置：任务列表约 370px 宽，日志占下方约三成
        self.after(120, lambda: self._safe_sash(pv, 0, self.px(470)))
        self.after(140, lambda: self._safe_sash(upper, 0, self.px(370)))

    @staticmethod
    def _safe_sash(pw: ttk.Panedwindow, idx: int, pos: int) -> None:
        try:
            pw.sashpos(idx, pos)
        except tk.TclError:
            pass

    def _build_header(self, parent) -> None:
        bar = ttk.Frame(parent, style="TFrame")
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)

        left = ttk.Frame(bar, style="TFrame")
        left.grid(row=0, column=0, sticky="w")
        ttk.Label(left, text=TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text="选中左侧任务 → 配置好路径 → 点「启动同步」，之后源文件夹的增删改会自动同步到目标。",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 0))

        right = ttk.Frame(bar, style="TFrame")
        right.grid(row=0, column=2, sticky="e")
        self.lbl_global = ttk.Label(right, text="● 空闲", style="Bg.TLabel",
                                    foreground=MUTED, font=(FONT, 10, "bold"))
        self.lbl_global.pack(anchor="e")

    # -- 任务列表 ---------------------------------------------------------- #
    def _build_task_card(self, parent) -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=self.px(12))
        card.rowconfigure(1, weight=1)
        card.columnconfigure(0, weight=1)

        ttk.Label(card, text="同步任务", style="Head.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8))

        wrap = ttk.Frame(card, style="Card.TFrame")
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(wrap, columns=("name", "state"),
                                 show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="任务")
        self.tree.heading("name", text="状态")
        self.tree.column("#0", width=self.px(200), stretch=True, minwidth=self.px(120))
        self.tree.column("name", width=self.px(130), stretch=False, minwidth=self.px(110), anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)

        self.tree.tag_configure("run", foreground=OK)
        self.tree.tag_configure("idle", foreground=MUTED)

        btns = ttk.Frame(card, style="Card.TFrame")
        btns.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text="＋ 新建", command=self._new_task).pack(side="left")
        ttk.Button(btns, text="⧉ 复制", command=self._dup_task).pack(side="left", padx=6)
        ttk.Button(btns, text="🗑 删除", style="Danger.TButton",
                   command=self._del_task).pack(side="left")

        card.columnconfigure(0, weight=1)
        return card

    # -- 配置表单 ---------------------------------------------------------- #
    def _build_form_card(self, parent) -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=self.px(12))
        card.columnconfigure(0, weight=1)
        card.rowconfigure(2, weight=1)

        # 基本
        b = ttk.Labelframe(card, text=" 文件夹 ", padding=(self.px(12), self.px(8)))
        b.grid(row=0, column=0, sticky="ew")
        b.columnconfigure(1, weight=1)

        ttk.Label(b, text="任务名称", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", pady=4, padx=(0, 8))
        ttk.Entry(b, textvariable=self.v_name).grid(
            row=0, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(b, text="源文件夹", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=4, padx=(0, 8))
        ttk.Entry(b, textvariable=self.v_src).grid(
            row=1, column=1, sticky="ew", pady=4)
        ttk.Button(b, text="浏览…", command=lambda: self._browse(self.v_src)).grid(
            row=1, column=2, sticky="e", padx=(6, 0), pady=4)

        ttk.Label(b, text="目标文件夹", style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", pady=4, padx=(0, 8))
        ttk.Entry(b, textvariable=self.v_dst).grid(
            row=2, column=1, sticky="ew", pady=4)
        ttk.Button(b, text="浏览…", command=lambda: self._browse(self.v_dst)).grid(
            row=2, column=2, sticky="e", padx=(6, 0), pady=4)

        ttk.Label(b, text="以「源」为准，目标会被改成与它完全一致",
                  style="Muted.TLabel").grid(row=3, column=1, sticky="w", pady=(2, 0))

        # 选项
        o = ttk.Labelframe(card, text=" 同步选项 ", padding=(self.px(12), self.px(8)))
        o.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        o.columnconfigure(0, weight=1)
        o.columnconfigure(1, weight=1)

        ttk.Checkbutton(o, text="镜像删除（源删了目标也删）",
                        variable=self.v_delete).grid(row=0, column=0, sticky="w", pady=3)
        ttk.Checkbutton(o, text="轮询模式（网盘 / 网络盘用）",
                        variable=self.v_poll).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Checkbutton(o, text="不使用内置默认排除规则",
                        variable=self.v_nodefault).grid(row=1, column=0, sticky="w", pady=3)
        ttk.Checkbutton(o, text="打开界面时自动启动本任务",
                        variable=self.v_autostart).grid(row=1, column=1, sticky="w", pady=3)

        nums = ttk.Frame(o, style="Card.TFrame")
        nums.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._num_field(nums, 0, "安静等待", self.v_debounce, "秒")
        self._num_field(nums, 1, "最长等待", self.v_maxdelay, "秒")
        self._num_field(nums, 2, "兜底校验", self.v_interval, "秒（0 关闭）")

        ttk.Label(o,
                  text="安静等待：变更后安静多久开始同步　|　最长等待：持续写入时的强制同步间隔"
                       "　|　兜底校验：定期全量比对，防漏事件",
                  style="Muted.TLabel", wraplength=self.px(640),
                  justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # 排除规则
        e = ttk.Labelframe(card, text=" 额外排除规则（每行一个通配模式，如 *.bak） ",
                           padding=(self.px(12), self.px(8)))
        e.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        e.rowconfigure(0, weight=1)
        e.columnconfigure(0, weight=1)

        self.txt_excl = tk.Text(e, height=3, font=(MONO, 10), bg="#ffffff",
                                fg=TEXT, insertbackground=TEXT, relief="flat",
                                highlightthickness=1, highlightbackground=BORDER,
                                highlightcolor=ACCENT, wrap="none", padx=6, pady=4)
        self.txt_excl.grid(row=0, column=0, sticky="nsew")
        esb = ttk.Scrollbar(e, orient="vertical", command=self.txt_excl.yview)
        esb.grid(row=0, column=1, sticky="ns")
        self.txt_excl.configure(yscrollcommand=esb.set)

        return card

    def _num_field(self, parent: ttk.Frame, col: int, label: str,
                   var: tk.StringVar, unit: str) -> None:
        f = ttk.Frame(parent, style="Card.TFrame")
        f.grid(row=0, column=col, sticky="w", padx=(0, self.px(24)))
        ttk.Label(f, text=label, style="Muted.TLabel").pack(side="left", padx=(0, 5))
        ttk.Entry(f, textvariable=var, width=6).pack(side="left")
        ttk.Label(f, text=unit, style="Muted.TLabel").pack(side="left", padx=(5, 0))

    # -- 日志 -------------------------------------------------------------- #
    def _build_log_card(self, parent) -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=self.px(12))
        card.rowconfigure(1, weight=1)
        card.columnconfigure(0, weight=1)

        head = ttk.Frame(card, style="Card.TFrame")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        head.columnconfigure(1, weight=1)

        ttk.Label(head, text="运行日志", style="Head.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(head, text="只看当前任务", variable=self.v_cur_only,
                        style="Bar.TCheckbutton").grid(row=0, column=1, sticky="w", padx=self.px(14))
        ttk.Button(head, text="打开日志文件夹",
                   command=self._open_log_dir).grid(row=0, column=2, sticky="e")
        ttk.Button(head, text="清空", command=self._clear_log).grid(
            row=0, column=3, sticky="e", padx=(6, 0))

        wrap = ttk.Frame(card, style="Card.TFrame")
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self.txt_log = tk.Text(wrap, font=(MONO, 9), bg="#fbfcfe", fg=TEXT,
                               relief="flat", wrap="none", padx=8, pady=6,
                               highlightthickness=1, highlightbackground=BORDER,
                               state="disabled")
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt_log.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.txt_log.configure(yscrollcommand=lsb.set)

        self.txt_log.tag_configure("sys", foreground=MUTED)
        self.txt_log.tag_configure("info", foreground=TEXT)
        self.txt_log.tag_configure("change", foreground=OK)
        self.txt_log.tag_configure("warn", foreground=WARN)
        self.txt_log.tag_configure("error", foreground=DANGER)
        self.txt_log.tag_configure("tag", foreground=ACCENT)
        self.txt_log.tag_configure("sep", foreground="#c8ced8")
        return card

    # -- 操作栏 ------------------------------------------------------------ #
    def _build_actionbar(self, parent) -> None:
        bar = ttk.Frame(parent, style="TFrame")
        bar.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        bar.columnconfigure(1, weight=1)

        self.btn_start = ttk.Button(bar, text="▶ 启动同步", style="Accent.TButton",
                                    command=lambda: self._start_task(None))
        self.btn_start.grid(row=0, column=0, sticky="w")

        mid = ttk.Frame(bar, style="TFrame")
        mid.grid(row=0, column=1, sticky="w", padx=self.px(10))
        ttk.Button(mid, text="■ 停止", command=lambda: self._stop_task(None)).pack(side="left")
        ttk.Button(mid, text="👁 预演（不改文件）",
                   command=lambda: self._run_once(dry=True)).pack(side="left", padx=6)
        ttk.Button(mid, text="⟳ 立即同步一次",
                   command=lambda: self._run_once(dry=False)).pack(side="left")

        right = ttk.Frame(bar, style="TFrame")
        right.grid(row=0, column=2, sticky="e")
        self.lbl_status = ttk.Label(right, textvariable=self.v_status,
                                    style="Bg.TLabel", foreground=MUTED,
                                    font=(FONT, 9))
        self.lbl_status.pack(side="right")

    # ------------------------------------------------------------------ #
    # 事件绑定
    # ------------------------------------------------------------------ #
    def _bind_events(self) -> None:
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        for var in (self.v_name, self.v_src, self.v_dst, self.v_debounce,
                    self.v_maxdelay, self.v_interval):
            var.trace_add("write", lambda *_: self._on_form_change())
        for var in (self.v_delete, self.v_poll, self.v_nodefault, self.v_autostart):
            var.trace_add("write", lambda *_: self._on_form_change())
        self.txt_excl.bind("<<Modified>>", self._on_excl_change)

    # ------------------------------------------------------------------ #
    # 表单 ↔ 数据
    # ------------------------------------------------------------------ #
    def _current_task(self) -> dict | None:
        for t in self.tasks:
            if t["id"] == self.current_id:
                return t
        return None

    def _on_form_change(self) -> None:
        if self._syncing:
            return
        t = self._current_task()
        if not t:
            return
        t["name"] = self.v_name.get().strip() or "未命名任务"
        t["src"] = self.v_src.get().strip()
        t["dst"] = self.v_dst.get().strip()
        t["delete"] = bool(self.v_delete.get())
        t["poll"] = bool(self.v_poll.get())
        t["no_default_exclude"] = bool(self.v_nodefault.get())
        t["autostart"] = bool(self.v_autostart.get())
        t["debounce"] = self._num(self.v_debounce.get(), 1.5)
        t["max_delay"] = self._num(self.v_maxdelay.get(), 10.0)
        t["interval"] = self._num(self.v_interval.get(), 600.0)
        self._refresh_row(t["id"])
        self._validate_hint()

    @staticmethod
    def _num(text: str, fallback: float) -> float:
        try:
            v = float(str(text).strip())
            return v if v >= 0 else fallback
        except (TypeError, ValueError):
            return fallback

    def _on_excl_change(self, _evt=None) -> None:
        if not self.txt_excl.edit_modified():
            return
        self.txt_excl.edit_modified(False)
        if self._syncing:
            return
        t = self._current_task()
        if not t:
            return
        raw = self.txt_excl.get("1.0", "end")
        t["excludes"] = [ln.strip() for ln in raw.splitlines() if ln.strip()]

    def _fill_form(self, t: dict) -> None:
        self._syncing = True
        try:
            self.v_name.set(t.get("name", ""))
            self.v_src.set(t.get("src", ""))
            self.v_dst.set(t.get("dst", ""))
            self.v_delete.set(bool(t.get("delete")))
            self.v_poll.set(bool(t.get("poll")))
            self.v_nodefault.set(bool(t.get("no_default_exclude")))
            self.v_autostart.set(bool(t.get("autostart")))
            self.v_debounce.set(str(t.get("debounce", 1.5)))
            self.v_maxdelay.set(str(t.get("max_delay", 10.0)))
            self.v_interval.set(str(t.get("interval", 600.0)))
            self.txt_excl.delete("1.0", "end")
            ex = t.get("excludes") or []
            if ex:
                self.txt_excl.insert("1.0", "\n".join(ex))
            self.txt_excl.edit_modified(False)
        finally:
            self._syncing = False

    # ------------------------------------------------------------------ #
    # 列表
    # ------------------------------------------------------------------ #
    def _refresh_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for t in self.tasks:
            self.tree.insert("", "end", iid=t["id"], text="  " + t["name"])
            self._refresh_row(t["id"])
        if self.current_id and self.tree.exists(self.current_id):
            self.tree.selection_set(self.current_id)

    def _refresh_row(self, tid: str) -> None:
        if not self.tree.exists(tid):
            return
        t = next((x for x in self.tasks if x["id"] == tid), None)
        if not t:
            return
        self.tree.item(tid, text="  " + t["name"])
        st, tag = self._state_text(tid)
        self.tree.set(tid, "name", st)
        self.tree.item(tid, tags=(tag,))

    def _state_text(self, tid: str) -> tuple[str, str]:
        info = self.procs.get(tid)
        if info and info.get("proc") and info["proc"].poll() is None:
            dur = human_dur(time.time() - info["started"])
            label = {"watch": "运行中", "once": "同步中", "dry": "预演中"}.get(info["mode"], "运行中")
            return f"● {label} {dur}", "run"
        return "○ 已停止", "idle"

    def _on_tree_select(self, _evt=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        tid = sel[0]
        if tid == self.current_id:
            return
        self.current_id = tid
        t = self._current_task()
        if t:
            self._fill_form(t)
            self._validate_hint()
        self._clear_log()

    # ------------------------------------------------------------------ #
    # 任务增删改
    # ------------------------------------------------------------------ #
    def _new_id(self) -> str:
        self._seq += 1
        return f"t{int(time.time() * 1000)}_{self._seq}"

    def _new_task(self) -> None:
        t = dict(DEFAULT_TASK)
        n = len([x for x in self.tasks if x["name"].startswith("新建任务")]) + 1
        t["name"] = f"新建任务{n}"
        t["id"] = self._new_id()
        self.tasks.append(t)
        self._refresh_list()
        self._select(t["id"])
        self._save_config()

    def _dup_task(self) -> None:
        t = self._current_task()
        if not t:
            return
        c = dict(t)
        c["id"] = self._new_id()
        c["name"] = t["name"] + " 副本"
        c["excludes"] = list(t.get("excludes") or [])
        c["autostart"] = False
        self.tasks.append(c)
        self._refresh_list()
        self._select(c["id"])
        self._save_config()

    def _del_task(self) -> None:
        t = self._current_task()
        if not t:
            messagebox.showinfo("提示", "请先在左侧选择一个任务。", parent=self)
            return
        if not messagebox.askyesno("删除任务",
                                   f"确定删除任务「{t['name']}」？\n\n"
                                   "只会移除这条配置，不会删除任何文件夹。", parent=self):
            return
        self._stop_task(t["id"], quiet=True)
        self.tasks = [x for x in self.tasks if x["id"] != t["id"]]
        self.current_id = None
        self._refresh_list()
        if self.tasks:
            self._select(self.tasks[0]["id"])
        else:
            self._fill_form(dict(DEFAULT_TASK))
        self._save_config()

    def _select(self, tid: str) -> None:
        if not self.tree.exists(tid):
            return
        self.tree.selection_set(tid)
        self.tree.focus(tid)
        self.current_id = tid
        t = self._current_task()
        if t:
            self._fill_form(t)
        self._validate_hint()

    # ------------------------------------------------------------------ #
    # 命令构造
    # ------------------------------------------------------------------ #
    def _build_cmd(self, t: dict, once: bool = False, dry: bool = False) -> list[str]:
        cmd = [python_exe(), "-u", str(SCRIPT),
               "--src", t["src"], "--dst", t["dst"],
               "--debounce", str(t.get("debounce", 1.5)),
               "--max-delay", str(t.get("max_delay", 10.0)),
               "--check", str(t.get("interval", 600.0))]
        if t.get("delete"):
            cmd.append("--delete")
        if t.get("poll"):
            cmd.append("--poll")
        if t.get("no_default_exclude"):
            cmd.append("--no-default-exclude")
        for pat in (t.get("excludes") or []):
            cmd += ["--exclude", pat]
        if once:
            cmd.append("--once")
        if dry:
            cmd.append("--dry-run")
        logfile = LOG_DIR / f"{sanitize(t.get('name', 'task'))}_{datetime.now():%Y-%m-%d}.log"
        cmd += ["--log", str(logfile)]
        return cmd

    def _set_status(self, text: str, color: str) -> None:
        self.v_status.set(text)
        lbl = getattr(self, "lbl_status", None)
        if lbl is not None:
            try:
                lbl.configure(foreground=color)
            except tk.TclError:
                pass

    def _validate_hint(self) -> str:
        t = self._current_task()
        if not t:
            self._set_status("未选择任务", MUTED)
            return ""
        if not t["src"]:
            self._set_status("① 请选择源文件夹", WARN)
        elif not os.path.isdir(t["src"]):
            self._set_status("⚠ 源文件夹不存在，请重新选择", DANGER)
        elif not t["dst"]:
            self._set_status("② 请选择目标文件夹", WARN)
        elif os.path.abspath(t["src"]).lower().rstrip("\\/") == \
                os.path.abspath(t["dst"]).lower().rstrip("\\/"):
            self._set_status("⚠ 源与目标不能是同一个文件夹", DANGER)
        else:
            self._set_status("✔ 配置就绪，可以启动", OK)
        return self.v_status.get()

    def _precheck(self, t: dict) -> bool:
        if not t["src"] or not os.path.isdir(t["src"]):
            messagebox.showerror("配置不完整",
                                 f"源文件夹不存在：\n{t['src'] or '（未填写）'}",
                                 parent=self)
            return False
        if not t["dst"]:
            messagebox.showerror("配置不完整", "请先选择目标文件夹。", parent=self)
            return False
        s = os.path.abspath(t["src"]).lower().rstrip("\\/")
        d = os.path.abspath(t["dst"]).lower().rstrip("\\/")
        if s == d:
            messagebox.showerror("配置错误", "源文件夹与目标文件夹不能相同。", parent=self)
            return False
        if d.startswith(s + os.sep) or s.startswith(d + os.sep):
            messagebox.showerror("配置错误",
                                 "源与目标不能互相包含（会造成无限递归）。", parent=self)
            return False
        return True

    # ------------------------------------------------------------------ #
    # 启动 / 停止
    # ------------------------------------------------------------------ #
    def _start_task(self, tid: str | None) -> None:
        tid = tid or self.current_id
        t = next((x for x in self.tasks if x["id"] == tid), None)
        if not t:
            messagebox.showinfo("提示", "请先在左侧选择一个任务。", parent=self)
            return
        if self._is_running(tid):
            return
        if not self._precheck(t):
            return
        if not SCRIPT.exists():
            messagebox.showerror("错误", f"找不到主程序：\n{SCRIPT}", parent=self)
            return

        if t.get("delete") and not self._confirm_delete(t):
            return

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        cmd = self._build_cmd(t)
        self._spawn(t, cmd, mode="watch")

    def _confirm_delete(self, t: dict) -> bool:
        return messagebox.askyesno(
            "确认开启镜像删除",
            f"任务「{t['name']}」已勾选「镜像删除」。\n\n"
            "这意味着：源文件夹里删掉的文件，目标文件夹里也会被删除。\n"
            "如果目标里有些文件是你手工放的、源里没有，它们会被清掉。\n\n"
            "确定要启动吗？（可先点「预演」看看会动哪些文件）",
            parent=self, icon="warning")

    def _run_once(self, dry: bool) -> None:
        tid = self.current_id
        t = self._current_task()
        if not t:
            messagebox.showinfo("提示", "请先在左侧选择一个任务。", parent=self)
            return
        if self._is_running(tid):
            messagebox.showinfo("提示", "该任务正在运行中，请先停止再执行单次操作。", parent=self)
            return
        if not self._precheck(t):
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        cmd = self._build_cmd(t, once=True, dry=dry)
        self._spawn(t, cmd, mode=("dry" if dry else "once"))

    def _spawn(self, t: dict, cmd: list[str], mode: str) -> None:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # 管道模式下避免 cp936 编码把中文日志写崩
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(HERE),
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self._log_err(f"启动失败：{exc}")
            return

        tid = t["id"]
        self._run_seq += 1
        token = self._run_seq
        self.procs[tid] = {"proc": proc, "started": time.time(),
                           "mode": mode, "token": token}
        self._stopping.discard(tid)

        self._log_sep()
        label = {"watch": "启动实时同步", "once": "执行单次同步", "dry": "预演"}[mode]
        self._log_sys(f"【{t['name']}】{label}　PID {proc.pid}")
        self._log_sys(f"  源　：{t['src']}")
        self._log_sys(f"  目标：{t['dst']}")

        th = threading.Thread(target=self._reader, args=(tid, token, proc), daemon=True)
        th.start()
        self._refresh_row(tid)
        self._update_global()

    def _reader(self, tid: str, token: int, proc: subprocess.Popen) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self.q.put(("line", tid, token, line.rstrip("\r\n")))
        except Exception as exc:                      # noqa: BLE001
            self.q.put(("line", tid, token, f"[读取输出异常] {exc}"))
        finally:
            try:
                proc.stdout.close()                   # type: ignore[union-attr]
            except Exception:                         # noqa: BLE001
                pass
            rc = proc.wait()
            self.q.put(("exit", tid, token, rc))

    def _stop_task(self, tid: str | None, quiet: bool = False) -> None:
        tid = tid or self.current_id
        info = self.procs.get(tid)
        t = next((x for x in self.tasks if x["id"] == tid), None)
        if not info:
            if not quiet:
                self._log_sys("该任务当前未在运行。")
            return
        proc = info["proc"]
        self._stopping.add(tid)
        if proc.poll() is None:
            self._kill_tree(proc.pid)
        self.procs.pop(tid, None)
        if not quiet:
            name = t["name"] if t else tid
            self._log_warn(f"【{name}】已停止")
        self._refresh_row(tid)
        self._update_global()

    @staticmethod
    def _kill_tree(pid: int) -> None:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               creationflags=CREATE_NO_WINDOW, timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except Exception:                         # noqa: BLE001
                pass
        try:
            import signal
            os.kill(pid, signal.SIGTERM)
        except Exception:                             # noqa: BLE001
            pass

    def _is_running(self, tid: str | None) -> bool:
        info = self.procs.get(tid) if tid else None
        return bool(info and info["proc"] and info["proc"].poll() is None)

    # ------------------------------------------------------------------ #
    # 输出泵
    # ------------------------------------------------------------------ #
    def _pump(self) -> None:
        try:
            while True:
                kind, tid, token, payload = self.q.get_nowait()
                info = self.procs.get(tid)
                # 令牌不匹配 = 上一轮进程的迟到消息（任务被停止后又重启），直接丢弃
                if not info or info.get("token") != token:
                    continue
                if kind == "line":
                    self._log_line(tid, str(payload))
                else:
                    self._on_exit(tid, int(payload))
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _on_exit(self, tid: str, rc: int) -> None:
        info = self.procs.pop(tid, None)
        t = next((x for x in self.tasks if x["id"] == tid), None)
        name = t["name"] if t else tid
        if info and info.get("mode") in ("once", "dry"):
            if rc == 0:
                self._log_sys(f"【{name}】已完成。")
            else:
                self._log_err(f"【{name}】异常结束，返回码 {rc}。")
        elif rc == 0:
            self._log_sys(f"【{name}】进程已正常退出。")
        else:
            self._log_err(f"【{name}】进程结束，返回码 {rc}（若刚点过停止可忽略）。")
        self._stopping.discard(tid)
        self._refresh_row(tid)
        self._update_global()

    def _tick(self) -> None:
        for tid in list(self.procs.keys()):
            self._refresh_row(tid)
        self._update_global()
        self.after(1000, self._tick)

    def _update_global(self) -> None:
        n = sum(1 for tid, i in self.procs.items()
                if i["proc"] and i["proc"].poll() is None)
        if n:
            self.lbl_global.configure(text=f"● {n} 个任务运行中", foreground=OK)
        else:
            self.lbl_global.configure(text="● 空闲", foreground=MUTED)

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #
    def _log_tid_allowed(self, tid: str) -> bool:
        if not self.v_cur_only.get():
            return True
        return tid == self.current_id

    def _log_line(self, tid: str, line: str) -> None:
        if not self._log_tid_allowed(tid):
            return
        tag = "info"
        body = line
        if "] »" in line:
            tag = "change"
        elif "] !" in line:
            tag = "warn"
        elif "] ×" in line:
            tag = "error"
        elif line.startswith("["):
            tag = "info"
        elif line.startswith("  "):
            tag = "sys"
        self._append(body + "\n", tag)

    def _log_sys(self, msg: str) -> None:
        self._append(msg + "\n", "sys")

    def _log_warn(self, msg: str) -> None:
        self._append(msg + "\n", "warn")

    def _log_err(self, msg: str) -> None:
        self._append(msg + "\n", "error")

    def _log_sep(self) -> None:
        self._append("─" * 80 + "\n", "sep")

    def _append(self, text: str, tag: str) -> None:
        w = self.txt_log
        w.configure(state="normal")
        w.insert("end", text, tag)
        # 限制总行数，避免长时间运行吃内存
        try:
            if int(w.index("end-1c").split(".")[0]) > 5000:
                w.delete("1.0", "1000.0")
        except Exception:                             # noqa: BLE001
            pass
        w.see("end")
        w.configure(state="disabled")

    def _clear_log(self) -> None:
        w = self.txt_log
        w.configure(state="normal")
        w.delete("1.0", "end")
        w.configure(state="disabled")

    def _open_log_dir(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(LOG_DIR))            # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(LOG_DIR)])
        except Exception as exc:                      # noqa: BLE001
            messagebox.showerror("打开失败", str(exc), parent=self)

    # ------------------------------------------------------------------ #
    # 配置读写
    # ------------------------------------------------------------------ #
    def _load_config(self) -> None:
        if not self.cfg_path.exists():
            t = dict(DEFAULT_TASK)
            t["id"] = self._new_id()
            t["name"] = "我的同步任务"
            self.tasks = [t]
            return
        try:
            data = json.loads(self.cfg_path.read_text(encoding="utf-8"))
            raw = data.get("tasks") or []
            out = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                t = dict(DEFAULT_TASK)
                t.update({k: v for k, v in item.items() if v is not None})
                t["id"] = item.get("id") or self._new_id()
                t.setdefault("excludes", [])
                out.append(t)
            self.tasks = out or []
        except Exception as exc:                      # noqa: BLE001
            messagebox.showwarning("配置读取失败",
                                   f"配置文件损坏，已忽略：\n{exc}", parent=self)
            self.tasks = []

    def _save_config(self) -> None:
        data = {"version": APP_VERSION, "tasks": self.tasks}
        try:
            tmp = self.cfg_path.parent / (self.cfg_path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.cfg_path)
        except OSError as exc:
            self._log_err(f"保存配置失败：{exc}")

    # ------------------------------------------------------------------ #
    # 关闭
    # ------------------------------------------------------------------ #
    def _on_close(self) -> None:
        running = [tid for tid in self.procs if self._is_running(tid)]
        if running:
            names = "、".join(
                next((x["name"] for x in self.tasks if x["id"] == tid), tid)
                for tid in running)
            if not messagebox.askyesno(
                    "退出",
                    f"还有 {len(running)} 个任务在运行：\n{names}\n\n"
                    "退出会停止它们，同步将中断。确定退出吗？", parent=self):
                return
        self._save_config()
        for tid in list(self.procs.keys()):
            self._stop_task(tid, quiet=True)
        self.destroy()

    # ------------------------------------------------------------------ #
    # 自检（无头）
    # ------------------------------------------------------------------ #
    def _selftest(self) -> int:
        lines: list[str] = []
        fails = 0

        def ck(cond: bool, label: str, detail: str = "") -> None:
            nonlocal fails
            if not cond:
                fails += 1
            lines.append(f"  {'PASS' if cond else 'FAIL'}  {label}"
                         + (f"   [{detail}]" if detail else ""))

        # 1) 默认已有一个任务，界面构建正常
        ck(len(self.tasks) == 1, "启动时自动创建一个任务", f"{len(self.tasks)} 个")
        ck(self.current_id is not None, "首个任务已选中")

        # 2) 表单回写
        self.v_name.set("自检任务")
        self.v_src.set(str(HERE))
        self.v_dst.set(str(HERE / "_selftest_dst"))
        self.v_delete.set(True)
        self.v_debounce.set("2.5")
        self.update_idletasks()
        self.txt_excl.insert("1.0", "*.bak\n*.log")
        self.update()
        self._on_excl_change()          # 兜底：确保排除规则被收进任务数据
        t = self._current_task() or {}
        ck(t.get("name") == "自检任务", "任务名回写", str(t.get("name")))
        ck(t.get("delete") is True, "镜像删除回写")
        ck(abs(t.get("debounce", 0) - 2.5) < 1e-6, "防抖时长回写", str(t.get("debounce")))
        ck("*.bak" in (t.get("excludes") or []), "排除规则回写", str(t.get("excludes")))

        # 3) 命令拼装
        cmd = self._build_cmd(t)
        joined = " ".join(cmd)
        ck("--delete" in cmd, "命令行含 --delete")
        ck("--exclude" in cmd and "*.bak" in cmd, "命令行含自定义排除")
        ck("--debounce" in cmd and "2.5" in cmd, "命令行含防抖参数")
        cmd2 = self._build_cmd(t, once=True, dry=True)
        ck("--once" in cmd2 and "--dry-run" in cmd2, "单次预演命令正确")
        ck("-u" in cmd, "以无缓冲模式启动子进程")

        # 4) 路径护栏
        bad = dict(t, src="D:\\a", dst="D:\\a")
        ck(self._same_path(bad) is True, "识别出源与目标相同")
        inner = dict(t, src="D:\\a", dst="D:\\a\\b")
        ck(self._nested(inner) is True, "识别出目标在源内部")

        # 5) 配置保存 / 读取
        self._save_config()
        ck(self.cfg_path.exists(), "配置文件已写出")
        raw = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        ck(raw["tasks"][0]["name"] == "自检任务", "配置内容正确")

        # 6) 日志渲染
        self.v_cur_only.set(False)
        self._clear_log()
        self._log_line("t", "[10:00:00] » 更新 a.txt")
        self._log_line("t", "[10:00:01] ! 文件仍在写入")
        self._log_line("t", "[10:00:02] × 出错了")
        content = self.txt_log.get("1.0", "end")
        ck("更新 a.txt" in content, "日志写入正常")

        # 清理自检产物，绝不动用户真实配置
        try:
            self.cfg_path.unlink(missing_ok=True)
        except OSError:
            pass

        print("mirror_gui 自检结果")
        print("\n".join(lines))
        print(f"\n合计 {len(lines) - fails}/{len(lines)} 项通过")
        return 0 if fails == 0 else 1

    def _same_path(self, t: dict) -> bool:
        s = os.path.abspath(t["src"]).lower().rstrip("\\/")
        d = os.path.abspath(t["dst"]).lower().rstrip("\\/")
        return s == d

    def _nested(self, t: dict) -> bool:
        s = os.path.abspath(t["src"]).lower().rstrip("\\/")
        d = os.path.abspath(t["dst"]).lower().rstrip("\\/")
        return d.startswith(s + os.sep) or s.startswith(d + os.sep)

    def _browse(self, var: tk.StringVar) -> None:
        cur = var.get().strip()
        init = cur if cur and os.path.isdir(cur) else str(Path.home())
        path = filedialog.askdirectory(title="选择文件夹", initialdir=init, parent=self)
        if path:
            var.set(os.path.normpath(path))

    # ------------------------------------------------------------------ #
    # 出错兜底：Tk 回调里的异常不会走 sys.excepthook，这里单独接住
    # ------------------------------------------------------------------ #
    def report_callback_exception(self, exc, val, tb) -> None:
        text = "".join(traceback.format_exception(exc, val, tb))
        _write_error_log(text)
        try:
            messagebox.showerror(
                "操作出错",
                f"{val}\n\n详细信息已写入：\n{ERROR_LOG}",
                parent=self)
        except Exception:                             # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 出错记录与启动前自检
# --------------------------------------------------------------------------- #

def _write_error_log(text: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n{text}")
    except Exception:                                 # noqa: BLE001
        pass


def _install_excepthook() -> None:
    """把未捕获异常写进日志并弹窗。

    用 pythonw.exe 启动时没有控制台，崩溃了用户什么都看不到，
    这是「双击一闪而过」最常见的成因，所以必须兜住。
    """
    def hook(exc_type, exc, tb) -> None:
        _write_error_log("".join(traceback.format_exception(exc_type, exc, tb)))
        try:
            messagebox.showerror(
                "程序出错",
                f"{exc_type.__name__}: {exc}\n\n详细信息已写入：\n{ERROR_LOG}")
        except Exception:                             # noqa: BLE001
            pass
    sys.excepthook = hook


def _check_env() -> int:
    """启动前环境自检，结果打印到控制台，供 .bat 调用。"""
    print("=" * 62)
    print("  文件夹镜像同步 · 启动前自检")
    print("=" * 62)
    ok = True

    major, minor = sys.version_info[:2]
    print(f"  解释器 : {sys.executable}")
    print(f"  Python : {major}.{minor}.{sys.version_info[2]}")
    if (major, minor) < (3, 10):
        print("  [X] 需要 Python 3.10 或更高版本")
        ok = False
    else:
        print("  [OK] 版本满足要求")

    try:
        import tkinter as _tk
        try:
            tcl = _tk.Tcl().eval("info patchlevel")
        except Exception:                             # noqa: BLE001
            tcl = "未知"
        print(f"  tkinter: 可用（Tcl/Tk {tcl}）")
    except Exception as exc:                          # noqa: BLE001
        print(f"  [X] 无法加载 tkinter：{exc}")
        print("       请重新安装 Python，安装时勾选「tcl/tk and IDLE」。")
        print("=" * 62)
        return 1

    try:
        probe_root = _tk.Tk()
        probe_root.withdraw()
        probe_root.destroy()
        print("  [OK] 能够创建图形窗口")
    except Exception as exc:                          # noqa: BLE001
        print(f"  [X] 无法创建图形窗口：{exc}")
        ok = False

    if SCRIPT.exists():
        print(f"  [OK] 主程序就位：{SCRIPT.name}")
    else:
        print(f"  [X] 找不到主程序：{SCRIPT}")
        print("       请确认 mirror_gui.py 与 mirror_sync.py 在同一个文件夹内。")
        ok = False

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        probe = HERE / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print(f"  [OK] 程序目录可读写：{HERE}")
    except Exception as exc:                          # noqa: BLE001
        print(f"  [X] 程序目录不可写：{exc}")
        print("       请把整个文件夹移到「文档」或「桌面」等位置后重试。")
        ok = False

    if CONFIG_PATH.exists():
        try:
            json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            print(f"  [OK] 配置文件正常：{CONFIG_PATH.name}")
        except Exception as exc:                      # noqa: BLE001
            print(f"  [!] 配置文件已损坏，启动时会自动重建：{exc}")

    print("=" * 62)
    print("  自检通过，正在启动界面…" if ok else "  自检未通过，请按上面的提示处理后重试。")
    print("=" * 62)
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main() -> int:
    if "--check" in sys.argv:
        return _check_env()

    silent_test = "--selftest" in sys.argv
    _install_excepthook()

    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "mirrorsync.gui.1")
        except Exception:                             # noqa: BLE001
            pass

    app = MirrorGUI(silent=silent_test)

    if silent_test:
        code = {"v": 1}
        app.after(400, lambda: (code.update(v=app._selftest()), app.destroy()))
        app.mainloop()
        return code["v"]

    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
