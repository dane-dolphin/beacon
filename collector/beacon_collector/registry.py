from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# SQLite control plane (§2.4, M0). All timestamps are stored as REAL unix
# epoch seconds — never formatted strings, never year-less (§1.11).

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    serial        TEXT PRIMARY KEY,
    address       TEXT NOT NULL,
    nuc           TEXT NOT NULL,
    friendly_name TEXT,
    first_seen    REAL,
    last_seen     REAL
);

-- One row per (device, boot). boot_epoch converts device-monotonic time
-- (dmesg, /proc/uptime) to wall clock for that boot (§1.11).
CREATE TABLE IF NOT EXISTS boots (
    serial      TEXT NOT NULL,
    boot_epoch  REAL NOT NULL,      -- host wall clock at device boot
    boot_reason TEXT,
    clock_offset REAL,              -- host_now - device_now at detection
    detected_at REAL NOT NULL,
    PRIMARY KEY (serial, boot_epoch)
);

-- App identity cross-reference discovered from logs (§12 q5):
-- canonical key is ro.serialno; dId/venueCode/installationId map onto it.
CREATE TABLE IF NOT EXISTS identity_map (
    serial          TEXT PRIMARY KEY,
    d_id            TEXT,
    venue_code      TEXT,
    installation_id TEXT,
    updated_at      REAL
);

-- Collector lifecycle: the incident engine must never blame a device for the
-- collector's own downtime (§6 verdict 4).
CREATE TABLE IF NOT EXISTS collector_lifecycle (
    ts    REAL NOT NULL,
    nuc   TEXT NOT NULL,
    event TEXT NOT NULL             -- start | stop | crash-recovered
);

-- Device onboarding state: log remediation applied/verified (§1.7).
CREATE TABLE IF NOT EXISTS onboarding (
    serial              TEXT PRIMARY KEY,
    logd_applied_at     REAL,
    logd_verified_at    REAL,
    logd_reboot_checked REAL        -- persist.logd.size confirmed across a reboot
);
"""


class Registry:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    # -- devices ------------------------------------------------------------

    def upsert_device(self, serial: str, address: str, nuc: str, friendly_name: str = ""):
        now = time.time()
        self.db.execute(
            """INSERT INTO devices (serial, address, nuc, friendly_name, first_seen, last_seen)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(serial) DO UPDATE SET
                 address=excluded.address, nuc=excluded.nuc,
                 friendly_name=excluded.friendly_name, last_seen=excluded.last_seen""",
            (serial, address, nuc, friendly_name, now, now),
        )
        self.db.commit()

    def touch_device(self, serial: str):
        self.db.execute("UPDATE devices SET last_seen=? WHERE serial=?", (time.time(), serial))
        self.db.commit()

    # -- boots --------------------------------------------------------------

    def record_boot(self, serial: str, boot_epoch: float,
                    boot_reason: str | None, clock_offset: float | None) -> bool:
        """Insert a boot row; returns True if this is a NEW boot (reboot detected).

        boot_epoch jitters by a fraction of a second between recomputations of
        wall_now - uptime for the same boot, so match with tolerance.
        """
        cur = self.db.execute(
            "SELECT boot_epoch FROM boots WHERE serial=? AND ABS(boot_epoch - ?) < 5.0",
            (serial, boot_epoch),
        )
        if cur.fetchone():
            return False
        self.db.execute(
            "INSERT INTO boots (serial, boot_epoch, boot_reason, clock_offset, detected_at)"
            " VALUES (?,?,?,?,?)",
            (serial, boot_epoch, boot_reason, clock_offset, time.time()),
        )
        self.db.commit()
        return True

    def current_boot_epoch(self, serial: str) -> float | None:
        cur = self.db.execute(
            "SELECT boot_epoch FROM boots WHERE serial=? ORDER BY boot_epoch DESC LIMIT 1",
            (serial,),
        )
        row = cur.fetchone()
        return row[0] if row else None

    # -- identity -----------------------------------------------------------

    def update_identity(self, serial: str, d_id: str | None = None,
                        venue_code: str | None = None, installation_id: str | None = None):
        self.db.execute(
            """INSERT INTO identity_map (serial, d_id, venue_code, installation_id, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(serial) DO UPDATE SET
                 d_id=COALESCE(excluded.d_id, identity_map.d_id),
                 venue_code=COALESCE(excluded.venue_code, identity_map.venue_code),
                 installation_id=COALESCE(excluded.installation_id, identity_map.installation_id),
                 updated_at=excluded.updated_at""",
            (serial, d_id, venue_code, installation_id, time.time()),
        )
        self.db.commit()

    # -- lifecycle / onboarding ----------------------------------------------

    def lifecycle(self, nuc: str, event: str):
        self.db.execute(
            "INSERT INTO collector_lifecycle (ts, nuc, event) VALUES (?,?,?)",
            (time.time(), nuc, event),
        )
        self.db.commit()

    def mark_onboarding(self, serial: str, field: str):
        assert field in ("logd_applied_at", "logd_verified_at", "logd_reboot_checked")
        self.db.execute(
            f"""INSERT INTO onboarding (serial, {field}) VALUES (?,?)
                ON CONFLICT(serial) DO UPDATE SET {field}=excluded.{field}""",
            (serial, time.time()),
        )
        self.db.commit()

    def onboarding_state(self, serial: str) -> dict:
        cur = self.db.execute(
            "SELECT logd_applied_at, logd_verified_at, logd_reboot_checked"
            " FROM onboarding WHERE serial=?", (serial,))
        row = cur.fetchone()
        if not row:
            return {}
        return dict(zip(("logd_applied_at", "logd_verified_at", "logd_reboot_checked"), row))
