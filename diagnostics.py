"""Local diagnostics with an allowlist: no tokens, accounts, paths or payloads."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import platform
import re

from extractor import __version__

EVENTS = {"startup", "import.failed", "import.save_failed", "login.failed", "login.cancelled",
          "cooldown.failed", "presence.failed", "store.recovered", "store.recovery_failed", "ui.failed"}


class Diagnostics:
    def __init__(self, folder):
        self.path = os.path.join(folder, "diagnostics.log")
        self.logger = logging.Logger("jwtractor.diagnostics", logging.INFO)
        self.logger.propagate = False
        try:
            os.makedirs(folder, exist_ok=True)
            handler = RotatingFileHandler(self.path, maxBytes=256 * 1024, backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s UTC %(message)s"))
            import time
            handler.formatter.converter = time.gmtime
            self.logger.addHandler(handler)
        except OSError:
            pass

    def record(self, event, exception=None):
        if event not in EVENTS:
            return
        kind = type(exception).__name__ if exception is not None else "none"
        kind = re.sub(r"[^A-Za-z0-9_]", "", kind)[:64]
        # Exception text, traceback locals and arbitrary caller data never enter
        # the log. Backend exceptions can contain credentials or page bodies.
        try:
            self.logger.info("event=%s exception=%s", event, kind)
        except Exception:
            pass

    def report(self):
        rows = [f"JWTractor {__version__}", f"Python {platform.python_version()}", f"Platform {platform.system()}"]
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.read(256 * 1024).splitlines(keepends=True)[-100:]
            # Only our formatted event records are exported, even if someone
            # edits the local log. Arbitrary file contents are never copied.
            pattern = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} UTC event=([a-z.]+) exception=([A-Za-z0-9_]{1,64})\s*"
            for line in lines:
                match = re.fullmatch(pattern, line)
                if match and match[1] in EVENTS:
                    rows.append(line.rstrip())
        except OSError:
            rows.append("Diagnostic log unavailable.")
        return "\n".join(rows)

    def close(self):
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)
