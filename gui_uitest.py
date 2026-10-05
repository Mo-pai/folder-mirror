#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui_uitest.py —— 图形界面端到端集成测试

在系统临时目录里自建文件夹，真实拉起 mirror_sync 子进程，验证
「界面配置 → 启动进程 → 实时同步 → 日志回显 → 停止」整条链路可用。

全程只在临时目录内读写，不触碰电脑上任何原有文件夹，跑完自动清理。
用法：python gui_uitest.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import mirror_gui  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def ck(cond: bool, label: str, detail: str = "") -> None:
    RESULTS.append((label, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    print(f"  {mark}  {label}" + (f"   [{detail}]" if detail else ""), flush=True)


def main() -> int:
    if not (HERE / "mirror_sync.py").exists():
        print("找不到 mirror_sync.py，请与本脚本放在同一目录。", file=sys.stderr)
        return 2

    root = Path(tempfile.mkdtemp(prefix="mirror_gui_uitest_")).resolve()
    src = root / "源文件夹"
    dst = root / "目标文件夹"
    src.mkdir(parents=True)
    (src / "种子.txt").write_text("seed\n", encoding="utf-8")

    print("=" * 68)
    print("图形界面端到端测试")
    print(f"测试专用临时目录：{root}")
    print("说明：只在该目录内造数据，不动你电脑上任何原有文件夹。")
    print("=" * 68)

    app = mirror_gui.MirrorGUI(silent=True)
    app.withdraw()                      # 隐藏窗口，不打扰

    # --- 界面配置 ------------------------------------------------------- #
    app.v_name.set("UI测试任务")
    app.v_src.set(str(src))
    app.v_dst.set(str(dst))
    app.v_debounce.set("1.0")
    app.v_cur_only.set(True)
    app.update()

    t = app._current_task()
    ck(t is not None and t.get("src") == str(src), "界面配置已写入任务数据")

    started = time.time()

    # --- 步骤 1：点「启动同步」 ------------------------------------------ #
    def step1() -> None:
        app._start_task(None)
        running = app._is_running(app.current_id)
        ck(running, "点「启动同步」后子进程已拉起",
           f"PID {app.procs[app.current_id]['proc'].pid}" if running else "未启动")
        app.after(2500, step2)

    # --- 步骤 2：首轮全量同步 -------------------------------------------- #
    def step2() -> None:
        ok = (dst / "种子.txt").exists()
        ck(ok, "启动后自动完成首轮全量同步", "种子.txt 已在目标出现" if ok else "目标为空")
        (src / "实时新增.txt").write_text("live content\n", encoding="utf-8")
        app.after(3500, step3)

    # --- 步骤 3：实时监听到新建文件 -------------------------------------- #
    def step3() -> None:
        p = dst / "实时新增.txt"
        ok = p.exists() and p.read_text(encoding="utf-8") == "live content\n"
        ck(ok, "源中新建文件被实时同步到目标", "内容一致" if ok else "未同步或内容不符")

        # 嵌套目录 + 修改
        (src / "子目录" / "深层").mkdir(parents=True, exist_ok=True)
        (src / "子目录" / "深层" / "n.txt").write_text("nested\n", encoding="utf-8")
        (src / "种子.txt").write_text("modified v2\n", encoding="utf-8")
        app.after(3500, step4)

    # --- 步骤 4：嵌套目录与内容修改 -------------------------------------- #
    def step4() -> None:
        n = dst / "子目录" / "深层" / "n.txt"
        m = dst / "种子.txt"
        ck(n.exists() and n.read_text(encoding="utf-8") == "nested\n",
           "嵌套目录被自动创建并同步")
        ck(m.exists() and "modified v2" in m.read_text(encoding="utf-8"),
           "已有文件的内容修改已同步")

        log = app.txt_log.get("1.0", "end")
        ck("启动实时同步" in log, "界面日志区显示启动信息")
        ck("实时新增.txt" in log or "子目录" in log, "界面日志区显示同步动作")

        app._stop_task(None)
        app.after(2000, step5)

    # --- 步骤 5：停止 ---------------------------------------------------- #
    def step5() -> None:
        ck(not app._is_running(app.current_id), "点「停止」后子进程已结束")
        log = app.txt_log.get("1.0", "end")
        ck("已停止" in log, "界面日志提示已停止")

        # 停止后源再变，目标不应再跟随
        (src / "停止后新增.txt").write_text("should not sync\n", encoding="utf-8")
        app.after(3000, step6)

    # --- 步骤 6：停止后不再同步 ------------------------------------------ #
    def step6() -> None:
        gone = not (dst / "停止后新增.txt").exists()
        ck(gone, "停止后源的新变更不再同步（符合预期）")

        # 预演：先造一个只在源里存在的文件，预演应报告「打算复制」但不真复制
        (src / "预演测试.txt").write_text("dry run only\n", encoding="utf-8")
        app._clear_log()
        app._run_once(dry=True)
        app.after(9000, step7)

    # --- 步骤 7：预演不改文件 -------------------------------------------- #
    def step7() -> None:
        log = app.txt_log.get("1.0", "end")
        ck("预演" in log, "预演模式已在界面日志中体现")
        ck(not (dst / "预演测试.txt").exists(), "预演没有真的往目标复制文件")
        ck((dst / "种子.txt").exists(), "预演未破坏目标已有文件")

        app.after(600, finish)

    def finish() -> None:
        try:
            for tid in list(app.procs.keys()):
                app._stop_task(tid, quiet=True)
        except Exception:                              # noqa: BLE001
            pass
        try:
            app.cfg_path.unlink(missing_ok=True)
        except OSError:
            pass
        app.destroy()

    app.after(700, step1)
    app.after(90000, finish)           # 兜底，防止某一步卡住导致永久等待
    app.mainloop()

    # --- 收尾 ------------------------------------------------------------ #
    shutil.rmtree(root, ignore_errors=True)

    fails = sum(1 for _, ok, _ in RESULTS if not ok)
    print("=" * 68)
    print(f"合计 {len(RESULTS) - fails}/{len(RESULTS)} 项通过"
          f"　　耗时 {time.time() - started:.1f} 秒")
    print(f"临时目录已清理：{root}")
    print("=" * 68)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
