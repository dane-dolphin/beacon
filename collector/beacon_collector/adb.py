from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# §10: the old pipeline hardcoded Windows adb.exe paths and split command
# strings on spaces. Here: portable resolution, arguments always as lists.

_CANDIDATE_PATHS = [
    "~/Android/Sdk/platform-tools/adb",
    "~/android-sdk/platform-tools/adb",
    "/opt/android-sdk/platform-tools/adb",
    "/usr/lib/android-sdk/platform-tools/adb",
]


def find_adb() -> str:
    """Resolve the adb binary: $BEACON_ADB, PATH, $ANDROID_HOME, then common spots."""
    env = os.environ.get("BEACON_ADB")
    if env and Path(env).expanduser().is_file():
        return str(Path(env).expanduser())
    on_path = shutil.which("adb")
    if on_path:
        return on_path
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(var)
        if root:
            p = Path(root) / "platform-tools" / "adb"
            if p.is_file():
                return str(p)
    for cand in _CANDIDATE_PATHS:
        p = Path(cand).expanduser()
        if p.is_file():
            return str(p)
    raise FileNotFoundError(
        "adb not found. Set BEACON_ADB, add adb to PATH, or set ANDROID_HOME."
    )


class Adb:
    """Thin async wrapper around one adb binary, addressing devices by serial/address."""

    def __init__(self, adb_path: str | None = None):
        self.adb = adb_path or find_adb()

    async def _run(self, *args: str, timeout: float = 20.0) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            self.adb, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def connect(self, address: str) -> bool:
        rc, out, err = await self._run("connect", address, timeout=10.0)
        ok = "connected" in out  # covers "connected to" and "already connected"
        if not ok:
            log.debug("adb connect %s failed: %s%s", address, out.strip(), err.strip())
        return ok

    async def disconnect(self, address: str) -> None:
        await self._run("disconnect", address, timeout=10.0)

    async def devices(self) -> dict[str, str]:
        """address/serial -> state (device, offline, unauthorized...)."""
        _, out, _ = await self._run("devices")
        result = {}
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                result[parts[0]] = parts[1]
        return result

    async def root(self, address: str) -> bool:
        """adb root; the daemon restarts, so re-verify with a uid check."""
        try:
            await self._run("-s", address, "root", timeout=15.0)
        except asyncio.TimeoutError:
            return False
        await asyncio.sleep(2.0)
        await self.connect(address)  # root restarts adbd over TCP; reattach
        try:
            _, out, _ = await self._run("-s", address, "shell", "id", timeout=10.0)
        except asyncio.TimeoutError:
            return False
        return "uid=0(root)" in out

    async def shell(self, address: str, *cmd: str, timeout: float = 20.0) -> str:
        """Run a shell command; returns stdout. Args are passed as a list, never split."""
        rc, out, err = await self._run("-s", address, "shell", *cmd, timeout=timeout)
        if rc != 0:
            log.debug("adb shell %s rc=%d err=%s", cmd, rc, err.strip())
        return out

    async def push(self, address: str, local: str | Path, remote: str) -> bool:
        rc, _, err = await self._run("-s", address, "push", str(local), remote, timeout=60.0)
        if rc != 0:
            log.warning("adb push %s -> %s failed: %s", local, remote, err.strip())
        return rc == 0

    async def pull(self, address: str, remote: str, local: str | Path) -> bool:
        rc, _, err = await self._run("-s", address, "pull", remote, str(local), timeout=300.0)
        return rc == 0

    def stream(self, address: str, *cmd: str) -> "AdbStream":
        """Long-lived streaming command (logcat, dmesg -w, tail -F ...)."""
        return AdbStream(self.adb, address, list(cmd))


class AdbStream:
    """A long-lived adb subprocess yielding lines. Dies with the connection —
    that is expected (§1.12); the supervisor restarts it on reconnect."""

    def __init__(self, adb_path: str, address: str, cmd: list[str]):
        self.adb_path = adb_path
        self.address = address
        self.cmd = cmd
        self.proc: asyncio.subprocess.Process | None = None

    async def __aenter__(self):
        self.proc = await asyncio.create_subprocess_exec(
            self.adb_path, "-s", self.address, *self.cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def lines(self, idle_timeout: float | None = None):
        """Async iterator of decoded lines until the stream ends.

        idle_timeout: end the iteration (and kill the subprocess) if no line
        arrives for this many seconds. Used for followers of rotated files —
        this device's toybox tail has no -F, so a `tail -f` keeps the old fd
        after rotation and goes silent instead of dying; the caller reopens.
        """
        assert self.proc and self.proc.stdout
        while True:
            try:
                # readline() with a limit guard: logcat lines can be long but bounded
                if idle_timeout is None:
                    line = await self.proc.stdout.readline()
                else:
                    line = await asyncio.wait_for(
                        self.proc.stdout.readline(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                await self.close()
                return
            except (ValueError, asyncio.LimitOverrunError):
                # pathological line longer than the 64K default limit — skip it
                continue
            if not line:
                return
            yield line.decode(errors="replace").rstrip("\r\n")

    async def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            try:
                await self.proc.wait()
            except ProcessLookupError:
                pass
