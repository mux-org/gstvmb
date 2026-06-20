"""Per-instance configuration loaded from a single YAML file.

Each container of this image is bound to exactly one Camera. Its entire
camera configuration — identity and the GStreamer pipeline — comes from one
YAML file (``$CONFIG_FILE``, default ``/app/config.yaml``). Serving knobs
(``HOST``, ``PORT``, ``LOG_LEVEL``, ``GST_DEBUG``) remain environment
variables.

The pipeline string is used *literally* — there is no ``$VAR`` substitution.
Loading fails fast: a missing file, malformed YAML, or an absent ``id`` or
``pipeline`` aborts startup rather than running with an unintended config.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "/app/config.yaml"))


class ConfigError(RuntimeError):
    """Raised when the instance configuration is missing or invalid."""


@dataclass(frozen=True)
class CameraConfig:
    """Resolved configuration for the one Camera this Instance serves.

    :param id: Operator-assigned stable handle for the Instance (e.g. ``cam0``).
    :param label: Human-friendly display name. Defaults to ``id`` when omitted.
    :param pipeline: gst-launch description, used literally.
    """

    id: str
    label: str
    pipeline: str


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

    return CameraConfig(id=cam_id, label=label, pipeline=pipeline)
