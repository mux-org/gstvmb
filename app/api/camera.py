from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_config, get_pipeline
from app.api.models import CameraInfo, EnumValueIn, EnumValueOut, FloatValue, RoiValue
from app.config import CameraConfig
from app.pipeline import Pipeline

router = APIRouter()


@router.get(
    "",
    response_model=CameraInfo,
    summary="Describe this instance's camera (id, label, bound device)",
)
def get_camera(
    config: CameraConfig = Depends(get_config),
    pipeline: Pipeline = Depends(get_pipeline),
) -> CameraInfo:
    return CameraInfo(id=config.id, label=config.label, device=pipeline.device)


# --- Scalar controls --------------------------------------------------------
#
# Each control is a thin, typed wrapper over a single vmbsrc property. Numeric
# bounds are enforced by the camera (the GParamSpec ranges are C type limits,
# not real device limits), so a PUT always reads the value back and returns it:
# a clamp or silent refusal shows up as a GET-after-PUT mismatch the caller can
# see, rather than being hidden. ``VmbSrcNotPresent`` (pipeline not running /
# no vmbsrc element) maps to 404 centrally in app.api.errors.


def _get_float(pipeline: Pipeline, prop: str) -> FloatValue:
    return FloatValue(value=pipeline.get_vmbsrc_property(prop))


def _set_float(pipeline: Pipeline, prop: str, body: FloatValue) -> FloatValue:
    return FloatValue(value=pipeline.set_vmbsrc_property(prop, body.value))


@router.get("/exposure_time", response_model=FloatValue, summary="Get exposure time (µs)")
def get_exposure_time(pipeline: Pipeline = Depends(get_pipeline)) -> FloatValue:
    return _get_float(pipeline, "exposuretime")


@router.put("/exposure_time", response_model=FloatValue, summary="Set exposure time (µs)")
def set_exposure_time(
    body: FloatValue, pipeline: Pipeline = Depends(get_pipeline)
) -> FloatValue:
    return _set_float(pipeline, "exposuretime", body)


@router.get("/gain", response_model=FloatValue, summary="Get gain")
def get_gain(pipeline: Pipeline = Depends(get_pipeline)) -> FloatValue:
    return _get_float(pipeline, "gain")


@router.put("/gain", response_model=FloatValue, summary="Set gain")
def set_gain(body: FloatValue, pipeline: Pipeline = Depends(get_pipeline)) -> FloatValue:
    return _set_float(pipeline, "gain", body)


# --- Enum controls ----------------------------------------------------------
#
# GET carries the valid ``options`` (string nicks) so a UI can populate a
# dropdown without a separate schema fetch. PUT accepts a nick (case-insensitive)
# and returns 400 for an unknown one.


def _get_enum(pipeline: Pipeline, prop: str) -> EnumValueOut:
    return EnumValueOut(
        value=pipeline.get_vmbsrc_property(prop),
        options=pipeline.get_enum_options(prop),
    )


def _set_enum(pipeline: Pipeline, prop: str, body: EnumValueIn) -> EnumValueOut:
    try:
        value = pipeline.set_vmbsrc_property(prop, body.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return EnumValueOut(value=value, options=pipeline.get_enum_options(prop))


@router.get("/exposure_auto", response_model=EnumValueOut, summary="Get auto-exposure mode")
def get_exposure_auto(pipeline: Pipeline = Depends(get_pipeline)) -> EnumValueOut:
    return _get_enum(pipeline, "exposureauto")


@router.put("/exposure_auto", response_model=EnumValueOut, summary="Set auto-exposure mode")
def set_exposure_auto(
    body: EnumValueIn, pipeline: Pipeline = Depends(get_pipeline)
) -> EnumValueOut:
    return _set_enum(pipeline, "exposureauto", body)


@router.get(
    "/white_balance_auto",
    response_model=EnumValueOut,
    summary="Get auto white-balance mode",
)
def get_white_balance_auto(pipeline: Pipeline = Depends(get_pipeline)) -> EnumValueOut:
    return _get_enum(pipeline, "balancewhiteauto")


@router.put(
    "/white_balance_auto",
    response_model=EnumValueOut,
    summary="Set auto white-balance mode",
)
def set_white_balance_auto(
    body: EnumValueIn, pipeline: Pipeline = Depends(get_pipeline)
) -> EnumValueOut:
    return _set_enum(pipeline, "balancewhiteauto", body)


# --- Region of interest -----------------------------------------------------
#
# The ROI is one resource, not four properties, because width/height/offset are
# interdependent: setting them independently can be refused by the camera if an
# intermediate state would exceed the sensor. PUT is full-replace (all four
# required) and applies them in an always-safe order (zero offsets -> set size
# -> set offsets); see Pipeline.set_roi. An offset of -1 means "center on that
# axis" on the way in; responses always report the concrete resolved offset,
# never -1.


@router.get("/roi", response_model=RoiValue, summary="Get the region of interest")
def get_roi(pipeline: Pipeline = Depends(get_pipeline)) -> RoiValue:
    return RoiValue(**pipeline.get_roi())


@router.put("/roi", response_model=RoiValue, summary="Set the region of interest")
def set_roi(body: RoiValue, pipeline: Pipeline = Depends(get_pipeline)) -> RoiValue:
    return RoiValue(
        **pipeline.set_roi(body.width, body.height, body.offset_x, body.offset_y)
    )
