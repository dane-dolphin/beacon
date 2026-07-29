from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .adb import Adb
from .config import load_config
from .registry import Registry

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "beacon.yaml"

log = logging.getLogger("beacon")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="beacon", description="Dolphin Stick telemetry collector")
    p.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe", help="report which assumed commands work on a device (§M0)")
    sp.add_argument("serial")

    so = sub.add_parser("onboard", help="apply + verify log remediation (§1.7)")
    so.add_argument("serial")

    sub.add_parser("run", help="run the collector for all devices this NUC owns")

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(args.config)

    if args.cmd == "probe":
        return asyncio.run(_probe(cfg, args.serial))
    if args.cmd == "onboard":
        return asyncio.run(_onboard(cfg, args.serial))
    if args.cmd == "run":
        return asyncio.run(_run(cfg))
    return 2


async def _device(cfg, serial):
    dev = cfg.devices.get(serial)
    if not dev:
        print(f"unknown device {serial!r}; known: {sorted(cfg.devices)}", file=sys.stderr)
        raise SystemExit(2)
    adb = Adb()
    if not await adb.connect(dev.address):
        print(f"cannot connect to {dev.address}", file=sys.stderr)
        raise SystemExit(1)
    rooted = await adb.root(dev.address)
    if not rooted:
        print("warning: adb root failed — root-only checks will fail", file=sys.stderr)
    return adb, dev


async def _probe(cfg, serial) -> int:
    from . import onboarding
    adb, dev = await _device(cfg, serial)
    results = await onboarding.probe(adb, dev.address)
    ok = sum(1 for r in results.values() if r["ok"])
    print(json.dumps(results, indent=2))
    print(f"\n{ok}/{len(results)} checks passed", file=sys.stderr)
    return 0 if ok == len(results) else 1


async def _onboard(cfg, serial) -> int:
    from . import onboarding
    adb, dev = await _device(cfg, serial)
    registry = Registry(cfg.registry_db)
    report = await onboarding.onboard(adb, dev.address, dev.serial, registry)
    print(json.dumps(report, indent=2))
    return 0 if report["all_buffers_4mib"] else 1


async def _run(cfg) -> int:
    from . import selfmon
    from .pipeline.metrics import EventCounters, VMWriter
    from .pipeline.spool import RawSpool
    from .supervisor import DeviceSupervisor

    devices = cfg.my_devices()
    if not devices:
        print(f"no devices assigned to NUC {cfg.nuc_id!r} in config", file=sys.stderr)
        return 2

    adb = Adb()
    registry = Registry(cfg.registry_db)
    spool = RawSpool(cfg.spool_dir)
    vm = VMWriter(cfg.victoriametrics, cfg.spool_dir)
    counters = EventCounters(vm)

    from .pipeline.processor import Processor
    processor = Processor(cfg, vm, counters, registry)

    selfmon.record_start(registry, cfg.nuc_id)
    log.info("collector %s starting: %d device(s): %s",
             cfg.nuc_id, len(devices), [d.serial for d in devices])

    sups = [DeviceSupervisor(adb, d, cfg, registry, spool, vm, counters, processor)
            for d in devices]
    tasks = [asyncio.create_task(s.run(), name=f"sup-{s.dev.serial}") for s in sups]
    tasks.append(asyncio.create_task(vm.run(), name="vm-writer"))
    tasks.append(asyncio.create_task(counters.run(), name="counter-publish"))
    tasks.append(asyncio.create_task(selfmon.heartbeat(vm, cfg.nuc_id), name="selfmon"))
    if processor:
        tasks.append(asyncio.create_task(processor.run(), name="processor"))

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        selfmon.record_stop(registry, cfg.nuc_id)
        spool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
