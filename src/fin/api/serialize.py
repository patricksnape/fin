"""JSON conversion: money as strings (Decimal), dates ISO, NaN as null."""

import dataclasses
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd
from pydantic import BaseModel


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, BaseModel):
        return to_jsonable(obj.model_dump())
    if isinstance(obj, pd.DataFrame):
        return [to_jsonable(r) for r in obj.to_dict("records")]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return to_jsonable(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set):
        return [to_jsonable(v) for v in obj]
    if obj is None or isinstance(obj, str | int | bool):
        return obj
    if pd.isna(obj):
        return None
    return obj
