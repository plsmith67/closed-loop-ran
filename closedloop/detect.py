"""DETECT stage. Rule-based thresholds plus Isolation Forest."""
from sklearn.ensemble import IsolationForest
from .simulator import KPI_COLUMNS

def breaches_for(row, thresholds):
    return [k for k, (op, v) in thresholds.items()
            if (op == "gt" and row[k] > v) or (op == "lt" and row[k] < v)]

def detect(df, cfg):
    df = df.copy()
    th = cfg["thresholds"]
    df["breaches"] = [breaches_for(r, th) for _, r in df.iterrows()]
    if cfg["ml"]["enabled"]:
        iso = IsolationForest(contamination=cfg["ml"]["contamination"], random_state=0)
        df["ml_anomaly"] = iso.fit_predict(df[KPI_COLUMNS]) == -1
        df["ml_score"] = iso.score_samples(df[KPI_COLUMNS]).round(3)
    else:
        df["ml_anomaly"] = False
        df["ml_score"] = 0.0
    return df[(df["breaches"].str.len() > 0) | df["ml_anomaly"]]
