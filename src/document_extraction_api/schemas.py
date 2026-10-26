from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from .tasks import Delivery, Invoice


class JobResponse(BaseModel):
    job_id: str
    task_id: str
    status: Literal["queued", "running", "succeeded", "needs_review", "failed"]
    result_url: str
    created_at: datetime
    updated_at: datetime
    data: Invoice | Delivery | None
    review_reasons: list[str]
    error: dict[str, str] | None
    result_expired: bool
    versions: dict[str, str]
