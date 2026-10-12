"""Measured V/F curves (SPEC-v3 section 3, ARCHITECTURE contract 9): the record schema and the root-owned store. Pure stdlib; no hardware access."""
from .record import MAX_NAME, RECORD_BUDGET, fit_record, is_curve_id, is_unit_id, new_curve_id, summary, unit_kind, unit_order, validate_name
from .store import MAX_RECORDS, CurveStore, StoreError

__all__ = [
    "CurveStore", "MAX_NAME", "MAX_RECORDS", "RECORD_BUDGET", "StoreError", "fit_record", "is_curve_id", "is_unit_id", "new_curve_id", "summary",
    "unit_kind", "unit_order", "validate_name",
]
