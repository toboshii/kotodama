"""Local UDP endpoint for the Windower addon."""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any

from . import protocol
from .config import Config
from .engine import Engine

log = logging.getLogger("kotodama.net")


class UDPServer:
    def __init__(self, cfg: Config, engine: Engine):
        self.cfg = cfg
        self.engine = engine
        host = str(cfg.get("listen.host", "127.0.0.1"))
        port = int(cfg.get("listen.port", 47123))
        # The addon is the only intended client; never listen beyond loopback.
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"listen.host must be a loopback address, got {host!r}")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        # Done here, not in serve_forever. A Q or R typed while the sidecar
        # starts is acted on (from the key thread) as soon as the key reader
        # starts, so shutdown() can close the socket before the loop runs.
        self.sock.settimeout(0.5)
        self.bound: tuple[str, int] = self.sock.getsockname()[:2]
        self.addr: tuple[str, int] | None = None
        self._stop = threading.Event()
        engine.reply = self.send

    @property
    def port(self) -> int:
        return self.bound[1]

    def send(self, msg: dict[str, Any]) -> None:
        if self.addr is None:
            return
        try:
            self.sock.sendto(protocol.encode(msg), self.addr)
        except OSError as exc:
            log.debug("send failed: %s", exc)

    def serve_forever(self) -> None:
        # Nothing touches the socket before the loop, which handles a closed one.
        log.info("listening on %s:%d", *self.bound)
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(protocol.MAX_DATAGRAM + 1)
            except socket.timeout:
                continue
            except ConnectionResetError:
                # Windows reports an earlier reply that hit a closed port (the
                # addon reloading) as an error on the next receive. Not fatal.
                continue
            except OSError:
                break
            msg = protocol.decode(data)
            if msg is None:
                continue
            self.addr = addr
            self.engine.handle(msg)

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
