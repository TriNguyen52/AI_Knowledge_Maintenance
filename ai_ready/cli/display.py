"""Rich-formatted display for the closed-loop improvement demo.

All formatting logic lives here so that demo scripts (demo_closed_loop.py)
remain call-only.  Uses the ``rich`` library for colors, tables, panels,
and visual indicators that help developers understand:

  1. Root causes  -- what problems were found and why
  2. Proposals    -- which strategy was chosen and the trade-offs
  3. Verification -- did the fix work, what changed
  4. Institutional memory -- how the system learns across runs
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.rule import Rule
from rich.align import Align

console = Console(width=100)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_len: int = 120) -> str:
    if not text:
        return ""
    return text if len(text) <= max_len else text[:max_len] + "..."


def _confidence_color(conf: float | str) -> str:
    """Map a confidence value to a rich color string."""
    if isinstance(conf, str):
        try:
            conf = float(conf)
        except (ValueError, TypeError):
            return "white"
    if conf >= 0.85:
        return "bold red"
    if conf >= 0.7:
        return "bold yellow"
    if conf >= 0.5:
        return "yellow"
    return "dim white"


def _outcome_color(outcome: str) -> str:
    """Map a verification outcome to a rich color string."""
    o = (outcome or "").lower()
    if o == "resolved":
        return "bold green"
    if o == "partially_resolved":
        return "bold yellow"
    if o == "unchanged":
        return "dim white"
    if o in ("regressed", "misdiagnosed"):
        return "bold red"
    if o == "rolled_back":
        return "bold magenta"
    return "white"


def _severity_color(severity: str) -> str:
    """Map a severity level to a rich color string."""
    s = (severity or "").upper()
    if s == "CRITICAL":
        return "bold red"
    if s == "HIGH":
        return "red"
    if s == "MEDIUM":
        return "yellow"
    if s == "LOW":
        return "dim cyan"
    if s == "INFO":
        return "dim white"
    return "white"


def _score_color(score: int | float) -> str:
    """Map a score (0-100) to a color."""
    if score >= 80:
        return "bold green"
    if score >= 60:
        return "green"
    if score >= 40:
        return "yellow"
    return "bold red"


def _delta_color(delta: int | float) -> str:
    """Map a score delta to a color."""
    if delta > 0:
        return "bold green"
    if delta < 0:
        return "bold red"
    return "dim white"


def _step_type_icon(step_type: str) -> str:
    """Map a step type to a visual icon."""
    icons = {
        "add_metadata": "[blue]METADATA[/blue]",
        "update_document": "[cyan]UPDATE[/cyan]",
        "create_relationship": "[magenta]LINK[/magenta]",
        "remove_relationship": "[dim magenta]UNLINK[/dim magenta]",
        "archive_artifact": "[dim red]ARCHIVE[/dim red]",
        "merge_artifacts": "[yellow]MERGE[/yellow]",
        "split_artifact": "[yellow]SPLIT[/yellow]",
    }
    return icons.get(step_type, step_type)


# ---------------------------------------------------------------------------
# Top-level layout
# ---------------------------------------------------------------------------

def print_banner(title: str, subtitle: str = "", char: str = "=", width: int = 100) -> None:
    """Print a full-width banner with optional subtitle."""
    console.print(Rule(char * 1, style="bold cyan"))
    console.print(Align.center(f"[bold cyan]{title}[/bold cyan]"))
    if subtitle:
        console.print(Align.center(f"[dim]{subtitle}[/dim]"))
    console.print(Rule(char * 1, style="bold cyan"))


def print_section(title: str) -> None:
    """Print a section header."""
    console.print()
    console.print(f"  [bold blue]--- {title} ---[/bold blue]")
    console.print()


def print_info(label: str, value: str, color: str = "white") -> None:
    """Print a key-value pair with colored value."""
    console.print(f"  [dim]{label}:[/dim] [{color}]{value}[/{color}]")


def print_kv(label: str, value: Any) -> None:
    """Print a key-value pair."""
    console.print(f"  [dim]{label}:[/dim] {value}")


def print_blank() -> None:
    console.print()


# ---------------------------------------------------------------------------
# Intro / overview
# ---------------------------------------------------------------------------

def print_intro(source: str, limit: int, runs: int) -> None:
    """Print the demo intro panel."""
    run_lines = []
    for i in range(1, runs + 1):
        if i == 1:
            run_lines.append(f"  [green]Run {i}[/green]: No history -> outcome saved to CockroachDB")
        else:
            run_lines.append(f"  [green]Run {i}[/green]: Reads CockroachDB history -> LLM sees {i-1} prior outcome(s)")
    body = (
        f"[dim]This demo runs the improvement workflow {runs} time(s):[/dim]\n"
        + "\n".join(run_lines) + "\n"
        f"[dim]After all runs: per-run comparison + CockroachDB verification.[/dim]\n\n"
        f"[dim]Source:[/dim] {source}\n"
        f"[dim]Limit: [/dim] {limit} artifacts"
    )
    panel = Panel(
        body,
        title="[bold cyan]Closed-Loop Institutional Memory Demo[/bold cyan]",
        subtitle="[dim]CockroachDB (distributed, ACID, vector-indexed)[/dim]",
        border_style="cyan",
        width=100,
    )
    console.print(panel)


# ---------------------------------------------------------------------------
# CockroachDB connection
# ---------------------------------------------------------------------------

def print_db_connecting(url_prefix: str) -> None:
    print_section("Connect to CockroachDB")
    console.print(f"  [dim]Connection:[/dim] {url_prefix}...")
    console.print(f"  [dim]Connecting to CockroachDB Cloud...[/dim]")


def print_db_connected() -> None:
    console.print(f"  [green]Connected successfully.[/green]")

def print_db_schema_ready() -> None:
    console.print(f"  [green]Schema ready.[/green]")

def print_db_cleared() -> None:
    console.print(f"  [dim]Data cleared.[/dim]")


# ---------------------------------------------------------------------------
# Load & assess
# ---------------------------------------------------------------------------

def print_load_start(source: str, limit: int) -> None:
    print_section("Load Knowledge and Run Assessment")
    console.print(f"  [dim]Source:[/dim] {source}")
    console.print(f"  [dim]Artifact limit:[/dim] {limit}")
    console.print()
    console.print(f"  [dim]Loading knowledge (with language exclusion filter)...[/dim]")


def print_load_done(artifact_count: int, rel_count: int, elapsed: float, limited: int | None = None) -> None:
    console.print(f"  [green]Loaded {artifact_count} artifacts in {elapsed:.1f}s[/green]")
    console.print(f"  [dim]Relationships (pruned): {rel_count}[/dim]")
    if limited:
        console.print(f"  [yellow]Limited to first {limited} artifacts[/yellow]")


def print_assessment_start() -> None:
    console.print()
    console.print(f"  [dim]Running assessment pipeline...[/dim]")


def print_assessment_done(score: int, signal_count: int, elapsed: float) -> None:
    console.print(f"  [dim]Assessment complete in {elapsed:.1f}s[/dim]")
    score_text = Text()
    score_text.append(f"  Score: ", style="dim")
    score_text.append(f"{score}", style=_score_color(score))
    score_text.append(f"    Signals: ", style="dim")
    score_text.append(f"{signal_count}", style="bold white")
    console.print(score_text)


# ---------------------------------------------------------------------------
# Persist to CockroachDB
# ---------------------------------------------------------------------------

def print_persist_header(artifact_count: int, signal_count: int) -> None:
    print_section("Persist to CockroachDB (via persist_assessment)")
    console.print(f"  [dim]Persisting {artifact_count} artifacts + {signal_count} signals[/dim]")


def print_persist_done(artifact_count: int, signal_count: int, assessment_id: str, embedding_status: str) -> None:
    console.print(f"  [green]Artifacts in DB:[/green] {artifact_count} [dim](embeddings: {embedding_status})[/dim]")
    console.print(f"  [green]Signals in DB:[/green] {signal_count} [dim](with lifecycle tracking)[/dim]")
    console.print(f"  [green]Assessment in DB:[/green] {assessment_id}")


def print_vector_search(query: str, results: list[dict]) -> None:
    print_section("Vector Search (CockroachDB <=> cosine distance)")
    console.print(f"  [dim]Query: artifacts similar to '{query}'[/dim]")
    for i, r in enumerate(results[:5]):
        sim = r.get("similarity", 0)
        uri = r.get("artifact_uri", "?")
        console.print(f"    [{i+1}] [bold]sim={sim:.3f}[/bold]  [dim]{_truncate(uri, 60)}[/dim]")


def print_llm_info(provider: str, model: str) -> None:
    console.print(f"\n  [bold]LLM:[/bold] {provider} ({model})")

def print_llm_unavailable(reason: str) -> None:
    console.print(f"\n  [yellow]LLM: unavailable ({reason}), using heuristics[/yellow]")

def print_llm_no_key() -> None:
    console.print(f"\n  [yellow]LLM: no GROQ_API_KEY, using heuristics[/yellow]")

def print_backup_done(count: int, dest: str) -> None:
    console.print(f"\n  [green]Backed up {count} files to {dest}[/green]")

def print_backup_skipped() -> None:
    console.print(f"\n  [dim]Backup skipped (--no-backup)[/dim]")

def print_restore_done(count: int) -> None:
    console.print(f"\n  [green]Restored {count} files from backup[/green]")


# ---------------------------------------------------------------------------
# Run improvement workflow
# ---------------------------------------------------------------------------

def print_run_header(run_label: str) -> None:
    print_banner(f"RUN {run_label}: Improvement Workflow")


def print_manager_created() -> None:
    console.print(f"  [dim]Creating ImprovementManager (CockroachDB-only)...[/dim]")
    console.print(f"  [green]CockroachDB: connected[/green]")


def print_history_check(outcomes: list[Any]) -> None:
    print_section("Checking CockroachDB Institutional Memory (before this run)")
    if not outcomes:
        console.print(f"  [dim]Existing outcomes in CockroachDB: 0[/dim]")
        return
    console.print(f"  [bold]Existing outcomes in CockroachDB: {len(outcomes)}[/bold]")
    table = Table(show_header=True, header_style="bold cyan", border_style="dim", width=96)
    table.add_column("Outcome ID", style="dim", width=12)
    table.add_column("Issue Type", style="white", width=20)
    table.add_column("Strategy", style="white", width=25)
    table.add_column("Outcome", width=15)
    for h in outcomes[:5]:
        outcome_str = h.verification_outcome
        color = _outcome_color(outcome_str)
        table.add_row(
            h.outcome_id[:8],
            h.issue_type,
            _truncate(h.strategy, 25),
            f"[{color}]{outcome_str}[/{color}]",
        )
    console.print(table)


def print_workflow_starting() -> None:
    print_section("Starting workflow (analyze -> propose -> approve checkpoint)")


def print_workflow_started(app_id: str, elapsed: float) -> None:
    console.print(f"  [green]Workflow started:[/green] app_id={app_id}")
    console.print(f"  [dim]Elapsed: {elapsed:.1f}s[/dim]")


def print_agent_state(status: str, stage: str) -> None:
    console.print(f"  [dim]Agent state in DB:[/dim] status={status}, stage={stage}")


def print_agent_state_not_found() -> None:
    console.print(f"  [red]Agent state NOT FOUND in CockroachDB[/red]")


# ---------------------------------------------------------------------------
# Root causes
# ---------------------------------------------------------------------------

def print_root_causes(root_causes: list[dict]) -> None:
    """Display root causes in a colored table with confidence bars."""
    print_section(f"Root Causes ({len(root_causes)})")

    if not root_causes:
        console.print(f"  [dim]No root causes identified.[/dim]")
        return

    table = Table(show_header=True, header_style="bold red", border_style="red", width=96)
    table.add_column("#", style="dim", width=3)
    table.add_column("Category", style="bold yellow", width=22)
    table.add_column("Hypothesis", width=48)
    table.add_column("Confidence", justify="center", width=18)

    for i, rc in enumerate(root_causes[:5]):
        category = rc.get("category", "?")
        hypothesis = _truncate(rc.get("hypothesis", ""), 100)
        conf = rc.get("confidence", 0)
        conf_val = float(conf) if isinstance(conf, (int, float, str)) else 0

        # Visual confidence bar
        bar_len = int(conf_val * 10)
        bar = "#" * bar_len + "-" * (10 - bar_len)
        color = _confidence_color(conf_val)
        conf_str = f"[{color}]{bar} {conf_val:.2f}[/{color}]"

        table.add_row(str(i + 1), category, hypothesis, conf_str)

    console.print(table)


# ---------------------------------------------------------------------------
# Knowledge problems
# ---------------------------------------------------------------------------

def print_knowledge_problems(problems: list[dict]) -> None:
    """Display knowledge problems as styled panels."""
    print_section(f"Knowledge Problems ({len(problems)})")

    if not problems:
        console.print(f"  [dim]No knowledge problems identified.[/dim]")
        return

    for i, p in enumerate(problems[:5]):
        category = p.get("category", "?")
        description = _truncate(p.get("description", ""), 100)
        signal_ids = p.get("signal_ids", [])
        signal_count = len(signal_ids) if signal_ids else 0

        panel = Panel(
            f"[bold yellow]{category}[/bold yellow]\n"
            f"[white]{description}[/white]\n"
            f"[dim]Signals affected: {signal_count}[/dim]",
            border_style="yellow",
            width=96,
            padding=(0, 1),
        )
        console.print(panel)


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------

def print_proposals(proposals: list[dict], selected_idx: int | None = None) -> None:
    """Display proposals in comparison panels.

    If selected_idx is provided, highlights the selected proposal.
    """
    print_section(f"Proposals ({len(proposals)})")

    if not proposals:
        console.print(f"  [dim]No proposals generated.[/dim]")
        return

    for i, p in enumerate(proposals):
        strategy = p.get("strategy", "unknown")
        description = _truncate(p.get("description", ""), 100)
        reasoning = _truncate(p.get("reasoning", ""), 100)
        steps = p.get("modification_steps", [])
        step_count = len(steps)

        # Determine step types for a summary
        step_types: dict[str, int] = {}
        for s in steps:
            st = s.get("step_type", "unknown")
            step_types[st] = step_types.get(st, 0) + 1

        type_summary = ", ".join(f"{_step_type_icon(k)} x{v}" for k, v in sorted(step_types.items()))

        # Highlight selected proposal
        is_selected = selected_idx is not None and i == selected_idx
        border = "bold green" if is_selected else "dim"
        title_prefix = "[bold green]>>> SELECTED[/bold green] " if is_selected else ""

        body_lines = [
            f"{title_prefix}[bold]{strategy}[/bold]",
            f"[dim]Description:[/dim] {description}",
            f"[dim]Reasoning:[/dim]  {reasoning}",
            f"[dim]Steps:[/dim]     {step_count}  ({type_summary})",
        ]

        # Show first 3 steps as examples
        if steps:
            body_lines.append("")
            body_lines.append("[dim]Sample steps:[/dim]")
            for s in steps[:3]:
                step_type = s.get("step_type", "")
                artifact = _truncate(s.get("artifact_uri", ""), 50)
                desc = _truncate(s.get("description", ""), 70)
                body_lines.append(f"  {_step_type_icon(step_type)} [bold]{artifact}[/bold]")
                body_lines.append(f"      [dim]{desc}[/dim]")

        panel = Panel(
            "\n".join(body_lines),
            border_style=border,
            width=96,
            padding=(0, 1),
        )
        console.print(panel)


# ---------------------------------------------------------------------------
# Historical context
# ---------------------------------------------------------------------------

def print_historical_context(context: str) -> None:
    print_section("Historical Context Used by LLM")
    if context:
        console.print(f"  [dim]{_truncate(context, 200)}[/dim]")
    else:
        console.print(f"  [dim](No historical context -- first run for this issue type)[/dim]")


# ---------------------------------------------------------------------------
# Execution & verification
# ---------------------------------------------------------------------------

def print_auto_approve() -> None:
    print_section("Auto-approving and completing workflow")
    console.print(f"  [dim]Running: execute_change -> verify_improvement...[/dim]")


def print_execution_done(elapsed: float, final_stage: str, status: str) -> None:
    console.print(f"  [green]Completed in {elapsed:.1f}s[/green]")
    console.print(f"  [dim]Final stage: {final_stage}[/dim]")
    console.print(f"  [green]Agent state updated (status={status})[/green]")


def print_verification(verification: dict) -> None:
    """Display verification results in a styled panel."""
    if not verification:
        return

    console.print()
    outcome = verification.get("overall_outcome", "unknown")
    before = verification.get("before_score", "?")
    after = verification.get("after_score", "?")
    diff = verification.get("score_difference", "?")

    before_color = _score_color(before) if isinstance(before, (int, float)) else "white"
    after_color = _score_color(after) if isinstance(after, (int, float)) else "white"
    diff_color = _delta_color(diff) if isinstance(diff, (int, float)) else "dim"
    diff_str = f"{diff:+}" if isinstance(diff, (int, float)) else str(diff)

    body_lines = [
        f"  Outcome: [{_outcome_color(outcome)}]{outcome.upper()}[/]",
        f"  Score:   [{before_color}]{before}[/] [bold green]->[/] [{after_color}]{after}[/] [{diff_color}]({diff_str})[/]",
    ]

    panel = Panel(
        "\n".join(body_lines),
        title="[bold]Verification Results[/bold]",
        border_style=_outcome_color(outcome).replace("bold ", ""),
        width=96,
        padding=(0, 1),
    )
    console.print(panel)


# ---------------------------------------------------------------------------
# Memory in action
# ---------------------------------------------------------------------------

def print_memory_in_action(memory_influence: dict) -> None:
    """Display how institutional memory influenced this run."""
    if not memory_influence:
        return

    print_section("Memory in Action (store -> retrieve -> act)")

    retrieved = memory_influence.get("retrieved", [])
    avoided = memory_influence.get("avoided_strategies", [])
    reused = memory_influence.get("reused_strategy")
    rationale = memory_influence.get("rationale", "")

    if retrieved:
        console.print(f"  [bold]Retrieved {len(retrieved)} prior outcome(s) from CockroachDB:[/bold]")
        table = Table(show_header=True, header_style="bold cyan", border_style="dim", width=96)
        table.add_column("Result", width=8)
        table.add_column("Strategy", width=30)
        table.add_column("Outcome", width=20)
        table.add_column("Score Change", justify="right", width=15)
        for r in retrieved[:5]:
            result_tag = "[green]OK[/green]" if r.get("result") == "success" else "[red]FAIL[/red]"
            outcome_str = r.get("verification_outcome", "?")
            color = _outcome_color(outcome_str)
            table.add_row(
                result_tag,
                _truncate(r.get("strategy", "?"), 30),
                f"[{color}]{outcome_str}[/{color}]",
                str(r.get("score_change", "?")),
            )
        console.print(table)
    else:
        console.print(f"  [dim]No prior outcomes retrieved (first run for this issue type).[/dim]")

    if avoided:
        console.print(f"\n  [red]Avoided {len(avoided)} failed strategy/strategies:[/red]")
        for a in avoided[:3]:
            console.print(f"    [red]x[/red] [bold]{a.get('strategy', '?')}[/bold]: {_truncate(a.get('failure_reason', ''), 70)}")

    if reused:
        console.print(f"\n  [green]Reused successful strategy: {reused}[/green]")

    console.print(f"\n  [dim]Rationale: {rationale}[/dim]")


# ---------------------------------------------------------------------------
# Knowledge health before/after
# ---------------------------------------------------------------------------

def print_knowledge_health(verification: dict) -> None:
    """Display before/after knowledge health with dimension deltas."""
    if not verification:
        return

    print_section("Knowledge Health: Before -> After")

    before_score = verification.get("before_score", "?")
    after_score = verification.get("after_score", "?")
    score_delta = verification.get("score_difference", 0)
    resolved_ids = verification.get("resolved_signal_ids", [])
    new_ids = verification.get("new_signal_ids", [])
    remaining_ids = verification.get("remaining_signal_ids", [])
    dim_deltas = verification.get("dimension_deltas", {})

    # Score comparison table
    table = Table(show_header=True, header_style="bold cyan", border_style="cyan", width=96)
    table.add_column("Metric", style="dim", width=25)
    table.add_column("Before", justify="right", width=12)
    table.add_column("After", justify="right", width=12)
    table.add_column("Delta", justify="right", width=12)

    before_str = str(before_score)
    after_str = str(after_score)
    delta_str = f"{score_delta:+d}" if isinstance(score_delta, (int, float)) else "?"
    table.add_row(
        "Score",
        f"[{_score_color(before_score) if isinstance(before_score, (int, float)) else 'white'}]{before_str}[/]",
        f"[{_score_color(after_score) if isinstance(after_score, (int, float)) else 'white'}]{after_str}[/]",
        f"[{_delta_color(score_delta) if isinstance(score_delta, (int, float)) else 'dim'}]{delta_str}[/]",
    )
    table.add_row("Signals resolved", "", f"[green]{len(resolved_ids)}[/green]", f"[green]+{len(resolved_ids)}[/green]")
    table.add_row("New signals", "", f"[yellow]{len(new_ids)}[/yellow]", f"[yellow]+{len(new_ids)}[/yellow]")
    table.add_row("Remaining", "", f"[dim]{len(remaining_ids)}[/dim]", "")

    console.print(table)

    # Dimension deltas
    if dim_deltas:
        console.print()
        console.print(f"  [bold]Dimension Changes:[/bold]")
        dim_table = Table(show_header=True, header_style="bold cyan", border_style="dim", width=96)
        dim_table.add_column("Dimension", style="white", width=25)
        dim_table.add_column("Delta", justify="right", width=10)
        dim_table.add_column("Trend", width=15)

        for dim_name, delta in sorted(dim_deltas.items()):
            if isinstance(delta, (int, float)):
                if delta > 0.001:
                    trend = "[green]improved[/green]"
                    color = _delta_color(delta)
                elif delta < -0.001:
                    trend = "[red]regressed[/red]"
                    color = _delta_color(delta)
                else:
                    trend = "[dim]unchanged[/dim]"
                    color = "dim"
                dim_table.add_row(dim_name, f"[{color}]{delta:+d}[/{color}]", trend)
        console.print(dim_table)


# ---------------------------------------------------------------------------
# CockroachDB sync confirmation
# ---------------------------------------------------------------------------

def print_cockroach_sync(history: list[Any], traces: list[dict]) -> None:
    """Display CockroachDB sync confirmation with outcome and decision traces."""
    print_section("CockroachDB Sync Confirmation")

    console.print(f"  [bold]Total outcomes in CockroachDB: {len(history)}[/bold]")

    if history:
        latest = history[0]
        console.print(f"  [dim]Latest outcome:[/dim]")
        console.print(f"    [dim]outcome_id:[/dim] {latest.outcome_id}")
        console.print(f"    [dim]issue_type:[/dim] {latest.issue_type}")
        console.print(f"    [dim]strategy:[/dim] {latest.strategy}")
        outcome_str = latest.verification_outcome
        color = _outcome_color(outcome_str)
        console.print(f"    [dim]verification_outcome:[/dim] [{color}]{outcome_str}[/{color}]")
        console.print(f"    [dim]tokens_used:[/dim] {latest.tokens_used}  [dim]forked:[/dim] {latest.forked}")

        # Decision traces as a timeline
        console.print(f"\n  [bold]Decision traces saved: {len(traces)}[/bold]")
        trace_table = Table(show_header=True, header_style="bold cyan", border_style="dim", width=96)
        trace_table.add_column("Stage", style="bold magenta", width=15)
        trace_table.add_column("Reasoning", width=78)
        for t in traces[:6]:
            stage = t.get("stage", "?")
            reasoning = _truncate(t.get("reasoning", ""), 100)
            trace_table.add_row(stage, reasoning)
        console.print(trace_table)


# ---------------------------------------------------------------------------
# Signal lifecycle
# ---------------------------------------------------------------------------

def print_signal_lifecycle(new_count: int, persistent_count: int, resolved_count: int,
                           resolved_sample: list[Any] | None = None) -> None:
    """Display signal lifecycle state as a visual flow."""
    print_section("Signal Lifecycle Update (via verify_improvement)")

    # Visual lifecycle flow
    flow = (
        f"  [dim]NEW[/dim] ({new_count})  "
        f"[dim]->[/dim]  "
        f"[yellow]PERSISTENT[/yellow] ({persistent_count})  "
        f"[dim]->[/dim]  "
        f"[green]RESOLVED[/green] ({resolved_count})"
    )
    console.print(flow)

    if resolved_count > 0:
        console.print(f"\n  [green]Confirmed: {resolved_count} signal(s) now in resolved status[/green]")
    elif persistent_count > 0:
        console.print(f"\n  [yellow]Confirmed: {persistent_count} signal(s) in persistent status[/yellow]")

    if resolved_sample:
        console.print(f"\n  [bold]Resolved signals (sample):[/bold]")
        sample_table = Table(show_header=True, header_style="bold green", border_style="dim", width=96)
        sample_table.add_column("Severity", width=10)
        sample_table.add_column("Signal Type", style="white", width=25)
        sample_table.add_column("Artifact", style="dim", width=55)
        for s in resolved_sample[:5]:
            sev = s.severity if hasattr(s, "severity") else s.get("severity", "?")
            stype = s.signal_type if hasattr(s, "signal_type") else s.get("signal_type", "?")
            uri = s.artifact_uri if hasattr(s, "artifact_uri") else s.get("artifact_uri", "?")
            sev_color = _severity_color(sev)
            sample_table.add_row(
                f"[{sev_color}]{sev}[/{sev_color}]",
                stype,
                _truncate(uri, 55),
            )
        console.print(sample_table)


# ---------------------------------------------------------------------------
# CockroachDB institutional memory (MCP section)
# ---------------------------------------------------------------------------

def print_mcp_header(tool_count: int, tools: list[dict]) -> None:
    print_banner("CockroachDB Institutional Memory (After All Runs)")
    print_section(f"MCP Server Interface ({tool_count} tools)")
    console.print(f"  [dim]External AI agents query CockroachDB via MCP tools:[/dim]")
    for t in tools:
        console.print(f"    [cyan]*[/cyan] [bold]{t['name']}[/bold]: {_truncate(t['description'], 60)}")
    console.print()
    console.print(f"  [dim]Calling MCP tools against live CockroachDB data...[/dim]")


def print_mcp_health(score: float, signals_count: int) -> None:
    print_section("MCP: get_knowledge_health")
    console.print(f"  Score: [{_score_color(score)}]{score}[/]")
    console.print(f"  Signals: {signals_count}")


def print_mcp_signals(signals: list[dict], count: int) -> None:
    print_section("MCP: get_signals (limit=5)")
    console.print(f"  Signals: {count}")
    for s in signals[:3]:
        sev = s.get("severity", "?")
        stype = s.get("signal_type", "?")
        uri = _truncate(s.get("artifact_uri", ""), 50)
        sev_color = _severity_color(sev)
        console.print(f"    [{sev_color}]{sev}[/{sev_color}] {stype} -> {uri}")


def print_mcp_vector_search(query: str, results: list[dict]) -> None:
    print_section("MCP: search_artifacts_semantic (vector index)")
    console.print(f"  [dim]Query: '{query}'[/dim]")
    console.print(f"  Results: {len(results)}")
    for r in results[:3]:
        sim = r.get("similarity", 0)
        uri = _truncate(r.get("artifact_uri", ""), 60)
        console.print(f"    [bold]sim={sim:.3f}[/bold]  [dim]{uri}[/dim]")


def print_mcp_workflows(workflows: list[dict], all_workflows: list[dict] | None = None) -> None:
    print_section("MCP: get_active_workflows (agent state)")
    console.print(f"  Active/paused workflows: {len(workflows)}")
    for w in workflows[:5]:
        app_id = str(w.get("app_id", ""))[:8]
        stage = w.get("current_stage", "")
        status = w.get("status", "")
        console.print(f"    [dim][{app_id}][/dim] stage={stage} status={status}")

    if all_workflows is not None:
        print_section("MCP: All workflows (full lifecycle)")
        console.print(f"  Total workflows in CockroachDB: {len(all_workflows)}")
        for w in all_workflows[:5]:
            app_id = str(w.get("app_id", ""))[:8]
            stage = w.get("current_stage", "")
            status = w.get("status", "")
            console.print(f"    [dim][{app_id}][/dim] stage={stage} status={status}")


def print_mcp_remediation_history(history: list[dict], count: int) -> None:
    print_section("MCP: get_remediation_history")
    console.print(f"  Total outcomes: {count}")
    for h in history:
        console.print()
        outcome_id = h.get("outcome_id", "")[:8]
        issue_type = h.get("issue_type", "?")
        strategy = h.get("strategy", "?")
        outcome = h.get("verification_outcome", "?")
        score_before = h.get("score_before", "?")
        score_after = h.get("score_after", "?")
        tokens = h.get("tokens_used", "?")
        forked = h.get("forked", "?")
        color = _outcome_color(outcome)

        panel = Panel(
            f"  [dim]issue_type:[/dim] {issue_type}\n"
            f"  [dim]strategy:[/dim] {strategy}\n"
            f"  [dim]verification_outcome:[/dim] [{color}]{outcome}[/{color}]\n"
            f"  [dim]score:[/dim] {score_before} -> {score_after}\n"
            f"  [dim]tokens:[/dim] {tokens}  [dim]forked:[/dim] {forked}",
            title=f"[bold][{outcome_id}][/bold]",
            border_style="cyan",
            width=96,
            padding=(0, 1),
        )
        console.print(panel)


def print_mcp_decision_traces(outcome_id: str, traces: list[dict], count: int) -> None:
    print_section("MCP: get_decision_trace (reasoning chain)")
    console.print(f"  Traces for outcome {outcome_id[:8]}: {count}")

    trace_table = Table(show_header=True, header_style="bold cyan", border_style="dim", width=96)
    trace_table.add_column("Stage", style="bold magenta", width=15)
    trace_table.add_column("Reasoning", width=78)
    for t in traces[:5]:
        stage = t.get("stage", "?")
        reasoning = _truncate(t.get("reasoning", ""), 100)
        trace_table.add_row(stage, reasoning)
    console.print(trace_table)


def print_mcp_remediation_context(issue_type: str, result: dict) -> None:
    print_section("MCP: get_remediation_context (what Run 3 would see)")
    console.print(f"  [bold]Context for '{issue_type}':[/bold]")
    successful = result.get("successful_strategies", [])
    failed = result.get("failed_strategies", [])
    total = result.get("total_attempts", 0)

    console.print(f"    [green]Successful strategies: {len(successful)}[/green]")
    console.print(f"    [red]Failed strategies: {len(failed)}[/red]")
    console.print(f"    [dim]Total attempts: {total}[/dim]")

    if result.get("fallback_used"):
        console.print(f"    [yellow](Fallback: broad match used -- no exact issue_type match)[/yellow]")


def print_mcp_signal_lifecycle_summary(new_sigs: list, persistent_sigs: list, resolved_sigs: list, recurring_sigs: list = None) -> None:
    print_section("Signal Lifecycle (new -> persistent -> resolved)")
    recurring_sigs = recurring_sigs or []
    console.print(f"  new: {len(new_sigs)}, persistent: {len(persistent_sigs)}, resolved: {len(resolved_sigs)}, recurring: {len(recurring_sigs)}")
    if resolved_sigs:
        console.print(f"  [bold green]Resolved signals (sample):[/bold green]")
        for s in resolved_sigs[:3]:
            sev = s.severity if hasattr(s, "severity") else "?"
            stype = s.signal_type if hasattr(s, "signal_type") else "?"
            uri = _truncate(s.artifact_uri if hasattr(s, "artifact_uri") else "", 50)
            sev_color = _severity_color(sev)
            console.print(f"    [{sev_color}]{sev}[/{sev_color}] {stype} -> {uri}")


def print_mcp_metrics(metrics: dict) -> None:
    print_section("MCP: get_remediation_metrics")
    table = Table(show_header=True, header_style="bold cyan", border_style="cyan", width=96)
    table.add_column("Metric", style="dim", width=30)
    table.add_column("Value", style="bold white", width=20)
    for k, v in metrics.items():
        if isinstance(v, float):
            v_str = f"{v:.1f}"
        else:
            v_str = str(v)
        table.add_row(k, v_str)
    console.print(table)


# ---------------------------------------------------------------------------
# Demo complete
# ---------------------------------------------------------------------------

def print_demo_complete(runs: int = 3) -> None:
    """Print the final demo completion summary."""
    console.print()
    console.print(Rule(style="bold green"))
    console.print(Align.center("[bold green]Demo Complete[/bold green]"))
    console.print(Rule(style="bold green"))

    run_lines = []
    for i in range(1, runs + 1):
        if i == 1:
            run_lines.append(f"  [green]{3+i}.[/green] Run {i} had no history -> outcome saved to CockroachDB")
        else:
            run_lines.append(f"  [green]{3+i}.[/green] Run {i} read CockroachDB history -> saw {i-1} prior outcome(s)")
    run_lines.append(f"  [green]{3+runs+1}.[/green] Run {runs+1} would see ALL {runs} outcomes + decision traces + vector search")

    body = (
        "[bold]The closed loop is now functional (CockroachDB-only, no SQLite):[/bold]\n\n"
        "  [green]1.[/green] Artifacts saved with VECTOR(384) embeddings -> semantic search\n"
        "  [green]2.[/green] Signals + lifecycle tracked in CockroachDB (new -> persistent -> resolved)\n"
        "  [green]3.[/green] Agent workflow state persisted (active -> paused -> completed)\n"
        + "\n".join(run_lines) + "\n\n"
        "[bold]CockroachDB tools demonstrated:[/bold]\n"
        "  [cyan]*[/cyan] Distributed Vector Indexing (VECTOR(384) + <=> cosine distance)\n"
        "  [cyan]*[/cyan] ACID Transactions (retry on serialization conflicts)\n"
        "  [cyan]*[/cyan] Agent State Persistence (Burr workflow survives restarts)\n"
        "  [cyan]*[/cyan] Signal Lifecycle Tracking (NEW -> PERSISTENT -> RESOLVED)\n"
        "  [cyan]*[/cyan] Decision Traces (full reasoning chain observability)"
    )
    panel = Panel(body, border_style="green", width=100, padding=(1, 2))
    console.print(panel)
    console.print(Rule(style="bold green"))


# ---------------------------------------------------------------------------
# Per-run comparison and CockroachDB verification (3-run demo)
# ---------------------------------------------------------------------------

def print_per_run_summary(results: list[dict]) -> None:
    """Display a per-run comparison table showing improvement across all runs."""
    print_banner("Per-Run Improvement Summary")

    table = Table(show_header=True, header_style="bold cyan", border_style="cyan", width=96)
    table.add_column("Run", style="bold", justify="center", width=5)
    table.add_column("Score Before", justify="right", width=13)
    table.add_column("Score After", justify="right", width=12)
    table.add_column("Delta", justify="right", width=8)
    table.add_column("Resolved", justify="right", width=10)
    table.add_column("New", justify="right", width=6)
    table.add_column("Outcome", width=18)
    table.add_column("Strategy", width=24)

    for r in results:
        delta = r.get("score_delta", 0)
        delta_color = _delta_color(delta)
        delta_str = f"{delta:+}" if isinstance(delta, (int, float)) else "?"
        outcome = r.get("outcome", "unknown")
        outcome_color = _outcome_color(outcome)
        before = r.get("score_before", "?")
        after = r.get("score_after", "?")
        before_color = _score_color(before) if isinstance(before, (int, float)) else "white"
        after_color = _score_color(after) if isinstance(after, (int, float)) else "white"

        table.add_row(
            str(r.get("run", "?")),
            f"[{before_color}]{before}[/]",
            f"[{after_color}]{after}[/]",
            f"[{delta_color}]{delta_str}[/]",
            str(r.get("signals_resolved", 0)),
            str(r.get("new_signals", 0)),
            f"[{outcome_color}]{outcome}[/]",
            _truncate(r.get("strategy", "?"), 24),
        )

    console.print(table)


def print_cumulative_improvement(results: list[dict]) -> None:
    """Display total improvement across all runs."""
    if not results:
        return

    print_section("Cumulative Improvement")

    first = results[0]
    last = results[-1]
    total_delta = last.get("score_after", 0) - first.get("score_before", 0)
    total_resolved = sum(r.get("signals_resolved", 0) for r in results)
    total_new = sum(r.get("new_signals", 0) for r in results)
    total_rollbacks = sum(1 for r in results if r.get("rolled_back", False))

    table = Table(show_header=True, header_style="bold green", border_style="green", width=96)
    table.add_column("Metric", style="dim", width=30)
    table.add_column("Value", style="bold white", width=20)
    table.add_column("Trend", width=20)

    table.add_row("Score (Run 1 before)", str(first.get("score_before", "?")), "")
    table.add_row(f"Score (Run {len(results)} after)", str(last.get("score_after", "?")), "")
    table.add_row(
        "Total score improvement",
        f"[{_delta_color(total_delta)}]{total_delta:+}[/]",
        f"[{_delta_color(total_delta)}]{'improved' if total_delta > 0 else 'unchanged'}[/]",
    )
    table.add_row("Total signals resolved", str(total_resolved), "")
    table.add_row("Total new signals", str(total_new), "")
    table.add_row("Runs completed", str(len(results)), "")
    if total_rollbacks:
        table.add_row("Rollbacks (regression safety)", str(total_rollbacks), "")

    console.print(table)


def print_cockroach_verification(verification: dict) -> None:
    """Display CockroachDB verification results."""
    print_banner("CockroachDB Verification")

    # Context storage
    print_section("Context Storage (remediation_history)")
    cs = verification.get("context_storage", {})
    outcomes_count = cs.get("outcomes_count", 0)
    all_have_steps = cs.get("all_have_modification_steps", False)
    issue_types = cs.get("issue_types", [])

    if outcomes_count > 0:
        console.print(f"  [green]\u2713[/green] {outcomes_count} outcome(s) stored in remediation_history")
        if all_have_steps:
            console.print(f"  [green]\u2713[/green] All outcomes have modification_steps (institutional memory)")
        else:
            console.print(f"  [yellow]\u26a0[/yellow] Some outcomes missing modification_steps")
        if issue_types:
            console.print(f"  [dim]Issue types: {', '.join(issue_types)}[/dim]")
    else:
        console.print(f"  [red]\u2717[/red] No outcomes found in remediation_history")

    # Agent state
    print_section("Agent State (Burr + CockroachDB)")
    as_state = verification.get("agent_state", {})
    workflows_count = as_state.get("workflows_count", 0)
    all_completed = as_state.get("all_completed", False)
    stages = as_state.get("stages", [])

    if workflows_count > 0:
        console.print(f"  [green]\u2713[/green] {workflows_count} workflow(s) persisted in agent_state")
        if all_completed:
            console.print(f"  [green]\u2713[/green] All workflows have status=completed")
        else:
            console.print(f"  [yellow]\u26a0[/yellow] Some workflows not completed")
        if stages:
            console.print(f"  [dim]Stages: {', '.join(stages)}[/dim]")
    else:
        console.print(f"  [red]\u2717[/red] No workflows found in agent_state")

    # MCP tools
    print_section("MCP Tools (CockroachDB Cloud managed MCP)")
    mcp_results = verification.get("mcp_tools", {})
    for tool_name, result in mcp_results.items():
        if "error" in result:
            console.print(f"  [red]\u2717[/red] {tool_name}: {result.get('error', 'error')}")
        elif "message" in result and "count" not in result and "score" not in result and "results" not in result and "total_workflows" not in result:
            console.print(f"  [yellow]\u26a0[/yellow] {tool_name}: {result.get('message', 'no data')}")
        elif "count" in result:
            console.print(f"  [green]\u2713[/green] {tool_name}: {result['count']} items returned")
        elif "score" in result:
            console.print(f"  [green]\u2713[/green] {tool_name}: score={result.get('score', '?')}, signals={result.get('signals_count', '?')}")
        elif "results" in result:
            console.print(f"  [green]\u2713[/green] {tool_name}: {len(result['results'])} results returned")
        elif "total_workflows" in result:
            console.print(f"  [green]\u2713[/green] {tool_name}: {result['total_workflows']} workflows, {result.get('resolved', 0)} resolved")
        else:
            console.print(f"  [green]\u2713[/green] {tool_name}: data returned")

    # Vector search
    print_section("Vector Search (CockroachDB distributed vector index)")
    vs = verification.get("vector_search", {})
    vs_results = vs.get("results", [])
    if vs_results:
        console.print(f"  [green]\u2713[/green] {len(vs_results)} results returned")
        for r in vs_results[:3]:
            sim = r.get("similarity", 0)
            uri = _truncate(r.get("artifact_uri", "?"), 60)
            console.print(f"    [bold]sim={sim:.3f}[/bold]  [dim]{uri}[/dim]")
    else:
        console.print(f"  [red]\u2717[/red] No vector search results")

    # Memory influence summary
    print_section("Institutional Memory Influence (per run)")
    memory_summary = verification.get("memory_influence", [])
    for m in memory_summary:
        run = m.get("run", "?")
        retrieved = m.get("retrieved_count", 0)
        avoided = m.get("avoided_count", 0)
        reused = m.get("reused_strategy")
        if retrieved > 0:
            line = f"  Run {run}: [green]{retrieved} outcome(s) retrieved[/green], [red]{avoided} avoided[/red]"
            if reused:
                line += f", [green]reused: {_truncate(reused, 30)}[/green]"
            console.print(line)
        else:
            console.print(f"  Run {run}: [dim]no prior outcomes (first run)[/dim]")


# ---------------------------------------------------------------------------
# Error display
# ---------------------------------------------------------------------------

def print_memory_ab_test(ab_result: dict) -> None:
    """Display the A/B memory test result (Item 1).

    Shows two proposal generations side-by-side:
    - With memory (prior outcomes retrievable)
    - Without memory (empty history store)

    Highlights whether the strategy or proposal count differs,
    proving that institutional memory causally influences LLM output.
    """
    print_banner("A/B Memory Test (proves memory influences LLM)")

    with_mem = ab_result.get("with_memory", {})
    without_mem = ab_result.get("without_memory", {})
    differs = ab_result.get("differs", False)
    explanation = ab_result.get("explanation", "")

    # Side-by-side comparison table
    table = Table(show_header=True, header_style="bold cyan", border_style="cyan", width=96)
    table.add_column("Metric", style="dim", width=22)
    table.add_column("With Memory", style="bold green", width=35)
    table.add_column("Without Memory", style="bold yellow", width=35)

    table.add_row(
        "Strategy",
        _truncate(with_mem.get("strategy", "?"), 33),
        _truncate(without_mem.get("strategy", "?"), 33),
    )
    table.add_row(
        "Proposal count",
        str(with_mem.get("proposals_count", 0)),
        str(without_mem.get("proposals_count", 0)),
    )
    table.add_row(
        "Reasoning (excerpt)",
        _truncate(with_mem.get("reasoning_excerpt", ""), 33),
        _truncate(without_mem.get("reasoning_excerpt", ""), 33),
    )

    # Memory influence details
    mi_with = with_mem.get("memory_influence", {})
    mi_without = without_mem.get("memory_influence", {})
    retrieved_with = len(mi_with.get("retrieved", []))
    retrieved_without = len(mi_without.get("retrieved", []))
    avoided_with = len(mi_with.get("avoided_strategies", []))
    avoided_without = len(mi_without.get("avoided_strategies", []))

    table.add_row(
        "Retrieved outcomes",
        str(retrieved_with),
        str(retrieved_without),
    )
    table.add_row(
        "Avoided strategies",
        str(avoided_with),
        str(avoided_without),
    )

    reused = mi_with.get("reused_strategy", "")
    table.add_row(
        "Reused strategy",
        _truncate(reused, 33) if reused else "(none)",
        "(none)",
    )

    console.print(table)

    # Verdict
    if differs:
        console.print()
        console.print(f"  [bold green]RESULT: Memory DID influence the proposal.[/bold green]")
    else:
        console.print()
        console.print(f"  [yellow]RESULT: Memory did not change the proposal.[/yellow]")
    console.print(f"  [dim]{explanation}[/dim]")


def print_pause_resume_demo(result: dict) -> None:
    """Display the pause/resume demonstration result (Item 2).

    Shows that workflow state survives across a simulated process restart
    via CockroachDB persistence:
    - The paused stage (e.g., "awaiting_approval")
    - The resumed stage (should match paused)
    - Whether the state matched
    - The final stage after completion
    """
    print_banner("Pause/Resume Demonstration (proves state survives restart)")

    paused_stage = result.get("paused_stage", "unknown")
    resumed_stage = result.get("resumed_stage", "unknown")
    state_match = result.get("state_match", False)
    completed = result.get("completed", False)
    final_stage = result.get("final_stage", "unknown")
    app_id = result.get("app_id", "?")

    table = Table(show_header=True, header_style="bold cyan", border_style="cyan", width=96)
    table.add_column("Phase", style="dim", width=30)
    table.add_column("Value", style="bold white", width=60)

    table.add_row("App ID", _truncate(app_id, 58))
    table.add_row("Paused stage", paused_stage)
    table.add_row("Resumed stage", resumed_stage)

    match_str = "[bold green]MATCH[/bold green]" if state_match else "[bold red]MISMATCH[/bold red]"
    table.add_row("State match", match_str)
    table.add_row("Final stage", final_stage)

    completed_str = "[bold green]YES[/bold green]" if completed else "[bold red]NO[/bold red]"
    table.add_row("Completed", completed_str)

    console.print(table)

    if state_match and completed:
        console.print()
        console.print(
            "  [bold green]RESULT: Workflow successfully resumed from CockroachDB[/bold green]"
        )
        console.print(
            f"  [dim]Paused at '{paused_stage}' -> resumed at '{resumed_stage}' -> completed at '{final_stage}'[/dim]"
        )
    elif not state_match:
        console.print()
        console.print(
            "  [bold red]RESULT: State mismatch — resume did not restore correct state[/bold red]"
        )
    elif not completed:
        console.print()
        console.print(
            f"  [yellow]RESULT: Workflow resumed but did not complete (final stage: {final_stage})[/yellow]"
        )


def print_error(msg: str) -> None:
    console.print(f"  [bold red]ERROR: {msg}[/bold red]")


def print_warning(msg: str) -> None:
    console.print(f"  [yellow]WARNING: {msg}[/yellow]")


def print_dim(msg: str) -> None:
    console.print(f"  [dim]{msg}[/dim]")


def print_ok(msg: str) -> None:
    console.print(f"  [green]{msg}[/green]")


# ---------------------------------------------------------------------------
# Cloud (Lambda/S3/EventBridge) display functions
# ---------------------------------------------------------------------------

def print_cloud_intro(
    endpoint: str,
    function_name: str,
    s3_bucket: str,
    s3_prefix: str,
    num_cycles: int,
    artifact_count: int = 0,
) -> None:
    """Print the cloud demo intro panel showing the AWS architecture."""
    body = (
        f"[bold cyan]AWS Cloud Architecture:[/bold cyan]\n\n"
        f"  [dim]EventBridge[/dim]  [cyan]---schedule--->[/cyan]  [bold]Lambda[/bold]  [cyan]---assess--->[/cyan]  [bold]S3[/bold]\n"
        f"  [dim](rate 5 min)[/dim]                    [bold](serverless)[/bold]    [cyan]<---remediate--[/cyan]  [bold](artifacts)[/bold]\n"
        f"                                       |                          |\n"
        f"                                       [cyan]---persist--->[/cyan]  [bold]CockroachDB[/bold] [dim](memory + vectors)[/dim]\n"
        f"                                       [cyan]<---context---[/cyan]  [bold](ACID + distributed)[/bold]\n\n"
        f"[dim]This demo runs the improvement workflow {num_cycles} time(s) entirely on AWS Lambda:[/dim]\n"
        f"  [green]*[/green] Artifacts stored in S3 (durable, versioned)\n"
        f"  [green]*[/green] Assessment + remediation run as Lambda functions (serverless)\n"
        f"  [green]*[/green] Institutional memory in CockroachDB (survives cold starts)\n"
        f"  [green]*[/green] EventBridge triggers periodic assessment (automated)\n\n"
        f"[dim]Endpoint:    [/dim] {endpoint}\n"
        f"[dim]Function:   [/dim] {function_name}\n"
        f"[dim]S3 source:  [/dim] s3://{s3_bucket}/{s3_prefix}/\n"
        f"[dim]Artifacts:  [/dim] {artifact_count if artifact_count else '(loading...)'}\n"
        f"[dim]Cycles:     [/dim] {num_cycles}"
    )
    panel = Panel(
        body,
        title="[bold cyan]Cloud-Native Closed-Loop Demo (Lambda + S3 + CockroachDB)[/bold cyan]",
        subtitle="[dim]Serverless • Event-driven • Distributed[/dim]",
        border_style="cyan",
        width=100,
    )
    console.print(panel)


def print_lambda_invoking(action: str, cycle: int | None = None) -> None:
    """Print a message before invoking Lambda."""
    label = f"Cycle {cycle} " if cycle else ""
    console.print(f"  [dim]{label}Invoking Lambda: action={action}...[/dim]", end=" ")


def print_lambda_response(
    action: str,
    elapsed: float,
    result: dict,
    cycle: int | None = None,
) -> None:
    """Print a summary of a Lambda response."""
    label = f"Cycle {cycle} " if cycle else ""
    console.print(f"[green]OK[/green] ({elapsed:.1f}s)")

    if action == "assess":
        score = result.get("score", "?")
        artifacts = result.get("artifact_count", "?")
        signals = result.get("signal_count", "?")
        problems = result.get("problems_discovered", "?")
        console.print(f"  [dim]{label}Assessment:[/dim] score={score}, "
                       f"artifacts={artifacts}, signals={signals}, "
                       f"problems={problems}")

    elif action == "remediate":
        outcome = result.get("verification_outcome", "?")
        before = result.get("score_before", "?")
        after = result.get("score_after", "?")
        delta = result.get("score_delta", 0)
        strategy = _truncate(result.get("strategy", "?"), 30)
        ctx = result.get("context_used", {})
        past = ctx.get("past_attempts", 0)
        resolved = result.get("signals_resolved", 0)
        new_sigs = result.get("new_signals", 0)
        modified = len(result.get("modified_uris", []))
        rolled_back = result.get("rolled_back", False)

        outcome_color = _outcome_color(outcome)
        delta_color = _delta_color(delta)

        console.print(f"  [dim]{label}Remediation:[/dim]")
        console.print(f"    Outcome:  [{outcome_color}]{outcome}[/{outcome_color}]")
        console.print(f"    Score:     {before} -> {after} "
                       f"[{delta_color}]({delta:+})[/{delta_color}]")
        console.print(f"    Strategy:  {strategy}")
        console.print(f"    Signals:  {resolved} resolved, {new_sigs} new")
        console.print(f"    Modified: {modified} file(s)")
        if rolled_back:
            console.print(f"    [bold magenta]ROLLED BACK (regression detected)[/bold magenta]")
        console.print(f"    Memory:   past_attempts={past}, "
                       f"successful={ctx.get('successful_strategies', 0)}, "
                       f"failed={ctx.get('failed_strategies', 0)}, "
                       f"similar={ctx.get('similar_problems', 0)}")

    elif action == "status":
        queue = result.get("problem_queue", {})
        metrics = result.get("remediation_metrics", {})
        console.print(f"  [dim]Status:[/dim] "
                       f"open={queue.get('open', 0)}, "
                       f"resolved={queue.get('resolved', 0)}, "
                       f"total_outcomes={metrics.get('total_outcomes', 0)}")


def print_s3_summary(bucket: str, prefix: str, artifact_count: int) -> None:
    """Print S3 storage summary."""
    print_section("S3 Knowledge Storage")
    console.print(f"  [bold]Bucket:[/bold] s3://{bucket}/{prefix}/")
    console.print(f"  [green]Artifacts: {artifact_count}[/green]")
    console.print(f"  [dim]Durable: 11 nines (99.999999999%)[/dim]")
    console.print(f"  [dim]Versioned: S3 versioning enabled[/dim]")


def print_eventbridge_summary(rules: list[dict]) -> None:
    """Print EventBridge rules summary."""
    print_section("EventBridge Scheduled Rules")
    if not rules:
        console.print(f"  [dim]No rules configured[/dim]")
        return
    table = Table(show_header=True, header_style="bold cyan", border_style="cyan", width=96)
    table.add_column("Rule Name", style="bold", width=25)
    table.add_column("Schedule", width=20)
    table.add_column("State", width=10)
    table.add_column("Target", width=35)
    for r in rules:
        name = r.get("name", "?")
        schedule = r.get("schedule", "?")
        state = r.get("state", "?")
        target = r.get("target", "?")
        state_color = "green" if state == "ENABLED" else "dim red"
        table.add_row(name, schedule, f"[{state_color}]{state}[/{state_color}]",
                       _truncate(target, 35))
    console.print(table)


def print_cloud_demo_complete(runs: int, total_elapsed: float,
                               lambda_invocations: int) -> None:
    """Print the cloud demo completion summary."""
    console.print()
    console.print(Rule(style="bold green"))
    console.print(Align.center("[bold green]Cloud Demo Complete[/bold green]"))
    console.print(Rule(style="bold green"))

    run_lines = []
    for i in range(1, runs + 1):
        if i == 1:
            run_lines.append(
                f"  [green]{3+i}.[/green] Run {i} had no history -> outcome saved to CockroachDB"
            )
        else:
            run_lines.append(
                f"  [green]{3+i}.[/green] Run {i} read CockroachDB history -> saw {i-1} prior outcome(s)"
            )
    run_lines.append(
        f"  [green]{3+runs+1}.[/green] Run {runs+1} would see ALL {runs} outcomes + decision traces + vector search"
    )

    body = (
        "[bold]The closed loop is now functional entirely on AWS Lambda:[/bold]\n\n"
        "  [green]1.[/green] Artifacts stored in S3 -> durable, versioned, event-driven\n"
        "  [green]2.[/green] Assessment + remediation run as Lambda -> serverless, auto-scaling\n"
        "  [green]3.[/green] Institutional memory in CockroachDB -> survives cold starts\n"
        "  [green]4.[/green] EventBridge triggers periodic assessment -> automated\n"
        + "\n".join(run_lines) + "\n\n"
        f"[bold]Cloud services demonstrated:[/bold]\n"
        "  [cyan]*[/cyan] S3 (durable artifact storage, 11 nines)\n"
        "  [cyan]*[/cyan] Lambda (serverless compute, 15-min timeout)\n"
        "  [cyan]*[/cyan] EventBridge (scheduled assessment, event routing)\n"
        "  [cyan]*[/cyan] CockroachDB (distributed ACID, vector index, agent state)\n\n"
        f"[dim]Total elapsed: {total_elapsed:.1f}s[/dim]\n"
        f"[dim]Lambda invocations: {lambda_invocations}[/dim]"
    )
    panel = Panel(body, border_style="green", width=100, padding=(1, 2))
    console.print(panel)
    console.print(Rule(style="bold green"))
