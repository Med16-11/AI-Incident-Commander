"""Compose-backed experiment execution with guaranteed best-effort rollback."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from opsmind_chaoslab.catalog import Experiment

ResultStatus = Literal["succeeded", "failed", "rollback_failed", "dry_run", "interrupted"]


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    experiment_id: str
    experiment: str
    target: str
    failure_type: str
    duration_seconds: float
    started_at: str
    finished_at: str
    status: ResultStatus
    expected_symptoms: tuple[str, ...]
    rollback_procedure: str
    error: str | None
    rollback_error: str | None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class ComposeRunner:
    """Apply controlled faults to the local ShopStack Compose profile only."""

    def __init__(self, compose_file: Path, results_file: Path) -> None:
        self.compose_file = compose_file
        self.results_file = results_file

    def _command(self, action: str, target: str, *, recreate: bool = False) -> list[str]:
        command = ["docker", "compose", "-f", str(self.compose_file), "--profile", "shopstack"]
        if action == "up":
            command.extend(["up", "-d"])
            if recreate:
                command.append("--force-recreate")
            command.append(target)
        else:
            command.extend([action, target])
        return command

    def _run_command(self, command: list[str], environment: dict[str, str]) -> None:
        completed = subprocess.run(
            command,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()[-1200:]
            raise RuntimeError(f"command failed ({completed.returncode}): {detail}")

    @staticmethod
    def _setting_name(experiment: Experiment) -> str | None:
        if experiment.setting is None:
            return None
        service_label = {
            "shopstack-api-gateway": "GATEWAY",
            "shopstack-user-service": "USER",
            "shopstack-order-service": "ORDER",
            "shopstack-inventory-service": "INVENTORY",
            "shopstack-payment-service": "PAYMENT",
        }.get(experiment.target)
        if service_label is None:
            return None
        return f"SHOPSTACK_{service_label}_FAILURE_{experiment.setting}"

    def run(
        self, experiment: Experiment, duration_seconds: float, *, dry_run: bool = False
    ) -> ExperimentResult:
        experiment_id = str(uuid4())
        started_at = _utc_now()
        started = time.monotonic()
        environment = os.environ.copy()
        setting_name = self._setting_name(experiment)
        original_setting = environment.get(setting_name) if setting_name else None
        status: ResultStatus = "dry_run" if dry_run else "succeeded"
        error: str | None = None
        rollback_error: str | None = None

        if dry_run:
            return self._record(
                ExperimentResult(
                    experiment_id,
                    experiment.name,
                    experiment.target,
                    experiment.failure_type,
                    duration_seconds,
                    started_at,
                    _utc_now(),
                    status,
                    experiment.expected_symptoms,
                    experiment.rollback_procedure,
                    None,
                    None,
                )
            )

        try:
            if experiment.stop_target:
                self._run_command(self._command("stop", experiment.target), environment)
            else:
                if setting_name is None or experiment.value is None:
                    raise RuntimeError("experiment has no supported ShopStack injection")
                environment[setting_name] = experiment.value
                self._run_command(
                    self._command("up", experiment.target, recreate=True), environment
                )
            time.sleep(duration_seconds)
        except KeyboardInterrupt:
            status = "interrupted"
            error = "interrupted by operator"
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            status = "failed"
            error = str(exc)
        finally:
            try:
                if setting_name is not None:
                    if original_setting is None:
                        environment.pop(setting_name, None)
                    else:
                        environment[setting_name] = original_setting
                self._run_command(
                    self._command("up", experiment.target, recreate=True), environment
                )
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                status = "rollback_failed"
                rollback_error = str(exc)

        result = ExperimentResult(
            experiment_id=experiment_id,
            experiment=experiment.name,
            target=experiment.target,
            failure_type=experiment.failure_type,
            duration_seconds=round(time.monotonic() - started, 3),
            started_at=started_at,
            finished_at=_utc_now(),
            status=status,
            expected_symptoms=experiment.expected_symptoms,
            rollback_procedure=experiment.rollback_procedure,
            error=error,
            rollback_error=rollback_error,
        )
        self._record(result)
        return result

    def _record(self, result: ExperimentResult) -> ExperimentResult:
        self.results_file.parent.mkdir(parents=True, exist_ok=True)
        with self.results_file.open("a", encoding="utf-8") as output:
            output.write(json.dumps(asdict(result), sort_keys=True) + "\n")
        return result
