# Closed-Loop RAN Anomaly Prototype

Personal learning prototype. Not affiliated with any operator or vendor.

## What it is

A closed-loop RAN operations prototype: detect, diagnose, approve, act, verify, escalate. Default runs use a simulated 24-cell LTE network (8 sites, 3 sectors each). The same loop can also replay scrubbed real Ericsson eNodeB PM (`--source real`).

Approval is per fault type: proven types can run unattended (TM Forum Level 4) while unproven ones still require a human gate (Level 3). `--auto` overrides the list and auto-approves every actionable fault for quick testing. Rules drive actions; a local LLM can run alongside in shadow mode for comparison. Systemd timers can run the loop (and clean old logs) unattended on a Jetson.

**Interface:** this is a terminal- and audit-log-driven tool. Output is what you see in the console plus `logs/run_*.jsonl`. A web dashboard was built earlier and then removed — a fixed-seed simulator and a static historical export give a live status UI nothing new to ever show, so the terminal and audit log remain the authoritative interface.

## How it works

Each cycle is one 15-minute reporting period (ROP). `main.py` walks the loop:

| Stage | What happens | File |
|---|---|---|
| Collect | One KPI row per cell for this ROP (simulator, or scrubbed PM via `PMDataReader`) | `closedloop/simulator.py` / `closedloop/pm_reader.py` |
| Detect | Flag cells that breach a threshold, or that the Isolation Forest marks as anomalous | `closedloop/detect.py` |
| Diagnose | Name the fault with rules, a local LLM, or both | `closedloop/diagnose.py` |
| Decide | Look up the cmedit command for that fault | `closedloop/diagnose.py` |
| Twin (what-if) | Heuristic prediction of the primary KPI after the proposed fix; warn if unlikely to clear | `closedloop/twin.py` |
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
| `sleeping_cell` | Injected as near-zero traffic (PRB ~2%, DL throughput ~0.5 Mbps, drop rate ~0.1%). Detected by a dedicated rule when PRB, throughput, and drop rate are all quiet (`sleeping_cell_pattern`), not by the Isolation Forest |
| `pim` | UL noise +12 dBm, drop rate +2.0 points, PRB utilization drawn around 75% (busy, under the congestion threshold). RRC success stays healthy |

## Design decisions

- Rules drive actions. A cell that only the Isolation Forest flags, with no threshold breach, is queued for engineer review and is never auto-actioned.
- Approval graduates per fault type. `loop.auto_approve_faults` in `config.yaml` lists types that skip the human gate (Level 4). Types not on the list stay Level 3. `pim` is intentionally kept Level 3; `unknown` and `multiple` never auto-approve even if listed. `--auto` overrides the list for the whole run.
- The LLM only classifies. It never writes commands. The cmedit string always comes from the approved template table in `closedloop/diagnose.py`. Output that is not one of the known fault labels is forced to `unknown`.
- Shadow mode: rules act, and the LLM label is scored against the simulator's ground truth.
- Escalation: after 2 failed verifications on the same cell and fault, the loop stops proposing another automated action for that pair and routes it to an engineer.
- Isolation Forest contamination is 0.04. With 24 cells that budget is about one cell (0.04 × 24 ≈ 1). The next step is another whole cell, so tuning moves in whole-cell steps.

## Model evaluation

These results are from one synthetic 10-seed dataset (shadow mode, `--seeds 1-10`). Timing figures were measured on the Jetson at power mode MODE_30W (30W cap), not the board's maximum performance mode.

All six injected fault types (`ul_interference`, `overshoot`, `congestion`, `combined`/`multiple`, `sleeping_cell`, `pim`) are solved deterministically by rules at 100% accuracy on that harness. The LLM is comparison / shadow-mode only; rules drive diagnosis and action selection.

The table below is the earlier LLM-only bake-off (before rule coverage was complete). `sleeping_cell` was added to the simulator after that bake-off and is rules-only, so it has no LLM column here.

| Fault | llama3.2:3b | qwen2.5:7b | llama3.1:8b | mistral:7b |
|---|---:|---:|---:|---:|
| ul_interference | 9.1% | 90.9% | 100% | 81.8% |
| overshoot | 100% | 100% | 100% | 41.7% |
| congestion | 100% | 100% | 100% | 100% |
| combined | 0% | 100% | 5% | 0% |
| pim | 0% | 0% | 22.5% | 95% |

`llama3.1:8b` is the chosen model (US-based, Meta). `qwen2.5:7b` was evaluated but excluded for US enterprise use.

`llama3.1:8b` scored 9.1% on `ul_interference` until the prompt gave it contrastive `ul_interference`-vs-`pim` guidance, after which it hit 100%. Combined-fault accuracy dropped on that same prompt change, which is why prompt changes must be tested against the full fault distribution. After the stronger simulator `pim` drop-rate offset (+2.0), LLM `pim` accuracy in shadow mode fell to 0% (likely reading more like overshoot); that does not affect the loop, because rules own diagnosis.

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
| `python main.py --source real --pm-file data/pm_sample_scrubbed.csv` | Replay scrubbed real PM instead of the simulator (see [Real data mode](#real-data-mode)). |

`--auto` and `--mode` can be combined. `--seeds` always auto-approves and suppresses per-cycle output; it is simulator-only (not valid with `--source real`). Every run writes an audit trail to `logs/run_<timestamp>.jsonl`.

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

Two systemd timers ship under `deploy/`:

1. **Loop timer** (`closed-loop-ran.timer`) — runs a 4-cycle shadow loop (`--mode shadow --auto --cycles 4`) two minutes after boot and 15 minutes after each run. Each firing covers detect, act, verify, and escalate in one process. Escalation counts reset on each run because each firing is a fresh process.
2. **Log cleanup timer** (`closed-loop-ran-cleanup.timer`) — once a day, deletes `logs/*.jsonl` older than 14 days.

Install both:

```bash
bash deploy/install.sh
```

Check status:

```bash
systemctl list-timers | grep closed-loop
journalctl -u closed-loop-ran -n 50 --no-pager
journalctl -u closed-loop-ran-cleanup -n 20 --no-pager
```

Stop the loop timer:

```bash
sudo systemctl disable --now closed-loop-ran.timer
```

## Real data mode

Default runs still use the synthetic simulator. To replay scrubbed Ericsson eNodeB PM:

1. Scrub a raw export (path is an argument — never hardcode or commit the raw file):

```bash
python scripts/scrub_pm_data.py /path/to/eNodeB_Performance_KPIs.csv
```

This writes `data/pm_sample_scrubbed.csv` (generic `SITE001`… cell labels, identifying `ObjectId` / `SubNetwork*` columns removed) and a private `data/pm_data_id_map.csv` (do not commit). Raw PM filenames and the id map are gitignored.

2. Run the loop against the scrubbed file:

```bash
python main.py --source real --pm-file data/pm_sample_scrubbed.csv --cycles 4
```

Optional: `--auto`, `--mode shadow`, etc. work as usual. `--seeds` does not (simulator-only). The reader advances one calendar ROP per cycle from the file (daily in the current export). `apply()` is a no-op with a log line because historical rows cannot be remediated.

**Sleeping / dead cells on real FWA data (open gap):** busy-hour PRB utilization on this fixed-wireless network is often only ~3%, and some cells legitimately have zero subscribers, so the synthetic traffic-based `sleeping_cell_pattern` is unusable on real data. An availability-based rule was tried (`Cell Availability (%)` below 90 → `cell_down_pattern`). Against real historical PM, that metric’s low sample resolution produced noisy values like 33.33% / 66.67% / 0.0% and triggered on the large majority of cells — clearly more sensitive to sampling noise than to real outages. The availability check is therefore disabled for real-data mode; the PRB-based rule remains unchanged for the synthetic simulator (no availability column). Real-data sleeping-cell detection is still open. What would fix it: a higher-resolution availability counter (e.g. hourly rather than a handful of daily samples), or multi-day persistence (only flag if availability stays low across several consecutive days).

**PRB placeholder:** `prb_util_pct` is currently inferred by normalizing downlink traffic volume to 0–100 against the file max. It is a placeholder, not a real PRB counter, and should be replaced once a PRB-inclusive export is available. `cell_availability_pct` is still scrubbed into the file for analysis but is not used by any active rule. Other KPI columns come from the export under closed-loop names (`rrc_success_pct`, `drop_rate_pct`, `ul_noise_dbm`, `dl_tput_mbps`).

## Digital twin / what-if prediction

Before approval, the loop runs a lightweight what-if prediction (`closedloop/twin.py`) on every fault that has an action template. It uses the same fault→KPI map as `verify.py`, moves the current value ~60% of the way back toward the healthy threshold edge for that metric, and reports confidence from `simulator.fix_success_rate` (default 85%). The prediction is printed in the terminal and written onto the `act` audit event (`twin_predicted_value`, `twin_predicted_resolved`, `twin_confidence_pct`) so it can later be compared to the real `verify()` outcome. If the heuristic predicts the fix will **not** fully clear the metric, a warning is printed; approval is **not** blocked.

**Honesty:** this is a deliberate small demonstration of the digital-twin *decision pattern* (predict before commit). It is **not** a trained model and **not** a PHY/channel/UE-level simulator. A production-grade twin would need calibrated response models (or ray-tracing / system-level RAN simulation), per-site RF and traffic context, and closed-loop comparison of predicted vs actual outcomes to retrain or retune the predictor — the audit fields are there so that calibration path is possible later.

## Known limitations

- There is no action template for `pim`. Rules detect it and queue it for engineer review (physical inspection), and it is intentionally left off `auto_approve_faults`.
- LLM accuracy can shift a few points across otherwise identical runs because GPU inference is not fully deterministic even at temperature 0.
- The simulator is synthetic. KPI offsets are hand-written, and a matching action clears the injected fault 85% of the time (`fix_success_rate`).
- Real-data mode cannot yet detect sleeping or dead cells reliably (see [Real data mode](#real-data-mode)). Synthetic `sleeping_cell` detection is solved.
- On real PM, `prb_util_pct` is a traffic-normalized placeholder until a real PRB counter is available in the export.
- The digital-twin stage is a heuristic gap-closure predictor, not a physics-based or learned twin (see [Digital twin / what-if prediction](#digital-twin--what-if-prediction)).

## Roadmap

**Done**

1. Run the loop as an unattended systemd service — Done (`deploy/closed-loop-ran.timer`), plus daily log cleanup (`closed-loop-ran-cleanup.timer`).
2. Replace the simulator with a reader for real PM counter exports, keeping the same column names — Done (`closedloop/pm_reader.py`, `--source real`).
3. Add a sleeping-cell rule so a quiet on-air cell can be detected without spending the Isolation Forest budget — Done for the synthetic simulator (`sleeping_cell_pattern` in `closedloop/detect.py`).
4. Dashboard — built, then removed; a fixed-seed simulator and a static historical export give it nothing new to ever show, so the terminal output and audit log (`logs/*.jsonl`) are the authoritative interface.
5. Digital-twin what-if stage before approval — Done (`closedloop/twin.py`; heuristic only).

**Open**

6. Real-data sleeping / dead-cell detection that survives low-resolution availability and low FWA PRB (see Real data mode).
7. Replace the inferred real-data PRB placeholder with a true PRB counter once the export includes one.
8. Calibrate or replace the twin heuristic using logged predicted-vs-actual verify outcomes; optionally a PHY/channel-level twin.
