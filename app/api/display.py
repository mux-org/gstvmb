from fastapi import APIRouter, Depends

from app.api.deps import get_display
from app.api.models import DisplayStatus
from app.display import DisplayPump

router = APIRouter()


@router.get("", response_model=DisplayStatus, summary="Get display pump status")
def get_status(display: DisplayPump = Depends(get_display)) -> DisplayStatus:
    return DisplayStatus(**display.status())
