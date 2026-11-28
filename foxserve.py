
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WS_FILE = HERE / ".camoufox_ws"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _is_true(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def main() -> None:
    headless = _is_true(os.environ.get("CAMOUFOX_HEADLESS", "0"))

    try:
        WS_FILE.unlink()
    except OSError:
        pass

    from fastfox import FAST_OPTS, STARTUP_PREFS

    opts = dict(FAST_OPTS)
    opts["headless"] = headless
    opts["firefox_user_prefs"] = dict(STARTUP_PREFS)

    opts_path = Path(tempfile.gettempdir()) / "camoufox_server_opts.json"
    opts_path.write_text(json.dumps(opts), encoding="utf-8")

    code = (
        "import json\n"
        "from camoufox.server import launch_server\n"
        f"opts = json.load(open(r'{opts_path}', encoding='utf-8'))\n"
        "launch_server(**opts)\n"
    )

    print(f"[foxserve] launching Camoufox server (headless={headless})...", flush=True)
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    wrote = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if not wrote:
                m = re.search(r"ws://\S+", line)
                if m:
                    ws = m.group(0).rstrip(".,'\"")
                    WS_FILE.write_text(
                        json.dumps({"ws": ws, "headless": headless,
                                    "pid": proc.pid}),
                        encoding="utf-8")
                    wrote = True
                    print(f"[foxserve] ready -> {ws}", flush=True)
        proc.wait()
        print(f"[foxserve] server exited (code {proc.returncode})", flush=True)
    except KeyboardInterrupt:
        print("\n[foxserve] shutting down...", flush=True)
    finally:
        try:
            WS_FILE.unlink()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
