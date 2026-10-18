"""Focused contract tests for Kubernetes remediation behavior."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from opsmind_models.domain.remediation import RemediationActionType, RemediationRisk
from opsmind_remediation_engine.executors import RemediationCommand
from opsmind_remediation_engine.kubernetes_adapter import KubernetesRemediationAdapter
from urllib3 import HTTPConnectionPool
from urllib3.exceptions import ReadTimeoutError


def _deployment(
    *,
    name: str = "payment",
    uid: str = "deployment-uid",
    replicas: int = 2,
    revision: str = "3",
    template_annotations: dict[str, str] | None = None,
) -> client.V1Deployment:
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=name,
            uid=uid,
            generation=4,
            annotations={"deployment.kubernetes.io/revision": revision},
        ),
        spec=client.V1DeploymentSpec(
            replicas=replicas,
            selector=client.V1LabelSelector(match_labels={"app": name}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(annotations=template_annotations or {}),
                spec=client.V1PodSpec(containers=[client.V1Container(name="app", image="app:v1")]),
            ),
        ),
        status=client.V1DeploymentStatus(
            observed_generation=4,
            updated_replicas=replicas,
            available_replicas=replicas,
            ready_replicas=replicas,
        ),
    )


def _command(
    action: RemediationActionType,
    parameters: dict[str, object],
    *,
    target: str = "payment",
) -> RemediationCommand:
    return RemediationCommand(
        action_id=uuid4(),
        incident_id=uuid4(),
        action_type=action,
        target=target,
        parameters=parameters,
        risk_level=(
            RemediationRisk.LOW_RISK
            if action == RemediationActionType.RESTART_SERVICE
            else RemediationRisk.MEDIUM_RISK
        ),
    )


def _adapter(
    apps: Any,
    core: Any | None = None,
    *,
    dry_run: bool = False,
    sleep: Any = lambda _delay: None,
) -> KubernetesRemediationAdapter:
    return KubernetesRemediationAdapter(
        namespace="shopstack",
        apps_api=apps,
        core_api=core or client.CoreV1Api(),
        max_retries=2,
        retry_backoff_seconds=0.01,
        rollout_timeout_seconds=0.01,
        dry_run=dry_run,
        sleep=sleep,
    )


def test_restart_deployment_uses_annotated_rollout_and_kubernetes_dry_run() -> None:
    apps = client.AppsV1Api()
    apps.read_namespaced_deployment = lambda **_kwargs: _deployment()
    calls: list[dict[str, object]] = []
    apps.patch_namespaced_deployment = lambda **kwargs: calls.append(kwargs)
    command = _command(
        RemediationActionType.RESTART_SERVICE,
        {"grace_period_seconds": 30},
    )

    result = _adapter(apps, dry_run=True).execute(command)

    assert result.result["operation"] == "restart"
    assert result.result["dry_run"] is True
    assert calls[0]["dry_run"] == "All"
    patch = calls[0]["body"]
    annotations = patch["spec"]["template"]["metadata"]["annotations"]  # type: ignore[index]
    assert annotations["opsmind.io/last-remediation-action"] == str(command.action_id)
    assert annotations["opsmind.io/requested-grace-period-seconds"] == "30"


def test_restart_same_action_is_idempotent() -> None:
    command = _command(RemediationActionType.RESTART_SERVICE, {"grace_period_seconds": 30})
    deployment = _deployment(
        template_annotations={"opsmind.io/last-remediation-action": str(command.action_id)}
    )
    apps = client.AppsV1Api()
    apps.read_namespaced_deployment = lambda **_kwargs: deployment
    apps.patch_namespaced_deployment = lambda **_kwargs: pytest.fail(
        "idempotent restart must not patch twice"
    )

    result = _adapter(apps).execute(command)

    assert result.result["already_applied"] is True


def test_scale_deployment_uses_scale_subresource_and_captures_previous_replicas() -> None:
    apps = client.AppsV1Api()
    apps.read_namespaced_deployment = lambda **_kwargs: _deployment(replicas=2)
    calls: list[dict[str, object]] = []
    apps.patch_namespaced_deployment_scale = lambda **kwargs: calls.append(kwargs)
    command = _command(RemediationActionType.SCALE_SERVICE, {"replicas": 5})

    result = _adapter(apps).execute(command)

    assert result.result == {
        "operation": "scale",
        "deployment": "payment",
        "previous_replicas": 2,
        "replicas": 5,
        "dry_run": False,
    }
    assert calls[0]["body"] == {"spec": {"replicas": 5}}


def test_scale_compensation_restores_recorded_replica_count() -> None:
    apps = client.AppsV1Api()
    apps.read_namespaced_deployment = lambda **_kwargs: _deployment(replicas=5)
    calls: list[dict[str, object]] = []
    apps.patch_namespaced_deployment_scale = lambda **kwargs: calls.append(kwargs)
    command = _command(RemediationActionType.SCALE_SERVICE, {"replicas": 5})

    result = _adapter(apps).rollback(command, {"previous_replicas": 2})

    assert result.result["previous_replicas"] == 5
    assert result.result["replicas"] == 2
    assert calls[0]["body"] == {"spec": {"replicas": 2}}


def test_rollback_deployment_restores_selected_owned_replica_set_revision() -> None:
    deployment = _deployment(revision="5")
    previous = client.V1ReplicaSet(
        metadata=client.V1ObjectMeta(
            name="payment-previous",
            annotations={"deployment.kubernetes.io/revision": "2"},
            owner_references=[
                client.V1OwnerReference(
                    api_version="apps/v1",
                    kind="Deployment",
                    name="payment",
                    uid="deployment-uid",
                    controller=True,
                    block_owner_deletion=True,
                )
            ],
        ),
        spec=client.V1ReplicaSetSpec(
            selector=client.V1LabelSelector(match_labels={"app": "payment"}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels={"app": "payment"}),
                spec=client.V1PodSpec(containers=[client.V1Container(name="app", image="app:v2")]),
            ),
        ),
    )
    apps = client.AppsV1Api()
    apps.read_namespaced_deployment = lambda **_kwargs: deployment
    apps.list_namespaced_replica_set = lambda **_kwargs: client.V1ReplicaSetList(items=[previous])
    calls: list[dict[str, object]] = []
    apps.patch_namespaced_deployment = lambda **kwargs: calls.append(kwargs)
    command = _command(
        RemediationActionType.ROLLBACK_DEPLOYMENT,
        {"deployment_id": uuid4(), "target_version": "2"},
    )

    result = _adapter(apps).execute(command)

    assert result.result["previous_revision"] == "5"
    assert result.result["target_revision"] == "2"
    patch = calls[0]["body"]
    assert patch["spec"]["template"]["spec"]["containers"][0]["image"] == "app:v2"  # type: ignore[index]


def test_transient_api_errors_retry_with_backoff() -> None:
    adapter = _adapter(client.AppsV1Api())
    attempts = 0
    delays: list[float] = []

    def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ApiException(status=503)
        return "ok"

    adapter._sleep = delays.append

    assert adapter._call(call) == "ok"
    assert attempts == 3
    assert delays == [0.01, 0.02]


def test_inspection_returns_deployment_and_pod_health_without_pod_spec() -> None:
    apps, core = client.AppsV1Api(), client.CoreV1Api()
    apps.read_namespaced_deployment = lambda **_kwargs: _deployment()
    core.list_namespaced_pod = lambda **_kwargs: client.V1PodList(
        items=[
            client.V1Pod(
                metadata=client.V1ObjectMeta(name="payment-abc"),
                status=client.V1PodStatus(
                    phase="Running",
                    conditions=[client.V1PodCondition(type="Ready", status="True")],
                    container_statuses=[
                        client.V1ContainerStatus(
                            name="app",
                            restart_count=2,
                            ready=True,
                            image="app",
                            image_id="id",
                            container_id="id",
                        )
                    ],
                ),
            )
        ]
    )
    adapter = _adapter(apps, core)

    deployment_status = adapter.inspect_deployment_status("payment")
    pod_health = adapter.inspect_pod_health("payment")

    assert deployment_status["healthy"] is True
    assert pod_health[0]["ready"] is True
    assert pod_health[0]["restarts"] == 2
    assert "spec" not in pod_health[0]


def test_event_and_log_retrieval_is_scoped_to_deployment_pods_and_bounded() -> None:
    apps, core = client.AppsV1Api(), client.CoreV1Api()
    apps.read_namespaced_deployment = lambda **_kwargs: _deployment()
    core.list_namespaced_pod = lambda **_kwargs: client.V1PodList(
        items=[client.V1Pod(metadata=client.V1ObjectMeta(name="payment-abc"))]
    )
    core.list_namespaced_event = lambda **_kwargs: client.CoreV1EventList(
        items=[
            client.CoreV1Event(
                metadata=client.V1ObjectMeta(name="payment-warning"),
                type="Warning",
                reason="BackOff",
                message="container restart backoff",
                count=2,
                involved_object=client.V1ObjectReference(kind="Pod", name="payment-abc"),
            ),
            client.CoreV1Event(
                metadata=client.V1ObjectMeta(name="other-event"),
                type="Normal",
                reason="Other",
                message="unrelated event",
                count=1,
                involved_object=client.V1ObjectReference(kind="Pod", name="other-pod"),
            ),
        ]
    )
    core.read_namespaced_pod_log = lambda **kwargs: f"logs:{kwargs['name']}:{kwargs['tail_lines']}"
    adapter = _adapter(apps, core)

    events = adapter.retrieve_events("payment", limit=10)
    logs = adapter.retrieve_pod_logs("payment", tail_lines=25)

    assert len(events) == 1
    assert events[0]["reason"] == "BackOff"
    assert logs == {"payment-abc": "logs:payment-abc:25"}
    with pytest.raises(ValueError, match="tail"):
        adapter.retrieve_pod_logs("payment", tail_lines=1001)


def test_invalid_namespace_and_target_are_rejected_before_api_calls() -> None:
    apps = client.AppsV1Api()
    with pytest.raises(ValueError, match="namespace"):
        KubernetesRemediationAdapter(
            namespace="../default", apps_api=apps, core_api=client.CoreV1Api()
        )

    apps.read_namespaced_deployment = lambda **_kwargs: pytest.fail(
        "unsafe target must fail before an API request"
    )
    command = _command(
        RemediationActionType.RESTART_SERVICE,
        {"grace_period_seconds": 30},
        target="other/namespace",
    )
    with pytest.raises(ValueError, match="deployment target"):
        _adapter(apps).execute(command)


def test_unavailable_kubernetes_api_retries_then_fails_closed() -> None:
    apps = client.AppsV1Api()
    attempts = 0

    def unavailable(**_kwargs: object) -> client.V1Deployment:
        nonlocal attempts
        attempts += 1
        raise ReadTimeoutError(
            HTTPConnectionPool("kubernetes.local"),
            "kubernetes",
            "simulated API timeout",
        )

    apps.read_namespaced_deployment = unavailable
    delays: list[float] = []
    command = _command(RemediationActionType.RESTART_SERVICE, {"grace_period_seconds": 30})

    with pytest.raises(ReadTimeoutError):
        _adapter(apps, sleep=delays.append).execute(command)

    assert attempts == 3
    assert delays == [0.01, 0.02]
