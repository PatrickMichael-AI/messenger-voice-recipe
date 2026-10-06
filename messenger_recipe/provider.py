"""Lossless Vapi JSON transport, independent of business settings and state paths.

Use the documented HTTP endpoints through httpx: preserve explicit nulls/empty
lists, unknown response fields and provider evidence. No Vapi SDK is required.
No transport retry is enabled, especially for non-idempotent POST requests.

Ten retained dashboard runtime fields and voice.inputMinCharacters are absent
from current request documentation. This does not establish that the API rejects
them. Serialization is checked offline; API acceptance and behavior remain the
Phase 5 live gate (README).
"""

from __future__ import annotations

import re
from typing import Any

import httpx


class ProviderError(RuntimeError):
    """A request failed; a mutation may still have reached the provider."""

    def __init__(self, message: str, *, evidence: dict | None = None):
        super().__init__(message)
        # Kept out of printed errors; callers may save this in private state.
        self.evidence = evidence


def resource_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Resource ID must be a non-empty safe path segment.")
    return value


class VapiProvider:
    """Six documented operations; transport may be injected for offline tests."""

    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("VAPI_API_KEY is required.")
        self._client = httpx.Client(
            base_url="https://api.vapi.ai/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        try:
            response = self._client.request(method, path, json=payload)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Preserve validation details privately without printing provider
            # content, request payloads or authorization into terminal logs.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            failed_response = getattr(exc, "response", None)
            evidence = None
            if failed_response is not None:
                try:
                    body = failed_response.json()
                except ValueError:
                    body = failed_response.text
                evidence = {"method": method, "path": path, "status": status, "body": body}
            raise ProviderError(f"Vapi {method} {path} failed (status {status}).",
                                evidence=evidence) from exc
        if not isinstance(data, dict):
            raise ProviderError(f"Vapi {method} {path} returned a non-object response.")
        return data

    def create_assistant(self, payload: dict[str, Any]) -> dict:
        return self._request("POST", "assistant", payload)

    def update_assistant(self, assistant_id: str, payload: dict[str, Any]) -> dict:
        return self._request("PATCH", f"assistant/{resource_id(assistant_id)}", payload)

    def get_assistant(self, assistant_id: str) -> dict:
        return self._request("GET", f"assistant/{resource_id(assistant_id)}")

    def create_output(self, payload: dict[str, Any]) -> dict:
        return self._request("POST", "structured-output", payload)

    def get_output(self, output_id: str) -> dict:
        return self._request("GET", f"structured-output/{resource_id(output_id)}")

    def get_call(self, call_id: str) -> dict:
        return self._request("GET", f"call/{resource_id(call_id)}")
