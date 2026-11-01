"""ComfyUI Windows self-pipe keepalive / re-arm for the portable build.

Installed by ComfyUI-WinFix into ``python_embeded\\Lib\\site-packages``.
It is loaded automatically by ``sitecustomize`` even when ComfyUI is started
with ``python -s ...``; no ComfyUI source file is modified.

What it does (v2.3, Proactor only):

  1. Adds a heartbeat timer (default 60 seconds).  Every heartbeat also writes
     one byte through the event loop self-pipe.  That keeps the loopback
     socketpair active and is the primary defence against the ~2 hour idle
     WinError 121 that killed the Proactor self-pipe read future.
  2. If ``_loop_self_reading()`` still fails, schedules a re-arm of the same
     socket after a short delay.  If re-arm keeps failing, rebuilds the
     socketpair.
  3. Optional log (default off): init/make/close, first write, write failures,
     read-loop failures/re-arm/rebuild, and a 60 second health line.
  4. Sets ``AIOHTTP_NOSENDFILE`` from ``COMFY_WIN_NOSENDFILE`` (default on).
     This avoids the Windows Proactor native-sendfile path that can wedge after
     rapid phone asset browsing.  Set ``COMFY_WIN_NOSENDFILE=0`` to use native
     sendfile.

The banner:
    [ComfyUI-WinFix] COMFY-WINFIX v2.3 ACTIVE | heartbeat=...

Environment variables:
    COMFY_WIN_HEARTBEAT=0             disable heartbeat + self-pipe keepalive
    COMFY_WIN_HEARTBEAT_INTERVAL=60   heartbeat/keepalive seconds (1..3600)
    COMFY_WIN_NOSENDFILE=0            use native aiohttp sendfile
    COMFY_WIN_SAVELOG=1               enable the self-pipe log
    COMFY_WIN_SAVELOG_PATH=...        override the self-pipe log path
    COMFY_WIN_QUIET=1                 do not print the startup banner
"""

import os
import sys

PATCH_TAG = "COMFY-WINFIX v2.3"
sys._comfy_winfix_loaded = True


def _is_comfyui_main() -> bool:
    if not sys.argv or not sys.argv[0]:
        return False
    try:
        path = os.path.abspath(sys.argv[0]).replace("\\", "/").lower()
    except Exception:
        return False
    return path.endswith("/comfyui/main.py") or (
        "/comfyui/" in path and path.endswith("/main.py")
    )


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# self-pipe diagnostics
# ---------------------------------------------------------------------------

_SELFPIPE_LOG_ENABLED = False
_SELFPIPE_LOG_PATH = None
_SELFPIPE_LOG_FILE = None
_SELFPIPE_LOG_LOCK = None
_SELFPIPE_COUNTS = {}
_SELFPIPE_CONSOLE_ONCE = set()
_SELFPIPE_FIRST_WRITE = False
_SELFPIPE_LAST_HEALTH = 0.0


def _selfpipe_default_log_path() -> str:
    try:
        main = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
        user_dir = os.path.join(os.path.dirname(main), "user")
        if os.path.isdir(user_dir):
            return os.path.join(user_dir, "winfix_selfpipe.log")
    except Exception:
        pass
    import tempfile

    return os.path.join(tempfile.gettempdir(), "ComfyUI-WinFix-selfpipe.log")


def _selfpipe_log(
    event: str,
    exc=None,
    stack: bool = False,
    details: str = "",
) -> None:
    global _SELFPIPE_FIRST_WRITE

    if not _SELFPIPE_LOG_ENABLED:
        return
    try:
        import threading
        import time
        import traceback

        count = _SELFPIPE_COUNTS.get(event, 0) + 1
        _SELFPIPE_COUNTS[event] = count
        if event in ("write-fail", "read-loop-fail"):
            if count != 1 and count % 100 != 0:
                return

        parts = [
            time.strftime("%Y-%m-%d %H:%M:%S"),
            f"pid={os.getpid()}",
            f"thread={threading.current_thread().name}",
            f"event={event}",
            f"count={count}",
        ]
        if details:
            parts.append(details)
        if exc is not None:
            parts.append(
                "exc=%r winerror=%r errno=%r"
                % (
                    exc,
                    getattr(exc, "winerror", None),
                    getattr(exc, "errno", None),
                )
            )
        if stack:
            frames = traceback.format_stack(limit=8)
            parts.append("stack=" + " <- ".join(line.strip() for line in frames))
        line = " | ".join(parts)
        if _SELFPIPE_LOG_LOCK is not None:
            with _SELFPIPE_LOG_LOCK:
                _SELFPIPE_LOG_FILE.write(line + "\n")
                _SELFPIPE_LOG_FILE.flush()
        else:
            _SELFPIPE_LOG_FILE.write(line + "\n")
            _SELFPIPE_LOG_FILE.flush()
    except Exception:
        pass

    if event in ("write-fail", "read-loop-fail", "health-bad"):
        if event not in _SELFPIPE_CONSOLE_ONCE:
            _SELFPIPE_CONSOLE_ONCE.add(event)
            try:
                print(
                    f"[ComfyUI-WinFix] self-pipe {event} | "
                    f"log={_SELFPIPE_LOG_PATH}",
                    flush=True,
                )
            except Exception:
                pass


def _selfpipe_sock_info(sock) -> str:
    if sock is None:
        return "None"
    try:
        return (
            f"fd={sock.fileno()} family={sock.family} "
            f"local={sock.getsockname()} peer={sock.getpeername()}"
        )
    except Exception as exc:
        return f"err={exc!r}"


def _selfpipe_log_health(loop) -> None:
    if not _SELFPIPE_LOG_ENABLED:
        return
    csock = getattr(loop, "_csock", None)
    ssock = getattr(loop, "_ssock", None)
    future = getattr(loop, "_self_reading_future", None)
    try:
        cf = csock.fileno() if csock is not None else -1
    except Exception:
        cf = -1
    try:
        sf = ssock.fileno() if ssock is not None else -1
    except Exception:
        sf = -1

    try:
        if future is None:
            future_state = "None"
        elif future.cancelled():
            future_state = "cancelled"
        elif future.done():
            try:
                exc = future.exception()
            except Exception as err:
                exc = err
            future_state = f"done exc={exc!r}"
        else:
            future_state = "pending"
    except Exception as err:
        future_state = f"state-error={err!r}"

    alive = (
        cf >= 0
        and sf >= 0
        and future is not None
        and not future.cancelled()
        and not future.done()
    )
    event = "health" if alive else "health-bad"
    _selfpipe_log(
        event,
        details=(
            f"csock[{_selfpipe_sock_info(csock)}] "
            f"ssock[{_selfpipe_sock_info(ssock)}] "
            f"self_reading_future={future_state}"
        ),
    )


def _install_self_pipe_guard() -> bool:
    """Install keepalive-friendly logging, re-arm and rebuild for Proactor."""
    global _SELFPIPE_LOG_ENABLED, _SELFPIPE_LOG_PATH, _SELFPIPE_LOG_FILE
    global _SELFPIPE_LOG_LOCK

    try:
        import asyncio
        import threading
        from asyncio import proactor_events
    except Exception:
        return False

    cls = getattr(proactor_events, "BaseProactorEventLoop", None)
    if cls is None:
        return False
    if getattr(cls, "_comfy_winfix_v17", False):
        return True

    log_enabled = os.environ.get("COMFY_WIN_SAVELOG", "0") == "1"
    if log_enabled:
        path = (
            os.environ.get("COMFY_WIN_SAVELOG_PATH")
            or _selfpipe_default_log_path()
        )
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            log_file = open(path, "a", encoding="utf-8", buffering=1)
        except Exception:
            log_enabled = False

    if log_enabled:
        _SELFPIPE_LOG_PATH = path
        _SELFPIPE_LOG_FILE = log_file
        _SELFPIPE_LOG_LOCK = threading.Lock()
        _SELFPIPE_LOG_ENABLED = True
        _selfpipe_log(
            "init",
            details=f"path={path} argv0={sys.argv[0] if sys.argv else ''!r}",
        )

    original_write = cls._write_to_self
    original_self_reading = cls._loop_self_reading
    original_make = cls._make_self_pipe
    original_close = cls._close_self_pipe

    def _write_to_self(self, _original=original_write):
        global _SELFPIPE_FIRST_WRITE

        csock = getattr(self, "_csock", None)
        if csock is None:
            _selfpipe_log("write-closed", stack=True)
            return None
        try:
            csock.send(b"\0")
        except Exception as exc:
            _selfpipe_log("write-fail", exc, stack=True)
            return None
        if not _SELFPIPE_FIRST_WRITE:
            _SELFPIPE_FIRST_WRITE = True
            _selfpipe_log("write-ok-first")
        return None

    def _comfy_winfix_rearm(self):
        if self.is_closed():
            return
        self._self_reading_future = None
        try:
            self._loop_self_reading()
        except Exception as exc:
            _selfpipe_log("read-loop-rearm-error", exc, stack=True)
            if not self.is_closed():
                self.call_later(0.5, self._comfy_winfix_rebuild)

    def _comfy_winfix_rebuild(self):
        if self.is_closed():
            return
        _selfpipe_log(
            "self-pipe-rebuild",
            details=(
                f"csock[{_selfpipe_sock_info(getattr(self, '_csock', None))}] "
                f"ssock[{_selfpipe_sock_info(getattr(self, '_ssock', None))}]"
            ),
        )
        try:
            self._close_self_pipe()
            self._make_self_pipe()
            self._self_reading_future = None
            try:
                import signal

                if threading.current_thread() is threading.main_thread():
                    signal.set_wakeup_fd(self._csock.fileno())
            except Exception:
                pass
            self._comfy_winfix_selfpipe_failures = 0
            self._loop_self_reading()
        except Exception as exc:
            _selfpipe_log("self-pipe-rebuild-fail", exc, stack=True)
            if not self.is_closed():
                self.call_later(1.0, self._comfy_winfix_rebuild)

    def _loop_self_reading(self, f=None):
        try:
            if f is not None:
                data = f.result()  # may raise
                if self._self_reading_future is not f:
                    return
                if not data:
                    _selfpipe_log("read-eof", stack=True)
                    self._self_reading_future = None
                    self.call_later(0.25, self._comfy_winfix_rebuild)
                    return
            if self._self_reading_future is not f:
                return
            f = self._proactor.recv(self._ssock, 4096)
        except asyncio.CancelledError:
            _selfpipe_log("read-loop-cancelled", stack=True)
            return
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            _selfpipe_log("read-loop-fail", exc, stack=True)
            self._self_reading_future = None
            failures = (
                getattr(self, "_comfy_winfix_selfpipe_failures", 0) + 1
            )
            self._comfy_winfix_selfpipe_failures = failures
            if not self.is_closed():
                if failures <= 3:
                    _selfpipe_log(
                        "read-loop-rearm",
                        details=f"failures={failures}",
                    )
                    self.call_later(0.25, self._comfy_winfix_rearm)
                else:
                    _selfpipe_log(
                        "read-loop-rebuild-scheduled",
                        details=f"failures={failures}",
                    )
                    self.call_later(0.25, self._comfy_winfix_rebuild)
            try:
                self.call_exception_handler(
                    {
                        "message": "Error on reading from the event loop self pipe",
                        "exception": exc,
                        "loop": self,
                    }
                )
            except Exception:
                pass
        else:
            self._self_reading_future = f
            self._comfy_winfix_selfpipe_failures = 0
            f.add_done_callback(self._loop_self_reading)

    def _make_self_pipe(self, _original=original_make):
        result = _original(self)
        _selfpipe_log(
            "make",
            details=(
                f"csock[{_selfpipe_sock_info(getattr(self, '_csock', None))}] "
                f"ssock[{_selfpipe_sock_info(getattr(self, '_ssock', None))}]"
            ),
        )
        return result

    def _close_self_pipe(self, _original=original_close):
        _selfpipe_log(
            "close",
            stack=True,
            details=(
                f"csock[{_selfpipe_sock_info(getattr(self, '_csock', None))}] "
                f"ssock[{_selfpipe_sock_info(getattr(self, '_ssock', None))}]"
            ),
        )
        return _original(self)

    cls._write_to_self = _write_to_self
    cls._loop_self_reading = _loop_self_reading
    cls._comfy_winfix_rearm = _comfy_winfix_rearm
    cls._comfy_winfix_rebuild = _comfy_winfix_rebuild
    cls._make_self_pipe = _make_self_pipe
    cls._close_self_pipe = _close_self_pipe
    cls._comfy_winfix_v17 = True
    return True


def _install_heartbeat(interval: float) -> bool:
    import asyncio

    base = asyncio.base_events.BaseEventLoop
    original = base.run_forever
    if getattr(original, "_comfy_winfix", False):
        return True

    def _run_forever(self, *args, **kwargs):
        if not getattr(self, "_comfy_winfix_handler", False):
            previous = self.get_exception_handler()

            def _handler(loop, context):
                message = str(context.get("message", ""))
                exc = context.get("exception")
                low = message.lower()
                if "self pipe" in low or "_read_from_self" in low:
                    if exc is None or isinstance(
                        exc,
                        (ConnectionResetError, ConnectionAbortedError, OSError),
                    ):
                        return
                if previous is not None:
                    previous(loop, context)
                else:
                    loop.default_exception_handler(context)

            try:
                self.set_exception_handler(_handler)
                self._comfy_winfix_handler = True
            except Exception:
                pass

        if not getattr(self, "_comfy_winfix_heartbeat", False):
            self._comfy_winfix_heartbeat = True

            def _beat():
                global _SELFPIPE_LAST_HEALTH

                if self.is_closed():
                    return
                # Keepalive: one byte through the self-pipe every heartbeat.
                try:
                    self._write_to_self()
                except Exception:
                    pass
                if _SELFPIPE_LOG_ENABLED:
                    try:
                        import time

                        now = time.monotonic()
                        if _SELFPIPE_LAST_HEALTH == 0.0:
                            _SELFPIPE_LAST_HEALTH = now
                        elif now - _SELFPIPE_LAST_HEALTH >= 60.0:
                            _SELFPIPE_LAST_HEALTH = now
                            _selfpipe_log_health(self)
                    except Exception:
                        pass
                self.call_later(interval, _beat)

            self.call_soon(_beat)

        return original(self, *args, **kwargs)

    _run_forever._comfy_winfix = True
    base.run_forever = _run_forever
    return True


def apply_winfix() -> bool:
    if sys.platform != "win32":
        return False
    if not _is_comfyui_main():
        return False

    nosendfile_ok = os.environ.get("COMFY_WIN_NOSENDFILE", "1") != "0"
    if nosendfile_ok:
        os.environ["AIOHTTP_NOSENDFILE"] = "1"
    else:
        os.environ.pop("AIOHTTP_NOSENDFILE", None)

    selfpipe_ok = _install_self_pipe_guard()

    heartbeat_requested = os.environ.get("COMFY_WIN_HEARTBEAT", "1") != "0"
    interval = _env_float("COMFY_WIN_HEARTBEAT_INTERVAL", 60.0, 1.0, 3600.0)
    heartbeat_ok = False
    if heartbeat_requested:
        heartbeat_ok = _install_heartbeat(interval)

    if os.environ.get("COMFY_WIN_QUIET", "0") != "1":
        hb_text = f"{interval:g}s" if heartbeat_ok else (
            "off" if not heartbeat_requested else "FAILED"
        )
        print(
            f"[ComfyUI-WinFix] {PATCH_TAG} ACTIVE | "
            f"heartbeat={hb_text} | "
            f"nosendfile={'on' if nosendfile_ok else 'off'} | "
            f"selfpipe-log={'on' if _SELFPIPE_LOG_ENABLED else 'off'} | "
            f"selfpipe-keepalive={'on' if heartbeat_ok else 'off'} | "
            f"selfpipe-rearm={'on' if selfpipe_ok else 'off'}",
            flush=True,
        )

    return True


apply_winfix()
