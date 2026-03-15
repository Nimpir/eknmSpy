"""
main.py — entry point, loads .env and launches GUI
"""
import io
import os
import sys
import logging
import logging.handlers
import threading
from datetime import datetime
from pathlib import Path

# Load .env if present
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

# Configure logging
logs_dir = Path(__file__).parent / "data" / "logs"
logs_dir.mkdir(parents=True, exist_ok=True)

_log_file = logs_dir / f"app_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
_handler = logging.handlers.TimedRotatingFileHandler(
    _log_file,
    when="midnight",
    backupCount=7,
    encoding="utf-8",
)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

logging.basicConfig(
    level=logging.WARNING,         # default: only warnings+ from third-party libs
    handlers=[
        _handler,
        logging.StreamHandler(),
    ],
)

# Our own code logs at DEBUG+; discord internals stay at WARNING
logging.getLogger("bot").setLevel(logging.DEBUG)
logging.getLogger("gui").setLevel(logging.DEBUG)
logging.getLogger("processor").setLevel(logging.DEBUG)
logging.getLogger("db").setLevel(logging.DEBUG)
logging.getLogger("discord").setLevel(logging.WARNING)

# Route unhandled thread exceptions and stderr into the log
_stderr_log = logging.getLogger("stderr")

class _StderrToLog(io.TextIOBase):
    def write(self, msg):
        msg = msg.rstrip()
        if msg:
            _stderr_log.error("%s", msg)
        return len(msg)
    def flush(self): pass

sys.stderr = _StderrToLog()

def _thread_excepthook(args):
    logging.getLogger("thread").error(
        "Unhandled exception in thread %s", args.thread.name,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )

threading.excepthook = _thread_excepthook

from gui import main

if __name__ == "__main__":
    main()
