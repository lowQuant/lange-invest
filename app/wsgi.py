"""Threadless ASGI->WSGI adapter for the lange-invest app.

a2wsgi runs the ASGI app in a background thread + queue; under some uWSGI configs
(notably PythonAnywhere) that thread can stall, so requests hang forever with no
error and zero bytes. This adapter instead runs the ASGI coroutine INLINE in the
worker thread (a fresh event loop per request, run to completion) and buffers the
whole response. That's perfectly fine for this app's short request/response pages
and removes any dependency on background threads.

Use it as the WSGI ``application`` on hosts where a2wsgi hangs:

    from app.wsgi import application
"""
from __future__ import annotations

import asyncio
from http import HTTPStatus

from app.main import app as _asgi_app


def _phrase(code: int) -> str:
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return ""


def _scope_headers(environ) -> list[tuple[bytes, bytes]]:
    headers: list[tuple[bytes, bytes]] = []
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            name = key[5:].replace("_", "-").lower()
            headers.append((name.encode("latin1"), str(value).encode("latin1")))
    if environ.get("CONTENT_TYPE"):
        headers.append((b"content-type", environ["CONTENT_TYPE"].encode("latin1")))
    if environ.get("CONTENT_LENGTH"):
        headers.append((b"content-length", environ["CONTENT_LENGTH"].encode("latin1")))
    return headers


def application(environ, start_response):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    body = environ["wsgi.input"].read(length) if length > 0 else b""

    path = environ.get("PATH_INFO", "/") or "/"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": environ.get("REQUEST_METHOD", "GET"),
        "scheme": environ.get("wsgi.url_scheme", "http"),
        "path": path,
        "raw_path": path.encode("latin1"),
        "query_string": environ.get("QUERY_STRING", "").encode("latin1"),
        "root_path": environ.get("SCRIPT_NAME", ""),
        "headers": _scope_headers(environ),
        "server": (environ.get("SERVER_NAME", ""), int(environ.get("SERVER_PORT") or 0)),
        "client": (environ.get("REMOTE_ADDR", ""), 0),
    }

    state = {"sent": False}

    async def receive():
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    resp = {"status": 200, "headers": [], "body": bytearray()}

    async def send(message):
        t = message.get("type")
        if t == "http.response.start":
            resp["status"] = message["status"]
            resp["headers"] = message.get("headers", []) or []
        elif t == "http.response.body":
            resp["body"].extend(message.get("body", b"") or b"")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_asgi_app(scope, receive, send))
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()

    status_line = f"{resp['status']} {_phrase(resp['status'])}".strip()
    headers = [
        (k.decode("latin1") if isinstance(k, bytes) else str(k),
         v.decode("latin1") if isinstance(v, bytes) else str(v))
        for k, v in resp["headers"]
    ]
    start_response(status_line, headers)
    return [bytes(resp["body"])]
