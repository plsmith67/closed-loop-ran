"""DETECT stage. Rule-based thresholds plus Isolation Forest."""
from sklearn.ensemble import IsolationForest
from .simulator import KPI_COLUMNS

def breaches_for(row, thresholds):
    return [k for k, (op, v) in thresholds.items()
            if k in row and ((op == "gt" and row[k] > v) or (op == "lt" and row[k] < v))]

def detect(df, cfg):
    df = df.copy()
    th = cfg["thresholds"]
    # sleeping_cell_prb is not a KPI column; apply it via the pattern check below.
    kpi_thresholds = {k: v for k, v in th.items() if k != "sleeping_cell_prb"}
    prb_limit = th["sleeping_cell_prb"][1]
    # Real PM scrub includes cell_availability_pct; the synthetic simulator does not.
    has_availability = "cell_availability_pct" in df.columns
    avail_limit = 90.0

    def breaches_with_sleeping(r):
        breaches = breaches_for(r, kpi_thresholds)
        if has_availability:
            # Disabled — Cell Availability (%) in this network's export is computed
            # from a small number of samples per day, producing noisy values
            # (e.g. 33.33%, 66.67%) that don't reliably distinguish a real outage
            # from a single missed check. A flat threshold here produced far too
            # many false positives in testing against real data (see README).
            # Revisit if a higher-resolution availability counter becomes available.
            # if r["cell_availability_pct"] < avail_limit:
            #     breaches = list(breaches) + ["cell_down_pattern"]
            pass
        else:
            # Synthetic simulator: near-zero traffic pattern (unchanged).
            if (r["prb_util_pct"] < prb_limit
                    and r["dl_tput_mbps"] < 1.0
                    and r["drop_rate_pct"] < 0.5):
                breaches = list(breaches) + ["sleeping_cell_pattern"]
        return breaches

    df["breaches"] = [breaches_with_sleeping(r) for _, r in df.iterrows()]
    if cfg["ml"]["enabled"]:
        iso = IsolationForest(contamination=cfg["ml"]["contamination"], random_state=0)
        df["ml_anomaly"] = iso.fit_predict(df[KPI_COLUMNS]) == -1
        df["ml_score"] = iso.score_samples(df[KPI_COLUMNS]).round(3)
    else:
        df["ml_anomaly"] = False
        df["ml_score"] = 0.0
    return df[(df["breaches"].str.len() > 0) | df["ml_anomaly"]]
