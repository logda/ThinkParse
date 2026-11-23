"""The single HTTP egress to the ThinkParse /api/v2 gateway.

Every outbound call goes through ThinkParseClient: tools never assemble URLs
themselves (spec §3). Transport failures and 5xx responses are retried once
with a backoff; anything else is mapped to UpstreamError for the tools layer.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from .config import Settings


class UpstreamError(Exception):
    """A non-recoverable or retried-out response from ThinkParse."""

    def __init__(self, status: int, detail: str, retryable: bool) -> None:
        super().__init__(f"ThinkParse returned {status}: {detail}")
        self.status = status
        self.detail = detail
        self.retryable = retryable


_RETRIED_STATUS = 0  # Reported when the connection itself never succeeded.


class ThinkParseClient:
    """HTTP client bound to one ThinkParse gateway."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_backoff_s: float = 1.0,
    ) -> None:
        self._settings = settings
        self._backoff = retry_backoff_s
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            headers={"Authorization": f"Bearer {settings.api_key}"},
            trust_env=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            timeout=httpx.Timeout(60.0, connect=5.0),
            transport=transport,
        )

    async def __aenter__(self) -> ThinkParseClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        content_factory: Callable[[], AsyncIterator[bytes]] | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        attempts = 0
        while True:
            connect_error: Exception | None = None
            response: httpx.Response | None = None
            try:
                response = await self._http.request(
                    method,
                    path,
                    json=json,
                    content=None if content_factory is None else content_factory(),
                    headers=headers,
                    params=params,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                connect_error = exc

            failed = connect_error is not None or (response is not None and response.status_code >= 500)
            if not failed:
                break
            attempts += 1
            if attempts > 1:
                if connect_error is not None:
                    raise UpstreamError(_RETRIED_STATUS, str(connect_error), True) from connect_error
                assert response is not None
                raise UpstreamError(response.status_code, _detail(response), True)
            await asyncio.sleep(self._backoff)

        assert response is not None
        if response.is_error:
            raise UpstreamError(response.status_code, _detail(response), False)
        return response

    async def get_tiers(self) -> dict[str, Any]:
        response = await self._request("GET", "/api/v2/tiers")
        body = response.json()
        data = body.get("data")
        if not isinstance(data, list) or not all(
            isinstance(item, dict) and isinstance(item.get("id"), str) for item in data
        ):
            raise UpstreamError(500, "tiers payload is malformed", True)
        return body

    async def health(self) -> dict[str, Any]:
        response = await self._request("GET", "/api/v2/health")
        return response.json()

    async def create_upload(
        self,
        filename: str,
        size: int,
        mime_type: str,
        sha256sum: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"filename": filename, "bytes": size, "mime_type": mime_type}
        if sha256sum is not None:
            body["sha256sum"] = sha256sum
        response = await self._request("POST", "/api/v2/uploads", json=body)
        return response.json()

    async def put_upload_content(
        self,
        upload_id: str,
        content_factory: Callable[[], AsyncIterator[bytes]],
        size: int,
    ) -> None:
        """PUT the upload body; a factory rebuilds the stream per attempt.

        A one-shot async generator cannot be replayed, so retries (connect
        errors, 5xx) re-open the file rather than sending an empty body under
        the original Content-Length.
        """
        await self._request(
            "PUT",
            f"/api/v2/uploads/{upload_id}/content",
            content_factory=content_factory,
            headers={"Content-Length": str(size)},
        )

    async def complete_upload(self, upload_id: str, sha256sum: str) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/api/v2/uploads/{upload_id}/complete",
            json={"sha256sum": sha256sum},
        )
        return response.json()

    async def create_job(
        self,
        file_id: str,
        tier: str,
        ocr_mode: str,
        batch_id: str | None = None,
        priority: int = 0,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "file_id": file_id,
            "tier": tier,
            "ocr_mode": ocr_mode,
            "priority": priority,
        }
        if batch_id is not None:
            body["batch_id"] = batch_id
        response = await self._request("POST", "/api/v2/jobs", json=body)
        return response.json()

    async def get_job(self, job_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"/api/v2/jobs/{job_id}")
        return response.json()

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        response = await self._request("DELETE", f"/api/v2/jobs/{job_id}")
        return response.json()

    async def get_file_meta(self, file_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"/api/v2/files/{file_id}")
        return response.json()

    async def get_file_text(self, file_id: str) -> str:
        response = await self._request("GET", f"/api/v2/files/{file_id}/content")
        return response.text

    async def get_file_bytes(self, file_id: str) -> bytes:
        response = await self._request("GET", f"/api/v2/files/{file_id}/content")
        return response.content

    async def get_batch(self, batch_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"/api/v2/batches/{batch_id}")
        return response.json()

    async def get_batch_jobs(
        self, batch_id: str, status: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status
        response = await self._request(
            "GET", f"/api/v2/batches/{batch_id}/jobs", params=params
        )
        return response.json()


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text or response.reason_phrase
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"]
    return response.text or response.reason_phrase
