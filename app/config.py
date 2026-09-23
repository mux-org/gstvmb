"""Per-instance configuration loaded from a single YAML file.

Each container of this image is bound to exactly one Camera. Its entire
camera configuration — identity, the GStreamer pipeline, and the hardware
facts GStreamer cannot report — comes from one YAML file (``$CONFIG_FILE``,
default ``/app/config.yaml``). Serving knobs (``HOST``, ``PORT``,
``LOG_LEVEL``, ``GST_DEBUG``) remain environment variables.

The pipeline string is used *literally* — there is no ``$VAR`` substitution.
Loading fails fast: a missing file, malformed YAML, or an absent ``id`` or
``pipeline`` aborts startup rather than running with an unintended config.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "/app/config.yaml"))

# The GenICam monochrome pixel formats this service can interpret, mapped to the
# number of significant bits the Device puts in each word.
#
# This table exists because the pixel format is *undiscoverable at runtime*.
# vmbsrc exposes no pixelformat property (the full `gst-inspect-1.0 vmbsrc`
# property list has nothing of the sort), and every unpacked mono format above 8
# bits negotiates as the same `video/x-raw,format=GRAY16_LE` — Mono10, Mono12,
# Mono14 and Mono16 are indistinguishable from caps alone. So a Raw Frame's
# values could top out at 1023, 4095, 16383 or 65535 and nothing in the running
# process can tell which. It is declared here, alongside the Device id and the
# capsfilter, as one more hardware fact pinned in the one config file.
#
# Packed formats (Mono12p, Mono12Packed) are deliberately absent: they have no
# clean video/x-raw mapping and would need unpacking by hand.
PIXEL_FORMATS: dict[str, int] = {
    "Mono8": 8,
    "Mono10": 10,
    "Mono12": 12,
    "Mono14": 14,
    "Mono16": 16,
}


class ConfigError(RuntimeError):
    """Raised when the instance configuration is missing or invalid."""


@dataclass(frozen=True)
class CameraConfig:
    """Resolved configuration for the one Camera this Instance serves.

    :param id: Operator-assigned stable handle for the Instance (e.g. ``cam0``).
    :param label: Human-friendly display name. Defaults to ``id`` when omitted.
    :param pipeline: gst-launch description, used literally.
    :param pixel_format: GenICam pixel format the Device is producing (e.g.
        ``Mono12``), or ``None`` when undeclared. Optional because a
        streaming-only Instance never needs it; required before a Capture,
        which cannot interpret Raw Frames without it.
    """

    id: str
    label: str
    pipeline: str
    pixel_format: str | None = None

    @property
    def bit_depth(self) -> int | None:
        """Significant bits per pixel, derived from :attr:`pixel_format`.

        ``None`` when no pixel format is declared. Derived rather than
        configured so the two can never disagree.
        """
        if self.pixel_format is None:
            return None
        return PIXEL_FORMATS[self.pixel_format]


def load_config(path: Path = CONFIG_FILE) -> CameraConfig:
    """Load and validate the instance config from ``path``.

    Raises :class:`ConfigError` on a missing/empty file, malformed YAML, a
    non-mapping document, or a missing/blank ``id`` or ``pipeline``.
    """
    if not path.is_file():
        raise ConfigError(f"config file {path} does not exist")

    text = path.read_text()
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file {path} is not valid YAML: {exc}") from exc

    if raw is None:
        raise ConfigError(f"config file {path} is empty")
    if not isinstance(raw, dict):
        raise ConfigError(f"config file {path} must be a YAML mapping, got {type(raw).__name__}")

    cam_id = raw.get("id")
    if not isinstance(cam_id, str) or not cam_id.strip():
        raise ConfigError("config field 'id' is required and must be a non-empty string")
    cam_id = cam_id.strip()

    pipeline = raw.get("pipeline")
    if not isinstance(pipeline, str) or not pipeline.strip():
        raise ConfigError("config field 'pipeline' is required and must be a non-empty string")
    pipeline = pipeline.strip()

    label = raw.get("label")
    if label is None:
        label = cam_id
    elif not isinstance(label, str):
        raise ConfigError("config field 'label' must be a string")

    pixel_format = _parse_pixel_format(raw.get("pixel_format"))

    return CameraConfig(
        id=cam_id, label=label, pipeline=pipeline, pixel_format=pixel_format
    )


def _parse_pixel_format(value) -> str | None:
    """Validate an optional ``pixel_format`` and return its canonical spelling.

    Matching is case-insensitive (``"mono12"`` is accepted) but the value is
    stored in canonical GenICam form (``"Mono12"``) so it can be written into a
    FITS header verbatim. A declared-but-unrecognised format is a hard config
    error rather than a silent fallback: guessing the bit depth of saved science
    data is worse than refusing to start.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("config field 'pixel_format' must be a string")

    needle = value.strip().casefold()
    for canonical in PIXEL_FORMATS:
        if canonical.casefold() == needle:
            return canonical

    valid = ", ".join(PIXEL_FORMATS)
    raise ConfigError(
        f"config field 'pixel_format' has unknown value {value!r}; expected one of {valid}"
    )
