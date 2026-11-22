"""Stand-ins for the outside world a plugin talks to, used by the compatibility test kit.

The HTTP fake sits under ``app.services.alerting.safe_http``: URL validation, the public
address rule, the HTTPS rule and the response size cap all still run, only the socket is
replaced. The VM fake replaces the Proxmox adapter the SDK resolves for a VM service.
"""

from __future__ import annotations

import json as jsonlib
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

PUBLIC_ADDRESS = "93.184.216.34"


@dataclass
class HttpRequest:
    method: str
    url: str
    host: str
    path: str
    headers: dict[str, str]
    body: Optional[bytes]

    @property
    def text(self) -> str:
        return (self.body or b"").decode("utf-8", "replace")

    def json(self) -> Any:
        return jsonlib.loads(self.text)


class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit is None or limit < 0 else self._body[:limit]


class _FakeConnection:
    def __init__(self, fake: "FakeHttp", scheme: str, host: str, port: int) -> None:
        self._fake = fake
        self._scheme = scheme
        self._host = host
        self._port = port
        self._request: Optional[HttpRequest] = None

    def request(self, method: str, path: str, body: Optional[bytes] = None, headers: Optional[dict] = None) -> None:
        default_port = 443 if self._scheme == "https" else 80
        netloc = self._host if self._port == default_port else f"{self._host}:{self._port}"
        url = f"{self._scheme}://{netloc}{path}"
        self._request = HttpRequest(method, url, self._host, path, dict(headers or {}), body)
        self._fake.requests.append(self._request)

    def getresponse(self) -> _FakeResponse:
        assert self._request is not None
        return self._fake._answer(self._request)

    def close(self) -> None:
        return None


class FakeHttp:
    """Records the requests ``ctx.http`` makes and answers them from a route table.

    By default every request gets ``200`` and an empty body. ``respond`` and ``fail`` add
    routes; the newest route whose ``match`` text appears in the URL wins.
    """

    def __init__(self) -> None:
        self.requests: list[HttpRequest] = []
        self._routes: list[tuple[str, Any]] = []
        self._addresses: dict[str, list[str]] = {}

    def respond(self, match: str = "", status: int = 200, body: str = "") -> None:
        self._routes.append((match, (status, body)))

    def respond_with(self, match: str, handler: Callable[[HttpRequest], tuple[int, str]]) -> None:
        self._routes.append((match, handler))

    def fail(self, match: str = "", error: Optional[BaseException] = None) -> None:
        self._routes.append((match, error or ConnectionError("connection refused")))

    def resolve(self, host: str, *addresses: str) -> None:
        """Make a host name resolve to these addresses (the default is a public address)."""
        self._addresses[host] = list(addresses)

    def clear(self) -> None:
        self.requests.clear()

    def _answer(self, request: HttpRequest) -> _FakeResponse:
        for match, route in reversed(self._routes):
            if match in request.url:
                if isinstance(route, BaseException):
                    raise route
                status, body = route(request) if callable(route) else route
                return _FakeResponse(status, body)
        return _FakeResponse(200, "")

    def _resolver(self, host: str, port: int) -> list[str]:
        return list(self._addresses.get(host) or [PUBLIC_ADDRESS])

    def install(self, monkeypatch) -> None:
        from app.services.alerting import safe_http

        def https(host, port, _address, _timeout):
            return _FakeConnection(self, "https", host, port)

        def http(host, port, _address, _timeout):
            return _FakeConnection(self, "http", host, port)

        monkeypatch.setattr(safe_http, "_PinnedHTTPSConnection", https)
        monkeypatch.setattr(safe_http, "_PinnedHTTPConnection", http)
        monkeypatch.setattr(safe_http, "_default_resolver", self._resolver)

    def __len__(self) -> int:
        return len(self.requests)

    def to(self, text: str) -> list[HttpRequest]:
        return [r for r in self.requests if text in r.url]


@dataclass
class FakeVm:
    """The VM a plugin reaches through ``ctx.guest`` and ``ctx.vm``.

    ``on_exec`` receives the argv list and returns a dict with ``exit_code``, ``stdout`` and
    ``stderr`` (missing keys default to success and empty output). Set ``agent_ready`` to
    ``False`` to make ``ctx.guest.ping`` fail, or ``error`` to make every call raise it.
    """

    agent_ready: bool = True
    on_exec: Optional[Callable[[list], dict]] = None
    error: Optional[BaseException] = None
    commands: list = field(default_factory=list)
    power_calls: list = field(default_factory=list)

    def _check(self) -> None:
        if self.error is not None:
            raise self.error

    async def guest_agent_ready(self) -> bool:
        self._check()
        return self.agent_ready

    async def guest_exec(self, command: list, input_data: Optional[str] = None, max_wait: float = 180.0) -> dict:
        self._check()
        self.commands.append(list(command))
        result = self.on_exec(list(command)) if self.on_exec else {}
        return {
            "exitcode": result.get("exit_code", 0),
            "out-data": result.get("stdout", ""),
            "err-data": result.get("stderr", ""),
        }

    async def power_on(self) -> bool:
        self._check()
        self.power_calls.append("on")
        return True

    async def power_off(self, force: bool = False) -> bool:
        self._check()
        self.power_calls.append("off" if force else "shutdown")
        return True

    async def power_reset(self) -> bool:
        self._check()
        self.power_calls.append("reset")
        return True

    def install(self, monkeypatch) -> None:
        from app.plugin_host import sdk

        async def resolve(_db, _service):
            return self

        monkeypatch.setattr(sdk, "resolve_vm", resolve)


class FakeRedis:
    """Just the counter calls the plugin host's rate limits use."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key: str, _seconds: int) -> bool:
        return True
