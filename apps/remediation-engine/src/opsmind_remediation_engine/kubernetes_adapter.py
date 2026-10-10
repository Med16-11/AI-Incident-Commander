"""Least-privilege Kubernetes remediation adapter using the official Python API client."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from opsmind_remediation_engine.executors import (
    ExecutionResult,
    RemediationCommand,
    VerificationResult,
)

_K8S_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?\Z")
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_ACTION_ANNOTATION = "opsmind.io/last-remediation-action"
_RESTART_ANNOTATION = "opsmind.io/restarted-at"


class KubernetesRemediationAdapter:
    """Perform narrowly scoped deployment operations and bounded read-only inspection."""

    def __init__(
        self,
        *,
        namespace: str,
        apps_api: client.AppsV1Api,
        core_api: client.CoreV1Api,
        request_timeout_seconds: float = 5.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 0.2,
        rollout_timeout_seconds: float = 120.0,
        dry_run: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        _validate_name(namespace, "namespace")
        if request_timeout_seconds <= 0 or rollout_timeout_seconds <= 0:
            raise ValueError("Kubernetes timeouts must be positive")
        if max_retries < 0 or retry_backoff_seconds < 0:
            raise ValueError("Kubernetes retry settings cannot be negative")
        self.namespace = namespace
        self.apps_api = apps_api
        self.core_api = core_api
        self.request_timeout_seconds = request_timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.rollout_timeout_seconds = rollout_timeout_seconds
        self.dry_run = dry_run
        self._sleep = sleep
        self._monotonic = monotonic

    @classmethod
    def from_cluster(
        cls,
        *,
        namespace: str,
        local_development: bool = False,
        **options: Any,
    ) -> KubernetesRemediationAdapter:
        """Build official API clients from in-cluster credentials or explicit local kubeconfig."""
        if local_development:
            config.load_kube_config()
        else:
            config.load_incluster_config()
        api_client = client.ApiClient()
        return cls(
            namespace=namespace,
            apps_api=client.AppsV1Api(api_client),
            core_api=client.CoreV1Api(api_client),
            **options,
        )

    def execute(self, command: RemediationCommand) -> ExecutionResult:
        deployment_name = self._deployment_name(command.target)
        deployment = self._read_deployment(deployment_name)
        action_id = str(command.action_id)
        if command.action_type.value == "restart_service":
            return self._restart(deployment_name, deployment, action_id, command.parameters)
        if command.action_type.value == "scale_service":
            return self._scale(deployment_name, deployment, command.parameters)
        if command.action_type.value == "rollback_deployment":
            return self._rollback_revision(
                deployment_name,
                deployment,
                action_id,
                str(command.parameters["target_version"]),
            )
        raise ValueError(f"Kubernetes adapter does not implement {command.action_type.value}")

    def rollback(
        self, command: RemediationCommand, previous_result: dict[str, object]
    ) -> ExecutionResult:
        deployment_name = self._deployment_name(command.target)
        deployment = self._read_deployment(deployment_name)
        action_id = str(command.action_id)
        if command.action_type.value == "scale_service":
            previous_replicas = previous_result.get("previous_replicas")
            if not isinstance(previous_replicas, int) or previous_replicas < 0:
                raise ValueError("Recorded result does not contain a valid previous replica count")
            return self._scale_to(deployment_name, deployment, previous_replicas)
        if command.action_type.value == "rollback_deployment":
            previous_revision = previous_result.get("previous_revision")
            if not isinstance(previous_revision, str) or not previous_revision:
                raise ValueError("Recorded result does not contain a previous deployment revision")
            return self._rollback_revision(
                deployment_name, deployment, action_id, previous_revision
            )
        raise ValueError(f"Kubernetes adapter cannot reverse {command.action_type.value}")

    def verify(self, command: RemediationCommand, result: ExecutionResult) -> VerificationResult:
        del result
        deployment_name = self._deployment_name(command.target)
        deadline = self._monotonic() + self.rollout_timeout_seconds
        status: dict[str, object] = {}
        while True:
            status = self.inspect_deployment_status(deployment_name)
            if status["healthy"] is True:
                return VerificationResult(True, status)
            if self._monotonic() >= deadline:
                return VerificationResult(False, {**status, "timed_out": True})
            self._sleep(min(1.0, max(0.0, deadline - self._monotonic())))

    def inspect_deployment_status(self, deployment_name: str) -> dict[str, object]:
        """Return the bounded, normalized observed state for a named deployment."""
        deployment = self._read_deployment(self._deployment_name(deployment_name))
        spec = deployment.spec
        status = deployment.status
        desired = int(spec.replicas or 0) if spec else 0
        updated = int(status.updated_replicas or 0) if status else 0
        available = int(status.available_replicas or 0) if status else 0
        observed = int(status.observed_generation or 0) if status else 0
        generation = int(deployment.metadata.generation or 0) if deployment.metadata else 0
        return {
            "deployment": deployment_name,
            "namespace": self.namespace,
            "generation": generation,
            "observed_generation": observed,
            "desired_replicas": desired,
            "updated_replicas": updated,
            "available_replicas": available,
            "ready_replicas": int(status.ready_replicas or 0) if status else 0,
            "healthy": observed >= generation and updated == desired and available == desired,
        }

    def inspect_pod_health(self, deployment_name: str) -> list[dict[str, object]]:
        """Inspect at most 100 matching pods without returning pod specs or environment data."""
        deployment = self._read_deployment(self._deployment_name(deployment_name))
        selector = _label_selector(deployment)
        pod_list = self._call(
            lambda: self.core_api.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=selector,
                limit=100,
                _request_timeout=self.request_timeout_seconds,
            )
        )
        pods: list[dict[str, object]] = []
        for pod in pod_list.items:
            metadata, status = pod.metadata, pod.status
            conditions = status.conditions or [] if status else []
            pods.append(
                {
                    "pod": metadata.name if metadata else "unknown",
                    "phase": status.phase if status else "Unknown",
                    "ready": any(
                        condition.type == "Ready" and condition.status == "True"
                        for condition in conditions
                    ),
                    "restarts": sum(
                        int(container.restart_count or 0)
                        for container in (status.container_statuses or [] if status else [])
                    ),
                    "conditions": [
                        {
                            "type": condition.type,
                            "status": condition.status,
                            "reason": condition.reason,
                        }
                        for condition in conditions
                    ],
                }
            )
        return pods

    def retrieve_events(self, deployment_name: str, *, limit: int = 100) -> list[dict[str, object]]:
        """Retrieve recent events for pods selected by the deployment, capped at 100."""
        if not 1 <= limit <= 100:
            raise ValueError("Event limit must be between 1 and 100")
        deployment = self._read_deployment(self._deployment_name(deployment_name))
        pods = self._call(
            lambda: self.core_api.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=_label_selector(deployment),
                limit=100,
                _request_timeout=self.request_timeout_seconds,
            )
        )
        pod_names = {pod.metadata.name for pod in pods.items if pod.metadata}
        events = self._call(
            lambda: self.core_api.list_namespaced_event(
                namespace=self.namespace,
                limit=limit,
                _request_timeout=self.request_timeout_seconds,
            )
        )
        relevant = [
            event
            for event in events.items
            if event.involved_object
            and (
                event.involved_object.name == deployment_name
                or event.involved_object.name in pod_names
            )
        ]
        return [
            {
                "type": event.type,
                "reason": event.reason,
                "message": (event.message or "")[:2000],
                "count": event.count,
                "first_timestamp": _iso(event.first_timestamp),
                "last_timestamp": _iso(event.last_timestamp or event.event_time),
                "object_kind": event.involved_object.kind,
                "object_name": event.involved_object.name,
            }
            for event in relevant[:limit]
        ]

    def retrieve_pod_logs(self, deployment_name: str, *, tail_lines: int = 100) -> dict[str, str]:
        """Retrieve bounded, timestamped log tails from up to ten matching pods."""
        if not 1 <= tail_lines <= 1000:
            raise ValueError("Pod log tail must be between 1 and 1000 lines")
        deployment = self._read_deployment(self._deployment_name(deployment_name))
        pods = self._call(
            lambda: self.core_api.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=_label_selector(deployment),
                limit=10,
                _request_timeout=self.request_timeout_seconds,
            )
        )
        logs: dict[str, str] = {}
        for pod in pods.items:
            if pod.metadata is None or pod.metadata.name is None:
                continue
            name = pod.metadata.name

            def read_pod_logs(pod_name: str = name) -> str:
                return cast(
                    str,
                    self.core_api.read_namespaced_pod_log(
                        name=pod_name,
                        namespace=self.namespace,
                        tail_lines=tail_lines,
                        timestamps=True,
                        _request_timeout=self.request_timeout_seconds,
                    ),
                )

            logs[name] = self._call(read_pod_logs)
        return logs

    def _restart(
        self,
        name: str,
        deployment: client.V1Deployment,
        action_id: str,
        parameters: dict[str, object],
    ) -> ExecutionResult:
        annotations = _template_annotations(deployment)
        if annotations.get(_ACTION_ANNOTATION) == action_id:
            return ExecutionResult({"operation": "restart", "already_applied": True})
        previous_generation = int(deployment.metadata.generation or 0)
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            _ACTION_ANNOTATION: action_id,
                            _RESTART_ANNOTATION: datetime.now(UTC).isoformat(),
                            "opsmind.io/requested-grace-period-seconds": str(
                                parameters["grace_period_seconds"]
                            ),
                        }
                    }
                }
            }
        }
        self._mutate(
            lambda dry_run: self.apps_api.patch_namespaced_deployment(
                name=name,
                namespace=self.namespace,
                body=patch,
                dry_run=dry_run,
                field_manager="opsmind-remediation",
                _request_timeout=self.request_timeout_seconds,
            )
        )
        return ExecutionResult(
            {
                "operation": "restart",
                "deployment": name,
                "previous_generation": previous_generation,
                "dry_run": self.dry_run,
            }
        )

    def _scale(
        self,
        name: str,
        deployment: client.V1Deployment,
        parameters: dict[str, object],
    ) -> ExecutionResult:
        replicas = parameters["replicas"]
        if not isinstance(replicas, int):
            raise ValueError("Validated replica count must be an integer")
        return self._scale_to(name, deployment, replicas=replicas)

    def _scale_to(
        self,
        name: str,
        deployment: client.V1Deployment,
        replicas: int,
    ) -> ExecutionResult:
        previous_replicas = int(deployment.spec.replicas or 0) if deployment.spec else 0
        patch = {"spec": {"replicas": replicas}}
        self._mutate(
            lambda dry_run: self.apps_api.patch_namespaced_deployment_scale(
                name=name,
                namespace=self.namespace,
                body=patch,
                dry_run=dry_run,
                field_manager="opsmind-remediation",
                _request_timeout=self.request_timeout_seconds,
            )
        )
        return ExecutionResult(
            {
                "operation": "scale",
                "deployment": name,
                "previous_replicas": previous_replicas,
                "replicas": replicas,
                "dry_run": self.dry_run,
            }
        )

    def _rollback_revision(
        self,
        name: str,
        deployment: client.V1Deployment,
        action_id: str,
        target_revision: str,
    ) -> ExecutionResult:
        annotations = _template_annotations(deployment)
        current_revision = _annotations(deployment).get("deployment.kubernetes.io/revision")
        if annotations.get(_ACTION_ANNOTATION) == action_id:
            return ExecutionResult(
                {
                    "operation": "rollback_deployment",
                    "target_revision": target_revision,
                    "already_applied": True,
                }
            )
        selector = _label_selector(deployment)
        replica_sets = self._call(
            lambda: self.apps_api.list_namespaced_replica_set(
                namespace=self.namespace,
                label_selector=selector,
                limit=100,
                _request_timeout=self.request_timeout_seconds,
            )
        )
        deployment_uid = deployment.metadata.uid if deployment.metadata else None
        target = next(
            (
                replica_set
                for replica_set in replica_sets.items
                if _owned_by(replica_set, deployment_uid)
                and _annotations(replica_set).get("deployment.kubernetes.io/revision")
                == target_revision
            ),
            None,
        )
        if target is None or target.spec is None or target.spec.template is None:
            raise ValueError(f"Deployment revision {target_revision!r} was not found")
        template = client.ApiClient().sanitize_for_serialization(target.spec.template)
        template_metadata = template.setdefault("metadata", {})
        template_annotations = template_metadata.get("annotations") or {}
        template_metadata["annotations"] = template_annotations
        template_annotations[_ACTION_ANNOTATION] = action_id
        self._mutate(
            lambda dry_run: self.apps_api.patch_namespaced_deployment(
                name=name,
                namespace=self.namespace,
                body={"spec": {"template": template}},
                dry_run=dry_run,
                field_manager="opsmind-remediation",
                _request_timeout=self.request_timeout_seconds,
            )
        )
        return ExecutionResult(
            {
                "operation": "rollback_deployment",
                "deployment": name,
                "previous_revision": current_revision or "",
                "target_revision": target_revision,
                "dry_run": self.dry_run,
            }
        )

    def _read_deployment(self, name: str) -> client.V1Deployment:
        return self._call(
            lambda: self.apps_api.read_namespaced_deployment(
                name=name,
                namespace=self.namespace,
                _request_timeout=self.request_timeout_seconds,
            )
        )

    def _deployment_name(self, target: str) -> str:
        _validate_name(target, "deployment target")
        return target

    def _mutate(self, operation: Callable[[str | None], Any]) -> Any:
        return self._call(lambda: operation("All" if self.dry_run else None))

    def _call(self, operation: Callable[[], Any]) -> Any:
        """Retry transient API/network failures with bounded exponential backoff."""
        for attempt in range(self.max_retries + 1):
            try:
                return operation()
            except ApiException as exc:
                if exc.status not in _RETRYABLE_STATUS or attempt == self.max_retries:
                    raise
                retry_after = _retry_after(exc)
            except (HTTPError, TimeoutError, OSError):
                if attempt == self.max_retries:
                    raise
                retry_after = None
            delay = (
                retry_after
                if retry_after is not None
                else self.retry_backoff_seconds * (2**attempt)
            )
            self._sleep(min(delay, 10.0))
        raise RuntimeError("Kubernetes request retry loop exited unexpectedly")


def _validate_name(value: str, label: str) -> None:
    if len(value) > 253 or not _K8S_NAME.fullmatch(value):
        raise ValueError(f"{label} must be a Kubernetes DNS name")


def _annotations(resource: Any) -> dict[str, str]:
    metadata = getattr(resource, "metadata", None)
    return dict(metadata.annotations or {}) if metadata else {}


def _template_annotations(deployment: client.V1Deployment) -> dict[str, str]:
    template_metadata = deployment.spec.template.metadata if deployment.spec else None
    return dict(template_metadata.annotations or {}) if template_metadata else {}


def _label_selector(deployment: client.V1Deployment) -> str:
    labels = deployment.spec.selector.match_labels if deployment.spec else None
    if not labels:
        raise ValueError("Deployment must have a non-empty matchLabels selector")
    return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))


def _owned_by(replica_set: client.V1ReplicaSet, deployment_uid: str | None) -> bool:
    if deployment_uid is None or replica_set.metadata is None:
        return False
    return any(
        owner.uid == deployment_uid and owner.kind == "Deployment"
        for owner in (replica_set.metadata.owner_references or [])
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _retry_after(error: ApiException) -> float | None:
    if not error.headers:
        return None
    raw_value = error.headers.get("Retry-After")
    try:
        return max(0.0, float(raw_value)) if raw_value is not None else None
    except ValueError:
        return None
