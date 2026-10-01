"""Closed-loop RAN prototype entry point.

COLLECT -> DETECT -> DIAGNOSE -> DECIDE -> APPROVE -> ACT -> VERIFY -> repeat

Examples:
  python main.py                      # uses config.yaml, asks before each action
  python main.py --auto               # Level 4 style, no human gate
  python main.py --mode shadow --auto # rules act, LLM graded alongside
"""
import argparse
import copy
import csv
import os
import time
from datetime import datetime, timedelta

from closedloop.config import load_config
from closedloop.simulator import NetworkSimulator
from closedloop.pm_reader import PMDataReader
from closedloop.detect import detect
from closedloop.diagnose import diagnose, command_for, rule_diagnose
from closedloop.act import AuditLog, approve, execute
from closedloop.verify import verify

EXPECTED_LABEL = {
    "ul_interference": "ul_interference",
    "overshoot": "overshoot",
    "congestion": "congestion",
    "combined": "multiple",
    "sleeping_cell": "sleeping_cell",
    "pim": "pim",
    "none": "unknown",
}
FAULT_ORDER = list(EXPECTED_LABEL)
MAX_RETRIES = 2  # one retry after first verify failure, then escalate
NEVER_AUTO = frozenset({"unknown", "multiple"})


def auto_approve_for(fault_type, lc):
    """Level 4 for listed fault types; --auto overrides to approve all actionable faults."""
    if lc.get("auto_approve_all"):
        return True
    if fault_type in NEVER_AUTO:
        return False
    return fault_type in lc.get("auto_approve_faults", [])


def empty_scores():
    return {fault: {"detected": 0, "rules_correct": 0, "llm_correct": 0}
            for fault in FAULT_ORDER}


def print_scoring_table(scores):
    print("\n  Ground-truth scoring:")
    print(f"  {'true fault':16s} {'detected':>8s} {'rules correct':>13s} {'LLM correct':>11s}")
    for fault in FAULT_ORDER:
        row = scores[fault]
        print(f"  {fault:16s} {row['detected']:8d} {row['rules_correct']:13d} {row['llm_correct']:11d}")


def run(cfg, quiet=False, show_summary=True, run_suffix="", source="simulator",
        pm_file=None, pace_seconds=0):
    lc, th = cfg["loop"], cfg["detect"]["thresholds"]
    if source == "real":
        if not pm_file:
            raise ValueError("--source real requires --pm-file <scrubbed.csv>")
        sim = PMDataReader(pm_file)
        t = sim.first_rop()
    else:
        sim = NetworkSimulator(cfg["simulator"])
        t = datetime.now().replace(second=0, microsecond=0)
    # Pacing is demo-only for real PM replay; ignored for the simulator.
    pacing = source == "real" and pace_seconds > 0
    now = datetime.now()
    run_id = now.strftime("%Y%m%d_%H%M%S") + run_suffix
    log = AuditLog(cfg["output"]["log_dir"], run_id)
    # Let the dashboard discover the cell inventory for this run (real PM
    # uses SITE001… labels, not the simulator's SITE001_A sector names).
    log.write(ts=t, stage="meta", source=source, cells=list(sim.cells),
              n_cells=len(sim.cells), pace_seconds=pace_seconds if pacing else 0)
    pending = {}
    fail_counts = {}  # (cell, fault_type) -> consecutive verify failures
    scores = empty_scores()
    missed = {fault: 0 for fault in FAULT_ORDER if fault != "none"}
    stats = {"detected": 0, "actions": 0, "resolved": 0, "unresolved": 0,
             "review_only": 0, "escalated": 0, "shadow_real_total": 0,
             "shadow_real_agree": 0, "shadow_ml_total": 0, "shadow_ml_agree": 0}

    if not quiet:
        if lc.get("auto_approve_all"):
            gate = "ALL (--auto)"
        else:
            gate = lc.get("auto_approve_faults", [])
        pace_note = f" | pace: {pace_seconds}s" if pacing else ""
        print(f"Diagnosis mode: {cfg['diagnose']['mode']} | auto_approve_faults: {gate}"
              f" | source: {source}{pace_note}")
    for c in range(1, lc["cycles"] + 1):
        df = sim.collect(t)
        if not quiet:
            if pacing:
                print(f"\n=== Replaying real production PM data: Cycle {c} — "
                      f"ROP {t:%Y-%m-%d %H:%M} ({len(df)} cells) ===")
            elif source == "real":
                print(f"\n=== Cycle {c}  ROP {t:%Y-%m-%d %H:%M} ===")
            else:
                print(f"\n=== Cycle {c}  ROP {t:%H:%M} ===")

        # VERIFY actions taken last cycle
        for cell, (fault, prev, truth) in list(pending.items()):
            new = df[df.cell == cell].iloc[0]
            v = verify(prev, new, fault, th)
            tag = "RESOLVED" if v["resolved"] else f"NOT RESOLVED {v['remaining_breaches']}, escalate or roll back"
            if not quiet:
                print(f"  VERIFY {cell}: {v['kpi']} {v['before']} -> {v['after']}  {tag}")
            stats["resolved" if v["resolved"] else "unresolved"] += 1
            if not v["resolved"]:
                key = (cell, fault)
                fail_counts[key] = fail_counts.get(key, 0) + 1
            log.write(ts=t, stage="verify", cell=cell, fault=fault, **truth, **v)
            del pending[cell]

        anomalies = detect(df, cfg["detect"])
        if c == 1:
            detected_cells = set(anomalies["cell"])
            for cell in sim.cells:
                true_fault = sim.true_fault(cell)
                if true_fault != "none" and cell not in detected_cells:
                    missed[true_fault] += 1
        if anomalies.empty and not quiet:
            print("  No anomalies.")
        for _, r in anomalies.iterrows():
            stats["detected"] += 1
            d = diagnose(r, cfg["diagnose"])
            true_fault = sim.true_fault(r["cell"])
            expected_label = EXPECTED_LABEL[true_fault]
            rules_fault, _ = rule_diagnose(r)
            rules_correct = rules_fault == expected_label
            mode = cfg["diagnose"]["mode"]
            if mode == "shadow":
                llm_fault = d.get("llm_fault")
            elif mode == "llm" and d["source"] == "llm":
                llm_fault = d["fault_type"]
            else:
                llm_fault = None
            llm_correct = mode in ("shadow", "llm") and llm_fault == expected_label
            score = scores[true_fault]
            score["detected"] += 1
            score["rules_correct"] += rules_correct
            score["llm_correct"] += llm_correct
            truth = {
                "true_fault": true_fault,
                "expected_label": expected_label,
                "rules_correct": rules_correct,
                "llm_correct": llm_correct,
            }
            if not quiet:
                print(f"  DETECT {r['cell']}: breaches={r['breaches']} ml={bool(r['ml_anomaly'])}")
                print(f"    RCA [{d['source']}]: {d['fault_type']} | {d['rca']}")
            if "agree" in d:
                group = "real" if r["breaches"] else "ml"
                stats[f"shadow_{group}_total"] += 1
                stats[f"shadow_{group}_agree"] += d["agree"]
                if not quiet:
                    print(f"    LLM shadow: {d['llm_fault']} ({'agrees' if d['agree'] else 'DISAGREES'})")
            if "llm_error" in d and not quiet:
                print(f"    LLM unavailable, used rules: {d['llm_error']}")

            cmd = command_for(d["fault_type"], r["cell"])
            if cmd is None:
                if not quiet:
                    print("    No automated action. Queued for engineer review.")
                stats["review_only"] += 1
                log.write(ts=t, stage="review", cell=r["cell"], **truth, **d)
                continue
            fail_key = (r["cell"], d["fault_type"])
            n_fail = fail_counts.get(fail_key, 0)
            if n_fail >= MAX_RETRIES:
                if not quiet:
                    print(f"  ESCALATE {r['cell']}: {d['fault_type']} failed verification "
                          f"{n_fail} times. No further automated action. "
                          f"Routed to engineer escalation.")
                stats["escalated"] += 1
                log.write(ts=t, stage="escalate", cell=r["cell"],
                          failure_count=n_fail, **truth, **d)
                continue
            auto = auto_approve_for(d["fault_type"], lc)
            ok = True if quiet and auto else approve(
                r["cell"], d["rca"], cmd, auto)
            log.write(ts=t, stage="act", cell=r["cell"], cmd=cmd, approved=ok,
                      **truth, **d)
            if ok:
                stats["actions"] += 1
                execute(sim, r["cell"], d["fault_type"], cmd)
                pending[r["cell"]] = (d["fault_type"], r, truth)
        if source == "real":
            try:
                t = sim.next_rop(t)
            except ValueError as exc:
                if not quiet:
                    print(f"\n  Stopping early: {exc}")
                break
        else:
            t += timedelta(minutes=lc["rop_minutes"])

        # Pause between cycles so a live viewer can follow detect → act → verify.
        if pacing and c < lc["cycles"]:
            if not quiet:
                print(f"  … pacing {pace_seconds}s before next cycle …")
            time.sleep(pace_seconds)

    log.close()
    if show_summary:
        print("\n=== Summary ===")
        for k, v in stats.items():
            if not k.startswith("shadow"):
                print(f"  {k:12s} {v}")
        if stats["shadow_real_total"]:
            pct = 100 * stats["shadow_real_agree"] / stats["shadow_real_total"]
            print(f"  LLM agreement on real faults: {stats['shadow_real_agree']}/{stats['shadow_real_total']} ({pct:.0f}%)")
        if stats["shadow_ml_total"]:
            pct = 100 * stats["shadow_ml_agree"] / stats["shadow_ml_total"]
            print(f"  LLM agreement on ML-only cells: {stats['shadow_ml_agree']}/{stats['shadow_ml_total']} ({pct:.0f}%)")
        print_scoring_table(scores)
        missed_text = ", ".join(f"{fault}={count}" for fault, count in missed.items() if count)
        print(f"\n  Missed detections in cycle 1: {missed_text or 'none'}")
        print(f"  Audit log: {log.path}")
    return {"scores": scores, "missed": missed}


def parse_seed_range(value):
    try:
        start, end = (int(part) for part in value.split("-", 1))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("seeds must be an inclusive range like 1-10") from exc
    if start > end:
        raise argparse.ArgumentTypeError("seed range start must not exceed its end")
    return range(start, end + 1)


def print_and_save_combined(combined, missed, log_dir):
    print("\n=== Combined scoring across seeds ===")
    print(f"{'true fault':16s} {'detected':>8s} {'rules accuracy':>15s} "
          f"{'LLM accuracy':>13s} {'missed':>8s}")
    rows = []
    for fault in FAULT_ORDER:
        score = combined[fault]
        detected = score["detected"]
        rules_pct = 100 * score["rules_correct"] / detected if detected else 0
        llm_pct = 100 * score["llm_correct"] / detected if detected else 0
        missed_count = missed.get(fault, 0)
        print(f"{fault:16s} {detected:8d} {rules_pct:14.1f}% "
              f"{llm_pct:12.1f}% {missed_count:8d}")
        rows.append({
            "true_fault": fault,
            "detected": detected,
            "rules_accuracy_pct": round(rules_pct, 1),
            "llm_accuracy_pct": round(llm_pct, 1),
            "missed_detections": missed_count,
        })

    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(log_dir, f"combined_scoring_{timestamp}.csv")
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"Combined CSV: {path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--cycles", type=int)
    p.add_argument("--auto", action="store_true")
    p.add_argument("--mode", choices=["rules", "llm", "shadow"])
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--seeds", type=parse_seed_range, metavar="START-END")
    p.add_argument("--source", choices=["simulator", "real"], default="simulator")
    p.add_argument("--pm-file", help="Scrubbed PM CSV (required with --source real)")
    p.add_argument("--pace-seconds", type=float, default=0,
                   help="Seconds to sleep between cycles (real-data mode only; "
                        "0 = no delay). Useful for live demos.")
    a = p.parse_args()
    cfg = load_config(a.config)
    if a.cycles: cfg["loop"]["cycles"] = a.cycles
    if a.auto: cfg["loop"]["auto_approve_all"] = True
    if a.mode: cfg["diagnose"]["mode"] = a.mode
    if a.source == "real" and not a.pm_file:
        p.error("--source real requires --pm-file <path to scrubbed CSV>")
    if a.seeds and a.source == "real":
        p.error("--seeds is only supported with the simulator source")
    if a.pace_seconds < 0:
        p.error("--pace-seconds must be >= 0")
    if a.seeds:
        combined = empty_scores()
        combined_missed = {fault: 0 for fault in FAULT_ORDER if fault != "none"}
        for seed in a.seeds:
            seed_cfg = copy.deepcopy(cfg)
            seed_cfg["simulator"]["seed"] = seed
            seed_cfg["loop"]["auto_approve_all"] = True
            result = run(seed_cfg, quiet=True, show_summary=False,
                         run_suffix=f"_seed{seed}")
            for fault in FAULT_ORDER:
                for key in combined[fault]:
                    combined[fault][key] += result["scores"][fault][key]
            for fault in combined_missed:
                combined_missed[fault] += result["missed"][fault]
        print_and_save_combined(combined, combined_missed, cfg["output"]["log_dir"])
    else:
        run(cfg, quiet=a.quiet, source=a.source, pm_file=a.pm_file,
            pace_seconds=a.pace_seconds)
