"""LAN discovery of adb-over-TCP devices (§2.3, relaxed for a test bench).

plan.md §2.3 specifies EXPLICIT device->NUC assignment, because two NUCs
connecting to one stick doubles device load and corrupts every cumulative
counter (each collector keeps its own in-memory total and both publish to the
same series, so rate() reads the interleaving as repeated counter resets).

Discovery deliberately relaxes that for a single-NUC office test bench, where
the operator wants every device on the wifi collected without editing YAML for
each one. Two things keep it honest:

  * it is OFF unless `discovery.enabled` is true, so copying a config to a
    second NUC does not silently start a second collector adopting everything;
  * `skip_serials` is an escape hatch for anything on the LAN that should be
    left alone.

Adopted devices are treated exactly like configured ones: `adb root`, log
buffer remediation, the detached recorder. That MUTATES the device — fine for
a bench you own, which is why this is opt-in.

Discovery also fixes the problem that made a 13 h outage invisible on
2026-07-29: a configured `host:` is a DHCP lease, not an identity. When a
known serial turns up at a new address we relocate the running supervisor
rather than retrying a dead IP forever.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging

from .adb import Adb

log = logging.getLogger(__name__)

# Cap concurrent TCP probes. A /24 is 254 hosts; without a cap we would open
# every socket at once, which on a busy NUC hits the fd limit and looks like a
# network fault.
_PROBE_CONCURRENCY = 64


async def _port_open(host: str, port: int, timeout: float) -> str | None:
    """Cheap liveness probe: a completed TCP handshake, nothing more.

    Deliberately not `adb connect` for the sweep — that spawns a process per
    host and mutates adb's device list for 254 addresses, most of which are
    printers and laptops.
    """
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return f"{host}:{port}"


async def sweep(subnet: str, port: int = 5555, timeout: float = 0.6) -> list[str]:
    """Return `host:port` for every address on `subnet` accepting TCP on `port`."""
    try:
        net = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        log.error("discovery: %r is not a valid subnet — discovery disabled", subnet)
        return []

    sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def probe(ip):
        async with sem:
            return await _port_open(str(ip), port, timeout)

    hosts = list(net.hosts())
    if len(hosts) > 1024:
        log.warning("discovery: %s has %d hosts — sweeping only the first 1024",
                    subnet, len(hosts))
        hosts = hosts[:1024]

    results = await asyncio.gather(*(probe(ip) for ip in hosts))
    return [r for r in results if r]


async def identify(adb: Adb, address: str) -> str | None:
    """adb-connect and read ro.serialno. None if it is not an adb device.

    Anything can listen on 5555. Only a device that completes the adb
    handshake AND reports a serial is adopted.
    """
    if not await adb.connect(address):
        return None
    try:
        serial = (await adb.shell(address, "getprop", "ro.serialno")).strip()
    except Exception:
        log.debug("discovery: %s completed TCP but did not answer getprop", address)
        return None
    if not serial:
        # unauthorized devices connect but answer nothing useful
        await adb.disconnect(address)
        return None
    return serial


async def scan(adb: Adb, subnet: str, port: int = 5555) -> dict[str, str]:
    """One full pass. Returns {serial: address}."""
    addresses = await sweep(subnet, port)
    log.debug("discovery: %d host(s) with %d open on %s", len(addresses), port, subnet)

    found: dict[str, str] = {}
    for address in addresses:
        serial = await identify(adb, address)
        if serial:
            if serial in found:
                # two addresses answering for one serial: a stale adb entry for
                # a previous lease. Last one wins; the old address will fail to
                # connect and be dropped on the next pass.
                log.warning("discovery: serial %s seen at both %s and %s",
                            serial, found[serial], address)
            found[serial] = address
    return found
