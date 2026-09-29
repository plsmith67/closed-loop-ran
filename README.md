# Closed-Loop RAN Anomaly Prototype

Personal learning prototype using simulated data. Not affiliated with any operator or vendor.

## What it is

A closed-loop RAN operations prototype: detect, diagnose, approve, act, verify, escalate. It runs a simulated 24-cell LTE network (8 sites, 3 sectors each). Approval is per fault type: proven types can run unattended (TM Forum Level 4) while unproven ones still require a human gate (Level 3). `--auto` overrides the list and auto-approves every actionable fault for quick testing.

## How it works

Each cycle is one 15-minute reporting period (ROP). `main.py` walks the loop:

| Stage | What happens | File |
|---|---|---|
| Collect | One KPI row per cell for this ROP | `closedloop/simulator.py` |
| Detect | Flag cells that breach a threshold, or that the Isolation Forest marks as anomalous | `closedloop/detect.py` |
| Diagnose | Name the fault with rules, a local LLM, or both | `closedloop/diagnose.py` |
| Decide | Look up the cmedit command for that fault | `closedloop/diagnose.py` |
| Approve and act | Ask for approval (or auto-approve), then apply the change | `closedloop/act.py` |
| Verify | On the next ROP, check whether the fault KPI recovered and the cell is clean | `closedloop/verify.py` |
| Escalate | After repeated failed verifications, stop retrying and route the cell to an engineer | `main.py` |

`main.py` also writes the audit log and the ground-truth scoring summary.

## Fault types

Every cell starts from a healthy KPI draw. `closedloop/simulator.py` then overwrites the cells listed under `simulator.faults` in `config.yaml`.

| Fault | How it is injected |
|---|---|
| `ul_interference` | UL noise +15 dBm, RRC success −4 points, drop rate +1.2 points |
| `overshoot` | Drop rate +2.5 points and DL throughput −8 Mbps. UL noise stays in the healthy range |
| `congestion` | PRB utilization drawn around 94%, DL throughput −18 Mbps |
| `combined` | The `ul_interference` offsets and the `congestion` pattern on the same cell |
| `sleeping_cell` | Injected as near-zero traffic (PRB ~2%, DL throughput ~0.5 Mbps, drop rate ~0.1%). Detected by a dedicated rule when PRB, throughput, and drop rate are all quiet (`sleeping_cell_pattern`), not by the Isolation Forest. |
| `pim` | UL noise +12 dBm, drop rate +1.5 points, PRB utilization drawn around 75% (busy, under the congestion threshold). RRC success stays healthy |

## Design decisions

- Rules drive actions. A cell that only the Isolation Forest flags, with no threshold breach, is queued for engineer review and is never auto-actioned.
- Approval graduates per fault type. `loop.auto_approve_faults` in `config.yaml` lists types that skip the human gate (Level 4). Types not on the list stay Level 3. `pim` is intentionally kept Level 3; `unknown` and `multiple` never auto-approve even if listed. `--auto` overrides the list for the whole run.
- The LLM only classifies. It never writes commands. The cmedit string always comes from the approved template table in `closedloop/diagnose.py`. Output that is not one of the known fault labels is forced to `unknown`.
- Shadow mode: rules act, and the LLM label is scored against the simulator's ground truth.
- Escalation: after 2 failed verifications on the same cell and fault, the loop stops proposing another automated action for that pair and routes it to an engineer.
- Isolation Forest contamination is 0.04. With 24 cells that budget is about one cell (0.04 × 24 ≈ 1). The next step is another whole cell, so tuning moves in whole-cell steps.

## Model evaluation

These results are from one synthetic 10-seed dataset (shadow mode, `--seeds 1-10`). Timing figures were measured on the Jetson at power mode MODE_30W (30W cap), not the board's maximum performance mode.

All six injected fault types (`ul_interference`, `overshoot`, `congestion`, `combined`/`multiple`, `sleeping_cell`, `pim`) are now solved deterministically by rules at 100% accuracy on that harness. The LLM is comparison / shadow-mode only; rules drive diagnosis and action selection.

| Fault | llama3.2:3b | qwen2.5:7b | llama3.1:8b | mistral:7b |
|---|---:|---:|---:|---:|
| ul_interference | 9.1% | 90.9% | 100% | 81.8% |
| overshoot | 100% | 100% | 100% | 41.7% |
| congestion | 100% | 100% | 100% | 100% |
| combined | 0% | 100% | 5% | 0% |
| pim | 0% | 0% | 22.5% | 95% |

`llama3.1:8b` is the chosen model (US-based, Meta). `qwen2.5:7b` was evaluated but excluded for US enterprise use. The table above is the earlier LLM-only comparison from before the rule coverage was complete.

`llama3.1:8b` scored 9.1% on `ul_interference` until the prompt gave it contrastive `ul_interference`-vs-`pim` guidance, after which it hit 100%. Combined-fault accuracy dropped on that same prompt change, which is why prompt changes must be tested against the full fault distribution. After the stronger simulator `pim` drop-rate offset (+2.0), LLM `pim` accuracy in shadow mode fell to 0% (likely reading more like overshoot); that does not affect the loop, because rules own diagnosis.

## Known limitations

- There is no action template for `pim`. Rules detect it and queue it for engineer review (physical inspection), and it is intentionally left off `auto_approve_faults`.
- LLM accuracy can shift a few points across otherwise identical runs because GPU inference is not fully deterministic even at temperature 0.
- The simulator is synthetic. KPI offsets are hand-written, and a matching action clears the injected fault 85% of the time (`fix_success_rate`).

## Setup and run

Dependencies are in `requirements.txt`: `numpy`, `pandas`, `scikit-learn`, `pyyaml`, `requests`.

### Windows

```bat
git clone https://github.com/plsmith67/closed-loop-ran.git closed-loop-ran
cd closed-loop-ran
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

### Linux and Jetson

```bash
git clone https://github.com/plsmith67/closed-loop-ran.git closed-loop-ran && cd closed-loop-ran
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

### Run modes

| Command | What it does |
|---|---|
| `python main.py` | Rules diagnose. Fault types in `auto_approve_faults` skip approval (Level 4); others prompt (Level 3). Default is 4 cycles. |
| `python main.py --auto` | Override: auto-approve every actionable fault for this run. |
| `python main.py --mode shadow` | Rules act. The LLM is scored against ground truth. |
| `python main.py --mode llm` | The LLM diagnoses. Rules are the fallback if Ollama is unavailable. |
| `python main.py --cycles N` | Run N ROPs. |
| `python main.py --mode shadow --seeds 1-10` | Quiet multi-seed harness. Prints a combined table and writes `logs/combined_scoring_<timestamp>.csv`. |

`--auto` and `--mode` can be combined. `--seeds` always auto-approves and suppresses per-cycle output. Every run writes an audit trail to `logs/run_<timestamp>.jsonl`.

Approval policy mirrors the TM Forum idea of graduating fault types from Level 3 to Level 4 as accuracy is proven. Today `ul_interference`, `overshoot`, `congestion`, `combined`, and `sleeping_cell` are listed; `pim` is intentionally left out.

### Ollama

Install [Ollama](https://ollama.com), then:

```bash
ollama pull llama3.1:8b
python main.py --mode shadow --auto
```

`config.yaml` sets `diagnose.llm.model` to `llama3.1:8b` and `diagnose.llm.url` to `http://localhost:11434/api/generate`. If Ollama is not running, the loop keeps going on rules and logs the failure.

To run the model on a Jetson and the loop on another machine, start Ollama on the Jetson so it listens on the network:

```bash
OLLAMA_HOST=0.0.0.0 ollama serve
```

Set `diagnose.llm.url` in `config.yaml` to `http://<jetson-ip>:11434/api/generate`. On a healthy Jetson Orin, GPU use shows up as a `GR3D_FREQ` spike in `tegrastats` during inference.

## Running unattended

On this Jetson, a systemd timer runs a 4-cycle shadow loop (`--mode shadow --auto --cycles 4`) two minutes after boot and 15 minutes after each run. Each firing covers detect, act, verify, and escalate in one process.

Install:

```bash
bash deploy/install.sh
```

Check status:

```bash
systemctl list-timers | grep closed-loop
journalctl -u closed-loop-ran -n 50 --no-pager
```

Stop:

```bash
sudo systemctl disable --now closed-loop-ran.timer
```

Escalation counts reset on each run because each firing is a fresh process.

## Roadmap

1. Run the loop as an unattended systemd service. Done (`deploy/closed-loop-ran.timer`).
2. Replace the simulator with a reader for real PM counter exports, keeping the same column names.
3. Add a sleeping-cell rule so a quiet on-air cell can be detected without spending the Isolation Forest budget. Done (`sleeping_cell_pattern` in `closedloop/detect.py`).
4. Add a digital-twin what-if stage that previews a change before approval.
