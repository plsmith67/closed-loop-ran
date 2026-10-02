"""Digital-twin what-if prediction (heuristic).

Inserted between DECIDE and APPROVE: given the current KPI row and a
diagnosed fault type, estimate whether the proposed fix is likely to clear
the primary metric before a human (or auto-approve policy) commits.

This is an explainable heuristic for demonstrating the digital-twin
decision pattern — not a trained model and not a PHY/channel/UE-level
simulator. Upgrading it to a calibrated or physics-based twin is a named
next step. Logging predicted values on the act audit event next to the
later verify() outcome is the bootstrap for that calibration.
"""
from .verify import FAULT_KPI as _VERIFY_FAULT_KPI

# Same mapping verify.py uses, plus pim (diagnosed today but no action template).
FAULT_KPI = {**_VERIFY_FAULT_KPI, "pim": "drop_rate_pct"}

RECOVERY_FRACTION = 0.60
DEFAULT_SUCCESS_RATE = 0.85

# Nominal healthy setpoints (inside the safe side of each threshold). Moving
# only toward the threshold *edge* with fraction < 1 never clears a breach;
# these refs are the "normal range" the gap-closure heuristic aims at.
HEALTHY_REF = {
    "drop_rate_pct": 0.5,
    "prb_util_pct": 50.0,
    "ul_noise_dbm": -115.0,
    "rrc_success_pct": 99.0,
}

BASIS = (
    "Historical fixes of this type typically recover ~60% of the gap "
    "to normal range; confidence reflects the proportion of similar "
    "fixes that fully resolved on first attempt."
)


def _threshold_for(fault_type, kpi, thresholds):
    """Return (op, edge) for the KPI this fix is meant to move.

    sleeping_cell uses sleeping_cell_prb (lt) applied to prb_util_pct —
    traffic must rise out of the near-zero band, not fall under the
    congestion (gt) threshold on the same column.
    """
    if fault_type == "sleeping_cell" and "sleeping_cell_prb" in thresholds:
        return thresholds["sleeping_cell_prb"]
    return thresholds.get(kpi)


def _healthy_target(kpi, op, edge):
    """Nominal post-fix value on the healthy side of the threshold."""
    ref = HEALTHY_REF.get(kpi)
    if ref is not None:
        if op == "gt" and ref <= edge:
            return ref
        if op == "lt" and ref >= edge:
            return ref
    # Fallback: a small step past the edge into the healthy band.
    if op == "gt":
        return edge - max(1.0, abs(edge) * 0.05)
    return edge + max(1.0, abs(edge) * 0.05)


def predict_outcome(row, fault_type, cfg):
    """Predict post-fix primary KPI and whether it would clear its threshold.

    Returns dict with predicted_value, predicted_resolved, confidence_pct, basis,
    plus kpi/before for display and logging.
    """
    thresholds = cfg["detect"]["thresholds"]
    success = float(
        cfg.get("simulator", {}).get("fix_success_rate", DEFAULT_SUCCESS_RATE)
    )
    confidence_pct = int(round(100 * success))

    kpi = FAULT_KPI.get(fault_type)
    if kpi is None or kpi not in row:
        return {
            "kpi": kpi or "unknown",
            "before": None,
            "predicted_value": None,
            "predicted_resolved": False,
            "confidence_pct": confidence_pct,
            "basis": BASIS,
        }

    current = float(row[kpi])
    th = _threshold_for(fault_type, kpi, thresholds)
    if th is None:
        return {
            "kpi": kpi,
            "before": current,
            "predicted_value": current,
            "predicted_resolved": False,
            "confidence_pct": confidence_pct,
            "basis": BASIS,
        }

    op, edge = th[0], float(th[1])
    target = _healthy_target(kpi, op, edge)

    if op == "gt":
        if current > edge:
            predicted = current - RECOVERY_FRACTION * (current - target)
        else:
            predicted = current
        predicted_resolved = predicted <= edge
    else:
        # op == "lt": healthy when value >= edge
        if current < edge:
            predicted = current + RECOVERY_FRACTION * (target - current)
        else:
            predicted = current
        predicted_resolved = predicted >= edge

    return {
        "kpi": kpi,
        "before": round(current, 2),
        "predicted_value": round(predicted, 2),
        "predicted_resolved": bool(predicted_resolved),
        "confidence_pct": confidence_pct,
        "basis": BASIS,
    }


def format_kpi_value(kpi, value):
    if value is None:
        return "n/a"
    if kpi == "ul_noise_dbm":
        return f"{value} dBm"
    return f"{value}%"
