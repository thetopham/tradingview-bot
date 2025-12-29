"""Diagnostics helpers for dashboard visibility.

Authentication note: By default diagnostics require WEBHOOK_SECRET via
`?secret=` query param or `X-Webhook-Secret` header unless
`DASHBOARD_DIAGNOSTICS_PUBLIC=true` is set in the environment. Keep
secrets out of logs and mask sensitive tokens before returning data.
"""
import logging
import os
import re
from datetime import datetime
from typing import List, Optional

from flask import request

LOG_MASK_PATTERNS = [
    re.compile(r"Authorization:\s*Bearer\s+[^\s]+", re.IGNORECASE),
    re.compile(r"(apiKey|token|secret|webhook|bearer)[^\n\r]{0,40}", re.IGNORECASE),
    re.compile(r"(PROJECTX_API_KEY|SUPABASE_KEY|WEBHOOK_SECRET)\s*=\s*[^\s]+", re.IGNORECASE),
    re.compile(r"[A-Fa-f0-9]{24,}"),
]


def mask_line(line: str) -> str:
    masked = line
    for pattern in LOG_MASK_PATTERNS:
        masked = pattern.sub("***", masked)
    return masked


def tail_file(path: str, max_lines: int) -> List[str]:
    """Return the last `max_lines` lines from file efficiently."""
    if max_lines <= 0:
        return []

    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            block_size = 4096
            buffer = b""
            lines: List[bytes] = []
            pos = end
            while pos > 0 and len(lines) <= max_lines:
                read_size = block_size if pos - block_size > 0 else pos
                pos -= read_size
                f.seek(pos)
                buffer = f.read(read_size) + buffer
                lines = buffer.splitlines()
                if len(lines) > max_lines:
                    break
            selected = lines[-max_lines:]
            return [mask_line(l.decode(errors="replace")) for l in selected]
    except FileNotFoundError:
        logging.warning("Diagnostics tail missing file: %s", path)
        return []
    except Exception as exc:
        logging.error("Diagnostics tail failed for %s: %s", path, exc)
        return []


def find_rotated_logs(base_path: str) -> List[str]:
    """Find rotated log files alongside base_path."""
    directory, filename = os.path.split(base_path)
    if not directory:
        directory = "."
    rotated = []
    try:
        for entry in sorted(os.listdir(directory)):
            if entry.startswith(filename) and entry != filename:
                rotated.append(os.path.join(directory, entry))
    except FileNotFoundError:
        return []
    return rotated


def get_log_tail(log_file: str, max_lines: int) -> dict:
    candidates = [log_file] + find_rotated_logs(log_file)
    lines_collected: List[str] = []
    used_path: Optional[str] = None
    last_modified: Optional[str] = None

    for candidate in candidates:
        if not os.path.exists(candidate):
            continue
        used_path = candidate
        lines_collected = tail_file(candidate, max_lines)
        try:
            last_modified = datetime.fromtimestamp(os.path.getmtime(candidate)).isoformat()
        except Exception:
            last_modified = None
        if lines_collected:
            break

    return {
        "path": used_path or log_file,
        "lines": lines_collected,
        "last_modified": last_modified,
        "redacted": True,
    }


def allow_diagnostics(public_flag: bool, webhook_secret: Optional[str]) -> bool:
    if public_flag:
        return True
    provided = request.args.get("secret") or request.headers.get("X-Webhook-Secret")
    remote = request.remote_addr or ""
    if provided and webhook_secret and provided == webhook_secret:
        return True
    return remote in {"127.0.0.1", "::1"}
