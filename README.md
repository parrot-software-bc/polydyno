# 🏎️ Polydyno (v0.2.0)

**By [Polycracker](https://polycracker.dev)** — the 50-turn adversarial context drift and invariant benchmark harness for AI coding agents.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Verified Workload](https://img.shields.io/badge/workload-SHA--256%20verified-aa13421a3cdf)

Polydyno evaluates agent state retention across long sessions. Instead of isolated tool calls or single-shot evals, Polydyno subjects models to a standardized 50-turn canonical PostgreSQL schema and Pydantic refactoring track. As context inflates, agents face **adversarial knocks** (production incident threads, EXPLAIN dumps, breaking product specs) designed to induce silent invariant loss, AST drift, and token thrashing.

Every run outputs JSON telemetry, an interactive HTML/Chart.js dyno curve, and an ASCII dyno slip scored across **PolyScore (0–1000)**, **Structural Torque**, **Thermal Grade (BSFC & token burn)**, and a tamper-evident workload hash. PolyScore couples AST invariant retention with **token economics**: per-turn bloat flags, cumulative context burn, and non-diluted recovery overhead on long sessions.

---

## Quickstart

### Installation

```bash
git clone https://github.com/parrot-software-bc/polydyno.git
cd polydyno
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -e .           # or: pip install -r requirements.txt (pins match pyproject.toml)
```

Copy `.env.example` to `.env` before running live API models (API keys are only required for `--mode live`; simulation uses `--mode sim` and needs no keys):

```bash
cp .env.example .env
```

### Deterministic Simulation (No API Keys Needed)

Run the deterministic local harness in ~5 seconds to test CLI flags, telemetry, and reporting offline:

```bash
# 1. Unassisted baseline run (Turn 18 FK / cents / email collapse)
polydyno --mode sim --open-chart

# 2. Closed-loop self-repair with post-failure containment telemetry
polydyno --mode sim --invariant-mode verify-commit --max-retries 3 --open-chart
```

Run artifacts are written locally under `results/` (gitignored — not shipped in the repository):

| File | Purpose |
| :--- | :--- |
| `dyno_sheet_<timestamp>.json` | Raw turn-by-turn telemetry, tokens, latency, AST diffs, and verification hash |
| `dyno_chart_<timestamp>.html` | Interactive Chart.js dyno plot with recovery overlays and KPI scorecard |
| `dyno_report_<timestamp>.md` | GFM-formatted markdown summary for PRs and issues |

### Live API Benchmarks

```bash
# OpenAI models
polydyno --mode live --provider openai --model gpt-4o --load outlaw

# Anthropic models
polydyno --mode live --provider anthropic --model claude-sonnet-4-5 --load stage2

# Local runners & gateways (Ollama, vLLM, LM Studio, OpenRouter)
polydyno --mode live --provider openai --model qwen2.5:72b --base-url http://localhost:11434/v1
```

---

## Invariant Modes & Active Recovery (v0.2)

By default, Polydyno evaluates raw models under unassisted context pressure (`--invariant-mode baseline`). In v0.2, you can benchmark prompt mitigation strategies and active closed-loop self-repair:

```bash
# Baseline: Standard unassisted 50-turn challenge
polydyno --mode sim --invariant-mode baseline

# Verify-Commit: "Propose, Verify, Commit" self-repair loop with retry budget
polydyno --mode sim --invariant-mode verify-commit --max-retries 3

# Ledger: Compact machine-readable invariant ID block on every turn
polydyno --mode sim --invariant-mode ledger

# Prose-Repeat: Full prose reinjection of all invariants on every turn
polydyno --mode sim --invariant-mode prose-repeat
```

### Comparative Simulation Trajectories

| Mode | First Drop | Terminal Turn | Clean Recoveries | Cascading Regressions | PolyScore | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`baseline`** | Turn 18 | Turn 18 | 0 | 0 | **245 / 1000** | Adversarial collapse: nullable `orders.user_id`, dropped `fk_user_id`, `total_cents` → `DOUBLE PRECISION`, `users.email` uniqueness weakened (`--stop-on-drift` halts on first violation). |
| **`prose-repeat`** | Turn 21 | Turn 21 | 0 | 0 | **151 / 1000** | Prompt bloat triggers early context saturation. |
| **`ledger`** | Turn 26 | Turn 26 | 0 | 0 | **360 / 1000** | Compact IDs delay drift without token thrash. |
| **`verify-commit`** | Turn 18 | Turn 31 | 1 / 3 | 3 | **179 / 1000** | Self-repair loop suffers heavy retry & regression tax. |

---

## PolyScore Architecture (Structural Torque × Thermal BSFC)

PolyScore (0–1000) is the product of **structural torque** (how much of the canonical track you kept intact under load and repair) and an **effective thermal multiplier** (how efficiently you spent tokens, including post-failure recovery). Scoring constants are versioned with the harness but **do not** change the canonical workload fingerprint (`aa13421a3cdf`).

```text
Per-turn validation  →  BSFC multiplier + thermal bloat flags
        ↓
Run aggregate        →  Base thermal grade (cumulative burn, yield, bloat %)
        ↓
Recovery overhead  →  Effective thermal multiplier (floor 0.45×)
        ↓
× Structural torque  →  PolyScore (0–1000)
```

Unlike benchmarks that reward endless retry loops, **Polydyno penalizes post-failure containment overhead.** Extra intact turns after a drop do not fully count toward torque; burning seven figures of input without yield collapses the thermal multiplier even when structural torque stays in the mid-300s.

### Composite formula

$$\text{PolyScore} = \text{clamp}\left( \text{round}(\text{Net Torque} \times \text{Effective Thermal Multiplier}), 0, 1000 \right)$$

---

### Per-turn token metrics (validation layer)

Each turn’s validator output feeds telemetry and grading (invariant pass/fail remains independent):

| Metric | Definition |
| :--- | :--- |
| **Thermal efficiency** | `output_tokens / input_tokens` |
| **BSFC multiplier** | `input_tokens / artifact_tokens`, where `artifact_tokens = max(output_tokens, len(schema_sql) // 4, 1)` |
| **Thermal bloat** | **Either** character guard: `input_tokens > 50 × artifact_chars` (`artifact_chars` from schema length or ~4× output), **or** quadratic runaway: `input_tokens > 22,000` **and** `input_tokens / output_tokens > 22.0` (unpruned chat history thrash) |

---

### Structural Torque (0–1000)

Torque measures weighted progress through the canonical track, scaled by **Eddy-Current load** (`stage1` ×0.4, `stage2` ×0.7, `outlaw` ×1.0) and average **retention** fraction parsed from per-turn retention scores.

**Zero-retry guard:** If there are no repair attempts, cascading regressions, or recovery tokens, torque uses the classic unassisted formula:

$$\text{Torque} = \text{round}\left(1000 \times \text{Load} \times \frac{\text{Intact Turns}}{|TURN\_SPECS|} \times \text{Retention}\right)$$

**Anti-dilution (diminished turn credit):** From the first turn with repair activity or an initial invariant failure, each **still-intact** turn at or after that point contributes **0.25** toward weighted intact count (turns before that contribute **1.0**). This prevents repair loops from inflating structural credit.

**Retry & regression taxes (point deductions on gross torque):**

* **Retry tax:** −15 per repair attempt.
* **Cascading regression penalty:** −45 per event (fixing one invariant while breaking another that previously passed).

$$\text{Net Torque} = \max\left(0, \text{Gross Torque} - \text{Retry Tax} - \text{Regression Penalty}\right)$$

**Lean vs. strict gross torque** (total **input** tokens across the run):

| Path | Condition | Gross torque |
| :--- | :--- | :--- |
| **Lean** | `Σ input_tokens ≤ 250,000` | Blended anti-dilution path: `0.5 ×` load-track intact torque `+ 0.6 ×` weighted-intact torque (both normalized by `max(|TURN_SPECS|, 1)`). Preserves calibrated short/sim runs. |
| **Strict** | `Σ input_tokens > 250,000` | **No blend.** `Gross = round(1000 × Load × (Weighted Intact / run_benchmark) × Retention)` where `run_benchmark = min(turns completed, |TURN_SPECS|)`. Long live sessions cannot earn inflated torque from the legacy blend. |

---

### Base Thermal Grade (BSFC & cumulative token burn)

**Base thermal grade** is computed from run-level aggregates: total input tokens, **yield ratio** (`total output / total input`), fraction of turns flagged **thermal bloat**, and average per-turn thermal efficiency. Key thresholds:

| Threshold | Meaning |
| :--- | :--- |
| **250,000** input | Lean context budget — crossing it caps base grade (typically **C** or worse, depending on yield/bloat) |
| **1,100,000** input | Context runaway cap — forces **D** or **F** territory with low yield |
| **Bloat fraction** | Share of turns with `thermal_bloat == true` — high values drag grade down |

| Grade | Base multiplier | Typical signal |
| :--- | :--- | :--- |
| **A+** | 1.05 | ≤150k input, low bloat, strong yield & efficiency |
| **A** | 1.00 | Within lean budget, healthy yield |
| **B** | 0.90 | Moderate burn or bloat |
| **C** | 0.75 | Above lean budget or elevated bloat |
| **D** | 0.55 | Runaway input and/or low yield |
| **F** | 0.30 | Extreme runaway or collapse in yield |

---

### Effective Thermal Grade (recovery overhead)

When the run has retries, cascading regressions, or recovery tokens, the harness applies a **recovery overhead** penalty on top of the base grade. The **effective multiplier** is mapped back to a letter grade and is what PolyScore uses.

**If** `Σ input_tokens ≤ 250,000` **(lean path):**

$$\text{Recovery Ratio} = \frac{\text{Recovery Tokens}}{\text{Total Tokens} - \text{Recovery Tokens}}$$

**If** `Σ input_tokens > 250,000` **(strict path, non-diluted):**

$$\text{Recovery Ratio} = \frac{\text{Recovery Tokens}}{\min(\text{Pre-Failure Tokens}, 100{,}000)}$$

*Pre-failure tokens* = tokens consumed through the first unassisted drop (on the drop turn, recovery-token tally is excluded from the anchor).

$$\text{Effective Multiplier} = \max\left(0.45,\; \text{Base Multiplier} - (\text{Recovery Ratio} \times 1.25)\right)$$

The **0.45× floor** ensures catastrophic context runaway cannot be fully masked by structural torque alone.

---

### Token telemetry glossary (JSON, dyno slip, HTML)

Saved in `dyno_sheet_*.json` `metadata` and surfaced on the ASCII slip / Chart.js scorecard:

| Field | Meaning |
| :--- | :--- |
| `bsfc_avg` / `bsfc_peak` | Mean and max per-turn `input / output` (in/out bloat) |
| `yield_ratio` | Run-level `total output / total input` |
| `bloat_turns` | Count of turns with `thermal_bloat` |
| `pre_failure_tokens` | Token anchor before first unassisted drop |
| `post_failure_runaway_tokens` | Tokens burned on/after first drop (excluding explicit recovery bucket) |
| `context_runaway_factor` | `total input / pre_failure_tokens` when a drop exists |
| `recovery_overhead_vs_predrop_pct` | Recovery tokens as % of capped pre-drop anchor |
| `base_thermal_grade` | Grade from burn/yield/bloat before recovery penalty |
| `effective_thermal_multiplier` | Multiplier after recovery penalty (used in PolyScore) |

---

### Worked examples

#### Primary empirical anchor — live `gpt-4o` verify-commit

Closed-loop self-repair on the canonical workload with **~1.17M input tokens**. Structural torque reflects weighted intact turns after Turn 18; thermal collapses to the **0.45× floor** from runaway burn, low yield, and bloat.

```text
================ OFFICIAL HARNESS RUN SLIP (LIVE) ==============
Model:                 openai / gpt-4o
Track:                 50-Turn Canonical PostgreSQL Refactor (stage2)
Invariant Mode:        verify-commit
Turns completed:       50
Invariant pass rate:   50/50
First Unassisted Drop: Turn 18
Verification Hash:     aa13421a3cdf (SHA-256)
-------------------------------------------------------------
PolyScore:             146 / 1000
Structural Torque:     324 / 1000
Thermal Grade:         D [x0.45]   (base: D)
Total input tokens:    ~1,166,409
Effective yield:       ~3.8%
BSFC (in/out):         ~22.4x avg · ~36.6x peak · 23 bloat turns
Context runaway:       ~11.0x total input vs pre-drop · ~1.09M post-failure burn
Recovery overhead:     +28.3% vs pre-drop anchor (capped 100k)
Retry & Regression Tax: (varies) — net torque after repair attempt taxes
Outcome:               High structural torque cannot rescue headline score under thermal floor
=============================================================
```

#### Simulation reference — `verify-commit` (calibrated lean path)

Deterministic sim with **≤250k input**; uses **lean** gross torque blend and **diluted** recovery ratio for thermal.

```text
================ OFFICIAL HARNESS RUN SLIP (SIM) ================
Model:                 sim / simulated
Track:                 50-Turn Canonical PostgreSQL Refactor (stage2)
Turns completed:       31
Invariant pass rate:   30/31
First drift turn:      31
Verification Hash:     aa13421a3cdf (SHA-256)
-------------------------------------------------------------
PolyScore:             179 / 1000
Structural Torque:     218 / 1000
Thermal Grade:         B [x0.82]   (base: A+ → B)
Effective yield:       (see dyno sheet)
BSFC / bloat turns:    0 bloat turns on lean sim trajectory
Total input tokens:    ~34,337
-------------------------------------------------------------
============= POST-FAILURE CONTAINMENT & RECOVERY =============
Invariant Mode:        verify-commit
First Unassisted Drop: Turn 18
Terminal Drift Turn:   Turn 31
Total Repair Retries:  6
Recovery Token Overhead: +8,715 tokens (diluted lean-path recovery ratio)
Clean Recoveries:      1 / 3
Cascading Regressions: 3 (Secondary invariants broken)
Retry & Regression Tax: -225 pts (6 retries, 3 cascading breaks)
Outcome:               Retry budget exhausted at Turn 31 (3/3); cascading regressions: 1 on final turn
=============================================================
```

---

## How It Works (The 3 Cylinders)

Validation uses three distinct AST engines to eliminate false positives from regex or simple keyword matching:

### 1. PostgreSQL AST (`sqlglot`)

Parses all generated DDL statements and checks declarative invariants:

* **Referential Integrity:** `orders.user_id` stays `NOT NULL` with `fk_user_id REFERENCES users(id) ON DELETE CASCADE`.
* **Financial Integer Precision:** All `*_cents` columns remain integer types (`BIGINT`/`INTEGER`); floats or decimals are rejected.
* **Identity Retention:** `users.email` remains `NOT NULL` and `UNIQUE`.
* **Index & Constraint Retention:** Retains prior tables, partial indexes (e.g., `idx_orders_cancelled_at`), and `CHECK` constraints across all turns.

### 2. Python Pydantic AST (`cross_artifact_type_sync`)

Active under outlaw load. Parses backend Pydantic v2 models alongside SQL DDL. Validates that Python types match database constraints: `user_id` cannot become `Optional`, and monetary columns cannot resolve to `float`.

### 3. Temporal State Traps (`temporal_state_consistency`)

Injects mutating requirements over time to catch attention ghosting:

* **Turn 12+:** `orders.status` `CHECK` constraint must permit `'disputed'`.
* **Turn 28+:** Replaces `'disputed'` with `'chargeback_pending'` and forbids `'disputed'` going forward.

---

## Eddy-Current Load Stages (`--load`)

| Stage | Multiplier | Description |
| :--- | :--- | :--- |
| `stage1` | $\times 0.4$ | Baseline workload with gentle prompt growth and standard migrations. |
| `stage2` | $\times 0.7$ | Adversarial context noise, incident logs, and temporal state traps (default). |
| `outlaw` | $\times 1.0$ | Dual SQL + Pydantic artifacts with heavy adversarial knocks. |

---

## Black Box Autopsy

When an invariant fails or a retry budget is exhausted, Polydyno dumps a forensic autopsy:

* **Adversarial Context:** Incident thread transcripts and misleading PM notes from the failing turn.
* **Unified SQL Diff:** Precise `difflib` AST comparison between the last valid schema state and the mutated failure.
* **Cascading Regression Log:** Specific invariant IDs broken during self-repair attempts.

---

## Verified Canonical Workload

All runs are validated against a tamper-evident SHA-256 fingerprint:

$$\text{SHA-256}(\text{schema.sql} + \text{invariants.yaml} + \text{canonical TURN\_SPECS})$$

Canonical verification prefix:

```text
🔒 VERIFIED CANONICAL WORKLOAD [aa13421a3cdf]
```

Running custom `--schema` or `--invariants` files automatically tags the output as `CUSTOM WORKLOAD`, keeping benchmark leaderboards strictly reproducible.

---

## The Polycracker Ecosystem

Polydyno is the diagnostic dyno that pinpoints where coding agents fail under load: invariant drift, financial type dilution, temporal ghosting, and retry thrash.

**[Polycracker](https://polycracker.dev)** provides the runtime edge guardrails and MCP tools to prevent context detonation in production: enforcing AST constraints before state commits, compressing prompt history, and eliminating cascading regressions across continuous agent sessions.

---

## PolyScore calibration anchors

These scores are **regression anchors** for the harness scoring model (BSFC / anti-dilution). Changing formulas requires updating this table and re-verifying scores from freshly exported `results/dyno_sheet_<timestamp>.json` files on your machine.

**Sim rows** are reproduced directly from the commands below. **Live rows** are reference outcomes from canonical workload runs; after you execute the listed live flags, PolyScore and telemetry appear in your local `results/dyno_sheet_*.json` (timestamped filenames vary — e.g. `dyno_sheet_20261109_174943.json` — and are never committed to git).

| Run | PolyScore | Structural Torque | Thermal (effective) |
| :--- | :--- | :--- | :--- |
| `polydyno --mode sim` (baseline) | **245** | 233 | A+ [×1.05] |
| `polydyno --mode sim --invariant-mode verify-commit --max-retries 3` | **179** | 218 | B [×0.82] |
| Live gpt-4o baseline — `polydyno --mode live --provider openai --model gpt-4o --load stage2 --invariant-mode baseline` | **245** | 233 | A+ [×1.05] |
| Live gpt-4o verify-commit — `polydyno --mode live --provider openai --model gpt-4o --load stage2 --invariant-mode verify-commit --max-retries 3` | **146** | 324 | D [×0.45] |

Canonical workload verification prefix (unchanged): `aa13421a3cdf`.

---

## License

MIT © 2026 Parrot Software — see [LICENSE](LICENSE).
