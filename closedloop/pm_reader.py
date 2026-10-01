"""COLLECT stage backed by a scrubbed real PM CSV (replay, not live).

Drop-in alternative to NetworkSimulator: same collect() / apply() / cells /
true_fault() surface so main.py can swap sources without touching detect,
diagnose, or verify.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from .simulator import KPI_COLUMNS

OUT_COLUMNS = ["rop", "cell"] + KPI_COLUMNS
OPTIONAL_COLUMNS = ["cell_availability_pct"]


class PMDataReader:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"PM scrubbed CSV not found: {self.path}")
        df = pd.read_csv(self.path)
        required = set(OUT_COLUMNS)
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"PM file missing columns: {sorted(missing)}")
        df["rop"] = df["rop"].astype(str)
        self.df = df
        self._out_cols = OUT_COLUMNS + [c for c in OPTIONAL_COLUMNS if c in df.columns]
        self.cells = sorted(df["cell"].astype(str).unique().tolist())
        self._rops = sorted(df["rop"].unique().tolist())
        if not self._rops:
            raise ValueError(f"PM file has no ROP rows: {self.path}")

    def first_rop(self) -> datetime:
        return self._parse_rop(self._rops[0])

    def next_rop(self, rop_time: datetime) -> datetime:
        """Return the next ROP after rop_time, or raise if this was the last."""
        key = self._format_rop(rop_time)
        # Prefer exact key; else match by date prefix for daily exports
        try:
            idx = self._rops.index(key)
        except ValueError:
            day = rop_time.strftime("%Y-%m-%d")
            matches = [i for i, r in enumerate(self._rops) if r.startswith(day)]
            if not matches:
                raise ValueError(
                    f"Current ROP {key} not in PM data "
                    f"(range {self._rops[0]} .. {self._rops[-1]})"
                )
            idx = matches[-1]
        if idx + 1 >= len(self._rops):
            raise ValueError(
                f"No further ROPs after {key}. "
                f"File ends at {self._rops[-1]} ({len(self._rops)} unique ROPs)."
            )
        return self._parse_rop(self._rops[idx + 1])

    def collect(self, rop_time):
        key = self._format_rop(rop_time)
        chunk = self.df[self.df["rop"] == key]
        if chunk.empty:
            day = rop_time.strftime("%Y-%m-%d")
            chunk = self.df[self.df["rop"].str.startswith(day)]
        if chunk.empty:
            raise ValueError(
                f"No PM rows for ROP {key}. "
                f"Available range: {self._rops[0]} .. {self._rops[-1]} "
                f"({len(self._rops)} unique ROPs)."
            )
        out = chunk[self._out_cols].copy()
        # Normalize displayed rop to the requested timestamp format
        out["rop"] = key if (self.df["rop"] == key).any() else out["rop"]
        return out.reset_index(drop=True)

    def true_fault(self, cell):
        """Real PM has no injected ground-truth labels."""
        return "none"

    def apply(self, cell, fault_type):
        """No-op: replaying historical PM cannot remediate a live network."""
        print(f"    [pm replay] apply({cell}, {fault_type}) skipped — "
              f"historical data, not a live network")

    @staticmethod
    def _format_rop(rop_time: datetime) -> str:
        return rop_time.strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _parse_rop(value: str) -> datetime:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        return pd.to_datetime(value).to_pydatetime()
