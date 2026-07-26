"""CLI entry point - ai-ready command."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Optional

# Force UTF-8 output on Windows to avoid cp1252 encoding errors
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import typer
from rich.console import Console
from rich.table import Table

from ai_ready.config import Config
from ai_ready.output import (
    format_json,
    format_regression_json,
    format_regression_terminal,
    format_sarif,
    format_terminal,
)
from ai_ready.knowledge.loader import load_knowledge_base
from ai_ready.pipeline import ScanPipeline
from ai_ready.snapshot import SnapshotStore

app = typer.Typer(
    name="ai-ready",
    help="AI knowledge observability platform - evaluate whether a knowledge base is AI-ready",
    no_args_is_help=True,
)
console = Console()

# Default snapshot DB location
DEFAULT_DB = Path.cwd() / ".ai-ready" / "snapshots.db"


def _load_config(source: str | Path) -> Config:
    """Load config from .ai-ready.yml in the source directory."""
    source_path = Path(source)
    if source_path.is_dir():
        config_path = source_path / ".ai-ready.yml"
    else:
        config_path = Path.cwd() / ".ai-ready.yml"
    return Config.from_file(config_path)


def _get_store(db_path: str | None) -> SnapshotStore:
    """Get snapshot store."""
    path = Path(db_path) if db_path else DEFAULT_DB
    return SnapshotStore(path)


def _detect_git_commit(source: str | Path) -> str:
    """Try to detect the current git commit."""
    import subprocess
    try:
        source_path = Path(source)
        cwd = source_path if source_path.is_dir() else source_path.parent
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(cwd), timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _load_kb(source: Path, config: Config) -> tuple[list, list]:
    """Load documents and relations from source."""
    knowledge_source = load_knowledge_base(source)
    return knowledge_source.documents, knowledge_source.relations


def _run_incremental_scan(
    pipeline: ScanPipeline,
    source: Path,
    documents: list,
    relations: list,
    git_commit: str,
    db: str | None,
) -> Any:
    """Run an incremental scan, falling back to full scan if needed.

    Loads the previous snapshot, detects changes via git diff, and
    delegates to pipeline.run_incremental(). Falls back to a full scan
    if no previous snapshot exists or changes can't be determined.
    """
    from ai_ready.incremental import detect_changes_via_git, detect_changes

    store = _get_store(db)
    prev_snapshot = store.latest()

    if prev_snapshot is None:
        console.print("[dim]No previous snapshot found — running full scan.[/dim]")
        return pipeline.run(documents, source=str(source), git_commit=git_commit, relations=relations)

    # Try git-based change detection first
    change_events = detect_changes_via_git(source, prev_snapshot, documents)

    if change_events is None:
        # Git not available — fall back to snapshot comparison
        change_events = detect_changes(source, prev_snapshot, documents)

    if not change_events:
        console.print("[dim]No changes detected — reusing previous snapshot.[/dim]")
        # Still save a new snapshot with updated metadata
        return pipeline.run(documents, source=str(source), git_commit=git_commit, relations=relations)

    changed_count = len({e.document_path for e in change_events})
    console.print(f"[dim]Incremental scan: {changed_count} document(s) changed.[/dim]")

    return pipeline.run_incremental(
        prev_snapshot=prev_snapshot,
        change_events=change_events,
        documents=documents,
        relations=relations,
        source=str(source),
        git_commit=git_commit,
    )


@app.command()
def scan(
    path: str = typer.Argument(..., help="Path to the knowledge base directory"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
    sarif: bool = typer.Option(False, "--sarif", help="Output as SARIF"),
    db: str = typer.Option(None, "--db", help="Snapshot database path"),
    config_path: str = typer.Option(None, "--config", help="Config file path"),
    save: bool = typer.Option(True, "--save/--no-save", help="Save snapshot to store"),
    incremental: bool = typer.Option(False, "--incremental", help="Run incremental scan (only re-evaluate changed documents)"),
) -> None:
    """Scan a knowledge base and produce an AI readiness report."""
    source = Path(path).resolve()
    if not source.exists():
        console.print(f"[red]Error: Path does not exist: {source}[/red]")
        raise typer.Exit(3)

    # Load config
    if config_path:
        config = Config.from_file(config_path)
    else:
        config = _load_config(source)

    # Connect and load documents + relations
    documents, relations = _load_kb(source, config)

    if not documents:
        console.print(f"[yellow]Warning: No documents found in {source}[/yellow]")
        raise typer.Exit(0)

    # Detect git commit
    git_commit = _detect_git_commit(source)

    # Run pipeline
    pipeline = ScanPipeline(
        weights=config.weights,
        enabled_rules=config.enabled_collector_ids,
        thresholds=config.thresholds,
        fail_on=config.fail_on,
    )

    if incremental:
        snapshot = _run_incremental_scan(
            pipeline, source, documents, relations, git_commit, db
        )
    else:
        snapshot = pipeline.run(documents, source=str(source), git_commit=git_commit, relations=relations)

    # Save snapshot
    if save:
        store = _get_store(db)
        store.save(snapshot)

    # Output
    if json_output:
        print(format_json(snapshot))
    elif sarif:
        print(format_sarif(snapshot))
    else:
        print(format_terminal(snapshot))

    # Exit code
    exit_code = pipeline.get_exit_code(snapshot)
    if exit_code != 0:
        raise typer.Exit(exit_code)


@app.command()
def diff(
    prev_id: str = typer.Option(None, "--prev", help="Previous snapshot ID (default: auto-detect)"),
    curr_id: str = typer.Option(None, "--curr", help="Current snapshot ID (default: latest)"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
    db: str = typer.Option(None, "--db", help="Snapshot database path"),
) -> None:
    """Diff two snapshots and report what changed."""
    store = _get_store(db)

    # Determine which snapshots to compare
    if prev_id and curr_id:
        report = store.diff(prev_id, curr_id)
    elif not prev_id and not curr_id:
        report = store.diff_latest()
    else:
        # curr specified, auto-detect prev
        curr = store.load(curr_id) if curr_id else store.latest()
        if not curr:
            console.print("[red]Error: No current snapshot found[/red]")
            raise typer.Exit(3)
        prev = store.previous(curr.snapshot_id)
        if not prev:
            console.print("[red]Error: No previous snapshot found[/red]")
            raise typer.Exit(3)
        report = store._compute_diff(prev, curr)

    if not report:
        console.print("[yellow]Not enough snapshots to diff (need at least 2).[/yellow]")
        raise typer.Exit(0)

    if json_output:
        print(format_regression_json(report))
    else:
        print(format_regression_terminal(report))

    if report.score_delta < 0 or report.new_high_count > 0:
        raise typer.Exit(2)


@app.command()
def history(
    limit: int = typer.Option(10, "--limit", help="Number of snapshots to show"),
    db: str = typer.Option(None, "--db", help="Snapshot database path"),
) -> None:
    """Show snapshot history."""
    store = _get_store(db)
    snapshots = store.history(limit=limit)

    if not snapshots:
        console.print("[yellow]No snapshots found.[/yellow]")
        return

    console.print("")
    console.print("AI Readiness History")
    console.print("=" * 60)
    for snap in snapshots:
        console.print(f"  {snap.snapshot_id}  Score: {snap.score}/100  "
                       f"Signals: {len(snap.findings)}  "
                       f"Source: {snap.metadata.get('source', 'N/A')}")
    console.print("=" * 60)


@app.command()
def status(
    db: str = typer.Option(None, "--db", help="Snapshot database path"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show current health, recent changes, and oldest unresolved findings."""
    store = _get_store(db)
    latest = store.latest()

    if not latest:
        console.print("[yellow]No snapshots found. Run a scan first.[/yellow]")
        raise typer.Exit(0)

    if json_output:
        import json
        result = {
            "latest_snapshot": latest.to_dict(),
            "diff": store.diff_latest().to_dict() if store.diff_latest() else None,
            "finding_stats": store.get_finding_stats(),
            "oldest_findings": [f.to_dict() for f in store.get_oldest_findings(limit=10)],
            "recently_resolved": [f.to_dict() for f in store.get_recently_resolved(limit=10)],
        }
        print(json.dumps(result, indent=2, default=str))
        return

    # Terminal output
    console.print("")
    console.print("[bold]AI-Ready Status[/bold]")
    console.print("=" * 60)

    # Latest snapshot summary
    console.print(f"\n  Latest Scan: {latest.snapshot_id}")
    console.print(f"  Score:       {latest.score}/100")
    console.print(f"  Documents:   {latest.metadata.get('document_count', '?')}")
    console.print(f"  Findings:    {len(latest.findings)}")
    console.print(f"  Source:      {latest.metadata.get('source', 'N/A')}")
    console.print(f"  Collectors:  {', '.join(sorted(latest.dimensions.keys()))}")
    if latest.metadata.get("git_commit"):
        console.print(f"  Git:         {latest.metadata['git_commit']}")

    # Dimensions
    console.print(f"\n  Dimensions:")
    for dim_name, dim in sorted(latest.dimensions.items()):
        console.print(f"    {dim_name:20s} {dim.score:>3}/100  ({dim.findings_count} signals)")

    # Recent changes
    diff_report = store.diff_latest()
    if diff_report:
        console.print(f"\n  Changes since last scan:")
        console.print(f"    Score: {diff_report.prev_score} -> {diff_report.curr_score} ({diff_report.score_delta:+d})")
        console.print(f"    New findings: {len(diff_report.new_findings)}")
        console.print(f"    Resolved:     {len(diff_report.resolved_findings)}")
        console.print(f"    Persistent:   {len(diff_report.persistent_findings)}")
        if diff_report.severity_changes:
            console.print(f"    Severity changes: {len(diff_report.severity_changes)}")
        if diff_report.explanation:
            console.print(f"    {diff_report.explanation}")

    # Finding stats
    stats = store.get_finding_stats()
    console.print(f"\n  Finding Lifecycle:")
    console.print(f"    New:         {stats.get('new', 0)}")
    console.print(f"    Persistent:  {stats.get('persistent', 0)}")
    console.print(f"    Recurring:   {stats.get('recurring', 0)}")
    console.print(f"    Resolved:    {stats.get('resolved', 0)}")
    if stats.get("avg_age_days"):
        console.print(f"    Avg age:     {stats['avg_age_days']} days")

    # Oldest unresolved findings
    oldest = store.get_oldest_findings(limit=5)
    if oldest:
        console.print(f"\n  Oldest Unresolved Signals:")
        for f in oldest:
            console.print(f"    {f.rule_id:25s} {f.document_path}  (since {f.first_seen[:10]})")

    # Recently resolved
    resolved = store.get_recently_resolved(limit=5)
    if resolved:
        console.print(f"\n  Recently Resolved:")
        for f in resolved:
            console.print(f"    {f.rule_id:25s} {f.document_path}  (resolved in {f.resolved_snapshot})")

    console.print("\n" + "=" * 60)


@app.command()
def trend(
    limit: int = typer.Option(30, "--limit", help="Number of snapshots to analyze"),
    db: str = typer.Option(None, "--db", help="Snapshot database path"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show health trend over time."""
    store = _get_store(db)
    report = store.get_trend(limit=limit)

    if json_output:
        print(format_json_trend(report))
        return

    console.print("")
    console.print("[bold]AI-Ready Trend Analysis[/bold]")
    console.print("=" * 60)

    if not report.score_trend:
        console.print(f"\n  {report.summary}")
        return

    console.print(f"\n  {report.summary}")
    console.print(f"\n  Score Trend:")
    for entry in report.score_trend:
        bar = _score_bar(entry["score"])
        console.print(f"    {entry['snapshot_id']}  {bar} {entry['score']}/100")

    console.print(f"\n  Signal Trend:")
    for entry in report.finding_trend:
        console.print(f"    {entry['snapshot_id']}  Total: {entry['total_findings']:>4}  High: {entry['high_findings']:>4}")

    if report.dimension_trends:
        console.print(f"\n  Dimension Trends:")
        for dim_name, entries in sorted(report.dimension_trends.items()):
            scores = [e["score"] for e in entries]
            if len(scores) >= 2:
                delta = scores[-1] - scores[0]
                trend_str = f"{scores[0]} -> {scores[-1]} ({delta:+d})"
            else:
                trend_str = f"{scores[0]}"
            console.print(f"    {dim_name:20s} {trend_str}")

    console.print("\n" + "=" * 60)


@app.command()
def findings(
    status_filter: str = typer.Option(None, "--status", help="Filter by status (new, persistent, recurring, resolved)"),
    rule: str = typer.Option(None, "--rule", help="Filter by rule/collector ID"),
    collector: str = typer.Option(None, "--collector", help="Filter by collector ID (alias for --rule)"),
    limit: int = typer.Option(50, "--limit", help="Max findings to show"),
    db: str = typer.Option(None, "--db", help="Snapshot database path"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List findings (signals) with lifecycle information."""
    store = _get_store(db)

    if status_filter:
        from ai_ready.models import FindingStatus
        try:
            status_enum = FindingStatus(status_filter)
        except ValueError:
            console.print(f"[red]Invalid status: {status_filter}. Use: new, persistent, recurring, resolved[/red]")
            raise typer.Exit(3)

        if status_enum == FindingStatus.RESOLVED:
            all_findings = store.get_recently_resolved(limit=limit)
        elif status_enum == FindingStatus.RECURRING:
            all_findings = store.get_recurring_findings(limit=limit)
        else:
            all_findings = [f for f in store.get_open_findings(limit=limit) if f.status == status_enum]
    else:
        all_findings = store.get_open_findings(limit=limit)

    # Filter by rule/collector (--collector is an alias for --rule)
    filter_id = collector or rule
    if filter_id:
        all_findings = [f for f in all_findings if f.rule_id == filter_id]

    if json_output:
        import json
        print(json.dumps([f.to_dict() for f in all_findings], indent=2, default=str))
        return

    if not all_findings:
        console.print("[yellow]No findings found.[/yellow]")
        return

    console.print("")
    console.print(f"[bold]Findings ({len(all_findings)} shown)[/bold]")
    console.print("=" * 80)

    table = Table(show_header=True, header_style="bold")
    table.add_column("Collector", style="cyan", width=20)
    table.add_column("Document", style="white", width=40)
    table.add_column("Status", style="yellow", width=12)
    table.add_column("Age (scans)", justify="right", width=10)
    table.add_column("First Seen", width=14)

    for f in all_findings:
        table.add_row(
            f.rule_id,
            f.document_path,
            f.status.value,
            str(len(f.snapshot_ids)),
            f.first_seen[:10] if f.first_seen else "",
        )

    console.print(table)
    console.print("=" * 80)


@app.command()
def monitor(
    path: str = typer.Argument(..., help="Path to the knowledge base directory"),
    interval: int = typer.Option(300, "--interval", help="Scan interval in seconds"),
    db: str = typer.Option(None, "--db", help="Snapshot database path"),
) -> None:
    """Continuously monitor a knowledge base."""
    console.print(f"[bold]AI-Ready Monitor[/bold]")
    console.print(f"  Path: {path}")
    console.print(f"  Interval: {interval}s")
    console.print(f"  Press Ctrl+C to stop.")
    console.print("")

    try:
        while True:
            console.print(f"[dim]{time.strftime('%Y-%m-%d %H:%M:%S')} - Scanning...[/dim]")

            source = Path(path).resolve()
            config = _load_config(source)
            documents, relations = _load_kb(source, config)

            if not documents:
                console.print("[yellow]No documents found.[/yellow]")
            else:
                git_commit = _detect_git_commit(source)
                pipeline = ScanPipeline(
                    weights=config.weights,
                    enabled_rules=config.enabled_collector_ids,
                    thresholds=config.thresholds,
                    fail_on=config.fail_on,
                )
                snapshot = pipeline.run(documents, source=str(source), git_commit=git_commit, relations=relations)

                store = _get_store(db)
                store.save(snapshot)

                console.print(f"  Score: {snapshot.score}/100  Signals: {len(snapshot.findings)}  Relations: {len(relations)}")

                # Check for regression
                report = store.diff_latest()
                if report and report.score_delta < 0:
                    console.print(f"  [red][!] Regression detected: {report.score_delta:+d}[/red]")
                elif report and report.score_delta > 0:
                    console.print(f"  [green][+] Improvement: {report.score_delta:+d}[/green]")

            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[bold]Monitor stopped.[/bold]")


def _score_bar(score: int, width: int = 20) -> str:
    """Generate a simple ASCII bar chart for a score."""
    filled = int(score / 100 * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def format_json_trend(report) -> str:
    """Format a trend report as JSON."""
    import json
    return json.dumps(report.to_dict(), indent=2, default=str)


if __name__ == "__main__":
    app()
