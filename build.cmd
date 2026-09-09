@echo off
rem dsh-switch Windows 一键构建脚本（需要 Python 3.8+ 且含 tkinter）
rem 产物：dist\dsh-switch.exe

python -m venv venv || goto :err
venv\Scripts\python -m pip install -U paramiko pystray pillow pyinstaller || goto :err

venv\Scripts\pyinstaller --onefile --noconsole ^
    --icon assets\dsh.ico ^
    --add-data "assets\dsh-icon-512.png;assets" ^
    --add-data "templates;templates" ^
    --name dsh-switch src\dsh_switch.pyw || goto :err

echo.
echo Build OK: dist\dsh-switch.exe
exit /b 0

:err
echo Build FAILED.
exit /b 1
