"""Regression checks for security-sensitive deployment and browser boundaries."""

from pathlib import Path

import yaml  # type: ignore[import-untyped]
from opsmind_common.config import Settings
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[2]


def test_internal_service_tokens_are_secret_values() -> None:
    settings = Settings(
        correlation_api_token=SecretStr("correlation-test"),
        investigation_api_token=SecretStr("investigation-test"),
    )
    assert isinstance(settings.correlation_api_token, SecretStr)
    assert isinstance(settings.investigation_api_token, SecretStr)
    assert "correlation-test" not in repr(settings)
    assert "investigation-test" not in repr(settings)


def test_remediation_rbac_is_namespace_scoped_and_has_no_shell_or_secret_access() -> None:
    path = ROOT / "infra/kubernetes/remediation-rbac.yaml"
    objects = list(yaml.safe_load_all(path.read_text()))
    roles = [item for item in objects if item.get("kind") == "Role"]
    assert len(roles) == 1
    role = roles[0]
    assert role["metadata"]["namespace"] == "shopstack"
    for rule in role["rules"]:
        assert "secrets" not in rule["resources"]
        assert "pods/exec" not in rule["resources"]
        assert set(rule["verbs"]).issubset({"get", "list", "patch"})
    assert not any(item.get("kind") == "ClusterRole" for item in objects)


def test_shopstack_pods_are_nonroot_and_do_not_mount_service_tokens() -> None:
    path = ROOT / "infra/kubernetes/shopstack/services.yaml"
    deployments = [
        item
        for item in yaml.safe_load_all(path.read_text())
        if item and item.get("kind") == "Deployment"
    ]
    assert len(deployments) == 5
    for deployment in deployments:
        pod_spec = deployment["spec"]["template"]["spec"]
        assert pod_spec["automountServiceAccountToken"] is False
        assert pod_spec["securityContext"]["runAsNonRoot"] is True
        container = pod_spec["containers"][0]
        security = container["securityContext"]
        assert security["allowPrivilegeEscalation"] is False
        assert security["readOnlyRootFilesystem"] is True
        assert security["capabilities"]["drop"] == ["ALL"]


def test_browser_does_not_persist_bearer_tokens_and_sets_security_headers() -> None:
    auth = (ROOT / "apps/web/src/ui/auth.tsx").read_text()
    client = (ROOT / "apps/web/src/data/client.ts").read_text()
    nginx = (ROOT / "apps/web/nginx.conf").read_text()
    assert "sessionStorage" not in auth
    assert "localStorage" not in auth
    assert 'credentials: "include"' not in client
    assert "Content-Security-Policy" in nginx
    assert "X-Content-Type-Options" in nginx
    assert "X-Frame-Options" in nginx
