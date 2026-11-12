"""Rich live dashboard and JSON export for polycracker-dyno runs."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any

from autopsy import BlackBoxAutopsy, autopsy_diff_to_html, parse_retention_fraction
from workload import (
    CANONICAL_WORKLOAD_VERIFICATION_HASH,
    TURN_SPECS,
    short_workload_verification_hash,
)
from pydantic import BaseModel, Field
from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

DYNO_HEADER = "🏎️ POLYCRACKER DYNO — 50-TURN CONTEXT DRIFT & INVARIANT BENCHMARK"

LOAD_TORQUE_MULTIPLIER: dict[str, float] = {
    "stage1": 0.4,
    "stage2": 0.7,
    "outlaw": 1.0,
}

THERMAL_GRADE_MULTIPLIER: dict[str, float] = {
    "A+": 1.05,
    "A": 1.00,
    "B": 0.90,
    "C": 0.75,
    "D": 0.55,
    "F": 0.30,
}

# BSFC / context runaway thresholds (scoring only — workload hash unchanged)
LEAN_RUN_INPUT_BUDGET = 250_000
CONTEXT_RUNAWAY_INPUT_CAP = 1_100_000
PRE_FAILURE_TOKEN_CAP = 100_000
QUADRATIC_BLOAT_INPUT_MIN = 22_000
QUADRATIC_BLOAT_IO_RATIO = 22.0
RECOVERY_PENALTY_COEFFICIENT = 1.25
EFFECTIVE_THERMAL_FLOOR = 0.45


class TurnRecord(BaseModel):
    turn: int
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cumulative_cost_usd: float
    retention_score: str = "—"
    invariant_intact: bool
    thermal_efficiency_ratio: float = 0.0
    token_multiplier: float = 0.0
    thermal_bloat: bool = False
    status: str | None = None
    notes: str = ""
    initial_failed_invariants: list[str] = Field(default_factory=list)
    repair_attempts: int = 0
    recovery_tokens: int = 0
    cascading_regressions: int = 0
    recovered_cleanly: bool = False
    lines_added: int = 0
    lines_removed: int = 0
    net_diff_lines: int = 0
    collateral_churn_chars: int = 0


class DynoRunSheet(BaseModel):
    started_at: str
    finished_at: str | None = None
    provider: str
    model: str
    total_turns: int = 50
    turns: list[TurnRecord] = Field(default_factory=list)
    autopsy: BlackBoxAutopsy | None = None
    workload_verification_hash: str | None = None
    structural_torque: int | None = None
    thermal_grade: str | None = None
    polyscore: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DynoDashboard:
    """Live terminal dashboard for turn-by-turn dyno telemetry."""

    def __init__(self, console: Console | None = None, total_turns: int = 50) -> None:
        self.console = console or Console()
        self.total_turns = total_turns
        self.turns: list[TurnRecord] = []
        self._live: Live | None = None

    def _status_label(self, row: TurnRecord) -> str:
        if row.status == "API_ERROR":
            return "API ERROR"
        if not row.invariant_intact:
            if row.repair_attempts > 0:
                return f"🔴 DRIFT ({row.repair_attempts}R)"
            return "🔴 DRIFT"
        if row.repair_attempts > 0:
            if row.cascading_regressions > 0:
                return f"🟠 REPAIRED ({row.repair_attempts}R/{row.cascading_regressions}C)"
            return f"🟢 REPAIRED ({row.repair_attempts}R)"
        if row.thermal_bloat:
            return "🟡 BLOAT"
        return "🟢 INTACT"

    def _format_cost(self, cumulative_cost_usd: float) -> str:
        return f"${cumulative_cost_usd:.4f}"

    def _build_table(self) -> Table:
        table = Table(
            title=DYNO_HEADER,
            box=box.ROUNDED,
            show_lines=False,
            expand=False,
            pad_edge=False,
        )
        table.add_column("Turn", justify="center", style="bold cyan", no_wrap=True)
        table.add_column("Tokens (In / Out)", justify="right", no_wrap=True)
        table.add_column("Retention", justify="center", no_wrap=True)
        table.add_column("Cost", justify="right", style="green", no_wrap=True)
        table.add_column("Status", justify="center", no_wrap=True)

        for row in self.turns:
            table.add_row(
                f"{row.turn:02d}/{self.total_turns}",
                f"{row.input_tokens:,} / {row.output_tokens:,}",
                row.retention_score,
                self._format_cost(row.cumulative_cost_usd),
                self._status_label(row),
            )
        if not self.turns:
            table.add_row("—", "—", "—", "—", "—")
        return table

    def _drift_panel(self) -> Panel | None:
        drift_rows = [row for row in self.turns if not row.invariant_intact]
        if not drift_rows:
            return None
        drift = drift_rows[-1]
        body = Text(drift.notes or "Invariant violation detected.", style="bold red")
        return Panel(
            body,
            title=f"[bold red]Critical drift — turn {drift.turn:02d}/{self.total_turns}[/bold red]",
            border_style="red",
            padding=(1, 2),
        )

    def render(self) -> RenderableType:
        table = self._build_table()
        drift = self._drift_panel()
        if drift is None:
            return Panel(table, border_style="bright_blue")
        return Panel(Group(table, drift), border_style="bright_blue")

    def start(self) -> Live:
        self._live = Live(self.render(), console=self.console, refresh_per_second=8)
        self._live.start()
        return self._live

    def stop(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def record_turn(self, row: TurnRecord) -> None:
        self.turns.append(row)
        if self._live is not None:
            self._live.update(self.render())


def load_dyno_sheet(path: str | Path) -> DynoRunSheet:
    return DynoRunSheet.model_validate_json(Path(path).read_text(encoding="utf-8"))


def export_dyno_sheet(sheet: DynoRunSheet, results_dir: str | Path = "results") -> Path:
    out_dir = Path(results_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"dyno_sheet_{stamp}.json"
    path.write_text(sheet.model_dump_json(indent=2), encoding="utf-8")
    return path


def _avg_retention_fraction(turns: list[TurnRecord]) -> float:
    fractions: list[float] = []
    for row in turns:
        frac = parse_retention_fraction(row.retention_score)
        if frac is not None:
            fractions.append(frac)
    if not fractions:
        return 0.0
    return sum(fractions) / len(fractions)


def _turn_bsfc_bloat(input_tokens: int, output_tokens: int, *, schema_sql: str | None = None) -> bool:
    artifact_chars = max(len(schema_sql or ""), max(output_tokens, 0) * 4, 1)
    io_ratio = input_tokens / max(output_tokens, 1)
    char_bloat = input_tokens > (50 * artifact_chars)
    return char_bloat or (
        input_tokens > QUADRATIC_BLOAT_INPUT_MIN and io_ratio > QUADRATIC_BLOAT_IO_RATIO
    )


def _refresh_turn_bsfc_flags(sheet: DynoRunSheet) -> None:
    """Recompute per-turn BSFC multiplier and bloat flags (for rescoring saved sheets)."""
    for row in sheet.turns:
        out = max(row.output_tokens, 0)
        artifact_tokens = max(out, 1)
        row.token_multiplier = row.input_tokens / artifact_tokens
        row.thermal_bloat = _turn_bsfc_bloat(row.input_tokens, row.output_tokens)


def _compute_bsfc_aggregate(sheet: DynoRunSheet) -> dict[str, float | int]:
    turns = sheet.turns
    if not turns:
        return {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "yield_ratio": 0.0,
            "avg_bsfc": 0.0,
            "peak_bsfc": 0.0,
            "bloat_turns": 0,
            "bloat_frac": 0.0,
        }
    total_in = sum(t.input_tokens for t in turns)
    total_out = sum(t.output_tokens for t in turns)
    bsfc_values = [t.input_tokens / max(t.output_tokens, 1) for t in turns]
    bloat_turns = sum(1 for t in turns if t.thermal_bloat)
    return {
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "yield_ratio": total_out / max(total_in, 1),
        "avg_bsfc": sum(bsfc_values) / len(bsfc_values),
        "peak_bsfc": max(bsfc_values),
        "bloat_turns": bloat_turns,
        "bloat_frac": bloat_turns / len(turns),
    }


def _pre_failure_token_base(sheet: DynoRunSheet) -> int:
    """Tokens burned before first unassisted drop (attempt 0 only on drop turn)."""
    _, _, _, _, _, first_drop = _recovery_totals(sheet)
    if first_drop is None:
        return sum(t.input_tokens + t.output_tokens for t in sheet.turns)
    total = 0
    for row in sheet.turns:
        if row.turn < first_drop:
            total += row.input_tokens + row.output_tokens
        elif row.turn == first_drop:
            total += row.input_tokens + row.output_tokens - row.recovery_tokens
            break
    return max(total, 1)


def _post_failure_runaway_tokens(sheet: DynoRunSheet) -> int:
    """All tokens burned on/after first drop, excluding explicit recovery overhead tally."""
    _, _, total_recovery, _, _, first_drop = _recovery_totals(sheet)
    if first_drop is None:
        return 0
    post = 0
    for row in sheet.turns:
        if row.turn >= first_drop:
            post += row.input_tokens + row.output_tokens
    return max(0, post - total_recovery)


def _recovery_totals(sheet: DynoRunSheet) -> tuple[int, int, int, int, int, int | None]:
    """Return (total_retries, total_cascading, total_recovery_tokens, clean_recoveries, turns_with_failures, first_unassisted_drop)."""
    turns = sheet.turns
    total_retries = sum(t.repair_attempts for t in turns)
    total_cascading = sum(t.cascading_regressions for t in turns)
    total_recovery_tokens = sum(t.recovery_tokens for t in turns)
    clean_recoveries = sum(1 for t in turns if t.recovered_cleanly)
    turns_with_failures = sum(
        1 for t in turns if t.repair_attempts > 0 or not t.invariant_intact or t.initial_failed_invariants
    )
    first_unassisted_drop: int | None = None
    for t in turns:
        if t.repair_attempts > 0 or not t.invariant_intact or t.initial_failed_invariants:
            first_unassisted_drop = t.turn
            break
    return (
        total_retries,
        total_cascading,
        total_recovery_tokens,
        clean_recoveries,
        turns_with_failures,
        first_unassisted_drop,
    )


def _multiplier_to_thermal_grade(mult: float) -> str:
    if mult >= 1.03:
        return "A+"
    if mult >= 0.95:
        return "A"
    if mult >= 0.82:
        return "B"
    if mult >= 0.65:
        return "C"
    if mult >= 0.45:
        return "D"
    return "F"


def compute_structural_torque(sheet: DynoRunSheet) -> int:
    """PolyScore structural torque (0–1000), scaled by Eddy-Current load stage and anti-dilution penalties."""
    turns = sheet.turns
    if not turns:
        return 0
    load = str(sheet.metadata.get("load", sheet.metadata.get("profile", "stage2")))
    if load in ("standard", "adversarial"):
        load = "stage1" if load == "standard" else "stage2"
    multiplier = LOAD_TORQUE_MULTIPLIER.get(load, 0.7)
    benchmark_turns = max(len(TURN_SPECS), 1)
    retention = _avg_retention_fraction(turns)

    total_retries, total_cascading, total_recovery_tokens, _, _, _ = _recovery_totals(sheet)
    # Zero-Retry Guard: 100% untouched formula when no retries/regressions/recovery tokens
    if total_retries == 0 and total_cascading == 0 and total_recovery_tokens == 0:
        intact = sum(1 for row in turns if row.invariant_intact)
        turn_factor = intact / benchmark_turns
        score = 1000.0 * multiplier * turn_factor * retention
        return max(0, min(1000, int(round(score))))

    # Anti-Dilution Scoring: turns requiring repair or completed after the first repair earn 25% gross structural weight
    first_repair_turn = next(
        (row.turn for row in turns if row.repair_attempts > 0 or row.initial_failed_invariants),
        None,
    )
    weighted_intact = 0.0
    for row in turns:
        if not row.invariant_intact:
            continue
        if first_repair_turn is not None and row.turn >= first_repair_turn:
            weighted_intact += 0.25
        else:
            weighted_intact += 1.0

    total_input = sum(row.input_tokens for row in turns)
    strict_context_runaway = total_input > LEAN_RUN_INPUT_BUDGET

    if strict_context_runaway:
        run_benchmark = max(min(len(turns), benchmark_turns), 1)
        gross_torque = int(
            round(1000.0 * multiplier * (weighted_intact / run_benchmark) * retention)
        )
    else:
        intact_count = sum(1 for row in turns if row.invariant_intact)
        unassisted_equiv_torque = 1000.0 * (weighted_intact / benchmark_turns) * retention
        full_track_gross_torque = 1000.0 * multiplier * (intact_count / benchmark_turns) * retention
        gross_torque = int(round((full_track_gross_torque * 0.5) + (unassisted_equiv_torque * 0.6)))

    retry_tax = total_retries * 15
    regression_penalty = total_cascading * 45
    net_torque = max(0, gross_torque - retry_tax - regression_penalty)
    return max(0, min(1000, net_torque))


def compute_effective_thermal(sheet: DynoRunSheet) -> tuple[str, float]:
    """Return (thermal_grade, effective_thermal_multiplier) with recovery token overhead penalty."""
    base_grade = compute_thermal_grade(sheet)
    base_mult = thermal_grade_multiplier(base_grade)
    total_retries, total_cascading, total_recovery_tokens, _, _, _ = _recovery_totals(sheet)
    if total_retries == 0 and total_cascading == 0 and total_recovery_tokens == 0:
        return base_grade, base_mult

    total_in = sum(row.input_tokens for row in sheet.turns)
    if total_in > LEAN_RUN_INPUT_BUDGET:
        pre_failure_base = min(_pre_failure_token_base(sheet), PRE_FAILURE_TOKEN_CAP)
        recovery_overhead_ratio = total_recovery_tokens / max(pre_failure_base, 1)
    else:
        turns = sheet.turns
        total_tokens = sum(row.input_tokens + row.output_tokens for row in turns)
        base_turn_tokens = max(1, total_tokens - total_recovery_tokens)
        recovery_overhead_ratio = total_recovery_tokens / base_turn_tokens
    penalized_mult = max(
        EFFECTIVE_THERMAL_FLOOR,
        base_mult - (recovery_overhead_ratio * RECOVERY_PENALTY_COEFFICIENT),
    )
    penalized_grade = _multiplier_to_thermal_grade(penalized_mult)
    return penalized_grade, penalized_mult


def compute_thermal_grade(sheet: DynoRunSheet) -> str:
    """PolyScore thermal letter grade from BSFC / cumulative token burn."""
    turns = sheet.turns
    if not turns:
        return "F"
    agg = _compute_bsfc_aggregate(sheet)
    total_in = int(agg["total_input_tokens"])
    yield_ratio = float(agg["yield_ratio"])
    bloat_frac = float(agg["bloat_frac"])
    avg_eff = sum(row.thermal_efficiency_ratio for row in turns) / len(turns)

    if yield_ratio < 0.025 or bloat_frac >= 0.55:
        return "F"
    if total_in > CONTEXT_RUNAWAY_INPUT_CAP or yield_ratio < 0.035 or bloat_frac >= 0.45:
        return "D"
    if total_in > LEAN_RUN_INPUT_BUDGET or yield_ratio < 0.042 or bloat_frac >= 0.30:
        return "C"
    if total_in > 450_000 or yield_ratio < 0.05 or bloat_frac >= 0.15:
        return "B"
    if total_in <= 150_000 and bloat_frac < 0.05 and yield_ratio >= 0.06 and avg_eff >= 0.06:
        return "A+"
    if total_in <= LEAN_RUN_INPUT_BUDGET and yield_ratio >= 0.05:
        return "A"
    return "B"


def thermal_grade_multiplier(thermal_grade: str) -> float:
    return THERMAL_GRADE_MULTIPLIER.get(thermal_grade, THERMAL_GRADE_MULTIPLIER["F"])


def calculate_polyscore(
    structural_torque: int,
    thermal_grade: str,
    *,
    thermal_multiplier_override: float | None = None,
) -> int:
    """Headline PolyScore (0–1000): structural torque adjusted by thermal grade."""
    multiplier = (
        thermal_multiplier_override
        if thermal_multiplier_override is not None
        else thermal_grade_multiplier(thermal_grade)
    )
    raw = round(structural_torque * multiplier)
    return max(0, min(1000, raw))


def _output_efficiency_totals(sheet: DynoRunSheet) -> dict[str, Any]:
    """Aggregate isolated Output Efficiency metrics without touching PolyScore or Torque."""
    turns = sheet.turns
    n = max(len(turns), 1)
    total_added = sum(t.lines_added for t in turns)
    total_removed = sum(t.lines_removed for t in turns)
    total_net_diff = sum(t.net_diff_lines for t in turns)
    total_churn = sum(t.collateral_churn_chars for t in turns)
    peak_turn = max(turns, key=lambda t: t.collateral_churn_chars, default=None)
    return {
        "total_lines_added": total_added,
        "total_lines_removed": total_removed,
        "total_net_diff_lines": total_net_diff,
        "avg_lines_added": round(total_added / n, 2),
        "avg_lines_removed": round(total_removed / n, 2),
        "avg_net_diff_lines": round(total_net_diff / n, 2),
        "total_collateral_churn_chars": total_churn,
        "avg_collateral_churn_chars": round(total_churn / n, 1),
        "peak_churn_turn": peak_turn.turn if peak_turn is not None else None,
        "peak_churn_chars": peak_turn.collateral_churn_chars if peak_turn is not None else 0,
    }


def finalize_polyscore(sheet: DynoRunSheet) -> None:
    _refresh_turn_bsfc_flags(sheet)
    agg = _compute_bsfc_aggregate(sheet)
    pre_failure = _pre_failure_token_base(sheet)
    post_failure = _post_failure_runaway_tokens(sheet)
    total_retries, _, total_recovery, _, _, first_drop = _recovery_totals(sheet)
    sheet.metadata["bsfc_avg"] = round(float(agg["avg_bsfc"]), 2)
    sheet.metadata["bsfc_peak"] = round(float(agg["peak_bsfc"]), 2)
    sheet.metadata["yield_ratio"] = round(float(agg["yield_ratio"]), 4)
    sheet.metadata["bloat_turns"] = int(agg["bloat_turns"])
    sheet.metadata["pre_failure_tokens"] = pre_failure
    sheet.metadata["post_failure_runaway_tokens"] = post_failure
    sheet.metadata["output_efficiency"] = _output_efficiency_totals(sheet)
    if first_drop is not None and pre_failure > 0:
        sheet.metadata["context_runaway_factor"] = round(
            int(agg["total_input_tokens"]) / pre_failure,
            2,
        )
    recovery_anchor = min(pre_failure, PRE_FAILURE_TOKEN_CAP)
    if total_recovery > 0 and recovery_anchor > 0:
        sheet.metadata["recovery_overhead_vs_predrop_pct"] = round(
            100.0 * total_recovery / recovery_anchor,
            1,
        )

    sheet.structural_torque = compute_structural_torque(sheet)
    base_grade = compute_thermal_grade(sheet)
    grade, effective_mult = compute_effective_thermal(sheet)
    sheet.thermal_grade = grade
    sheet.metadata["base_thermal_grade"] = base_grade
    sheet.metadata["effective_thermal_multiplier"] = round(effective_mult, 4)
    sheet.polyscore = calculate_polyscore(
        sheet.structural_torque,
        sheet.thermal_grade,
        thermal_multiplier_override=effective_mult,
    )


def workload_verification_badge(sheet: DynoRunSheet) -> str:
    full = sheet.workload_verification_hash or ""
    short = short_workload_verification_hash(full) if full else "unknown"
    if full and full == CANONICAL_WORKLOAD_VERIFICATION_HASH:
        return f"🔒 VERIFIED CANONICAL WORKLOAD [{short}]"
    if full:
        return f"🔒 CUSTOM WORKLOAD [{short}]"
    return "🔒 WORKLOAD UNVERIFIED"


def _first_drift_turn(sheet: DynoRunSheet) -> int | None:
    for row in sheet.turns:
        if not row.invariant_intact:
            return row.turn
    return None


def _ascii_token_curve(values: list[int], width: int = 48) -> str:
    if not values:
        return "(no data)"
    blocks = "▁▂▃▄▅▆▇█"
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return blocks[0] * min(len(values), width)
    step = max(len(values) // width, 1)
    sampled = values[::step][:width]
    lines: list[str] = []
    for value in sampled:
        idx = int((value - lo) / (hi - lo) * (len(blocks) - 1))
        lines.append(blocks[idx])
    return "".join(lines)


def _sheet_summary_row(label: str, sheet: DynoRunSheet) -> dict[str, str]:
    turns = sheet.turns
    total_in = sum(t.input_tokens for t in turns)
    total_cached = sum(t.cached_input_tokens for t in turns)
    total_out = sum(t.output_tokens for t in turns)
    drift = _first_drift_turn(sheet)
    load_stage = sheet.metadata.get("load", sheet.metadata.get("profile", "n/a"))
    intact = sum(1 for t in turns if t.invariant_intact)
    avg_eff = sum(t.thermal_efficiency_ratio for t in turns) / len(turns) if turns else 0.0
    avg_mult = sum(t.token_multiplier for t in turns) / len(turns) if turns else 0.0
    bloat_turns = sum(1 for t in turns if t.thermal_bloat)
    torque = sheet.structural_torque
    if torque is None:
        torque = compute_structural_torque(sheet)
    eff_grade, eff_mult = compute_effective_thermal(sheet)
    grade = sheet.thermal_grade or eff_grade
    thermal_mult = float(sheet.metadata.get("effective_thermal_multiplier", eff_mult))
    score = sheet.polyscore
    if score is None:
        score = calculate_polyscore(torque, grade, thermal_multiplier_override=thermal_mult)
    w_hash = sheet.workload_verification_hash or "n/a"
    invariant_mode = str(sheet.metadata.get("invariant_mode", "baseline"))
    total_retries, total_cascading, total_recovery_tokens, clean_recoveries, turns_with_failures, first_drop = _recovery_totals(sheet)
    base_tokens = max(1, (total_in + total_out) - total_recovery_tokens)
    overhead_pct = (total_recovery_tokens / base_tokens) * 100.0 if total_recovery_tokens > 0 else 0.0
    return {
        "label": label,
        "model": f"{sheet.provider}/{sheet.model}",
        "polyscore": str(score),
        "structural_torque": str(torque),
        "thermal_grade": grade,
        "thermal_multiplier": f"{thermal_mult:.2f}",
        "workload_hash": short_workload_verification_hash(w_hash)
        if w_hash not in ("", "n/a")
        else "n/a",
        "turns": str(len(turns)),
        "load": str(load_stage),
        "profile": str(load_stage),
        "invariant_mode": invariant_mode,
        "drift_turn": str(drift) if drift else "none",
        "first_unassisted_drop": str(first_drop) if first_drop else "none",
        "total_retries": str(total_retries),
        "recovery_tokens": f"+{total_recovery_tokens:,} ({overhead_pct:.1f}%)",
        "clean_recoveries": f"{clean_recoveries}/{turns_with_failures}",
        "cascading_regressions": str(total_cascading),
        "pass_rate": f"{intact}/{len(turns)}" if turns else "0/0",
        "input_tokens": f"{total_in:,}",
        "cached_input_tokens": f"{total_cached:,}",
        "output_tokens": f"{total_out:,}",
        "final_cost": f"${turns[-1].cumulative_cost_usd:.4f}" if turns else "$0.0000",
        "thermal_efficiency": f"{avg_eff:.4f}",
        "token_multiplier": f"{avg_mult:.1f}x",
        "thermal_bloat_turns": str(bloat_turns),
    }


def _readme_badge_snippet(sheet: DynoRunSheet) -> str:
    drift = _first_drift_turn(sheet)
    intact = all(t.invariant_intact for t in sheet.turns) if sheet.turns else False
    if intact and drift is None:
        badge = "![Polycracker Dyno](https://img.shields.io/badge/dyno-invariants%20intact-brightgreen)"
        status = "passing"
    else:
        turn = drift if drift is not None else "?"
        badge = (
            f"![Polycracker Dyno]"
            f"(https://img.shields.io/badge/dyno-drift%20turn%20{turn}-red)"
        )
        status = "drift"
    return f"""### README badge snippet ({status})

```markdown
{badge}
```
"""


def _cta_footer_markdown() -> str:
    return dedent(
        """
        ---

        ## Polycracker

        > **Stop Context Detonation.** Eliminate multi-turn drift and cut token overhead with the
        > **Polycracker MCP Server** → [polycracker.dev](https://polycracker.dev)
        """
    ).strip()


def _telemetry_table_markdown(sheet: DynoRunSheet) -> str:
    header = (
        "| Turn | Input | Output | Cached In | Retries | Recov Tok | Cascading | Retention | Cost | Status |\n"
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: | :---: |"
    )
    rows: list[str] = []
    for t in sheet.turns:
        if t.status == "API_ERROR":
            status = "API_ERROR"
        elif not t.invariant_intact:
            status = f"DRIFT ({t.repair_attempts}R)" if t.repair_attempts > 0 else "DRIFT"
        elif t.repair_attempts > 0:
            status = (
                f"REPAIRED ({t.repair_attempts}R/{t.cascading_regressions}C)"
                if t.cascading_regressions > 0
                else f"REPAIRED ({t.repair_attempts}R)"
            )
        elif t.thermal_bloat:
            status = "BLOAT"
        else:
            status = "INTACT"
        rows.append(
            f"| {t.turn} | {t.input_tokens:,} | {t.output_tokens:,} | {t.cached_input_tokens:,} "
            f"| {t.repair_attempts} | {t.recovery_tokens:,} | {t.cascading_regressions} "
            f"| {t.retention_score} | ${t.cumulative_cost_usd:.4f} | {status} |"
        )
    return header + "\n" + "\n".join(rows)


def _kpi_block_markdown(row: dict[str, str]) -> str:
    return dedent(
        f"""
        ### PolyScore — {row["label"]}

        - **PolyScore:** **{row["polyscore"]}** / 1000
        - **Breakdown:** Torque {row["structural_torque"]}/1000 · Thermal {row["thermal_grade"]} (×{row["thermal_multiplier"]}) · Load `{row["load"]}` · Mode `{row["invariant_mode"]}`
        - **Workload verification:** `{row["workload_hash"]}`

        ### KPI summary — {row["label"]}

        - **Model:** `{row["model"]}`
        - **Eddy-Current load:** `{row["load"]}`
        - **Invariant mode:** `{row["invariant_mode"]}`
        - **Turns completed:** {row["turns"]}
        - **Invariant pass rate:** {row["pass_rate"]}
        - **First unassisted drop:** {row["first_unassisted_drop"]}
        - **Terminal drift turn:** {row["drift_turn"]}
        - **Total repair retries:** {row["total_retries"]}
        - **Recovery token overhead:** {row["recovery_tokens"]}
        - **Clean recoveries:** {row["clean_recoveries"]}
        - **Cascading regressions:** {row["cascading_regressions"]}
        - **Total input tokens:** {row["input_tokens"]}
        - **Cached input tokens:** {row["cached_input_tokens"]}
        - **Total output tokens:** {row["output_tokens"]}
        - **Final cost (USD):** {row["final_cost"]}
        - **Avg thermal efficiency:** {row["thermal_efficiency"]}
        - **Avg token multiplier:** {row["token_multiplier"]}
        - **Thermal bloat turns:** {row["thermal_bloat_turns"]}
        """
    ).strip()


def _autopsy_markdown(sheet: DynoRunSheet) -> str:
    if sheet.autopsy is None:
        return "_No Black Box Autopsy recorded (run completed without invariant drift)._"
    autopsy = sheet.autopsy
    noise = autopsy.adversarial_context_noise or "(no context noise captured)"
    diff = autopsy.unified_diff or "(empty diff)"
    return dedent(
        f"""
        ## Black Box Autopsy (turn {autopsy.previous_turn:02d} → {autopsy.drift_turn:02d})

        ### Adversarial context at drift

        ```
        {noise.strip()}
        ```

        ### Unified SQL diff

        ```diff
        {diff.strip()}
        ```
        """
    ).strip()


def export_markdown_report(
    sheet: DynoRunSheet,
    results_dir: str | Path = "results",
    *,
    compare_sheet: DynoRunSheet | None = None,
) -> Path:
    """Export GitHub-flavored Markdown dyno report for one run (optional head-to-head compare)."""
    out_root = Path(results_dir)
    if out_root.suffix.lower() == ".md":
        out_path = out_root
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = out_root / f"dyno_report_{stamp}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    row = _sheet_summary_row("Run", sheet)
    sparkline = _ascii_token_curve([t.input_tokens for t in sheet.turns])
    title = (
        "# Polycracker Dyno — Head-to-Head Report"
        if compare_sheet is not None
        else "# Polycracker Dyno — Run Report"
    )

    sections = [
        title,
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`  ",
        f"Run window: `{sheet.started_at}` → `{sheet.finished_at or 'n/a'}`",
        "",
        f"> {workload_verification_badge(sheet)}",
        "",
        _readme_badge_snippet(sheet),
        "",
        _kpi_block_markdown(row),
        "",
        "### Input token growth (ASCII)",
        "",
        "```text",
        sparkline,
        "```",
        "",
        "## Turn telemetry",
        "",
        _telemetry_table_markdown(sheet),
        "",
        _autopsy_markdown(sheet),
    ]

    if compare_sheet is not None:
        row_b = _sheet_summary_row("Run B", compare_sheet)
        curve_a = _ascii_token_curve([t.input_tokens for t in sheet.turns])
        curve_b = _ascii_token_curve([t.input_tokens for t in compare_sheet.turns])
        sections.extend(
            [
                "",
                "## Head-to-head comparison",
                "",
                "| Metric | Run A | Run B |",
                "| --- | --- | --- |",
                f"| Model | `{row['model']}` | `{row_b['model']}` |",
                f"| Load | `{row['load']}` | `{row_b['load']}` |",
                f"| Invariant mode | `{row['invariant_mode']}` | `{row_b['invariant_mode']}` |",
                f"| PolyScore | **{row['polyscore']}** | **{row_b['polyscore']}** |",
                f"| Turns | {row['turns']} | {row_b['turns']} |",
                f"| First unassisted drop | {row['first_unassisted_drop']} | {row_b['first_unassisted_drop']} |",
                f"| Terminal drift turn | {row['drift_turn']} | {row_b['drift_turn']} |",
                f"| Repair retries | {row['total_retries']} | {row_b['total_retries']} |",
                f"| Cascading regressions | {row['cascading_regressions']} | {row_b['cascading_regressions']} |",
                f"| Recovery overhead | {row['recovery_tokens']} | {row_b['recovery_tokens']} |",
                f"| Input tokens | {row['input_tokens']} | {row_b['input_tokens']} |",
                f"| Final cost | {row['final_cost']} | {row_b['final_cost']} |",
                "",
                "```text",
                f"A: {curve_a}",
                f"B: {curve_b}",
                "```",
            ]
        )

    sections.extend(["", _cta_footer_markdown(), ""])
    out_path.write_text("\n".join(sections), encoding="utf-8")
    return out_path


def export_markdown_report_from_paths(
    sheet_a_path: str | Path,
    sheet_b_path: str | Path,
    results_dir: str | Path = "results",
) -> Path:
    """Compare two dyno sheet JSON files in a Markdown report."""
    return export_markdown_report(
        load_dyno_sheet(sheet_a_path),
        results_dir,
        compare_sheet=load_dyno_sheet(sheet_b_path),
    )


def render_summary(sheet: DynoRunSheet, console: Console | None = None) -> None:
    console = console or Console()
    turns = sheet.turns
    if not turns:
        console.print("[yellow]No turn data to summarize.[/yellow]")
        return

    total_input = sum(t.input_tokens for t in turns)
    total_cached = sum(t.cached_input_tokens for t in turns)
    total_output = sum(t.output_tokens for t in turns)
    total_latency = sum(t.latency_ms for t in turns)
    final_cost = turns[-1].cumulative_cost_usd
    intact_count = sum(1 for t in turns if t.invariant_intact)
    drift_turns = [t.turn for t in turns if not t.invariant_intact]
    first_drift = drift_turns[0] if drift_turns else None

    summary = Table(title="Dyno Benchmark Summary", box=box.ROUNDED, expand=False)
    summary.add_column("Metric", style="bold", no_wrap=True)
    summary.add_column("Value", justify="right", no_wrap=True)
    load_stage = sheet.metadata.get("load", sheet.metadata.get("profile", "n/a"))
    avg_eff = sum(t.thermal_efficiency_ratio for t in turns) / len(turns)
    _refresh_turn_bsfc_flags(sheet)
    agg = _compute_bsfc_aggregate(sheet)
    avg_bsfc = float(agg["avg_bsfc"])
    peak_bsfc = float(agg["peak_bsfc"])
    yield_ratio = float(agg["yield_ratio"])
    bloat_turns = int(agg["bloat_turns"])
    torque = sheet.structural_torque if sheet.structural_torque is not None else compute_structural_torque(sheet)
    eff_grade, eff_mult = compute_effective_thermal(sheet)
    grade = sheet.thermal_grade or eff_grade
    thermal_mult = float(sheet.metadata.get("effective_thermal_multiplier", eff_mult))
    score = (
        sheet.polyscore
        if sheet.polyscore is not None
        else calculate_polyscore(torque, grade, thermal_multiplier_override=thermal_mult)
    )
    verify_badge = workload_verification_badge(sheet)

    hero = Text()
    hero.append("🏎️ POLYSCORE: ", style="bold")
    hero.append(f"{score}", style="bold bright_green")
    hero.append(" / 1000", style="bold")
    hero.append("\n")
    hero.append(
        f"(Torque: {torque}/1000 | Thermal: {grade} [x{thermal_mult:.2f}] | Load: {load_stage})",
        style="dim",
    )
    hero.append("\n")
    hero.append(verify_badge, style="cyan")
    polyscore_hero = Panel(hero, border_style="bright_green", padding=(1, 2))

    summary.add_row("Eddy-Current load", str(load_stage))
    summary.add_row("Provider / Model", f"{sheet.provider} / {sheet.model}")
    summary.add_row("Turns completed", str(len(turns)))
    summary.add_row("Effective yield (out/in)", f"{yield_ratio:.1%}")
    summary.add_row("BSFC token bloat (in/out)", f"{avg_bsfc:.1f}x avg (peak {peak_bsfc:.1f}x)")
    summary.add_row("Avg thermal efficiency", f"{avg_eff:.4f}")
    summary.add_row("Thermal bloat turns", str(bloat_turns))
    runaway = sheet.metadata.get("context_runaway_factor")
    if runaway is not None:
        summary.add_row("Context runaway vs pre-drop", f"{runaway:.1f}x total input")
    post_fail = sheet.metadata.get("post_failure_runaway_tokens")
    if post_fail:
        summary.add_row("Post-failure token burn", f"{int(post_fail):,} tokens")
    summary.add_row("Invariant intact rate", f"{intact_count}/{len(turns)}")
    summary.add_row("First drift turn", str(first_drift) if first_drift else "none")
    summary.add_row("Total input tokens", f"{total_input:,}")
    summary.add_row("Cached input tokens", f"{total_cached:,}")
    summary.add_row("Total output tokens", f"{total_output:,}")
    summary.add_row("Total latency (ms)", f"{total_latency:,.1f}")
    summary.add_row("Final cumulative cost (USD)", f"${final_cost:.4f}")

    invariant_mode = str(sheet.metadata.get("invariant_mode", "baseline"))
    max_retries_cfg = int(sheet.metadata.get("max_retries", 0) or 0)
    (
        total_retries,
        total_cascading,
        total_recovery_tokens,
        clean_recoveries,
        turns_with_failures,
        first_unassisted_drop,
    ) = _recovery_totals(sheet)
    recovery_anchor = min(_pre_failure_token_base(sheet), PRE_FAILURE_TOKEN_CAP)
    overhead_pct = (
        (total_recovery_tokens / recovery_anchor) * 100.0
        if total_recovery_tokens > 0 and recovery_anchor > 0
        else 0.0
    )
    thermal_penalty_pct = overhead_pct * RECOVERY_PENALTY_COEFFICIENT
    base_thermal = str(sheet.metadata.get("base_thermal_grade", grade))
    retry_and_regression_tax = (total_retries * 15) + (total_cascading * 45)

    containment = Table(
        title="POST-FAILURE CONTAINMENT & RECOVERY",
        box=box.ROUNDED,
        expand=False,
    )
    containment.add_column("Metric", style="bold", no_wrap=True)
    containment.add_column("Value", justify="right", no_wrap=True)
    if invariant_mode == "baseline" and max_retries_cfg == 0 and total_retries == 0:
        containment.add_row("Invariant Mode", "baseline")
        containment.add_row("Self-Repair Status", "N/A (Unassisted Baseline)")
        containment.add_row(
            "First Unassisted Drop",
            f"Turn {first_unassisted_drop}" if first_unassisted_drop else "none",
        )
        containment.add_row(
            "Terminal Drift Turn",
            f"Turn {first_drift}" if first_drift else "none",
        )
    else:
        containment.add_row("Invariant Mode", invariant_mode)
        containment.add_row(
            "First Unassisted Drop",
            f"Turn {first_unassisted_drop}" if first_unassisted_drop else "none",
        )
        containment.add_row(
            "Terminal Drift Turn",
            f"Turn {first_drift}" if first_drift else "none",
        )
        containment.add_row("Total Repair Retries", str(total_retries))
        containment.add_row(
            "Recovery Token Overhead",
            f"+{total_recovery_tokens:,} tokens (+{overhead_pct:.1f}% vs pre-drop · -{thermal_penalty_pct:.1f}% thermal mult)",
        )
        containment.add_row(
            "Thermal grade (base → effective)",
            f"{base_thermal} → {grade} [x{thermal_mult:.2f}]",
        )
        containment.add_row(
            "Clean Recoveries",
            f"{clean_recoveries} / {turns_with_failures}",
        )
        containment.add_row(
            "Cascading Regressions",
            f"{total_cascading} (Secondary invariants broken)",
        )
        containment.add_row(
            "Retry & Regression Tax",
            f"-{retry_and_regression_tax} pts ({total_retries} retries, {total_cascading} cascading breaks)",
        )

    drift_line = Text()
    if first_drift:
        drift_line.append("Critical drift detected", style="bold red")
        drift_line.append(f" starting at turn {first_drift}.")
    else:
        drift_line.append("All turns preserved fk_user_id.", style="bold green")

    input_curve = [t.input_tokens for t in turns]
    sparkline = _ascii_token_curve(input_curve)
    spark_panel = Panel(
        Text.from_markup(
            f"[bold]Input token trend[/bold]  [cyan]{sparkline}[/cyan]\n"
            f"[dim]min {min(input_curve):,} → max {max(input_curve):,} tokens "
            f"({len(turns)} turns)[/dim]"
        ),
        title="Context inflation sparkline",
        border_style="cyan",
        padding=(0, 1),
    )

    out_eff = _output_efficiency_totals(sheet)
    output_efficiency_table = Table(
        title="OUTPUT EFFICIENCY (ISOLATED)",
        box=box.ROUNDED,
        expand=False,
    )
    output_efficiency_table.add_column("Metric", style="bold", no_wrap=True)
    output_efficiency_table.add_column("Value", justify="right", no_wrap=True)
    output_efficiency_table.add_row(
        "Net Diff (Cumulative)",
        f"+{out_eff['total_lines_added']:,} / -{out_eff['total_lines_removed']:,} lines "
        f"({out_eff['total_net_diff_lines']:,} lines touched)",
    )
    output_efficiency_table.add_row(
        "Avg Net Diff / Turn",
        f"+{out_eff['avg_lines_added']:.1f} / -{out_eff['avg_lines_removed']:.1f} lines "
        f"({out_eff['avg_net_diff_lines']:.1f} avg)",
    )
    output_efficiency_table.add_row(
        "Collateral Churn (Excess Edit Dist)",
        f"{out_eff['total_collateral_churn_chars']:,} chars rewritten "
        f"({out_eff['avg_collateral_churn_chars']:,.1f}/turn)",
    )
    peak_turn_label = (
        f"Turn {out_eff['peak_churn_turn']} ({out_eff['peak_churn_chars']:,} chars)"
        if out_eff["peak_churn_turn"] is not None
        else "none"
    )
    output_efficiency_table.add_row("Peak Turn Churn", peak_turn_label)

    parts: list[RenderableType] = [
        polyscore_hero,
        output_efficiency_table,
        summary,
        containment,
        spark_panel,
        drift_line,
    ]
    first_drift_row = next((t for t in turns if not t.invariant_intact), None)
    if first_drift_row is not None:
        parts.append(
            Panel(
                Text(first_drift_row.notes, style="bold red"),
                title=f"[bold red]Invariant dropped — turn {first_drift_row.turn:02d}[/bold red]",
                border_style="red",
                padding=(1, 2),
            )
        )

    console.print()
    console.print(Panel(Group(*parts), border_style="green"))


def _cumulative_torque_series(sheet: DynoRunSheet) -> list[int]:
    """Compute progressive Structural Torque at each turn 1..N for the Power Band chart."""
    turns = sheet.turns
    if not turns:
        return []
    series: list[int] = []
    for idx in range(1, len(turns) + 1):
        partial = DynoRunSheet(
            started_at=sheet.started_at,
            finished_at=sheet.finished_at,
            provider=sheet.provider,
            model=sheet.model,
            total_turns=sheet.total_turns,
            turns=turns[:idx],
            metadata=dict(sheet.metadata),
        )
        series.append(compute_structural_torque(partial))
    return series


def export_html_dyno_chart(
    sheet: DynoRunSheet,
    results_dir: str | Path = "results",
) -> Path:
    """Self-contained dark-mode HTML dashboard with dual Chart.js dyno plots."""
    turns = sheet.turns
    labels = [str(t.turn) for t in turns]
    input_tokens = [t.input_tokens for t in turns]
    recovery_tokens_series = [t.recovery_tokens for t in turns]
    repair_attempts_series = [t.repair_attempts for t in turns]
    cascading_series = [t.cascading_regressions for t in turns]
    costs = [t.cumulative_cost_usd for t in turns]
    collateral_churn_series = [t.collateral_churn_chars for t in turns]
    net_diff_series = [t.net_diff_lines for t in turns]
    lines_added_series = [t.lines_added for t in turns]
    lines_removed_series = [t.lines_removed for t in turns]
    torque_series = _cumulative_torque_series(sheet)
    retention_pct = []
    for t in turns:
        frac = parse_retention_fraction(t.retention_score)
        retention_pct.append(round(frac * 100, 2) if frac is not None else None)

    out_eff = _output_efficiency_totals(sheet)
    peak_churn_label = (
        f"Turn {out_eff['peak_churn_turn']} ({out_eff['peak_churn_chars']:,} chars)"
        if out_eff["peak_churn_turn"] is not None
        else "none"
    )

    drift_turn = _first_drift_turn(sheet)
    total_input = sum(t.input_tokens for t in turns)
    total_output = sum(t.output_tokens for t in turns)
    intact = sum(1 for t in turns if t.invariant_intact)
    final_cost = turns[-1].cumulative_cost_usd if turns else 0.0
    pass_rate = f"{intact}/{len(turns)}" if turns else "0/0"
    load_stage = sheet.metadata.get("load", sheet.metadata.get("profile", "n/a"))
    invariant_mode = str(sheet.metadata.get("invariant_mode", "baseline"))
    (
        total_retries,
        total_cascading,
        total_recovery_tokens,
        clean_recoveries,
        turns_with_failures,
        first_unassisted_drop,
    ) = _recovery_totals(sheet)
    _refresh_turn_bsfc_flags(sheet)
    agg = _compute_bsfc_aggregate(sheet)
    recovery_anchor = min(_pre_failure_token_base(sheet), PRE_FAILURE_TOKEN_CAP)
    overhead_pct = (
        (total_recovery_tokens / recovery_anchor) * 100.0
        if total_recovery_tokens > 0 and recovery_anchor > 0
        else 0.0
    )
    thermal_penalty_pct = overhead_pct * RECOVERY_PENALTY_COEFFICIENT
    retry_and_regression_tax = (total_retries * 15) + (total_cascading * 45)
    avg_eff = sum(t.thermal_efficiency_ratio for t in turns) / len(turns) if turns else 0.0
    avg_bsfc = float(agg["avg_bsfc"])
    peak_bsfc = float(agg["peak_bsfc"])
    yield_ratio = float(agg["yield_ratio"])
    bloat_turns = int(agg["bloat_turns"])
    runaway = sheet.metadata.get("context_runaway_factor")
    post_fail_tokens = int(sheet.metadata.get("post_failure_runaway_tokens") or 0)
    torque = sheet.structural_torque if sheet.structural_torque is not None else compute_structural_torque(sheet)
    eff_grade, eff_mult = compute_effective_thermal(sheet)
    grade = sheet.thermal_grade or eff_grade
    base_thermal = str(sheet.metadata.get("base_thermal_grade", grade))
    thermal_mult = float(sheet.metadata.get("effective_thermal_multiplier", eff_mult))
    score = (
        sheet.polyscore
        if sheet.polyscore is not None
        else calculate_polyscore(torque, grade, thermal_multiplier_override=thermal_mult)
    )
    verify_badge = workload_verification_badge(sheet)
    hero_subtext = f"Torque {torque} · Thermal {grade} ({thermal_mult:.2f}x) · Load {load_stage} · Mode {invariant_mode}"

    drift_turn_js = "null" if drift_turn is None else str(drift_turn)

    diff_html = autopsy_diff_to_html(sheet.autopsy)
    noise_html = ""
    if sheet.autopsy and sheet.autopsy.adversarial_context_noise:
        from html import escape

        noise_html = escape(sheet.autopsy.adversarial_context_noise)

    out_root = Path(results_dir)
    if out_root.suffix.lower() == ".html":
        out_path = out_root
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = out_root / f"dyno_chart_{stamp}.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Polycracker Dyno — {sheet.provider}/{sheet.model}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {{
      --bg: #0d1117;
      --panel: #161b22;
      --text: #e6edf3;
      --muted: #8b949e;
      --accent: #58a6ff;
      --good: #3fb950;
      --bad: #ff4d4f;
      --border: #30363d;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    .wrap {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }}
    h1 {{ font-size: 1.35rem; margin: 0 0 8px; }}
    .sub {{ color: var(--muted); margin-bottom: 20px; font-size: 0.9rem; }}
    .kpis {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }}
    .kpi {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px;
    }}
    .kpi label {{ display: block; font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }}
    .kpi strong {{ font-size: 1.15rem; }}
    .kpi.hero {{
      grid-column: 1 / -1;
      border-color: #388bfd;
      background: linear-gradient(135deg, #1c2333 0%, #161b22 100%);
    }}
    .kpi.hero .polyscore-value {{
      display: block;
      font-size: 2.75rem;
      font-weight: 700;
      line-height: 1.1;
      margin: 4px 0 8px;
      color: var(--good);
    }}
    .kpi.hero .poly-sub {{ font-size: 0.95rem; color: var(--muted); margin-top: 4px; }}
    .verify {{
      display: inline-block;
      margin-top: 8px;
      font-size: 0.82rem;
      color: var(--good);
      font-family: ui-monospace, monospace;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px;
      margin-bottom: 20px;
    }}
    canvas {{ max-height: 420px; }}
    .autopsy-noise {{
      white-space: pre-wrap;
      font-size: 0.85rem;
      color: var(--muted);
      border-left: 3px solid #d29922;
      padding-left: 12px;
      margin-bottom: 16px;
    }}
    .diff-wrap {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.78rem;
      overflow-x: auto;
      border-radius: 8px;
      border: 1px solid var(--border);
      padding: 8px;
      background: #010409;
    }}
    .diff-add {{ color: #3fb950; }}
    .diff-del {{ color: #ff7b72; }}
    .diff-hunk {{ color: var(--accent); }}
    .diff-meta {{ color: var(--muted); }}
    .diff-ctx {{ color: var(--text); }}
    .cta {{
      margin-top: 28px;
      padding: 16px 18px;
      border-radius: 10px;
      border: 1px solid #238636;
      background: linear-gradient(90deg, #122117, #161b22);
      font-size: 0.95rem;
    }}
    .cta a {{ color: #58a6ff; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>🏎️ Polycracker Dyno Chart</h1>
    <p class="sub">Run {sheet.started_at} → {sheet.finished_at or "in progress"} · profile {sheet.metadata.get("profile", "n/a")}</p>
    <div class="kpis">
      <div class="kpi hero">
        <label>PolyScore</label>
        <span class="polyscore-value">{score}</span>
        <div class="poly-sub">{hero_subtext}</div>
        <span class="verify">{verify_badge}</span>
      </div>
      <div class="kpi"><label>Load Stage</label><strong>{load_stage}</strong></div>
      <div class="kpi"><label>Invariant Mode</label><strong>{invariant_mode}</strong></div>
      <div class="kpi"><label>Model</label><strong>{sheet.provider}/{sheet.model}</strong></div>
      <div class="kpi"><label>First Unassisted Drop</label><strong>{f"Turn {first_unassisted_drop}" if first_unassisted_drop else "none"}</strong></div>
      <div class="kpi"><label>Terminal Drift Turn</label><strong>{f"Turn {drift_turn}" if drift_turn else "none"}</strong></div>
      <div class="kpi"><label>Total Repair Retries</label><strong>{total_retries} ({clean_recoveries}/{turns_with_failures} clean)</strong></div>
      <div class="kpi"><label>Cascading Regressions</label><strong>{total_cascading}</strong></div>
      <div class="kpi"><label>Retry &amp; Regression Tax</label><strong>-{retry_and_regression_tax} pts ({total_retries} retries, {total_cascading} cascading breaks)</strong></div>
      <div class="kpi"><label>BSFC Token Bloat (in/out)</label><strong>{avg_bsfc:.1f}x avg · peak {peak_bsfc:.1f}x · yield {yield_ratio:.1%}</strong></div>
      <div class="kpi"><label>Context Runaway</label><strong>{f"{runaway:.1f}x vs pre-drop" if runaway is not None else "n/a"} · {post_fail_tokens:,} post-failure tok</strong></div>
      <div class="kpi"><label>Recovery Token Overhead</label><strong>+{total_recovery_tokens:,} tok (+{overhead_pct:.1f}% vs pre-drop → {base_thermal}→{grade} x{thermal_mult:.2f})</strong></div>
      <div class="kpi"><label>Thermal Bloat Turns</label><strong>{bloat_turns}</strong></div>
      <div class="kpi"><label>Thermal Efficiency</label><strong>{avg_eff:.4f}</strong></div>
      <div class="kpi"><label>Net Diff (Cumulative)</label><strong>+{out_eff['total_lines_added']:,} / -{out_eff['total_lines_removed']:,} ({out_eff['total_net_diff_lines']:,} lines · {out_eff['avg_net_diff_lines']:.1f}/turn)</strong></div>
      <div class="kpi"><label>Collateral Churn (Excess Edit Dist)</label><strong>{out_eff['total_collateral_churn_chars']:,} chars ({out_eff['avg_collateral_churn_chars']:,.1f}/turn · Peak {peak_churn_label})</strong></div>
      <div class="kpi"><label>Total Tokens (in+out)</label><strong>{total_input + total_output:,}</strong></div>
      <div class="kpi"><label>Total Cost</label><strong>${final_cost:.4f}</strong></div>
      <div class="kpi"><label>Invariant Pass Rate</label><strong>{pass_rate}</strong></div>
    </div>
    <div class="panel">
      <h2 style="margin-top:0;margin-bottom:4px;font-size:1rem;">⚡ Power Band vs. Parasitic Output Churn (Dyno Crossover)</h2>
      <p style="margin:0 0 12px;font-size:0.82rem;color:var(--muted);">Left Y: Structural Torque (0–1000) &amp; Retention (%) · Right Y: Collateral Churn (excess edit chars) &amp; Net Diff (lines touched)</p>
      <canvas id="dynoPowerPlot"></canvas>
    </div>
    <div class="panel">
      <h2 style="margin-top:0;margin-bottom:4px;font-size:1rem;">🔥 Fuel Burn &amp; Context Telemetry (Tokens &amp; Cost)</h2>
      <p style="margin:0 0 12px;font-size:0.82rem;color:var(--muted);">Left Y: Input Tokens &amp; Self-Repair Recovery Tokens · Right Y: Cumulative API Cost (USD)</p>
      <canvas id="dynoTokenPlot"></canvas>
    </div>
    <div class="panel">
      <h2 style="margin-top:0;font-size:1rem;">Black Box Autopsy</h2>
      <div class="autopsy-noise">{noise_html or "No adversarial context captured."}</div>
      <div class="diff-wrap">{diff_html}</div>
    </div>
    <div class="cta">
      🏎️ Stop Context Detonation. Eliminate multi-turn drift and cut token overhead with the Polycracker MCP Server →
      <a href="https://polycracker.dev" target="_blank" rel="noopener">polycracker.dev</a>
    </div>
  </div>
  <script>
    const labels = {labels!r};
    const inputTokens = {input_tokens!r};
    const recoveryTokens = {recovery_tokens_series!r};
    const repairAttempts = {repair_attempts_series!r};
    const cascadingBreaks = {cascading_series!r};
    const costs = {costs!r};
    const retentionPct = {retention_pct!r};
    const torqueSeries = {torque_series!r};
    const collateralChurn = {collateral_churn_series!r};
    const netDiffLines = {net_diff_series!r};
    const linesAdded = {lines_added_series!r};
    const linesRemoved = {lines_removed_series!r};
    const driftTurn = {drift_turn_js};
    const tokenPointColors = labels.map((_, idx) => {{
      if (cascadingBreaks[idx] > 0) return '#ff4d4f';
      if (repairAttempts[idx] > 0) return '#f0883e';
      return '#58a6ff';
    }});
    const powerPointColors = labels.map((_, idx) => {{
      if (driftTurn !== null && String(labels[idx]) === String(driftTurn)) return '#ff4d4f';
      if (cascadingBreaks[idx] > 0) return '#ff4d4f';
      if (repairAttempts[idx] > 0) return '#f0883e';
      return '#3fb950';
    }});
    const pointRadii = labels.map((_, idx) => (repairAttempts[idx] > 0 || cascadingBreaks[idx] > 0 ? 7 : 3));
    const driftCliffPlugin = {{
      id: 'driftCliff',
      afterDraw(chart) {{
        const {{ top, bottom }} = chart.chartArea;
        const ctx2 = chart.ctx;
        ctx2.save();
        labels.forEach((lbl, idx) => {{
          if (repairAttempts[idx] > 0) {{
            const rx = chart.scales.x.getPixelForValue(lbl);
            ctx2.strokeStyle = cascadingBreaks[idx] > 0 ? 'rgba(255,77,79,0.55)' : 'rgba(240,136,62,0.55)';
            ctx2.lineWidth = 2;
            ctx2.setLineDash([4, 4]);
            ctx2.beginPath();
            ctx2.moveTo(rx, top);
            ctx2.lineTo(rx, bottom);
            ctx2.stroke();
            ctx2.setLineDash([]);
            ctx2.fillStyle = cascadingBreaks[idx] > 0 ? '#ff7b72' : '#f0883e';
            ctx2.font = '11px sans-serif';
            const tag = cascadingBreaks[idx] > 0
              ? `T${{lbl}}: ${{repairAttempts[idx]}}R / ${{cascadingBreaks[idx]}}C`
              : `T${{lbl}}: ${{repairAttempts[idx]}}R clean`;
            ctx2.fillText(tag, rx + 4, top + 28);
          }}
        }});
        if (driftTurn !== null) {{
          const idx = labels.indexOf(String(driftTurn));
          if (idx >= 0) {{
            const x = chart.scales.x.getPixelForValue(labels[idx]);
            ctx2.strokeStyle = '#ff4d4f';
            ctx2.lineWidth = 3;
            ctx2.beginPath();
            ctx2.moveTo(x, top);
            ctx2.lineTo(x, bottom);
            ctx2.stroke();
            ctx2.fillStyle = '#ff4d4f';
            ctx2.font = '12px sans-serif';
            ctx2.fillText('Drift Cliff T' + driftTurn, x + 6, top + 14);
          }}
        }}
        ctx2.restore();
      }},
    }};

    const powerCtx = document.getElementById('dynoPowerPlot');
    new Chart(powerCtx, {{
      plugins: [driftCliffPlugin],
      type: 'line',
      data: {{
        labels,
        datasets: [
          {{
            label: 'Structural Torque (Power Band)',
            data: torqueSeries,
            borderColor: '#3fb950',
            backgroundColor: 'rgba(63,185,80,0.14)',
            fill: true,
            pointBackgroundColor: powerPointColors,
            pointBorderColor: powerPointColors,
            pointRadius: pointRadii,
            borderWidth: 2.5,
            yAxisID: 'yPower',
            tension: 0.2,
            order: 1,
          }},
          {{
            label: 'Retention %',
            data: retentionPct,
            borderColor: '#58a6ff',
            backgroundColor: 'rgba(88,166,255,0.08)',
            borderDash: [5, 4],
            pointRadius: 2,
            borderWidth: 1.8,
            yAxisID: 'yPower',
            spanGaps: true,
            tension: 0.2,
            order: 2,
          }},
          {{
            type: 'bar',
            label: 'Collateral Churn (Excess Edit Chars)',
            data: collateralChurn,
            borderColor: '#ff4d4f',
            backgroundColor: 'rgba(255,77,79,0.55)',
            borderWidth: 1,
            yAxisID: 'yChurn',
            order: 3,
          }},
          {{
            label: 'Net Diff (Lines Touched)',
            data: netDiffLines,
            borderColor: '#f0883e',
            backgroundColor: 'rgba(240,136,62,0.20)',
            pointBackgroundColor: '#f0883e',
            pointRadius: 3,
            borderWidth: 2,
            yAxisID: 'yChurn',
            tension: 0.2,
            order: 2,
          }},
        ],
      }},
      options: {{
        responsive: true,
        interaction: {{ mode: 'index', intersect: false }},
        plugins: {{
          legend: {{ labels: {{ color: '#e6edf3' }} }},
          tooltip: {{
            callbacks: {{
              afterBody(items) {{
                if (!items || !items.length) return '';
                const idx = items[0].dataIndex;
                return `Line Diff Breakdown: +${{linesAdded[idx]}} / -${{linesRemoved[idx]}} lines`;
              }},
            }},
          }},
        }},
        scales: {{
          x: {{
            ticks: {{ color: '#8b949e' }},
            grid: {{ color: '#30363d' }},
            title: {{ display: true, text: 'Turn (RPM Sweep)', color: '#8b949e' }},
          }},
          yPower: {{
            type: 'linear',
            position: 'left',
            min: 0,
            suggestedMax: 400,
            ticks: {{ color: '#3fb950' }},
            grid: {{ color: '#30363d' }},
            title: {{ display: true, text: 'Structural Torque (0–1000) / Retention (%)', color: '#3fb950' }},
          }},
          yChurn: {{
            type: 'linear',
            position: 'right',
            min: 0,
            ticks: {{ color: '#ff7b72' }},
            grid: {{ drawOnChartArea: false }},
            title: {{ display: true, text: 'Parasitic Loss: Collateral Churn (chars) & Net Diff (lines)', color: '#ff7b72' }},
          }},
        }},
      }},
    }});

    const tokenCtx = document.getElementById('dynoTokenPlot');
    new Chart(tokenCtx, {{
      plugins: [driftCliffPlugin],
      type: 'line',
      data: {{
        labels,
        datasets: [
          {{
            label: 'Input Tokens',
            data: inputTokens,
            borderColor: '#58a6ff',
            backgroundColor: 'rgba(88,166,255,0.15)',
            pointBackgroundColor: tokenPointColors,
            pointBorderColor: tokenPointColors,
            pointRadius: pointRadii,
            yAxisID: 'yTokens',
            tension: 0.2,
          }},
          {{
            type: 'bar',
            label: 'Recovery Tokens (Self-Repair)',
            data: recoveryTokens,
            borderColor: '#f0883e',
            backgroundColor: 'rgba(240,136,62,0.45)',
            yAxisID: 'yTokens',
          }},
          {{
            label: 'Cumulative Cost ($)',
            data: costs,
            borderColor: '#d2a8ff',
            backgroundColor: 'rgba(210,168,255,0.12)',
            yAxisID: 'yCost',
            tension: 0.2,
          }},
        ],
      }},
      options: {{
        responsive: true,
        interaction: {{ mode: 'index', intersect: false }},
        plugins: {{
          legend: {{ labels: {{ color: '#e6edf3' }} }},
        }},
        scales: {{
          x: {{
            ticks: {{ color: '#8b949e' }},
            grid: {{ color: '#30363d' }},
            title: {{ display: true, text: 'Turn (RPM Sweep)', color: '#8b949e' }},
          }},
          yTokens: {{
            type: 'linear',
            position: 'left',
            ticks: {{ color: '#58a6ff' }},
            grid: {{ color: '#30363d' }},
            title: {{ display: true, text: 'Input & Recovery Tokens', color: '#58a6ff' }},
          }},
          yCost: {{
            type: 'linear',
            position: 'right',
            min: 0,
            ticks: {{ color: '#d2a8ff' }},
            grid: {{ drawOnChartArea: false }},
            title: {{ display: true, text: 'Cumulative Cost ($)', color: '#d2a8ff' }},
          }},
        }},
      }},
    }});
  </script>
</body>
</html>"""
    out_path.write_text(html_doc, encoding="utf-8")
    return out_path
