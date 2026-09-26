# Closed-Loop RAN Anomaly Prototype

Personal learning prototype using simulated data. Not affiliated with any operator or vendor.

A learning prototype of the closed loop used in O-RAN rApps and TM Forum Autonomous Networks Level 3/4:

```
COLLECT -> DETECT -> DIAGNOSE -> DECIDE -> APPROVE -> ACT -> VERIFY -> repeat
```

It simulates 24 LTE cells with injected faults, detects them with threshold rules plus an Isolation
Forest, diagnoses root cause with rules and/or a local LLM, proposes cmedit changes, gates them
behind human approval, verifies recovery on the next ROP, and escalates cell/fault pairs that fail
verification repeatedly.

## Injected faults

| Fault | Typical signature | Auto-action |
|---|---|---|
| `ul_interference` | High `ul_noise_dbm`, often low `rrc_success_pct` | Yes (mitigation + field ticket) |
| `overshoot` | High `drop_rate_pct`, normal UL noise | Yes (downtilt) |
| `congestion` | High `prb_util_pct`, low throughput | Yes (CIO offload) |
| `combined` | Multiple fault patterns on one cell | Review only (rules label as `ul_interference` first) |
| `pim` | UL noise rises with DL load; RRC usually OK, drop rate up | Review only today (rules still map noise → `ul_interference`) |
| `sleeping_cell` | On-air, almost no traffic/throughput | Review only; often missed by ML at current contamination |

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
| `python main.py --mode shadow --seeds 1-10` | Quiet multi-seed ground-truth harness + combined CSV |

Every run writes an audit trail to `logs/run_<timestamp>.jsonl`. Multi-seed runs also write
`logs/combined_scoring_<timestamp>.csv`.

## Escalation

If an approved action is applied and VERIFY returns NOT RESOLVED, the same `(cell, fault_type)` may
retry once (`MAX_RETRIES = 2`). After that, the loop stops proposing automated actions for that
pair, logs `stage: escalate`, and counts it under `escalated` in the summary (separate from
`review_only`). DETECT can still print the cell; approve/execute do not run again for that pair.

## Adding the local LLM

Install Ollama (https://ollama.com), then:

```bash
ollama pull llama3.1:8b
python main.py --mode shadow --auto
```

Primary model in `config.yaml` is **`llama3.1:8b`**. If Ollama is not running, the loop keeps
working on rules and logs the failure.

### Model comparison (10-seed harness)

Four open models were evaluated in shadow mode. No model wins outright:

| Model | Notes |
|---|---|
| `llama3.2:3b` | Fast baseline; confuses `ul_interference` with `pim` (~9% on UL interference) |
| `qwen2.5:7b` | Strong on combined faults (100%), ruled out for US enterprise use (Chinese-origin) |
| `llama3.1:8b` **(chosen)** | 100% on ul_interference / overshoot / congestion after contrastive prompt; weak on combined (~5%) and pim (~22.5%) |
| `mistral:7b` | Best on pim (~95%), but breaks overshoot (~42%) |

Rules remain authoritative for action-taking; LLM output is evaluation / shadow-mode only. The
system prompt includes contrastive guidance so the LLM can separate `ul_interference` from `pim`
(both raise UL noise). Known gaps: combined-fault and pim diagnosis, and `sleeping_cell` (never
detected in the harness at the current ML contamination budget).

## Moving to the Jetson

Clone the repo on the Jetson and follow the same setup. Ollama on Jetson Orin uses the GPU when
healthy (`tegrastats` `GR3D_FREQ` spikes during inference). To run the model on the Jetson but the
loop on your laptop, start Ollama with `OLLAMA_HOST=0.0.0.0 ollama serve` and set
`diagnose.llm.url` in `config.yaml` to `http://<jetson-ip>:11434/api/generate`.

## Design notes

- **Guardrail:** the LLM only classifies the fault. The actual cmedit command always comes from the
  approved template table in `closedloop/diagnose.py`, and unexpected LLM output is forced to
  `unknown`.
- **ML-only anomalies are never auto-actioned.** They go to engineer review, since Isolation Forest
  flags some healthy cells every cycle (false positives by design, worth watching).
- **Shadow mode** is how you evaluate an AI component before trusting it with actions. Use
  `--seeds 1-10` for ground-truth scoring across fault types.
- **Escalation** prevents infinite retries on unfixable / misdiagnosed cells.

## Layout

```
main.py                  loop orchestration, escalation, scoring summary
config.yaml              thresholds, faults, mode, LLM endpoint / model
closedloop/simulator.py  COLLECT  (swap for a real PM export reader)
closedloop/detect.py     DETECT   (rules + Isolation Forest)
closedloop/diagnose.py   DIAGNOSE + DECIDE (rules, LLM, shadow, action templates)
closedloop/act.py        APPROVE + ACT + audit log
closedloop/verify.py     VERIFY
```

## Roadmap

1. Replace the simulator with real (scrubbed) PM counter exports
2. Improve LLM combined-fault and pim classification; give sleeping_cell a detectable path
3. FastAPI service plus Prometheus metrics and a Grafana dashboard
4. Containerize and deploy on k3s
5. Map the design to an O-RAN rApp on the Non-RT RIC (R1 interface)
