"""Capture: persisting Raw Frames from this Instance's Appsink to the archive.

One :class:`CaptureManager` per Instance, owning at most one running Capture at
a time. That limit is physical, not policy: one Device, one Pipeline, one
Appsink — two concurrent Captures would interleave pulls from the same sink and
corrupt both files. A second start is refused, never queued.

The Capture runs on its own thread and is *commanded*, not awaited: ``start``
returns as soon as the file is open and the preconditions hold, and the caller
learns the outcome by polling :meth:`status`. A blocking call would sit behind
nginx's default 60 s ``proxy_read_timeout`` and 504 while continuing to run
server-side.

Two rules are enforced here:

* **A bounded extent promises completeness.** ``count`` aborts on a detected
  Drop, because "50 frames" and "50 consecutive frames" are different claims and
  only the second is worth keeping. ``vmbsrc`` discards incomplete GigE frames
  silently, so a gap is visible only as a discontinuity in PTS cadence.
* **Nothing is ever auto-deleted.** A Capture whose computed size will not fit
  is refused up front, which is possible precisely because a Count extent is
  bounded. The archive shares a filesystem with the OS, so a runaway Capture is
  a host outage rather than a failed Capture.

This module deliberately does **not** start the Pipeline. ``start`` requires it
already ``playing`` and refuses otherwise, so Pipeline lifecycle keeps a single
owner.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app import __version__
from app.clock import ClockReference, clock_state, utc_to_iso
from app.config import CameraConfig
from app.fitscube import BLOCK, FitsCube, provenance_cards
from app.pipeline import AppsinkNotPresent, AppsinkTimeout, Pipeline

log = logging.getLogger(__name__)

DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data"))

FRAMES_FILENAME = "frames.fits"

# Extents bounded by frame count can promise completeness and can have their
# size computed before the first frame. Only these are implemented; the
# time-bounded extents named in docs/data-capture-widget.md are rejected with a
# message saying so rather than silently doing something else.
BOUNDED_MODES = frozenset({"snapshot", "count"})
UNIMPLEMENTED_MODES = frozenset({"duration", "continuous"})

MAX_COUNT = 100_000

# Free bytes to leave untouched. The archive lives on the root filesystem, so
# this is what stands between a forgotten Capture and a host that cannot boot —
# absolute rather than a percentage, because what matters is whether the OS can
# still function.
RESERVE_BYTES = 20 * 1024**3

# Per-frame pull timeout. Generous: a long exposure legitimately produces frames
# slowly, and a spurious timeout would abort a good Capture.
PULL_TIMEOUT_S = 30.0

# Drop detection. Cadence cannot be read from caps — the Appsink negotiates
# framerate=0/1 because the Device free-runs — so it is learned from the first
# few intervals and a gap is an interval materially longer than that.
CADENCE_WARMUP = 5
DROP_FACTOR = 1.6


def _human(n: float) -> str:
    """Format a byte count for an operator-facing message.

    A capture of a few frames is megabytes; rendering it as "0.00 GB" makes a
    refusal look like a bug rather than an explanation.
    """
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= scale:
            return f"{n / scale:.2f} {unit}"
    return f"{n:.0f} B"


_LABEL_ALLOWED_RE = re.compile(r"[^A-Za-z0-9._-]+")
_CAPTURE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class CaptureBusy(RuntimeError):
    """Raised when a Capture is already running on this Instance."""


class CaptureNotReady(RuntimeError):
    """Raised when the Instance cannot currently capture — the Pipeline is not
    playing, no pixel format is declared, the archive is not mounted, or there
    is not enough free space."""


class CaptureRejected(ValueError):
    """Raised when the request itself is malformed or names an unimplemented
    extent."""


@dataclass(frozen=True)
class Extent:
    """What bounds a Capture.

    :param mode: ``snapshot`` (one frame) or ``count`` (``value`` frames).
    :param value: Frame count; ignored for ``snapshot``.
    """

    mode: str
    value: int = 1

    @property
    def frames(self) -> int:
        """The number of frames this extent asks for."""
        return 1 if self.mode == "snapshot" else self.value

    @classmethod
    def parse(cls, mode: object, value: object) -> "Extent":
        """Validate a requested extent.

        Unimplemented modes are named explicitly rather than lumped in with
        typos, so a client built against docs/data-capture-widget.md gets told
        which of the four extents exist today.
        """
        if not isinstance(mode, str):
            raise CaptureRejected("extent.mode must be a string")
        mode = mode.strip().casefold()
        if mode in UNIMPLEMENTED_MODES:
            raise CaptureRejected(
                f"extent mode {mode!r} is not implemented yet; "
                f"supported: {', '.join(sorted(BOUNDED_MODES))}"
            )
        if mode not in BOUNDED_MODES:
            raise CaptureRejected(
                f"unknown extent mode {mode!r}; expected one of "
                f"{', '.join(sorted(BOUNDED_MODES | UNIMPLEMENTED_MODES))}"
            )
        if mode == "snapshot":
            return cls(mode="snapshot", value=1)
        if not isinstance(value, int) or isinstance(value, bool):
            raise CaptureRejected("extent.value must be an integer for mode 'count'")
        if not 1 <= value <= MAX_COUNT:
            raise CaptureRejected(f"extent.value must be between 1 and {MAX_COUNT}")
        return cls(mode="count", value=value)


def sanitize_label(label: str | None) -> str | None:
    """Reduce an operator label to characters safe in a path component.

    Returns ``None`` for an absent label or one that sanitizes to nothing — the
    absence of a label is preserved as itself rather than replaced by a
    placeholder, so a directory named only for its timestamp says plainly that
    nobody named it.
    """
    if label is None:
        return None
    cleaned = _LABEL_ALLOWED_RE.sub("_", label.strip()).strip("._-")[:40]
    return cleaned or None


def default_capture_id(started_utc_s: float, label: str | None) -> str:
    """Generate a leaf directory name: a UTC timestamp plus the optional label.

    The timestamp is server-generated and always leads, so two Captures with the
    same label cannot collide and ``ls`` sorts chronologically.
    """
    stamp = time.strftime("%H%M%S", time.gmtime(started_utc_s)) + "Z"
    return f"{stamp}_{label}" if label else stamp


@dataclass
class CaptureState:
    """Snapshot of the current or most recent Capture, mirroring PipelineStatus.

    ``state`` distinguishes *why* a Capture is not running, which is what a UI
    needs: ``complete`` and ``aborted`` and ``error`` all mean "not running" but
    demand different things of the operator.
    """

    state: str = "idle"
    detail: str | None = None
    capture_id: str | None = None
    label: str | None = None
    path: str | None = None
    requested: int | None = None
    written: int = 0
    started: str | None = None
    finished: str | None = None


class CaptureManager:
    """Owns this Instance's one-at-a-time Capture."""

    def __init__(self, pipeline: Pipeline, config: CameraConfig, root: Path = DATA_ROOT):
        self._pipeline = pipeline
        self._config = config
        self._root = root
        self._lock = threading.Lock()
        self._state = CaptureState()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ status

    def status(self) -> CaptureState:
        """Return a copy of the current state, safe to serialize."""
        with self._lock:
            return CaptureState(**vars(self._state))

    @property
    def is_running(self) -> bool:
        return self._state.state == "capturing"

    # ----------------------------------------------------------------- command

    def start(
        self,
        extent: Extent,
        *,
        label: str | None = None,
        capture_id: str | None = None,
        appsink: str | None = None,
    ) -> CaptureState:
        """Validate, open the file, and begin capturing on a worker thread.

        Every precondition is checked *before* the thread starts, so a refusal is
        reported synchronously with a specific reason rather than appearing as an
        ``error`` state on a later poll.
        """
        with self._lock:
            if self._state.state == "capturing":
                raise CaptureBusy(
                    f"a capture is already running ({self._state.written} frames written)"
                )

            label = sanitize_label(label)
            capture_id = self._resolve_capture_id(capture_id, label)
            sink = self._resolve_appsink(appsink)
            caps = self._require_caps(sink)
            width, height = caps["width"], caps["height"]
            frame_bytes = width * height * 2

            self._require_pixel_format()
            self._require_space(extent.frames, frame_bytes)

            started_s = time.time()
            directory = self._capture_dir(started_s, capture_id)
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise CaptureNotReady(f"cannot create capture directory {directory}: {exc}")

            # Discard the Appsink's backlog so frame 0 is genuinely the next
            # frame off the Device. The Appsink is a FIFO and the Pipeline may
            # have been playing for hours: without this a Capture returns the
            # OLDEST queued frames, so "capture 10 frames" silently means
            # "recover 10 frames from whenever the pipeline started" — with
            # timestamps that are honest about arrival and completely wrong
            # about what the operator asked for.
            stale = self._pipeline.drain_appsink(sink)
            if stale:
                log.info("discarded %d stale frame(s) before capture", stale)

            reference = self._clock_reference()
            exposure, gain = self._read_controls()
            cards = provenance_cards(
                camera_id=self._config.id,
                label=self._config.label,
                device=self._pipeline.device,
                pixel_format=self._config.pixel_format,
                bit_depth=self._config.bit_depth,
                caps=caps.get("caps", ""),
                exposure_us=exposure,
                gain=gain,
                first_utc_s=started_s,
                reference=reference,
                state=clock_state(),
                capture_id=capture_id,
                capture_label=label,
                version=__version__,
            )

            path = directory / FRAMES_FILENAME
            try:
                cube = FitsCube(
                    path,
                    width=width,
                    height=height,
                    expected=extent.frames,
                    header_cards=cards,
                )
            except OSError as exc:
                raise CaptureNotReady(f"cannot open {path} for writing: {exc}")

            self._stop.clear()
            self._state = CaptureState(
                state="capturing",
                capture_id=capture_id,
                label=label,
                path=str(path.relative_to(self._root)),
                requested=extent.frames,
                written=0,
                started=utc_to_iso(started_s),
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(cube, sink, extent.frames, reference, exposure),
                name="capture",
                daemon=True,
            )
            self._thread.start()
            return CaptureState(**vars(self._state))

    def stop(self) -> CaptureState:
        """Ask a running Capture to finish early. Idempotent."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=PULL_TIMEOUT_S + 5.0)
        return self.status()

    # ------------------------------------------------------------ preconditions

    def _resolve_capture_id(self, capture_id: str | None, label: str | None) -> str:
        """Validate a supplied capture id, or generate one.

        A supplied id becomes a path component verbatim, so it is checked
        against a strict allowlist rather than sanitized — silently rewriting a
        coordinator's id would split one multi-source Capture across two
        directories, which is the exact failure the shared id exists to prevent.
        """
        if capture_id is None:
            return default_capture_id(time.time(), label)
        capture_id = capture_id.strip()
        if not _CAPTURE_ID_RE.match(capture_id):
            raise CaptureRejected(
                f"capture_id {capture_id!r} must be 1-64 characters of [A-Za-z0-9._-]"
            )
        if capture_id in (".", ".."):
            raise CaptureRejected("capture_id must not be '.' or '..'")
        return capture_id

    def _resolve_appsink(self, name: str | None) -> str:
        """Return the Appsink to pull from, defaulting to the only one present."""
        sinks = self._pipeline.list_appsinks()
        if name is not None:
            if name not in sinks:
                raise CaptureNotReady(
                    f"no appsink named {name!r}; present: {', '.join(sorted(sinks)) or 'none'}"
                )
            return name
        if not sinks:
            raise CaptureNotReady(
                "pipeline contains no appsink; a capture needs one (see the raw "
                "pipeline in config.example.yaml)"
            )
        if len(sinks) > 1:
            raise CaptureRejected(
                f"pipeline has several appsinks ({', '.join(sorted(sinks))}); name one"
            )
        return next(iter(sinks))

    def _require_caps(self, sink: str) -> dict:
        """Return negotiated caps, or explain why the Capture cannot size itself."""
        caps = self._pipeline.get_appsink_caps(sink)
        if not caps or "width" not in caps or "height" not in caps:
            raise CaptureNotReady(
                f"appsink {sink!r} has not negotiated caps yet; the pipeline must be "
                "playing and producing frames before a capture can start"
            )
        return caps

    def _require_pixel_format(self) -> None:
        if self._config.pixel_format is None:
            raise CaptureNotReady(
                "config field 'pixel_format' is not declared, so saved frames could not "
                "be interpreted (Mono10/12/14/16 are indistinguishable from caps); "
                "declare it and restart the container"
            )

    def _require_space(self, frames: int, frame_bytes: int) -> None:
        """Refuse a Capture whose computed size will not fit above the reserve."""
        if not self._root.is_dir():
            raise CaptureNotReady(
                f"archive root {self._root} does not exist — the data volume is not "
                "mounted, and frames would be written to the container's ephemeral "
                "storage and lost on restart"
            )
        if not os.access(self._root, os.W_OK):
            raise CaptureNotReady(f"archive root {self._root} is not writable")

        needed = frames * frame_bytes + BLOCK * 4
        free = shutil.disk_usage(self._root).free
        if free - needed < RESERVE_BYTES:
            raise CaptureNotReady(
                f"insufficient space: {frames} frame(s) need {_human(needed)}, "
                f"{_human(free)} free, and {_human(RESERVE_BYTES)} must stay free "
                "(the archive shares a filesystem with the OS)"
            )

    def _capture_dir(self, started_s: float, capture_id: str) -> Path:
        """``<root>/<YYYYMMDD>/<capture id>/<camera id>/``."""
        date = time.strftime("%Y%m%d", time.gmtime(started_s))
        return self._root / date / capture_id / self._config.id

    def _clock_reference(self) -> ClockReference:
        return self._pipeline.clock_reference()

    def _read_controls(self) -> tuple[float | None, float | None]:
        """Read exposure and gain for the header, tolerating their absence.

        A camera without these properties should still produce a file; the
        header simply omits what could not be read.
        """
        exposure = gain = None
        try:
            exposure = float(self._pipeline.get_vmbsrc_property("exposuretime"))
        except Exception as exc:  # noqa: BLE001 - provenance is best-effort
            log.warning("could not read exposuretime for capture header: %s", exc)
        try:
            gain = float(self._pipeline.get_vmbsrc_property("gain"))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read gain for capture header: %s", exc)
        return exposure, gain

    # ---------------------------------------------------------------- the loop

    def _run(
        self,
        cube: FitsCube,
        sink: str,
        frames: int,
        reference: ClockReference,
        exposure_us: float | None,
    ) -> None:
        """Pull and write frames until done, stopped, or broken.

        Runs on the capture thread. Terminal state is always recorded, and the
        file is always closed — a partial cube with a correct ``NAXIS3`` is
        recoverable, a file left open is not.
        """
        detector = _CadenceDetector()
        final, detail = "complete", None
        try:
            for _ in range(frames):
                if self._stop.is_set():
                    final, detail = "aborted", f"stopped by operator after {cube.written} frames"
                    break
                try:
                    data, _caps, meta = self._pipeline.pull_appsink_sample(
                        sink, timeout_s=PULL_TIMEOUT_S, wait=True
                    )
                except AppsinkTimeout as exc:
                    final, detail = "error", str(exc)
                    break
                except AppsinkNotPresent as exc:
                    final, detail = "error", f"pipeline stopped mid-capture: {exc}"
                    break

                pts = meta.get("pts")
                dropped = detector.observe(pts)
                if dropped:
                    # A bounded extent promised N consecutive frames and can no
                    # longer deliver. Abort rather than hand back a file with a
                    # silent hole; the partial cube is kept and flagged.
                    final = "error"
                    detail = (
                        f"dropped frame detected before frame {cube.written} "
                        f"(interval {detector.last_interval_ms:.1f} ms vs cadence "
                        f"{detector.cadence_ms:.1f} ms); capture aborted to keep the "
                        "'N consecutive frames' promise"
                    )
                    cube.append(
                        data,
                        pts_ns=pts or 0,
                        utc_s=reference.utc_of_pts(pts or 0),
                        dropped_before=True,
                    )
                    break

                cube.append(
                    data,
                    pts_ns=pts or 0,
                    utc_s=reference.utc_of_pts(pts or 0),
                    dropped_before=False,
                )
                with self._lock:
                    self._state.written = cube.written
        except Exception as exc:  # noqa: BLE001 - the thread must not die silently
            log.exception("capture failed")
            final, detail = "error", f"{type(exc).__name__}: {exc}"
        finally:
            try:
                written = cube.close()
            except Exception as exc:  # noqa: BLE001
                log.exception("failed to finalize capture file")
                final, detail = "error", f"failed to finalize file: {exc}"
                written = cube.written
            with self._lock:
                self._state.state = final
                self._state.detail = detail
                self._state.written = written
                self._state.finished = utc_to_iso(time.time())
            log.info("capture %s: %s (%d frames)", self._state.capture_id, final, written)


class _CadenceDetector:
    """Detects Drops from PTS intervals.

    ``vmbsrc`` discards incomplete GigE frames silently, so a missing frame is
    invisible except as an interval materially longer than the established
    cadence. Cadence has to be learned rather than read: the Appsink negotiates
    ``framerate=0/1`` because the Device free-runs.

    The warm-up window is a real blind spot — a Drop among the first few frames
    is indistinguishable from the cadence itself and will not be detected.
    """

    def __init__(self) -> None:
        self._previous: int | None = None
        self._intervals: list[int] = []
        self._cadence: float | None = None
        self.last_interval_ms = 0.0

    @property
    def cadence_ms(self) -> float:
        return (self._cadence or 0) / 1e6

    def observe(self, pts_ns: int | None) -> bool:
        """Record a frame's PTS; return ``True`` if a gap preceded it."""
        if pts_ns is None:
            return False
        previous, self._previous = self._previous, pts_ns
        if previous is None:
            return False

        interval = pts_ns - previous
        self.last_interval_ms = interval / 1e6
        if interval <= 0:
            return False

        if self._cadence is None:
            self._intervals.append(interval)
            if len(self._intervals) >= CADENCE_WARMUP:
                ordered = sorted(self._intervals)
                self._cadence = ordered[len(ordered) // 2]
            return False

        return interval > self._cadence * DROP_FACTOR
