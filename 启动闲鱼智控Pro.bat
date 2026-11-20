@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

:: ============================================================
:: 闲鱼智控 Pro —— 启动（也用于修复环境）
:: 首次运行会自检依赖和浏览器内核，之后几秒钟就能打开窗口
:: ============================================================

title 闲鱼智控 Pro

if not exist "runtime\python\python.exe" (
    echo.
    echo [错误] 找不到内置运行环境：runtime\python\python.exe
    echo        请确认整个文件夹完整，没有被杀毒软件隔离。
    echo.
    pause
    exit /b 1
)

"runtime\python\python.exe" "desktop\bootstrap.py"
if errorlevel 1 (
    echo.
    echo [错误] 环境自检未通过，详见 desktop\bootstrap.log
    echo.
    pause
    exit /b 1
)

start "" "runtime\python\pythonw.exe" "desktop\launcher.pyw"
exit /b 0
