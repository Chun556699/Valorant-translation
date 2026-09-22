@echo off
REM Electron 版打包：portable 单文件 exe
REM 注：不再内嵌管理员权限（安全考虑）。Valorant 提权运行时热键可能失效，
REM     此时右键 exe「以管理员身份运行」即可。
cd /d %~dp0client-electron
set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
set ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/
npx electron-builder --win portable
echo.
echo 输出: client-electron\dist\VTranslate.exe
