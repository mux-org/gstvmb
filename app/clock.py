"""Wall-clock provenance for captured frames.

Two unrelated-looking things live here because they answer one question: *how
much should anyone trust the timestamps in a capture?*

:func:`clock_state` asks the kernel what it thinks of its own clock. On an
instrument host that cannot reach a time server this is the difference between
a file whose epoch is trustworthy and one whose epoch is unanchored — and the
whole point of ADR-0010 is that the file says which it is, rather than leaving
a future reader to guess.

:class:`ClockReference` converts a GStreamer buffer PTS into UTC. PTS is
pipeline *running time*, which is monotonic and has no relationship to the wall
clock, so the two are tied together by sampling both at the start of a Capture
and carrying the pair in the file's header.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass

# From <sys/timex.h>. STA_UNSYNC is the kernel's own "I am not disciplined"
# flag; TIME_ERROR is the clock state that accompanies it.
STA_UNSYNC = 0x0040
TIME_ERROR = 5

# The kernel grows `maxerror` without bound while unsynchronized but clamps the
# reported value here. A capture that sees exactly this value should read it as
# "unbounded", not as a 16-second bound — hence `error_is_bounded`.
MAXERROR_CEILING_US = 16_000_000

# The FITS convention epoch: MJD 0 is 1858-11-17T00:00:00 UTC, which is
# 40587 days before the Unix epoch.
_MJD_UNIX_EPOCH = 40587.0
_SECONDS_PER_DAY = 86400.0


class _Timex(ctypes.Structure):
    """``struct timex`` as passed to ``adjtimex(2)``.

    Only ``status`` and ``maxerror`` are read. The remaining fields are declared
    so the structure is the size the kernel expects; ``pad`` covers the tail
    reserved fields, which differ between kernel versions but are never read
    here.
    """

    _fields_ = [
        ("modes", ctypes.c_uint),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("constant", ctypes.c_long),
        ("precision", ctypes.c_long),
        ("tolerance", ctypes.c_long),
        ("tv_sec", ctypes.c_long),
        ("tv_usec", ctypes.c_long),
        ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long),
        ("jitter", ctypes.c_long),
        ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long),
        ("jitcnt", ctypes.c_long),
        ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long),
        ("stbcnt", ctypes.c_long),
        ("tai", ctypes.c_int),
        ("pad", ctypes.c_char * 44),
    ]


@dataclass(frozen=True)
class ClockState:
    """The kernel's opinion of its own clock, as written into every capture.

    :param synchronized: ``False`` when the kernel reports ``STA_UNSYNC`` — the
        clock is free-running and its offset from UTC is unknown.
    :param max_error_s: The kernel's estimated error bound, in seconds.
    :param error_is_bounded: ``False`` when ``max_error_s`` is pegged at the
        kernel's ceiling, i.e. the kernel has stopped bounding the error at all.
    :param available: ``False`` on a platform without ``adjtimex`` (macOS during
        local development). The other fields are then meaningless and the file
        records the absence rather than an invented value.
    """

    synchronized: bool
    max_error_s: float
    error_is_bounded: bool
    available: bool = True

    @property
    def trustworthy(self) -> bool:
        """``True`` only when the kernel vouches for the clock with a real bound."""
        return self.available and self.synchronized and self.error_is_bounded


def clock_state() -> ClockState:
    """Read the clock's synchronization state via a read-only ``adjtimex(2)``.

    Calling with ``modes = 0`` is a pure query and needs no privileges. On a
    platform without the call (or without ``libc.so.6``) this reports
    ``available=False`` rather than raising: an unknown clock state must not be
    able to abort a capture, and a file that records "unknown" is still honest.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        timex = _Timex()
        state = libc.adjtimex(ctypes.byref(timex))
    except (OSError, AttributeError):
        return ClockState(
            synchronized=False, max_error_s=0.0, error_is_bounded=False, available=False
        )

    if state < 0:
        return ClockState(
            synchronized=False, max_error_s=0.0, error_is_bounded=False, available=False
        )

    unsynced = bool(timex.status & STA_UNSYNC) or state == TIME_ERROR
    return ClockState(
        synchronized=not unsynced,
        max_error_s=timex.maxerror / 1e6,
        error_is_bounded=timex.maxerror < MAXERROR_CEILING_US,
    )


@dataclass(frozen=True)
class ClockReference:
    """Ties GStreamer running time to UTC, sampled once at the start of a Capture.

    A buffer's PTS is running time — nanoseconds since the pipeline's base time,
    off a monotonic clock with no relation to the wall clock. Absolute pipeline
    clock time for a buffer is therefore ``base_time_ns + pts``, and the offset
    to UTC is fixed by having sampled ``utc_s`` and ``clock_ns`` together.

    Both halves are written into the capture's header so every timestamp in the
    file can be re-derived from the raw PTS if this conversion is ever found to
    be wrong (ADR-0010).

    :param utc_s: Unix epoch seconds at the moment of sampling.
    :param clock_ns: Pipeline clock reading at the same moment, in nanoseconds.
    :param base_time_ns: The pipeline's base time, in nanoseconds.
    """

    utc_s: float
    clock_ns: int
    base_time_ns: int

    def utc_of_pts(self, pts_ns: int) -> float:
        """Return the Unix epoch seconds corresponding to a buffer PTS.

        This is frame *arrival* at the source element. It is not exposure start —
        exposure, readout and variable GigE transport all sit between them, which
        is why the caller subtracts the exposure time and why ADR-0010 insists
        the result is documented as an estimate.
        """
        return self.utc_s + (self.base_time_ns + pts_ns - self.clock_ns) / 1e9


def utc_to_mjd(utc_s: float) -> float:
    """Convert Unix epoch seconds to Modified Julian Date."""
    return _MJD_UNIX_EPOCH + utc_s / _SECONDS_PER_DAY


def utc_to_iso(utc_s: float) -> str:
    """Format Unix epoch seconds as an ISO-8601 UTC string with milliseconds.

    Uses the ``...T...Z`` spelling FITS ``DATE-OBS`` expects rather than
    ``datetime``'s ``+00:00`` suffix.
    """
    whole = int(utc_s)
    millis = int(round((utc_s - whole) * 1000))
    if millis == 1000:  # rounding carried into the next second
        whole += 1
        millis = 0
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole)) + f".{millis:03d}Z"
