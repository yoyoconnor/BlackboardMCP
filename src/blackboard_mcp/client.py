"""Thin async client for the Blackboard Learn public REST API.

Auth modes (pick one via environment variables):
  1. Session cookie  -- BB_COOKIE="BbRouter=...; JSESSIONID=..."  (works for students;
     copy it from your browser after logging in through SSO/Duo)
  2. OAuth2 app      -- BB_APP_KEY / BB_APP_SECRET (only if your school registered a
     REST app for you; client-credentials tokens act as the app's system user)
"""

from __future__ import annotations

import os
import time
from typing import Any, AsyncIterator

import httpx

API = "/learn/api/public"


class BlackboardError(RuntimeError):
    pass


class BlackboardClient:
    def __init__(
        self,
        base_url: str | None = None,
        cookie: str | None = None,
        app_key: str | None = None,
        app_secret: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("BB_BASE_URL", "")).rstrip("/")
        if not self.base_url:
            raise BlackboardError("Set BB_BASE_URL, e.g. https://ualearn.blackboard.com")
        self.cookie = cookie if cookie is not None else os.environ.get("BB_COOKIE")
        self.app_key = app_key or os.environ.get("BB_APP_KEY")
        self.app_secret = app_secret or os.environ.get("BB_APP_SECRET")
        if not self.cookie and not (self.app_key and self.app_secret):
            raise BlackboardError("Set BB_COOKIE, or BB_APP_KEY and BB_APP_SECRET")

        headers = {"Accept": "application/json", "User-Agent": "blackboard-mcp/0.1"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(30.0, read=120.0),
            follow_redirects=True,
            transport=transport,
        )
        self._token: str | None = None
        self._token_exp = 0.0
        self._me: dict[str, Any] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- auth
    async def _auth_headers(self) -> dict[str, str]:
        if self.cookie:
            return {}
        if not self._token or time.time() > self._token_exp - 60:
            r = await self._http.post(
                f"{API}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(self.app_key or "", self.app_secret or ""),
            )
            if r.status_code != 200:
                raise BlackboardError(f"OAuth token request failed ({r.status_code}): {r.text[:300]}")
            body = r.json()
            self._token = body["access_token"]
            self._token_exp = time.time() + int(body.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}"}

    # ---------------------------------------------------------------- core
    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("/learn") else f"{API}{path}"
        r = await self._http.get(url, params=params, headers=await self._auth_headers())
        if r.status_code == 401:
            raise BlackboardError(
                "Blackboard returned 401 Unauthorized. Your session cookie has probably "
                "expired -- log in again in your browser and update BB_COOKIE."
            )
        if r.status_code == 403:
            raise BlackboardError(f"403 Forbidden for {url}: {r.text[:300]}")
        if r.status_code == 404:
            raise BlackboardError(f"404 Not Found: {url}")
        if r.status_code >= 400:
            raise BlackboardError(f"HTTP {r.status_code} for {url}: {r.text[:300]}")
        ctype = r.headers.get("content-type", "")
        if "json" not in ctype:
            # SSO login pages come back as 200 HTML when the cookie is dead.
            raise BlackboardError(
                f"Expected JSON from {url} but got {ctype or 'unknown content'}; "
                "the session cookie is probably invalid or expired."
            )
        return r.json()

    async def paginate(
        self, path: str, params: dict[str, Any] | None = None, limit: int = 1000
    ) -> AsyncIterator[dict[str, Any]]:
        """Follow Blackboard's paging.nextPage links."""
        seen = 0
        body = await self.get(path, params)
        while True:
            for item in body.get("results", []):
                yield item
                seen += 1
                if seen >= limit:
                    return
            nxt = (body.get("paging") or {}).get("nextPage")
            if not nxt:
                return
            body = await self.get(nxt)

    async def collect(self, path: str, params: dict[str, Any] | None = None, limit: int = 1000) -> list[dict]:
        return [x async for x in self.paginate(path, params, limit)]

    async def download(self, path: str) -> tuple[bytes, str, str | None]:
        """Download a file; returns (bytes, content-type, filename-from-header)."""
        url = path if path.startswith("/learn") or path.startswith("http") else f"{API}{path}"
        r = await self._http.get(url, headers=await self._auth_headers())
        if r.status_code >= 400:
            raise BlackboardError(f"Download failed ({r.status_code}) for {url}")
        cd = r.headers.get("content-disposition", "")
        fname = None
        if "filename=" in cd:
            fname = cd.split("filename=")[-1].strip().strip('"').split(";")[0]
        return r.content, r.headers.get("content-type", ""), fname

    # ---------------------------------------------------------------- helpers
    async def me(self) -> dict[str, Any]:
        if self._me is None:
            if self.cookie:
                self._me = await self.get("/v1/users/me")
            else:
                user = os.environ.get("BB_USER_ID")
                if not user:
                    raise BlackboardError(
                        "With OAuth app auth, set BB_USER_ID (e.g. userName:jdoe) "
                        "so the server knows whose courses to read."
                    )
                self._me = await self.get(f"/v1/users/{user}")
        return self._me
