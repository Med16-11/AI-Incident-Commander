"""Provider adapters for structured function calling."""

from __future__ import annotations

import json
from typing import Any

import httpx
from opsmind_common.urls import validate_outbound_base_url

from opsmind_incident_engine.investigation import (
    ConversationMessage,
    ProviderFailure,
    ProviderRequest,
    ProviderTurn,
    ToolCall,
)


class OpenAICompatibleProvider:
    """HTTP Chat Completions adapter; endpoint and model are environment configured."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        allow_insecure_http: bool = False,
    ) -> None:
        validated_base_url = validate_outbound_base_url(
            base_url, name="LLM base URL", allow_insecure_http=allow_insecure_http
        )
        if not api_key.strip() or not model.strip():
            raise ValueError("LLM API key and model must be configured")
        self._client = client
        self._url = f"{validated_base_url}/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    def complete(self, request: ProviderRequest) -> ProviderTurn:
        """Request one structured response and normalize only tool calls or final content."""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [self._message(message) for message in request.messages],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.arguments_schema,
                    },
                }
                for tool in request.tools
            ],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "incident_analysis",
                    "strict": True,
                    "schema": request.response_schema,
                },
            },
        }
        try:
            response = self._client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
                timeout=self._timeout,
            )
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderFailure("LLM provider request failed") from exc
        try:
            message = document["choices"][0]["message"]
            content = message.get("content")
            calls = [
                ToolCall(
                    id=str(call["id"]),
                    name=str(call["function"]["name"]),
                    arguments=self._arguments(call["function"].get("arguments")),
                )
                for call in message.get("tool_calls", [])
            ]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderFailure("LLM provider returned an invalid response envelope") from exc
        if content is not None and not isinstance(content, str):
            raise ProviderFailure("LLM provider returned non-text structured output")
        return ProviderTurn(content=content, tool_calls=calls)

    @staticmethod
    def _arguments(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return json.dumps(value, separators=(",", ":"))
        raise ProviderFailure("LLM provider returned invalid tool arguments")

    @staticmethod
    def _message(message: ConversationMessage) -> dict[str, Any]:
        value: dict[str, Any] = {"role": message.role}
        if message.content is not None:
            value["content"] = message.content
        if message.tool_call_id is not None:
            value["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            value["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in message.tool_calls
            ]
        return value
