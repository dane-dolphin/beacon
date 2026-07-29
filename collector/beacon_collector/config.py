from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class DeviceConfig:
    serial: str
    host: str
    port: int = 5555
    nuc: str = ""
    modules: list[str] = field(default_factory=lambda: ["gpu"])
    friendly_name: str = ""

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class Config:
    nuc_id: str
    registry_db: Path
    spool_dir: Path
    parquet_dir: Path
    victoriametrics: str
    loki: str
    s3_bucket: str | None
    s3_region: str
    app_package: str
    devices: dict[str, DeviceConfig]
    vsync_interval: int = 10
    dumpsys_interval: int = 45
    reconnect_backoff_max: int = 60
    heartbeat_stall_threshold: int = 30

    def my_devices(self) -> list[DeviceConfig]:
        """Devices this NUC owns — explicit assignment only (§2.3)."""
        return [d for d in self.devices.values() if d.nuc == self.nuc_id]


def load_config(path: str | os.PathLike) -> Config:
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    base = path.parent.parent  # config/beacon.yaml -> repo root for relative paths

    def _p(rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (base / p).resolve()

    paths = raw.get("paths", {})
    endpoints = raw.get("endpoints", {})
    intervals = raw.get("intervals", {})

    devices = {}
    for serial, d in (raw.get("devices") or {}).items():
        devices[serial] = DeviceConfig(
            serial=serial,
            host=d["host"],
            port=int(d.get("port", 5555)),
            nuc=d.get("nuc", ""),
            modules=list(d.get("modules", ["gpu"])),
            friendly_name=d.get("friendly_name", serial),
        )

    return Config(
        nuc_id=raw["nuc_id"],
        registry_db=_p(paths.get("registry_db", "./var/registry.sqlite3")),
        spool_dir=_p(paths.get("spool_dir", "./var/spool")),
        parquet_dir=_p(paths.get("parquet_dir", "./var/parquet")),
        victoriametrics=endpoints.get("victoriametrics", "http://localhost:8428"),
        loki=endpoints.get("loki", "http://localhost:3100"),
        s3_bucket=endpoints.get("s3_bucket"),
        s3_region=endpoints.get("s3_region", "us-east-1"),
        app_package=raw.get("app_package", "com.dolphin_us.dolphinstore"),
        devices=devices,
        vsync_interval=int(intervals.get("vsync", 10)),
        dumpsys_interval=int(intervals.get("dumpsys", 45)),
        reconnect_backoff_max=int(intervals.get("reconnect_backoff_max", 60)),
        heartbeat_stall_threshold=int(intervals.get("heartbeat_stall_threshold", 30)),
    )
