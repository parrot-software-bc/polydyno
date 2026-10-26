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
    notes: str = ""


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
        if not row.invariant_intact:
            return "🔴 DRIFT"
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


def compute_structural_torque(sheet: DynoRunSheet) -> int:
    """PolyScore structural torque (0–1000), scaled by Eddy-Current load stage."""
    turns = sheet.turns
    if not turns:
        return 0
    load = str(sheet.metadata.get("load", sheet.metadata.get("profile", "stage2")))
    if load in ("standard", "adversarial"):
        load = "stage1" if load == "standard" else "stage2"
    multiplier = LOAD_TORQUE_MULTIPLIER.get(load, 0.7)
    intact = sum(1 for row in turns if row.invariant_intact)
    benchmark_turns = max(len(TURN_SPECS), 1)
    turn_factor = intact / benchmark_turns
    retention = _avg_retention_fraction(turns)
    score = 1000.0 * multiplier * turn_factor * retention
    return max(0, min(1000, int(round(score))))


def compute_thermal_grade(sheet: DynoRunSheet) -> str:
    """PolyScore thermal letter grade from useful output vs. context bloat."""
    turns = sheet.turns
    if not turns:
        return "F"
    total_in = sum(row.input_tokens for row in turns)
    total_out = sum(row.output_tokens for row in turns)
    yield_ratio = total_out / max(total_in, 1)
    avg_eff = sum(row.thermal_efficiency_ratio for row in turns) / len(turns)
    bloat_turns = sum(1 for row in turns if row.thermal_bloat)
    bloat_frac = bloat_turns / len(turns)

    if bloat_frac >= 0.5 or yield_ratio < 0.005:
        return "F"
    if bloat_frac >= 0.25 or yield_ratio < 0.01:
        return "D"
    if bloat_frac >= 0.12 or yield_ratio < 0.02 or avg_eff < 0.025:
        return "C"
    if bloat_frac >= 0.05 or yield_ratio < 0.035 or avg_eff < 0.045:
        return "B"
    if bloat_frac == 0 and yield_ratio >= 0.06 and avg_eff >= 0.06:
        return "A+"
    return "A"


def thermal_grade_multiplier(thermal_grade: str) -> float:
    return THERMAL_GRADE_MULTIPLIER.get(thermal_grade, THERMAL_GRADE_MULTIPLIER["F"])


def calculate_polyscore(structural_torque: int, thermal_grade: str) -> int:
    """Headline PolyScore (0–1000): structural torque adjusted by thermal grade."""
    multiplier = thermal_grade_multiplier(thermal_grade)
    raw = round(structural_torque * multiplier)
    return max(0, min(1000, raw))


def finalize_polyscore(sheet: DynoRunSheet) -> None:
    sheet.structural_torque = compute_structural_torque(sheet)
    sheet.thermal_grade = compute_thermal_grade(sheet)
    sheet.polyscore = calculate_polyscore(sheet.structural_torque, sheet.thermal_grade)


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
    grade = sheet.thermal_grade or compute_thermal_grade(sheet)
    thermal_mult = thermal_grade_multiplier(grade)
    score = sheet.polyscore
    if score is None:
        score = calculate_polyscore(torque, grade)
    w_hash = sheet.workload_verification_hash or "n/a"
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
        "drift_turn": str(drift) if drift else "none",
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
        > **Polycracker MCP Server** → [polycracker.com](https://polycracker.com)
        """
    ).strip()


def _telemetry_table_markdown(sheet: DynoRunSheet) -> str:
    header = (
        "| Turn | Input | Output | Cached In | Retention | Cost | Status |\n"
        "| ---: | ---: | ---: | ---: | :---: | ---: | :---: |"
    )
    rows: list[str] = []
    for t in sheet.turns:
        if not t.invariant_intact:
            status = "DRIFT"
        elif t.thermal_bloat:
            status = "BLOAT"
        else:
            status = "INTACT"
        rows.append(
            f"| {t.turn} | {t.input_tokens:,} | {t.output_tokens:,} | {t.cached_input_tokens:,} "
            f"| {t.retention_score} | ${t.cumulative_cost_usd:.4f} | {status} |"
        )
    return header + "\n" + "\n".join(rows)


def _kpi_block_markdown(row: dict[str, str]) -> str:
    return dedent(
        f"""
        ### PolyScore — {row["label"]}

        - **PolyScore:** **{row["polyscore"]}** / 1000
        - **Breakdown:** Torque {row["structural_torque"]}/1000 · Thermal {row["thermal_grade"]} (×{row["thermal_multiplier"]}) · Load `{row["load"]}`
        - **Workload verification:** `{row["workload_hash"]}`

        ### KPI summary — {row["label"]}

        - **Model:** `{row["model"]}`
        - **Eddy-Current load:** `{row["load"]}`
        - **Turns completed:** {row["turns"]}
        - **Invariant pass rate:** {row["pass_rate"]}
        - **First drift turn:** {row["drift_turn"]}
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
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = Path(results_dir) / f"dyno_report_{stamp}.md"
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
                f"| Turns | {row['turns']} | {row_b['turns']} |",
                f"| Drift turn | {row['drift_turn']} | {row_b['drift_turn']} |",
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
    avg_mult = sum(t.token_multiplier for t in turns) / len(turns)
    bloat_turns = sum(1 for t in turns if t.thermal_bloat)
    torque = sheet.structural_torque if sheet.structural_torque is not None else compute_structural_torque(sheet)
    grade = sheet.thermal_grade or compute_thermal_grade(sheet)
    thermal_mult = thermal_grade_multiplier(grade)
    score = sheet.polyscore if sheet.polyscore is not None else calculate_polyscore(torque, grade)
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
    summary.add_row("Avg thermal efficiency", f"{avg_eff:.4f}")
    summary.add_row("Avg token multiplier", f"{avg_mult:.1f}x")
    summary.add_row("Thermal bloat turns", str(bloat_turns))
    summary.add_row("Invariant intact rate", f"{intact_count}/{len(turns)}")
    summary.add_row("First drift turn", str(first_drift) if first_drift else "none")
    summary.add_row("Total input tokens", f"{total_input:,}")
    summary.add_row("Cached input tokens", f"{total_cached:,}")
    summary.add_row("Total output tokens", f"{total_output:,}")
    summary.add_row("Total latency (ms)", f"{total_latency:,.1f}")
    summary.add_row("Final cumulative cost (USD)", f"${final_cost:.4f}")

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

    parts: list[RenderableType] = [polyscore_hero, summary, spark_panel, drift_line]
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


def export_html_dyno_chart(
    sheet: DynoRunSheet,
    results_dir: str | Path = "results",
) -> Path:
    """Self-contained dark-mode HTML dashboard with Chart.js dyno plot."""
    turns = sheet.turns
    labels = [str(t.turn) for t in turns]
    input_tokens = [t.input_tokens for t in turns]
    costs = [t.cumulative_cost_usd for t in turns]
    retention_pct = []
    for t in turns:
        frac = parse_retention_fraction(t.retention_score)
        retention_pct.append(round(frac * 100, 2) if frac is not None else None)

    drift_turn = _first_drift_turn(sheet)
    total_input = sum(t.input_tokens for t in turns)
    total_output = sum(t.output_tokens for t in turns)
    intact = sum(1 for t in turns if t.invariant_intact)
    final_cost = turns[-1].cumulative_cost_usd if turns else 0.0
    pass_rate = f"{intact}/{len(turns)}" if turns else "0/0"
    load_stage = sheet.metadata.get("load", sheet.metadata.get("profile", "n/a"))
    avg_eff = sum(t.thermal_efficiency_ratio for t in turns) / len(turns) if turns else 0.0
    avg_mult = sum(t.token_multiplier for t in turns) / len(turns) if turns else 0.0
    bloat_turns = sum(1 for t in turns if t.thermal_bloat)
    torque = sheet.structural_torque if sheet.structural_torque is not None else compute_structural_torque(sheet)
    grade = sheet.thermal_grade or compute_thermal_grade(sheet)
    thermal_mult = thermal_grade_multiplier(grade)
    score = sheet.polyscore if sheet.polyscore is not None else calculate_polyscore(torque, grade)
    verify_badge = workload_verification_badge(sheet)
    hero_subtext = f"Torque {torque} · Thermal {grade} ({thermal_mult:.2f}x) · Load {load_stage}"

    drift_turn_js = "null" if drift_turn is None else str(drift_turn)

    diff_html = autopsy_diff_to_html(sheet.autopsy)
    noise_html = ""
    if sheet.autopsy and sheet.autopsy.adversarial_context_noise:
        from html import escape

        noise_html = escape(sheet.autopsy.adversarial_context_noise)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = Path(results_dir) / f"dyno_chart_{stamp}.html"
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
      <div class="kpi"><label>Model</label><strong>{sheet.provider}/{sheet.model}</strong></div>
      <div class="kpi"><label>First Drift Turn</label><strong>{drift_turn if drift_turn else "none"}</strong></div>
      <div class="kpi"><label>Thermal Efficiency</label><strong>{avg_eff:.4f}</strong></div>
      <div class="kpi"><label>Token Multiplier</label><strong>{avg_mult:.1f}x</strong></div>
      <div class="kpi"><label>Thermal Bloat Turns</label><strong>{bloat_turns}</strong></div>
      <div class="kpi"><label>Total Tokens (in+out)</label><strong>{total_input + total_output:,}</strong></div>
      <div class="kpi"><label>Total Cost</label><strong>${final_cost:.4f}</strong></div>
      <div class="kpi"><label>Invariant Pass Rate</label><strong>{pass_rate}</strong></div>
    </div>
    <div class="panel">
      <canvas id="dynoPlot"></canvas>
    </div>
    <div class="panel">
      <h2 style="margin-top:0;font-size:1rem;">Black Box Autopsy</h2>
      <div class="autopsy-noise">{noise_html or "No adversarial context captured."}</div>
      <div class="diff-wrap">{diff_html}</div>
    </div>
    <div class="cta">
      🏎️ Stop Context Detonation. Eliminate multi-turn drift and cut token overhead with the Polycracker MCP Server →
      <a href="https://polycracker.com" target="_blank" rel="noopener">polycracker.com</a>
    </div>
  </div>
  <script>
    const labels = {labels!r};
    const inputTokens = {input_tokens!r};
    const costs = {costs!r};
    const retentionPct = {retention_pct!r};
    const driftTurn = {drift_turn_js};
    const driftCliffPlugin = {{
      id: 'driftCliff',
      afterDraw(chart) {{
        if (driftTurn === null) return;
        const idx = labels.indexOf(String(driftTurn));
        if (idx < 0) return;
        const x = chart.scales.x.getPixelForValue(labels[idx]);
        const {{ top, bottom }} = chart.chartArea;
        const ctx2 = chart.ctx;
        ctx2.save();
        ctx2.strokeStyle = '#ff4d4f';
        ctx2.lineWidth = 3;
        ctx2.beginPath();
        ctx2.moveTo(x, top);
        ctx2.lineTo(x, bottom);
        ctx2.stroke();
        ctx2.fillStyle = '#ff4d4f';
        ctx2.font = '12px sans-serif';
        ctx2.fillText('Drift Cliff T' + driftTurn, x + 6, top + 14);
        ctx2.restore();
      }},
    }};
    const ctx = document.getElementById('dynoPlot');
    new Chart(ctx, {{
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
            yAxisID: 'y',
            tension: 0.2,
          }},
          {{
            label: 'Cumulative Cost ($)',
            data: costs,
            borderColor: '#d2a8ff',
            backgroundColor: 'rgba(210,168,255,0.12)',
            yAxisID: 'y',
            tension: 0.2,
          }},
          {{
            label: 'Retention %',
            data: retentionPct,
            borderColor: '#3fb950',
            backgroundColor: 'rgba(63,185,80,0.12)',
            yAxisID: 'y1',
            spanGaps: true,
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
          x: {{ ticks: {{ color: '#8b949e' }}, grid: {{ color: '#30363d' }} }},
          y: {{
            type: 'linear',
            position: 'left',
            ticks: {{ color: '#8b949e' }},
            grid: {{ color: '#30363d' }},
            title: {{ display: true, text: 'Input Tokens / Cost ($)', color: '#8b949e' }},
          }},
          y1: {{
            type: 'linear',
            position: 'right',
            min: 0,
            max: 100,
            ticks: {{ color: '#3fb950' }},
            grid: {{ drawOnChartArea: false }},
            title: {{ display: true, text: 'Retention %', color: '#3fb950' }},
          }},
        }},
      }},
    }});
  </script>
</body>
</html>"""
    out_path.write_text(html_doc, encoding="utf-8")
    return out_path
