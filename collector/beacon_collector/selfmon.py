from __future__ import annotations

import asyncio
import time

from .pipeline.metrics import VMWriter, line
from .registry import Registry

# §2.2: the cloud's unique job is noticing a dead NUC. Each collector pushes
# stick_collector_up{nuc}; a cloud alert fires on its absence. The lifecycle
# log lets the incident engine distinguish "collector restarted" from
# "device went silent" (§6 verdict 4).


async def heartbeat(vm: VMWriter, nuc_id: str, interval: float = 15.0):
    while True:
        vm.enqueue([line("stick_collector_up", {"nuc": nuc_id}, 1, time.time())])
        await asyncio.sleep(interval)


def record_start(registry: Registry, nuc_id: str):
    registry.lifecycle(nuc_id, "start")


def record_stop(registry: Registry, nuc_id: str):
    registry.lifecycle(nuc_id, "stop")
