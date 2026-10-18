"""Local network and process guard for the Agent Worker (D9).

LIMITATION: this is an in-process protection layer based on Python audit hooks. It is not
infrastructure-level isolation and not a full replacement for it. Code that bypasses the
Python socket layer (native extensions) or other processes on the machine are not covered.
Infrastructure enforcement is still required before any real deployment.

What it does once installed (it cannot be removed afterwards):
- Name resolution is allowed only for hosts in infra/network-allowlist.yaml.
- A socket may connect only to an address that was resolved for an allowed host.
- Starting processes (subprocess, os.system, exec, spawn, startfile) is blocked.
"""

import socket
import sys
from pathlib import Path

import yaml

from . import PROJECT_ROOT

ALLOWLIST_FILE = PROJECT_ROOT / "infra" / "network-allowlist.yaml"

BLOCKED_PROCESS_EVENTS = {
    "subprocess.Popen",
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "os.spawn",
    "os.startfile",
}


class NetworkBlocked(PermissionError):
    pass


class Guard:
    def __init__(self, allowed: set[tuple[str, int]]):
        self.allowed = {(h.lower(), int(p)) for h, p in allowed}
        self.resolved_ips: set[str] = set()
        self.blocked: list[str] = []

    def host_allowed(self, host, port) -> bool:
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        try:
            port = int(port) if port is not None else None
        except (TypeError, ValueError):
            port = None
        return any(str(host).lower() == h and (port is None or port == p) for h, p in self.allowed)

    def connect_allowed(self, address) -> bool:
        return isinstance(address, tuple) and len(address) >= 2 and address[0] in self.resolved_ips

    def _block(self, message: str):
        self.blocked.append(message)
        raise NetworkBlocked(f"blocked by local network guard (D9): {message}")

    def hook(self, event: str, args) -> None:
        if event == "socket.getaddrinfo":
            if not self.host_allowed(args[0], args[1]):
                self._block(f"resolve {args[0]!r}:{args[1]!r}")
        elif event == "socket.connect":
            if not self.connect_allowed(args[1]):
                self._block(f"connect {args[1]!r}")
        elif event in BLOCKED_PROCESS_EVENTS:
            self._block(f"process start ({event})")


_installed: Guard | None = None


def load_allowlist(path: Path = ALLOWLIST_FILE) -> set[tuple[str, int]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {(e["host"], e["port"]) for e in data.get("allowed", [])}


def install(allowed: set[tuple[str, int]] | None = None) -> Guard:
    """Install the guard in this process. Idempotent; cannot be uninstalled."""
    global _installed
    if _installed is not None:
        return _installed
    guard = Guard(load_allowlist() if allowed is None else allowed)
    original = socket.getaddrinfo

    def recording_getaddrinfo(host, port, *a, **kw):
        result = original(host, port, *a, **kw)  # the audit hook has already approved the host
        for *_, sockaddr in result:
            guard.resolved_ips.add(sockaddr[0])
        return result

    socket.getaddrinfo = recording_getaddrinfo
    sys.addaudithook(guard.hook)
    _installed = guard
    return guard


def installed() -> Guard | None:
    return _installed
