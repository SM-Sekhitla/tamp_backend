from datetime import datetime

from pydantic import Field

from app.schemas.auth import Input


class PositionInput(Input):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)
    recordedAt: datetime
    accuracyM: float | None = Field(default=None, ge=0, le=5000, allow_inf_nan=False)
    speedKph: float | None = Field(default=None, ge=0, le=300, allow_inf_nan=False)
    heading: float | None = Field(default=None, ge=0, lt=360, allow_inf_nan=False)

