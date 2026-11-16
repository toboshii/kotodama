"""File helpers that behave the same on Linux and Windows."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

REPLACE_ATTEMPTS = 10
REPLACE_DELAY = 0.1


def replace_file(src: str | Path, dst: str | Path, *, attempts: int = REPLACE_ATTEMPTS,
                 delay: float = REPLACE_DELAY, sleep: Callable[[float], None] = time.sleep) -> None:
    """os.replace, retried for about a second on PermissionError.

    On Windows the rename fails for a moment while another process holds
    either file: an antivirus scanner checking the new file, or a player
    reading the one being replaced. On Linux this is a plain os.replace
    unless the permissions are really wrong, and then it raises as before.
    """
    for attempt in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt >= attempts - 1:
                raise
            sleep(delay)
