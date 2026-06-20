import json
import secrets

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse

from app.api.deps import get_pipeline
from app.api.models import AppsinkInfo
from app.pipeline import AppsinkNotPresent, AppsinkTimeout, Pipeline

router = APIRouter()


@router.get(
    "",
    response_model=dict[str, AppsinkInfo],
    summary="List appsinks in the running pipeline",
)
def list_appsinks(pipeline: Pipeline = Depends(get_pipeline)) -> dict[str, AppsinkInfo]:
    return {name: AppsinkInfo(name=name, caps=caps) for name, caps in pipeline.list_appsinks().items()}


@router.get(
    "/{name}",
    response_model=AppsinkInfo,
    summary="Get caps for a single appsink",
)
def get_appsink(name: str, pipeline: Pipeline = Depends(get_pipeline)) -> AppsinkInfo:
    return AppsinkInfo(name=name, caps=pipeline.get_appsink_caps(name))


@router.post(
    "/{name}/sample",
    summary="Pull a single sample from an appsink",
    responses={
        200: {
            "content": {"application/octet-stream": {}},
            "description": "Raw buffer bytes. Caps and timing on X-Gst-* headers.",
        },
        204: {"description": "No sample available (wait=false and the queue was empty)."},
        404: {"description": "Unknown appsink name or pipeline not running."},
        504: {"description": "No sample arrived within the timeout (wait=true)."},
    },
)
def pull_sample(
    name: str,
    wait: bool = Query(True, description="Block until a sample is available or timeout elapses."),
    timeout: float = Query(5.0, ge=0.0, description="Block timeout in seconds when wait=true."),
    pipeline: Pipeline = Depends(get_pipeline),
) -> Response:
    result = pipeline.pull_appsink_sample(name, timeout_s=timeout, wait=wait)
    if result is None:
        return Response(status_code=204)

    data, caps, meta = result
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers=_frame_headers(caps, meta),
    )


def _frame_headers(caps: dict, meta: dict) -> dict[str, str]:
    """Build the X-Gst-* headers describing one frame's caps and timing."""
    headers: dict[str, str] = {}
    if caps:
        headers["X-Gst-Caps"] = caps.get("caps", "")
        headers["X-Gst-Caps-Json"] = json.dumps(caps)
        for field in ("media", "format", "width", "height", "framerate"):
            if field in caps:
                headers[f"X-Gst-{field.title()}"] = str(caps[field])
    for field, value in meta.items():
        if value is not None:
            headers[f"X-Gst-{field.title()}"] = str(value)
    return headers


@router.post(
    "/{name}/samples",
    summary="Pull a bounded sequence of samples from an appsink, streamed as multipart",
    responses={
        200: {
            "content": {"multipart/mixed": {}},
            "description": (
                "Streamed multipart/mixed. One application/octet-stream part per frame "
                "with X-Gst-* headers, then a final application/json summary part "
                "carrying {captured, requested, error}."
            ),
        },
        404: {"description": "Unknown appsink name or pipeline not running."},
    },
)
def pull_samples(
    name: str,
    count: int = Query(..., ge=1, le=1000, description="Number of samples to pull."),
    timeout: float = Query(5.0, ge=0.0, description="Per-sample timeout in seconds."),
    pipeline: Pipeline = Depends(get_pipeline),
) -> StreamingResponse:
    # Validate eagerly so a missing appsink / stopped pipeline returns a clean
    # 404 instead of a streaming response containing only an error summary.
    pipeline.get_appsink_caps(name)

    boundary = secrets.token_hex(16)
    sep = f"\r\n--{boundary}\r\n".encode()
    terminator = f"\r\n--{boundary}--\r\n".encode()

    def encode_frame_part(index: int, data: bytes, caps: dict, meta: dict) -> bytes:
        headers = {"Content-Type": "application/octet-stream", "X-Frame-Index": str(index)}
        headers.update(_frame_headers(caps, meta))
        head = "".join(f"{k}: {v}\r\n" for k, v in headers.items()).encode() + b"\r\n"
        return head + data

    def encode_summary_part(captured: int, requested: int, error: str | None) -> bytes:
        body = json.dumps(
            {"captured": captured, "requested": requested, "error": error}
        ).encode()
        head = (
            "Content-Type: application/json\r\n"
            "X-Summary: true\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode()
        return head + body

    def gen():
        captured = 0
        error: str | None = None
        first = True
        try:
            for i in range(count):
                try:
                    data, caps, meta = pipeline.pull_appsink_sample(
                        name, timeout_s=timeout, wait=True
                    )
                except AppsinkTimeout as exc:
                    error = str(exc)
                    break
                except AppsinkNotPresent as exc:
                    error = str(exc)
                    break
                # Boundary separator goes *between* parts; the opening one
                # has no leading CRLF.
                yield (sep if not first else f"--{boundary}\r\n".encode())
                first = False
                yield encode_frame_part(i, data, caps, meta)
                captured += 1
        finally:
            yield (sep if not first else f"--{boundary}\r\n".encode())
            yield encode_summary_part(captured, count, error)
            yield terminator

    return StreamingResponse(
        gen(),
        media_type=f'multipart/mixed; boundary="{boundary}"',
    )
