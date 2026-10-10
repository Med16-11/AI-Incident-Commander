"""LLM transport failures are normalized and never leak provider details."""

import httpx
import pytest
from opsmind_common.urls import validate_outbound_base_url
from opsmind_incident_engine.investigation import (
    ConversationMessage,
    ProviderFailure,
    ProviderRequest,
    ToolDefinition,
)
from opsmind_incident_engine.providers import OpenAICompatibleProvider


def _request() -> ProviderRequest:
    return ProviderRequest(
        messages=[ConversationMessage(role="user", content="Investigate safely")],
        tools=[
            ToolDefinition(
                name="search_logs",
                description="Search logs",
                arguments_schema={"type": "object", "properties": {}},
            )
        ],
        response_schema={"type": "object"},
    )


def test_llm_network_timeout_becomes_provider_failure() -> None:
    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated upstream timeout")

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        provider = OpenAICompatibleProvider(
            client, base_url="https://llm.example", api_key="test-key", model="test-model"
        )
        with pytest.raises(ProviderFailure, match="provider request failed"):
            provider.complete(_request())


def test_malformed_provider_response_envelope_becomes_provider_failure() -> None:
    def malformed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with httpx.Client(transport=httpx.MockTransport(malformed)) as client:
        provider = OpenAICompatibleProvider(
            client, base_url="https://llm.example", api_key="test-key", model="test-model"
        )
        with pytest.raises(ProviderFailure, match="invalid response envelope"):
            provider.complete(_request())


@pytest.mark.parametrize(
    "url",
    [
        "http://llm.example",
        "https://user:password@llm.example",
        "https://llm.example/path?token=secret",
        "https://llm.example/#fragment",
    ],
)
def test_outbound_provider_urls_reject_cleartext_or_credential_injection(url: str) -> None:
    with pytest.raises(ValueError):
        validate_outbound_base_url(url, name="LLM URL")


def test_local_mode_may_explicitly_allow_http_without_url_credentials() -> None:
    assert (
        validate_outbound_base_url("http://llm:8000/v1/", name="LLM URL", allow_insecure_http=True)
        == "http://llm:8000/v1"
    )
