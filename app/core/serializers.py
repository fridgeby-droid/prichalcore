from decimal import Decimal
from datetime import date, datetime


def clean(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    return value


def row(obj, *fields):
    return {field: clean(getattr(obj, field)) for field in fields}
