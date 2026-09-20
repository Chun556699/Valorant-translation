@echo off
REM Electron 版打包：portable 单文件 exe（管理员权限内嵌）
cd /d %~dp0client-electron
set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
set ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/
npx electron-builder --win portable
echo.
echo 输出: client-electron\dist\VTranslate.exe
