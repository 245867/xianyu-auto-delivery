@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

title 重建桌面快捷方式

if not exist "runtime\python\python.exe" (
    echo.
    echo [错误] 找不到内置运行环境，请先运行「启动闲鱼智控Pro.bat」。
    echo.
    pause
    exit /b 1
)

echo 重建桌面图标（按当前文件夹位置重新指向）…
echo.
"runtime\python\python.exe" "desktop\bootstrap.py" --shortcut
echo.
pause
exit /b 0
