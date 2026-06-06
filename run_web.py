#!/usr/bin/env python3
"""Local dev entry point for the lange-invest web app.

Production runs under the host's WSGI/ASGI server pointing at ``app.main:app``.
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
