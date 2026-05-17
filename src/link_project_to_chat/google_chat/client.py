from __future__ import annotations

import json
from pathlib import Path


class GoogleChatClient:
    def __init__(self, *, http) -> None:
        self._http = http

    async def create_message(
        self,
        space: str,
        body: dict,
        *,
        thread_name: str | None = None,
        request_id: str | None = None,
        message_reply_option: str | None = None,
    ) -> dict:
        params: dict[str, object] = {}
        if request_id:
            params["requestId"] = request_id
        if message_reply_option:
            params["messageReplyOption"] = message_reply_option
        if thread_name:
            body = dict(body)
            body["thread"] = {"name": thread_name}
        response = await self._http.post(f"/v1/{space}/messages", json=body, params=params)
        return response.json()

    async def update_message(
        self,
        message_name: str,
        body: dict,
        *,
        update_mask: str,
        allow_missing: bool = False,
    ) -> dict:
        params = {"updateMask": update_mask, "allowMissing": allow_missing}
        response = await self._http.patch(f"/v1/{message_name}", json=body, params=params)
        return response.json()

    async def upload_attachment(
        self,
        space: str,
        path: Path,
        *,
        mime_type: str | None,
        max_bytes: int = 25_000_000,
        display_name: str | None = None,
    ) -> dict:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        size_bytes = path.stat().st_size
        if size_bytes > max_bytes:
            raise ValueError(f"Google Chat attachment exceeds max_bytes={max_bytes}")

        metadata = {"filename": display_name or path.name}
        with path.open("rb") as fh:
            response = await self._http.post(
                f"/upload/v1/{space}/attachments:upload",
                params={"uploadType": "multipart"},
                files={
                    "metadata": (None, json.dumps(metadata), "application/json; charset=UTF-8"),
                    "file": (path.name, fh, mime_type or "application/octet-stream"),
                },
            )
        return response.json()

    async def download_attachment(
        self,
        resource_name: str,
        destination: Path,
        *,
        max_bytes: int = 25_000_000,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")

        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            async with self._http.stream("GET", f"/v1/media/{resource_name}?alt=media") as response:
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()
                with destination.open("wb") as fh:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > max_bytes:
                            raise ValueError(f"Google Chat attachment exceeds max_bytes={max_bytes}")
                        fh.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
