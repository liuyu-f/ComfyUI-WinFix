# ComfyUI-WinFix

- 修复Comfy UI长时间空置运行后无法访问。
- 修复局域网设备（如手机）访问Comfy UI并在资产页快速下滑后导致任何设备将无法访问Comfy UI。

## 安装

需要同时放入两个文件：

```text
comfy_win_fix.py
zzz_comfy_win_fix.pth
```

- Comfy Desktop端：放入 `(comfy实例)\ComfyUI\.venv\Lib\site-packages`
- ComfyUI便携版：放入 `ComfyUI_windows_portable\python_embeded\Lib\site-packages`

`zzz_comfy_win_fix.pth` 只有一行，且不能带 BOM：

```text
import comfy_win_fix
```

## 现象

如果你遇到下面这些情况，这个补丁就是针对它们的：

- ComfyUI 刚启动时一切正常，网页秒开；
- 运行大约两小时后，网页开始难以访问，最后要多次访问偶尔才能打开甚至打不开；
- 手机快速连续预览资产后，整个网页服务可能突然无响应；
- 关掉终端里返回 `200` 但页面就是不出来。

这些现象不是 ComfyUI 节点、显卡或模型的问题，而是 Windows 上 asyncio Proactor / aiohttp 的几个底层问题。

---

## 原因

### 问题一：self-pipe 长时间空闲后失效

Windows 上 asyncio 的 self-pipe 实际是一对 `127.0.0.1` TCP 连接。

事件循环靠它接收其他线程的唤醒，例如：

- Ctrl+C；
- 后台线程回调；
- 线程池完成通知。

这台机器上，self-pipe 在约 **2 小时没有数据**后，Proactor 的读操作会以：

```text
WinError 121（信号灯超时时间已到）
```

结束。原版 asyncio 在失败后只记录异常，**不会重新发起读取**，于是读循环永久停止。

结果就是：

- 主循环还在跑；
- API 还能响应；
- 但线程池完成、Ctrl+C 等无法响应；

### 问题二：极速资产后 sendfile 只发头不发 body

手机在极短时间内连续预览大量资产时，aiohttp 的 Windows 原生 sendfile 路径可能进入：

```text
HTTP 200
Content-Length: 20089
实际 body: 0 字节
```

此时 `/system_stats` 仍然正常，主线程也还在 `_poll`，所以不是 self-pipe，也不是主线程 `closesocket`。

### 问题三：sendfile 中断会连带让 `closesocket` 卡住

手机快速浏览资产会建立大量连接，然后很快取消或关闭。  
如果此时正在走 native sendfile，Proactor 的 sendfile 状态可能残留，随后：

```text
_call_connection_lost()
  -> socket.close()
    -> closesocket()
```

`closesocket` 可能长时间不返回，主线程被卡住。

这不是独立的 close 问题，而是 native sendfile 被 abort 后的下游表现。  
v2.4 默认：

```bat
set COMFY_WIN_NOSENDFILE=1
```

让 aiohttp 走 read/write 回退，同时避开 body 0 和 close 卡住。  
回退路径在 self-pipe 保活下实测是毫秒级。

如果要恢复原生 sendfile：

```bat
set COMFY_WIN_NOSENDFILE=0
```

代价是极速资产后仍可能再次出现 body 0 或 `closesocket` 卡住。AIOHTTP_NOSENDFILE 现在由补丁统一管理，不需要再手动设置它。

---

## 修复

### 1. self-pipe 保活

心跳每 60 秒向 self-pipe 写 1 字节：

- self-pipe 不再长时间空闲；
- 读操作持续刷新；
- 2 小时 WinError 121 不再出现。

日志中长时间运行应只有：

```text
health ... self_reading_future=pending
```

不再出现：

```text
read-loop-fail ... winerror=121
```

### 2. self-pipe 失败后的兜底

如果保活仍然失败：

1. 0.25 秒后用同一对 socket 重新发起读取（re-arm）；
2. 连续失败 3 次后关闭并重建 self-pipe（rebuild）；
3. 同时更新信号唤醒 fd。

### 3. 默认禁用 sendfile

补丁默认：

```bat
set COMFY_WIN_NOSENDFILE=1
```

并据此设置 aiohttp 的 `AIOHTTP_NOSENDFILE`。

要恢复原生 sendfile：

```bat
set COMFY_WIN_NOSENDFILE=0
```

### 4. 不再保留 reset 延迟 close

早期版本曾尝试对 RST 连接延迟 close，但后续确认：

- close 卡住主要是 native sendfile 被 abort 后的下游表现；
- 默认禁用 sendfile 后，极速资产不再产生该问题；
- 延迟 close 没有解决 sendfile 本身，反而增加线程和 detached socket 的复杂度。

因此 v2.4 删除了 reset 延迟 close 代码。  
如果未来在 `NOSENDFILE=1` 下仍出现主线程卡在 `socket.close`，再重新评估。

---

## 默认行为

默认只保留真正需要长期生效的修复：

- self-pipe 心跳保活；
- self-pipe re-arm/rebuild；
- 禁用 native sendfile（`COMFY_WIN_NOSENDFILE=1`）。

默认关闭：

- self-pipe 诊断日志（需要时 `COMFY_WIN_SAVELOG=1` 手动开启）；
- 原生 sendfile（需要时 `COMFY_WIN_NOSENDFILE=0` 手动开启）。

日志默认不写文件，但 self-pipe 保活和 re-arm/rebuild 仍然生效。  
`COMFY_WIN_SAVELOG=0` 是默认状态，不需要在启动脚本里写。

心跳默认 **60 秒**，可通过 `COMFY_WIN_HEARTBEAT_INTERVAL` 调整，范围 **1～3600 秒**。  
如果设得很大，self-pipe 失效后的兜底唤醒也会变慢；一般 60 秒足够保活。

### 可选环境变量

| 变量                           | 默认 | 说明                                   |
| ------------------------------ | ---- | -------------------------------------- |
| `COMFY_WIN_HEARTBEAT`          | `1`  | `0` = 关闭心跳和 self-pipe 保活        |
| `COMFY_WIN_HEARTBEAT_INTERVAL` | `60` | 心跳/保活秒数，1～3600                 |
| `COMFY_WIN_NOSENDFILE`         | `1`  | `1` = 禁用 native sendfile；`0` = 启用 |
| `COMFY_WIN_SAVELOG`            | `0`  | `1` = 开启 self-pipe 诊断日志          |
| `COMFY_WIN_SAVELOG_PATH`       | 自动 | 覆盖日志路径                           |
| `COMFY_WIN_QUIET`              | `0`  | `1` = 不打印启动横幅                   |

---

## 原理简述

### self-pipe

```text
其他线程 / Ctrl+C / 线程池完成
  -> 写 self-pipe
    -> 事件循环醒来
      -> 执行回调
```

self-pipe 坏掉后，网络 I/O 还能处理，但“线程唤醒”失效。

### closesocket

```text
手机 RST
  -> Proactor 通知连接丢失
    -> socket.close()
      -> closesocket()
```

`closesocket` 卡住时，主线程无法继续 `_poll`，所有请求都会停。

### 与 sendfile 的关系

这台机器上，`closesocket` 卡住不是独立问题，而是 native sendfile 被 abort 后的下游表现。  
默认 `COMFY_WIN_NOSENDFILE=1` 后不再产生这种 sendfile 残留，因此 v2.4 删除了 reset 延迟 close 代码。

self-pipe 则是另一条独立路径：它是唤醒通道，不会因为禁用 sendfile 而自动修好。

---

## 诊断日志

默认关闭。需要时在启动脚本里手动开启：

```bat
set COMFY_WIN_SAVELOG=1
```

日志写入：

```text
ComfyUI\user\winfix_selfpipe.log
```

可用 `COMFY_WIN_SAVELOG_PATH` 改路径。  
日志关闭不影响 self-pipe 保活/re-arm/rebuild。

---

## 兼容性

- Windows 11；
- ComfyUI 便携版，Python 3.13；
- 只对 `...\ComfyUI\main.py` 启动的进程生效；
- 不修改 ComfyUI 源码，只通过 `zzz_comfy_win_fix.pth` 加载 `comfy_win_fix.py` 生效。
