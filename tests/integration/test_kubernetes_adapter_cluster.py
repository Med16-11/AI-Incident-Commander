"""Opt-in contract tests against a locally running Kubernetes cluster."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from opsmind_models.domain.remediation import RemediationActionType, RemediationRisk
from opsmind_remediation_engine.executors import RemediationCommand
from opsmind_remediation_engine.kubernetes_adapter import KubernetesRemediationAdapter

_CLUSTER_TEST_SKIP = pytest.mark.skipif(
    os.environ.get("OPSMIND_KUBERNETES_INTEGRATION") != "1",
    reason="Set OPSMIND_KUBERNETES_INTEGRATION=1 to run against a local cluster",
)


@_CLUSTER_TEST_SKIP
def test_local_cluster_inspection_and_server_side_dry_run() -> None:
    namespace = os.environ.get("OPSMIND_KUBERNETES_TEST_NAMESPACE", "shopstack")
    deployment_name = os.environ.get("OPSMIND_KUBERNETES_TEST_DEPLOYMENT", "shopstack-api-gateway")
    adapter = KubernetesRemediationAdapter.from_cluster(
        namespace=namespace,
        local_development=True,
        dry_run=True,
        request_timeout_seconds=5,
        max_retries=2,
        rollout_timeout_seconds=5,
    )
    deployment = adapter._read_deployment(deployment_name)
    assert adapter.inspect_deployment_status(deployment_name)["deployment"] == deployment_name
    assert isinstance(adapter.inspect_pod_health(deployment_name), list)
    assert isinstance(adapter.retrieve_events(deployment_name, limit=25), list)
    assert isinstance(adapter.retrieve_pod_logs(deployment_name, tail_lines=20), dict)

    restart = RemediationCommand(
        action_id=uuid4(),
        incident_id=uuid4(),
        action_type=RemediationActionType.RESTART_SERVICE,
        target=deployment_name,
        parameters={"grace_period_seconds": 30},
        risk_level=RemediationRisk.LOW_RISK,
    )
    restart_result = adapter.execute(restart)
    assert restart_result.result["dry_run"] is True

    replicas = int(deployment.spec.replicas or 0) if deployment.spec else 0
    scale = RemediationCommand(
        action_id=uuid4(),
        incident_id=uuid4(),
        action_type=RemediationActionType.SCALE_SERVICE,
        target=deployment_name,
        parameters={"replicas": replicas},
        risk_level=RemediationRisk.MEDIUM_RISK,
    )
    scale_result = adapter.execute(scale)
    assert scale_result.result["dry_run"] is True

    current_revision = (
        (deployment.metadata.annotations or {}).get("deployment.kubernetes.io/revision")
        if deployment.metadata
        else None
    )
    if current_revision is not None:
        rollback = RemediationCommand(
            action_id=uuid4(),
            incident_id=uuid4(),
            action_type=RemediationActionType.ROLLBACK_DEPLOYMENT,
            target=deployment_name,
            parameters={"deployment_id": uuid4(), "target_version": current_revision},
            risk_level=RemediationRisk.MEDIUM_RISK,
        )
        rollback_result = adapter.execute(rollback)
        assert rollback_result.result["dry_run"] is True

    after = adapter._read_deployment(deployment_name)
    assert (after.spec.replicas if after.spec else None) == (
        deployment.spec.replicas if deployment.spec else None
    )
    assert adapter.dry_run is True


def test_rbac_manifest_is_namespace_scoped_and_never_cluster_admin() -> None:
    from pathlib import Path

    import yaml  # type: ignore[import-untyped]

    manifest_path = Path(__file__).resolve().parents[2] / "infra/kubernetes/remediation-rbac.yaml"
    documents = [item for item in yaml.safe_load_all(manifest_path.read_text()) if item]
    kinds = {item["kind"] for item in documents}
    assert "ClusterRole" not in kinds
    assert "ClusterRoleBinding" not in kinds
    role = next(item for item in documents if item["kind"] == "Role")
    binding = next(item for item in documents if item["kind"] == "RoleBinding")
    assert role["metadata"]["namespace"] == "shopstack"
    assert binding["metadata"]["namespace"] == "shopstack"
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": "opsmind-remediation",
    }
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "opsmind-remediation",
            "namespace": "opsmind-system",
        }
    ]
    assert {tuple(sorted(rule["resources"])) for rule in role["rules"]} == {
        ("deployments",),
        ("deployments/scale",),
        ("replicasets",),
        ("pods",),
        ("pods/log",),
        ("events",),
    }
