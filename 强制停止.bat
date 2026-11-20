@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

title 彻底停止闲鱼智控 Pro

if not exist "runtime\python\python.exe" (
    echo.
    echo [错误] 找不到内置运行环境，无法执行停止脚本。
    echo        可在任务管理器里结束 python.exe / pythonw.exe 进程。
    echo.
    pause
    exit /b 1
)

echo 闲鱼智控 Pro 平时常驻后台（关窗口不停服），这里是把它真正关掉。
echo 会依次关掉：管理界面窗口 → 后台服务 → 托盘图标。
echo.
"runtime\python\python.exe" "desktop\stop.py"

echo.
echo 5 秒后关闭本窗口。
timeout /t 5 >nul
exit /b 0
