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
from datetime import datetime, timedelta

from closedloop.config import load_config
from closedloop.simulator import NetworkSimulator
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


def empty_scores():
    return {fault: {"detected": 0, "rules_correct": 0, "llm_correct": 0}
            for fault in FAULT_ORDER}


def print_scoring_table(scores):
    print("\n  Ground-truth scoring:")
    print(f"  {'true fault':16s} {'detected':>8s} {'rules correct':>13s} {'LLM correct':>11s}")
    for fault in FAULT_ORDER:
        row = scores[fault]
        print(f"  {fault:16s} {row['detected']:8d} {row['rules_correct']:13d} {row['llm_correct']:11d}")


def run(cfg, quiet=False, show_summary=True, run_suffix=""):
    lc, th = cfg["loop"], cfg["detect"]["thresholds"]
    sim = NetworkSimulator(cfg["simulator"])
    now = datetime.now()
    run_id = now.strftime("%Y%m%d_%H%M%S") + run_suffix
    t = now.replace(second=0, microsecond=0)
    log = AuditLog(cfg["output"]["log_dir"], run_id)
    pending = {}
    fail_counts = {}  # (cell, fault_type) -> consecutive verify failures
    scores = empty_scores()
    missed = {fault: 0 for fault in FAULT_ORDER if fault != "none"}
    stats = {"detected": 0, "actions": 0, "resolved": 0, "unresolved": 0,
             "review_only": 0, "escalated": 0, "shadow_real_total": 0,
             "shadow_real_agree": 0, "shadow_ml_total": 0, "shadow_ml_agree": 0}

    if not quiet:
        print(f"Diagnosis mode: {cfg['diagnose']['mode']} | auto_approve: {lc['auto_approve']}")
    for c in range(1, lc["cycles"] + 1):
        if not quiet:
            print(f"\n=== Cycle {c}  ROP {t:%H:%M} ===")
        df = sim.collect(t)

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
            ok = True if quiet and lc["auto_approve"] else approve(
                r["cell"], d["rca"], cmd, lc["auto_approve"])
            log.write(ts=t, stage="act", cell=r["cell"], cmd=cmd, approved=ok,
                      **truth, **d)
            if ok:
                stats["actions"] += 1
                execute(sim, r["cell"], d["fault_type"], cmd)
                pending[r["cell"]] = (d["fault_type"], r, truth)
        t += timedelta(minutes=lc["rop_minutes"])

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
    a = p.parse_args()
    cfg = load_config(a.config)
    if a.cycles: cfg["loop"]["cycles"] = a.cycles
    if a.auto: cfg["loop"]["auto_approve"] = True
    if a.mode: cfg["diagnose"]["mode"] = a.mode
    if a.seeds:
        combined = empty_scores()
        combined_missed = {fault: 0 for fault in FAULT_ORDER if fault != "none"}
        for seed in a.seeds:
            seed_cfg = copy.deepcopy(cfg)
            seed_cfg["simulator"]["seed"] = seed
            seed_cfg["loop"]["auto_approve"] = True
            result = run(seed_cfg, quiet=True, show_summary=False,
                         run_suffix=f"_seed{seed}")
            for fault in FAULT_ORDER:
                for key in combined[fault]:
                    combined[fault][key] += result["scores"][fault][key]
            for fault in combined_missed:
                combined_missed[fault] += result["missed"][fault]
        print_and_save_combined(combined, combined_missed, cfg["output"]["log_dir"])
    else:
        run(cfg, quiet=a.quiet)
