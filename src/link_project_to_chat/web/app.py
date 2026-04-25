"""FastAPI web app for WebTransport UI.

create_app() is a factory so WebTransport can share the store and queues.
Routes only translate HTTP <-> normalized events; no bot logic lives here.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .store import WebStore

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    store: WebStore,
    inbound_queue: asyncio.Queue[dict[str, Any]],
    sse_queues: dict[str, list[asyncio.Queue]],
) -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)

    @app.get("/")
    async def root():
        return RedirectResponse("/chat/default")

    @app.get("/chat/{chat_id}", response_class=HTMLResponse)
    async def chat_page(request: Request, chat_id: str):
        messages = await store.get_messages(chat_id)
        return templates.TemplateResponse(
            request, "chat.html", {"chat_id": chat_id, "messages": messages}
        )

    @app.get("/chat/{chat_id}/messages", response_class=HTMLResponse)
    async def messages_partial(request: Request, chat_id: str):
        messages = await store.get_messages(chat_id)
        return templates.TemplateResponse(
            request, "messages.html", {"messages": messages}
        )

    @app.post("/chat/{chat_id}/message")
    async def post_message(
        chat_id: str,
        text: str = Form(...),
        username: str | None = Form(None),
    ):
        payload = {
            "text": text,
            "sender_native_id": "browser_user",
            "sender_display_name": username or "You",
            "sender_handle": username,
        }
        await inbound_queue.put({
            "event_type": "inbound_message",
            "chat_id": chat_id,
            "payload": payload,
        })
        await _notify_sse(sse_queues, chat_id)
        return HTMLResponse("", status_code=204)

    @app.get("/chat/{chat_id}/sse")
    async def chat_sse(chat_id: str):
        queue: asyncio.Queue = asyncio.Queue()
        sse_queues.setdefault(chat_id, []).append(queue)

        async def generate():
            try:
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=25)
                        yield f"event: update\ndata: {json.dumps(payload)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                queues = sse_queues.get(chat_id, [])
                try:
                    queues.remove(queue)
                except ValueError:
                    pass

        return StreamingResponse(generate(), media_type="text/event-stream")

    return app


async def _notify_sse(sse_queues: dict[str, list[asyncio.Queue]], chat_id: str) -> None:
    for q in list(sse_queues.get(chat_id, [])):
        await q.put({"chat_id": chat_id})
