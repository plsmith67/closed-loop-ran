# Closed-Loop RAN — Command Reference

Run all of these from the project folder, with the virtual environment active:

```bash
cd ~/closed-loop-ran
source .venv/bin/activate
```

---

## Basic runs (synthetic simulator)

### `python main.py`
Default run. Rules diagnose each fault. Fault types in `auto_approve_faults`
(currently `ul_interference`, `overshoot`, `congestion`, `combined`,
`sleeping_cell`) act automatically. Anything else (`pim`, `multiple`,
`unknown`) pauses and asks `Approve? [y/N]`. Runs 4 cycles by default.

### `python main.py --cycles 8`
Same as above, but runs 8 cycles (ROPs) instead of 4. Use this to see what
happens after the easy faults resolve — by cycle 2 the four auto-fixable
faults are usually RESOLVED, and only the two hard cases (`multiple`, `pim`)
keep showing up every cycle, correctly held for review.

### `python main.py --auto`
Overrides the approval list for this run only — every actionable fault
auto-approves, including ones normally held for a human. Useful for fast
testing when you don't want to sit and type `y` each time.

### `python main.py --mode shadow`
Rules still make every decision and take every action. A local LLM also
diagnoses each fault *in parallel*, purely for comparison. Its answer is
logged and scored against ground truth but never acts on anything.

### `python main.py --mode llm`
The LLM makes the diagnosis instead of the rules. Rules are only used as a
fallback if the LLM/Ollama is unavailable. This is the mode where you saw
the LLM misdiagnose `pim` as `overshoot`, which then failed verification
twice and escalated — a real example of why rules, not the LLM, own
diagnosis by default.

### `python main.py --mode shadow --seeds 1-10`
Runs the whole thing 10 times with different random seeds, quietly (no
per-cycle printout), and prints one combined accuracy table at the end.
This is the multi-seed test harness used to produce the "100% rules
accuracy" numbers in the README. Simulator only — does not work with
`--source real`.

---

## Real production data

### `python scripts/scrub_pm_data.py /path/to/raw_export.csv`
One-time step. Takes a raw PM export, strips identifying columns
(`ObjectId`, `SubNetwork*`), renames the real cell IDs to generic labels
(`SITE001`, `SITE002`, …), and writes a safe copy to
`data/pm_sample_scrubbed.csv`. The raw file and the real ID mapping are
never committed to git.

### `python main.py --source real --pm-file data/pm_sample_scrubbed.csv --cycles 4`
Runs the exact same detect → diagnose → act → verify → escalate loop, but
reading real scrubbed KPI data instead of the synthetic simulator. Proves
the rules engine works on real counters, not just made-up test data.
`apply()` is a no-op here — historical rows can't actually be changed, so
nothing is "fixed," only diagnosed and logged.

### `python main.py --source real --pm-file data/pm_sample_scrubbed.csv --cycles 6 --pace-seconds 3`
Same as above, but pauses 3 seconds between each cycle so a human watching
the terminal can actually see it step through real days of data, instead of
finishing instantly. This is the version meant for showing someone live.

---

## Seeing the digital-twin prediction clearly

### `python main.py --cycles 4 | grep -A2 "TWIN PREDICTION"`
Filters the output down to just the twin's prediction lines (plus the
"Basis" line right after each one), so you're not hunting for it in a wall
of text. **Important:** the twin only prints for faults that get a proposed
action — `pim` and `multiple` never reach this step, since nothing is ever
proposed for them. It also stops appearing for a cell once that cell has
been escalated, since no new action is being proposed for it anymore.

---

## Checking what's actually running

### `git log --oneline -1`
Shows the current commit. Compare this against GitHub to confirm the code
on this machine matches what's pushed, and to check whether a specific
feature (like the twin) is even present in this version.

### `systemctl list-timers | grep closed-loop`
Shows when the unattended loop and the log-cleanup job last ran and when
they'll run next. Confirms the systemd timers are actually alive.

### Stop both timers
```bash
sudo systemctl disable --now closed-loop-ran.timer
sudo systemctl disable --now closed-loop-ran-cleanup.timer
```

### Start both timers again
```bash
sudo systemctl enable --now closed-loop-ran.timer
sudo systemctl enable --now closed-loop-ran-cleanup.timer
```

### `journalctl -u closed-loop-ran -n 50 --no-pager`
Shows the last 50 lines of output from the most recent unattended run —
same thing you'd see running `main.py` by hand, but from the background
service.

### `ls -lt logs | head -5`
Lists the 5 most recent audit log files, newest first. Each one is a full
record of one run: every detection, diagnosis, action, verification, and
escalation, in JSON.

---

## Git — saving your work

```bash
git status                  # what's changed, not yet committed
git add <file1> <file2>     # stage specific files
git commit -m "message"     # save a snapshot with a description
git push                    # send it to GitHub
git log --oneline -5        # see the last 5 commits
```
