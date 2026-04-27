"""FastAPI web app for WebTransport UI.

create_app() is a factory so WebTransport can share the store and queues.
Routes only translate HTTP <-> normalized events; no bot logic lives here.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .store import WebStore

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_SESSION_COOKIE = "lp2c_web_session"
_CSRF_COOKIE = "lp2c_web_csrf"


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _session_values(request: Request) -> tuple[str, str]:
    session_id = request.cookies.get(_SESSION_COOKIE) or _new_token()
    csrf_token = request.cookies.get(_CSRF_COOKIE) or _new_token()
    return session_id, csrf_token


def _attach_session_cookies(response, session_id: str, csrf_token: str) -> None:
    response.set_cookie(_SESSION_COOKIE, session_id, httponly=True, samesite="lax")
    response.set_cookie(_CSRF_COOKIE, csrf_token, httponly=True, samesite="lax")


def _verify_csrf(request: Request, csrf_token: str) -> tuple[str, str]:
    session_id = request.cookies.get(_SESSION_COOKIE)
    expected = request.cookies.get(_CSRF_COOKIE)
    if not session_id or not expected or not secrets.compare_digest(csrf_token, expected):
        raise HTTPException(status_code=403, detail="CSRF token required")
    return session_id, expected


def create_app(
    store: WebStore,
    inbound_queue: asyncio.Queue[dict[str, Any]],
    sse_queues: dict[str, list[asyncio.Queue]],
    *,
    authenticated_handle: str | None = None,
) -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)

    @app.get("/")
    async def root():
        return RedirectResponse("/chat/default")

    @app.get("/chat/{chat_id}", response_class=HTMLResponse)
    async def chat_page(request: Request, chat_id: str):
        session_id, csrf_token = _session_values(request)
        messages = await store.get_messages(chat_id)
        response = templates.TemplateResponse(
            request,
            "chat.html",
            {"chat_id": chat_id, "messages": messages, "csrf_token": csrf_token},
        )
        _attach_session_cookies(response, session_id, csrf_token)
        return response

    @app.get("/chat/{chat_id}/messages", response_class=HTMLResponse)
    async def messages_partial(request: Request, chat_id: str):
        session_id, csrf_token = _session_values(request)
        messages = await store.get_messages(chat_id)
        response = templates.TemplateResponse(
            request,
            "messages.html",
            {"messages": messages, "csrf_token": csrf_token},
        )
        _attach_session_cookies(response, session_id, csrf_token)
        return response

    @app.post("/chat/{chat_id}/message")
    async def post_message(
        request: Request,
        chat_id: str,
        text: str = Form(""),
        username: str | None = Form(None),
        csrf_token: str = Form(""),
        file: UploadFile | None = File(None),
    ):
        session_id, _ = _verify_csrf(request, csrf_token)
        files: list[dict] = []
        if file is not None and file.filename:
            tmpdir = tempfile.mkdtemp(prefix="lp2c-web-")
            # Sanitize filename: strip path separators
            safe_name = file.filename.replace("/", "_").replace("\\", "_") or "upload"
            dest = Path(tmpdir) / safe_name
            dest.write_bytes(await file.read())
            files.append({
                "path": str(dest),
                "original_name": safe_name,
                "mime_type": file.content_type or "application/octet-stream",
                "size_bytes": dest.stat().st_size,
            })
        payload = {
            "text": text,
            "sender_native_id": f"web-session:{session_id}",
            "sender_display_name": username or "You",
            "sender_handle": authenticated_handle,
            "form_username": username,
            "files": files,
        }
        await inbound_queue.put({
            "event_type": "inbound_message",
            "chat_id": chat_id,
            "payload": payload,
        })
        return HTMLResponse("", status_code=204)

    @app.post("/chat/{chat_id}/button")
    async def post_button(
        request: Request,
        chat_id: str,
        message_id: str = Form(...),
        value: str = Form(...),
        csrf_token: str = Form(""),
    ):
        session_id, _ = _verify_csrf(request, csrf_token)
        await inbound_queue.put({
            "event_type": "button_click",
            "chat_id": chat_id,
            "payload": {
                "message_id": message_id,
                "value": value,
                "sender_native_id": f"web-session:{session_id}",
                "sender_display_name": "You",
                "sender_handle": authenticated_handle,
            },
        })
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
