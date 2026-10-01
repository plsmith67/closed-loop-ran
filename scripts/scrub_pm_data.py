#!/usr/bin/env python3
"""Scrub a raw Ericsson eNodeB PM CSV into closed-loop column names.

Usage:
  python scripts/scrub_pm_data.py /path/to/raw_export.csv

Writes:
  data/pm_sample_scrubbed.csv   (safe to share; no site IDs)
  data/pm_data_id_map.csv       (PRIVATE mapping; never commit)

Never commit the raw export or the id map.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data"
SCRUBBED_PATH = OUT_DIR / "pm_sample_scrubbed.csv"
MAP_PATH = OUT_DIR / "pm_data_id_map.csv"

DROP_COLS = ["SubNetwork1", "SubNetwork2", "SubNetwork3", "SubNetwork4"]
RENAME = {
    "ObjectId": "cell_raw",  # temporary; replaced by SITE### then dropped
    "RRC Success Rate (%)": "rrc_success_pct",
    "Ret_ERAB_DropRate (%)": "drop_rate_pct",
    "N+I (Pusch)": "ul_noise_dbm",
    "Downlink Cell Throughput (Mbps)": "dl_tput_mbps",
    "Cell Availability (%)": "cell_availability_pct",
    "RecordDate Local": "rop",
}
KEEP = [
    "rop", "cell", "rrc_success_pct", "drop_rate_pct",
    "ul_noise_dbm", "dl_tput_mbps", "prb_util_pct", "cell_availability_pct",
]


def find_traffic_volume_column(columns):
    """Locate a downlink traffic-volume column for the PRB placeholder."""
    exact = [
        "DL Traffic Volume",
        "Downlink Traffic Volume",
        "Downlink Traffic Volume (Mbyte)",
        "Downlink Traffic Volume (MB)",
        "Downlink Traffic Volume (MByte)",
    ]
    for name in exact:
        if name in columns:
            return name
    lowered = {c: c.lower() for c in columns}
    for c, low in lowered.items():
        if "traffic" in low and "volume" in low and ("down" in low or "dl" in low):
            return c
    for c, low in lowered.items():
        if "traffic" in low and "volume" in low:
            return c
    return None


def main():
    p = argparse.ArgumentParser(description="Scrub Ericsson eNodeB PM CSV for closed-loop-ran")
    p.add_argument("raw_csv", type=Path, help="Path to the raw PM export (never commit this file)")
    p.add_argument("--out", type=Path, default=SCRUBBED_PATH, help="Scrubbed CSV output path")
    p.add_argument("--map-out", type=Path, default=MAP_PATH, help="Private ObjectId→SITE map path")
    args = p.parse_args()

    if not args.raw_csv.is_file():
        sys.exit(f"Raw CSV not found: {args.raw_csv}")

    df = pd.read_csv(args.raw_csv)
    missing = [c for c in ["ObjectId", "RecordDate Local"] if c not in df.columns]
    # Required KPI columns after rename keys (except ObjectId handled separately)
    for src in RENAME:
        if src == "ObjectId":
            continue
        if src not in df.columns:
            missing.append(src)
    traffic_col = find_traffic_volume_column(df.columns)
    if traffic_col is None:
        missing.append("DL Traffic Volume (or similar)")
    if missing:
        sys.exit("Missing required columns: " + ", ".join(missing))

    # Drop identifying subnet columns if present
    df = df.drop(columns=[c for c in DROP_COLS if c in df.columns], errors="ignore")

    # Stable ObjectId → SITE001, SITE002, ...
    unique_ids = sorted(df["ObjectId"].astype(str).unique())
    id_map = {oid: f"SITE{i:03d}" for i, oid in enumerate(unique_ids, start=1)}
    map_df = pd.DataFrame(
        {"ObjectId": list(id_map.keys()), "cell": list(id_map.values())}
    )
    args.map_out.parent.mkdir(parents=True, exist_ok=True)
    map_df.to_csv(args.map_out, index=False)

    df = df.rename(columns={k: v for k, v in RENAME.items() if k != "ObjectId"})
    df["cell"] = df["ObjectId"].astype(str).map(id_map)
    df = df.drop(columns=["ObjectId"])

    # Placeholder PRB util: normalize downlink traffic volume to 0–100 vs file max.
    # This is inferred from traffic volume, not a real PRB counter, and should be
    # replaced once a PRB-inclusive export is available.
    traffic = pd.to_numeric(df[traffic_col], errors="coerce").fillna(0.0)
    tmax = float(traffic.max()) if len(traffic) else 0.0
    if tmax > 0:
        df["prb_util_pct"] = (100.0 * traffic / tmax).clip(0, 100)
    else:
        df["prb_util_pct"] = 0.0

    # Normalize ROP timestamps to a consistent string
    rop = pd.to_datetime(df["rop"], errors="coerce")
    if rop.isna().any():
        sys.exit("Failed to parse some RecordDate Local / rop values")
    # Daily exports often have date-only; keep midnight time for matching
    df["rop"] = rop.dt.strftime("%Y-%m-%d %H:%M")

    for col in ("rrc_success_pct", "drop_rate_pct", "ul_noise_dbm",
                "dl_tput_mbps", "prb_util_pct", "cell_availability_pct"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    out = df[KEEP].copy().round(2)
    banned = [c for c in out.columns if c.lower().startswith("subnetwork") or c == "ObjectId"]
    if banned:
        sys.exit(f"Identifying columns still present in output: {banned}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.out, index=False)

    print("Scrub complete.")
    print(f"  rows:       {len(out)}")
    print(f"  cells:      {out['cell'].nunique()}")
    print(f"  date range: {out['rop'].min()} -> {out['rop'].max()}")
    print(f"  columns:    {list(out.columns)}")
    print(f"  traffic→PRB placeholder source column: {traffic_col!r}")
    print("  NOTE: cell_availability_pct is scrubbed for analysis but is not "
          "used by any active detection rule (noisy daily samples; see README).")
    print("  NOTE: re-run this scrubber after column-set changes so "
          "data/pm_sample_scrubbed.csv includes cell_availability_pct.")
    print(f"  wrote:      {args.out}")
    print(f"  id map:     {args.map_out}  (PRIVATE — do not commit)")
    print("  identifying SubNetwork*/ObjectId columns absent from scrubbed output: OK")


if __name__ == "__main__":
    main()
