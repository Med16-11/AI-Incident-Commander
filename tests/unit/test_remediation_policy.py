"""Remediation allowlist, risk classification, and parameter policy tests."""

from uuid import uuid4

import pytest
from opsmind_models.domain.remediation import RemediationRisk
from opsmind_remediation_engine.policy import RemediationPolicyEngine, RemediationPolicyError


def test_action_risk_classifications() -> None:
    policy = RemediationPolicyEngine()
    assert policy.classify("restart_service") is RemediationRisk.LOW_RISK
    assert policy.classify("clear_cache") is RemediationRisk.LOW_RISK
    assert policy.classify("scale_service") is RemediationRisk.MEDIUM_RISK
    assert policy.classify("rollback_deployment") is RemediationRisk.MEDIUM_RISK
    assert policy.classify("disable_feature_flag") is RemediationRisk.HIGH_RISK
    assert policy.classify("database_modification") is RemediationRisk.HIGH_RISK


def test_policy_rejects_unlisted_actions_and_unsafe_arguments() -> None:
    policy = RemediationPolicyEngine()
    with pytest.raises(RemediationPolicyError, match="not executable"):
        policy.validate("database_modification", "db-primary", {})
    with pytest.raises(RemediationPolicyError, match="parameters"):
        policy.validate("scale_service", "payment", {"replicas": 0})
    with pytest.raises(RemediationPolicyError, match="parameters"):
        policy.validate("restart_service", "payment", {"command": "rm -rf /"})


def test_action_digest_is_stable_and_parameters_are_normalized() -> None:
    policy = RemediationPolicyEngine()
    first = policy.validate("restart_service", "payment", {})
    second = policy.validate("restart_service", "payment", {"grace_period_seconds": 30})
    assert first.digest == second.digest
    assert first.parameters.model_dump() == {"grace_period_seconds": 30}


def test_rollback_deployment_accepts_typed_uuid_from_json_request() -> None:
    policy = RemediationPolicyEngine()
    deployment_id = uuid4()
    result = policy.validate(
        "rollback_deployment",
        "payment",
        {"deployment_id": str(deployment_id), "target_version": "v1.2.3"},
    )
    assert result.risk_level is RemediationRisk.MEDIUM_RISK
    assert result.parameters.model_dump(mode="json")["deployment_id"] == str(deployment_id)
