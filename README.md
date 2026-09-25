# Closed-Loop RAN Anomaly Prototype

Personal learning prototype using simulated data. Not affiliated with any operator or vendor.

A learning prototype of the closed loop used in O-RAN rApps and TM Forum Autonomous Networks Level 3/4:

```
COLLECT -> DETECT -> DIAGNOSE -> DECIDE -> APPROVE -> ACT -> VERIFY -> repeat
```

It simulates 24 LTE cells with injected faults (UL interference, overshoot, congestion), detects them with
threshold rules plus an Isolation Forest, diagnoses root cause with rules and/or a local LLM, proposes cmedit
changes, gates them behind human approval, and verifies the KPI recovered on the next ROP.

## Setup (laptop or Jetson)

```bash
git clone <your-repo-url> closed-loop-ran && cd closed-loop-ran
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Run modes

| Command | What it does |
|---|---|
| `python main.py` | Rules diagnose, you approve each action (Level 3) |
| `python main.py --auto` | No human gate (Level 4 style) |
| `python main.py --mode shadow --auto` | Rules act, LLM runs alongside, agreement is scored |
| `python main.py --mode llm --auto` | LLM drives diagnosis, rules are the fallback |
| `python main.py --cycles 8` | Run more ROPs |

Every run writes an audit trail to `logs/run_<timestamp>.jsonl`.

## Adding the local LLM

Install Ollama (https://ollama.com), then:

```bash
ollama pull llama3.2:3b
python main.py --mode shadow --auto
```

If Ollama is not running, the loop keeps working on rules and logs the failure.

## Moving to the Jetson

Clone the repo on the Jetson and follow the same setup. To run the model on the Jetson but the loop on your
laptop, start Ollama on the Jetson with `OLLAMA_HOST=0.0.0.0 ollama serve` and set `diagnose.llm.url` in
`config.yaml` to `http://<jetson-ip>:11434/api/generate`.

## Design notes

- **Guardrail:** the LLM only classifies the fault. The actual cmedit command always comes from the
  approved template table in `closedloop/diagnose.py`, and unexpected LLM output is forced to `unknown`.
- **ML-only anomalies are never auto-actioned.** They go to engineer review, since Isolation Forest
  flags some healthy cells every cycle (false positives by design, worth watching).
- **Shadow mode** is how you evaluate an AI component before trusting it with actions.

## Layout

```
main.py                  loop orchestration and summary
config.yaml              thresholds, faults, mode, LLM endpoint
closedloop/simulator.py  COLLECT  (swap for a real PM export reader)
closedloop/detect.py     DETECT   (rules + Isolation Forest)
closedloop/diagnose.py   DIAGNOSE + DECIDE (rules, LLM, shadow, action templates)
closedloop/act.py        APPROVE + ACT + audit log
closedloop/verify.py     VERIFY
```

## Roadmap

1. Replace the simulator with real (scrubbed) PM counter exports
2. Run shadow mode on the Jetson and compare model sizes
3. FastAPI service plus Prometheus metrics and a Grafana dashboard
4. Containerize and deploy on k3s
5. Map the design to an O-RAN rApp on the Non-RT RIC (R1 interface)
