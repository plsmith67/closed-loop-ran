"""Closed-loop RAN prototype entry point.

COLLECT -> DETECT -> DIAGNOSE -> DECIDE -> APPROVE -> ACT -> VERIFY -> repeat

Examples:
  python main.py                      # uses config.yaml, asks before each action
  python main.py --auto               # Level 4 style, no human gate
  python main.py --mode shadow --auto # rules act, LLM graded alongside
"""
import argparse
from datetime import datetime, timedelta

from closedloop.config import load_config
from closedloop.simulator import NetworkSimulator
from closedloop.detect import detect
from closedloop.diagnose import diagnose, command_for
from closedloop.act import AuditLog, approve, execute
from closedloop.verify import verify

def run(cfg):
    lc, th = cfg["loop"], cfg["detect"]["thresholds"]
    sim = NetworkSimulator(cfg["simulator"])
    now = datetime.now()
    run_id = now.strftime("%Y%m%d_%H%M%S")
    t = now.replace(second=0, microsecond=0)
    log = AuditLog(cfg["output"]["log_dir"], run_id)
    pending = {}
    stats = {"detected": 0, "actions": 0, "resolved": 0, "unresolved": 0,
             "review_only": 0, "shadow_real_total": 0, "shadow_real_agree": 0,
             "shadow_ml_total": 0, "shadow_ml_agree": 0}

    print(f"Diagnosis mode: {cfg['diagnose']['mode']} | auto_approve: {lc['auto_approve']}")
    for c in range(1, lc["cycles"] + 1):
        print(f"\n=== Cycle {c}  ROP {t:%H:%M} ===")
        df = sim.collect(t)

        # VERIFY actions taken last cycle
        for cell, (fault, prev) in list(pending.items()):
            new = df[df.cell == cell].iloc[0]
            v = verify(prev, new, fault, th)
            tag = "RESOLVED" if v["resolved"] else f"NOT RESOLVED {v['remaining_breaches']}, escalate or roll back"
            print(f"  VERIFY {cell}: {v['kpi']} {v['before']} -> {v['after']}  {tag}")
            stats["resolved" if v["resolved"] else "unresolved"] += 1
            log.write(ts=t, stage="verify", cell=cell, fault=fault, **v)
            del pending[cell]

        anomalies = detect(df, cfg["detect"])
        if anomalies.empty:
            print("  No anomalies.")
        for _, r in anomalies.iterrows():
            stats["detected"] += 1
            d = diagnose(r, cfg["diagnose"])
            print(f"  DETECT {r['cell']}: breaches={r['breaches']} ml={bool(r['ml_anomaly'])}")
            print(f"    RCA [{d['source']}]: {d['fault_type']} | {d['rca']}")
            if "agree" in d:
                group = "real" if r["breaches"] else "ml"
                stats[f"shadow_{group}_total"] += 1
                stats[f"shadow_{group}_agree"] += d["agree"]
                print(f"    LLM shadow: {d['llm_fault']} ({'agrees' if d['agree'] else 'DISAGREES'})")
            if "llm_error" in d:
                print(f"    LLM unavailable, used rules: {d['llm_error']}")

            cmd = command_for(d["fault_type"], r["cell"])
            if cmd is None:
                print("    No automated action. Queued for engineer review.")
                stats["review_only"] += 1
                log.write(ts=t, stage="review", cell=r["cell"], **d)
                continue
            ok = approve(r["cell"], d["rca"], cmd, lc["auto_approve"])
            log.write(ts=t, stage="act", cell=r["cell"], cmd=cmd, approved=ok, **d)
            if ok:
                stats["actions"] += 1
                execute(sim, r["cell"], d["fault_type"], cmd)
                pending[r["cell"]] = (d["fault_type"], r)
        t += timedelta(minutes=lc["rop_minutes"])

    log.close()
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
    print(f"  Audit log: {log.path}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--cycles", type=int)
    p.add_argument("--auto", action="store_true")
    p.add_argument("--mode", choices=["rules", "llm", "shadow"])
    a = p.parse_args()
    cfg = load_config(a.config)
    if a.cycles: cfg["loop"]["cycles"] = a.cycles
    if a.auto: cfg["loop"]["auto_approve"] = True
    if a.mode: cfg["diagnose"]["mode"] = a.mode
    run(cfg)
