"""APPROVE + ACT stage, plus the audit log."""
import json, os

class AuditLog:
    def __init__(self, log_dir, run_id):
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, f"run_{run_id}.jsonl")
        self.f = open(self.path, "w")

    def write(self, **event):
        self.f.write(json.dumps(event, default=str) + "\n")
        self.f.flush()

    def close(self):
        self.f.close()

def approve(cell, rca, cmd, auto):
    print(f"    Proposed: {cmd}")
    if auto:
        print("    [auto-approved]")
        return True
    return input("    Approve? [y/N] ").strip().lower() == "y"

def execute(sim, cell, fault_type, cmd):
    """In production this would call the ENM northbound REST API.
    Here we let the simulator react instead."""
    sim.apply(cell, fault_type)
