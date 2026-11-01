# ComfyUI-WinFix

A single-file workaround for Windows ComfyUI servers that become slow or unresponsive over time, or after a phone rapidly browses asset thumbnails/videos.

The only file in this repository is:

```text
sitecustomize.py
```

It is loaded automatically by Python from `site-packages`; no ComfyUI source code is patched.

> This is a workaround for issues in Windows `asyncio` Proactor / aiohttp. It is not an upstream fix. Back up any file you replace.

---

## Symptoms

- ComfyUI starts fine and the web UI is fast.
- After roughly **2 hours**, pages become slower and slower, eventually taking tens of seconds.
- Ctrl+C or background callbacks become slow.
- After a phone rapidly scrolls/previews many assets, static pages can return:

```text
HTTP 200
but 0 bytes of body
```

- `GET /system_stats` may still work while `GET /index.html` does not.
- The server may freeze completely in `socket.close()` / `closesocket()`.

---

## What it does

### 1. Self-pipe keepalive

On Windows, asyncio's self-pipe is a loopback TCP socketpair. After a long idle period its Proactor read future can end with:

```text
WinError 121 - The semaphore timeout period has expired
```

The original code does not re-arm that future. The patched code:

- writes 1 byte through the self-pipe on a timer (default 60 seconds);
- re-arms the read loop if it fails;
- rebuilds the socketpair if re-arming keeps failing.

### 2. Avoids the broken native-sendfile state

On Windows Proactor, aiohttp's native sendfile path can get stuck after many aborted asset requests. This patch sets:

```text
AIOHTTP_NOSENDFILE=1
```

by default, so static files use the read/write fallback.

Set `COMFY_WIN_NOSENDFILE=0` to use native sendfile again.

### 3. Optional reset-socket deferred close

If enabled, only sockets closed because of a reset/`OSError` are handed to a bounded daemon thread. Normal closes stay synchronous.

This prevents `closesocket()` from blocking the main event loop.

### 4. Optional diagnostic log

Off by default. Enable it with:

```bat
set COMFY_WIN_SAVELOG=1
```

Log file:

```text
<ComfyUI>\user\winfix_selfpipe.log
```

---

## Requirements

- Windows 10/11.
- CPython 3.12 or 3.13.
- ComfyUI using the Windows Proactor event loop (the default).
- `sitecustomize.py` must actually be loaded. Starting Python with `-S` disables it.

Tested with:

- ComfyUI Windows portable, Python 3.13.14;
- ComfyUI Desktop/repo `.venv`, Python 3.13.12;
- aiohttp 3.14.x.

---

## Install (manual)

There is deliberately no installer. Different ComfyUI packages use different Python environments.

### 1. Find the Python environment that launches ComfyUI

Run this with the **same Python executable** that launches ComfyUI:

```bat
python.exe -c "import site; print(site.getsitepackages())"
```

Copy `sitecustomize.py` into the **first writable `site-packages` directory** printed.

Common locations:

**Windows portable**

```text
ComfyUI_windows_portable\python_embeded\Lib\site-packages\sitecustomize.py
```

**Repository / venv / Desktop**

```text
<ComfyUI repo>\ComfyUI\.venv\Lib\site-packages\sitecustomize.py
```

For the ComfyUI Desktop package, the runtime may use `.venv`, `standalone-env`, or another bundled environment. Use the environment whose `site-packages` contains `aiohttp` and whose Python launches `ComfyUI\main.py`.

### 2. Do not overwrite an existing `sitecustomize.py`

If `sitecustomize.py` already exists, back it up and merge the code. Replacing another application's `sitecustomize.py` can break it.

### 3. Restart ComfyUI

On startup you should see:

```text
[ComfyUI-WinFix] COMFY-WINFIX v2.2 ACTIVE | heartbeat=60s | nosendfile=on | selfpipe-log=off | selfpipe-keepalive=on | selfpipe-rearm=on | deferred-close=off
```

If the banner is absent, the file is not being loaded:

- wrong `site-packages`;
- ComfyUI started with `-S`;
- `sitecustomize.py` syntax error;
- another `sitecustomize.py` takes precedence.

---

## Verify

```powershell
curl.exe --noproxy "*" -o NUL -w "HTTP %{http_code} bytes=%{size_download} time %{time_total}s\n" http://127.0.0.1:8870/
```

You want:

```text
HTTP 200 bytes=20089
```

not:

```text
HTTP 200 bytes=0
```

---

## Defaults

```text
Self-pipe keepalive/re-arm/rebuild    on
Heartbeat                             60 seconds
Native sendfile                       off (AIOHTTP_NOSENDFILE=1)
Diagnostic log                        off
Reset-socket deferred close           off
```

`COMFY_WIN_DEFERRED_CLOSE=0` is already the default.

---

## Options

| Variable | Default | Meaning |
| --- | --- | --- |
| `COMFY_WIN_HEARTBEAT` | `1` | `0` disables heartbeat and self-pipe keepalive |
| `COMFY_WIN_HEARTBEAT_INTERVAL` | `60` | seconds, 1..3600 |
| `COMFY_WIN_NOSENDFILE` | `1` | `1` = disable native sendfile; `0` = enable it |
| `COMFY_WIN_DEFERRED_CLOSE` | `0` | `1` = defer close of reset sockets |
| `COMFY_WIN_MAX_CLOSE_THREADS` | `16` | 1..256; only used when deferred close is on |
| `COMFY_WIN_SAVELOG` | `0` | `1` = write diagnostic log |
| `COMFY_WIN_SAVELOG_PATH` | auto | override log path |
| `COMFY_WIN_QUIET` | `0` | `1` = no startup banner |

Example for a phone-heavy setup:

```bat
set COMFY_WIN_NOSENDFILE=1
set COMFY_WIN_DEFERRED_CLOSE=1
set COMFY_WIN_SAVELOG=1
```

---

## Uninstall

Stop ComfyUI and delete `sitecustomize.py` from the `site-packages` directory. Restart.

If you replaced an existing `sitecustomize.py`, restore your backup.

---

## Notes

- This is not an official ComfyUI, aiohttp or CPython fix.
- Native sendfile is faster in normal use, but it can wedge after many aborted requests on Windows Proactor. The fallback is the safer default.
- ComfyUI updates that replace the Python environment will remove the file; copy it again after updating.
- The self-pipe log is useful when reporting a problem. Include the startup banner and the log.

---

## 中文说明

这是一个单文件补丁，只上传 `sitecustomize.py`。

### 解决的现象

- 约两小时后网页越来越慢，最后打不开；
- 手机快速连续预览资产后，静态页面返回 200 但没有 body；
- 主线程卡在 `socket.close()`，或服务整体失去响应。

### 放到哪里

找到**启动 ComfyUI 的那个 Python** 的 `site-packages`：

```bat
python.exe -c "import site; print(site.getsitepackages())"
```

把 `sitecustomize.py` 放进第一个可写的 `site-packages`。

常见位置：

```text
便携版：ComfyUI_windows_portable\python_embeded\Lib\site-packages\sitecustomize.py
venv/Desktop：<ComfyUI 目录>\.venv\Lib\site-packages\sitecustomize.py
```

如果已有 `sitecustomize.py`，**先备份并合并**，不要直接覆盖。

重启后看到：

```text
[ComfyUI-WinFix] COMFY-WINFIX v2.2 ACTIVE | ... | nosendfile=on | ...
```

说明加载成功。

### 默认行为

```text
self-pipe 保活/自动恢复   开启
native sendfile           关闭（走 read/write）
日志                      关闭
reset 延迟 close          关闭
```

需要时：

```bat
set COMFY_WIN_DEFERRED_CLOSE=1
set COMFY_WIN_SAVELOG=1
```

日志在：

```text
<ComfyUI>\user\winfix_selfpipe.log
```

这个补丁是 Windows Proactor 的兜底方案，不是上游根因修复。
