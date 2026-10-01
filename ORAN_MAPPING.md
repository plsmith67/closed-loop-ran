# O-RAN mapping: closed-loop RAN → Non-RT RIC / rApp

This repository implements a working closed-loop fault and performance management pattern — collect, detect, diagnose, decide, approve, act, verify, escalate — validated on synthetic LTE data and scrubbed real fixed-wireless (FWA) eNodeB PM. That control pattern maps cleanly onto the O-RAN **Non-RT RIC** and **rApp** model: analytics and policy over multi-minute-to-hourly timescales, consuming PM/CM-style inputs and producing configuration or escalation outcomes.

This document is an **architectural mapping of a working LTE system**, not a claim that the code is an O-RAN RIC, an rApp runtime, or an SMO component. There is no R1 client, no A1/E2 stack, no O-RAN Alliance Service Management and Orchestration (SMO) integration, and no O-RU/O-DU/O-CU managed objects. The value of the mapping is to show exactly where the existing loop would sit in the O-RAN reference architecture, and what is still missing for a production rApp.

## Stage-by-stage mapping

Stages follow the loop in `main.py` (`run()`), which calls into `closedloop/`.

| Stage | This system today | O-RAN equivalent | Gap to a real O-RAN deployment |
|---|---|---|---|
| **Collect** | `NetworkSimulator.collect()` (`closedloop/simulator.py`) or `PMDataReader.collect()` (`closedloop/pm_reader.py`) returns one KPI row per cell for the current ROP (15-minute simulated ROP, or calendar ROP from scrubbed CSV). | PM data an rApp would consume from the Non-RT RIC data layer. In a full stack, counters typically enter via the SMO **O1** management plane (and/or vendor PM export into the SMO data lake), then are exposed to rApps through Non-RT RIC platform services on **R1**. | No O1 collector, no SMO data pipeline, no R1 data API. Real mode replays a scrubbed offline CSV; `apply()` cannot change a live network. KPI set is LTE/FWA eNodeB-shaped, not O-RAN O1 YANG PM models. |
| **Detect** | `detect()` in `closedloop/detect.py`: configurable KPI thresholds plus Isolation Forest; synthetic sleeping-cell pattern when no availability column is present. | rApp (or Non-RT RIC analytics service) anomaly / threshold logic over enriched PM — still Non-RT timescale. | No formal rApp packaging or shared analytics services. No subscription to SMO FM/PM streams. Real-data sleeping/dead-cell detection remains an open gap after availability-based rules failed validation. |
| **Diagnose** | `diagnose()` / `rule_diagnose()` / `llm_diagnose()` in `closedloop/diagnose.py`: deterministic rules own the acting label; optional local LLM in shadow or llm mode. | rApp root-cause / classification logic. LLM-as-shadow is an implementation choice inside the rApp, not an O-RAN interface. | No standardized enrichment (topology, alarms, neighbor relations) from SMO. No multi-rApp handoff of intermediate findings. |
| **Decide** | `command_for()` maps a fault label to a fixed cmedit-style string from `ACTION_TEMPLATES` in `closedloop/diagnose.py`. The LLM never authors commands. | Intent or change proposal an rApp would emit toward Non-RT RIC policy / CM orchestration (commonly discussed as rApp output over **R1**, with CM ultimately effected toward NFs via **O1**). Guardrailed templates are analogous to allowing only approved action types. | No R1 intent/policy API. Templates are hard-coded strings, not SMO-validated CM payloads or A1 policies toward a Near-RT RIC. `pim` has diagnosis but **no** action template (intentional). |
| **Approve** | `approve()` in `closedloop/act.py` plus `auto_approve_for()` in `main.py`. `loop.auto_approve_faults` in `config.yaml` lists Level-4-style types; others prompt. `--auto` overrides for testing. `unknown` / `multiple` never auto-approve. | Intent-based / policy-based enforcement: which closed-loop actions may execute without a human gate. In O-RAN terms this sits with Non-RT RIC policy control and operator governance over rApp privileges — not with the rApp inventing its own authority. | No external policy engine, no operator RBAC against an SMO, no signed policy bundles. Approval is local config + terminal prompt (or systemd unattended runs with `--auto` / listed faults). |
| **Act** | `execute()` → `sim.apply()` (`closedloop/act.py`). Simulator may clear an injected fault; `PMDataReader.apply()` logs a no-op. | Enforcement of an approved change: typically **O1** CM toward the managed element (or A1 policy toward Near-RT RIC if the decision were near-RT control — which this loop is not). | No O1/NETCONF/REST CM client, no ENM/OSS northbound, no Near-RT path. Real-PM runs cannot remediate; action is audit + log only. |
| **Verify** | `verify()` in `closedloop/verify.py` on the next ROP: checks the fault-relevant KPI and remaining breaches. | Closed-loop assurance: re-read PM (again O1 → Non-RT data path) and confirm the intent had the expected effect — standard rApp / Non-RT assurance pattern. | Same collect gaps. No formal assurance service or SLA object in an SMO; verify is in-process against the next dataframe row. |
| **Escalate** | After `MAX_RETRIES` (2) failed verifications for the same `(cell, fault_type)`, `main.py` stops acting and writes an `escalate` audit stage. Review-only faults (no template) go to `review`. | Operational breakout when automation is unsafe or ineffective — trouble ticket / NOC workflow, often outside the RIC proper but triggered by rApp or SMO assurance. | No ITSM/ticketing integration, no durable cross-run escalation state (each process start resets fail counts). Dashboard surfaces escalated cells from the latest JSONL only. |

Supporting pieces that are **not** O-RAN stages but matter operationally: JSONL audit log (`closedloop/act.py` `AuditLog`), FastAPI dashboard (`dashboard.py`), and systemd timers under `deploy/` for unattended shadow runs and log retention. Those map to observability and lifecycle around an rApp, not to R1/A1/E2 themselves.

## Near-RT RIC / xApp vs Non-RT RIC / rApp

| | Near-RT RIC / **xApp** | Non-RT RIC / **rApp** (this system’s home) |
|---|---|---|
| Timescale | Roughly 10 ms – 1 s | Greater than ~1 s; often minutes to hours |
| Typical decisions | Scheduling, power control, fast load balancing, beam/core control loops over **E2** | PM/FM analytics, configuration optimization, intent/policy, assurance over **O1** / **R1** (and **A1** when guiding Near-RT) |
| This repo | Not in scope. Nothing here issues sub-second control or speaks E2. | Natural fit: `config.yaml` uses `rop_minutes: 15`; real replay advances one historical ROP (daily in the current export) per cycle. Detect → act → verify needs at least one subsequent ROP to close the loop. |

The 15-minute (or daily replay) cadence is evidence that the design belongs at the **Non-RT RIC / rApp** layer. It is not a shortcoming relative to Near-RT; a sleeping-cell or interference CM change validated on the next PM period is the wrong workload for an xApp.

## TM Forum Autonomous Network Levels (per-fault policy)

TM Forum’s Autonomous Network Levels distinguish, among other things:

- **Level 3 (conditional autonomy):** the system proposes; a human (or strict external gate) still authorizes execution for that class of action.
- **Level 4 (high autonomy within policy):** the system executes automatically inside pre-agreed boundaries; humans govern the policy, not every instance.

This repo approximates that split **per fault type**:

- Listed in `loop.auto_approve_faults` (today: `ul_interference`, `overshoot`, `congestion`, `combined`, `sleeping_cell`) → skip the interactive gate (Level 4–style for those labels only).
- Omitted types (notably `pim`) and hard exclusions (`unknown`, `multiple`) → remain review / human (Level 3–style).
- `pim` is diagnosed by rules but has no `ACTION_TEMPLATES` entry, so it cannot be auto-acted even under `--auto` without code changes — physical inspection, not a safe CM knob.

Real-data testing is the validation-before-trust process a Level 4 rollout would require:

- **Synthetic `sleeping_cell`** reaches 100% rule accuracy on the multi-seed harness and is listed for auto-approve.
- **Real FWA PM** showed busy-hour PRB often ~3% and noisy availability samples (e.g. 33%/67%), so traffic- and availability-based sleeping-cell rules were **not** trustworthy for auto-action on that network. Availability detection was disabled; real-data sleeping-cell remains open.
- **`pim`** stays Level 3 by policy after rule/LLM behavior was characterized; there is still no automated remediation template.

That is the operational meaning of graduating fault types: prove on representative data, then widen `auto_approve_faults` — the same discipline an operator would use before granting an rApp unsupervised O1 change rights.

## What would be required to move from this architecture to a working Non-RT RIC rApp

Concrete gaps (none of these exist in the repo today):

1. **SMO + Non-RT RIC platform** — e.g. O-RAN Software Community reference SMO/Non-RT RIC, or a vendor Non-RT RIC, providing rApp lifecycle, data services, and policy services.
2. **Real R1 client** — register the app, consume PM/topology via platform APIs, publish intents/results; replace ad-hoc CSV/`config.yaml` with platform contracts.
3. **O1 (and optionally A1) integration** — CM/PM against real managed elements through the SMO; use A1 only if some decisions must guide a Near-RT RIC (not required for this CM/assurance loop).
4. **Formal rApp packaging and onboarding** — image/helm or platform-specific package, health checks, versioned config, RBAC, and controlled privileges for which MO classes may be written.
5. **Multi-domain / enriched context** — alarms, topology, neighbor relations, transport, and core/subscriber context beyond a single eNodeB PM table.
6. **Durable assurance and ops integration** — cross-run escalation state, ticketing, and audit retained in the SMO — not only local JSONL and a read-only dashboard.
7. **Data-model alignment** — map scrubbed LTE KPIs to the operator’s O1/PM models (and replace the real-data PRB placeholder with a true PRB counter before any Level 4 congestion logic on live FWA).

Until those exist, this codebase remains a **faithful Non-RT closed-loop prototype**: correct timescale, correct stage structure, real PM validation lessons, and an explicit Level 3/4 policy split — sitting *architecturally* where an rApp would sit, without being one.
