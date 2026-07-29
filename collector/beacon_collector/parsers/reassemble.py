from __future__ import annotations

import re
from dataclasses import dataclass, field

from .logcat import LogcatLine

# §3.2 — React Native pretty-prints one console.log object across 20-30
# physical lines. All lines of one logical event share
# (timestamp, pid, tid, tag); allow a few ms tolerance for events straddling
# a millisecond boundary, and require continuations to be continuation-shaped.

_CONTINUATION = re.compile(
    r"""^(\s+            # indented
        |[\{\}\[\]]      # brace/bracket line
        |\s*['"]?[\w$]+['"]?\s*: # key: value
        )""",
    re.X,
)

_TOLERANCE_S = 0.005


def _is_continuation_shaped(message: str) -> bool:
    return bool(_CONTINUATION.match(message)) or message.rstrip().endswith(("}", "},"))


@dataclass
class LogicalEvent:
    first: LogcatLine
    lines: list[LogcatLine] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(l.message for l in self.lines)

    @property
    def n_lines(self) -> int:
        return len(self.lines)


class Reassembler:
    """Feed parsed lines in arrival order; yields completed LogicalEvents.

    An event closes when a line arrives with a different (pid, tid, tag),
    a timestamp beyond tolerance, or a non-continuation shape. flush()
    closes whatever is open (call on stream end / timeout).
    """

    def __init__(self):
        self._open: LogicalEvent | None = None

    def feed(self, line: LogcatLine) -> list[LogicalEvent]:
        out: list[LogicalEvent] = []
        cur = self._open
        if cur is not None:
            same_stream = (
                line.pid == cur.first.pid
                and line.tid == cur.first.tid
                and line.tag == cur.first.tag
                and abs(line.device_ts - cur.lines[-1].device_ts) <= _TOLERANCE_S
            )
            if same_stream and _is_continuation_shaped(line.message):
                cur.lines.append(line)
                return out
            out.append(cur)
        self._open = LogicalEvent(first=line, lines=[line])
        return out

    def flush(self) -> list[LogicalEvent]:
        out = [self._open] if self._open else []
        self._open = None
        return out
