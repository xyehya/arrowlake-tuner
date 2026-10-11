"""Measured V/F curves (SPEC-v3 section 3, ARCHITECTURE contract 9): the record schema and the root-owned store. Pure stdlib; no hardware access."""
from .record import MAX_NAME, QUICK_RATIOS, RECORD_BUDGET, UNITS, fit_record, is_curve_id, new_curve_id, summary, unit_cpus, unit_kind, validate_name
from .store import MAX_RECORDS, CurveStore, StoreError

__all__ = [
    "CurveStore", "MAX_NAME", "MAX_RECORDS", "QUICK_RATIOS", "RECORD_BUDGET", "StoreError", "UNITS", "fit_record", "is_curve_id", "new_curve_id", "summary",
    "unit_cpus", "unit_kind", "validate_name",
]
