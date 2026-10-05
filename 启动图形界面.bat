@echo off
chcp 936 >nul 2>nul
set "PYTHONUTF8=0"
set "PYTHONIOENCODING=gbk"
cd /d "%~dp0"
title 文件夹镜像同步 - 控制台

rem ============================================================
rem  第一步：查找 Python
rem ============================================================
set "PY="
set "PYW="

if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    set "PYW=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
)

if not defined PY (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PY set "PY=%%i"
    )
)
if not defined PYW (
    for /f "delims=" %%i in ('where pythonw 2^>nul') do (
        if not defined PYW set "PYW=%%i"
    )
)

if not defined PY (
    echo.
    echo   [X] 没有找到 Python，无法启动界面。
    echo.
    echo       请先安装 Python 3.10 或更高版本：
    echo         https://www.python.org/downloads/
    echo       安装时务必勾选 "Add python.exe to PATH"。
    echo.
    pause
    exit /b 1
)

rem ============================================================
rem  第二步：启动前自检
rem  有问题会直接显示在本窗口里，不会一闪而过
rem ============================================================
"%PY%" "%~dp0mirror_gui.py" --check
if errorlevel 1 (
    echo.
    echo   请按上面的提示处理后，再重新双击本文件。
    echo.
    pause
    exit /b 1
)

rem ============================================================
rem  第三步：启动图形界面
rem  用 pythonw.exe 启动，不会弹出黑色控制台窗口
rem ============================================================
if defined PYW (
    start "" "%PYW%" "%~dp0mirror_gui.py"
) else (
    start "" "%PY%" "%~dp0mirror_gui.py"
)
exit /b 0
