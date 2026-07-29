from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class RawSpool:
    """Durable raw capture on the NUC (§2.1 point 3).

    One JSONL file per (serial, source, hour): every raw line with its
    collector receive-time (primary timestamp, always has a year — §1.11).
    These hour files are the input for deterministic Parquet objects (§3.5):
    the same hour re-processed overwrites the same Parquet key, so replays
    and retries never duplicate rows downstream.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._handles: dict[Path, object] = {}

    def path_for(self, serial: str, source: str, ts: float) -> Path:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return self.root / serial / source / f"dt={dt:%Y-%m-%d}" / f"hour={dt:%H}.jsonl"

    def append(self, serial: str, source: str, line: str,
               ts: float | None = None, meta: dict | None = None) -> None:
        ts = ts if ts is not None else time.time()
        path = self.path_for(serial, source, ts)
        fh = self._handles.get(path)
        if fh is None:
            # a new hour began: close old handles for this stream
            for p in [p for p in self._handles if p.parts[-4:-2] == path.parts[-4:-2]]:
                self._handles.pop(p).close()
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a", encoding="utf-8")
            self._handles[path] = fh
        rec = {"ts": ts, "line": line}
        if meta:
            rec.update(meta)
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def flush(self):
        for fh in self._handles.values():
            fh.flush()

    def close(self):
        for fh in self._handles.values():
            fh.close()
        self._handles.clear()


class PendingQueue:
    """Durable retry queue for HTTP shippers (VM, Loki).

    Payloads that fail to send are persisted as numbered files and retried
    oldest-first; a file is deleted only after a successful send. Survives a
    collector restart. VictoriaMetrics is idempotent on resend (§3.5), and
    Loki tolerates duplicate pushes with identical timestamps/lines.
    """

    def __init__(self, dirpath: str | Path, name: str):
        self.dir = Path(dirpath) / "pending" / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self._seq = int(time.time() * 1000)

    def put(self, payload: bytes) -> None:
        self._seq += 1
        tmp = self.dir / f".{self._seq}.tmp"
        tmp.write_bytes(payload)
        os.replace(tmp, self.dir / f"{self._seq}.bin")

    def items(self) -> list[Path]:
        return sorted(p for p in self.dir.iterdir() if p.suffix == ".bin")

    def size(self) -> int:
        return len(self.items())
