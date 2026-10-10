"""Reproducibly benchmark OpsMind's local incident-response path."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
from fastapi import Request
from opsmind_api.auth import Principal
from opsmind_api.main import create_app as create_api_app
from opsmind_common.config import Settings
from opsmind_detection_engine.detection import (
    DetectorSettings,
    DeterministicDetector,
    MetricObservation,
    Threshold,
)
from opsmind_events.publisher import RedisStreamPublisher
from opsmind_incident_engine.investigation import (
    AIInvestigationEngine,
    InvestigationEvidence,
    InvestigationIncident,
    InvestigationToolRegistry,
    LLMProvider,
    ProviderRequest,
    ProviderTurn,
    ToolCall,
    ToolResult,
)
from opsmind_incident_engine.investigation_tools import MetricQueryArgs
from opsmind_models.domain.remediation import RemediationActionType
from opsmind_models.events import EventEnvelope, EventPayload, OpsMindEventType
from opsmind_remediation_engine.executors import (
    ExecutionResult,
    RemediationCommand,
    VerificationResult,
)
from opsmind_remediation_engine.main import (
    RemediationPrincipal,
)
from opsmind_remediation_engine.main import (
    create_app as create_remediation_app,
)
from opsmind_storage.database import create_session_factory
from opsmind_storage.models import ServiceDependencyRow, ServiceRow
from redis import Redis
from sqlalchemy import create_engine, delete, make_url
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
REQUESTER_ID = UUID("b0000000-0000-4000-8000-000000000001")
REVIEWER_ID = UUID("b0000000-0000-4000-8000-000000000002")
SERVICE_NAME = "benchmark-payment-service"
STATES = ("TRIAGING", "INVESTIGATING", "IDENTIFIED", "MITIGATING", "RESOLVED")


def load_dotenv() -> None:
    """Load local Compose values without printing them or overriding shell values."""
    path = ROOT / ".env"
    if not path.exists():
        raise RuntimeError(".env is required; copy .env.example and set local passwords")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        os.environ.setdefault(key, value.strip().strip("\"'"))


def host_urls(settings: Settings) -> tuple[str, str]:
    """Map Compose service DNS names to the loopback-published local ports."""
    db = make_url(settings.database_url)
    redis_url = make_url(settings.redis_url)
    if db.host not in {"postgres", "localhost", "127.0.0.1"}:
        raise RuntimeError("Benchmark requires the local Compose PostgreSQL service")
    if redis_url.host not in {"redis", "localhost", "127.0.0.1"}:
        raise RuntimeError("Benchmark requires the local Compose Redis service")
    db_url = db.set(
        host="127.0.0.1", port=int(os.getenv("OPSMIND_POSTGRES_PORT", "15432"))
    ).render_as_string(hide_password=False)
    redis_host_url = redis_url.set(
        host="127.0.0.1", port=int(os.getenv("OPSMIND_REDIS_PORT", "16379"))
    ).render_as_string(hide_password=False)
    return db_url, redis_host_url


class ApiBenchmarkAuth:
    """Inject a fixed benchmark identity; this is never installed in the running API."""

    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id

    def authenticate(self, request: Request) -> Principal:
        actor = (
            REVIEWER_ID if request.headers.get("x-benchmark-actor") == "reviewer" else REQUESTER_ID
        )
        return Principal(self.tenant_id, actor, frozenset({"*"}))


class RemediationBenchmarkAuth:
    """Give the benchmark separate proposer and human approver identities."""

    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id

    def authenticate(self, request: Request) -> RemediationPrincipal:
        actor = (
            REVIEWER_ID if request.headers.get("x-benchmark-actor") == "reviewer" else REQUESTER_ID
        )
        return RemediationPrincipal(
            tenant_id=self.tenant_id,
            subject_id=actor,
            permissions=frozenset({"*"}),
            actor_type="user",
        )


class BenchmarkExecutor:
    """Deterministic no-op executor for exercising approval and execution boundaries."""

    def execute(self, command: RemediationCommand) -> ExecutionResult:
        return ExecutionResult(
            result={
                "benchmark_adapter": True,
                "operation": command.action_type.value,
                "target": command.target,
            },
            external_operation_id=f"bench-{command.action_id}",
        )

    def rollback(
        self, command: RemediationCommand, previous_result: dict[str, object]
    ) -> ExecutionResult:
        return ExecutionResult(
            result={"benchmark_adapter": True, "rolled_back": True, **previous_result}
        )

    def verify(self, command: RemediationCommand, result: ExecutionResult) -> VerificationResult:
        return VerificationResult(verified=True, details={"benchmark_adapter": True})


class BenchmarkToolBackend:
    """One measured, deterministic evidence lookup for each investigator run."""

    argument_models = {"query_metrics": MetricQueryArgs}
    descriptions = {"query_metrics": "Return labeled synthetic benchmark metric evidence."}

    def __init__(self) -> None:
        self.calls = 0
        self.handlers = {"query_metrics": self.query_metrics}

    def query_metrics(
        self, arguments: MetricQueryArgs, incident: InvestigationIncident
    ) -> ToolResult:
        self.calls += 1
        evidence_id = UUID(int=incident.incident_id.int ^ arguments.window_minutes)
        evidence = InvestigationEvidence(
            evidence_id=evidence_id,
            source="benchmark.synthetic_metrics",
            timestamp=incident.detected_at,
            service=arguments.service,
            evidence_type="metric_observation",
            summary=f"Labeled synthetic {arguments.metric} anomaly for {arguments.service}",
            confidence=1.0,
        )
        return ToolResult(evidence=[evidence], warnings=[])


class BenchmarkModel(LLMProvider):
    """Predictable provider stub; its latency is excluded from external LLM claims."""

    def complete(self, request: ProviderRequest) -> ProviderTurn:
        tool_messages = [message for message in request.messages if message.role == "tool"]
        if not tool_messages:
            progress = json.dumps(
                {
                    "hypotheses": ["Synthetic metric anomaly is under investigation."],
                    "missing_information": ["Read benchmark metric evidence."],
                }
            )
            call = ToolCall(
                id="benchmark-query-metrics",
                name="query_metrics",
                arguments=json.dumps(
                    {
                        "service": "benchmark-payment-service",
                        "metric": "http_error_rate",
                        "window_minutes": 5,
                    }
                ),
            )
            return ProviderTurn(content=progress, tool_calls=[call])
        result = ToolResult.model_validate_json(tool_messages[-1].content or "{}")
        evidence_id = result.evidence[0].evidence_id
        claim = {
            "text": "A labeled synthetic HTTP error-rate anomaly was observed.",
            "evidence_status": "supported",
            "evidence_ids": [str(evidence_id)],
            "historical_incident_ids": [],
        }
        analysis = {
            "summary": claim,
            "severity": {"level": "SEV3", "rationale": claim},
            "affected_services": [claim],
            "symptoms": [claim],
            "probable_root_cause": claim,
            "confidence": 0.9,
            "supporting_evidence": [str(evidence_id)],
            "historical_context_ids": [],
            "alternative_hypotheses": [],
            "recommended_actions": [claim],
            "missing_information": [],
            "investigation_steps": [],
        }
        return ProviderTurn(content=json.dumps(analysis), tool_calls=[])


class RequestRecorder:
    """Collect status and wall-clock latency for actual in-process HTTP API calls."""

    def __init__(self) -> None:
        self.latencies_ms: list[float] = []
        self.statuses: Counter[int] = Counter()

    async def request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        start = time.perf_counter()
        response = await client.request(method, path, **kwargs)
        self.latencies_ms.append((time.perf_counter() - start) * 1000)
        self.statuses[response.status_code] += 1
        return response


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return round(ordered[index], 3)


def summarize(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "mean_ms": round(statistics.fmean(values), 3) if values else 0.0,
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": round(max(values), 3) if values else 0.0,
    }


def summarize_counts(values: list[int]) -> dict[str, float | int]:
    """Summarize discrete tool-call counts without assigning latency units."""
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 3) if values else 0.0,
        "p50": percentile([float(value) for value in values], 0.50),
        "p95": percentile([float(value) for value in values], 0.95),
        "p99": percentile([float(value) for value in values], 0.99),
        "max": max(values) if values else 0,
    }


def detector_quality(sample_count: int = 200) -> dict[str, int | float]:
    """Measure static error-rate detector results against known synthetic labels."""
    settings = DetectorSettings(thresholds={"error_rate": Threshold(maximum=0.05)})
    true_positive = false_negative = false_positive = true_negative = 0
    for index in range(sample_count):
        actual_anomaly = index < sample_count // 2
        value = 0.20 if actual_anomaly else 0.01
        detector = DeterministicDetector(settings)
        found = (
            detector.detect_metric(
                MetricObservation(
                    service=f"quality-{index}",
                    metric="error_rate",
                    value=value,
                    observed_at=datetime.now(UTC),
                    unit="ratio",
                )
            )
            is not None
        )
        if actual_anomaly and found:
            true_positive += 1
        elif actual_anomaly:
            false_negative += 1
        elif found:
            false_positive += 1
        else:
            true_negative += 1
    negatives = false_positive + true_negative
    positives = true_positive + false_negative
    return {
        "labeled_cases": sample_count,
        "true_positive": true_positive,
        "false_negative": false_negative,
        "false_positive": false_positive,
        "true_negative": true_negative,
        "false_positive_rate": false_positive / negatives if negatives else 0.0,
        "false_negative_rate": false_negative / positives if positives else 0.0,
    }


def measure_event_throughput(redis: Redis, run_id: UUID, count: int) -> dict[str, Any]:
    stream = f"opsmind:benchmark:{run_id}:events"
    publisher = RedisStreamPublisher(cast(Any, redis), stream)
    latencies: list[float] = []
    started = time.perf_counter()
    for index in range(count):
        event = EventEnvelope[EventPayload](
            event_type=OpsMindEventType.METRIC_ANOMALY_DETECTED,
            correlation_id=f"benchmark-{run_id.hex[:24]}",
            source="opsmind-benchmark",
            aggregate_type="benchmark",
            aggregate_id=run_id,
            payload=EventPayload.model_validate({"index": index, "labeled": True}),
        )
        tick = time.perf_counter()
        publisher.publish(event)
        latencies.append((time.perf_counter() - tick) * 1000)
    elapsed = time.perf_counter() - started
    persisted_count = int(cast(Any, redis).xlen(stream))
    redis.delete(stream)
    return {
        "published": count,
        "persisted": persisted_count,
        "elapsed_seconds": round(elapsed, 4),
        "events_per_second": round(count / elapsed, 2) if elapsed else 0.0,
        "publish_latency": summarize(latencies),
    }


async def run_scenario(
    *,
    count: int,
    concurrency: int,
    mode: str,
    tenant_id: UUID,
    service_id: UUID,
    sessions: sessionmaker[Session],
    api_client: httpx.AsyncClient,
    remediation_client: httpx.AsyncClient,
    run_id: UUID,
) -> dict[str, Any]:
    recorder = RequestRecorder()
    detector_samples: list[float] = []
    creation_samples: list[float] = []
    investigation_samples: list[float] = []
    investigation_tool_calls: list[int] = []
    remediation_samples: list[float] = []
    mttr_samples: list[float] = []
    semaphore = asyncio.Semaphore(concurrency if mode == "concurrent" else 1)
    detection_settings = DetectorSettings(
        thresholds={"error_rate": Threshold(maximum=0.05)},
    )

    async def exercise(index: int) -> None:
        async with semaphore:
            incident_started = time.perf_counter()
            detected_at = datetime.now(UTC)
            detector = DeterministicDetector(detection_settings)
            detection_started = time.perf_counter()
            anomaly = detector.detect_metric(
                MetricObservation(
                    service=SERVICE_NAME,
                    metric="error_rate",
                    value=0.2,
                    observed_at=detected_at,
                    unit="ratio",
                )
            )
            detector_samples.append((time.perf_counter() - detection_started) * 1000)
            if anomaly is None:
                raise RuntimeError("Labeled anomaly was not detected")

            create_started = time.perf_counter()
            response = await recorder.request(
                api_client,
                "POST",
                "/incidents",
                headers={"Idempotency-Key": f"benchmark-{run_id}-{mode}-{count}-{index}"},
                json={
                    "title": f"Benchmark incident {run_id.hex[:8]}-{mode}-{index}",
                    "description": "Synthetic benchmark incident; no production signal "
                    "is represented.",
                    "service_id": str(service_id),
                    "severity": "SEV3",
                },
            )
            if response.status_code != 201:
                raise RuntimeError(
                    f"Incident API returned {response.status_code}: {response.text[:300]}"
                )
            creation_samples.append((time.perf_counter() - create_started) * 1000)
            incident = response.json()
            incident_id = UUID(incident["id"])
            expected_version = int(incident["version"])

            async def transition(state: str) -> None:
                nonlocal expected_version
                patch = await recorder.request(
                    api_client,
                    "PATCH",
                    f"/incidents/{incident_id}",
                    json={"expected_version": expected_version, "state": state},
                )
                if patch.status_code != 200:
                    raise RuntimeError(
                        f"Incident transition returned {patch.status_code}: {patch.text[:300]}"
                    )
                expected_version = int(patch.json()["version"])

            for state in STATES[:3]:
                await transition(state)

            backend = BenchmarkToolBackend()
            investigation = AIInvestigationEngine(
                BenchmarkModel(),
                InvestigationToolRegistry(cast(Any, backend)),
                max_tool_calls=4,
                max_execution_seconds=10,
                tool_timeout_seconds=3,
                max_context_chars=20_000,
                confidence_threshold=0.75,
            )
            context = InvestigationIncident(
                incident_id=incident_id,
                title=incident["title"],
                description=incident["description"],
                severity="SEV3",
                state="IDENTIFIED",
                service_id=service_id,
                service=SERVICE_NAME,
                environment="benchmark",
                detected_at=detected_at.isoformat(),
            )
            investigate_started = time.perf_counter()
            analysis = await asyncio.to_thread(investigation.investigate, context)
            investigation_samples.append((time.perf_counter() - investigate_started) * 1000)
            investigation_tool_calls.append(backend.calls)
            if analysis.confidence < 0.75:
                raise RuntimeError("Benchmark investigator returned insufficient confidence")

            remediation_started = time.perf_counter()
            deployment_id = uuid4()
            proposal = await recorder.request(
                remediation_client,
                "POST",
                f"/incidents/{incident_id}/remediations",
                headers={"Idempotency-Key": f"benchmark-action-{run_id}-{mode}-{count}-{index}"},
                json={
                    "action_type": RemediationActionType.ROLLBACK_DEPLOYMENT.value,
                    "target": f"benchmark-deployment-{index}",
                    "parameters": {
                        "deployment_id": str(deployment_id),
                        "target_version": "benchmark-healthy",
                    },
                    "rationale": "Measure policy-gated benchmark remediation flow.",
                },
            )
            if proposal.status_code != 201:
                raise RuntimeError(
                    f"Remediation proposal returned {proposal.status_code}: {proposal.text[:300]}"
                )
            action_id = UUID(proposal.json()["action_id"])
            approved = await recorder.request(
                remediation_client,
                "POST",
                f"/remediations/{action_id}/approve",
                headers={"x-benchmark-actor": "reviewer"},
                json={"reason": "Approved by the benchmark's separate test identity."},
            )
            if approved.status_code != 200:
                raise RuntimeError(
                    f"Remediation approval returned {approved.status_code}: {approved.text[:300]}"
                )
            executed = await recorder.request(
                remediation_client,
                "POST",
                f"/remediations/{action_id}/execute",
                headers={
                    "x-benchmark-actor": "reviewer",
                    "Idempotency-Key": f"benchmark-exec-{run_id}-{mode}-{index}",
                },
                json={"dry_run": False},
            )
            if (
                executed.status_code != 200
                or executed.json().get("execution_status") != "COMPLETED"
            ):
                raise RuntimeError(
                    f"Remediation execution failed: {executed.status_code} {executed.text[:300]}"
                )
            remediation_samples.append((time.perf_counter() - remediation_started) * 1000)

            for state in STATES[3:]:
                await transition(state)
            mttr_samples.append((time.perf_counter() - incident_started) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*(exercise(index) for index in range(count)))
    elapsed = time.perf_counter() - started
    return {
        "scenario": mode,
        "incident_count": count,
        "concurrency_limit": concurrency if mode == "concurrent" else 1,
        "elapsed_seconds": round(elapsed, 3),
        "incidents_per_second": round(count / elapsed, 3) if elapsed else 0.0,
        "anomaly_detection_latency": summarize(detector_samples),
        "incident_creation_latency": summarize(creation_samples),
        "investigation_duration": summarize(investigation_samples),
        "investigation_tool_calls": summarize_counts(investigation_tool_calls),
        "remediation_latency": summarize(remediation_samples),
        "mttr": summarize(mttr_samples),
        "api_request_count": len(recorder.latencies_ms),
        "api_throughput_requests_per_second": round(len(recorder.latencies_ms) / elapsed, 3)
        if elapsed
        else 0.0,
        "api_request_latency": summarize(recorder.latencies_ms),
        "http_status_counts": {str(key): value for key, value in sorted(recorder.statuses.items())},
    }


def cleanup_benchmark_data(sessions: sessionmaker[Session], tenant_id: UUID) -> None:
    """Delete only rows tagged with this unique benchmark tenant."""
    from opsmind_storage.models import (
        HistoricalIncidentRow,
        IncidentCreateIdempotencyRow,
        IncidentEventRow,
        IncidentEvidenceRow,
        IncidentHypothesisRow,
        IncidentRow,
        IncidentTimelineRow,
        RemediationActionRow,
        RemediationApprovalRequestRow,
        RemediationExecutionRow,
    )

    with sessions.begin() as session:
        session.execute(
            delete(HistoricalIncidentRow).where(HistoricalIncidentRow.tenant_id == tenant_id)
        )
        session.execute(
            delete(RemediationExecutionRow).where(RemediationExecutionRow.tenant_id == tenant_id)
        )
        session.execute(
            delete(RemediationApprovalRequestRow).where(
                RemediationApprovalRequestRow.tenant_id == tenant_id
            )
        )
        session.execute(
            delete(RemediationActionRow).where(RemediationActionRow.tenant_id == tenant_id)
        )
        session.execute(
            delete(IncidentCreateIdempotencyRow).where(
                IncidentCreateIdempotencyRow.tenant_id == tenant_id
            )
        )
        session.execute(
            delete(IncidentTimelineRow).where(IncidentTimelineRow.tenant_id == tenant_id)
        )
        session.execute(
            delete(IncidentHypothesisRow).where(IncidentHypothesisRow.tenant_id == tenant_id)
        )
        session.execute(
            delete(IncidentEvidenceRow).where(IncidentEvidenceRow.tenant_id == tenant_id)
        )
        session.execute(delete(IncidentEventRow).where(IncidentEventRow.tenant_id == tenant_id))
        session.execute(delete(IncidentRow).where(IncidentRow.tenant_id == tenant_id))
        session.execute(
            delete(ServiceDependencyRow).where(ServiceDependencyRow.tenant_id == tenant_id)
        )
        session.execute(delete(ServiceRow).where(ServiceRow.tenant_id == tenant_id))


async def benchmark(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv()
    settings = Settings()
    db_url, redis_url = host_urls(settings)
    run_id = uuid4()
    tenant_id = uuid4()
    engine: Engine = create_engine(
        db_url,
        pool_size=max(5, args.concurrency),
        max_overflow=max(5, args.concurrency),
        pool_timeout=30,
        pool_pre_ping=True,
    )
    sessions = create_session_factory(engine)
    redis = Redis.from_url(redis_url, decode_responses=True)
    service_id = uuid4()
    try:
        with sessions.begin() as session:
            session.add(
                ServiceRow(
                    id=service_id,
                    tenant_id=tenant_id,
                    name=SERVICE_NAME,
                    environment="benchmark",
                    owner_team="OpsMind Benchmark",
                    metadata_json={"benchmark_managed": True},
                )
            )
        api_app = create_api_app(
            session_factory=sessions,
            authentication_backend=ApiBenchmarkAuth(tenant_id),
            settings=settings,
        )
        remediation_app = create_remediation_app(
            settings=settings,
            session_factory=sessions,
            authentication_backend=RemediationBenchmarkAuth(tenant_id),
            executor=BenchmarkExecutor(),
        )
        api_transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
        remediation_transport = httpx.ASGITransport(app=remediation_app, raise_app_exceptions=False)
        async with (
            httpx.AsyncClient(transport=api_transport, base_url="http://opsmind-api") as api_client,
            httpx.AsyncClient(
                transport=remediation_transport, base_url="http://opsmind-remediation"
            ) as remediation_client,
        ):
            scenarios: list[dict[str, Any]] = []
            for count in args.sizes:
                scenarios.append(
                    await run_scenario(
                        count=count,
                        concurrency=args.concurrency,
                        mode="sequential",
                        tenant_id=tenant_id,
                        service_id=service_id,
                        sessions=sessions,
                        api_client=api_client,
                        remediation_client=remediation_client,
                        run_id=run_id,
                    )
                )
            maximum = max(args.sizes)
            scenarios.append(
                await run_scenario(
                    count=maximum,
                    concurrency=args.concurrency,
                    mode="concurrent",
                    tenant_id=tenant_id,
                    service_id=service_id,
                    sessions=sessions,
                    api_client=api_client,
                    remediation_client=remediation_client,
                    run_id=run_id,
                )
            )
        event_runs = [measure_event_throughput(redis, run_id, count) for count in args.sizes]
        return {
            "benchmark_run_id": str(run_id),
            "started_at": datetime.now(UTC).isoformat(),
            "host": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "database": "local PostgreSQL via Compose TCP port",
                "event_store": "local Redis Streams via Compose TCP port",
                "api_mode": "in-process ASGI transport; no TCP proxy or load balancer",
                "investigation_provider": "deterministic local provider stub",
                "remediation_executor": "deterministic no-op benchmark adapter",
            },
            "scenarios": scenarios,
            "event_throughput": event_runs,
            "detector_quality": detector_quality(),
            "notes": [
                "All incidents and labels are synthetic and isolated under a unique benchmark "
                "tenant.",
                "Synthetic tenant data and Redis benchmark streams are deleted after each run.",
                "Append-only audit records remain as a benchmark audit trail under the "
                "temporary tenant.",
                "False-positive and false-negative rates describe only the labeled synthetic set.",
                "Investigation timings use one deterministic evidence tool and local provider "
                "stub.",
                "Remediation timings use real policy, approval, and execution endpoints with a "
                "no-op executor.",
                "MTTR spans incident creation through RESOLVED, including the stub investigation "
                "and remediation flow.",
            ],
        }
    finally:
        cleanup_benchmark_data(sessions, tenant_id)
        redis.close()
        engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        default="10,100",
        help="incident counts for sequential scenarios; the largest count also runs concurrently",
    )
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    try:
        args.sizes = [int(value) for value in args.sizes.split(",")]
    except ValueError as exc:
        parser.error(f"invalid --sizes: {exc}")
    if not args.sizes or any(value < 1 for value in args.sizes):
        parser.error("--sizes must contain positive incident counts")
    if args.concurrency < 2:
        parser.error("--concurrency must be at least 2")
    return args


def main() -> None:
    args = parse_args()
    result = asyncio.run(benchmark(args))
    default_output = ROOT / "docs" / "benchmark-results" / f"{result['benchmark_run_id']}.json"
    output = args.output or default_output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"Raw results: {output}")


if __name__ == "__main__":
    main()
