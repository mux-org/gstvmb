"""Capture control routes.

Deliberately mirrors ``/pipeline``: ``GET`` for status, ``POST`` for commands,
every response echoing the full status object. The client already polls
``/pipeline`` with react-query, so this is the same hook against a different URL
rather than a second state machine to learn.

Capture is a *commandable resource*, not a blocking call. ``start`` returns 202
as soon as the file is open and the preconditions hold; the outcome arrives on a
later ``GET``. A blocking endpoint would sit behind nginx's default 60 s
``proxy_read_timeout`` (``config/nginx/default.conf.template`` overrides it only
for the telescope SSE stream) and return 504 to the operator while the capture
kept running server-side.
"""

from fastapi import APIRouter, Depends

from app.api.deps import get_capture
from app.api.models import CaptureRequest, CaptureStatus
from app.capture import CaptureManager, CaptureState, Extent

router = APIRouter()


def _status(state: CaptureState) -> CaptureStatus:
    return CaptureStatus(**vars(state))


@router.get("", response_model=CaptureStatus, summary="Get capture status")
def get_status(capture: CaptureManager = Depends(get_capture)) -> CaptureStatus:
    return _status(capture.status())


@router.post(
    "/start",
    response_model=CaptureStatus,
    status_code=202,
    summary="Start a capture",
    responses={
        202: {"description": "Accepted. Poll GET /capture for progress and outcome."},
        400: {"description": "Malformed label/capture_id, or an unimplemented extent mode."},
        409: {
            "description": (
                "Cannot capture right now: one is already running, the pipeline is not "
                "playing, no pixel_format is declared, the archive is not mounted, or "
                "there is not enough free space. The reason is in `detail`."
            )
        },
    },
)
def start(
    body: CaptureRequest, capture: CaptureManager = Depends(get_capture)
) -> CaptureStatus:
    extent = Extent.parse(body.extent.mode, body.extent.value)
    return _status(
        capture.start(
            extent,
            label=body.label,
            capture_id=body.capture_id,
            appsink=body.appsink,
        )
    )


@router.post(
    "/stop",
    response_model=CaptureStatus,
    summary="Stop a running capture early",
)
def stop(capture: CaptureManager = Depends(get_capture)) -> CaptureStatus:
    """Finish a running capture early, leaving a conformant file.

    Idempotent, and a no-op when nothing is running. The cube's ``NAXIS3`` is
    corrected to the number of frames actually written, so an interrupted
    capture is a short file rather than a truncated one. A bounded capture
    stopped this way lands in ``aborted``, not ``error`` — it is a deliberate
    operator action, not a failure.
    """
    return _status(capture.stop())
