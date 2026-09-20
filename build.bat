@echo off
REM 打包客户端为单文件 exe -> client\dist\VTranslate.exe
REM 需要先 pip install -r requirements-client.txt pyinstaller
cd /d "%~dp0client"
pyinstaller --noconfirm --clean --name VTranslate --windowed --onefile --collect-all pyaudiowpatch main.py
if %errorlevel%==0 (
    echo.
    echo 完成: client\dist\VTranslate.exe
) else (
    echo 打包失败
)
pause
