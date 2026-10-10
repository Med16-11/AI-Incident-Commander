"""Mock integration coverage for Slack delivery and signed approval callbacks."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from opsmind_common.config import Settings
from opsmind_events.contracts import EventUnitOfWork
from opsmind_models.events import EventEnvelope, EventPayload, OpsMindEventType
from opsmind_notification_service.main import create_app
from opsmind_notification_service.slack import (
    HttpRemediationDecisionPort,
    RemediationDecisionPort,
    SlackDecision,
    SlackIdentityResolver,
    SlackIncidentEventHandler,
    SlackInteractionService,
    _section,
    _text,
)
from pydantic import SecretStr


class FakeAdapter:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    def post_message(
        self,
        *,
        channel: str,
        text: str,
        blocks: list[dict[str, object]],
        client_msg_id: str,
        thread_ts: str | None = None,
    ) -> str:
        self.messages.append(
            {
                "channel": channel,
                "text": text,
                "blocks": blocks,
                "client_msg_id": client_msg_id,
                "thread_ts": thread_ts,
            }
        )
        return f"{len(self.messages)}.0001"


class FakeThreads:
    def __init__(self) -> None:
        self.items: dict[UUID, str] = {}

    def get_thread(self, incident_id: UUID) -> str | None:
        return self.items.get(incident_id)

    def set_thread(self, incident_id: UUID, thread_ts: str) -> None:
        self.items[incident_id] = thread_ts


def _event(event_type: OpsMindEventType, payload: dict[str, object]) -> EventEnvelope[EventPayload]:
    return EventEnvelope[EventPayload](
        event_type=event_type,
        timestamp=datetime.now(UTC),
        correlation_id="test-correlation",
        source="test",
        aggregate_type="incident",
        aggregate_id=UUID(str(payload.get("incident_id", uuid4()))),
        payload=EventPayload.model_validate(payload),
    )


def test_incident_creation_and_updates_use_persistent_thread_port() -> None:
    adapter = FakeAdapter()
    threads = FakeThreads()
    handler = SlackIncidentEventHandler(adapter, threads, "C-INCIDENTS")
    incident_id = uuid4()
    created = EventEnvelope[EventPayload](
        event_type=OpsMindEventType.INCIDENT_CREATED,
        correlation_id="correlation-1",
        source="detection-engine",
        aggregate_type="incident",
        aggregate_id=incident_id,
        payload=EventPayload.model_validate(
            {
                "incident_id": str(incident_id),
                "service": "payments",
                "score": 88,
                "first_seen_at": "2026-10-06T10:00:00Z",
                "summary": "Payment failures are elevated",
            }
        ),
    )
    handler(created, cast(EventUnitOfWork, object()))
    assert "🚨 Incident detected" in str(adapter.messages[0]["text"])
    assert "SEV-1" in str(adapter.messages[0]["text"])
    assert adapter.messages[0]["thread_ts"] is None
    assert threads.get_thread(incident_id) == "1.0001"

    update = EventEnvelope[EventPayload](
        event_type=OpsMindEventType.HYPOTHESIS_GENERATED,
        correlation_id="correlation-1",
        source="incident-engine",
        aggregate_type="incident",
        aggregate_id=incident_id,
        payload=EventPayload.model_validate(
            {"incident_id": str(incident_id), "hypothesis": "Recent deployment", "confidence": 0.82}
        ),
    )
    handler(update, cast(EventUnitOfWork, object()))
    assert adapter.messages[1]["thread_ts"] == "1.0001"
    assert "Current hypothesis" in str(adapter.messages[1]["text"])


def test_slack_delivery_timeout_does_not_mark_thread_as_created() -> None:
    class UnavailableAdapter(FakeAdapter):
        def post_message(self, **_kwargs: object) -> str:
            raise httpx.ReadTimeout("Slack API timed out")

    threads = FakeThreads()
    incident_id = uuid4()
    event = EventEnvelope[EventPayload](
        event_type=OpsMindEventType.INCIDENT_CREATED,
        correlation_id="slack-timeout",
        source="test",
        aggregate_type="incident",
        aggregate_id=incident_id,
        payload=EventPayload.model_validate(
            {"incident_id": str(incident_id), "service": "payment", "score": 90}
        ),
    )

    with pytest.raises(httpx.ReadTimeout):
        SlackIncidentEventHandler(UnavailableAdapter(), threads, "C-INCIDENTS")(
            event, cast(EventUnitOfWork, object())
        )
    assert threads.get_thread(incident_id) is None

    recovered_adapter = FakeAdapter()
    SlackIncidentEventHandler(recovered_adapter, threads, "C-INCIDENTS")(
        event, cast(EventUnitOfWork, object())
    )
    assert threads.get_thread(incident_id) == "1.0001"


class FakeIdentities(SlackIdentityResolver):
    def __init__(self, known_user: str, user_token: str) -> None:
        self.known_user = known_user
        self.user_token = SecretStr(user_token)

    def resolve(self, team_id: str, slack_user_id: str) -> SecretStr:
        if team_id != "T1" or slack_user_id != self.known_user:
            raise KeyError(slack_user_id)
        return self.user_token


class FakeDecisions(RemediationDecisionPort):
    def __init__(self) -> None:
        self.calls: list[tuple[SlackDecision, SecretStr]] = []

    def decide(self, decision: SlackDecision, user_token: SecretStr) -> None:
        self.calls.append((decision, user_token))


def _signed_request(
    secret: str, payload: dict[str, object], timestamp: int
) -> tuple[bytes, str, str]:
    body = urlencode({"payload": json.dumps(payload, separators=(",", ":"))}).encode()
    ts = str(timestamp)
    digest = hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256)
    return body, ts, "v0=" + digest.hexdigest()


def _payload(*, user: str = "U1", action: str = "opsmind_approve") -> dict[str, object]:
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": user},
        "actions": [{"action_id": action, "value": str(uuid4())}],
    }


def test_signed_slack_decision_delegates_only_verified_identity_and_action() -> None:
    now = 1_800_000_000
    user_token = "existing-user-api-token"
    decisions = FakeDecisions()
    service = SlackInteractionService(
        "signing-secret",
        FakeIdentities("U1", user_token),
        decisions,
        clock=lambda: now,
    )
    action_payload = _payload()
    body, timestamp, signature = _signed_request("signing-secret", action_payload, now)
    service.handle(body, timestamp, signature)
    assert len(decisions.calls) == 1
    assert decisions.calls[0][0].decision == "approve"
    assert decisions.calls[0][1].get_secret_value() == user_token


def test_remediation_adapter_forwards_only_authenticated_decision_to_existing_api() -> None:
    action_id = uuid4()
    decision = SlackDecision(
        action_id=action_id,
        decision="reject",
        slack_user_id="U1",
        reason="Reject from Slack",
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "rejected"})

    port = HttpRemediationDecisionPort(
        "https://remediation.test",
        transport=httpx.MockTransport(respond),
    )
    port.decide(decision, SecretStr("mapped-existing-user-token"))
    assert len(requests) == 1
    assert requests[0].url.path == f"/remediations/{action_id}/reject"
    assert requests[0].headers["authorization"] == "Bearer mapped-existing-user-token"
    assert json.loads(requests[0].content) == {"reason": "Reject from Slack"}
    port.close()


def test_untrusted_slack_text_cannot_create_mentions_or_mrkdwn_controls() -> None:
    value = _text({"summary": "<!channel> <https://attacker.test|click> & hello"}, "summary", "")
    block = _section(value)
    assert "<!channel>" not in value
    assert "<https://attacker.test" not in value
    assert block["text"]["type"] == "plain_text"


def test_remediation_api_url_rejects_http_and_embedded_credentials() -> None:
    with pytest.raises(ValueError):
        HttpRemediationDecisionPort("http://remediation.test")
    with pytest.raises(ValueError):
        HttpRemediationDecisionPort("https://user:password@remediation.test")


@pytest.mark.parametrize("signature_kind", ["tampered", "stale", "missing"])
def test_slack_signature_failures_never_dispatch(signature_kind: str) -> None:
    now = 1_800_000_000
    decisions = FakeDecisions()
    service = SlackInteractionService(
        "signing-secret", FakeIdentities("U1", "token"), decisions, clock=lambda: now
    )
    timestamp = now - 301 if signature_kind == "stale" else now
    body, ts, signature = _signed_request("signing-secret", _payload(), timestamp)
    if signature_kind == "tampered":
        signature = "v0=" + "0" * 64
    if signature_kind == "missing":
        signature = ""
    with pytest.raises(HTTPException) as error:
        service.handle(body, ts, signature)
    assert error.value.status_code == 401
    assert decisions.calls == []


def test_unauthorized_slack_identity_and_unsupported_action_are_rejected() -> None:
    now = 1_800_000_000
    decisions = FakeDecisions()
    service = SlackInteractionService(
        "signing-secret", FakeIdentities("U1", "token"), decisions, clock=lambda: now
    )
    body, ts, signature = _signed_request("signing-secret", _payload(user="U-unmapped"), now)
    with pytest.raises(HTTPException) as error:
        service.handle(body, ts, signature)
    assert error.value.status_code == 403

    body, ts, signature = _signed_request(
        "signing-secret", _payload(action="arbitrary_operation"), now
    )
    with pytest.raises(HTTPException) as error:
        service.handle(body, ts, signature)
    assert error.value.status_code == 400
    assert decisions.calls == []


def test_slack_callback_http_requires_signed_payload() -> None:
    class FakeDecisionPort(FakeDecisions):
        pass

    decisions = FakeDecisionPort()
    settings = Settings(slack_signing_secret=SecretStr("signing-secret"))
    app = create_app(
        settings=settings,
        identity_resolver=FakeIdentities("U1", "token"),
        decision_port=decisions,
    )
    with TestClient(app) as client:
        response = client.post("/slack/interactions", data={"payload": json.dumps(_payload())})
        assert response.status_code == 401
        assert decisions.calls == []


def test_slack_callback_http_accepts_valid_signature_and_dispatches() -> None:
    decisions = FakeDecisions()
    settings = Settings(slack_signing_secret=SecretStr("signing-secret"))
    app = create_app(
        settings=settings,
        identity_resolver=FakeIdentities("U1", "mapped-token"),
        decision_port=decisions,
    )
    body, timestamp, signature = _signed_request(
        "signing-secret", _payload(), int(datetime.now(UTC).timestamp())
    )
    with TestClient(app) as client:
        response = client.post(
            "/slack/interactions",
            content=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Slack-Request-Timestamp": timestamp,
                "X-Slack-Signature": signature,
            },
        )
    assert response.status_code == 200
    assert len(decisions.calls) == 1
