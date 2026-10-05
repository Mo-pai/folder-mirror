@echo off
chcp 936 >nul 2>nul
set "PYTHONUTF8=0"
set "PYTHONIOENCODING=gbk"
cd /d "%~dp0"
title 文件夹镜像同步 - mirror_sync

rem ============================================================
rem  只需要改下面这两行：填上你自己的源文件夹和目标文件夹
rem ============================================================
set "SRC=D:/源文件夹"
set "DST=E:/目标文件夹"
rem ============================================================

rem 【模式说明】
rem   默认（下面命令不加 --delete）：
rem       只增不删。源里新增/修改的文件会同步过去，源里删掉的文件在目标里保留。
rem       更安全，建议先用这个模式跑一段时间。
rem
rem   如果想让它成为严格的镜像（源删什么，目标就删什么）：
rem       在下面那行末尾加上  --delete
rem
rem   第一次用不放心的话，先加上  --dry-run 跑一遍，
rem   它只会打印「打算做什么」，不会真的改动任何文件。
rem
rem   只想让它同步一次就退出（不常驻）：加上  --once

rem ============================================================
rem  查找 Python
rem ============================================================
set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PY set "PY=%%i"
    )
)
if not defined PY (
    echo.
    echo   [X] 没有找到 Python，请先安装 Python 3.10 或更高版本。
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0mirror_sync.py" (
    echo.
    echo   [X] 找不到 mirror_sync.py，请确认它与本文件在同一个文件夹内。
    echo.
    pause
    exit /b 1
)

echo ============================================================
echo   源  文件夹 : "%SRC%"
echo   目标文件夹 : "%DST%"
echo ============================================================
echo.

"%PY%" "%~dp0mirror_sync.py" --src "%SRC%" --dst "%DST%"

echo.
echo 同步已停止。按任意键关闭窗口。
pause >nul
