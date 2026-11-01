# ComfyUI-WinFix

Windows 上 ComfyUI 长时间运行后变慢、打不开，或手机快速浏览资产后静态页面只剩 `200` 却没有 body 的修复补丁。

本仓库只有一个文件：

```text
sitecustomize.py
```

Python 会从 `site-packages` 自动加载它，不需要修改 ComfyUI 源码。

> 这是针对 Windows `asyncio` Proactor / aiohttp 的兜底方案，不是上游根因修复。替换任何文件前请先备份。

---

## 现象

- ComfyUI 刚启动时网页秒开；
- 运行大约两小时后，网页开始越来越慢，最后要几十秒甚至打不开；
- Ctrl+C、后台回调也会变慢；
- 手机极短时间内连续预览资产后，可能出现：

  ```text
  HTTP 200
  Content-Length: 20089
  实际 body: 0 字节
  ```

- 此时 `/system_stats` 仍然正常，但 `/`、`/index.html` 出不来；
- 严重时主线程卡在 `socket.close()` / `closesocket()`，整个服务无响应；
- 手机上的视频加载需要等一会儿，但页面本身还能继续打开。

---

## 原因

### 现象一：长时间运行后越来越慢

原因：Windows 上 asyncio 的 self-pipe 是一对 `127.0.0.1` TCP 连接。长时间空闲后，Proactor 的读 future 会以：

```text
WinError 121（信号灯超时时间已到）
```

结束。原版 asyncio 不会重新发起读取，self-pipe 读循环永久停止。

主循环还在，API 也还能响应，但线程池完成、Ctrl+C 等唤醒只能等心跳；静态文件一个接一个等，首页就累积到几十秒。

对应修复：

- 心跳定时向 self-pipe 写 1 字节，防止空闲；
- 读循环失败后 re-arm；
- re-arm 连续失败后 rebuild。

### 现象二：手机极速浏览资产后 sendfile 卡 body

原因：aiohttp 的 `FileResponse` 在 Windows Proactor 下默认走 native sendfile。手机快速取消/中断大量资产请求后，native sendfile 状态可能残留，之后静态文件只发响应头，不发 body。

对应修复：

- 默认设置 `AIOHTTP_NOSENDFILE=1`，让静态文件走 read/write 回退；
- 在 self-pipe 保活正常的情况下，回退路径实测也是毫秒级；
- 如果需要原生 sendfile，设置 `COMFY_WIN_NOSENDFILE=0`。

### 现象三：reset 连接 close 卡住

原因：手机 RST 连接后，Proactor 清理会调用 `socket.close()` / `closesocket()`。在某些 Windows native sendfile 残留状态下，这个 close 会长时间不返回，卡住主线程。

对应修复（可选）：

- `COMFY_WIN_DEFERRED_CLOSE=1` 时，只对 reset/`OSError` 连接延迟 close；
- 普通正常关闭仍然同步；
- 最多同时 16 个 close 线程，超过上限的 socket 直接 `detach`，不创建新线程。

---

## 默认行为

```text
self-pipe 保活/re-arm/rebuild    on
heartbeat                        60 秒
COMFY_WIN_NOSENDFILE             1（禁用 native sendfile）
日志                              off
reset 延迟 close                  off
```

只有 `COMFY_WIN_NOSENDFILE=0` 才恢复原生 sendfile。

---

## 安装（手动）

这个仓库**没有安装脚本**，因为不同 ComfyUI 安装方式的 Python 环境不同。

### 1. 找到真正启动 ComfyUI 的 Python

用**启动 ComfyUI 的那个 Python**运行：

```bat
python.exe -c "import site; print(site.getsitepackages())"
```

把 `sitecustomize.py` 放进第一个可写的 `site-packages`。

常见位置：

**Windows 便携版**

```text
ComfyUI_windows_portable\python_embeded\Lib\site-packages\sitecustomize.py
```

**仓库 / venv / Desktop**

```text
<ComfyUI 目录>\.venv\Lib\site-packages\sitecustomize.py
```

ComfyUI Desktop 可能使用 `.venv`、`standalone-env` 或其他打包环境。  
**只放在一个没有 torch / aiohttp 的基础环境里不会生效，也不会让 ComfyUI 能运行**；要确认那个 Python 真的能启动 `ComfyUI\main.py`。

### 2. 不要覆盖已有的 `sitecustomize.py`

如果目标目录里已经有 `sitecustomize.py`：

- 先备份；
- 把本补丁的代码合并进去；
- 直接覆盖可能影响其他 Python 程序。

### 3. 重启 ComfyUI

启动横幅应出现：

```text
[ComfyUI-WinFix] COMFY-WINFIX v2.2 ACTIVE | heartbeat=60s | nosendfile=on | selfpipe-log=off | selfpipe-keepalive=on | selfpipe-rearm=on | deferred-close=off
```

看不到横幅说明没有加载：

- 放错 `site-packages`；
- 启动时用了 `-S`；
- 文件有语法错误；
- 被别的 `sitecustomize.py` 覆盖。

---

## 验证

```powershell
curl.exe --noproxy "*" -o NUL -w "HTTP %{http_code} bytes=%{size_download} time %{time_total}s\n" http://127.0.0.1:8870/
```

正常应是：

```text
HTTP 200 bytes=20089
```

不是：

```text
HTTP 200 bytes=0
```

---

## 可选环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `COMFY_WIN_HEARTBEAT` | `1` | `0` = 关闭心跳和 self-pipe 保活 |
| `COMFY_WIN_HEARTBEAT_INTERVAL` | `60` | 心跳/保活秒数，1～3600 |
| `COMFY_WIN_NOSENDFILE` | `1` | `1` = 禁用 native sendfile；`0` = 启用 |
| `COMFY_WIN_DEFERRED_CLOSE` | `0` | `1` = 启用 reset 连接的延迟 close |
| `COMFY_WIN_MAX_CLOSE_THREADS` | `16` | 延迟 close 最大并发线程数，1～256 |
| `COMFY_WIN_SAVELOG` | `0` | `1` = 写 self-pipe 诊断日志 |
| `COMFY_WIN_SAVELOG_PATH` | 自动 | 覆盖日志路径 |
| `COMFY_WIN_QUIET` | `0` | `1` = 不打印启动横幅 |

手机极速浏览资产较多时，建议保留：

```bat
set COMFY_WIN_NOSENDFILE=1
set COMFY_WIN_DEFERRED_CLOSE=1
```

需要排查时再加：

```bat
set COMFY_WIN_SAVELOG=1
```

日志默认在：

```text
ComfyUI\user\winfix_selfpipe.log
```

---

## 日志里看什么

```text
init
make
write-ok-first
health ... self_reading_future=pending
```

正常长跑应只有 `health ... pending`。

异常：

```text
read-loop-fail
read-loop-rearm
self-pipe-rebuild
health-bad
close-abandoned
```

`read-loop-fail` 是 self-pipe 读循环失败；`close-abandoned` 是 reset close 线程达到上限后主动 detach。

---

## 注意

- 这是 Windows 专用补丁；
- 需要 CPython 3.12/3.13；
- ComfyUI 更新、Desktop 重建 `.venv` 后，文件可能被删除，需要重新放入；
- native sendfile 性能更好，但在手机极速资产场景下可能卡 body；默认禁用它换稳定；
- 本补丁不修改 ComfyUI 源码，只通过 `sitecustomize.py` 生效；
- 卸载：停止 ComfyUI，删除 `site-packages\sitecustomize.py`，重启。

---

## English summary

A single-file Windows workaround for ComfyUI that:

- keeps the asyncio Proactor self-pipe alive;
- re-arms/rebuilds it if it fails;
- disables the fragile native sendfile path by default (`COMFY_WIN_NOSENDFILE=1`);
- optionally defers close of reset sockets (`COMFY_WIN_DEFERRED_CLOSE=1`).

Copy `sitecustomize.py` into the `site-packages` directory of the Python environment that actually launches `ComfyUI\main.py`, restart ComfyUI, and look for the `[ComfyUI-WinFix]` banner.

Do not overwrite an existing `sitecustomize.py`; back it up and merge.

Tested on Windows 11, CPython 3.12/3.13, ComfyUI portable and Desktop `.venv`.

This is a workaround, not an upstream fix.
