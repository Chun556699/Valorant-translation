# VTranslate — agent notes

## Layout
- `server/` FastAPI billing + WebSocket relay + admin API; admin console static files in `server/admin/` (served at `/admin`).
- `client-electron/` Windows Electron client (main process owns secrets/WebSockets; renderer talks via `preload.js` whitelist).
- `deploy/` Docker Compose / nginx / systemd / install script; `package_server.bat` -> `dist/vtranslate-server.tar.gz`.
- Glossary: `server/game_terms.json` and `client-electron/src/game_terms.json` must stay identical.

## Server tests (flat imports: run from `server/`)
```
cd server
ADMIN_TOKEN=admintest MOCK_UPSTREAM=1 ZPAY_KEY=testkey ZPAY_PID=1000 DB_PATH=_verify.db python -m uvicorn app:app --host 127.0.0.1 --port 8765
python smoke_test.py      # needs ZPAY_KEY=testkey on the server; exhausts guest quota for 127.0.0.1
python admin_test.py      # needs ADMIN_TOKEN=admintest
```
Delete `server/_verify.db*` afterwards. Single process only (SQLite + in-memory state).

## Client
- Install: `npm ci` in `client-electron/` with `ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/`. If the Electron download hangs, use `npm ci --ignore-scripts`, download `electron-v<ver>-win32-x64.zip` from the mirror, extract to `node_modules/electron/dist`, and write `electron.exe` to `node_modules/electron/path.txt`.
- Build: `npx electron-builder --win portable -c.electronDist=node_modules/electron/dist` -> `dist/VTranslate.exe`.
- electron-builder 26 rejects unknown keys inside `build` (put comments at the top level, e.g. `"//build"`).
- When launching Electron from a tool shell, unset `ELECTRON_RUN_AS_NODE`; use `--user-data-dir=<tmp>` to avoid touching the real `%APPDATA%\VTranslate` config and `--remote-debugging-port` for CDP checks.
