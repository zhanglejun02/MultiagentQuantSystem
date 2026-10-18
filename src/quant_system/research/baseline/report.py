"""Human-readable view of a baseline report; JSON remains the canonical payload."""

from typing import Any


def render_report(payload: dict[str, Any]) -> str:
    strategy_label = "Candidate" if "strategy_expression" in payload else "Momentum"
    lines = [
        f"# {payload['name']}",
        "",
        "Research-only historical baseline. No deployment approval.",
        "",
        "## Provenance",
        "",
        f"- Data snapshot: {payload['data_snapshot_id']}",
        f"- Data SHA-256: {payload['data_hash']}",
        f"- Protocol SHA-256: {payload['protocol_hash']}",
        f"- Code SHA-256: {payload['code_hash']}",
        f"- Commit: {payload['code_commit']}",
        "- Candidate pinned before validation/holdout; each segment starts from cash with historical feature warmup."
        if "strategy_expression" in payload
        else "- Fixed parameters, no search; each segment starts from cash with historical feature warmup.",
        "- First trading session of each ISO week decides at close; fills earliest next open.",
        "- Exposure target 90%; remaining cash limits reservation pressure.",
        "",
        "## Results",
        "",
        f"| Segment | {strategy_label} | Equal Weight | CSI 300 Price | Excess vs Index | Max Drawdown | Cost | 2x Cost Return |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for segment in payload["segments"]:
        momentum = segment["momentum"]["performance"]
        equal = segment["equal_weight"]["performance"]
        stress = segment["cost_stress"]["performance"]
        lines.append(
            f"| {segment['name']} | {momentum['total_return']:.2%} | {equal['total_return']:.2%} | "
            f"{momentum['benchmark_return']:.2%} | {momentum['excess_return']:.2%} | "
            f"{momentum['max_drawdown']:.2%} | {segment['momentum']['execution']['transaction_cost']:.2f} | "
            f"{stress['total_return']:.2%} |"
        )
    lines.extend(
        [
            "",
            "## Data Audit",
            "",
            f"- Equity vendor: {payload['dataset'].get('source', 'unknown')}",
            f"- Benchmark source: {payload['dataset'].get('benchmark_source', 'unknown')}",
            f"- Company reference: {payload['dataset'].get('reference_source', 'unknown')}",
            f"- Cash distributions: {payload['dataset'].get('n_cash_dividends', 'unknown')}",
            "- Raw vendor responses are part of the frozen snapshot.",
        ]
    )
    for item in payload["dataset"].get("dividend_reconciliations", []):
        lines.append(
            f"- {item['symbol']} {item['ex_date']}: announced cash {item['announced_cash']}, applied cash {item['applied_cash']}."
        )
    lines.extend(
        [
            "",
            "## Execution Audit",
            "",
            "| Segment | Fill Ratio | Recorded Rule/Risk Blocks | Types |",
            "| --- | ---: | ---: | --- |",
        ]
    )
    for segment in payload["segments"]:
        record = segment["momentum"]
        types = "; ".join(f"{key}: {count}" for key, count in record["violation_counts"].items()) or "none"
        lines.append(
            f"| {segment['name']} | {record['execution']['fill_ratio']:.2%} | {record['n_violations']} | {types} |"
        )
    lines.extend(
        [
            "",
            "Fill ratio uses submitted quantity; blocked intents are disclosed separately, not hidden by that ratio.",
            "",
            "## Technical Acceptance",
            "",
            "- Frozen input integrity, complete benchmark alignment and pre-segment warmup verified.",
            "- Ledger replay verified against each recorded event-log final hash.",
            f"- Repeated equity, metrics and semantic trades match for {strategy_label.lower()} and equal-weight runs.",
            "- Event logs retain unique run IDs; reproducibility concerns economic outputs, not identical IDs.",
            "- CLI runs archive the exact source and dependency lock under code/ alongside this report.",
            "",
            "## Assessment",
            "",
            payload["assessment"],
            "",
            "## Limitations",
            "",
        ]
    )
    lines.extend(f"- {limitation}" for limitation in payload["limitations"])
    lines.extend(["", "## Backtest Receipts", ""])
    for segment in payload["segments"]:
        for label in ("momentum", "equal_weight", "cost_stress"):
            record = segment[label]
            lines.append(f"- {segment['name']}/{label}: {record['artifact_id']} ({record['run_dir']})")
    return "\n".join(lines) + "\n"
