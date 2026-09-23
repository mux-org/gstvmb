"""Display Frames: the 16→8 bit perceptual encode between the Appsink and the encoder.

One :class:`DisplayPump` per Instance. It pulls GRAY16 frames from the display
Appsink, maps them through a fixed transfer curve into GRAY8, and pushes the
result into the Appsrc that heads the H.264 branch. It exists because ADR-0002's
one server-side requirement — *apply the perceptual transfer while the data is
still 16-bit, ahead of the 8-bit conversion* — has no GStreamer element behind
it. ``gamma``, ``videobalance`` and ``glupload`` all refuse ``GRAY16_LE`` with
``not-negotiated``, so the transform happens here, in process, or not at all.

Without it the branch is not merely unstretched, it is blank: ``videoconvert``
reduces GRAY16_LE as though the words were full-range 16-bit (``luma = word >>
8``), and this Device right-aligns ten significant bits, so a *saturated* pixel
reaches the encoder as luma 3 of 255.

The pump is deliberately lossy and deliberately subordinate:

* **It may drop frames; the Raw path may not.** The display Appsink runs
  ``drop=true`` and the pump never blocks the Pipeline, so a stalled RTSP
  connection or a slow encoder costs display frames and nothing else. Raw Frames
  are the ones that cannot be re-taken.
* **It owns no lifecycle.** Like :mod:`app.capture` it never starts or stops the
  Pipeline. It waits for one to appear, follows it through restarts, and goes
  quiet when it goes away.

An Instance whose description has no display Appsink (a streaming-only camera,
where the encoder is fed directly and none of this is needed) is detected on the
first poll and the pump disables itself permanently.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from app.config import CameraConfig
from app.pipeline import (
    AppsinkNotPresent,
    AppsinkTimeout,
    AppsrcNotPresent,
    AppsrcPushError,
    Pipeline,
)

log = logging.getLogger(__name__)

# Element names the pump binds to. Conventional rather than configurable: they
# are two halves of one mechanism, and an Instance that renamed them would have
# to rename them in matched pairs for no gain.
DISPLAY_APPSINK = "display"
DISPLAY_APPSRC = "display_src"

# Pull timeout. Short relative to the Capture's, because this thread has nothing
# to lose by looping: a timeout here just means the Device has not produced a
# frame yet, and re-polling costs one log-free iteration.
PULL_TIMEOUT_S = 5.0

# How long to wait before re-checking for a Pipeline that isn't running yet.
# The UI auto-starts an idle Pipeline, so this is the gap between process boot
# and an operator opening a tab — cheap to wait on, pointless to spin on.
IDLE_POLL_S = 1.0

# Steepness of the `log` curve: out = log1p(k·x) / log1p(k). At k=1000 an input
# at 1% of full scale leaves at ~48% of full output, which is the aggressive
# low-end lift the curve exists to provide.
LOG_GAIN = 1000.0

# The LUT covers the whole 16-bit input domain rather than just the declared
# bit depth, so a word above the declared maximum clamps to white instead of
# indexing out of bounds. That matters because the declared depth is a hardware
# fact taken on trust from config (it is undiscoverable at runtime — see
# app.config.PIXEL_FORMATS), and being wrong about it should dim or brighten the
# picture, never crash the pump.
LUT_SIZE = 1 << 16


def build_lut(bit_depth: int, transfer: str) -> np.ndarray:
    """Return a 65536-entry uint8 lookup table mapping ADU to display luma.

    ``bit_depth`` sets where full scale sits — the significant bits the Device
    puts in each 16-bit word, right-aligned — and ``transfer`` picks the curve
    (see :data:`app.config.DISPLAY_TRANSFERS`). Inputs at or above full scale
    map to 255.

    A table is used rather than per-frame arithmetic because it makes the cost
    of the transform independent of the curve: every option, however
    transcendental, costs one uint8 gather over the frame.
    """
    max_adu = (1 << bit_depth) - 1
    x = np.clip(np.arange(LUT_SIZE, dtype=np.float64) / max_adu, 0.0, 1.0)

    if transfer == "linear":
        y = x
    elif transfer == "sqrt":
        y = np.sqrt(x)
    elif transfer == "log":
        y = np.log1p(LOG_GAIN * x) / np.log1p(LOG_GAIN)
    else:  # pragma: no cover — config validation rejects this first
        raise ValueError(f"unknown display transfer {transfer!r}")

    return np.rint(y * 255.0).astype(np.uint8)


class DisplayPump:
    """Drives the display Appsink → transform → Appsrc loop on its own thread."""

    def __init__(self, pipeline: Pipeline, config: CameraConfig):
        self._pipeline = pipeline
        self._config = config
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._frames_in = 0
        self._frames_out = 0
        self._errors = 0
        self._last_error: str | None = None
        # Monotonic time of the last successful push. The counters only show
        # movement across two polls; this answers "is it flowing now" in one.
        self._last_frame_at: float | None = None
        # Set once the pump establishes it has no work to do. Distinct from an
        # error: a streaming-only Instance is correctly configured and simply
        # has no display branch.
        self._disabled: str | None = None

        bit_depth = config.bit_depth
        if bit_depth is None:
            self._lut = None
            self._disabled = (
                "no pixel_format is declared, so the display transfer has no bit depth "
                "to work from"
            )
        else:
            self._lut = build_lut(bit_depth, config.display_transfer)

    # ------------------------------------------------------------------ status

    def status(self) -> dict:
        """Return a serializable snapshot, served by ``GET /display``."""
        with self._lock:
            last = self._last_frame_at
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "disabled": self._disabled,
                "transfer": self._config.display_transfer,
                "bit_depth": self._config.bit_depth,
                "frames_in": self._frames_in,
                "frames_out": self._frames_out,
                "errors": self._errors,
                "last_error": self._last_error,
                "seconds_since_last_frame": (
                    None if last is None else round(time.monotonic() - last, 3)
                ),
            }

    # ----------------------------------------------------------------- command

    def start(self) -> None:
        """Begin pumping on a worker thread. Idempotent."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._disabled is not None:
                log.warning("display pump not started: %s", self._disabled)
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="display", daemon=True)
            self._thread.start()
            log.info(
                "display pump started: %s transfer over %d-bit input",
                self._config.display_transfer,
                self._config.bit_depth,
            )

    def stop(self) -> None:
        """Ask the pump to finish and wait for it. Idempotent.

        The join is bounded by the pull timeout plus slack: the thread spends
        nearly all its life blocked in a pull, and the pull is what it returns
        from to notice the stop.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=PULL_TIMEOUT_S + 2.0)
            if thread.is_alive():
                log.warning("display pump did not exit within the join timeout")

    # ------------------------------------------------------------------ worker

    def _run(self) -> None:
        """Run :meth:`_step` until stopped, surviving anything it raises.

        Every failure mode here is transient by nature — a Pipeline that has not
        started, one that was restarted underneath us, a Device that went quiet
        — so the loop records and continues rather than exiting. The one
        exception is discovering this Instance has no display branch at all,
        which is permanent and ends the thread.

        The catch-all is deliberate. An exception nobody anticipated used to end
        this thread with nothing but a traceback on stderr, and a dead pump is
        indistinguishable from a quiet camera: the display freezes, the Pipeline
        still reports ``playing``, and nothing in the service says why. Here an
        unexpected error is logged once with its traceback, counted, shown in
        :meth:`status`, and followed by a short back-off so a persistent fault
        cannot spin. ``BaseException`` (interpreter shutdown, ``SystemExit``) is
        still allowed through.
        """
        while not self._stop.is_set():
            try:
                if self._step():
                    return
            except Exception as exc:  # noqa: BLE001 — see docstring
                self._record_error(f"unexpected {type(exc).__name__}: {exc}", exc_info=exc)
                self._stop.wait(IDLE_POLL_S)

    def _step(self) -> bool:
        """Pull one frame, transform it, push it. Returns ``True`` to stop for good.

        Handles the failures it expects by name; anything else propagates to
        :meth:`_run`.
        """
        try:
            pulled = self._pipeline.pull_appsink_sample(
                DISPLAY_APPSINK, timeout_s=PULL_TIMEOUT_S, wait=True
            )
        except AppsinkTimeout:
            return False
        except AppsinkNotPresent:
            return self._handle_missing_appsink()

        if pulled is None:
            return False

        data, caps, meta = pulled
        with self._lock:
            self._frames_in += 1

        try:
            self._forward(data, caps, meta)
        except (AppsrcNotPresent, AppsrcPushError) as exc:
            self._record_error(str(exc))
            self._stop.wait(IDLE_POLL_S)
        except ValueError as exc:
            self._record_error(str(exc))
        return False

    def _forward(self, data: bytes, caps: dict, meta: dict) -> None:
        """Transform one frame and push it, after checking it fits the Appsrc.

        The Appsrc's format is declared in the Pipeline description, not here.
        The pump used to declare it on the first frame and cache that, which
        broke across restarts: a restarted Pipeline has a new Appsrc, the cache
        still said "already declared", and the new element pushed with no caps
        — ``not-negotiated``, intermittently, depending on restart timing. An
        element born with its caps has no such window, and a pump holding no
        state about it cannot go stale.

        What the pump does instead is refuse a frame that does not fit, with a
        message naming both geometries, rather than let a mismatch surface
        downstream as an anonymous negotiation failure.
        """
        width, height = caps.get("width"), caps.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            raise ValueError(f"display appsink caps carry no frame size: {caps.get('caps')!r}")

        expected = width * height * 2
        if len(data) != expected:
            raise ValueError(
                f"display frame is {len(data)} bytes, expected {expected} "
                f"for {width}x{height} GRAY16_LE"
            )

        declared = self._pipeline.get_appsrc_caps(DISPLAY_APPSRC)
        if declared is None:
            raise ValueError(
                f"appsrc {DISPLAY_APPSRC!r} has no caps; declare them in the pipeline "
                f'description, e.g. caps="video/x-raw,format=GRAY8,width={width},'
                f'height={height},framerate=0/1"'
            )
        if (declared.get("width"), declared.get("height")) != (width, height):
            raise ValueError(
                f"camera is producing {width}x{height} but appsrc {DISPLAY_APPSRC!r} "
                f"declares {declared.get('width')}x{declared.get('height')}; "
                "update its caps in the pipeline description"
            )

        frame = np.frombuffer(data, dtype="<u2")
        self._pipeline.push_appsrc_frame(
            DISPLAY_APPSRC,
            self._lut[frame].tobytes(),
            pts=meta.get("pts"),
            dts=meta.get("dts"),
            duration=meta.get("duration"),
        )
        with self._lock:
            self._frames_out += 1
            self._last_frame_at = time.monotonic()

    def _handle_missing_appsink(self) -> bool:
        """Decide whether a missing display Appsink is permanent.

        Returns ``True`` when the pump should stop for good. The Pipeline raises
        the same error for "not running" and "no such element", so they are told
        apart by asking for the Appsink listing: a Pipeline that answers is
        running and genuinely has no display branch.
        """
        try:
            names = self._pipeline.list_appsinks()
        except AppsinkNotPresent:
            self._stop.wait(IDLE_POLL_S)
            return False

        if DISPLAY_APPSINK in names:
            # Racing a restart: it existed a moment ago and does again now.
            self._stop.wait(IDLE_POLL_S)
            return False

        with self._lock:
            self._disabled = (
                f"this pipeline has no appsink named {DISPLAY_APPSINK!r}, "
                "so it has no Display Frame branch to feed"
            )
        log.info("display pump disabled: %s", self._disabled)
        return True

    def _record_error(self, detail: str, *, exc_info: BaseException | None = None) -> None:
        """Count an error and keep it for :meth:`status`, logging only on change.

        A persistent fault recurs once per frame or once per back-off, so logging
        every occurrence would bury the log; the first one, with its traceback
        when there is one, is what a reader needs. ``errors`` still counts them
        all, so "one error an hour" and "one error a frame" stay distinguishable.
        """
        with self._lock:
            self._errors += 1
            if detail != self._last_error:
                log.warning("display pump: %s", detail, exc_info=exc_info)
            self._last_error = detail
