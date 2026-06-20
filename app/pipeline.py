import logging
import re
import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, GObject, Gst  # noqa: E402

Gst.init(None)

log = logging.getLogger(__name__)

VMBSRC_FACTORY = "vmbsrc"
APPSINK_FACTORY = "appsink"

# Matches the vmbsrc ``camera=<device id>`` assignment in a gst-launch
# description so the bound Device id can be reported without a running
# element. Anchored on a word boundary so it doesn't match e.g. a
# hypothetical ``othercamera=`` property.
_CAMERA_PROP_RE = re.compile(r"(?:^|\s)camera=(\S+)")

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

    def _build(self) -> None:
        """Parse the description and start the MainLoop thread.

        Called from :meth:`start` under the lock. Leaves the pipeline in its
        default (NULL) state — the caller is responsible for transitioning to
        PLAYING.
        """
        pipeline = Gst.parse_launch(self._description)
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
        """Set the pipeline to NULL, quit the MainLoop, and drop references."""
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
        if self._loop is not None and self._loop.is_running():
            self._loop.quit()
        self._pipeline = None
        self._loop = None
        self._loop_thread = None

    def _on_bus_message(self, _bus: Gst.Bus, msg: Gst.Message) -> None:
        """Handle bus messages delivered on the MainLoop thread.

        Logs ERROR messages and tears the pipeline down to NULL on either
        ERROR or EOS so it doesn't sit in a half-broken PLAYING state. Other
        message types are ignored.
        """
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log.error("gst error: %s (%s)", err, dbg)
            if self._pipeline is not None:
                self._pipeline.set_state(Gst.State.NULL)
        elif t == Gst.MessageType.EOS:
            log.info("gst eos")
            if self._pipeline is not None:
                self._pipeline.set_state(Gst.State.NULL)

    def start(self) -> None:
        """Build the pipeline and transition it to PLAYING.

        No-op if already running. On state-change failure the pipeline is
        torn down and :class:`RuntimeError` is raised.
        """
        with self._lock:
            if self._pipeline is not None:
                return
            self._build()
            ret = self._pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                self._teardown()
                raise RuntimeError("failed to set pipeline to PLAYING")

    def stop(self) -> None:
        """Tear the pipeline down to NULL and stop the MainLoop. No-op if not running."""
        with self._lock:
            if self._pipeline is None:
                return
            self._teardown()

    def restart(self) -> None:
        """Stop the pipeline (if running) and start it again.

        Equivalent to :meth:`stop` followed by :meth:`start`. Useful for
        re-applying configuration that vmbsrc only reads at state transitions
        (e.g. settings loaded from ``settingsfile``).
        """
        self.stop()
        self.start()

    def is_running(self) -> bool:
        """Return ``True`` if :meth:`start` has been called and the pipeline has not been torn down."""
        return self._pipeline is not None

    @property
    def description(self) -> str:
        """The gst-launch description this pipeline was constructed with."""
        return self._description

    @property
    def device(self) -> str | None:
        """The bound Device id, parsed from the description's ``camera=`` (or ``None``)."""
        return parse_device(self._description)

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
