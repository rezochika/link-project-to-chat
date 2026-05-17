from __future__ import annotations

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

    async def upload_attachment(self, space: str, path: Path, *, mime_type: str | None) -> dict:
        raise NotImplementedError("Google Chat upload support lands in Task 11")

    async def download_attachment(self, resource_name: str, destination: Path) -> None:
        raise NotImplementedError("Google Chat download support lands in Task 11")
