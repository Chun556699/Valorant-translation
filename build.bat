@echo off
REM 打包客户端为单文件 exe -> client\dist\VTranslate.exe
REM 需要先 pip install -r requirements-client.txt pyinstaller
cd /d "%~dp0client"
REM --uac-admin: Valorant 以管理员权限运行，本程序同级才能收到全局热键
pyinstaller --noconfirm --clean --name VTranslate --windowed --onefile --uac-admin --collect-all pyaudiowpatch main.py
if %errorlevel%==0 (
    echo.
    echo 完成: client\dist\VTranslate.exe
) else (
    echo 打包失败
)
pause
