@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: 这个文件是旧版的启动脚本，现在只做转发。
:: 真正的启动器：双击「启动闲鱼智控Pro.bat」，或桌面上的「闲鱼智控 Pro」图标。

echo 运行环境已内置，正在启动闲鱼智控 Pro…
call "启动闲鱼智控Pro.bat"
exit /b %errorlevel%
