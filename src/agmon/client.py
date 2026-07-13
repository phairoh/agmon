"""Pure API client for the agmon HTTP API.

Takes a base URL, returns parsed dicts. No printing, no terminal awareness —
so a CLI, a future TUI, and an Emacs bridge can all reuse it. The only I/O is
the HTTP call itself (httpx), injectable for tests.

Run-id resolution (``resolve_run_id`` / the module-level ``resolve``) is the
one bit of policy that lives here rather than in the server: an id argument is
matched by unique substring, an exact full id always winning outright.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_URL = "http://localhost:8400"

# How many runs to pull when resolving an id substring / computing lineage.
# Newest-first; generous enough to cover any plausible working set on one box.
_RESOLVE_LIMIT = 1000


class ClientError(Exception):
    """Base class for client-side failures (network, HTTP, id resolution)."""


class RunNotFound(ClientError):
    def __init__(self, fragment: str | None):
        self.fragment = fragment
        if fragment is None:
            super().__init__("no runs found")
        else:
            super().__init__(f"no run matches {fragment!r}")


class AmbiguousRunId(ClientError):
    def __init__(self, fragment: str, candidates: list[str]):
        self.fragment = fragment
        self.candidates = candidates
        listed = "\n  ".join(candidates)
        super().__init__(
            f"{fragment!r} matches {len(candidates)} runs:\n  {listed}"
        )


class APIError(ClientError):
    """Non-2xx HTTP response from the server."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"server returned {status_code}: {detail}")


def resolve(runs: list[dict], fragment: str | None) -> str:
    """Resolve a run-id fragment against a newest-first run list.

    - ``fragment is None`` -> the most recently started run (list is newest
      first, so the first element).
    - an exact full-id match wins outright.
    - otherwise a substring matching exactly one run resolves to it.
    - a substring matching several runs raises ``AmbiguousRunId``.
    - no match raises ``RunNotFound``.
    """
    ids = [r["run_id"] for r in runs if r.get("run_id")]
    if fragment is None:
        if not ids:
            raise RunNotFound(None)
        return ids[0]
    if fragment in ids:  # exact full-id match wins
        return fragment
    matches = [rid for rid in ids if fragment in rid]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RunNotFound(fragment)
    raise AmbiguousRunId(fragment, matches)


def compute_lineage(runs: list[dict], run_id: str) -> dict:
    """Resume lineage for ``run_id``, grouping the run list on session_id.

    Runs sharing a session_id form a chain ordered by ``started_at``; a resume
    appends a later run to that chain. Returns ``{"resumed_from": <id|None>,
    "resumed_by": [<id>, ...]}`` — the run immediately before this one in the
    chain, and every run after it. Empty when the run has no session_id or no
    siblings.
    """
    target = next((r for r in runs if r.get("run_id") == run_id), None)
    sid = target.get("session_id") if target else None
    if sid is None:
        return {"resumed_from": None, "resumed_by": []}
    chain = [r for r in runs if r.get("session_id") == sid]
    # Oldest-first; None started_at sorts last (unknown time -> treat as latest).
    chain.sort(key=lambda r: (r.get("started_at") is None, r.get("started_at") or ""))
    ids = [r["run_id"] for r in chain]
    if run_id not in ids:
        return {"resumed_from": None, "resumed_by": []}
    i = ids.index(run_id)
    return {
        "resumed_from": ids[i - 1] if i > 0 else None,
        "resumed_by": ids[i + 1 :],
    }


class Client:
    """Thin HTTP wrapper around the agmon API returning parsed JSON."""

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        *,
        http: httpx.Client | None = None,
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=timeout)

    # -- low-level -------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> Any:
        try:
            resp = self._http.get(self.base_url + path, params=params or {})
        except httpx.HTTPError as exc:
            raise ClientError(f"cannot reach {self.base_url}: {exc}") from exc
        if resp.status_code >= 400:
            detail = _error_detail(resp)
            raise APIError(resp.status_code, detail)
        return resp.json()

    # -- endpoints -------------------------------------------------------

    def health(self) -> dict:
        return self._get("/healthz")

    def list_runs(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        session: str | None = None,
        labels: list[str] | None = None,
    ) -> list[dict]:
        """Runs newest-first. ``labels`` are repeatable ``key=value`` filters
        applied server-side (AND). ``session`` is filtered client-side (the API
        has no session filter)."""
        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status
        if labels:
            params["label"] = labels
        runs = self._get("/v1/runs", params)["runs"]
        if session is not None:
            runs = [r for r in runs if r.get("session_id") == session]
        return runs

    def get_run(self, run_id: str) -> dict:
        return self._get(f"/v1/runs/{run_id}")

    def get_summary(self, run_id: str) -> dict:
        return self._get(f"/v1/runs/{run_id}/summary")

    def get_events(
        self,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 200,
        errors_only: bool = False,
    ) -> dict:
        params: dict[str, Any] = {"after": after, "limit": limit}
        if errors_only:
            params["errors_only"] = "true"
        return self._get(f"/v1/runs/{run_id}/events", params)

    def get_artifacts(self, run_id: str) -> dict:
        return self._get(f"/v1/runs/{run_id}/artifacts")

    def get_artifact_content(self, run_id: str, name: str) -> str:
        """The raw text of one artifact. Raises ``APIError`` on 404 (unknown),
        409 (listed but unavailable), or 400 (ambiguous fragment) — the
        server's ``error`` field becomes the exception message."""
        try:
            resp = self._http.get(
                self.base_url + f"/v1/runs/{run_id}/artifacts/content",
                params={"name": name},
            )
        except httpx.HTTPError as exc:
            raise ClientError(f"cannot reach {self.base_url}: {exc}") from exc
        if resp.status_code >= 400:
            raise APIError(resp.status_code, _error_detail(resp))
        return resp.text

    def get_costs(
        self, *, since: str | None = None, until: str | None = None
    ) -> dict:
        params: dict[str, Any] = {}
        if since is not None:
            params["since"] = since
        if until is not None:
            params["until"] = until
        return self._get("/v1/stats/costs", params)

    # -- id resolution ---------------------------------------------------

    def all_runs(self) -> list[dict]:
        """Every run the server will return (up to ``_RESOLVE_LIMIT``),
        newest-first — the basis for id resolution and lineage."""
        return self.list_runs(limit=_RESOLVE_LIMIT)

    def resolve_run_id(self, fragment: str | None) -> str:
        return resolve(self.all_runs(), fragment)


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("error") or body.get("detail") or body)
    return str(body)
