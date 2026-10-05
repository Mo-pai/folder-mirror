#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py —— mirror_sync 自动化自测

自己在临时目录里造各种文件与变更，逐项验证 mirror_sync.py 的行为，
最后打印一张通过/失败清单。不改动任何真实数据。

运行：
    python selftest.py
    python selftest.py --clean      # 跑完后删除测试目录

测试目录默认保留在脚本旁的 _selftest/，方便你亲自查看结果。
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "mirror_sync.py"
PY = sys.executable

# 测试数据放在系统临时目录下自己的专属子目录里，绝不落在工作区或用户资料目录中。
# 这一路径在运行时会被打印出来，随时可查。
ROOT = Path(tempfile.gettempdir()) / "mirror_sync_selftest"

_results: list[tuple[str, bool, str]] = []


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# 与 mirror_sync.py 内置默认排除规则保持一致，快照比较时需跳过这些文件
DEFAULT_SKIP = ["~$*", "*.tmp", "*.temp", "*.mstmp",
                "Thumbs.db", "desktop.ini", ".DS_Store"]


def _skipped(rel: str) -> bool:
    parts = [p for p in rel.lower().split("/") if p]
    return any(fnmatch.fnmatch(x, pat) for x in parts for pat in DEFAULT_SKIP)


def snap(root: Path) -> dict:
    """给目录拍一张快照：相对路径 -> 内容指纹。用于比较两边是否完全一致。

    跳过被镜像工具排除的文件（它们本来就不该出现在目标侧）。
    """
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if _skipped(rel):
            continue
        if p.is_dir():
            out[rel + "/"] = "dir"
        else:
            out[rel] = f"{p.stat().st_size}:{md5(p)}"
    return out


def wait_until(predicate, timeout: float = 15.0, interval: float = 0.25) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    flag = "通过" if ok else "失败"
    tail = f"   {detail}" if detail else ""
    print(f"    [{flag}] {name}{tail}", flush=True)


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


def run_once(src: Path, dst: Path, extra: list[str] | None = None) -> subprocess.CompletedProcess:
    """跑一次 --once 模式，返回进程结果。"""
    cmd = [PY, str(SCRIPT), "--src", str(src), "--dst", str(dst), "--once",
           "--log", str(ROOT / "once.log"), "--quiet"] + (extra or [])
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def start_daemon(src: Path, dst: Path, extra: list[str] | None = None):
    """启动常驻实时监听，返回 (进程, 输出文件句柄)。"""
    out = open(ROOT / "daemon.out", "w", encoding="utf-8")
    cmd = [PY, str(SCRIPT), "--src", str(src), "--dst", str(dst),
           "--debounce", "0.8", "--check", "0", "--log", str(ROOT / "daemon.log")] + (extra or [])
    proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    return proc, out


def stop_daemon(proc, out) -> None:
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    if out:
        out.close()


def _assert_inside_root(p: Path) -> Path:
    """安全护栏：本脚本只允许在 _selftest/ 目录内创建或删除任何东西。

    一旦发现目标路径跑到测试目录之外，立刻终止，绝不碰测试目录外的文件。
    """
    try:
        Path(p).resolve().relative_to(ROOT.resolve())
    except ValueError:
        raise SystemExit(
            f"\n安全护栏已阻止操作：\n"
            f"  目标路径：{p}\n"
            f"  允许范围：{ROOT}\n"
            f"本自测脚本绝不会改动测试目录之外的任何文件。"
        )
    return p


def fresh(*parts: str) -> Path:
    p = ROOT.joinpath(*parts)
    _assert_inside_root(p)
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- #
# 场景 A：一次性同步（--once）
# --------------------------------------------------------------------------- #

def case_a_once() -> None:
    section("场景 A：一次性同步 (--once)")
    src = fresh("a", "src")
    dst = fresh("a", "dst")

    # A1 首次全量：多层目录 + 若干文件
    (src / "docs").mkdir()
    (src / "docs" / "deep" / "deeper").mkdir(parents=True)
    (src / "empty").mkdir()
    (src / "readme.txt").write_text("readme content\n", encoding="utf-8")
    (src / "docs" / "note.md").write_text("note\n", encoding="utf-8")
    (src / "docs" / "deep" / "deeper" / "leaf.bin").write_bytes(bytes(range(256)) * 40)
    run_once(src, dst)
    check("A1 首次全量：目录结构与文件内容完全一致", snap(src) == snap(dst),
          f"{len(snap(src))} 个条目")

    # A2 排除规则
    (src / "~$budget.docx").write_text("lock\n", encoding="utf-8")
    (src / "temp.tmp").write_text("tmp\n", encoding="utf-8")
    (src / "Thumbs.db").write_text("db\n", encoding="utf-8")
    (src / "docs" / "~$inside.xlsx").write_text("lock\n", encoding="utf-8")
    run_once(src, dst)
    leaked = [n for n in ("~$budget.docx", "temp.tmp", "Thumbs.db") if (dst / n).exists()]
    leaked += ["docs/~$inside.xlsx"] if (dst / "docs" / "~$inside.xlsx").exists() else []
    check("A2 排除规则：临时文件与系统文件未被同步", not leaked, f"泄漏：{leaked}" if leaked else "")

    # A3 幂等：再跑一次不应产生任何复制
    r = run_once(src, dst)
    check("A3 幂等：无变化时重复执行不复制任何文件", "更新 0" in r.stdout, r.stdout.strip().splitlines()[-1] if r.stdout else "")

    # A4 更新已有文件
    (src / "readme.txt").write_text("readme v2 changed\n", encoding="utf-8")
    run_once(src, dst)
    check("A4 更新：修改后的内容已同步",
          (dst / "readme.txt").read_text(encoding="utf-8") == "readme v2 changed\n")

    # A5 新增文件
    (src / "docs" / "brand-new.txt").write_text("new\n", encoding="utf-8")
    run_once(src, dst)
    check("A5 新增：新文件已同步", (dst / "docs" / "brand-new.txt").exists())

    # A6 只增不删（默认）：源删掉的文件在目标保留
    (src / "readme.txt").unlink()
    run_once(src, dst)
    check("A6 只增不删：源删除后目标仍保留", (dst / "readme.txt").exists())

    # A7 预演（--delete --dry-run）：只报告，不改动
    before = snap(dst)
    r = run_once(src, dst, ["--delete", "--dry-run"])
    check("A7 预演：报告了待删除项", "预演" in r.stdout, "")
    check("A7 预演：目标未被实际改动", snap(dst) == before)

    # A8 镜像删除（--delete）
    r = run_once(src, dst, ["--delete"])
    after = snap(dst)
    check("A8 镜像删除：多余文件已被删除", not (dst / "readme.txt").exists())
    check("A8 镜像删除：源与目标此时完全一致", snap(src) == after,
          f"源 {len(snap(src))} 项 / 目标 {len(after)} 项")

    # A9 空目录也同步
    check("A9 空目录也被创建", (dst / "empty").is_dir())

    # A10 深层大文件内容一致
    check("A10 深层文件内容一致（10KB 二进制）",
          md5(src / "docs" / "deep" / "deeper" / "leaf.bin")
          == md5(dst / "docs" / "deep" / "deeper" / "leaf.bin"))


# --------------------------------------------------------------------------- #
# 场景 B：常驻实时监听
# --------------------------------------------------------------------------- #

def case_b_realtime() -> None:
    section("场景 B：常驻实时监听")
    src = fresh("b", "src")
    dst = fresh("b", "dst")
    (src / "seed.txt").write_text("seed\n", encoding="utf-8")

    proc, out = start_daemon(src, dst)
    try:
        if not wait_until(lambda: (dst / "seed.txt").exists(), 12):
            check("B0 监听进程启动并完成首次同步", False, "目标未出现 seed.txt")
            return
        check("B0 监听进程启动并完成首次同步", True)

        # B1 实时新建文件
        (src / "live-new.txt").write_text("live\n", encoding="utf-8")
        ok = wait_until(lambda: (dst / "live-new.txt").exists(), 15)
        check("B1 实时新建：新文件在数秒内自动同步", ok)

        # B2 实时修改
        (src / "seed.txt").write_text("seed modified\n", encoding="utf-8")
        ok = wait_until(
            lambda: (dst / "seed.txt").read_text(encoding="utf-8") == "seed modified\n", 15)
        check("B2 实时修改：改动内容自动同步", ok)

        # B3 实时新建嵌套目录
        (src / "d1" / "d2" / "d3").mkdir(parents=True)
        (src / "d1" / "d2" / "d3" / "deep.txt").write_text("deep\n", encoding="utf-8")
        ok = wait_until(lambda: (dst / "d1" / "d2" / "d3" / "deep.txt").exists(), 15)
        check("B3 实时新建：多层嵌套目录自动建立", ok)

        # B4 重命名
        (src / "live-new.txt").rename(src / "renamed.txt")
        ok = wait_until(lambda: (dst / "renamed.txt").exists(), 15)
        check("B4 实时重命名：新名字出现在目标", ok)

        # B5 排除项在实时模式下同样生效
        (src / "ignore-me.tmp").write_text("x\n", encoding="utf-8")
        time.sleep(4)
        check("B5 实时模式：临时文件不触发同步", not (dst / "ignore-me.tmp").exists())

        # B6 大文件慢速写入（20MB，约 8 秒写完），验证不产生半截文件
        big_src = src / "big.bin"
        chunk = b"\xa5" * (1024 * 1024)
        found_half = False
        seen_partial_size = []
        with open(big_src, "wb") as f:
            for _ in range(20):
                f.write(chunk)
                f.flush()
                time.sleep(0.4)
                # 写入途中确认目标侧没有出现临时文件或大小不足的半截文件
                if list(dst.glob("*.mstmp")):
                    found_half = True
                    break
                if (dst / "big.bin").exists():
                    seen_partial_size.append((dst / "big.bin").stat().st_size)
        check("B6 写入途中：目标侧始终未出现临时/半截文件",
              not found_half and not seen_partial_size,
              f"中间态：{seen_partial_size[:3]}" if seen_partial_size else "源文件写入期间目标侧保持安静")

        got = wait_until(lambda: (dst / "big.bin").exists()
                         and (dst / "big.bin").stat().st_size == big_src.stat().st_size, 40)
        if got:
            detail = f"{big_src.stat().st_size} 字节"
        else:
            tgt = dst / "big.bin"
            detail = (f"源 {big_src.stat().st_size} 字节，目标 "
                      + ("缺失" if not tgt.exists() else f"{tgt.stat().st_size} 字节"))
        check("B6 大文件：写入完成后完整同步", got, detail)
        if got:
            check("B6 大文件：源与目标 MD5 一致（非半截文件）",
                  md5(big_src) == md5(dst / "big.bin"))

        # B7 大文件写入期间的小文件不被拖住
        (src / "small-during-big.txt").write_text("small\n", encoding="utf-8")
        ok = wait_until(lambda: (dst / "small-during-big.txt").exists(), 20)
        check("B7 小文件在合理时间内同步（未被大文件阻塞）", ok)

        # B8 收敛：源中的条目都应出现在目标，且共有条目内容一致
        time.sleep(3)
        s, d = snap(src), snap(dst)
        missing = sorted(set(s) - set(d))
        check("B8 收敛：源中的条目都已同步到目标", not missing,
              f"缺失：{missing}" if missing else f"{len(s)} 项")
        mismatch = [k for k in set(s) & set(d) if s[k] != d[k]]
        check("B8 收敛：共有条目内容全部一致", not mismatch,
              f"不一致：{mismatch}" if mismatch else "")

        # B9 只增不删模式的既定行为：源里重命名后，旧文件在目标保留
        check("B9 只增不删模式：重命名后旧文件在目标保留（符合该模式定义）",
              (dst / "live-new.txt").exists() and (dst / "renamed.txt").exists())

    finally:
        stop_daemon(proc, out)
        if any(n.startswith("B") and not ok for n, ok, _ in _results):
            logfile = ROOT / "daemon.out"
            if logfile.exists():
                print("\n  ── 监听进程日志尾部（排查用）──")
                for line in logfile.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]:
                    print("    " + line)


# --------------------------------------------------------------------------- #
# 场景 C：参数校验与安全护栏
# --------------------------------------------------------------------------- #

def case_c_guards() -> None:
    section("场景 C：参数校验与安全护栏")
    src = fresh("c", "src")
    dst = fresh("c", "dst")
    (src / "a.txt").write_text("a\n", encoding="utf-8")

    r = subprocess.run([PY, str(SCRIPT), "--src", str(src), "--dst", str(src), "--once"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("C1 拒绝「源与目标相同」", r.returncode != 0, (r.stderr or "").strip().splitlines()[0]
          if r.stderr else "")

    r = subprocess.run([PY, str(SCRIPT), "--src", str(src), "--dst", str(src / "inner"), "--once"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("C2 拒绝「目标在源目录内部」", r.returncode != 0, (r.stderr or "").strip().splitlines()[0]
          if r.stderr else "")

    r = subprocess.run([PY, str(SCRIPT), "--src", str(ROOT / "not-exist"), "--dst", str(dst), "--once"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("C3 拒绝「源目录不存在」", r.returncode != 0, (r.stderr or "").strip().splitlines()[0]
          if r.stderr else "")

    # 目标目录不存在时自动创建
    auto = ROOT / "c" / "auto-created-target"
    subprocess.run([PY, str(SCRIPT), "--src", str(src), "--dst", str(auto), "--once", "--quiet"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("C4 目标目录不存在时自动创建", auto.is_dir() and (auto / "a.txt").exists())

    # 自定义排除
    custom_src = fresh("c", "src2")
    custom_dst = fresh("c", "dst2")
    (custom_src / "keep.txt").write_text("k\n", encoding="utf-8")
    (custom_src / "skip.bak").write_text("b\n", encoding="utf-8")
    run_once(custom_src, custom_dst, ["--exclude", "*.bak"])
    check("C5 自定义排除模式生效",
          (custom_dst / "keep.txt").exists() and not (custom_dst / "skip.bak").exists())


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="mirror_sync 自动化自测")
    ap.add_argument("--keep", action="store_true",
                    help="跑完后保留测试目录（默认自动清理，不留任何残留）")
    args = ap.parse_args()

    if not SCRIPT.exists():
        print(f"找不到被测脚本：{SCRIPT}", file=sys.stderr)
        return 2

    _assert_inside_root(ROOT)
    ROOT.mkdir(parents=True, exist_ok=True)

    print(f"被测脚本：{SCRIPT}")
    print(f"测试目录：{ROOT}")
    print("说明：全部测试数据都在上面这个临时目录里，")
    print("      不会读取或改动你电脑上任何原有的文件与文件夹。")
    started = time.monotonic()

    case_a_once()
    case_b_realtime()
    case_c_guards()

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    failed = [(n, d) for n, ok, d in _results if not ok]

    print("\n" + "=" * 58)
    print(f"测试汇总：通过 {passed} / {total}，耗时 {time.monotonic() - started:.1f} 秒")
    if failed:
        print("-" * 58)
        for n, d in failed:
            print(f"  未通过：{n}   {d}")
    print("=" * 58)

    if args.keep:
        print(f"测试目录保留在：{ROOT}")
        print("查看完毕后可自行删除，不加 --keep 再次运行会自动清理。")
    else:
        _assert_inside_root(ROOT)
        shutil.rmtree(ROOT, ignore_errors=True)
        print(f"测试目录已自动清理：{ROOT}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
