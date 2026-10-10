# Slack integration

The notification service subscribes to `opsmind:events` with its own Redis Streams consumer group. An `IncidentCreated` event posts a root message to the configured incident channel; subsequent supported incident events become replies in that incident thread. Thread timestamps are stored in Redis so notification replicas and restarts continue the same thread. The stream inbox makes event handling restart-safe, and failed Slack deliveries use the shared retry and dead-letter behavior.

## Configure a Slack app

1. Create a Slack app for the workspace and install it with the `chat:write` bot scope.
2. Copy the bot token, signing secret, and destination channel ID into a local `.env` or secret manager. Invite the bot to that channel.
3. Set `OPSMIND_SLACK_USER_TOKENS` to a JSON object mapping each authorized Slack identity (`team_id:slack_user_id`) to that person's existing bearer token accepted by the Remediation API. This is a credential mapping, not account provisioning. Use short-lived, narrowly scoped credentials where the configured identity provider supports them.
4. Expose `https://<your-host>/slack/interactions` over HTTPS and configure it as the Slack app's Interactivity Request URL. For local development, use a trusted HTTPS tunnel and do not expose the callback without a signing secret.
5. Restart Compose. The notification service logs whether Slack delivery/actions are enabled without printing credential values.

Example `.env` keys (values intentionally omitted):

```dotenv
OPSMIND_SLACK_BOT_TOKEN=
OPSMIND_SLACK_SIGNING_SECRET=
OPSMIND_SLACK_INCIDENT_CHANNEL=C0123456789
OPSMIND_SLACK_USER_TOKENS={"T01234567:U01234567":"<existing-user-bearer-token>"}
```

The bot token and signing secret must remain private. Do not commit populated `.env` files. The identity map must only contain existing users who have the appropriate `remediations:approve` permission in the configured Remediation API authentication backend. That API performs authorization, prevents self-approval, checks expiration, and writes the audit record. Slack buttons only carry the remediation UUID; the callback verifies Slack's HMAC signature over the exact request body, rejects stale timestamps, validates the action allowlist, resolves the workspace/user mapping, and forwards the decision with that user's bearer token.

The Remediation API callback currently records a fixed reason identifying the Slack interactive action. Its existing policy and audit workflow remains authoritative. The callback never accepts a principal ID, target, risk level, or arbitrary API path from the Slack payload.

## Event coverage

The notification handler formats incident creation, incident updates, investigation start, evidence collection, hypothesis generation, remediation proposal/approval/start/result, and resolution events. A `RemediationProposed` event with an `action_id` includes Approve and Reject buttons. The API accepts only those two action identifiers. Unknown or unrelated event types are ignored.

The service consumes events already present on the shared stream. Producers must publish these lifecycle event types with an `aggregate_id` (or `incident_id` in the payload) for Slack to display them. Slack delivery is disabled when the bot token or incident channel is unset; interactive actions remain disabled unless a signing secret, identity map, and Remediation API URL are configured.
