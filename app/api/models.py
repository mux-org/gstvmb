from typing import Any, Literal

from pydantic import BaseModel


class CameraInfo(BaseModel):
    """Self-describing identity for the one Camera this Instance serves.

    ``id`` and ``label`` come from the instance config; ``device`` is parsed
    from the pipeline's ``vmbsrc camera=`` assignment and is ``None`` when the
    pipeline pins no device.
    """

    id: str
    label: str
    device: str | None = None


class PipelineStatus(BaseModel):
    """Lifecycle snapshot for the underlying GStreamer pipeline.

    ``state`` encodes operator intent, not just liveness: ``idle`` (never
    started since boot), ``playing``, ``stopped`` (deliberately stopped), and
    ``error``. ``detail`` carries human context for the current state — chiefly
    the last error reason — and is ``None`` when there's nothing to add.
    """

    state: Literal["idle", "playing", "stopped", "error"]
    detail: str | None = None
    description: str


class FloatValue(BaseModel):
    """A scalar (double) camera control's value.

    Shared request/response body for ``/camera/exposure_time`` and
    ``/camera/gain``. Numeric bounds are enforced by the camera, not here —
    the ``GParamSpec`` ranges are C type limits, not real device limits.
    """

    value: float


class EnumValueIn(BaseModel):
    """Request body for an enum camera control (e.g. ``/camera/exposure_auto``).

    ``value`` is the string nick, case-insensitive (``"off"`` / ``"Off"``).
    """

    value: str


class EnumValueOut(BaseModel):
    """Response body for an enum camera control.

    ``options`` lists the valid nicks so a UI can populate a dropdown without a
    separate schema fetch.
    """

    value: str
    options: list[str]


class RoiValue(BaseModel):
    """The region of interest, shared request/response body for ``/camera/roi``.

    On a PUT all four fields are required (full replace). ``offset_x`` /
    ``offset_y`` of ``-1`` mean "center on that axis"; responses always report
    the concrete resolved offsets, never ``-1``.
    """

    width: int
    height: int
    offset_x: int
    offset_y: int


class AppsinkInfo(BaseModel):
    """Snapshot of an appsink element's negotiated caps.

    ``caps`` is ``None`` until the pipeline has prerolled and the sink pad
    has negotiated with upstream — that is, until data is actually flowing.
    The ``caps`` dict, when present, always contains a ``"caps"`` key with
    the full gst-caps string, plus convenience fields parsed from the first
    structure (e.g. ``format``, ``width``, ``height``, ``framerate``).
    """

    name: str
    caps: dict[str, Any] | None = None
