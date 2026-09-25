"""VERIFY stage. Did the KPI tied to the fault recover, and is the cell clean?"""
from .detect import breaches_for

FAULT_KPI = {"ul_interference": "ul_noise_dbm",
             "congestion": "prb_util_pct",
             "overshoot": "drop_rate_pct"}

def verify(prev_row, new_row, fault_type, thresholds):
    key = FAULT_KPI[fault_type]
    before, after = prev_row[key], new_row[key]
    remaining = breaches_for(new_row, thresholds)
    return {"kpi": key, "before": before, "after": after,
            "remaining_breaches": remaining, "resolved": not remaining}
