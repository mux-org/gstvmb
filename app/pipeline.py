import logging
import re
import threading
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
gi.require_version("GstApp", "1.0")
from gi.repository import GLib, GObject, Gst  # noqa: E402

# Importing GstApp for its side effect: it binds the AppSink action methods
# (try_pull_sample, pull_sample) onto GstAppSink elements returned from the
# pipeline. Without it those elements carry only base Gst.Element methods and
# the pull raises AttributeError: 'GstAppSink' object has no attribute
# 'try_pull_sample'.
from gi.repository import GstApp  # noqa: E402,F401

from app.clock import ClockReference  # noqa: E402

Gst.init(None)

log = logging.getLogger(__name__)

VMBSRC_FACTORY = "vmbsrc"
APPSINK_FACTORY = "appsink"
APPSRC_FACTORY = "appsrc"

# Matches the vmbsrc ``camera=<device id>`` assignment in a gst-launch
# description so the bound Device id can be reported without a running
# element. Anchored on a word boundary so it doesn't match e.g. a
# hypothetical ``othercamera=`` property.
_CAMERA_PROP_RE = re.compile(r"(?:^|\s)camera=(\S+)")

# Default bound on an appsink's queue, applied whenever a description leaves it
# unlimited. An appsink defaults to max-buffers=0 — UNLIMITED — and with
# sync=false it accepts frames as fast as the device produces them, so a playing
# pipeline nobody is draining grows without limit: at 1456x1088 uint16 that is
# ~3.2 MB per frame, ~95 MB/s at 30 fps, until the container is OOM-killed.
#
# 32 frames is ~101 MB: enough slack to absorb write jitter, small enough to
# notice. With drop=false (the appsink default) a full queue does not discard
# silently, and frames lost upstream of it show up as PTS gaps — which is
# exactly what the capture's drop detector looks for.
#
# vmbsrc is ZERO-COPY: each buffer is one of the Device's `framebuffers`, handed
# back to the camera only when released. So a full appsink does not so much
# backpressure vmbsrc as hold its frames, and a description whose `framebuffers`
# does not exceed this bound (plus any other downstream retention) stops the
# Device acquiring whenever nothing drains the sink. That is a property of the
# description, not something this module can enforce — see
# docs/raw-frame-capture.md, Known gaps item 12.
APPSINK_MAX_BUFFERS = 32

# Ceiling on a single drain, so draining can never livelock against a camera
# producing faster than we discard.
DRAIN_LIMIT = 10_000

# How long to wait for a pipeline to actually reach NULL, and for its MainLoop
# thread to exit, during teardown. Bounded so a wedged element cannot hang an
# API request forever, but long enough that the normal case always completes.
TEARDOWN_TIMEOUT_S = 5.0

# The vmbsrc property surface is exposed through hand-written ``/camera`` routes
# (one per control) rather than a generic allowlist. Properties without a route
# are simply unreachable over HTTP, which doubles as the safety boundary that
# keeps advanced knobs (trigger config, etc.) off the API.


class VmbSrcNotPresent(RuntimeError):
    """Raised when a vmbsrc property is accessed but the pipeline has no
    vmbsrc element (either because it hasn't been started, or its description
    does not contain one)."""


class AppsinkNotPresent(RuntimeError):
    """Raised when an appsink operation targets a name that isn't bound to an
    ``appsink`` element in the running pipeline (or the pipeline isn't running)."""


class AppsinkTimeout(RuntimeError):
    """Raised when a blocking sample pull from an appsink times out."""


class AppsrcNotPresent(RuntimeError):
    """Raised when an appsrc operation targets a name that isn't bound to an
    ``appsrc`` element in the running pipeline (or the pipeline isn't running)."""


class AppsrcPushError(RuntimeError):
    """Raised when an appsrc rejects a pushed buffer with a hard flow error.

    A pipeline shutting down answers ``FLUSHING``, which is expected and is not
    raised — only a genuine failure reaches the caller.
    """


class PipelineStartError(RuntimeError):
    """Raised when a pipeline cannot be built or driven to PLAYING.

    Chiefly a malformed gst-launch description — a typo, a property the
    installed plugin does not have, or a caps filter nothing can negotiate.
    That is a *configuration* fault, and the operator driving this from a UI has
    no logs to read, so the reason travels in the exception message and is also
    recorded in :attr:`Pipeline.detail` so ``GET /pipeline`` is self-diagnosing.
    """


def _find_by_factory(pipeline: Gst.Pipeline, factory_name: str) -> Gst.Element | None:
    """Return the first element in ``pipeline`` produced by ``factory_name``, or ``None``."""
    it = pipeline.iterate_recurse()
    while True:
        result, value = it.next()
        if result == Gst.IteratorResult.DONE:
            return None
        if result != Gst.IteratorResult.OK:
            raise RuntimeError(f"failed to iterate pipeline elements: {result}")
        factory = value.get_factory()
        if factory is not None and factory.get_name() == factory_name:
            return value


def parse_device(description: str) -> str | None:
    """Return the Device id pinned in ``description`` as ``vmbsrc camera=...``.

    Returns ``None`` if the description contains no ``camera=`` assignment
    (e.g. a pipeline without vmbsrc, or one that auto-selects a camera). Works
    on the description string alone, so it reports the bound Device whether or
    not the pipeline is running.
    """
    match = _CAMERA_PROP_RE.search(description)
    return match.group(1) if match else None


def _is_enum_pspec(pspec: GObject.ParamSpec) -> bool:
    """Return ``True`` if ``pspec`` describes a GEnum-valued property."""
    return GObject.type_is_a(pspec.value_type, GObject.TYPE_ENUM)


def _find_vmbsrc_pspec(element: Gst.Element, name: str) -> GObject.ParamSpec:
    """Look up a vmbsrc property's :class:`GParamSpec` by name.

    ``name`` always comes from a hand-written endpoint, so absence is not a
    client error — it means the installed plugin doesn't carry a property this
    build expects (a broken/mismatched deploy). Raises :class:`RuntimeError`,
    which surfaces as a 500 rather than masquerading as a 404.
    """
    pspec = element.find_property(name)
    if pspec is None:
        raise RuntimeError(f"vmbsrc element has no property {name!r}")
    return pspec


def _enum_members(pspec: GObject.ParamSpec) -> list:
    """Return the GEnum value objects declared by an enum-typed ``pspec``."""
    return list(pspec.enum_class.__enum_values__.values())


def _enum_int_to_nick(pspec: GObject.ParamSpec, value: int) -> str:
    """Convert an enum integer to its canonical string nick.

    Falls back to ``str(value)`` if ``value`` is not a known member, which
    can happen for an out-of-band default reported by GObject.
    """
    for member in _enum_members(pspec):
        if int(member) == value:
            return member.value_nick
    return str(value)


def _enum_str_to_int(pspec: GObject.ParamSpec, value: str) -> int:
    """Convert a string nick or full name to its enum integer value.

    Matching is case-insensitive and accepts either the short nick
    (``"continuous"``) or the full GObject value name (``"Continuous"``).
    Raises :class:`ValueError` with the list of valid nicks if no match.
    """
    needle = value.casefold()
    for member in _enum_members(pspec):
        if member.value_nick.casefold() == needle or member.value_name.casefold() == needle:
            return int(member)
    valid = sorted({m.value_nick for m in _enum_members(pspec)})
    raise ValueError(f"invalid value {value!r} for {pspec.name}; expected one of {valid}")


def _iter_pipeline_elements(pipeline: Gst.Pipeline):
    """Yield every element in ``pipeline`` (recursively, including bins)."""
    it = pipeline.iterate_recurse()
    while True:
        result, value = it.next()
        if result == Gst.IteratorResult.DONE:
            return
        if result != Gst.IteratorResult.OK:
            raise RuntimeError(f"failed to iterate pipeline elements: {result}")
        yield value


def _find_appsinks(pipeline: Gst.Pipeline) -> dict[str, Gst.Element]:
    """Return all ``appsink`` elements in ``pipeline`` keyed by element name."""
    out: dict[str, Gst.Element] = {}
    for element in _iter_pipeline_elements(pipeline):
        factory = element.get_factory()
        if factory is not None and factory.get_name() == APPSINK_FACTORY:
            out[element.get_name()] = element
    return out


def _flow_name(flow) -> str:
    """Name a ``Gst.FlowReturn`` for a log line without assuming its Python type.

    PyGObject hands back a registered enum here, which carries ``value_nick``,
    but the signal is declared as returning a plain integer and a build that
    takes it at its word would turn a diagnostic into an ``AttributeError`` on
    the push path. The name is a nicety; the push is not.
    """
    return getattr(flow, "value_nick", None) or str(flow)


def _find_appsrcs(pipeline: Gst.Pipeline) -> dict[str, Gst.Element]:
    """Return all ``appsrc`` elements in ``pipeline`` keyed by element name."""
    out: dict[str, Gst.Element] = {}
    for element in _iter_pipeline_elements(pipeline):
        factory = element.get_factory()
        if factory is not None and factory.get_name() == APPSRC_FACTORY:
            out[element.get_name()] = element
    return out


def _caps_to_dict(caps: Gst.Caps | None) -> dict | None:
    """Convert a :class:`Gst.Caps` to a JSON-serializable dict.

    Returns ``None`` if ``caps`` is ``None`` or has no structures (which
    happens before negotiation, i.e. before the pipeline is producing data).
    The result always includes ``"caps"`` (the full caps string, for
    round-trippable consumption) plus a flattened view of the first
    structure's name and fields for convenience.
    """
    if caps is None or caps.get_size() == 0:
        return None
    struct = caps.get_structure(0)
    result: dict = {"caps": caps.to_string(), "media": struct.get_name()}
    for i in range(struct.n_fields()):
        field = struct.nth_field_name(i)
        value = struct.get_value(field)
        if isinstance(value, Gst.Fraction):
            result[field] = f"{value.num}/{value.denom}"
        elif isinstance(value, (int, float, str, bool)) or value is None:
            result[field] = value
        else:
            result[field] = str(value)
    return result


def _bound_appsink_queues(pipeline: Gst.Pipeline) -> None:
    """Replace any unlimited appsink queue with a bounded one.

    ``max-buffers=0`` means unlimited, and it is never a defensible choice for
    this service — an unlimited queue is an unbounded memory leak for as long as
    the pipeline plays. A description that sets its own positive bound is
    honoured; only ``0`` is overridden, so "unlimited" is simply not reachable
    from configuration.
    """
    for name, element in _find_appsinks(pipeline).items():
        if element.get_property("max-buffers") == 0:
            element.set_property("max-buffers", APPSINK_MAX_BUFFERS)
            log.info(
                "appsink %r had an unlimited queue; bounded to %d buffers",
                name,
                APPSINK_MAX_BUFFERS,
            )


def _force_appsrc_time_format(pipeline: Gst.Pipeline) -> None:
    """Put every appsrc in ``format=time``, whatever the description asked for.

    An appsrc defaults to ``format=bytes``, in which segment position is a byte
    offset and the PTS on a pushed buffer is ignored. Everything downstream of
    the display appsrc — the encoder's rate control, RTSP timestamping, and the
    browser's playback clock — is driven by those timestamps, so a description
    that forgets ``format=time`` does not fail loudly: it streams video whose
    frames all claim to arrive at once.

    Overridden rather than validated, on the same reasoning as
    :func:`_bound_appsink_queues`: there is no configuration for which ``bytes``
    is the right answer here, so it is simply not reachable.
    """
    for name, element in _find_appsrcs(pipeline).items():
        if element.get_property("format") != Gst.Format.TIME:
            element.set_property("format", Gst.Format.TIME)
            log.info("appsrc %r was not in format=time; overridden", name)


class Pipeline:
    """Thread-safe wrapper around a GStreamer pipeline.

    Owns a :class:`Gst.Pipeline` parsed from a gst-launch description plus a
    dedicated :class:`GLib.MainLoop` thread that delivers bus messages. Use
    :meth:`start` and :meth:`stop` to drive the lifecycle; both are idempotent
    and safe to call from multiple threads.
    """

    def __init__(self, description: str):
        """Create a pipeline.

        :param description: gst-launch pipeline string, used literally. No
            GStreamer resources are allocated until :meth:`start` is called.
        """
        self._description = description
        self._pipeline: Gst.Pipeline | None = None
        self._loop: GLib.MainLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # Lifecycle state, exposed over the API. Encodes *operator intent*, not
        # just liveness, so a UI can auto-start a fresh deployment while never
        # reversing a deliberate stop:
        #   idle    — never started since process boot
        #   playing — built and PLAYING
        #   stopped — an operator explicitly stopped it (stays off)
        #   error   — start failed, or the bus reported ERROR/EOS
        # The bus callback (``_on_bus_message``) writes these from the MainLoop
        # thread without the lock; they are single-reference assignments (safe
        # under the GIL) and readers tolerate a momentarily stale value.
        self._state: str = "idle"
        self._detail: str | None = None

    def _build(self) -> None:
        """Parse the description and start the MainLoop thread.

        Called from :meth:`start` under the lock. Leaves the pipeline in its
        default (NULL) state — the caller is responsible for transitioning to
        PLAYING.

        A description GStreamer cannot parse raises :class:`PipelineStartError`
        rather than letting a raw ``GLib.Error`` escape as an opaque 500 with
        ``state`` still reporting ``idle``.
        """
        try:
            pipeline = Gst.parse_launch(self._description)
        except GLib.Error as exc:
            raise PipelineStartError(f"invalid pipeline description: {exc.message}") from exc

        _bound_appsink_queues(pipeline)
        _force_appsrc_time_format(pipeline)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        self._pipeline = pipeline

        self._loop = GLib.MainLoop()
        self._loop_thread = threading.Thread(
            target=self._loop.run, name="gst-mainloop", daemon=True
        )
        self._loop_thread.start()

    def _teardown(self) -> None:
        """Set the pipeline to NULL, quit the MainLoop, and drop references.

        The ordering matters and is the fix for a crash that made every failed
        start undiagnosable: the process died (SIGABRT under gunicorn, SIGSEGV
        standalone) instead of reporting why the pipeline would not start, so a
        busy camera looked identical to a dead service.

        Three things are needed, in this order:

        1. **Remove the bus signal watch first.** ``_on_bus_message`` runs on the
           MainLoop thread and touches ``self._pipeline``; a message delivered
           while the pipeline is being dismantled lands on a half-destroyed
           object.
        2. **Wait for NULL to actually be reached.** ``set_state`` is
           asynchronous. Dropping the last reference while elements are still
           shutting down — which is exactly what a failed ``start`` does — is
           what segfaults.
        3. **Join the MainLoop thread.** It is a daemon, so without this the
           interpreter can tear GStreamer down underneath a thread still inside
           it.

        Local references are taken up front so the instance attributes are clear
        even if a step times out.
        """
        pipeline, loop, thread = self._pipeline, self._loop, self._loop_thread
        self._pipeline = None
        self._loop = None
        self._loop_thread = None

        if pipeline is not None:
            bus = pipeline.get_bus()
            bus.remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)
            pipeline.get_state(int(TEARDOWN_TIMEOUT_S * Gst.SECOND))

        if loop is not None and loop.is_running():
            loop.quit()
        # Never join from the MainLoop thread itself — ``_on_bus_message`` runs
        # there, and a future caller reaching teardown from a bus callback would
        # otherwise deadlock on itself.
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=TEARDOWN_TIMEOUT_S)
            if thread.is_alive():
                log.warning("gst MainLoop thread did not exit within %.0fs", TEARDOWN_TIMEOUT_S)

    def _on_bus_message(self, _bus: Gst.Bus, msg: Gst.Message) -> None:
        """Handle bus messages delivered on the MainLoop thread.

        Logs ERROR messages and tears the pipeline down to NULL on either
        ERROR or EOS so it doesn't sit in a half-broken PLAYING state, and
        records the ``error`` state (with a human ``detail``) so ``state``
        reports the truth instead of still claiming ``playing``. The
        ``_pipeline`` reference is deliberately left in place — recovery is the
        next :meth:`start`/:meth:`restart`, which tears down this dead pipeline
        before rebuilding; the loop is not quit from its own thread here. Other
        message types are ignored.
        """
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log.error("gst error: %s (%s)", err, dbg)
            self._state = "error"
            self._detail = str(err)
            if self._pipeline is not None:
                self._pipeline.set_state(Gst.State.NULL)
        elif t == Gst.MessageType.EOS:
            log.info("gst eos")
            self._state = "error"
            self._detail = "end of stream"
            if self._pipeline is not None:
                self._pipeline.set_state(Gst.State.NULL)

    def start(self) -> None:
        """Build the pipeline and transition it to PLAYING.

        No-op if already ``playing``. A prior ``error`` (or any leftover
        pipeline) is torn down first so a failed run can be recovered by
        starting again. On any failure — an unparseable description or a refused
        state change — the pipeline is torn down, ``state`` becomes ``error``
        with the reason in ``detail``, and :class:`PipelineStartError` is raised.
        """
        with self._lock:
            if self._state == "playing":
                return
            # Clear a dead/errored pipeline (and its orphaned MainLoop) before
            # rebuilding; ``_on_bus_message`` leaves the reference in place.
            if self._pipeline is not None:
                self._teardown()
            try:
                self._build()
            except PipelineStartError as exc:
                # Record before re-raising: without this ``state`` would still
                # say ``idle`` ("never started") when the truth is "the config
                # is broken", which is the one thing an operator must not be
                # told wrongly.
                self._teardown()
                self._state = "error"
                self._detail = str(exc)
                raise
            ret = self._pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                self._teardown()
                self._state = "error"
                self._detail = "failed to set pipeline to PLAYING"
                raise PipelineStartError("failed to set pipeline to PLAYING")
            self._state = "playing"
            self._detail = None

    def stop(self) -> None:
        """Tear the pipeline down to NULL and record the deliberate stop.

        Idempotent. Always lands in ``stopped`` (operator intent to stay off),
        which is what suppresses UI auto-start; this is the deliberate
        distinction from ``idle`` (never started).
        """
        with self._lock:
            if self._pipeline is not None:
                self._teardown()
            self._state = "stopped"
            self._detail = None

    def restart(self) -> None:
        """Stop the pipeline (if running) and start it again.

        Equivalent to :meth:`stop` followed by :meth:`start`. Useful for
        re-applying configuration that vmbsrc only reads at state transitions
        (e.g. settings loaded from ``settingsfile``).
        """
        self.stop()
        self.start()

    def is_running(self) -> bool:
        """Return ``True`` only while the pipeline is actually ``playing``.

        Reflects real state, not just "``start`` was called": after a bus
        ERROR/EOS this returns ``False`` even though a stale ``_pipeline``
        reference lingers until the next start/restart.
        """
        return self._state == "playing"

    @property
    def state(self) -> str:
        """Lifecycle state: ``idle`` | ``playing`` | ``stopped`` | ``error``."""
        return self._state

    @property
    def detail(self) -> str | None:
        """Human-readable context for the current state (e.g. the last error), or ``None``."""
        return self._detail

    @property
    def description(self) -> str:
        """The gst-launch description this pipeline was constructed with."""
        return self._description

    @property
    def device(self) -> str | None:
        """The bound Device id, parsed from the description's ``camera=`` (or ``None``)."""
        return parse_device(self._description)

    def clock_reference(self) -> ClockReference:
        """Sample the pipeline clock against the wall clock.

        Buffer PTS is *running time* — monotonic, with no relation to UTC — so
        converting a frame's timestamp to a real instant requires pairing the
        two clocks once. Taken at the start of a Capture and written into the
        file's header so every timestamp can be re-derived from the raw PTS
        later (ADR-0010).

        The two readings are taken as close together as possible; the gap
        between them is the conversion's systematic error, and it is
        microseconds against a wall clock whose own error is seconds.
        """
        with self._lock:
            if self._pipeline is None:
                raise AppsinkNotPresent("pipeline is not running")
            base_time = self._pipeline.get_base_time()
            clock = self._pipeline.get_pipeline_clock()
            if clock is None:
                raise AppsinkNotPresent("pipeline has no clock; is it playing?")
            utc_s = time.time()
            clock_ns = clock.get_time()
        return ClockReference(
            utc_s=utc_s, clock_ns=int(clock_ns), base_time_ns=int(base_time)
        )

    def _require_vmbsrc(self) -> Gst.Element:
        """Return the pipeline's vmbsrc element or raise :class:`VmbSrcNotPresent`.

        Caller must hold :attr:`_lock`.
        """
        if self._pipeline is None:
            raise VmbSrcNotPresent("pipeline is not running")
        element = _find_by_factory(self._pipeline, VMBSRC_FACTORY)
        if element is None:
            raise VmbSrcNotPresent("pipeline contains no vmbsrc element")
        return element

    def get_vmbsrc_property(self, name: str):
        """Return the current value of a vmbsrc property.

        Enum-typed properties are returned as their string nick (e.g.
        ``"Continuous"``). Raises :class:`VmbSrcNotPresent` if there's no
        vmbsrc element and :class:`RuntimeError` if the element lacks ``name``.
        """
        with self._lock:
            element = self._require_vmbsrc()
            pspec = _find_vmbsrc_pspec(element, name)
            value = element.get_property(name)
            if _is_enum_pspec(pspec):
                return _enum_int_to_nick(pspec, int(value))
            return value

    def set_vmbsrc_property(self, name: str, value):
        """Set a vmbsrc property and return its read-back value.

        For enum-typed properties, ``value`` may be either the integer value or
        the string name/nick (case-insensitive); the returned value is the nick.
        The value is read back from the live element after the set, so a camera
        clamp or silent refusal is visible to the caller. Raises
        :class:`ValueError` for invalid enum strings, :class:`VmbSrcNotPresent`
        if there's no vmbsrc element, and :class:`RuntimeError` if the element
        lacks ``name``.
        """
        with self._lock:
            element = self._require_vmbsrc()
            pspec = _find_vmbsrc_pspec(element, name)
            if _is_enum_pspec(pspec) and isinstance(value, str):
                value = _enum_str_to_int(pspec, value)
            element.set_property(name, value)
            read_back = element.get_property(name)
            if _is_enum_pspec(pspec):
                return _enum_int_to_nick(pspec, int(read_back))
            return read_back

    def get_enum_options(self, name: str) -> list[str]:
        """Return the valid string nicks for an enum-typed vmbsrc property.

        Raises :class:`VmbSrcNotPresent` if there's no vmbsrc element and
        :class:`RuntimeError` if the element lacks ``name``.
        """
        with self._lock:
            element = self._require_vmbsrc()
            pspec = _find_vmbsrc_pspec(element, name)
            return [member.value_nick for member in _enum_members(pspec)]

    def get_roi(self) -> dict:
        """Return the live region of interest as ``{width, height, offset_x, offset_y}``.

        Offsets are the concrete resolved values reported by the camera, never
        the ``-1`` "center" sentinel. Raises :class:`VmbSrcNotPresent` if
        there's no vmbsrc element.
        """
        with self._lock:
            return self._read_roi(self._require_vmbsrc())

    def set_roi(self, width: int, height: int, offset_x: int, offset_y: int) -> dict:
        """Set the full region of interest and return the read-back.

        Applies the four values in an order that can never transiently exceed
        the sensor bounds — zero both offsets, set width/height, then set the
        offsets to their targets. Without this, four independent sets could be
        refused depending on order. ``offset_x``/``offset_y`` of ``-1`` mean
        "center on that axis"; the read-back reports the concrete resolved
        offsets. Raises :class:`VmbSrcNotPresent` if there's no vmbsrc element.
        """
        with self._lock:
            element = self._require_vmbsrc()
            element.set_property("offsetx", 0)
            element.set_property("offsety", 0)
            element.set_property("width", width)
            element.set_property("height", height)
            element.set_property("offsetx", offset_x)
            element.set_property("offsety", offset_y)
            return self._read_roi(element)

    @staticmethod
    def _read_roi(element: Gst.Element) -> dict:
        """Read the four ROI properties off ``element``. Caller must hold :attr:`_lock`."""
        return {
            "width": element.get_property("width"),
            "height": element.get_property("height"),
            "offset_x": element.get_property("offsetx"),
            "offset_y": element.get_property("offsety"),
        }

    def list_appsinks(self) -> dict[str, dict | None]:
        """Return a mapping of appsink element name → negotiated caps dict.

        Caps are ``None`` for any appsink whose sink pad hasn't negotiated
        yet (which is normal when the pipeline isn't producing). Raises
        :class:`AppsinkNotPresent` if the pipeline isn't running.
        """
        with self._lock:
            if self._pipeline is None:
                raise AppsinkNotPresent("pipeline is not running")
            return {
                name: _caps_to_dict(element.get_static_pad("sink").get_current_caps())
                for name, element in _find_appsinks(self._pipeline).items()
            }

    def get_appsink_caps(self, name: str) -> dict | None:
        """Return the negotiated caps for a named appsink, or ``None`` if not yet negotiated.

        Raises :class:`AppsinkNotPresent` if the pipeline isn't running or
        contains no appsink with that name.
        """
        with self._lock:
            element = self._require_appsink(name)
            return _caps_to_dict(element.get_static_pad("sink").get_current_caps())

    def pull_appsink_sample(
        self, name: str, *, timeout_s: float = 5.0, wait: bool = True
    ) -> tuple[bytes, dict, dict] | None:
        """Pull a single sample from a named appsink.

        Returns ``(data, caps, meta)`` where ``data`` is the raw buffer
        bytes, ``caps`` is the negotiated caps dict (see :func:`_caps_to_dict`),
        and ``meta`` carries per-buffer timing (``pts``, ``dts``, ``duration``
        in nanoseconds; ``None`` for unset fields).

        With ``wait=True`` (default) this blocks up to ``timeout_s`` and
        raises :class:`AppsinkTimeout` if nothing arrives. With ``wait=False``
        returns ``None`` immediately if no sample is queued. Raises
        :class:`AppsinkNotPresent` for unknown name or unstarted pipeline.

        The element lookup is done under the lock, but the (possibly
        blocking) pull itself runs without it so other API calls aren't
        serialized behind a slow camera.
        """
        with self._lock:
            element = self._require_appsink(name)

        timeout_ns = 0 if not wait else int(timeout_s * Gst.SECOND)
        sample = element.try_pull_sample(timeout_ns)
        if sample is None:
            if wait:
                raise AppsinkTimeout(f"no sample from appsink {name!r} within {timeout_s}s")
            return None

        buffer = sample.get_buffer()
        data = bytes(buffer.extract_dup(0, buffer.get_size()))
        caps = _caps_to_dict(sample.get_caps())
        meta = {
            "pts": None if buffer.pts == Gst.CLOCK_TIME_NONE else int(buffer.pts),
            "dts": None if buffer.dts == Gst.CLOCK_TIME_NONE else int(buffer.dts),
            "duration": None if buffer.duration == Gst.CLOCK_TIME_NONE else int(buffer.duration),
        }
        return data, caps or {}, meta

    def drain_appsink(self, name: str, limit: int = DRAIN_LIMIT) -> int:
        """Discard every already-queued sample and return how many were dropped.

        An appsink is a FIFO: ``try_pull_sample`` returns the *oldest* buffer, so
        a pipeline that has been playing for a while serves history, not the
        present. Anything that means "what is the camera showing now" — a
        snapshot, or the first frame of a Capture — has to drain first, or it
        reads frames recorded before the request was even made. Without this a
        400x exposure change produced no visible change in pulled frames.

        Pulls with a zero timeout, so only buffers already queued are discarded;
        the ``limit`` guards against livelocking if the device produces faster
        than this loop can discard.
        """
        with self._lock:
            element = self._require_appsink(name)

        discarded = 0
        while discarded < limit and element.try_pull_sample(0) is not None:
            discarded += 1
        if discarded:
            log.debug("drained %d stale sample(s) from appsink %r", discarded, name)
        return discarded

    def _require_appsink(self, name: str) -> Gst.Element:
        """Look up an appsink by element name or raise :class:`AppsinkNotPresent`.

        Caller must hold :attr:`_lock`.
        """
        if self._pipeline is None:
            raise AppsinkNotPresent("pipeline is not running")
        appsinks = _find_appsinks(self._pipeline)
        element = appsinks.get(name)
        if element is None:
            raise AppsinkNotPresent(f"no appsink named {name!r}")
        return element

    # ------------------------------------------------------------------ appsrc

    def list_appsrcs(self) -> list[str]:
        """Return the element names of every appsrc in the running pipeline.

        Raises :class:`AppsrcNotPresent` if the pipeline isn't running, which is
        how a caller distinguishes "no pipeline yet" from "this description has
        no appsrc in it".
        """
        with self._lock:
            if self._pipeline is None:
                raise AppsrcNotPresent("pipeline is not running")
            return sorted(_find_appsrcs(self._pipeline))

    def get_appsrc_caps(self, name: str) -> dict | None:
        """Return the caps an appsrc was declared with, or ``None`` if it has none.

        Declared in the Pipeline description (``appsrc caps="..."``), so every
        appsrc is born knowing its format rather than learning it from whoever
        pushes first. Raises :class:`AppsrcNotPresent` for an unknown name or
        unstarted pipeline.
        """
        with self._lock:
            element = self._require_appsrc(name)
        return _caps_to_dict(element.get_property("caps"))

    def push_appsrc_frame(
        self,
        name: str,
        data: bytes,
        *,
        pts: int | None = None,
        dts: int | None = None,
        duration: int | None = None,
    ) -> None:
        """Push one frame into a named appsrc, carrying its timing forward.

        ``pts``/``dts``/``duration`` are nanoseconds, in the shape
        :meth:`pull_appsink_sample` reports them, and ``None`` leaves the field
        unset. Passing through the *source* buffer's timestamps rather than
        re-stamping is what keeps a transformed frame aligned with the frame it
        was made from — both branches then carry one pipeline running time.

        Raises :class:`AppsrcNotPresent` for an unknown name or unstarted
        pipeline, and :class:`AppsrcPushError` on a hard flow error. A pipeline
        being torn down answers ``FLUSHING``; that is normal and returns quietly.

        The element lookup is done under the lock; the push itself is not, so a
        slow consumer cannot serialize other API calls behind it.
        """
        with self._lock:
            element = self._require_appsrc(name)

        buffer = Gst.Buffer.new_wrapped(data)
        if pts is not None:
            buffer.pts = pts
        if dts is not None:
            buffer.dts = dts
        if duration is not None:
            buffer.duration = duration

        flow = element.emit("push-buffer", buffer)
        if flow == Gst.FlowReturn.OK:
            return
        if flow in (Gst.FlowReturn.FLUSHING, Gst.FlowReturn.EOS):
            log.debug("appsrc %r returned %s; pipeline is stopping", name, _flow_name(flow))
            return
        raise AppsrcPushError(f"appsrc {name!r} rejected a buffer: {_flow_name(flow)}")

    def _require_appsrc(self, name: str) -> Gst.Element:
        """Look up an appsrc by element name or raise :class:`AppsrcNotPresent`.

        Caller must hold :attr:`_lock`.
        """
        if self._pipeline is None:
            raise AppsrcNotPresent("pipeline is not running")
        element = _find_appsrcs(self._pipeline).get(name)
        if element is None:
            raise AppsrcNotPresent(f"no appsrc named {name!r}")
        return element
