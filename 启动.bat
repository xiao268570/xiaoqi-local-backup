@echo off
rem ============================================================
rem  本地备份 —— 双击我启动
rem
rem  启动程序（进系统托盘），并把设置界面在浏览器里打开。
rem  再双击一次只会把已经打开的界面调出来，不会启动第二个。
rem
rem  需要已经装好 Python 和依赖：
rem      pip install pystray pillow
rem  已经打包成 exe 的话，直接双击那个 exe 就行，不需要这个文件。
rem ============================================================
setlocal enableextensions
set "APP=%~dp0src\main.py"

if not exist "%APP%" (
  echo.
  echo [ERROR] 找不到程序文件：
  echo   %APP%
  echo.
  pause
  exit /b 1
)

rem --open-ui：每次启动都打开设置界面（不带这个参数就只进托盘，适合开机自启）
start "" pythonw "%APP%" --open-ui
exit /b 0
