"""Run the local end-to-end incident scenario with explicit integration simulators."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
from opsmind_common.config import Settings
from opsmind_detection_engine.detection import (
    DetectorSettings,
    DeterministicDetector,
    IncidentCorrelator,
    Threshold,
)
from opsmind_detection_engine.runner import DetectionRunner
from opsmind_detection_engine.sources import LokiSource, PrometheusSource
from opsmind_events.publisher import RedisStreamPublisher
from opsmind_incident_engine.correlation import (
    CorrelationWindow,
    DatabaseEvidenceSource,
    IncidentContext,
    IncidentCorrelationEngine,
)
from opsmind_incident_engine.investigation import (
    AIInvestigationEngine,
    IncidentAnalysis,
    InvestigationIncident,
    LLMProvider,
    ProviderRequest,
    ProviderTurn,
    ToolCall,
)
from opsmind_incident_engine.investigation_store import InvestigationRunStore
from opsmind_incident_engine.investigation_tools import DatabaseInvestigationTools
from opsmind_incident_engine.sources import LokiEvidenceSource, PrometheusEvidenceSource
from opsmind_models.domain.incidents import VALID_INCIDENT_TRANSITIONS, IncidentState
from opsmind_models.domain.remediation import RemediationActionType
from opsmind_models.events import CorrelatedIncidentPayload, EventEnvelope, OpsMindEventType
from opsmind_remediation_engine.policy import RemediationPolicyEngine
from opsmind_storage.database import create_session_factory
from opsmind_storage.models import (
    AuditLogRow,
    DeploymentRow,
    IncidentEvidenceRow,
    IncidentHypothesisRow,
    IncidentRow,
    IncidentTimelineRow,
    NotificationRow,
    RemediationActionRow,
    RemediationApprovalRequestRow,
    RemediationExecutionRow,
    ServiceDependencyRow,
    ServiceRow,
)
from redis import Redis
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

TENANT_ID = UUID("a0000000-0000-4000-8000-000000000001")
DEMO_ACTOR_ID = UUID("a0000000-0000-4000-8000-000000000002")
DEMO_ROOT = Path.cwd()
PAYMENT_SERVICE = "payment-service"
ORDER_SERVICE = "order-service"
PAYMENT_COMPOSE_SERVICE = "shopstack-payment-service"
BASE_URLS = {
    "gateway": "http://127.0.0.1:8080",
    "payment": "http://127.0.0.1:8104",
    "prometheus": "http://127.0.0.1:9090",
    "loki": "http://127.0.0.1:13100",
}


def _say(message: str) -> None:
    print(f"\n[OpsMind Demo] {message}", flush=True)


def _load_local_environment(root: Path) -> None:
    """Load local Compose values for host-side read access without printing secrets."""
    dotenv = root / ".env"
    if not dotenv.exists():
        raise RuntimeError(".env is missing; copy .env.example to .env first")
    for raw_line in dotenv.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


def _host_database_url(settings: Settings) -> str:
    url = make_url(settings.database_url)
    if url.host in {"postgres", "localhost", "127.0.0.1"}:
        return url.set(
            host="127.0.0.1",
            port=int(os.getenv("OPSMIND_POSTGRES_PORT", "15432")),
        ).render_as_string(hide_password=False)
    raise RuntimeError("Demo only supports the local Compose PostgreSQL service")


def _host_redis_url(settings: Settings) -> str:
    url = make_url(settings.redis_url)
    if url.host in {"redis", "localhost", "127.0.0.1"}:
        return url.set(
            host="127.0.0.1",
            port=int(os.getenv("OPSMIND_REDIS_PORT", "16379")),
        ).render_as_string(hide_password=False)
    raise RuntimeError("Demo only supports the local Compose Redis service")


def _compose(
    root: Path,
    args: list[str],
    *,
    environment: dict[str, str] | None = None,
    timeout: float = 900,
) -> str:
    command = [
        "docker",
        "compose",
        "--profile",
        "shopstack",
        "--profile",
        "observability",
        *args,
    ]
    completed = subprocess.run(
        command,
        cwd=root,
        env=environment or os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode:
        message = (completed.stderr or completed.stdout).strip()[-1600:]
        raise RuntimeError(f"Compose command failed ({completed.returncode}): {message}")
    return completed.stdout


def _wait_http(url: str, *, timeout: float = 90, expected: int = 200) -> None:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=2)
            if response.status_code == expected:
                return
            last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = type(exc).__name__
        time.sleep(2)
    raise RuntimeError(f"Readiness timed out for {url} ({last_error})")


def _audit(
    session: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: UUID,
    correlation_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLogRow(
            tenant_id=TENANT_ID,
            actor_type="local_demo_user",
            actor_id=DEMO_ACTOR_ID,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            outcome="success",
            correlation_id=correlation_id,
            before_state=before,
            after_state=after,
        )
    )


def _append_timeline(
    session: Session,
    *,
    incident_id: UUID,
    event_type: str,
    summary: str,
    occurred_at: datetime | None = None,
) -> None:
    sequence = (
        int(
            session.scalar(
                select(IncidentTimelineRow.sequence)
                .where(
                    IncidentTimelineRow.tenant_id == TENANT_ID,
                    IncidentTimelineRow.incident_id == incident_id,
                )
                .order_by(IncidentTimelineRow.sequence.desc())
                .limit(1)
            )
            or 0
        )
        + 1
    )
    session.add(
        IncidentTimelineRow(
            tenant_id=TENANT_ID,
            incident_id=incident_id,
            sequence=sequence,
            event_type=event_type,
            summary=summary,
            occurred_at=occurred_at or datetime.now(UTC),
        )
    )


def _ensure_registry(session: Session) -> dict[str, ServiceRow]:
    names = ["api-gateway", "user-service", ORDER_SERVICE, "inventory-service", PAYMENT_SERVICE]
    rows: dict[str, ServiceRow] = {}
    for name in names:
        row = session.scalar(
            select(ServiceRow).where(
                ServiceRow.tenant_id == TENANT_ID,
                ServiceRow.environment == "demo",
                ServiceRow.name == name,
            )
        )
        if row is None:
            row = ServiceRow(
                tenant_id=TENANT_ID,
                name=name,
                environment="demo",
                owner_team="OpsMind Demo",
                metadata_json={"demo_managed": True},
            )
            session.add(row)
            session.flush()
        rows[name] = row
    edges = (
        ("api-gateway", ORDER_SERVICE),
        (ORDER_SERVICE, "inventory-service"),
        (ORDER_SERVICE, PAYMENT_SERVICE),
        (PAYMENT_SERVICE, "postgresql"),
    )
    if "postgresql" not in rows:
        db_service = session.scalar(
            select(ServiceRow).where(
                ServiceRow.tenant_id == TENANT_ID,
                ServiceRow.environment == "demo",
                ServiceRow.name == "postgresql",
            )
        )
        if db_service is None:
            db_service = ServiceRow(
                tenant_id=TENANT_ID,
                name="postgresql",
                environment="demo",
                owner_team="OpsMind Demo",
                metadata_json={"demo_managed": True, "kind": "database"},
            )
            session.add(db_service)
            session.flush()
        rows["postgresql"] = db_service
    for source, target in edges:
        exists = session.scalar(
            select(ServiceDependencyRow.id).where(
                ServiceDependencyRow.tenant_id == TENANT_ID,
                ServiceDependencyRow.source_service_id == rows[source].id,
                ServiceDependencyRow.target_service_id == rows[target].id,
            )
        )
        if exists is None:
            session.add(
                ServiceDependencyRow(
                    tenant_id=TENANT_ID,
                    source_service_id=rows[source].id,
                    target_service_id=rows[target].id,
                    dependency_type="runtime",
                )
            )
    return rows


def _fault_environment(enabled: bool) -> dict[str, str]:
    result = os.environ.copy()
    if enabled:
        result["SHOPSTACK_PAYMENT_FAILURE_LATENCY_MS"] = "1250"
        result["SHOPSTACK_PAYMENT_FAILURE_HTTP_500_RATE"] = "0.20"
    else:
        result["SHOPSTACK_PAYMENT_FAILURE_LATENCY_MS"] = "0"
        result["SHOPSTACK_PAYMENT_FAILURE_HTTP_500_RATE"] = "0"
    return result


def _run_traffic(root: Path, requests: int) -> str:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/shopstack_traffic.py",
            "--requests",
            str(requests),
            "--concurrency",
            "12",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
    )
    if completed.returncode:
        raise RuntimeError(
            f"Traffic generation failed: {(completed.stderr or completed.stdout)[-1000:]}"
        )
    return completed.stdout.strip()


def _latest_stream_id(redis: Redis) -> str:
    rows = cast(Any, redis).xrevrange("opsmind:events", count=1)
    return str(rows[0][0]) if rows else "0-0"


def _read_incident_event(
    redis: Redis, cursor: str
) -> EventEnvelope[CorrelatedIncidentPayload] | None:
    rows = cast(Any, redis).xrange("opsmind:events", min=f"({cursor}")
    for _, fields in rows:
        event_json = fields.get("event")
        if not isinstance(event_json, str):
            continue
        try:
            event = EventEnvelope[CorrelatedIncidentPayload].model_validate_json(event_json)
        except ValueError:
            continue
        if (
            event.event_type == OpsMindEventType.INCIDENT_CREATED
            and event.payload.service == PAYMENT_SERVICE
        ):
            return event
    return None


def _detector_settings(settings: Settings) -> DetectorSettings:
    return DetectorSettings(
        thresholds={
            "error_rate": Threshold(maximum=settings.detection_error_rate_threshold),
            "dependency_error_rate": Threshold(
                maximum=settings.detection_dependency_error_rate_threshold
            ),
            "latency_p95": Threshold(maximum=settings.detection_latency_p95_threshold_seconds),
        },
        baseline_window=settings.detection_baseline_window,
        baseline_min_samples=min(
            settings.detection_baseline_min_samples, settings.detection_baseline_window
        ),
        baseline_deviation_ratio=settings.detection_baseline_deviation_ratio,
        z_score_threshold=settings.detection_z_score_threshold,
        traffic_drop_ratio=settings.detection_traffic_drop_ratio,
        log_window_seconds=settings.detection_log_window_seconds,
        log_occurrence_threshold=settings.detection_log_occurrence_threshold,
        correlation_window_seconds=settings.detection_correlation_window_seconds,
        deduplication_ttl_seconds=settings.detection_deduplication_ttl_seconds,
    )


class DemoDeduplicationStore:
    """Use per-run dedupe keys while publishing demo events to the shared stream."""

    def __init__(self, redis: Redis, run_id: UUID) -> None:
        self.redis = cast(Any, redis)
        self.prefix = f"opsmind:demo:{run_id}:dedupe:"

    def set(self, name: str, value: str, *, nx: bool, ex: int) -> bool | None:
        result = self.redis.set(f"{self.prefix}{name}", value, nx=nx, ex=ex)
        return cast(bool | None, result)

    def delete(self, name: str) -> int:
        return int(self.redis.delete(f"{self.prefix}{name}"))


class DemoRedisStreamPublisher(RedisStreamPublisher):
    """Namespace incident signatures so each isolated demo run has its own record."""

    def __init__(self, redis: Any, stream: str, run_id: UUID) -> None:
        super().__init__(redis, stream)
        self._run_id = run_id

    def publish[PayloadT: Any](self, event: EventEnvelope[PayloadT]) -> str:
        if event.event_type == OpsMindEventType.INCIDENT_CREATED and isinstance(
            event.payload, CorrelatedIncidentPayload
        ):
            payload = event.payload.model_copy(
                update={"signature": f"demo:{self._run_id}:{event.payload.signature}"}
            )
            event = event.model_copy(update={"payload": payload})
        return super().publish(event)


def _wait_for_detection(
    *,
    redis: Redis,
    settings: Settings,
    cursor: str,
    timeout_seconds: int = 150,
) -> tuple[EventEnvelope[CorrelatedIncidentPayload], list[str]]:
    client = httpx.Client(timeout=8)
    redis_streams = cast(Any, redis)
    detector_settings = _detector_settings(settings)
    run_id = uuid4()
    runner = DetectionRunner(
        PrometheusSource(
            client,
            BASE_URLS["prometheus"],
            job_regex=settings.detection_prometheus_job_regex,
        ),
        LokiSource(client, BASE_URLS["loki"]),
        DeterministicDetector(detector_settings),
        IncidentCorrelator(detector_settings),
        DemoRedisStreamPublisher(redis_streams, "opsmind:events", run_id),
        DemoDeduplicationStore(redis, run_id),
    )
    signals: list[str] = []
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            anomalies, incidents = runner.run_once()
            signals.append(f"published anomalies={anomalies}, incidents={incidents}")
            event = _read_incident_event(redis, cursor)
            if event is not None:
                return event, signals
            time.sleep(5)
    finally:
        client.close()
    raise RuntimeError("Detection Engine did not correlate a payment incident within 150 seconds")


def _wait_for_incident_projection(
    sessions: Any,
    event: EventEnvelope[CorrelatedIncidentPayload],
) -> IncidentRow:
    """Wait for the normal Redis worker to project a detection into PostgreSQL."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with sessions() as session:
            row = session.scalar(
                select(IncidentRow).where(
                    IncidentRow.tenant_id == TENANT_ID,
                    IncidentRow.id == event.payload.incident_id,
                )
            )
            if row is not None:
                return cast(IncidentRow, row)
        time.sleep(0.25)
    raise RuntimeError(
        "The event worker did not persist the detected incident within 30 seconds; "
        "inspect worker logs and the opsmind:events consumer group"
    )


class LocalDemoModel(LLMProvider):
    """Deterministic local model simulator that calls only registered evidence tools."""

    def complete(self, request: ProviderRequest) -> ProviderTurn:
        called = {call.name for message in request.messages for call in message.tool_calls}
        if "query_metrics" not in called:
            return self._tool_turn(
                request,
                [
                    (
                        "query_metrics",
                        {
                            "service": PAYMENT_SERVICE,
                            "metric": "http_error_rate",
                            "window_minutes": 5,
                        },
                    ),
                    (
                        "query_metrics",
                        {
                            "service": PAYMENT_SERVICE,
                            "metric": "request_latency_p95_seconds",
                            "window_minutes": 5,
                        },
                    ),
                ],
                "Check current Payment Service error rate and latency.",
            )
        if "search_logs" not in called:
            return self._tool_turn(
                request,
                [
                    (
                        "search_logs",
                        {
                            "service": PAYMENT_SERVICE,
                            "query": "injected_http_500",
                            "window_minutes": 10,
                            "limit": 100,
                        },
                    )
                ],
                "Metrics are elevated; inspect current Payment Service error logs.",
            )
        if "get_recent_deployments" not in called:
            return self._tool_turn(
                request,
                [
                    (
                        "get_recent_deployments",
                        {"service": PAYMENT_SERVICE, "lookback_hours": 1, "limit": 20},
                    )
                ],
                "Compare symptom onset with recent deployment history.",
            )
        evidence: dict[UUID, dict[str, Any]] = {}
        for message in request.messages:
            if message.role != "tool" or not message.content:
                continue
            try:
                result = json.loads(message.content)
            except ValueError:
                continue
            for item in result.get("evidence", []):
                try:
                    evidence[UUID(str(item["evidence_id"]))] = item
                except (KeyError, ValueError, TypeError):
                    continue
        metric_ids = [
            evidence_id
            for evidence_id, item in evidence.items()
            if item.get("source") == "prometheus"
        ]
        deployment_ids = [
            evidence_id
            for evidence_id, item in evidence.items()
            if item.get("source") == "opsmind.deployments"
            and "demo-bug-" in str(item.get("summary", ""))
        ]
        log_ids = [
            evidence_id for evidence_id, item in evidence.items() if item.get("source") == "loki"
        ]
        if not metric_ids or not deployment_ids:
            raise RuntimeError(
                "Local investigation did not collect current metric and faulty deployment evidence"
            )
        supporting = list(evidence)
        root_ids = list(dict.fromkeys(deployment_ids + metric_ids + log_ids))
        deployment_summary = str(evidence[deployment_ids[0]]["summary"])
        faulty_version = deployment_summary.removeprefix("Deployment ").split(
            " started with status", maxsplit=1
        )[0]
        cause = self._claim(
            f"Deployment {faulty_version} is the leading cause of the elevated latency "
            "and intermittent HTTP 500 responses.",
            root_ids,
        )
        analysis = {
            "summary": self._claim(
                "Payment Service is degraded following a recent release; current metrics "
                "and deployment history support the release as the leading cause.",
                root_ids,
            ),
            "severity": {
                "level": "SEV2",
                "rationale": self._claim(
                    "The payment path is returning server errors and elevated latency.", metric_ids
                ),
            },
            "affected_services": [self._claim("Payment Service is directly affected.", metric_ids)],
            "symptoms": [
                self._claim("Payment Service HTTP error rate is elevated.", metric_ids),
                self._claim("Payment Service p95 request latency is elevated.", metric_ids),
            ],
            "probable_root_cause": cause,
            "confidence": 0.92 if log_ids else 0.86,
            "supporting_evidence": [str(value) for value in supporting],
            "historical_context_ids": [],
            "alternative_hypotheses": [],
            "recommended_actions": [
                self._claim(
                    "Roll back the faulty Payment Service deployment to the recorded healthy "
                    "revision after explicit human approval.",
                    deployment_ids,
                )
            ],
            "missing_information": []
            if log_ids
            else [
                {
                    "question": "Were application error logs delivered to Loki?",
                    "reason": "The log query returned no current error records; metrics and "
                    "deployment evidence remain available.",
                    "related_evidence_ids": [str(value) for value in metric_ids],
                }
            ],
            "investigation_steps": [],
        }
        return ProviderTurn(content=json.dumps(analysis), tool_calls=[])

    @staticmethod
    def _tool_turn(
        request: ProviderRequest,
        calls: list[tuple[str, dict[str, object]]],
        hypothesis: str,
    ) -> ProviderTurn:
        return ProviderTurn(
            content=json.dumps(
                {
                    "hypotheses": [hypothesis],
                    "missing_information": ["query requested current evidence"],
                }
            ),
            tool_calls=[
                ToolCall(
                    id=f"local-demo-{len(request.messages)}-{index}",
                    name=name,
                    arguments=json.dumps(arguments),
                )
                for index, (name, arguments) in enumerate(calls)
            ],
        )

    @staticmethod
    def _claim(text: str, evidence_ids: list[UUID]) -> dict[str, object]:
        return {
            "text": text,
            "evidence_status": "supported",
            "evidence_ids": [str(value) for value in evidence_ids],
            "historical_incident_ids": [],
        }


def _persist_correlation(
    sessions: Any,
    client: httpx.Client,
    incident: IncidentRow,
    service: ServiceRow,
) -> list[UUID]:
    context = IncidentContext(
        incident_id=incident.id,
        tenant_id=TENANT_ID,
        service=PAYMENT_SERVICE,
        environment="demo",
        detected_at=incident.detected_at.replace(tzinfo=UTC)
        if incident.detected_at.tzinfo is None
        else incident.detected_at,
        dependencies=(ORDER_SERVICE,),
    )
    correlation = IncidentCorrelationEngine(
        sources=(
            DatabaseEvidenceSource(sessions),
            PrometheusEvidenceSource(client, BASE_URLS["prometheus"]),
            LokiEvidenceSource(client, BASE_URLS["loki"]),
        ),
        window=CorrelationWindow(before_seconds=600, after_seconds=0),
    )
    result = correlation.collect(context)
    with sessions() as session, session.begin():
        correlation.persist(session, context, result)
        session.flush()
        evidence_ids = list(
            session.scalars(
                select(IncidentEvidenceRow.id).where(
                    IncidentEvidenceRow.tenant_id == TENANT_ID,
                    IncidentEvidenceRow.incident_id == incident.id,
                )
            ).all()
        )
    if not evidence_ids:
        raise RuntimeError("Incident correlation did not persist evidence")
    return evidence_ids


def _investigate(
    sessions: Any,
    client: httpx.Client,
    settings: Settings,
    incident_row: IncidentRow,
    service: ServiceRow,
) -> tuple[IncidentAnalysis, UUID]:
    incident = InvestigationIncident(
        incident_id=incident_row.id,
        title=incident_row.title,
        description=incident_row.description,
        severity=cast(Any, incident_row.severity),
        state=incident_row.state,
        service_id=service.id,
        service=PAYMENT_SERVICE,
        environment="demo",
        detected_at=incident_row.detected_at.replace(tzinfo=UTC).isoformat()
        if incident_row.detected_at.tzinfo is None
        else incident_row.detected_at.isoformat(),
    )
    tools = DatabaseInvestigationTools(
        sessions,
        client,
        prometheus_url=BASE_URLS["prometheus"],
        loki_url=BASE_URLS["loki"],
        after_seconds=0,
    )
    run_id = uuid4()
    store = InvestigationRunStore(
        sessions,
        run_id=run_id,
        tenant_id=TENANT_ID,
        incident_id=incident_row.id,
        correlation_id=str(incident_row.id),
    )
    store.create()
    engine = AIInvestigationEngine(
        LocalDemoModel(),
        tools.registry,
        max_tool_calls=12,
        max_execution_seconds=60,
        tool_timeout_seconds=10,
        max_context_chars=settings.investigation_max_context_chars,
        confidence_threshold=0.75,
        observer=store,
    )
    try:
        analysis = engine.investigate(incident)
        store.finish("COMPLETED")
    except Exception:
        store.finish("FAILED")
        raise
    return analysis, run_id


def _record_remediation_proposal(
    sessions: Any,
    *,
    incident_id: UUID,
    deployment_id: UUID,
    target_version: str,
    correlation_id: str,
) -> tuple[UUID, UUID, dict[str, object]]:
    action_id = uuid4()
    approval_id = uuid4()
    parameters: dict[str, object] = {
        "deployment_id": deployment_id,
        "target_version": target_version,
    }
    policy = RemediationPolicyEngine()
    validated = policy.validate(
        RemediationActionType.ROLLBACK_DEPLOYMENT.value,
        PAYMENT_COMPOSE_SERVICE,
        parameters,
    )
    now = datetime.now(UTC)
    rationale = (
        "Rollback the faulty payment deployment after human approval in the local Slack simulator."
    )
    with sessions.begin() as session:
        action = RemediationActionRow(
            id=action_id,
            tenant_id=TENANT_ID,
            incident_id=incident_id,
            action_type=validated.action_type.value,
            target=PAYMENT_COMPOSE_SERVICE,
            parameters=validated.parameters.model_dump(mode="json"),
            risk_level=validated.risk_level.value.removesuffix("_RISK").lower(),
            status="approval_requested",
            idempotency_key=f"local-demo-{action_id}",
            action_digest=validated.digest,
            requested_by=DEMO_ACTOR_ID,
            rationale=rationale,
            expires_at=now + timedelta(minutes=15),
        )
        session.add(action)
        session.flush()
        approval = RemediationApprovalRequestRow(
            id=approval_id,
            tenant_id=TENANT_ID,
            action_id=action_id,
            action_type=action.action_type,
            target=action.target,
            risk_level=action.risk_level,
            request_reason=rationale,
            expires_at=action.expires_at,
            decision="requested",
        )
        session.add(approval)
        _audit(
            session,
            action="remediation.policy_evaluated",
            entity_type="remediation_action",
            entity_id=action_id,
            correlation_id=correlation_id,
            after={"allowed": True, "risk": action.risk_level, "policy_digest": validated.digest},
        )
        _audit(
            session,
            action="remediation.approval_requested",
            entity_type="remediation_action",
            entity_id=action_id,
            correlation_id=correlation_id,
            after={
                "approval_id": str(approval_id),
                "target": action.target,
                "risk": action.risk_level,
            },
        )
    return action_id, approval_id, parameters


def _request_simulated_slack_approval(
    sessions: Any,
    *,
    incident_id: UUID,
    action_id: UUID,
    approval_id: UUID,
    correlation_id: str,
) -> bool:
    _say("Slack simulator: approval requested for rollback_deployment")
    print(f"Incident: {incident_id}\nAction: rollback {PAYMENT_COMPOSE_SERVICE}\nRisk: MEDIUM_RISK")
    print("A local Slack-style approval request is recorded in notifications.")
    with sessions.begin() as session:
        session.add(
            NotificationRow(
                tenant_id=TENANT_ID,
                incident_id=incident_id,
                channel="slack-simulator",
                recipient="local-demo-responder",
                idempotency_key=f"slack-simulator:{action_id}",
                status="simulated_delivered",
                payload={
                    "type": "remediation_approval_request",
                    "action_id": str(action_id),
                    "approval_id": str(approval_id),
                    "target": PAYMENT_COMPOSE_SERVICE,
                    "risk": "MEDIUM_RISK",
                },
                delivered_at=datetime.now(UTC),
            )
        )
        _audit(
            session,
            action="slack.approval_request_simulated",
            entity_type="remediation_action",
            entity_id=action_id,
            correlation_id=correlation_id,
            after={"channel": "slack-simulator", "approval_id": str(approval_id)},
        )
    try:
        answer = input("Approve this rollback? [y/N] ").strip().casefold()
    except EOFError:
        answer = ""
    approved = answer in {"y", "yes"}
    reason = "Approved by local demo responder" if approved else "Rejected by local demo responder"
    now = datetime.now(UTC)
    with sessions.begin() as session:
        approval = session.get(RemediationApprovalRequestRow, approval_id)
        action = session.get(RemediationActionRow, action_id)
        if approval is None or action is None or approval.decision != "requested":
            raise RuntimeError("Demo approval request is missing or already decided")
        approval.decision = "approved" if approved else "rejected"
        approval.approver_id = DEMO_ACTOR_ID
        approval.decision_at = now
        approval.decision_reason = reason
        action.status = "approved" if approved else "rejected"
        if approved:
            action.approved_by = DEMO_ACTOR_ID
            action.approved_at = now
        _audit(
            session,
            action="remediation.approved" if approved else "remediation.rejected",
            entity_type="remediation_action",
            entity_id=action_id,
            correlation_id=correlation_id,
            after={"approver": str(DEMO_ACTOR_ID), "reason": reason, "timestamp": now.isoformat()},
        )
    return approved


class LocalKubernetesRollbackSimulator:
    """Apply the deployment rollback to Compose while marking it as a Kubernetes simulation."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def execute_and_verify(self) -> dict[str, object]:
        _compose(
            self.root,
            ["up", "-d", "--force-recreate", PAYMENT_COMPOSE_SERVICE],
            environment=_fault_environment(False),
        )
        _wait_http(f"{BASE_URLS['payment']}/ready", timeout=120)
        return {
            "adapter": "local-kubernetes-simulator",
            "operation": "rollback_deployment",
            "deployment": PAYMENT_COMPOSE_SERVICE,
            "target_version": "healthy Compose configuration",
            "verified": True,
            "health_endpoint": f"{BASE_URLS['payment']}/ready",
        }


def _execute_remediation(
    sessions: Any,
    *,
    action_id: UUID,
    incident_id: UUID,
    correlation_id: str,
    root: Path,
) -> dict[str, object]:
    with sessions.begin() as session:
        action = session.get(RemediationActionRow, action_id)
        if action is None or action.status != "approved":
            raise RuntimeError("Remediation execution requires an approved action")
        action.status = "running"
        action.execution_start = datetime.now(UTC)
        execution = RemediationExecutionRow(
            id=uuid4(),
            tenant_id=TENANT_ID,
            action_id=action_id,
            idempotency_key=f"local-demo-execute:{action_id}",
            status="RUNNING",
            request_payload={"dry_run": False, "simulated_kubernetes": True},
            started_at=action.execution_start,
        )
        session.add(execution)
        _audit(
            session,
            action="remediation.execution_started",
            entity_type="remediation_action",
            entity_id=action_id,
            correlation_id=correlation_id,
            after={"execution_id": str(execution.id)},
        )
    result = LocalKubernetesRollbackSimulator(root).execute_and_verify()
    now = datetime.now(UTC)
    with sessions.begin() as session:
        action = session.get(RemediationActionRow, action_id)
        execution = session.scalar(
            select(RemediationExecutionRow).where(
                RemediationExecutionRow.action_id == action_id,
                RemediationExecutionRow.idempotency_key == f"local-demo-execute:{action_id}",
            )
        )
        if action is None or execution is None:
            raise RuntimeError("Remediation record disappeared during simulated execution")
        action.status = "succeeded"
        action.execution_end = now
        action.result = result
        execution.status = "SUCCEEDED"
        execution.result_payload = result
        execution.completed_at = now
        _audit(
            session,
            action="remediation.execution_completed",
            entity_type="remediation_action",
            entity_id=action_id,
            correlation_id=correlation_id,
            after=result,
        )
    _say("Kubernetes rollback simulator executed; ShopStack health verification passed.")
    return result


def _transition_incident(
    sessions: Any,
    *,
    incident_id: UUID,
    target: IncidentState,
    correlation_id: str,
) -> None:
    with sessions.begin() as session:
        row = session.get(IncidentRow, incident_id)
        if row is None:
            raise RuntimeError("Incident disappeared before lifecycle transition")
        current = IncidentState(row.state)
        if target not in VALID_INCIDENT_TRANSITIONS[current]:
            raise RuntimeError(f"Invalid demo transition {current} -> {target}")
        before = {"state": row.state, "version": row.version}
        row.state = target.value
        row.version += 1
        if target is IncidentState.RESOLVED:
            row.resolved_at = datetime.now(UTC)
        _append_timeline(
            session,
            incident_id=incident_id,
            event_type=f"incident.{target.value.casefold()}",
            summary=f"Incident transitioned to {target.value}",
        )
        _audit(
            session,
            action="incident.state_transitioned",
            entity_type="incident",
            entity_id=incident_id,
            correlation_id=correlation_id,
            before=before,
            after={"state": target.value, "version": row.version},
        )


def _write_report(
    sessions: Any,
    *,
    incident_id: UUID,
    analysis: IncidentAnalysis,
    action_id: UUID,
    remediation_result: dict[str, object] | None,
    recovery_seconds: float | None,
    root: Path,
    step_status: list[tuple[str, str]],
) -> Path:
    with sessions() as session:
        incident = session.get(IncidentRow, incident_id)
        action = session.get(RemediationActionRow, action_id)
        evidence = session.scalars(
            select(IncidentEvidenceRow)
            .where(
                IncidentEvidenceRow.tenant_id == TENANT_ID,
                IncidentEvidenceRow.incident_id == incident_id,
            )
            .order_by(IncidentEvidenceRow.observed_at, IncidentEvidenceRow.id)
        ).all()
        timeline = session.scalars(
            select(IncidentTimelineRow)
            .where(
                IncidentTimelineRow.tenant_id == TENANT_ID,
                IncidentTimelineRow.incident_id == incident_id,
            )
            .order_by(IncidentTimelineRow.sequence)
        ).all()
        audit = session.scalars(
            select(AuditLogRow)
            .where(
                AuditLogRow.tenant_id == TENANT_ID,
                AuditLogRow.entity_id.in_([incident_id, action_id]),
            )
            .order_by(AuditLogRow.created_at)
        ).all()
    if incident is None or action is None:
        raise RuntimeError("Could not build report: persisted incident or remediation missing")
    lines = [
        "# OpsMind end-to-end demo report",
        "",
        f"- Incident ID: `{incident_id}`",
        f"- Final status: **{incident.state}**",
        f"- Severity: **{incident.severity}**",
        f"- Recovery time: **{recovery_seconds:.1f} seconds**"
        if recovery_seconds is not None
        else "- Recovery time: **not recovered**",
        "- Services affected: Payment Service; Order Service (upstream request path)",
        "- Integration mode: local Slack, model, and Kubernetes simulators",
        "",
        "## Timeline",
        "",
        "| Time (UTC) | Event | Details |",
        "|---|---|---|",
    ]
    for item in timeline:
        summary = item.summary.replace("|", "\\|")
        lines.append(f"| {item.occurred_at.isoformat()} | {item.event_type} | {summary} |")
    lines.extend(
        [
            "",
            "## Root-cause hypothesis",
            "",
            analysis.probable_root_cause.text,
            "",
            f"Confidence: **{analysis.confidence:.0%}**",
            "",
        ]
    )
    lines.extend(
        [
            "## Supporting evidence",
            "",
            "| Evidence ID | Time (UTC) | Service | Source / type | Summary |",
            "|---|---|---|---|---|",
        ]
    )
    for item in evidence:
        summary = item.normalized_summary.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| `{item.id}` | {item.observed_at.isoformat()} | {item.service} | "
            f"{item.source} / {item.evidence_type} | {summary} |"
        )
    lines.extend(
        [
            "",
            "## Remediation",
            "",
            f"- Proposed action: `{action.action_type}` on `{action.target}`",
            f"- Policy risk: **{action.risk_level.upper()}_RISK**",
            f"- Status: **{action.status}**",
            f"- Approved by: `{action.approved_by}`"
            if action.approved_by
            else "- Approved by: not approved",
            f"- Result: `{json.dumps(remediation_result, sort_keys=True)}`"
            if remediation_result
            else "- Result: no execution",
            "- Execution model: the local Kubernetes simulator rolled Compose to the healthy "
            "service configuration; no Kubernetes API was called.",
            "",
            "## Actions taken",
            "",
        ]
    )
    lines.extend(f"- {status}: {step}" for step, status in step_status)
    lines.extend(
        [
            "",
            "## Audit trail",
            "",
            "| Time (UTC) | Actor | Action | Entity | Outcome |",
            "|---|---|---|---|---|",
        ]
    )
    lines.extend(
        f"| {item.created_at.isoformat()} | {item.actor_type} / `{item.actor_id}` | "
        f"{item.action} | {item.entity_type} / `{item.entity_id}` | {item.outcome} |"
        for item in audit
    )
    report = root / ".opsmind" / "demo" / "latest-report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def run_demo(*, root: Path | None = None, traffic_requests: int = 180) -> Path:
    """Execute one full local scenario and return the generated report path."""
    repository = root or Path.cwd()
    if repository != Path.cwd():
        os.chdir(repository)
    if not (repository / "compose.yaml").is_file():
        raise RuntimeError("Run the demo from the OpsMind repository root")
    _load_local_environment(repository)
    settings = Settings()
    db_url = _host_database_url(settings)
    redis_url = _host_redis_url(settings)
    _say("Starting OpsMind, ShopStack, and observability services.")
    env = os.environ.copy()
    # The demo invokes the exact DetectionRunner code itself to avoid duplicate pollers.
    env["OPSMIND_DETECTION_POLL_INTERVAL_SECONDS"] = "3600"
    _compose(
        repository,
        ["up", "-d", "--build"],
        environment=_fault_environment(False) | {"OPSMIND_DETECTION_POLL_INTERVAL_SECONDS": "3600"},
    )
    _compose(repository, ["run", "--rm", "api", "alembic", "upgrade", "head"], environment=env)
    for url in (
        f"{BASE_URLS['gateway']}/ready",
        f"{BASE_URLS['payment']}/ready",
        f"{BASE_URLS['prometheus']}/-/ready",
        f"{BASE_URLS['loki']}/ready",
    ):
        _wait_http(url, timeout=180)

    engine = create_engine(db_url, pool_pre_ping=True)
    sessions = create_session_factory(engine)
    redis = Redis.from_url(redis_url, decode_responses=True)
    client = httpx.Client(timeout=15)
    incident_id: UUID | None = None
    action_id: UUID | None = None
    faulty_deployed = False
    remediation_result: dict[str, object] | None = None
    recovery_seconds: float | None = None
    step_status: list[tuple[str, str]] = []
    start_time = time.monotonic()
    event_cursor = _latest_stream_id(redis)
    try:
        with sessions.begin() as session:
            services = _ensure_registry(session)
            healthy_version = f"healthy-demo-{uuid4().hex[:8]}"
            faulty_version = f"demo-bug-{uuid4().hex[:8]}"
            healthy_started = datetime.now(UTC) - timedelta(seconds=2)
            healthy_deployment = DeploymentRow(
                id=uuid4(),
                tenant_id=TENANT_ID,
                service_id=services[PAYMENT_SERVICE].id,
                environment="demo",
                version=healthy_version,
                commit_sha="demo-baseline",
                deployed_by="opsmind-demo",
                status="succeeded",
                started_at=healthy_started,
                completed_at=healthy_started,
            )
            session.add(healthy_deployment)
        _say("1/18 Deploy faulty Payment Service version (Compose deployment simulator).")
        faulty_started = datetime.now(UTC)
        _compose(
            repository,
            ["up", "-d", "--force-recreate", PAYMENT_COMPOSE_SERVICE],
            environment=_fault_environment(True),
        )
        faulty_deployed = True
        _wait_http(f"{BASE_URLS['payment']}/ready", timeout=120)
        with sessions.begin() as session:
            faulty_deployment = DeploymentRow(
                id=uuid4(),
                tenant_id=TENANT_ID,
                service_id=services[PAYMENT_SERVICE].id,
                environment="demo",
                version=faulty_version,
                commit_sha="demo-buggy-release",
                deployed_by="opsmind-demo",
                status="succeeded",
                started_at=faulty_started,
            )
            session.add(faulty_deployment)
        step_status.append(("Deploy faulty version", "succeeded"))

        _say(f"2/18 Generate concurrent order traffic ({traffic_requests} requests).")
        traffic_output = _run_traffic(repository, traffic_requests)
        step_status.append(("Generate ShopStack traffic", "succeeded"))
        print(traffic_output)
        time.sleep(17)  # allow Prometheus's 15-second scrape to ingest post-deployment samples

        _say("3-4/18 Run Prometheus/Loki detection and create one correlated incident.")
        # One direct, deterministic runner avoids the background poller racing the demo stream.
        event, detection_cycles = _wait_for_detection(
            redis=redis, settings=settings, cursor=event_cursor, timeout_seconds=180
        )
        step_status.append(("Prometheus detects anomaly", "succeeded"))
        incident_row = _wait_for_incident_projection(sessions, event)
        incident_id = incident_row.id
        step_status.append(("Detection Engine creates incident", "succeeded"))
        _transition_incident(
            sessions,
            incident_id=incident_id,
            target=IncidentState.TRIAGING,
            correlation_id=str(incident_id),
        )
        _transition_incident(
            sessions,
            incident_id=incident_id,
            target=IncidentState.INVESTIGATING,
            correlation_id=str(incident_id),
        )
        _say("5/18 Incident Correlation Engine collects timeline evidence.")
        evidence_ids = _persist_correlation(
            sessions, client, incident_row, services[PAYMENT_SERVICE]
        )
        step_status.append(("Correlation Engine builds chronological timeline", "succeeded"))

        _say("6-9/18 Local AI model simulator uses the bounded read-only investigation tools.")
        analysis, investigation_run_id = _investigate(
            sessions, client, settings, incident_row, services[PAYMENT_SERVICE]
        )
        if analysis.confidence < 0.75 or "demo-bug-" not in analysis.probable_root_cause.text:
            raise RuntimeError("Investigation did not identify the faulty deployment with evidence")
        with sessions.begin() as session:
            row = session.get(IncidentRow, incident_id)
            if row is None:
                raise RuntimeError("Incident missing when persisting hypothesis")
            before = {"state": row.state, "version": row.version}
            row.state = IncidentState.IDENTIFIED.value
            row.version += 1
            hypothesis = IncidentHypothesisRow(
                tenant_id=TENANT_ID,
                incident_id=incident_id,
                summary=analysis.probable_root_cause.text,
                confidence=analysis.confidence,
                status="leading",
                rationale=(
                    "Local structured-output simulator cites current Prometheus, Loki, "
                    "and deployment evidence."
                ),
            )
            evidence_by_id = {
                item.id: item
                for item in session.scalars(
                    select(IncidentEvidenceRow).where(
                        IncidentEvidenceRow.tenant_id == TENANT_ID,
                        IncidentEvidenceRow.incident_id == incident_id,
                    )
                ).all()
            }
            hypothesis.evidence = [
                evidence_by_id[item]
                for item in analysis.supporting_evidence
                if item in evidence_by_id
            ]
            session.add(hypothesis)
            _append_timeline(
                session,
                incident_id=incident_id,
                event_type="hypothesis.generated",
                summary=analysis.probable_root_cause.text,
            )
            _audit(
                session,
                action="incident.hypothesis_created",
                entity_type="incident",
                entity_id=incident_id,
                correlation_id=str(incident_id),
                before=before,
                after={
                    "state": row.state,
                    "version": row.version,
                    "confidence": analysis.confidence,
                },
            )
        step_status.extend(
            [
                ("AI investigator queries Prometheus metrics", "succeeded"),
                (
                    "AI investigator queries Loki logs",
                    "succeeded"
                    if any(
                        evidence_by_id.get(item) is not None
                        and evidence_by_id[item].source == "loki"
                        for item in analysis.supporting_evidence
                    )
                    else "queried; no matching log cited",
                ),
                ("AI investigator checks deployment history", "succeeded"),
                ("AI identifies faulty deployment as leading hypothesis", "succeeded"),
                ("AI presents evidence references", "succeeded"),
            ]
        )
        _say(
            f"Leading hypothesis ({analysis.confidence:.0%} confidence): "
            f"{analysis.probable_root_cause.text}"
        )

        _say(
            "10-12/18 Evaluate and propose deployment rollback; simulated Slack requests approval."
        )
        with sessions() as session:
            faulty_deployment_row = session.scalar(
                select(DeploymentRow).where(
                    DeploymentRow.tenant_id == TENANT_ID,
                    DeploymentRow.service_id == services[PAYMENT_SERVICE].id,
                    DeploymentRow.version == faulty_version,
                )
            )
            if faulty_deployment_row is None:
                raise RuntimeError("Faulty deployment history record is unavailable")
            faulty_id = faulty_deployment_row.id
        action_id, approval_id, _ = _record_remediation_proposal(
            sessions,
            incident_id=incident_id,
            deployment_id=faulty_id,
            target_version=healthy_version,
            correlation_id=str(incident_id),
        )
        step_status.append(("AI proposes deployment rollback", "succeeded"))
        step_status.append(("Policy validates rollback as MEDIUM_RISK", "succeeded"))
        step_status.append(("Slack approval request", "simulated and persisted"))
        approved = _request_simulated_slack_approval(
            sessions,
            incident_id=incident_id,
            action_id=action_id,
            approval_id=approval_id,
            correlation_id=str(incident_id),
        )
        if not approved:
            step_status.append(("Human approval", "rejected or not approved"))
            report = _write_report(
                sessions,
                incident_id=incident_id,
                analysis=analysis,
                action_id=action_id,
                remediation_result=None,
                recovery_seconds=None,
                root=repository,
                step_status=step_status,
            )
            raise RuntimeError(f"Rollback was not approved; report saved at {report}")
        step_status.append(("Human approval", "approved"))
        _transition_incident(
            sessions,
            incident_id=incident_id,
            target=IncidentState.MITIGATING,
            correlation_id=str(incident_id),
        )

        _say("13-16/18 Human approval accepted; execute and verify local rollback simulation.")
        remediation_result = _execute_remediation(
            sessions,
            action_id=action_id,
            incident_id=incident_id,
            correlation_id=str(incident_id),
            root=repository,
        )
        faulty_deployed = False
        step_status.extend(
            [
                ("Kubernetes rollback", "local simulator executed Compose rollback"),
                ("Verify Payment Service readiness", "succeeded"),
            ]
        )
        recovery_seconds = time.monotonic() - start_time
        _transition_incident(
            sessions,
            incident_id=incident_id,
            target=IncidentState.RESOLVED,
            correlation_id=str(incident_id),
        )
        step_status.append(("Resolve incident", "succeeded"))
        step_status.append(("Generate final incident report", "succeeded"))
        report = _write_report(
            sessions,
            incident_id=incident_id,
            analysis=analysis,
            action_id=action_id,
            remediation_result=remediation_result,
            recovery_seconds=recovery_seconds,
            root=repository,
            step_status=step_status,
        )
        _say(f"18/18 Incident resolved. Final report: {report}")
        _say(
            f"Detection cycles: {', '.join(detection_cycles)}; "
            f"investigation run: {investigation_run_id}; correlated evidence: {len(evidence_ids)}"
        )
        return report
    finally:
        if faulty_deployed:
            _say("Safety cleanup: restoring the healthy Payment Service configuration.")
            try:
                _compose(
                    repository,
                    ["up", "-d", "--force-recreate", PAYMENT_COMPOSE_SERVICE],
                    environment=_fault_environment(False),
                )
                _wait_http(f"{BASE_URLS['payment']}/ready", timeout=120)
            except Exception as exc:
                print(f"Rollback cleanup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        client.close()
        redis.close()
        engine.dispose()
