#!/usr/bin/env python3
"""Minimal `nc HOST PORT` for the session agent's tcp attach (`docker exec -i <ctr> nc 127.0.0.1 <port>`).

Bridges stdin to the socket and the socket to stdout. Only loopback targets are accepted, so the shim cannot be used to
reach anything else. Replaces installing a netcat package at build time.
"""

from __future__ import annotations

import os
import socket
import sys
import threading

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


def parse_args(argv: list[str]) -> tuple[str, int]:
    args = [a for a in argv if not a.startswith("-")]
    if len(args) != 2 or args[0] not in ALLOWED_HOSTS:
        raise SystemExit("usage: nc 127.0.0.1 PORT (loopback only)")
    try:
        port = int(args[1])
    except ValueError:
        raise SystemExit("bad port")
    if not 1 <= port <= 65535:
        raise SystemExit("bad port")
    return args[0], port


def pump(src_read, dst_send, done: threading.Event, half_close=None) -> None:
    try:
        while True:
            data = src_read(65536)
            if not data:
                break
            dst_send(data)
    except OSError:
        pass
    finally:
        if half_close:
            try:
                half_close()
            except OSError:
                pass
        done.set()


def main() -> int:
    host, port = parse_args(sys.argv[1:])
    sock = socket.create_connection((host, port), timeout=10)
    sock.settimeout(None)
    done = threading.Event()
    stdin, stdout = sys.stdin.buffer.raw if hasattr(sys.stdin.buffer, "raw") else sys.stdin.buffer, sys.stdout.buffer

    def to_stdout(b: bytes) -> None:
        stdout.write(b)
        stdout.flush()

    threading.Thread(target=pump, args=(stdin.read, sock.sendall, done, lambda: sock.shutdown(socket.SHUT_WR)), daemon=True).start()
    threading.Thread(target=pump, args=(sock.recv, to_stdout, done), daemon=True).start()
    done.wait()
    try:
        sock.close()
    finally:
        os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
