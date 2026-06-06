from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import arcticdb as adb
from dotenv import load_dotenv

CONFIG_DIR = Path.home() / ".adbview"
CONFIG_FILE = CONFIG_DIR / "connections.json"


class ConnectionManager:
    """Manages ArcticDB connections with save/load and multi-instance support."""

    def __init__(self):
        self._arctic: adb.Arctic | None = None
        self._active_name: str | None = None
        self._active_uri: str | None = None

    @property
    def is_connected(self) -> bool:
        return self._arctic is not None

    @property
    def active_name(self) -> str | None:
        return self._active_name

    @property
    def active_uri(self) -> str | None:
        return self._active_uri

    def get_arctic(self) -> adb.Arctic:
        if self._arctic is None:
            raise RuntimeError("No active ArcticDB connection. Connect first.")
        return self._arctic

    def connect(self, uri: str, name: str | None = None) -> adb.Arctic:
        """Connect to an ArcticDB instance by URI."""
        ac = adb.Arctic(uri)
        # Test the connection
        ac.list_libraries()
        self._arctic = ac
        self._active_uri = uri
        self._active_name = name or uri[:50]
        return ac

    def disconnect(self):
        self._arctic = None
        self._active_name = None
        self._active_uri = None

    @staticmethod
    def test_connection(uri: str) -> tuple[bool, str]:
        """Test a connection URI. Returns (success, message)."""
        try:
            ac = adb.Arctic(uri)
            libs = ac.list_libraries()
            return True, f"Connected successfully. {len(libs)} libraries found."
        except Exception as e:
            return False, str(e)

    @staticmethod
    def build_s3_uri(
        bucket: str,
        region: str,
        access_key: str = "",
        secret_key: str = "",
        endpoint: str = "",
        use_https: bool = True,
        aws_auth: bool = False,
    ) -> str:
        """Build an S3 URI for ArcticDB."""
        scheme = "s3s" if use_https else "s3"
        if not endpoint:
            endpoint = f"s3.{region}.amazonaws.com"
        uri = f"{scheme}://{endpoint}:{bucket}?region={region}"
        if aws_auth:
            uri += "&aws_auth=true"
        elif access_key and secret_key:
            uri += f"&access={access_key}&secret={secret_key}"
        return uri

    @staticmethod
    def detect_env_connection() -> dict[str, Any] | None:
        """Check if .env has ArcticDB S3 vars. Returns connection info or None."""
        load_dotenv()
        bucket = os.getenv("BUCKET_NAME")
        region = os.getenv("AWS_REGION")
        access = os.getenv("AWS_ACCESS_KEY_ID")
        secret = os.getenv("AWS_SECRET_ACCESS_KEY")

        if bucket and region and access and secret:
            uri = ConnectionManager.build_s3_uri(
                bucket=bucket, region=region,
                access_key=access, secret_key=secret,
            )
            return {
                "name": f"S3 ({bucket})",
                "type": "s3_env",
                "uri": uri,
                "bucket": bucket,
                "region": region,
            }
        return None


# ── Saved connections persistence ──

def load_connections() -> dict:
    """Load saved connections from ~/.adbview/connections.json."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"connections": [], "last_used": None}


def save_connections(data: dict):
    """Save connections to ~/.adbview/connections.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def add_connection(name: str, conn_type: str, uri: str):
    """Add or update a saved connection."""
    data = load_connections()
    # Update if exists, otherwise append
    for c in data["connections"]:
        if c["name"] == name:
            c["type"] = conn_type
            c["uri"] = uri
            save_connections(data)
            return
    data["connections"].append({"name": name, "type": conn_type, "uri": uri})
    save_connections(data)


def remove_connection(name: str):
    """Remove a saved connection."""
    data = load_connections()
    data["connections"] = [c for c in data["connections"] if c["name"] != name]
    if data["last_used"] == name:
        data["last_used"] = None
    save_connections(data)


def set_last_used(name: str):
    """Set the last used connection name."""
    data = load_connections()
    data["last_used"] = name
    save_connections(data)


# ── Singleton manager for web app ──

_manager = ConnectionManager()


def get_manager() -> ConnectionManager:
    return _manager


def get_arctic() -> adb.Arctic:
    """Backward-compatible: used by core/operations.py and MCP server."""
    return _manager.get_arctic()


def get_arctic_env() -> adb.Arctic:
    """Direct .env connection for MCP server (no connection manager)."""
    load_dotenv()
    bucket = os.getenv("BUCKET_NAME")
    access = os.getenv("AWS_ACCESS_KEY_ID")
    secret = os.getenv("AWS_SECRET_ACCESS_KEY")
    region = os.getenv("AWS_REGION")
    uri = ConnectionManager.build_s3_uri(
        bucket=bucket, region=region,
        access_key=access, secret_key=secret,
    )
    return adb.Arctic(uri)
