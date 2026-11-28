
from __future__ import annotations

import os
import re
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "warning": 30, "error": 40}
_LABEL = {"debug": "DEBUG", "info": "INFO ", "warn": "WARN ", "error": "ERROR"}


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off", "")


LOG_ON = _env_bool("REG_LOG", True)
LOG_DIR = os.path.abspath(os.environ.get("REG_LOG_DIR") or os.path.join(HERE, "logs"))
LEVEL = _LEVELS.get((os.environ.get("REG_LOG_LEVEL") or "info").strip().lower(), 20)
ECHO = _env_bool("REG_LOG_ECHO", False)
try:
    MAX_BYTES = int(float(os.environ.get("REG_LOG_MAX_MB") or 20) * 1024 * 1024)
except ValueError:
    MAX_BYTES = 20 * 1024 * 1024
try:
    KEEP = max(1, int(os.environ.get("REG_LOG_KEEP") or 5))
except ValueError:
    KEEP = 5

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_LOCK = threading.RLock()
_LOGGERS: dict[str, "Logger"] = {}

_SECRETS = (
    re.compile(r"(thk_[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]*"),
    re.compile(r"(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]*"),
    re.compile(r"(?i)(borne?r\s+)([A-Za-z0-9._\-]{4})[A-Za-z0-9._\-]*"),
    re.compile(
        r"(?i)([\"']?(?:authorization|api[-_]?key|apikey|x-api-key|password|passwd|secret|"
        r"token|access[-_]?token|refresh[-_]?token|set-cookie|cookie)[\"']?\s*[:=]\s*[\"']?)"
        r"([^\s\"',;}]{3})[^\s\"',;}]*"
    ),
)
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+\-]{2})[A-Za-z0-9._%+\-]*(@[A-Za-z0-9.\-]+)\b")


def scrub(text: str) -> str:
    if not text:
        return text
    out = text
    for rx in _SECRETS:
        out = rx.sub(lambda m: m.group(1) + (m.group(2) if m.lastindex and m.lastindex >= 2 else "") + "...", out)
    out = _EMAIL.sub(lambda m: m.group(1) + "..." + m.group(2), out)
    return out


def _strip(text: str) -> str:
    return _ANSI.sub("", text.replace("\r", ""))


def _short(obj, limit: int = 600) -> str:
    if obj is None:
        return ""
    if isinstance(obj, (bytes, bytearray)):
        try:
            obj = obj.decode("utf-8", "replace")
        except Exception:
            obj = repr(obj)
    elif not isinstance(obj, str):
        obj = str(obj)
    obj = scrub(_strip(obj))
    obj = obj.replace("\n", "\\n")
    if len(obj) > limit:
        obj = obj[:limit] + f"...(+{len(obj) - limit})"
    return obj


class Logger:

    def __init__(self, name: str, path: str, level: int = LEVEL, echo: bool = ECHO):
        self.name = name
        self.path = path
        self.level = level
        self.echo = echo
        self.broken = False
        self._lock = threading.RLock()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception:
            self.broken = True

    def _rotate_if_needed(self) -> None:
        try:
            if not os.path.exists(self.path):
                return
            if os.path.getsize(self.path) < MAX_BYTES:
                return
            for i in range(KEEP - 1, 0, -1):
                src, dst = f"{self.path}.{i}", f"{self.path}.{i + 1}"
                if os.path.exists(src):
                    try:
                        os.replace(src, dst)
                    except OSError:
                        pass
            try:
                os.replace(self.path, self.path + ".1")
            except OSError:
                pass
        except Exception:
            pass

    def _write(self, label: str, msg: str) -> None:
        if not LOG_ON or self.broken:
            return
        ts = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
        line = f"{ts} {label} [{self.name}] {_strip(str(msg))}\n"
        try:
            with self._lock:
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8", errors="replace") as f:
                    f.write(line)
                    f.flush()
        except Exception:
            self.broken = True
        if self.echo:
            try:
                sys.__stderr__.write(line)
                sys.__stderr__.flush()
            except Exception:
                pass

    def _emit(self, name: str, msg: str, args) -> None:
        if _LEVELS.get(name, 20) < self.level:
            return
        if args:
            try:
                msg = msg % args
            except Exception:
                msg = f"{msg} {args!r}"
        self._write(_LABEL.get(name, "INFO "), msg)

    def debug(self, msg: str, *args) -> None:
        self._emit("debug", msg, args)

    def info(self, msg: str, *args) -> None:
        self._emit("info", msg, args)

    def warn(self, msg: str, *args) -> None:
        self._emit("warn", msg, args)

    def warning(self, msg: str, *args) -> None:
        self._emit("warn", msg, args)

    def error(self, msg: str, *args) -> None:
        self._emit("error", msg, args)

    def ok(self, msg: str, *args) -> None:
        self._emit("info", msg, args)

    def http(self, direction: str, target: str, code=None, body=None,
             extra: str = "") -> None:
        bits = [f"{direction} {target}"]
        if code is not None:
            bits.append(f"-> {code}")
        if extra:
            bits.append(extra)
        line = " ".join(bits)
        if body:
            line += " | " + _short(body)
        self._write("INFO ", line)

    def exc(self, msg: str = "", *args) -> None:
        self._exc_line(msg, args, traceback.format_exc())

    def exc_obj(self, exc: BaseException, msg: str = "", *args) -> None:
        self._exc_line(msg, args,
                       "".join(traceback.format_exception(type(exc), exc,
                                                          exc.__traceback__)))

    def _exc_line(self, msg: str, args, tb: str) -> None:
        if msg and args:
            try:
                msg = msg % args
            except Exception:
                msg = f"{msg} {args!r}"
        head = f"{msg}\n" if msg else ""
        prev, self.level = self.level, min(self.level, 10)
        try:
            self._write("ERROR", head + tb.rstrip())
        finally:
            self.level = prev

    def line(self, text: str) -> None:
        self._write("INFO ", text)


def get(name: str, path: str | None = None) -> Logger:
    key = f"{name}|{path or ''}"
    with _LOCK:
        lg = _LOGGERS.get(key)
        if lg is None:
            lg = Logger(name, path or os.path.join(LOG_DIR, f"{name}.log"))
            _LOGGERS[key] = lg
        return lg


def set_level(level: str) -> None:
    global LEVEL
    LEVEL = _LEVELS.get(str(level).strip().lower(), LEVEL)
    with _LOCK:
        for lg in _LOGGERS.values():
            lg.level = LEVEL


class _Tee:

    def __init__(self, stream, logger: Logger, label: str = "INFO "):
        self._stream = stream
        self._logger = logger
        self._label = label
        self._buf = ""

    def write(self, data):
        if not data:
            return 0
        try:
            self._stream.write(data)
        except Exception:
            pass
        try:
            clean = _strip(data)
            self._buf += clean
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self._logger._write(self._label, line)
        except Exception:
            pass
        return len(data)

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass
        try:
            if self._buf.strip():
                self._logger._write(self._label, self._buf)
            self._buf = ""
        except Exception:
            pass

    def __getattr__(self, item):
        return getattr(self._stream, item)

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False


_ORIG_STDOUT = None
_ORIG_STDERR = None


def capture_stdout(logger: Logger, also_stderr: bool = True) -> None:
    global _ORIG_STDOUT, _ORIG_STDERR
    if _ORIG_STDOUT is None:
        _ORIG_STDOUT = sys.stdout
    sys.stdout = _Tee(_ORIG_STDOUT, logger)
    if also_stderr:
        if _ORIG_STDERR is None:
            _ORIG_STDERR = sys.stderr
        sys.stderr = _Tee(_ORIG_STDERR, logger, "ERROR")


def restore_stdout() -> None:
    global _ORIG_STDOUT, _ORIG_STDERR
    if _ORIG_STDOUT is not None:
        sys.stdout = _ORIG_STDOUT
    if _ORIG_STDERR is not None:
        sys.stderr = _ORIG_STDERR


def install_excepthook(logger: Logger, echo: bool = True) -> None:
    def _hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        logger._write("ERROR", "UNCAUGHT\n" + text.rstrip())
        if echo:
            try:
                sys.__stderr__.write(text)
                sys.__stderr__.flush()
            except Exception:
                pass

    sys.excepthook = _hook
    try:
        def _thread_hook(args):
            text = "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                      args.exc_traceback))
            logger._write("ERROR", f"UNCAUGHT in thread {args.thread.name if args.thread else '?'}\n"
                                   + text.rstrip())
            if echo:
                try:
                    sys.__stderr__.write(text)
                    sys.__stderr__.flush()
                except Exception:
                    pass
        threading.excepthook = _thread_hook
    except AttributeError:
        pass


def note_startup(logger: Logger, **fields) -> None:
    bits = [f"{k}={v}" for k, v in fields.items()]
    logger.info("--- %s start | py=%s | %s ---",
                logger.name, ".".join(str(x) for x in sys.version_info[:3]),
                " ".join(bits))


if __name__ == "__main__":
    lg = get("self-test")
    note_startup(lg, hi="there")
    lg.info("plain line")
    lg.http(">>", "https://api.example/v1/chat", 429,
            '{"error":{"message":"rate limited","api_key":"sk-abcdef123456"}}')
    lg.debug("hidden at info level")
    try:
        raise ValueError("boom")
    except ValueError:
        lg.exc("demo failure")
    print(f"wrote {lg.path}")
