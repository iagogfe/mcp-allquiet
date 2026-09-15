"""MCP server for the All Quiet public API.

Curated tools cover the incident workflow. Generic tools reach every operation in
the bundled OpenAPI spec, so the client's context stays small.
"""

import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx2
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

PREFIX = "/api/public/v1"
METHODS = {"get", "post", "put", "patch", "delete"}
MAX_CHARS = 40_000
MAX_ERROR_CHARS = 2_000

SPEC = json.loads(
    files("mcp_allquiet").joinpath("openapi.json").read_text(encoding="utf-8")
)
SCHEMAS = SPEC["components"]["schemas"]
PATHS = {path.removeprefix(PREFIX): ops for path, ops in SPEC["paths"].items()}

# ponytail: test seam, None means the real network
_transport: httpx2.AsyncBaseTransport | None = None

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, open_world_hint=True
)
DELETE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, open_world_hint=True
)

INSTRUCTIONS = """\
All Quiet incident management API.
Prefer the incident tools (list_incidents, get_incident, create_incident, update_incident,
who_is_on_call). For anything else: list_operations to find the path template,
describe_operation to see its parameters and body, then call_read / call_write / call_delete.
GET /auth/me shows what the API key can reach. Keys spanning several organizations need the
organizationId query parameter. Timestamps are ISO-8601 UTC.
Operations accept organization API keys and personal access tokens, org-wide or team-scoped,
unless describe_operation lists narrower key types."""

# stated once in INSTRUCTIONS instead of in 86 of the 136 operation descriptions
DEFAULT_KEY_TYPES = (
    "\n- **Accepted key types:** organization API key (org-wide), organization API key "
    "(team-scoped), personal access token (org-wide), personal access token (team-scoped)"
)


@asynccontextmanager
async def lifespan(_: MCPServer) -> AsyncIterator[httpx2.AsyncClient]:
    key = os.environ.get("ALLQUIET_API_KEY")
    if not key:
        raise RuntimeError("ALLQUIET_API_KEY is not set")
    base = os.environ.get("ALLQUIET_BASE_URL", "https://allquiet.app").rstrip("/")
    if not base.startswith("https://"):
        raise RuntimeError("ALLQUIET_BASE_URL must start with https://")
    async with httpx2.AsyncClient(
        base_url=base + PREFIX,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        timeout=30,
        transport=_transport,
    ) as http:
        yield http


mcp = MCPServer("allquiet", instructions=INSTRUCTIONS, lifespan=lifespan)


def _operation(method: str, path: str) -> dict[str, Any]:
    op = PATHS.get(path, {}).get(method.lower())
    if op is None:
        raise ToolError(
            f"{method} {path} is not an All Quiet API operation. Use list_operations."
        )
    return op


def _fill(path: str, params: dict[str, str]) -> str:
    def value(m: re.Match[str]) -> str:
        if m[1] not in params:
            raise ToolError(f"path_params is missing {m[1]!r} for {path}")
        # safe="" encodes "/" too, so a value can never climb out of its segment
        return quote(str(params[m[1]]), safe="")

    return re.sub(r"\{(\w+)\}", value, path)


def _scope(op: dict[str, Any]) -> str:
    m = re.search(r"\*\*Scope:\*\* `([^`]+)`", op.get("description", ""))
    return m[1] if m else "no scope"


def _inline(
    node: Any,
    seen: frozenset[str] = frozenset(),
    shared: frozenset[str] = frozenset(),
    emitted: set[str] | None = None,
) -> Any:
    """Resolve $refs. With `emitted`, a schema in `shared` is shown in full once,
    tagged "$name", and as {"$see": name} afterwards."""
    if isinstance(node, list):
        return [_inline(v, seen, shared, emitted) for v in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        if name in seen:
            return {"description": f"recursive {name}"}
        label = name.removeprefix("AllQuiet.Api.")
        if emitted is not None:
            if name in emitted:
                return {"$see": label}
            emitted.add(name)
        out = _inline(SCHEMAS[name], seen | {name}, shared, emitted)
        return {"$name": label, **out} if name in shared else out
    out = {
        k: _inline(v, seen, shared, emitted)
        for k, v in node.items()
        if not _noise(k, v)
    }
    if "properties" in out and out.get("type") == "object":
        del out["type"]  # properties already says it is an object
    return out


def _shared_refs(node: Any) -> frozenset[str]:
    """$refs used more than once, counted in the order _inline expands them."""
    counts: dict[str, int] = {}

    def walk(n: Any, seen: frozenset[str]) -> None:
        if isinstance(n, list):
            for v in n:
                walk(v, seen)
        elif isinstance(n, dict) and "$ref" not in n:
            for v in n.values():
                walk(v, seen)
        elif isinstance(n, dict):
            name = n["$ref"].rsplit("/", 1)[-1]
            if name in seen:
                return
            counts[name] = counts.get(name, 0) + 1
            if (
                counts[name] == 1
            ):  # later uses become $see, so their insides don't count
                walk(SCHEMAS[name], seen | {name})

    walk(node, frozenset())
    return frozenset(name for name, c in counts.items() if c > 1)


NUMERIC_FORMATS = {"int32", "int64", "double", "float"}


def _noise(k: str, v: Any) -> bool:
    """Schema keys a model gains nothing from: nullable restates the required list,
    numeric formats and additionalProperties false don't change what it sends."""
    return (
        k == "nullable"
        or (k == "format" and v in NUMERIC_FORMATS)
        or (k == "additionalProperties" and v is False)
    )


def _param(p: dict[str, Any]) -> dict[str, Any]:
    """Parameter with its schema flattened in, and `required` only when true."""
    p = _inline(p)
    flat = {k: v for k, v in p.items() if k not in ("schema", "required")}
    for k, v in (p.get("schema") or {}).items():
        flat.setdefault(k, v)
    if p.get("required"):
        flat["required"] = True
    return flat


def _clip(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return (
        text[:MAX_CHARS]
        + f"\n... truncated ({len(text)} chars). Narrow the request with filters or Limit/Offset."
    )


async def _request(
    ctx: Context[httpx2.AsyncClient],
    method: str,
    path: str,
    path_params: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    body: Any = None,
) -> httpx2.Response:
    _operation(method, path)
    r = await ctx.request_context.lifespan_context.request(
        method, _fill(path, path_params or {}), params=query, json=body
    )
    if r.is_success:
        return r
    msg = f"All Quiet API returned HTTP {r.status_code}"
    if r.status_code in (401, 403):
        msg += ". Check the API key is valid and has the scope this operation needs"
    if r.status_code == 429:
        msg += (
            f". Rate limited, Retry-After: {r.headers.get('Retry-After', 'not given')}"
        )
    raise ToolError(f"{msg}\n{r.text[:MAX_ERROR_CHARS]}")


def _body(r: httpx2.Response) -> str:
    return _clip(r.text) if r.text else f"HTTP {r.status_code}, empty body"


# --- generic tools ----------------------------------------------------------

OPERATIONS = [
    f"{method.upper()} {path} — {op.get('summary', '')} [{_scope(op)}]"
    for path, ops in PATHS.items()
    for method, op in ops.items()
    if method in METHODS
]

Path = Annotated[
    str, Field(description="Path template exactly as listed, e.g. /team/{id}")
]
PathParams = Annotated[
    dict[str, str] | None, Field(description="Values for {placeholders} in the path")
]
Query = Annotated[
    dict[str, Any] | None,
    Field(description="Query parameters. Lists repeat the key (Statuses=a&Statuses=b)"),
]


@mcp.tool(annotations=READ, structured_output=False)
def list_operations(
    query: Annotated[
        str, Field(description="Case-insensitive filter on method, path, summary")
    ] = "",
) -> str:
    """List All Quiet API operations as `METHOD path — summary [required key scope]`."""
    q = query.lower()
    return "\n".join(line for line in OPERATIONS if q in line.lower()) or "No match."


@mcp.tool(annotations=READ, structured_output=False)
def describe_operation(method: str, path: Path) -> str:
    """Show an operation's parameters and JSON request body schema, with all $refs inlined.
    A schema used more than once appears in full once, tagged "$name", then as {"$see": name}."""
    op = _operation(method, path)
    content = op.get("requestBody", {}).get("content", {})
    body = content.get("application/json") or next(iter(content.values()), {})
    schema = body.get("schema")
    return json.dumps(
        {
            "description": (op.get("description") or "").replace(DEFAULT_KEY_TYPES, ""),
            "parameters": [_param(p) for p in op.get("parameters", [])],
            "request_body": _inline(schema, shared=_shared_refs(schema), emitted=set()),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


@mcp.tool(annotations=READ, structured_output=False)
async def call_read(
    ctx: Context[httpx2.AsyncClient],
    path: Path,
    path_params: PathParams = None,
    query: Query = None,
) -> str:
    """GET any All Quiet API operation. Find it first with list_operations."""
    return _body(await _request(ctx, "GET", path, path_params, query))


@mcp.tool(annotations=WRITE, structured_output=False)
async def call_write(
    ctx: Context[httpx2.AsyncClient],
    method: Literal["POST", "PUT", "PATCH"],
    path: Path,
    path_params: PathParams = None,
    query: Query = None,
    body: Annotated[Any, Field(description="JSON body, see describe_operation")] = None,
) -> str:
    """Create or change something through any All Quiet API operation (JSON bodies only)."""
    return _body(await _request(ctx, method, path, path_params, query, body))


@mcp.tool(annotations=DELETE, structured_output=False)
async def call_delete(
    ctx: Context[httpx2.AsyncClient],
    path: Path,
    path_params: PathParams = None,
    query: Query = None,
) -> str:
    """DELETE through any All Quiet API operation. Irreversible."""
    return _body(await _request(ctx, "DELETE", path, path_params, query))


# --- incident tools ---------------------------------------------------------


def _params(**kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}


def _incident(i: dict[str, Any]) -> dict[str, Any]:
    """The fields a responder scans. Status and severity live in the event log."""
    events = sorted(
        i.get("events") or [],
        key=lambda e: (e.get("modification") or {}).get("timestamp") or "",
    )

    def latest(field: str) -> Any:
        return next((e[field] for e in reversed(events) if e.get(field)), None)

    return {
        "id": i.get("id"),
        "title": i.get("title"),
        "status": latest("status"),
        "severity": latest("severity"),
        "createdAt": i.get("createdAt"),
        "lastUpdatedAt": i.get("lastUpdatedAt"),
        "teams": [t.get("displayName") for t in i.get("teams") or []],
        "unattended": i.get("unattended"),
    }


Ids = list[str] | None
Timestamp = Annotated[str | None, Field(description="ISO-8601 UTC")]
Severity = Annotated[str, Field(description="Critical, Warning or Minor")]


@mcp.tool(annotations=READ, structured_output=False)
async def list_incidents(
    ctx: Context[httpx2.AsyncClient],
    statuses: Annotated[Ids, Field(description="e.g. Open, Resolved")] = None,
    severities: Annotated[
        Ids, Field(description="e.g. Critical, Warning, Minor")
    ] = None,
    team_ids: Ids = None,
    user_ids: Ids = None,
    search_term: str | None = None,
    unattended: Annotated[
        bool | None, Field(description="Only incidents nobody took")
    ] = None,
    created_from: Timestamp = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> str:
    """List incidents as compact summaries. Use get_incident for the full timeline."""
    query = _params(
        Statuses=statuses,
        Severities=severities,
        TeamIds=team_ids,
        UserIds=user_ids,
        SearchTerm=search_term,
        Unattended=unattended,
        CreatedFrom=created_from,
        Limit=limit,
        Offset=offset,
    )
    data = (await _request(ctx, "GET", "/incident/search/list", query=query)).json()
    more = bool(data.get("hasMore"))
    return json.dumps(
        {
            "incidents": [_incident(i) for i in data.get("incidents") or []],
            "has_more": more,
            "next_offset": offset + limit if more else None,
        },
        ensure_ascii=False,
    )


@mcp.tool(annotations=READ, structured_output=False)
async def get_incident(ctx: Context[httpx2.AsyncClient], incident_id: str) -> str:
    """Full incident as markdown (timeline, attributes, assignees) plus the intents
    update_incident can record on it now."""
    params = {"incidentId": incident_id}
    md = await _request(ctx, "GET", "/incident/search/{incidentId}/markdown", params)
    # the markdown view omits allowedIntents, so read them from the JSON view
    inc = (await _request(ctx, "GET", "/incident/search/{incidentId}", params)).json()
    intents = ", ".join(inc.get("allowedIntents") or []) or "none"
    return _clip(f"{md.text}\n\nAllowed intents: {intents}")


@mcp.tool(annotations=WRITE, structured_output=False)
async def create_incident(
    ctx: Context[httpx2.AsyncClient],
    title: str,
    severity: Severity,
    status: Annotated[str, Field(description="Open or Resolved")] = "Open",
    team_ids: Ids = None,
    message: str | None = None,
) -> str:
    """Open an incident in one of the key's teams."""
    body = _params(
        title=title, severity=severity, status=status, teamIds=team_ids, message=message
    )
    r = await _request(ctx, "POST", "/incident", body=body)
    return json.dumps(_incident(r.json()), ensure_ascii=False)


@mcp.tool(annotations=WRITE, structured_output=False)
async def update_incident(
    ctx: Context[httpx2.AsyncClient],
    incident_id: str,
    intent: Annotated[
        str | None,
        Field(
            description=(
                "Action to record: Investigated (acknowledge), Resolved, Unresolved, "
                "Commented, Assigned, Escalated, Snoozed, Archived. get_incident shows "
                "which ones the incident allows now"
            )
        ),
    ] = None,
    message: Annotated[str | None, Field(max_length=5000)] = None,
    severity: Severity | None = None,
) -> str:
    """Record an action (resolve, acknowledge, comment...) and/or change severity."""
    ops: dict[str, Any] = {}
    if intent:
        ops["appendIntent"] = _params(intent=intent, message=message)
    elif message:
        raise ToolError("A message is recorded with an intent. Pass intent too.")
    if severity:
        ops["changeSeverity"] = {"severity": severity}
    if not ops:
        raise ToolError("Nothing to change. Pass an intent and/or a severity.")
    path = "/incident/{incidentId}"
    r = await _request(
        ctx, "PATCH", path, {"incidentId": incident_id}, body={"operations": ops}
    )
    # the spec declares a list response, the live API returns the incident itself
    return json.dumps(_incident(r.json()), ensure_ascii=False)


@mcp.tool(annotations=READ, structured_output=False)
async def who_is_on_call(
    ctx: Context[httpx2.AsyncClient],
    team_ids: Ids = None,
    user_ids: Ids = None,
    timestamp: Annotated[
        str | None, Field(description="ISO-8601 UTC, default now")
    ] = None,
) -> str:
    """Who is on call, per team, with escalation tier."""
    query = _params(TeamIds=team_ids, UserIds=user_ids, Timestamp=timestamp)
    return _body(await _request(ctx, "GET", "/on-call", query=query))


def main() -> None:
    mcp.run()
