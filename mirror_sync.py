#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mirror_sync.py —— 实时文件夹镜像同步工具

把「源文件夹」的内容实时保持到「目标文件夹」，一切以源为准。
纯 Python 标准库实现，无需安装任何第三方包。

核心行为
  · 首次启动先做一次全量比对，之后进入常驻监听
  · Windows 下用系统原生事件 ReadDirectoryChangesW（真正实时，零轮询开销）
  · 非 Windows 或事件接口不可用时自动降级为定时轮询
  · 事件防抖：一次保存往往触发多个事件，等安静若干秒后统一同步
  · 原子写入：先写 .mstmp 临时文件再改名，目标端不会出现半截文件
  · 写入稳定检测：源文件还在被写入时先不下手，避免复制到不完整的文件
  · 删除默认关闭：不加 --delete 时只增不删，防止误删扩散到目标
  · 周期兜底：定期全量校验一次，防止极端情况下事件丢失导致不一致

用法示例
  python mirror_sync.py --src "D:\\教学资料" --dst "E:\\备份\\教学资料"
  python mirror_sync.py --src "D:\\课件" --dst "F:\\网盘\\课件" --delete
  python mirror_sync.py --src "D:\\课件" --dst "E:\\课件" --once --dry-run
"""

from __future__ import annotations

import argparse
import ctypes
import fnmatch
import os
import shutil
import stat
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

VERSION = "1.0"
TMP_SUFFIX = ".mstmp"

# 默认排除：系统/Office/浏览器的临时产物，以及本工具自己的临时文件
DEFAULT_EXCLUDES = [
    "~$*",            # Office 打开文档时生成的锁文件
    "*.tmp", "*.temp",
    "*.mstmp",        # 本工具的写入中间态
    "*.crdownload", "*.part", "*.partial",
    "Thumbs.db", "desktop.ini", ".DS_Store",
    "$RECYCLE.BIN", "System Volume Information",
]

MAX_PATH_HINT = (
    "路径过长（超过 260 字符）。请在「组策略 / 注册表」中启用长路径支持：\n"
    "  HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem\\LongPathsEnabled = 1\n"
    "或把目标目录换到层级更浅的位置。"
)


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #

class Log:
    """同时输出到控制台和日志文件。"""

    def __init__(self, path: str | None = None, quiet: bool = False) -> None:
        self.quiet = quiet
        self._fh = None
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8", buffering=1)

    def _write(self, tag: str, msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {tag} {msg}"
        print(line, flush=True)
        if self._fh:
            self._fh.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {tag} {msg}\n")

    def info(self, msg: str) -> None:
        self._write("·", msg)

    def change(self, msg: str) -> None:
        self._write("»", msg)

    def warn(self, msg: str) -> None:
        self._write("!", msg)

    def error(self, msg: str) -> None:
        self._write("×", msg)

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


# --------------------------------------------------------------------------- #
# Windows 原生文件系统事件（ReadDirectoryChangesW）
# --------------------------------------------------------------------------- #

if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _FILE_NOTIFY_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("NextEntryOffset", ctypes.c_uint32),
            ("Action", ctypes.c_uint32),
            ("FileNameLength", ctypes.c_uint32),
            ("FileName", ctypes.c_wchar * 1),
        ]

    _ACTION_NAMES = {
        1: "新增", 2: "删除", 3: "修改", 4: "重命名", 5: "重命名",
    }


class WindowsDirWatcher(threading.Thread):
    """在后台线程里用一个目录句柄递归监听整棵子树，收到变更就回调。

    只监听「源目录」，所以本工具写到目标目录的动作不会自我触发。
    """

    FILE_LIST_DIRECTORY = 0x0001
    FILE_SHARE_ALL = 0x0001 | 0x0002 | 0x0004
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    NOTIFY_FILTER = (
        0x0001  # 文件名变化
        | 0x0002  # 目录名变化
        | 0x0008  # 大小变化
        | 0x0010  # 最后写入时间
        | 0x0040  # 创建时间
    )
    ERROR_NOTIFY_ENUM_DIR = 74        # 缓冲区溢出：有事件被丢弃
    ERROR_OPERATION_ABORTED = 995
    ERROR_INVALID_HANDLE = 6

    def __init__(self, path: str, on_change, log: Log, on_overflow=None) -> None:
        super().__init__(daemon=True, name="dir-watcher")
        self.path = path
        self.on_change = on_change
        self.on_overflow = on_overflow or (lambda: None)
        self.log = log
        self._stop = threading.Event()
        self._handle: int | None = None
        self.failed = False

    def _open(self) -> int | None:
        k = _kernel32
        k.CreateFileW.restype = ctypes.c_void_p
        k.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        handle = k.CreateFileW(
            self.path,
            self.FILE_LIST_DIRECTORY,
            self.FILE_SHARE_ALL,
            None,
            self.OPEN_EXISTING,
            self.FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if handle in (None, 0) or handle == ctypes.c_void_p(-1).value:
            return None
        return handle

    def run(self) -> None:
        handle = self._open()
        if handle is None:
            self.failed = True
            self.log.warn(f"原生事件接口打开失败（错误码 {ctypes.get_last_error()}），改用轮询模式。")
            return
        self._handle = handle

        k = _kernel32
        k.ReadDirectoryChangesW.restype = ctypes.c_int
        k.ReadDirectoryChangesW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p, ctypes.c_void_p,
        ]
        k.CloseHandle.argtypes = [ctypes.c_void_p]

        buf_size = 64 * 1024
        buf = ctypes.create_string_buffer(buf_size)
        returned = ctypes.c_uint32(0)
        base = ctypes.addressof(buf)

        while not self._stop.is_set():
            ok = k.ReadDirectoryChangesW(
                ctypes.c_void_p(handle), buf, buf_size, 1,
                self.NOTIFY_FILTER, ctypes.byref(returned), None, None,
            )
            if not ok:
                err = ctypes.get_last_error()
                if self._stop.is_set() or err in (
                    self.ERROR_OPERATION_ABORTED, self.ERROR_INVALID_HANDLE
                ):
                    break
                if err == self.ERROR_NOTIFY_ENUM_DIR:
                    self.log.warn("事件缓冲区溢出，可能有变更被丢弃，将做一次全量校验。")
                    self.on_overflow()
                    continue
                self.log.warn(f"监听出错（错误码 {err}），1 秒后重试。")
                time.sleep(1.0)
                continue

            offset = 0
            size = returned.value
            while offset < size:
                info = ctypes.cast(
                    base + offset, ctypes.POINTER(_FILE_NOTIFY_INFORMATION)
                ).contents
                name_len = info.FileNameLength // 2
                name = ctypes.wstring_at(base + offset + 12, name_len)
                try:
                    self.on_change(name, info.Action)
                except Exception as exc:      # 回调异常不能拖垮监听线程
                    self.log.error(f"处理事件失败：{exc}")
                if info.NextEntryOffset == 0:
                    break
                offset += info.NextEntryOffset

        if self._handle:
            k.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None

    def stop(self) -> None:
        self._stop.set()
        if self._handle:
            try:
                _kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# 镜像同步核心
# --------------------------------------------------------------------------- #

class Mirror:
    def __init__(self, src: str, dst: str, delete: bool, excludes: list[str],
                 dry_run: bool, log: Log, quiet: bool = False) -> None:
        self.src = src
        self.dst = dst
        self.delete = delete
        self.patterns = [p.lower() for p in excludes]
        self.dry_run = dry_run
        self.log = log
        self.quiet = quiet
        self.total_copied = 0
        self.total_deleted = 0
        self.total_errors = 0
        self._made_dirs: set[str] = set()
        self._defer: dict[str, float] = {}      # rel -> 允许再次尝试的时间戳

    def _ensure_dir(self, path: str | None) -> None:
        """按需建目录，同一路径只调用一次系统接口。"""
        if not path or path in self._made_dirs:
            return
        os.makedirs(path, exist_ok=True)
        self._made_dirs.add(path)

    # -- 重试调度 ---------------------------------------------------------- #
    # 大文件写入期间会被 _defer 推迟。此时源目录可能长时间不再产生新事件，
    # 若只依赖文件事件唤醒，被推迟的文件就永远等不到重试。
    # 下面两个方法让主循环能把「最早的到期时刻」也纳入等待时间。

    def defer_wait(self) -> float | None:
        """距离最早一个待重试文件到期的秒数；没有待重试文件时返回 None。"""
        if not self._defer:
            return None
        return max(0.0, min(self._defer.values()) - time.time())

    def has_due_defer(self) -> bool:
        """是否存在已到期、需要立刻重试的文件。"""
        now = time.time()
        return any(t <= now for t in self._defer.values())

    # -- 排除规则 ---------------------------------------------------------- #

    def _match(self, name: str) -> bool:
        low = name.lower()
        return any(fnmatch.fnmatch(low, pat) for pat in self.patterns)

    def is_excluded(self, rel: str) -> bool:
        """rel 使用 / 分隔的相对路径。任一路径分量命中排除规则即排除。"""
        if not rel:
            return False
        rel = rel.replace("\\", "/")
        if self._match(rel):
            return True
        return any(self._match(part) for part in rel.split("/") if part)

    # -- 单文件 ------------------------------------------------------------ #

    @staticmethod
    def _same(st_src: os.stat_result, st_dst: os.stat_result) -> bool:
        """大小一致、修改时间相差在 1 秒内即视为相同（FAT 时间精度较粗）。"""
        return st_dst.st_size == st_src.st_size and abs(st_dst.st_mtime - st_src.st_mtime) < 1.0

    # 修改时间早于「现在 - STABLE_AGE」的文件，直接认定已写完，无需等待。
    # 这条判断让首次全量同步（大量历史文件）几乎零开销。
    STABLE_AGE = 5.0
    # 一次等待失败后，多少秒内不再为同一文件空等，避免大文件持续写入时拖住整个同步循环。
    DEFER_SECONDS = 10.0

    def _wait_stable(self, path: str, first: os.stat_result,
                     timeout: float = 5.0, quiet: float = 0.7) -> bool:
        """等到源文件大小与修改时间都不再变化为止，避免复制到写了一半的文件。"""
        if time.time() - first.st_mtime > self.STABLE_AGE:
            return True
        last = (first.st_size, first.st_mtime)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(quiet)
            try:
                st = os.stat(path)
            except OSError:
                return False
            cur = (st.st_size, st.st_mtime)
            if cur == last:
                return True
            last = cur
        return False

    def _copy_one(self, rel: str) -> str:
        """返回 'copied' / 'skipped' / 'pending' / 'error'。"""
        src_file = os.path.join(self.src, rel)
        dst_file = os.path.join(self.dst, rel)
        try:
            st_src = os.stat(src_file)
        except OSError:
            self._defer.pop(rel, None)      # 源文件已消失，清掉重试记录
            return "skipped"

        if self._defer.get(rel, 0.0) > time.time():
            return "pending"        # 上次发现它还在写，先放一放，别空等

        need = True
        try:
            if self._same(st_src, os.stat(dst_file)):
                need = False
        except OSError:
            pass
        if not need:
            self._defer.pop(rel, None)
            return "skipped"

        if self.dry_run:
            self.log.change(f"[预演] 复制 {rel}  ({human(st_src.st_size)})")
            return "copied"

        if not self._wait_stable(src_file, st_src):
            self._defer[rel] = time.time() + self.DEFER_SECONDS
            self.log.warn(f"文件仍在写入，{self.DEFER_SECONDS:.0f} 秒后重试：{rel}")
            return "pending"

        parent = os.path.dirname(dst_file)
        if parent:
            self._ensure_dir(parent)

        tmp = dst_file + TMP_SUFFIX
        try:
            shutil.copy2(src_file, tmp)
            os.replace(tmp, dst_file)
        except PermissionError:
            # 目标文件只读或被占用：去掉只读属性再试一次
            try:
                if os.path.exists(dst_file):
                    os.chmod(dst_file, stat.S_IWRITE)
                os.replace(tmp, dst_file)
            except Exception as exc:
                self._cleanup(tmp)
                self.total_errors += 1
                self.log.error(f"写入失败 {rel}：{exc}")
                return "error"
        except Exception as exc:
            self._cleanup(tmp)
            self.total_errors += 1
            hint = f"\n    {MAX_PATH_HINT}" if isinstance(exc, OSError) and len(src_file) > 240 else ""
            self.log.error(f"写入失败 {rel}：{exc}{hint}")
            return "error"

        self._defer.pop(rel, None)
        self.log.change(f"更新 {rel}  ({human(st_src.st_size)})")
        return "copied"

    @staticmethod
    def _cleanup(path: str) -> None:
        try:
            if os.path.exists(path):
                os.chmod(path, stat.S_IWRITE)
                os.remove(path)
        except OSError:
            pass

    # -- 删除多余项 -------------------------------------------------------- #

    def _purge(self) -> int:
        """自底向上遍历目标，删掉源中已不存在的文件与空目录。"""
        removed = 0
        for dirpath, _dirnames, filenames in os.walk(self.dst, topdown=False):
            rel_dir = os.path.relpath(dirpath, self.dst)
            rel_dir = "" if rel_dir == "." else rel_dir

            for name in filenames:
                rel = os.path.join(rel_dir, name) if rel_dir else name
                rel_u = rel.replace(os.sep, "/")
                if self.is_excluded(rel_u) or name.endswith(TMP_SUFFIX):
                    continue
                if os.path.exists(os.path.join(self.src, rel)):
                    continue
                target = os.path.join(self.dst, rel)
                if self.dry_run:
                    self.log.change(f"[预演] 删除 {rel_u}")
                    removed += 1
                    continue
                try:
                    os.chmod(target, stat.S_IWRITE)
                    os.remove(target)
                except OSError as exc:
                    self.total_errors += 1
                    self.log.error(f"删除失败 {rel_u}：{exc}")
                    continue
                self.log.change(f"删除 {rel_u}")
                removed += 1

            if rel_dir and not self.is_excluded(rel_dir.replace(os.sep, "/")):
                shown = rel_dir.replace(os.sep, "/")
                if not os.path.isdir(os.path.join(self.src, rel_dir)):
                    if self.dry_run:
                        self.log.change(f"[预演] 删除目录 {shown}/")
                        removed += 1
                    else:
                        try:
                            os.rmdir(dirpath)
                            self.log.change(f"删除目录 {shown}/")
                            removed += 1
                        except OSError:
                            pass       # 非空或无权，忽略
        return removed

    # -- 全量比对 ---------------------------------------------------------- #

    def full_sync(self, reason: str = "") -> dict:
        copied = deleted = skipped = pending = 0
        self._made_dirs.clear()      # 每轮重新确认目录存在，防止目标被外部删除后不重建
        for dirpath, dirnames, filenames in os.walk(self.src):
            rel_dir = os.path.relpath(dirpath, self.src)
            rel_dir = "" if rel_dir == "." else rel_dir

            dirnames[:] = [
                d for d in dirnames
                if not self.is_excluded(((rel_dir + "/" if rel_dir else "") + d))
            ]

            # 空目录也要在目标侧建出来
            if rel_dir and not self.dry_run:
                try:
                    self._ensure_dir(os.path.join(self.dst, rel_dir))
                except OSError as exc:
                    self.total_errors += 1
                    self.log.error(f"建目录失败 {rel_dir}：{exc}")

            for name in filenames:
                rel = os.path.join(rel_dir, name) if rel_dir else name
                rel_u = rel.replace(os.sep, "/")
                if self.is_excluded(rel_u):
                    continue
                result = self._copy_one(rel)
                if result == "copied":
                    copied += 1
                elif result == "skipped":
                    skipped += 1
                elif result == "pending":
                    pending += 1

        if self.delete:
            deleted = self._purge()

        self.total_copied += copied
        self.total_deleted += deleted

        head = f"{reason}：" if reason else ""
        if copied or deleted or pending or self.total_errors:
            self.log.info(
                f"{head}更新 {copied}，删除 {deleted}，未变 {skipped}"
                + (f"，待重试 {pending}" if pending else "")
                + (f"，错误 {self.total_errors}" if self.total_errors else "")
            )
        elif not self.quiet:
            self.log.info(f"{head}无变化（{skipped} 个文件已是最新）")

        # 清掉指向已消失文件的重试记录，避免主循环被无效记录反复唤醒
        if self._defer:
            for rel in [r for r in self._defer if not os.path.exists(os.path.join(self.src, r))]:
                del self._defer[rel]

        return {"copied": copied, "deleted": deleted, "skipped": skipped, "pending": pending}


# --------------------------------------------------------------------------- #
# 主循环：防抖 + 兜底
# --------------------------------------------------------------------------- #

def run_service(mirror: Mirror, args, log: Log) -> None:
    trigger = threading.Event()
    pending_names: list[str] = []
    lock = threading.Lock()

    def on_change(name: str, action: int) -> None:
        if mirror.is_excluded(name):
            return
        with lock:
            pending_names.append(name)
        trigger.set()

    def on_overflow() -> None:
        trigger.set()

    # 1) 启动时先追平一次
    log.info(f"源目录：{mirror.src}")
    log.info(f"目标目录：{mirror.dst}")
    log.info(f"模式：{'镜像（目标多余项会被删除）' if mirror.delete else '只增不删（目标多余项保留）'}"
             + ("，预演（不做实际改动）" if mirror.dry_run else ""))
    log.info("正在进行首次全量比对……")
    mirror.full_sync("首次同步")

    if args.once:
        log.info(f"完成。累计更新 {mirror.total_copied}，删除 {mirror.total_deleted}，"
                 f"错误 {mirror.total_errors}。")
        return

    # 2) 启动实时监听
    watcher = None
    use_polling = args.poll or os.name != "nt"
    if not use_polling:
        watcher = WindowsDirWatcher(mirror.src, on_change, log, on_overflow)
        watcher.start()
        time.sleep(0.3)
        if watcher.failed:
            watcher = None
            use_polling = True

    if use_polling:
        log.info(f"已进入轮询模式，每 {args.debounce:.0f} 秒检查一次。")
    else:
        log.info("已进入实时监听模式，源文件夹的任何改动都会立即同步。")

    log.info("按 Ctrl+C 退出。")

    # 3) 事件 → 防抖 → 同步
    last_check = time.time()
    try:
        while True:
            if use_polling:
                time.sleep(max(args.debounce, 0.5))
                fired = True
            else:
                # 等待文件事件。若有大文件正被推迟重试，唤醒时间不晚于它的到期时刻，
                # 否则源目录长时间无新事件时，它会被永远搁置。
                timeout = 1.0
                due_in = mirror.defer_wait()
                if due_in is not None:
                    timeout = min(timeout, max(due_in, 0.1))
                fired = trigger.wait(timeout=timeout)
                if not fired and mirror.has_due_defer():
                    fired = True        # 重试时刻已到，主动唤醒一次

            if fired:
                # 防抖：每收到新事件就重新计时，直到安静 args.debounce 秒
                start = time.time()
                while True:
                    trigger.clear()
                    time.sleep(args.debounce)
                    if not trigger.is_set():
                        break
                    if time.time() - start > args.max_delay:
                        break
                trigger.clear()
                with lock:
                    count = len(pending_names)
                    pending_names.clear()
                if count:
                    log.info(f"检测到 {count} 个变更事件，开始同步。")
                mirror.full_sync("同步")
                last_check = time.time()

            # 周期兜底：防止极端情况下事件丢失
            if args.check > 0 and (time.time() - last_check) >= args.check:
                mirror.full_sync("周期校验")
                last_check = time.time()

    except KeyboardInterrupt:
        log.info("收到退出信号，正在停止……")
    finally:
        if watcher:
            watcher.stop()
            watcher.join(timeout=3.0)
        log.info(f"已停止。累计更新 {mirror.total_copied}，删除 {mirror.total_deleted}，"
                 f"错误 {mirror.total_errors}。")


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #

def check_layout(src: str, dst: str) -> None:
    s = Path(src).resolve()
    d = Path(dst).resolve()
    if s == d:
        raise SystemExit("错误：源目录与目标目录不能是同一个文件夹。")
    if str(d).lower().startswith(str(s).lower() + os.sep):
        raise SystemExit(f"错误：目标目录位于源目录内部（{d}），会导致无限递归。")
    if str(s).lower().startswith(str(d).lower() + os.sep):
        raise SystemExit(f"错误：源目录位于目标目录内部（{s}），会导致无限递归。")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mirror_sync",
        description="实时把源文件夹的内容保持到目标文件夹（以源为准）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python mirror_sync.py --src \"D:\\教学资料\" --dst \"E:\\备份\\教学资料\"\n"
               "  python mirror_sync.py --src \"D:\\课件\" --dst \"F:\\网盘\\课件\" --delete\n",
    )
    p.add_argument("--src", "-s", required=True, help="源文件夹（以此为准）")
    p.add_argument("--dst", "-d", required=True, help="目标文件夹（会被改成与源一致）")
    p.add_argument("--delete", action="store_true",
                   help="镜像删除：目标中源已不存在的文件会被删除（默认关闭，只增不删）")
    p.add_argument("--once", action="store_true",
                   help="只同步一次就退出，不常驻监听")
    p.add_argument("--dry-run", action="store_true",
                   help="预演：只打印将要做的动作，不实际改动任何文件")
    p.add_argument("--debounce", type=float, default=1.5, metavar="秒",
                   help="检测到变更后，安静多少秒再开始同步（默认 1.5）")
    p.add_argument("--max-delay", type=float, default=10.0, metavar="秒",
                   help="持续有变更时的最长等待时间，超过就强制同步一次（默认 10）")
    p.add_argument("--check", type=float, default=600.0, metavar="秒",
                   help="周期兜底全量校验间隔，设 0 关闭（默认 600）")
    p.add_argument("--poll", action="store_true",
                   help="强制使用轮询模式（网络盘、虚拟盘或事件接口不可用时使用）")
    p.add_argument("--exclude", "-x", action="append", default=[], metavar="模式",
                   help="额外排除的通配模式，可重复，如 --exclude \"*.bak\"")
    p.add_argument("--no-default-exclude", action="store_true",
                   help="不使用内置的默认排除规则")
    p.add_argument("--log", metavar="文件", help="日志文件路径（默认写到脚本旁 logs/ 目录）")
    p.add_argument("--quiet", "-q", action="store_true", help="不打印「无变化」的提示")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    src = os.path.abspath(os.path.expanduser(args.src))
    dst = os.path.abspath(os.path.expanduser(args.dst))

    if not os.path.isdir(src):
        print(f"错误：源文件夹不存在或不是文件夹：{src}", file=sys.stderr)
        return 2
    os.makedirs(dst, exist_ok=True)
    check_layout(src, dst)

    if args.delete and os.path.dirname(src) == src:
        print(f"警告：源目录是驱动器根目录（{src}），镜像删除风险极大。\n"
              f"       如确需如此，请先用 --dry-run 预演确认。", file=sys.stderr)

    log_path = args.log
    if log_path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(here, "logs",
                                f"mirror_{datetime.now().strftime('%Y-%m-%d')}.log")
    log = Log(log_path, quiet=args.quiet)
    log.info(f"=== mirror_sync v{VERSION} 启动 ===")
    log.info(f"日志文件：{log_path}")

    excludes = ([] if args.no_default_exclude else list(DEFAULT_EXCLUDES)) + list(args.exclude)

    mirror = Mirror(src, dst, args.delete, excludes, args.dry_run, log, args.quiet)
    try:
        run_service(mirror, args, log)
    finally:
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
