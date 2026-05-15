"""FastAPI web app for WebTransport UI.

create_app() is a factory so WebTransport can share the store and queues.
Routes only translate HTTP <-> normalized events; no bot logic lives here.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import tempfile
from dataclasses import dataclass
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
_AUTH_COOKIE = "lp2c_web_auth"

# CA-2: cap upload size to keep RAM/disk bounded. The previous
# `await file.read()` read the entire body into memory unconditionally.
# 25 MB is chosen as a generous cap for screenshots and short audio clips
# while rejecting accidental dumps and bulk-upload abuse. Operators can
# override per-deployment by editing this constant; a future env-var
# override is trivial if needed.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_UPLOAD_CHUNK = 64 * 1024


def _new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class _WebAuth:
    ok: bool
    supplied_token: str | None = None
    handle: str | None = None


def _session_values(request: Request) -> tuple[str, str]:
    session_id = request.cookies.get(_SESSION_COOKIE) or _new_token()
    csrf_token = request.cookies.get(_CSRF_COOKIE) or _new_token()
    return session_id, csrf_token


def _attach_session_cookies(response, session_id: str, csrf_token: str) -> None:
    response.set_cookie(_SESSION_COOKIE, session_id, httponly=True, samesite="lax")
    response.set_cookie(_CSRF_COOKIE, csrf_token, httponly=True, samesite="lax")


def _auth_from_request(
    request: Request,
    *,
    auth_token: str | None,
    authenticated_handle: str | None,
    authenticated_handles: dict[str, str] | None,
) -> _WebAuth:
    # Cookie-only auth on normal routes — query-string tokens are rejected
    # because the URL ends up in browser history, proxy/access logs, and
    # Referer headers. Operators bootstrap a session via GET /auth?token=...,
    # which sets the cookie and redirects without the token.
    supplied = request.cookies.get(_AUTH_COOKIE)
    if authenticated_handles is not None:
        if not supplied:
            return _WebAuth(ok=False)
        for token, handle in authenticated_handles.items():
            if secrets.compare_digest(supplied, token):
                return _WebAuth(ok=True, supplied_token=supplied, handle=handle)
        return _WebAuth(ok=False)

    if auth_token is None:
        return _WebAuth(ok=True, supplied_token=supplied, handle=authenticated_handle)
    if supplied and secrets.compare_digest(supplied, auth_token):
        return _WebAuth(ok=True, supplied_token=supplied, handle=authenticated_handle)
    return _WebAuth(ok=False)


def _safe_local_redirect(next_param: str | None, default: str = "/chat/default") -> str:
    """Return `next_param` only if it's a same-host local path; else `default`.

    Rejects: full URLs (https://evil.example/...), protocol-relative URLs
    (//evil.example/...), and Windows-style paths. The bootstrap endpoint uses
    this to prevent open-redirect against a stolen-but-valid token.
    """
    if not next_param:
        return default
    if not next_param.startswith("/"):
        return default
    if next_param.startswith("//"):
        return default
    if "\\" in next_param:
        return default
    return next_param


def _require_web_auth(
    request: Request,
    *,
    auth_token: str | None,
    authenticated_handle: str | None,
    authenticated_handles: dict[str, str] | None,
) -> _WebAuth:
    auth = _auth_from_request(
        request,
        auth_token=auth_token,
        authenticated_handle=authenticated_handle,
        authenticated_handles=authenticated_handles,
    )
    if not auth.ok:
        raise HTTPException(status_code=401, detail="Web auth token required")
    return auth


def _set_auth_cookie(response, token: str) -> None:
    response.set_cookie(_AUTH_COOKIE, token, httponly=True, samesite="lax")


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
    auth_token: str | None = None,
    authenticated_handles: dict[str, str] | None = None,
) -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)
    authenticated_handles = (
        dict(authenticated_handles)
        if authenticated_handles is not None
        else None
    )

    @app.get("/")
    async def root():
        return RedirectResponse("/chat/default")

    @app.get("/auth")
    async def auth_bootstrap(request: Request):
        """One-shot exchange: validate `?token=...`, set cookie, redirect.

        This is the only route that accepts a token in the URL. It immediately
        sets the auth cookie and redirects to `next` (or /chat/default), so
        the token never appears in any page-load URL after the bootstrap.
        """
        supplied = request.query_params.get("token", "")
        next_url = _safe_local_redirect(request.query_params.get("next"))
        validated_token: str | None = None
        if authenticated_handles is not None:
            for token in authenticated_handles:
                if secrets.compare_digest(supplied, token):
                    validated_token = token
                    break
        elif auth_token is not None:
            if supplied and secrets.compare_digest(supplied, auth_token):
                validated_token = auth_token
        else:
            # Auth not enforced; bootstrap is a no-op redirect.
            return RedirectResponse(next_url, status_code=303)
        if validated_token is None:
            raise HTTPException(status_code=401, detail="Invalid auth token")
        response = RedirectResponse(next_url, status_code=303)
        _set_auth_cookie(response, validated_token)
        return response

    @app.get("/chat/{chat_id}", response_class=HTMLResponse)
    async def chat_page(request: Request, chat_id: str):
        auth = _auth_from_request(
            request,
            auth_token=auth_token,
            authenticated_handle=authenticated_handle,
            authenticated_handles=authenticated_handles,
        )
        if not auth.ok:
            return HTMLResponse("Web auth token required", status_code=401)
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
        auth = _require_web_auth(
            request,
            auth_token=auth_token,
            authenticated_handle=authenticated_handle,
            authenticated_handles=authenticated_handles,
        )
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
        auth = _require_web_auth(
            request,
            auth_token=auth_token,
            authenticated_handle=authenticated_handle,
            authenticated_handles=authenticated_handles,
        )
        session_id, _ = _verify_csrf(request, csrf_token)
        files: list[dict] = []
        if file is not None and file.filename:
            # CA-2: stream to disk in chunks with a hard size cap so a
            # multi-GB upload can't fill memory + disk before any auth
            # check. Always clean the tempdir on failure paths.
            tmpdir = tempfile.mkdtemp(prefix="lp2c-web-")
            safe_name = file.filename.replace("/", "_").replace("\\", "_") or "upload"
            dest = Path(tmpdir) / safe_name
            total = 0
            try:
                with dest.open("wb") as out:
                    while True:
                        chunk = await file.read(_UPLOAD_CHUNK)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_UPLOAD_BYTES:
                            raise HTTPException(
                                status_code=413,
                                detail=(
                                    f"Upload exceeds maximum size "
                                    f"({MAX_UPLOAD_BYTES} bytes)."
                                ),
                            )
                        out.write(chunk)
            except BaseException:
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise
            files.append({
                "path": str(dest),
                "original_name": safe_name,
                "mime_type": file.content_type or "application/octet-stream",
                "size_bytes": total,
            })
        payload = {
            "text": text,
            "sender_native_id": (
                f"web-user:{auth.handle}"
                if auth.handle
                else f"web-session:{session_id}"
            ),
            "sender_display_name": username or "You",
            "sender_handle": auth.handle,
            "authenticated_handle": auth.handle,
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
        auth = _require_web_auth(
            request,
            auth_token=auth_token,
            authenticated_handle=authenticated_handle,
            authenticated_handles=authenticated_handles,
        )
        session_id, _ = _verify_csrf(request, csrf_token)
        await inbound_queue.put({
            "event_type": "button_click",
            "chat_id": chat_id,
            "payload": {
                "message_id": message_id,
                "value": value,
                "sender_native_id": (
                    f"web-user:{auth.handle}"
                    if auth.handle
                    else f"web-session:{session_id}"
                ),
                "sender_display_name": "You",
                "sender_handle": auth.handle,
                "authenticated_handle": auth.handle,
            },
        })
        return HTMLResponse("", status_code=204)

    @app.get("/chat/{chat_id}/sse")
    async def chat_sse(request: Request, chat_id: str):
        _require_web_auth(
            request,
            auth_token=auth_token,
            authenticated_handle=authenticated_handle,
            authenticated_handles=authenticated_handles,
        )
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
