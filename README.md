# 🏎️ Polydyno

**By [Polycracker](https://polycracker.com)** — the 50-turn adversarial context drift and invariant benchmark harness for AI coding agents.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Verified Workload](https://img.shields.io/badge/workload-SHA--256%20verified-aa13421a3cdf)

Polydyno runs your agent on a progressive PostgreSQL refactor track: context inflates every turn, **adversarial knocks** (incident threads, EXPLAIN dumps, PM briefs) steer toward bad shortcuts, and **Eddy-Current load stages** dial difficulty. A fixed ruleset (`invariants.yaml`) marks each turn intact or drift. Every completed run exports JSON telemetry, an HTML dyno chart, and a Markdown report with **PolyScore**, **Structural Torque**, **Thermal Grade**, and a tamper-evident workload hash.

---

## Quickstart

### Installation

```bash
git clone https://github.com/your-org/polydyno.git
cd polydyno
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -e .
```

Copy `.env.example` to `.env` before **live** API runs.

### Simulation (no API keys)

Deterministic local runner—CI, regression, and exploring the workload without tokens:

```bash
polydyno --mode sim --load outlaw --open-chart
```

Run artifacts (gitignored by default) go under `results/`:

| File | Purpose |
| --- | --- |
| `dyno_sheet_<timestamp>.json` | Turn telemetry, PolyScore, torque, thermal grade, verification hash |
| `dyno_chart_<timestamp>.html` | Chart.js dyno plot with PolyScore hero card |
| `dyno_report_<timestamp>.md` | Shareable GFM report for READMEs and PRs |

### Live API benchmarks

```bash
polydyno --mode live --provider openai --model gpt-4o --load outlaw
polydyno --mode live --provider anthropic --model claude-sonnet-4-5 --load stage2
```

OpenAI-compatible gateways (vLLM, Ollama, OpenRouter):

```bash
polydyno --mode live --provider openai --model your-model --base-url https://your-host/v1
```

**Eddy-Current load stages** (`--load`):

| Stage | Description |
| --- | --- |
| `stage1` | Baseline workload, lower torque ceiling (×0.4) |
| `stage2` | Adversarial context + temporal traps (×0.7, default) |
| `outlaw` | Stage2 + dual SQL/Python artifacts + heavier knocks (×1.0) |

Other useful flags:

- `--max-turns N` — 1–50 turns (default 50)
- `--stop-on-drift` / `--no-stop-on-drift` — Stop on first invariant break (default: stop)
- `--prompt-file rules.txt` — Your system prompt vs. the same adversarial workload
- `--compare sheet_a.json sheet_b.json` — Head-to-head Markdown dyno report

CLI entry points: **`polydyno`** (primary) and **`polycracker`** (alias)—same harness.

---

## How it works (the 3 cylinders)

Each turn requires a **full** PostgreSQL schema (and outlaw extras). Validation uses three complementary cylinders—not regex alone.

### 1. PostgreSQL AST (`sqlglot`)

DDL is parsed with **sqlglot** and checked against declarative invariants:

- **Referential integrity** — `orders.user_id` stays `NOT NULL` with `fk_user_id` → `users(id) ON DELETE CASCADE`
- **Financial integer precision** — `*_cents` columns stay integer-family types, never floats
- **Identity** — `users.email` remains `NOT NULL` and `UNIQUE`
- **Compiler gate** — zero parse errors in emitted SQL

Retention scoring verifies prior-turn artifacts (tables, indexes, constraints) still exist in the AST.

### 2. Python Pydantic AST (`cross_artifact_type_sync`)

**Outlaw** load adds a second cylinder: ```sql DDL plus ```python `UserModel` / `OrderModel` (Pydantic v2). Python is parsed with **`ast`** and cross-checked with SQL—`user_id` never `Optional`, monetary fields never `float`.

### 3. Temporal state traps (`temporal_state_consistency`)

Multi-turn **rule changes** (not just additive DDL):

- **Turn 12+** — `orders.status` CHECK must allow `disputed`
- **Turn 28+** — CHECK must allow `chargeback_pending` and **forbid** `disputed` (detects attention ghosting)

**Adversarial knocks** layer Slack threads, stack traces, and product noise on top—especially under outlaw load—so agents must reject bad suggestions buried in context.

Sim mode deliberately breaks invariants after turn 16 so you can demo drift, **Black Box Autopsy**, and scoring without API cost.

---

## Understanding PolyScore

PolyScore is the headline dyno number (**0–1000**): structural integrity under Eddy-Current load, adjusted by thermal efficiency.

### Structural torque (0–1000)

Torque measures progress through the **canonical 50-turn** track with invariants intact:

- Intact turns ÷ 50
- Average retention ratio across turns
- **Load multiplier**: `stage1` ×0.4 · `stage2` ×0.7 · `outlaw` ×1.0

Stored on the dyno sheet as `structural_torque`.

### Thermal grade (A+ → F)

Thermal grade scores **useful schema/model output vs. context bloat**:

- Output/input yield, per-turn thermal efficiency, and **thermal bloat** flags (input tokens ≫ artifact size—see `thermal_efficiency` in `invariants.yaml`)
- Heavy bloat can yield **D** or **F** even when structural checks still pass

| Grade | PolyScore multiplier |
| --- | ---: |
| A+ | 1.05 |
| A | 1.00 |
| B | 0.90 |
| C | 0.75 |
| D | 0.55 |
| F | 0.30 |

### Composite PolyScore

```text
polyscore = clamp( round(structural_torque × thermal_multiplier), 0, 1000 )
```

The Rich terminal summary, HTML hero card, JSON (`polyscore`), and Markdown report all show the same headline score plus torque / thermal breakdown.

---

## Black Box Autopsy

When an invariant fails (default: stop on first drift), Polydyno records a **Black Box Autopsy**:

- **Adversarial context** — knocks, incident traces, and thread noise from the failing turn
- **Unified SQL diff** — last good schema vs. broken output (`difflib`-style)

Autopsy data lives in the JSON sheet, the Rich console, the HTML chart, and the Markdown report—so you see *what* broke and *what context* caused it, not only a red status cell.

---

## Verified canonical workload

Benchmark parameters are fingerprinted for tamper-evident sharing:

```text
SHA-256( schema.sql + invariants.yaml + canonical TURN_SPECS )
```

Canonical short prefix: **`aa13421a3cdf`**

Default `schema.sql` + `invariants.yaml` runs display:

`🔒 VERIFIED CANONICAL WORKLOAD [aa13421a3cdf]`

Custom `--schema` / `--invariants` paths produce a different hash (**CUSTOM WORKLOAD**).

---

## The Polycracker ecosystem

Polydyno shows **where** agents lose torque: FK drift, monetary regression, temporal ghosting, dual-artifact desync, and thermal bloat from runaway history. Production fixes usually need **tighter invariants**, **smaller effective context**, and **structured memory**—not longer prompts alone.

**[Polycracker](https://polycracker.com)** provides an MCP server and tooling to stop multi-turn context detonation: enforce rules at the edge, compress what the model re-reads each turn, and keep schema state canonical across long agent sessions.

Run the dyno → read the autopsy and PolyScore → tune with Polycracker MCP so the next pull keeps torque up and thermal grade out of the basement.

---

## License

MIT © 2026 Parrot Software — see [LICENSE](LICENSE).
