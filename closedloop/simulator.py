"""COLLECT stage. Stand-in for ENM PM counter exports, one DataFrame per ROP.
Swap this module for a real file reader later; keep the same column names."""
import random
import numpy as np
import pandas as pd

KPI_COLUMNS = ["drop_rate_pct", "rrc_success_pct", "ul_noise_dbm",
               "prb_util_pct", "dl_tput_mbps"]

class NetworkSimulator:
    def __init__(self, cfg):
        random.seed(cfg["seed"])
        np.random.seed(cfg["seed"])
        self.cells = [f"SITE{s:03d}_{sec}" for s in range(1, cfg["sites"] + 1) for sec in "ABC"]
        self.faults = dict(cfg["faults"])
        self.fix_rate = cfg["fix_success_rate"]

    def collect(self, rop_time):
        rows = []
        for cell in self.cells:
            r = {
                "rop": rop_time.strftime("%Y-%m-%d %H:%M"), "cell": cell,
                "drop_rate_pct": np.random.normal(0.6, 0.15),
                "rrc_success_pct": np.random.normal(99.3, 0.3),
                "ul_noise_dbm": np.random.normal(-117, 1.5),
                "prb_util_pct": np.random.normal(45, 10),
                "dl_tput_mbps": np.random.normal(28, 4),
            }
            fault = self.faults.get(cell)
            if fault == "ul_interference":
                r["ul_noise_dbm"] += 15; r["rrc_success_pct"] -= 4; r["drop_rate_pct"] += 1.2
            elif fault == "overshoot":
                r["drop_rate_pct"] += 2.5; r["dl_tput_mbps"] -= 8
            elif fault == "congestion":
                r["prb_util_pct"] = np.random.normal(94, 2); r["dl_tput_mbps"] -= 18
            elif fault == "combined":
                r["ul_noise_dbm"] += 15; r["rrc_success_pct"] -= 4; r["drop_rate_pct"] += 1.2
                r["prb_util_pct"] = np.random.normal(94, 2); r["dl_tput_mbps"] -= 18
            elif fault == "sleeping_cell":
                r["prb_util_pct"] = max(0, np.random.normal(2, 1))
                r["dl_tput_mbps"] = max(0, np.random.normal(0.5, 0.3))
                r["drop_rate_pct"] = max(0, np.random.normal(0.1, 0.05))
            elif fault == "pim":
                r["ul_noise_dbm"] += 12; r["drop_rate_pct"] += 1.5
                r["prb_util_pct"] = np.random.normal(75, 3)
            rows.append(r)
        return pd.DataFrame(rows).round(2)

    def true_fault(self, cell):
        return self.faults.get(cell, "none")

    def apply(self, cell, fault_type):
        """Network 'responds' to a change. Only clears the fault if the right fix was chosen."""
        if self.faults.get(cell) == fault_type and random.random() < self.fix_rate:
            del self.faults[cell]
