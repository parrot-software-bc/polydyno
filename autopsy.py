"""Black Box Autopsy — SQL diff diagnostics on invariant drift."""

from __future__ import annotations

import difflib
import html
import re

from pydantic import BaseModel, Field
from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text


class BlackBoxAutopsy(BaseModel):
    drift_turn: int
    previous_turn: int
    adversarial_context_noise: str = ""
    unified_diff: str = ""
    schema_before: str = ""
    schema_after: str = ""


def compute_unified_sql_diff(
    schema_before: str,
    schema_after: str,
    *,
    previous_turn: int,
    drift_turn: int,
) -> str:
    before_lines = schema_before.splitlines(keepends=True)
    after_lines = schema_after.splitlines(keepends=True)
    if not before_lines and schema_before:
        before_lines = [schema_before if schema_before.endswith("\n") else schema_before + "\n"]
    if not after_lines and schema_after:
        after_lines = [schema_after if schema_after.endswith("\n") else schema_after + "\n"]
    diff_iter = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"turn_{previous_turn:02d}_valid.sql",
        tofile=f"turn_{drift_turn:02d}_drift.sql",
        lineterm="",
    )
    return "\n".join(diff_iter)


def build_black_box_autopsy(
    *,
    drift_turn: int,
    previous_turn: int,
    schema_before: str,
    schema_after: str,
    adversarial_context_noise: str,
) -> BlackBoxAutopsy:
    unified = compute_unified_sql_diff(
        schema_before,
        schema_after,
        previous_turn=previous_turn,
        drift_turn=drift_turn,
    )
    return BlackBoxAutopsy(
        drift_turn=drift_turn,
        previous_turn=previous_turn,
        adversarial_context_noise=adversarial_context_noise,
        unified_diff=unified,
        schema_before=schema_before,
        schema_after=schema_after,
    )


def render_autopsy_panel(autopsy: BlackBoxAutopsy) -> Panel:
    diff_body = autopsy.unified_diff or "(no diff — schemas identical or empty)"
    diff_syntax = Syntax(diff_body, "diff", theme="monokai", line_numbers=True)
    noise = Text(autopsy.adversarial_context_noise or "(no context noise recorded)", style="dim")
    title = (
        f"[bold red]Black Box Autopsy — turn {autopsy.previous_turn:02d} → "
        f"{autopsy.drift_turn:02d}[/bold red]"
    )
    from rich.console import Group

    return Panel(
        Group(
            Text("Adversarial / context noise at drift turn:", style="bold yellow"),
            noise,
            Text(""),
            Text("Unified SQL diff (git-style):", style="bold"),
            diff_syntax,
        ),
        title=title,
        border_style="red",
        padding=(1, 2),
    )


def render_autopsy_console(autopsy: BlackBoxAutopsy, console: Console | None = None) -> None:
    console = console or Console()
    console.print()
    console.print(render_autopsy_panel(autopsy))


def _diff_line_html(line: str) -> str:
    escaped = html.escape(line)
    if line.startswith("+++") or line.startswith("---"):
        return f'<div class="diff-meta">{escaped}</div>'
    if line.startswith("@@"):
        return f'<div class="diff-hunk">{escaped}</div>'
    if line.startswith("+"):
        return f'<div class="diff-add">{escaped}</div>'
    if line.startswith("-"):
        return f'<div class="diff-del">{escaped}</div>'
    return f'<div class="diff-ctx">{escaped}</div>'


def autopsy_diff_to_html(autopsy: BlackBoxAutopsy | None) -> str:
    if autopsy is None or not autopsy.unified_diff:
        return '<pre class="diff-empty">No drift autopsy recorded.</pre>'
    lines = autopsy.unified_diff.splitlines()
    return "".join(_diff_line_html(line) for line in lines)


def parse_retention_fraction(score: str) -> float | None:
    match = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", score)
    if not match:
        return None
    kept, total = int(match.group(1)), int(match.group(2))
    if total <= 0:
        return None
    return kept / total
