"""Command-line interface for running and reviewing ChaosLab experiments."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from opsmind_chaoslab.catalog import EXPERIMENTS, MAX_DURATION_SECONDS
from opsmind_chaoslab.runner import ComposeRunner


def _repository_root() -> Path:
    return Path.cwd()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opsmind", description="OpsMind operations CLI")
    commands = parser.add_subparsers(dest="command", required=True)
    chaos = commands.add_parser("chaos", help="manage ShopStack chaos experiments")
    chaos_commands = chaos.add_subparsers(dest="chaos_command", required=True)
    run = chaos_commands.add_parser("run", help="run a bounded ShopStack experiment")
    run.add_argument("experiment", choices=sorted(EXPERIMENTS))
    run.add_argument(
        "--duration", type=float, help=f"duration in seconds (maximum {MAX_DURATION_SECONDS})"
    )
    run.add_argument("--compose-file", type=Path, default=Path("compose.yaml"))
    run.add_argument("--results", type=Path, default=Path(".opsmind/chaos/results.jsonl"))
    run.add_argument(
        "--dry-run", action="store_true", help="record the plan without changing services"
    )
    list_command = chaos_commands.add_parser("list", help="list available experiments")
    list_command.add_argument("--json", action="store_true")
    results = chaos_commands.add_parser("results", help="show recorded experiment results")
    results.add_argument("--results", type=Path, default=Path(".opsmind/chaos/results.jsonl"))
    results.add_argument("--limit", type=int, default=20)

    demo = commands.add_parser("demo", help="run the complete local incident-response scenario")
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)
    demo_run = demo_commands.add_parser(
        "run", help="inject a faulty payment release and resolve it"
    )
    demo_run.add_argument(
        "--requests", type=int, default=180, help="number of ShopStack orders to send"
    )
    return parser


def _list_experiments(as_json: bool) -> int:
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "name": item.name,
                        "target": item.target,
                        "failure_type": item.failure_type,
                        "duration_seconds": item.duration_seconds,
                        "expected_symptoms": item.expected_symptoms,
                        "rollback_procedure": item.rollback_procedure,
                    }
                    for item in EXPERIMENTS.values()
                ],
                indent=2,
            )
        )
        return 0
    for item in EXPERIMENTS.values():
        print(f"{item.name:34} target={item.target:32} type={item.failure_type}")
        print(f"  expected: {'; '.join(item.expected_symptoms)}")
        print(f"  rollback: {item.rollback_procedure}")
    return 0


def _show_results(path: Path, limit: int) -> int:
    if limit < 1:
        print("--limit must be positive", file=sys.stderr)
        return 2
    if not path.exists():
        print(f"No experiment results recorded at {path}")
        return 0
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for line in rows[-limit:]:
        print(json.dumps(json.loads(line), indent=2))
    return 0


def main() -> None:
    args = _parser().parse_args()
    if args.command == "demo":
        if not 40 <= args.requests <= 1000:
            raise SystemExit("--requests must be between 40 and 1000")
        from opsmind_demo.runner import run_demo

        try:
            report = run_demo(traffic_requests=args.requests)
        except Exception as exc:
            raise SystemExit(f"Demo failed: {type(exc).__name__}: {exc}") from exc
        print(f"Final report: {report}")
        return
    if args.command != "chaos":
        raise SystemExit(2)
    if args.chaos_command == "list":
        raise SystemExit(_list_experiments(args.json))
    if args.chaos_command == "results":
        raise SystemExit(_show_results(args.results, args.limit))

    experiment = EXPERIMENTS[args.experiment]
    duration = float(args.duration if args.duration is not None else experiment.duration_seconds)
    if duration <= 0 or duration > MAX_DURATION_SECONDS:
        raise SystemExit(
            f"duration must be greater than 0 and at most {MAX_DURATION_SECONDS} seconds"
        )
    compose_file = (
        args.compose_file
        if args.compose_file.is_absolute()
        else _repository_root() / args.compose_file
    )
    results_file = args.results if args.results.is_absolute() else _repository_root() / args.results
    runner = ComposeRunner(compose_file, results_file)
    print(f"Experiment {experiment.name}: {experiment.failure_type}")
    print(f"Target: {experiment.target}; duration: {duration:g}s")
    print(f"Expected: {'; '.join(experiment.expected_symptoms)}")
    print(f"Rollback: {experiment.rollback_procedure}")
    if args.dry_run:
        print("Dry run: no service changes will be made.")
    else:
        print("Scope: local Docker Compose ShopStack profile only.")
    result = runner.run(experiment, duration, dry_run=args.dry_run)
    print(f"Experiment ID: {result.experiment_id}")
    print(f"Result: {result.status}")
    if result.error:
        print(f"Error: {result.error}", file=sys.stderr)
    if result.rollback_error:
        print(f"Rollback failed: {result.rollback_error}", file=sys.stderr)
    print(f"Recorded: {results_file}")
    if result.status not in {"succeeded", "dry_run"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
