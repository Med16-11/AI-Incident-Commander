"""Slack Web API adapter, incident event formatting, and signed interactive actions."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any, Literal, Protocol
from uuid import UUID

import httpx
from fastapi import HTTPException
from opsmind_common.urls import validate_outbound_base_url
from opsmind_events.contracts import EventHandler, EventUnitOfWork
from opsmind_models.events import EventEnvelope, EventPayload, OpsMindEventType
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError


class SlackAdapter(Protocol):
    """Replaceable port for posting messages to a Slack conversation."""

    def post_message(
        self,
        *,
        channel: str,
        text: str,
        blocks: list[dict[str, Any]],
        client_msg_id: str,
        thread_ts: str | None = None,
    ) -> str: ...


class SlackWebApiAdapter:
    """Small synchronous Slack Web API client with bounded network timeouts."""

    def __init__(self, token: str, *, timeout_seconds: float = 5.0) -> None:
        if not token:
            raise ValueError("Slack bot token is required")
        self._client = httpx.Client(
            base_url="https://slack.com/api/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(timeout_seconds),
        )

    def post_message(
        self,
        *,
        channel: str,
        text: str,
        blocks: list[dict[str, Any]],
        client_msg_id: str,
        thread_ts: str | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "channel": channel,
            "text": text,
            "blocks": blocks,
            "client_msg_id": client_msg_id,
        }
        if thread_ts is not None:
            body["thread_ts"] = thread_ts
        response = self._client.post("chat.postMessage", json=body)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            error = (
                result.get("error", "unknown_error")
                if isinstance(result, dict)
                else "invalid_response"
            )
            raise RuntimeError(f"Slack rejected chat.postMessage: {error}")
        timestamp = result.get("ts")
        if not isinstance(timestamp, str) or not timestamp:
            raise RuntimeError("Slack chat.postMessage response omitted its timestamp")
        return timestamp

    def close(self) -> None:
        self._client.close()


class SlackThreadStore(Protocol):
    def get_thread(self, incident_id: UUID) -> str | None: ...

    def set_thread(self, incident_id: UUID, thread_ts: str) -> None: ...


class SlackIncidentEventHandler(EventHandler):
    """Format domain events as a root incident message followed by threaded updates."""

    def __init__(self, adapter: SlackAdapter, threads: SlackThreadStore, channel: str) -> None:
        if not channel:
            raise ValueError("Slack incident channel is required")
        self._adapter = adapter
        self._threads = threads
        self._channel = channel

    def __call__(self, event: EventEnvelope[EventPayload], _uow: EventUnitOfWork) -> None:
        incident_id = event.aggregate_id or _uuid_from_payload(event.payload)
        if incident_id is None:
            # Non-incident stream events are intentionally ignored by this subscriber.
            return
        if event.event_type not in {
            OpsMindEventType.INCIDENT_CREATED,
            OpsMindEventType.INCIDENT_UPDATED,
            OpsMindEventType.INVESTIGATION_STARTED,
            OpsMindEventType.EVIDENCE_COLLECTED,
            OpsMindEventType.HYPOTHESIS_GENERATED,
            OpsMindEventType.REMEDIATION_PROPOSED,
            OpsMindEventType.REMEDIATION_APPROVED,
            OpsMindEventType.REMEDIATION_STARTED,
            OpsMindEventType.REMEDIATION_COMPLETED,
            OpsMindEventType.REMEDIATION_FAILED,
            OpsMindEventType.INCIDENT_RESOLVED,
        }:
            return
        payload = event.payload.model_dump(mode="json")
        if event.event_type == OpsMindEventType.INCIDENT_CREATED:
            service = _text(payload, "service", "unknown service")
            score = _number(payload, "score", 0)
            severity = _severity(score)
            detected = _text(payload, "first_seen_at", event.timestamp.isoformat())
            impact = _text(payload, "summary", f"Degradation detected in {service}.")
            text = (
                "🚨 Incident detected\n"
                f"Service: {service}\nSeverity: {severity}\n"
                f"Detected: {detected}\nCurrent impact: {impact}"
            )
            blocks = [_section(text)]
            thread_ts = None
        else:
            thread_ts = self._threads.get_thread(incident_id)
            if thread_ts is None:
                # Retry until the creation event establishes the incident thread.
                raise RuntimeError("Slack incident thread has not been created yet")
            text, blocks = _update_message(event, payload)

        posted_ts = self._adapter.post_message(
            channel=self._channel,
            text=text,
            blocks=blocks,
            client_msg_id=str(event.event_id),
            thread_ts=thread_ts,
        )
        if thread_ts is None:
            self._threads.set_thread(incident_id, posted_ts)


class SlackDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action_id: UUID
    decision: Literal["approve", "reject"]
    slack_user_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)


class SlackIdentityResolver(Protocol):
    """Map a verified Slack identity to credentials for an existing OpsMind user."""

    def resolve(self, team_id: str, slack_user_id: str) -> SecretStr: ...


class RemediationDecisionPort(Protocol):
    """Delegate decisions to the same authenticated remediation approval boundary."""

    def decide(self, decision: SlackDecision, user_token: SecretStr) -> None: ...


class SlackUserTokenMap:
    """Resolve Slack team/user pairs to existing Remediation API bearer tokens."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._tokens = {key: SecretStr(value) for key, value in values.items() if value}

    def resolve(self, team_id: str, slack_user_id: str) -> SecretStr:
        token = self._tokens.get(f"{team_id}:{slack_user_id}")
        if token is None:
            raise KeyError("Slack identity is not mapped")
        return token


class HttpRemediationDecisionPort:
    """Delegate button decisions to the existing authorization-protected API."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
        allow_insecure_http: bool = False,
    ) -> None:
        self._base_url = validate_outbound_base_url(
            base_url,
            name="Slack remediation API URL",
            allow_insecure_http=allow_insecure_http,
        )
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_seconds), transport=transport)

    def decide(self, decision: SlackDecision, user_token: SecretStr) -> None:
        response = self._client.post(
            f"{self._base_url}/remediations/{decision.action_id}/{decision.decision}",
            headers={"Authorization": f"Bearer {user_token.get_secret_value()}"},
            json={"reason": decision.reason},
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Remediation API rejected Slack decision with HTTP {response.status_code}"
            )

    def close(self) -> None:
        self._client.close()


class SlackInteractionService:
    """Verify Slack signatures, validate action payloads, then delegate decisions."""

    def __init__(
        self,
        signing_secret: str,
        identities: SlackIdentityResolver,
        decisions: RemediationDecisionPort,
        *,
        clock: Any = time.time,
        max_age_seconds: int = 300,
    ) -> None:
        if not signing_secret:
            raise ValueError("Slack signing secret is required")
        self._secret = signing_secret.encode()
        self._identities = identities
        self._decisions = decisions
        self._clock = clock
        self._max_age_seconds = max_age_seconds

    def handle(self, body: bytes, timestamp: str | None, signature: str | None) -> None:
        if not timestamp or len(timestamp) > 12 or not signature or not timestamp.isdigit():
            raise HTTPException(401, "Invalid Slack signature headers")
        request_time = int(timestamp)
        if abs(self._clock() - request_time) > self._max_age_seconds:
            raise HTTPException(401, "Slack request timestamp expired")
        base = b"v0:" + timestamp.encode() + b":" + body
        expected = "v0=" + hmac.new(self._secret, base, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(401, "Invalid Slack request signature")
        try:
            form = httpx.QueryParams(body.decode("utf-8"))
            raw_payload = form.get("payload")
            if raw_payload is None:
                raise ValueError("missing payload")
            payload = json.loads(raw_payload)
            decision, team_id = _parse_interaction(payload)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            raise HTTPException(400, "Malformed Slack interaction payload") from exc
        try:
            user_token = self._identities.resolve(team_id, decision.slack_user_id)
        except (KeyError, LookupError) as exc:
            raise HTTPException(
                403, "Slack user is not authorized for remediation decisions"
            ) from exc
        self._decisions.decide(decision, user_token)


def _parse_interaction(payload: object) -> tuple[SlackDecision, str]:
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be an object")
    if payload.get("type") != "block_actions":
        raise ValueError("unsupported interaction type")
    team = payload.get("team")
    user = payload.get("user")
    actions = payload.get("actions")
    if not isinstance(team, Mapping) or not isinstance(team.get("id"), str):
        raise ValueError("missing team identity")
    if not isinstance(user, Mapping) or not isinstance(user.get("id"), str):
        raise ValueError("missing user identity")
    if not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], Mapping):
        raise ValueError("expected exactly one action")
    action = actions[0]
    action_name = action.get("action_id")
    if not isinstance(action_name, str):
        raise ValueError("unsupported action")
    if action_name == "opsmind_approve":
        decision_name: Literal["approve", "reject"] = "approve"
    elif action_name == "opsmind_reject":
        decision_name = "reject"
    else:
        raise ValueError("unsupported action")
    if not isinstance(action.get("value"), str):
        raise ValueError("unsupported action")
    decision = SlackDecision(
        action_id=UUID(action["value"]),
        decision=decision_name,
        slack_user_id=user["id"],
        reason="Decision submitted from the authenticated Slack incident action.",
    )
    return decision, team["id"]


def remediation_approval_blocks(
    action_id: UUID, *, target: str, risk: str, reason: str
) -> list[dict[str, Any]]:
    """Create Slack controls whose value contains only an opaque action UUID."""
    return [
        _section(f"Approval requested\nTarget: {target}\nRisk: {risk}\nReason: {reason}"),
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "opsmind_approve",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "value": str(action_id),
                    "confirm": _confirm("Approve this remediation?"),
                },
                {
                    "type": "button",
                    "action_id": "opsmind_reject",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "value": str(action_id),
                    "confirm": _confirm("Reject this remediation?"),
                },
            ],
        },
    ]


def _update_message(
    event: EventEnvelope[EventPayload], payload: dict[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    event_type = str(event.event_type)
    fields = {
        "IncidentUpdated": "Incident update",
        "InvestigationStarted": "Investigation started",
        "EvidenceCollected": "Evidence discovered",
        "HypothesisGenerated": "Current hypothesis",
        "RemediationProposed": "Recommended remediation",
        "RemediationApproved": "Remediation approved",
        "RemediationStarted": "Remediation started",
        "RemediationCompleted": "Remediation result: completed",
        "RemediationFailed": "Remediation result: failed",
        "IncidentResolved": "Incident resolved",
    }
    heading = fields.get(event_type)
    if heading is None:
        return "", []
    details = _summarize_payload(payload)
    text = f"{heading}\n{details}" if details else heading
    blocks = [_section(text)]
    if event_type == "RemediationProposed":
        action_id = _uuid_value(payload.get("action_id"))
        if action_id is not None:
            blocks.extend(
                remediation_approval_blocks(
                    action_id,
                    target=_text(payload, "target", "unknown target"),
                    risk=_text(payload, "risk_level", "unknown"),
                    reason=_text(payload, "reason", _text(payload, "rationale", "")),
                )[1:]
            )
    return text, blocks


def _summarize_payload(payload: Mapping[str, Any]) -> str:
    preferred = (
        "summary",
        "message",
        "hypothesis",
        "confidence",
        "reason",
        "target",
        "risk_level",
        "result",
    )
    parts: list[str] = []
    for key in preferred:
        value = payload.get(key)
        if value is not None and isinstance(value, (str, int, float, bool)):
            label = key.replace("_", " ").capitalize()
            parts.append(f"{label}: {_escape_slack(str(value)[:1000])}")
    return "\n".join(parts)


def _uuid_from_payload(payload: EventPayload) -> UUID | None:
    values = payload.model_dump()
    return _uuid_value(values.get("incident_id"))


def _uuid_value(value: object) -> UUID | None:
    if not isinstance(value, (str, UUID)):
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _text(payload: Mapping[str, Any], key: str, default: str) -> str:
    value = payload.get(key)
    return _escape_slack(str(value)[:2000]) if value is not None else default


def _escape_slack(value: str) -> str:
    """Escape Slack control syntax so event data cannot create mentions or links."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _number(payload: Mapping[str, Any], key: str, default: int) -> int:
    value = payload.get(key)
    return value if isinstance(value, int) else default


def _severity(score: int) -> str:
    if score >= 85:
        return "SEV-1"
    if score >= 70:
        return "SEV-2"
    if score >= 50:
        return "SEV-3"
    return "SEV-4"


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "plain_text", "text": text[:3000]}}


def _confirm(text: str) -> dict[str, Any]:
    return {
        "title": {"type": "plain_text", "text": "Confirm decision"},
        "text": {"type": "mrkdwn", "text": text},
        "confirm": {"type": "plain_text", "text": "Confirm"},
        "deny": {"type": "plain_text", "text": "Cancel"},
    }
