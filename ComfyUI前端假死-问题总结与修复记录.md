# ComfyUI Windows 前端假死：问题总结与修复记录

> 记录时间：2026-11-03  
> 环境：Windows 11、ComfyUI 0.43、Python 3.13.14、aiohttp 3.14.x、ProactorEventLoop  
> 最终版本：ComfyUI-WinFix v2.4  
> 最终加载方式：`comfy_win_fix.py` + `zzz_comfy_win_fix.pth`  
> 已提交issue：https://github.com/Comfy-Org/ComfyUI/issues/17495

---

## 1. 结论摘要

这次前端假死不是 ComfyUI 节点、显卡、模型或前端 JS 的问题，而是 Windows asyncio Proactor 与 aiohttp 的几个底层问题叠加：

1. **self-pipe 长时间空闲后读 future 以 WinError 121 结束**，原版 asyncio 不会重新发起读取，导致线程池完成、Ctrl+C 等无法及时唤醒事件循环。
2. **Windows 原生 sendfile 在大量资产请求被中断后可能卡住**，表现为静态页面返回 HTTP 200 但 body 为 0 字节。
3. **native sendfile 中断后的连接清理会连带导致 `closesocket` 卡住**，主线程停在 `socket.close()`，整个服务无响应。

最终处理：

- `self-pipe`：心跳每 60 秒写入 1 字节保活；失败后 re-arm；连续失败后 rebuild。
- `sendfile`：默认设置 `AIOHTTP_NOSENDFILE=1`，走 read/write 回退。
- `closesocket`：不再单独做延迟 close；因为确认它主要是 native sendfile 中断后的下游问题。
- 加载方式：从 `sitecustomize.py` 迁移为 `zzz_comfy_win_fix.pth` + `comfy_win_fix.py`，避免 `sitecustomize` 单例冲突。

---

## 2. 现象

### 2.1 长时间运行后

- ComfyUI 刚启动时网页秒开；
- 运行约两小时后，页面开始难以访问；
- 有时多次刷新才能打开，最后完全打不开；
- 电脑、手机访问都受影响。

### 2.2 手机快速浏览资产后

- 手机在资产页快速下滑、连续预览大量视频/图片；
- 静态页面可能返回：

```text
HTTP 200
Content-Length: 20089
实际 body: 0 字节
```

- `/system_stats` 仍可能返回 200，但 `/index.html`、静态 JS/CSS 不出 body；
- 有时主线程直接卡在 `socket.close()`，所有请求都无响应。

### 2.3 实际抓到的栈

第一阶段，主线程卡在 `closesocket`：

```text
Thread "MainThread"
  _real_close (socket.py)
    close
      _call_connection_lost (proactor_events.py)
        exc: ConnectionResetError
```

同时：

```text
CLOSE_WAIT 39
Established 1
```

另一阶段，主线程健康，但静态文件 body 为 0：

```text
_poll (windows_events.py)
  timeout: 18.9s

GET /system_stats  -> 200，1180 字节
GET /              -> 200，0 字节，8s 超时
GET /index.html    -> 200，0 字节，8s 超时
```

---

## 3. 根因分析

### 3.1 self-pipe 空闲超时

Windows 上 asyncio 的 self-pipe 实际是一对 loopback TCP 连接：

```text
127.0.0.1:A <-> 127.0.0.1:B
```

事件循环通过它接收其他线程的唤醒：

- `call_soon_threadsafe`；
- 线程池完成回调；
- 信号/Ctrl+C 等。

日志显示：

```text
03:42:57 make
05:43:20 read-loop-fail winerror=121
```

时间差：

```text
2 小时 0 分 23 秒
```

`WinError 121` 为“信号灯超时时间已到”。原版 `_loop_self_reading()` 在失败后只调用异常处理器，**不会重新发起 recv**，于是读循环永久停止。

之后：

- 主循环还能处理网络 I/O；
- API 还能响应；
- 线程池完成却无法唤醒事件循环；
- 启用 NOSENDFILE 时，静态文件每个线程池完成最多等一个心跳；
- 1 秒心跳下，几十个静态资源可以累积到 40～60 秒。

### 3.2 native sendfile 卡住

ComfyUI 使用 aiohttp 的 `FileResponse` 发送静态文件。Windows Proactor 下 aiohttp 会优先走 native sendfile。

大量资产请求被手机快速取消/RST 后，可能进入：

```text
响应头 200
body 0
```

此时主线程可能仍然健康，`/system_stats` 正常，但所有 `FileResponse` 静态文件都不出 body。

### 3.3 sendfile 中断导致 closesocket 下游卡住

如果 native sendfile 正在发送时连接被中断，Proactor 的 sendfile/transport 状态可能残留，随后：

```text
_call_connection_lost()
  -> socket.close()
    -> closesocket()
```

`closesocket` 可能长时间不返回，主线程被拖死。

所以这次极速资产后的 close 卡住，**更像是 native sendfile 被 abort 后的下游症状**，而不是独立的 close bug。

### 3.4 相关上游问题

- [CPython #160429](https://github.com/python/cpython/issues/160429)：Proactor 清理时 `shutdown()` 抛 reset 会跳过后续 close/detach。
- [bpo-35682](https://mail.python.org/archives/list/new-bugs-announce@python.org/thread/PSW2NI3JVJBLQNSPD5DIKHGC6E6A4TGS/?noscript)：Windows native sendfile 的 `_force_close` / `_empty_waiter` 状态问题。
- [aiohttp PR #12096](https://github.com/aio-libs/aiohttp/pull/12096)：sendfile fallback 的读取/计数问题。

这些问题不是 ComfyUI 自己造成的，补丁只能绕开或兜底，不能真正修复 Windows Proactor/aiohttp 的 native sendfile 实现。

---

## 4. 错误排错方向

这部分很重要：最终方案不是一开始就想对的，中间走了不少弯路。

### 4.1 WFP / 雷神 / nfwfp / FlClash

最初怀疑：

- Windows Filtering Platform 过滤 loopback；
- 雷神加速器遗留 `nfwfp` 驱动；
- FlClash TUN 服务影响 127.0.0.1；
- 安全软件 reset self-pipe。

做过：

- 删除 `nfwfp`；
- 停止 FlClashHelperService；
- 跑 `check_loopback.py`，结果：

```text
[OK] 127.0.0.1 loopback connect
[OK] socket.socketpair()
RESULT: loopback looks healthy
```

结论：

- 回环检查正常；
- 删除过滤驱动后问题仍然存在；
- WFP/FlClash 不是这次的主因；
- 文章里的 WFP 思路可以参考，但不能解释 2 小时固定失败和 sendfile body 0。

### 4.2 改用 SelectorEventLoop

尝试过：

```text
COMFY_WIN_SELECTOR=1
```

结果：

- Selector 下静态文件能发；
- 但手机第一次打开页面会卡在加载页；
- 不能作为默认；
- 最终删除 Selector 代码。

### 4.3 只加 SO_LINGER

尝试过对 reset 连接设置：

```text
SO_LINGER {on, 0}
```

结果：

- 有时能减轻；
- 但仍会出现主线程卡在 `socket.close()`；
- 不能作为唯一修复。

### 4.4 延迟 close 当作最终方案

v1.8/v2.1 曾对所有 reset 连接做 daemon 线程 close，并加线程上限。

结果：

- 主线程不再卡在 close；
- 但 native sendfile 的 body 0 仍然存在；
- 后来确认 close 卡住主要是 sendfile 中断的下游问题；
- v2.3 删除延迟 close；
- v2.4 默认 NOSENDFILE 后，极速资产不再出现 close 堆积。

### 4.5 认为是 ComfyUI-Manager 的 30 秒

曾观察：

```text
ComfyUI-Manager cache update
FETCH DATA custom-node-list.json
```

一开始怀疑 Manager 拖慢页面。

结论：

- Manager 初始网络请求确实会造成一次性启动延迟；
- 但关闭 Manager 后页面问题仍存在；
- 不是本问题的根因。

### 4.6 NOSENDFILE + 1 秒心跳 40～60 秒

曾配置：

```text
AIOHTTP_NOSENDFILE=1
COMFY_WIN_HEARTBEAT_INTERVAL=1
```

出现页面 40～60 秒。

错误归因：

- 以为是 NOSENDFILE 回退本身慢。

实际原因：

- self-pipe 已经失效；
- 线程池完成只能等 1 秒心跳；
- 每个静态资源等一次心跳，累加成几十秒；
- 加入 self-pipe 保活后，NOSENDFILE 回退实测是毫秒级。

### 4.7 想把心跳设成 1 小时

曾考虑：

```text
心跳只负责保活，设 1 小时即可
```

没有采用，原因：

- 心跳同时是 self-pipe 失败后的兜底唤醒；
- 如果 re-arm/rebuild 也失败，页面和 Ctrl+C 最坏等 1 小时；
- 最终默认 60 秒，允许范围 1～3600 秒。
- 但是目前实测，self-pipe 约在两小时挂掉；而随心跳写入1字节 能够将 self-pipe 成功保活，所以心跳范围可设置为 1s 到 3600s（一个小时），默认60s。

### 4.8 认为 sendfile 和 self-pipe 完全独立

中途曾认为：

```text
sendfile body 0 与 self-pipe 无关
```

后来确认更准确的关系是：

- self-pipe 失效会让 NOSENDFILE 回退变慢（访问网页需要等几十次心跳加载资源）；
- native sendfile 中断会让 close 卡住；
- 两者都发生在 Proactor 上，但触发环节不同；
- 不能简单说“完全独立”，也不能说“同一个 bug”。

### 4.9 线程上限 / detach 作为最终修复

曾实现：

```text
COMFY_WIN_MAX_CLOSE_THREADS=16
超过上限 detach socket
```

结论：

- 能避免线程无限增长；
- 但只是 close 的兜底；
- 没有修复 native sendfile，comfy进程依旧卡死；
- 最终随延迟 close 一起删除。

---

## 5. 最终修复方案

### 5.1 加载方式：.pth + 独立模块

文件：

```text
comfy_win_fix.py
zzz_comfy_win_fix.pth
```

`zzz_comfy_win_fix.pth` 内容：

```text
import comfy_win_fix
```

放置到：

```text
ComfyUI_windows_portable\python_embeded\Lib\site-packages
```

或：

```text
<ComfyUI 目录>\.venv\Lib\site-packages
```

优点：

- 不再占用 `sitecustomize.py` 单例；
- 可以和 quiet-assets 等其他 `.pth` 补丁共存；
- 不会覆盖其他程序的 `sitecustomize.py`；
- `python -s` 仍生效；
- `python -S` 下两者都不生效。

注意：

- `.pth` 不能带 BOM；
- 必须单行，行首正好是 `import `；
- `.pth` 阶段异常会被 site 吞掉，所以启动 banner 是自检手段；
- 不要在模块顶层导入 torch 等重依赖。

### 5.2 self-pipe 保活

默认：

```text
heartbeat=60s
```

每次心跳向 self-pipe 写 1 字节。

作用：

- 防止约 2 小时空闲后 WinError 121；
- 保持读 future 活跃；
- 同时作为 self-pipe 失效时的兜底唤醒。

### 5.3 re-arm / rebuild

读循环失败时：

1. 0.25 秒后对同一对 socket 重新发起 `recv`（re-arm）；
2. 连续失败 3 次后关闭并重建 socketpair（rebuild）；
3. 更新 `signal.set_wakeup_fd`。

日志事件：

```text
read-loop-fail
read-loop-rearm
self-pipe-rebuild
self-pipe-rebuild-fail
health-bad
```

Note：保活成功，没有触发过这个步骤，不知道有没有效果。

### 5.4 NOSENDFILE 默认开启

默认：

```text
COMFY_WIN_NOSENDFILE=1
```

补丁设置：

```python
os.environ["AIOHTTP_NOSENDFILE"] = "1"
```

作用：

- 避开 native sendfile 的 body 0；
- 避开 native sendfile 中断后的 close 卡住；
- 回退路径在 self-pipe 保活下实测毫秒级。

可选恢复 native sendfile：

```bat
set COMFY_WIN_NOSENDFILE=0
```

补丁会清除 `AIOHTTP_NOSENDFILE`。

### 5.5 删除延迟 close

v2.3 起删除：

- `_DeferredCloseSocket`
- `_install_deferred_close_guard`
- `COMFY_WIN_DEFERRED_CLOSE`
- `COMFY_WIN_MAX_CLOSE_THREADS`

原因：

- native sendfile 中断是 close 卡住的主要触发点；
- 默认 NOSENDFILE 后极速资产不再出现 close 堆积；
- 延迟 close 没有解决 sendfile 本身，同时局域网手机快速预览资源进程依旧卡死，该修补功能无效；
- 避免线程和 detached socket 的复杂度。

如果未来在 `NOSENDFILE=1` 下再次出现 `socket.close` 卡住，再重新评估。

---

## 6. 默认配置与可选项

### 6.1 默认

```text
self-pipe 保活/re-arm/rebuild    on
heartbeat                        60 秒
COMFY_WIN_NOSENDFILE             1
日志                              off
reset 延迟 close                  removed
```

### 6.2 环境变量

| 变量                           | 默认 | 说明                                   |
| ------------------------------ | ---- | -------------------------------------- |
| `COMFY_WIN_HEARTBEAT`          | `1`  | `0` = 关闭心跳和 self-pipe 保活        |
| `COMFY_WIN_HEARTBEAT_INTERVAL` | `60` | 心跳/保活秒数，1～3600                 |
| `COMFY_WIN_NOSENDFILE`         | `1`  | `1` = 禁用 native sendfile；`0` = 启用 |
| `COMFY_WIN_SAVELOG`            | `0`  | `1` = 开启 self-pipe 诊断日志          |
| `COMFY_WIN_SAVELOG_PATH`       | 自动 | 覆盖日志路径                           |
| `COMFY_WIN_QUIET`              | `0`  | `1` = 不打印启动横幅                   |

已删除：

```text
COMFY_WIN_SELECTOR
COMFY_WIN_WRITE_GUARD
COMFY_WIN_CLOSE_GUARD
COMFY_WIN_DEFERRED_CLOSE
COMFY_WIN_MAX_CLOSE_THREADS
COMFY_WIN_SELF_PIPE_GUARD
COMFY_WIN_SELFPIPE_LOG
COMFY_WIN_SELFPIPE_LOG_PATH
```

---

## 7. 验证记录

### 7.1 2 小时 WinError 121

日志：

```text
make      03:42:57
fail      05:43:20
```

差值：

```text
2h 00m 23s
```

失败后：

```text
health-bad self_reading_future=done exc=121
```

### 7.2 8 小时保活验证

一次长时间运行日志：

```text
init                1
make                1
write-ok-first      1
health            651
read-loop-fail      0
read-loop-rearm     0
self-pipe-rebuild   0
health-bad          0
write-fail          0
```

说明：

- self-pipe 保活有效；
- re-arm/rebuild 没有被触发；
- 真正起作用的是“心跳写 1 字节”。

### 7.3 极速资产后 sendfile body 0

一次卡死状态：

```text
MainThread:
  _poll timeout=18.9s

GET /system_stats -> 200，1180 字节
GET /             -> 200，0 字节
GET /index.html   -> 200，0 字节

CLOSE_WAIT 7
comfy-winfix-close 线程 2
```

换成 NOSENDFILE 后：

```text
GET /              -> 200，20089 字节，0.004s
GET /index.html    -> 200，20089 字节，0.003s
GET /system_stats  -> 200，1090 字节，0.009s
```

### 7.4 .pth 加载验证

便携版：

```text
[ComfyUI-WinFix] COMFY-WINFIX v2.4 ACTIVE | heartbeat=60s | nosendfile=on | selfpipe-log=off | selfpipe-keepalive=on | selfpipe-rearm=on
```

页面：

```text
GET /              -> 200，20089 字节，0.005s
GET /index.html    -> 200，20089 字节，0.004s
GET /system_stats  -> 200，1120 字节，0.003s
```

说明：

- `python -s` 下 `.pth` 正常工作；
- 模块和 banner 正常；
- 页面完整发送。

### 7.5 桌面版

桌面版 `ComfyUI\.venv` 曾用同一补丁启动成功：

- banner 正常；
- `/` 返回 200、20089 字节；
- 说明补丁不依赖便携版目录。

后来桌面版目录被移除，安装器会跳过不存在的目标。

---

## 8. 安装与卸载脚本

当前个人脚本：

```text
D:\1_Necessary\AI_draw\0_ComfyUI_Custom\脚本\ComfyUI-WinFix\安装WinFix补丁.bat
```

变量：

```bat
set "SRC_DIR=%~dp0"
```

补丁文件从：

```text
%SRC_DIR%\comfy_win_fix.py
%SRC_DIR%\zzz_comfy_win_fix.pth
```

读取。

菜单：

```text
1. 安装
2. 卸载
```

安装目标：

```text
ComfyUI_windows_portable\python_embeded\Lib\site-packages
ComfyUI-Installs\*\ComfyUI\.venv\Lib\site-packages
```

不存在则跳过。

卸载只删除：

```text
comfy_win_fix.py
zzz_comfy_win_fix.pth
```

不再处理 `sitecustomize.py`，因为旧实现已经废弃，当前机器上也没有旧文件。

---

## 9. 诊断方法

### 9.1 启动横幅

看到：

```text
[ComfyUI-WinFix] COMFY-WINFIX v2.4 ACTIVE
```

说明补丁已加载。

没有横幅：

- 文件不在实际 Python 的 `site-packages`；
- `.pth` 有 BOM 或格式错误；
- 启动用了 `-S`；
- 目标环境不是启动 ComfyUI 的环境。

### 9.2 HTTP 判断

```text
/system_stats 正常，/index.html 200 但 0 字节
```

优先怀疑：

- native sendfile body 0；
- 检查 `COMFY_WIN_NOSENDFILE` 是否为 1。

```text
所有请求都不响应，主线程卡在 socket.close
```

优先怀疑：

- native sendfile 中断后的 close 下游问题；
- 抓 py-spy 确认栈。

```text
主线程 _poll 正常，但线程池/回调慢
```

优先怀疑：

- self-pipe 读循环失效；
- 开启 `COMFY_WIN_SAVELOG=1` 看 `read-loop-fail`/`health-bad`。

### 9.3 日志

默认关闭。开启：

```bat
set COMFY_WIN_SAVELOG=1
```

文件：

```text
ComfyUI\user\winfix_selfpipe.log
```

关注：

```text
read-loop-fail
read-loop-rearm
self-pipe-rebuild
health-bad
write-fail
```

正常长跑：

```text
make
write-ok-first
health ... pending
health ... pending
...
```

---

## 10. 最终文件与版本

### 10.1 补丁文件

```text
comfy_win_fix.py
zzz_comfy_win_fix.pth
README.md
```

### 10.2 版本

```text
COMFY-WINFIX v2.4
```

### 10.3 Git

本地仓库保留完整历史，关键提交：

```text
90c79a0 v2.4: load via comfy_win_fix.py + zzz_comfy_win_fix.pth
2558358 docs: remove old sitecustomize migration note
0221c29 v2.3: remove reset-socket deferred close
7d654fb docs: update install banner for default nosendfile
2a073d8 v2.2: unified COMFY_WIN_NOSENDFILE (default on) and docs
ab2a693 v2.1: native sendfile default, opt-in log/reset close, bounded close threads
36c8204 v2.0 baseline: self-pipe keepalive/rearm, optional log and reset close
```

### 10.4 公共仓库

```text
https://github.com/liuyu-f/ComfyUI-WinFix
```

仓库中只有：

```text
README.md
comfy_win_fix.py
zzz_comfy_win_fix.pth
```

旧 `sitecustomize.py` 已删除。

---

## 11. 已知限制

1. 这不是上游根因修复。Windows Proactor 的 native sendfile 问题需要 CPython/aiohttp 修复。
2. 默认 NOSENDFILE 会失去原生 sendfile 的性能优势，但当前实测回退路径是毫秒级。
3. `.pth` 加载异常会被 site 吞掉，只能靠启动 banner 自检。
4. ComfyUI 更新如果重建 `.venv`/`python_embeded`，补丁文件会丢失，需要重新安装。
5. 当前不再保留旧 `sitecustomize.py` 兼容逻辑；旧文件已手动清理。
6. 如果未来在 NOSENDFILE=1 下再次出现 close 卡住，需要重新抓栈并评估是否恢复延迟 close。

---

## 12. 经验总结

- 先用最小可复现实验区分问题层：
  - `/system_stats` 正常、静态文件 0 字节 → sendfile；
  - 所有请求无响应、主线程 close → close/transport；
  - 主线程 \_poll 正常、线程池慢 → self-pipe。
- 不要只看 HTTP 200；要看 `Content-Length` 和实际字节数。
- 不要把“暂时不卡”当成修复；0.05 秒心跳只是掩盖 self-pipe 死亡。
- 不要把 native sendfile 的性能优势和稳定性混为一谈；在 Windows Proactor 上它确实可能卡。
- 保留日志和可回退的 git 历史，比继续猜测更重要。
