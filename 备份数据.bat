@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

title 备份闲鱼智控 Pro 数据

if not exist "runtime\python\python.exe" (
    echo.
    echo [错误] 找不到内置运行环境，无法执行备份。
    echo        请确认整个文件夹完整（runtime 目录可能被杀毒软件隔离）。
    echo.
    pause
    exit /b 1
)

echo 正在备份 data\ 与 global_config.yml 到 工作区\05-备份\ ...
echo 备份包含 Cookie 与卡密，属本机敏感文件：只留本机，不要外传。
echo.
"runtime\python\python.exe" "desktop\backup.py"
echo.
pause
