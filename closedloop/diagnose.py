"""DIAGNOSE + DECIDE stage.

Guardrail: whatever diagnoses the fault (rules or LLM), the actual cmedit command
comes ONLY from the approved template table below. The LLM never writes commands.
"""
import json
import requests

FAULT_TYPES = ["ul_interference", "overshoot", "congestion", "multiple",
               "sleeping_cell", "pim", "unknown"]

ACTION_TEMPLATES = {
    "ul_interference": "cmedit set {cell} EUtranCellFDD pZeroNominalPusch=-100  # mitigation, open field ticket",
    "overshoot":       "cmedit set {cell} RetSubUnit electricalAntennaTilt=+10  # +1.0 deg downtilt",
    "congestion":      "cmedit set {cell} EUtranCellFDD cellIndividualOffsetEUtran=-3  # offload to neighbors",
}

def command_for(fault_type, cell):
    t = ACTION_TEMPLATES.get(fault_type)
    return t.format(cell=cell) if t else None

# ---------------- Rules ----------------
def rule_diagnose(r):
    b = set(r["breaches"])
    if "ul_noise_dbm" in b:
        return "ul_interference", f"UL noise {r['ul_noise_dbm']} dBm with RRC success {r['rrc_success_pct']}%; likely external interference"
    if "prb_util_pct" in b:
        return "congestion", f"PRB util {r['prb_util_pct']}% with DL throughput {r['dl_tput_mbps']} Mbps"
    if "drop_rate_pct" in b:
        return "overshoot", f"Drop rate {r['drop_rate_pct']}% with normal UL noise; suspect coverage overshoot"
    return "unknown", f"ML-only anomaly (score {r['ml_score']}); no rule matched"

# ---------------- LLM ----------------
SYSTEM = (
    "You are a senior LTE RAN optimization engineer doing root cause analysis. "
    "Normal ranges: drop rate under 2%, RRC success above 97%, UL noise below -108 dBm, "
    "PRB utilization below 85%. Classify the fault as exactly one of: "
    + ", ".join(FAULT_TYPES) + ". Use 'unknown' if KPIs look normal or unclear. "
    "The breached thresholds list is authoritative; base your diagnosis on which KPIs breached. "
    "High ul_noise_dbm with low rrc_success_pct indicates ul_interference. "
    "High drop_rate_pct with normal ul_noise_dbm indicates overshoot. "
    "High prb_util_pct with low throughput indicates congestion. "
    "multiple: more than one fault pattern is present on the same cell at once. "
    "sleeping_cell: the cell is on air and alarm-free but carries almost no traffic or throughput. "
    "pim: passive intermodulation, uplink noise that rises with downlink load, typically hurting "
    "retainability while access (RRC success) stays normal. "
    "If no thresholds are breached and there is no sleeping_cell pattern, answer unknown. "
    'Reply ONLY with JSON: {"fault_type": "...", "rca": "one sentence"}'
)

def llm_diagnose(r, cfg):
    kpis = (f"Cell {r['cell']}: drop rate {r['drop_rate_pct']}%, RRC success {r['rrc_success_pct']}%, "
            f"UL noise {r['ul_noise_dbm']} dBm, PRB util {r['prb_util_pct']}%, "
            f"DL throughput {r['dl_tput_mbps']} Mbps.")
    breaches = (f"Thresholds breached: {', '.join(r['breaches'])}" if r["breaches"]
                else "Thresholds breached: none (all KPIs within normal range)")
    resp = requests.post(cfg["url"], timeout=cfg["timeout_s"], json={
        "model": cfg["model"], "system": SYSTEM, "prompt": f"{kpis}\n{breaches}",
        "stream": False, "format": "json", "options": {"temperature": 0},
    })
    resp.raise_for_status()
    out = json.loads(resp.json()["response"])
    fault = out.get("fault_type", "unknown")
    if fault not in FAULT_TYPES:      # never trust free-form output
        fault = "unknown"
    return fault, out.get("rca", "")

# ---------------- Dispatcher ----------------
def diagnose(r, cfg):
    """Returns dict: fault_type, rca, source, and (in shadow mode) llm comparison."""
    mode = cfg["mode"]
    rule_fault, rule_rca = rule_diagnose(r)
    result = {"fault_type": rule_fault, "rca": rule_rca, "source": "rules"}

    if mode == "rules":
        return result
    try:
        llm_fault, llm_rca = llm_diagnose(r, cfg["llm"])
    except Exception as e:
        result["llm_error"] = str(e)[:120]
        result["source"] = "rules (llm failed)"
        return result

    if mode == "llm":
        return {"fault_type": llm_fault, "rca": llm_rca, "source": "llm"}
    # shadow mode: rules decide, LLM is recorded for evaluation
    result.update({"llm_fault": llm_fault, "llm_rca": llm_rca,
                   "agree": llm_fault == rule_fault})
    return result
