"""Async HTTP client for OpenCode server API.

Handles session management, prompt sending, event streaming, and
session lifecycle via the OpenCode REST API.
"""

import asyncio
import logging
import os
from typing import Any, AsyncIterator, Optional

import httpx

from session_store import SessionStore

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4096
TIMEOUT = 120.0


class OpenCodeClient:
    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        password: Optional[str] = None,
        timeout: float = TIMEOUT,
    ):
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout
        headers = {}
        if password:
            import base64
            credentials = f"opencode:{password}"
            encoded = base64.b64encode(credentials.encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def health_check(self) -> bool:
        """Check if the OpenCode server is reachable."""
        try:
            r = await self._client.get("/global/health")
            return r.status_code == 200
        except Exception as e:
            logger.error("Health check failed: %s", e)
            return False

    async def get_session(self, session_id: str) -> dict[str, Any]:
        """Get session details."""
        r = await self._client.get(f"/session/{session_id}")
        r.raise_for_status()
        return r.json()

    async def create_session(
        self,
        parent_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a new OpenCode session."""
        body: dict[str, Any] = {}
        if parent_id:
            body["parentID"] = parent_id
        if title:
            body["title"] = title
        r = await self._client.post("/session", json=body)
        r.raise_for_status()
        return r.json()

    async def delete_session(self, session_id: str) -> bool:
        """Delete an OpenCode session."""
        r = await self._client.delete(f"/session/{session_id}")
        return r.status_code in (200, 204)

    async def send_prompt(
        self,
        session_id: str,
        text: str,
        model: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Send a text prompt to a session and wait for response.

        Returns the full response with message parts.
        """
        body: dict[str, Any] = {
            "parts": [{"type": "text", "text": text}],
        }
        if model:
            body["model"] = model
        r = await self._client.post(f"/session/{session_id}/message", json=body)
        r.raise_for_status()
        return r.json()

    async def send_prompt_async(
        self,
        session_id: str,
        text: str,
        model: Optional[dict[str, str]] = None,
    ) -> None:
        """Send a prompt without waiting for response."""
        body: dict[str, Any] = {
            "parts": [{"type": "text", "text": text}],
        }
        if model:
            body["model"] = model
        r = await self._client.post(
            f"/session/{session_id}/prompt_async",
            json=body,
        )
        if r.status_code != 204:
            r.raise_for_status()

    async def abort_session(self, session_id: str) -> bool:
        """Abort a running session."""
        r = await self._client.post(f"/session/{session_id}/abort")
        return r.status_code in (200, 204)

    async def share_session(self, session_id: str) -> Optional[str]:
        """Share a session and return the share URL."""
        r = await self._client.post(f"/session/{session_id}/share")
        if r.status_code not in (200, 204):
            return None
        try:
            data = r.json()
            slug = data.get("slug") or data.get("shareId")
            if slug:
                return f"https://opncd.ai/s/{slug}"
            return None
        except Exception:
            return None

    async def unshare_session(self, session_id: str) -> bool:
        r = await self._client.delete(f"/session/{session_id}/share")
        return r.status_code in (200, 204)

    async def update_session(
        self,
        session_id: str,
        title: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        """Update session properties (title, model)."""
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if model is not None:
            body["model"] = model
        r = await self._client.patch(f"/session/{session_id}", json=body)
        r.raise_for_status()
        return r.json()

    async def list_providers(self) -> list[dict[str, Any]]:
        """List available model providers."""
        r = await self._client.get("/provider")
        r.raise_for_status()
        data = r.json()
        return data.get("all", [])

    async def list_sessions(self) -> list[dict[str, Any]]:
        """List all sessions."""
        r = await self._client.get("/session")
        r.raise_for_status()
        return r.json()

    async def get_session_messages(
        self, session_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Get messages for a session."""
        r = await self._client.get(
            f"/session/{session_id}/message",
            params={"limit": limit},
        )
        r.raise_for_status()
        return r.json()

    async def stream_events(
        self,
        session_id: str,
        timeout: float = 60.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream SSE events for a session.

        Yields event dictionaries with type and properties.
        """
        async with self._client.stream(
            "GET",
            "/global/event",
            timeout=timeout,
        ) as resp:
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                    continue
                if line.startswith("data:"):
                    try:
                        import json
                        data = json.loads(line[5:].strip())
                        # Filter events related to our session
                        if session_id and data.get("properties", {}).get("sessionID") == session_id:
                            yield {"type": event_type, **data}
                        elif not session_id:
                            yield {"type": event_type, **data}
                    except json.JSONDecodeError:
                        pass


def extract_text_response(response: dict[str, Any]) -> str:
    """Extract text from an OpenCode message response.

    The response has structure:
    {
        "info": { "role": "assistant", ... },
        "parts": [{ "type": "text", "text": "..." }, ...]
    }
    """
    texts: list[str] = []
    for part in response.get("parts", []):
        if part.get("type") == "text":
            text = part.get("text", "")
            if text:
                texts.append(text)
    return "\n\n".join(texts)


def extract_share_url(response: dict[str, Any]) -> Optional[str]:
    """Extract share URL from a session share response."""
    info = response.get("info", {})
    slug = info.get("slug") or info.get("shareId")
    if slug:
        return f"https://opncd.ai/s/{slug}"
    return None


async def wait_for_session_idle(
    client: OpenCodeClient,
    session_id: str,
    poll_interval: float = 1.0,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Wait for a session to finish processing and return the final response.

    Polls the session status until it becomes idle/complete.
    """
    elapsed = 0.0
    while elapsed < timeout:
        sessions_status = await client._client.get("/session/status")
        if sessions_status.status_code == 200:
            status_data = sessions_status.json()
            session_status = status_data.get(session_id, {})
            state = session_status.get("state", "")
            if state in ("idle", "complete", "done", ""):
                break
            # Check if there's a recent message
            try:
                msgs = await client.get_session_messages(session_id, limit=1)
                if msgs:
                    last = msgs[-1]
                    if last.get("info", {}).get("role") == "assistant":
                        break
            except Exception:
                pass
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    else:
        logger.warning("Timeout waiting for session %s to complete", session_id)

    # Return the latest message
    msgs = await client.get_session_messages(session_id, limit=1)
    return msgs[-1] if msgs else {"parts": []}