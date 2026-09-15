"""Tests for mcp-allquiet.

Every value here is synthetic. Never paste a real API response, ID or team name.
"""

import json

import httpx2
import pytest
from mcp import Client

from mcp_allquiet import server

KEY = "aq-test-key-0000"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeAllQuiet:
    """Stands in for the All Quiet API: records every request, returns `response`."""

    def __init__(self):
        self.requests: list[httpx2.Request] = []
        self.response = httpx2.Response(200, json={})

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.response


@pytest.fixture
def api(monkeypatch):
    fake = FakeAllQuiet()
    monkeypatch.setenv("ALLQUIET_API_KEY", KEY)
    monkeypatch.delenv("ALLQUIET_BASE_URL", raising=False)
    monkeypatch.setattr(server, "_transport", httpx2.MockTransport(fake))
    return fake


@pytest.fixture
async def client(api):
    async with Client(server.mcp, raise_exceptions=True) as c:
        yield c


async def call(client, tool, **args):
    result = await client.call_tool(tool, args)
    return result.content[0].text, result.is_error


# --- list_operations / describe_operation ---------------------------------


@pytest.mark.anyio
async def test_list_operations_filters_by_query(client):
    text, is_error = await call(client, "list_operations", query="on-call-override")
    assert not is_error
    assert "GET /on-call-override/{id}" in text
    assert "/team/" not in text


@pytest.mark.anyio
async def test_list_operations_shows_required_scope(client):
    text, _ = await call(client, "list_operations", query="incident/search/list")
    assert "GET /incident/search/list" in text
    assert "incidents:list" in text


@pytest.mark.anyio
async def test_list_operations_marks_operations_without_scope(client):
    text, _ = await call(client, "list_operations", query="/timezone")
    assert text.endswith("[no scope]")


@pytest.mark.anyio
async def test_describe_operation_inlines_schemas(client):
    text, is_error = await call(
        client, "describe_operation", method="POST", path="/incident"
    )
    assert not is_error
    assert "$ref" not in text
    body = json.loads(text)["request_body"]
    assert set(body["required"]) == {"title", "status", "severity"}
    assert (
        body["properties"]["userAssignments"]["items"]["properties"]["userId"]["type"]
        == "string"
    )
    assert "incidents:create" in text


@pytest.mark.anyio
async def test_describe_unknown_operation_is_error(client):
    text, is_error = await call(
        client, "describe_operation", method="GET", path="/nope"
    )
    assert is_error
    assert "list_operations" in text


# --- call_read / call_write / call_delete ---------------------------------


@pytest.mark.anyio
async def test_call_read_sends_bearer_key_to_us_region(client, api):
    api.response = httpx2.Response(200, json={"ok": True})
    text, is_error = await call(client, "call_read", path="/auth/me")
    assert not is_error
    assert json.loads(text) == {"ok": True}
    [req] = api.requests
    assert str(req.url) == "https://allquiet.app/api/public/v1/auth/me"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["accept"] == "application/json"


@pytest.mark.anyio
async def test_base_url_env_selects_eu_region(api, monkeypatch):
    monkeypatch.setenv("ALLQUIET_BASE_URL", "https://allquiet.eu")
    async with Client(server.mcp, raise_exceptions=True) as c:
        await call(c, "call_read", path="/auth/me")
    assert api.requests[0].url.host == "allquiet.eu"


@pytest.mark.anyio
async def test_path_params_are_encoded_as_one_segment(client, api):
    await call(
        client,
        "call_read",
        path="/incident/search/{incidentId}",
        path_params={"incidentId": "../../team"},
    )
    assert (
        api.requests[0].url.raw_path == b"/api/public/v1/incident/search/..%2F..%2Fteam"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "path_params"),
    [
        ("/nope", {}),  # template not in spec
        ("/incident", {}),  # exists, but only as POST
        ("/incident/search/{incidentId}", {}),  # path param missing
        ("https://evil.example/api/public/v1/auth/me", {}),  # absolute URL
    ],
)
async def test_call_read_refuses_bad_paths_without_calling_api(
    client, api, path, path_params
):
    _, is_error = await call(client, "call_read", path=path, path_params=path_params)
    assert is_error
    assert api.requests == []


@pytest.mark.anyio
async def test_array_query_params_repeat_the_key(client, api):
    await call(
        client,
        "call_read",
        path="/incident/search/list",
        query={"Statuses": ["Open", "Resolved"], "Limit": 5},
    )
    params = api.requests[0].url.params
    assert params.get_list("Statuses") == ["Open", "Resolved"]
    assert params["Limit"] == "5"


@pytest.mark.anyio
async def test_403_hints_at_key_scope_and_never_leaks_key(client, api):
    api.response = httpx2.Response(403, json={"error": "forbidden"})
    text, is_error = await call(client, "call_read", path="/auth/me")
    assert is_error
    assert "403" in text
    assert "scope" in text
    assert KEY not in text


@pytest.mark.anyio
async def test_429_reports_retry_after(client, api):
    api.response = httpx2.Response(429, headers={"Retry-After": "37"})
    text, is_error = await call(client, "call_read", path="/auth/me")
    assert is_error
    assert "37" in text


@pytest.mark.anyio
async def test_large_response_is_truncated(client, api):
    api.response = httpx2.Response(200, json={"blob": "x" * 100_000})
    text, is_error = await call(client, "call_read", path="/auth/me")
    assert not is_error
    assert len(text) < 45_000
    assert "truncated" in text


@pytest.mark.anyio
async def test_call_write_sends_json_body(client, api):
    body = {"title": "Disk full", "status": "Open", "severity": "Minor"}
    _, is_error = await call(
        client, "call_write", method="POST", path="/incident", body=body
    )
    assert not is_error
    [req] = api.requests
    assert req.method == "POST"
    assert json.loads(req.content) == body


@pytest.mark.anyio
async def test_call_write_refuses_get(client, api):
    _, is_error = await call(client, "call_write", method="GET", path="/auth/me")
    assert is_error
    assert api.requests == []


@pytest.mark.anyio
async def test_call_delete_sends_delete(client, api):
    _, is_error = await call(
        client, "call_delete", path="/team/{id}", path_params={"id": "t-1"}
    )
    assert not is_error
    [req] = api.requests
    assert req.method == "DELETE"
    assert req.url.path == "/api/public/v1/team/t-1"


@pytest.mark.anyio
async def test_startup_refuses_plain_http_base_url(api, monkeypatch):
    monkeypatch.setenv("ALLQUIET_BASE_URL", "http://allquiet.app")
    with pytest.raises(Exception) as exc:
        async with Client(server.mcp, raise_exceptions=True) as c:
            await call(c, "call_read", path="/auth/me")
    assert "must start with https" in repr(exc.value)
    assert api.requests == []


# --- curated incident tools -----------------------------------------------


def event(ts, status=None, severity=None, message=None):
    return {
        "severity": severity,
        "status": status,
        "modification": {
            "timestamp": ts,
            "intent": None,
            "user": None,
            "externalUser": None,
        },
        "userAssignments": [],
        "onCallUsers": [],
        "message": message,
        "messageIsPublic": False,
        "unattended": False,
        "teams": [],
        "attributeChanges": [],
    }


INCIDENT = {
    "id": "inc-1",
    "title": "Disk full on db-1",
    "integration": {"id": "int-1", "displayName": "Synthetic monitor"},
    "createdAt": "2026-01-01T10:00:00Z",
    "lastUpdatedAt": "2026-01-01T10:30:00Z",
    "attributes": [{"name": "host", "value": "db-1", "isImage": False}],
    # out of order on purpose, and the newest event only carries a comment
    "events": [
        event("2026-01-01T10:00:00Z", "Open", "Critical"),
        event("2026-01-01T10:30:00Z", message="looking"),
        event("2026-01-01T10:20:00Z", "Resolved", "Warning"),
    ],
    "eventsTotalCount": 3,
    "services": [],
    "sourceUser": None,
    "unattended": False,
    "isArchived": False,
    "onCallUsers": [
        {"id": "u-1", "displayName": "Alex", "avatarUrl": None, "email": "a@x.test"}
    ],
    "teams": [{"id": "t-1", "displayName": "Platform"}],
    "snoozed": None,
    "subIncidents": [],
    "allowedIntents": ["Resolved"],
    "canDelete": False,
    "excludeFromUptimeCalculation": False,
    "statusPageStartOverride": None,
    "statusPageEndOverride": None,
}

COMPACT = {
    "id": "inc-1",
    "title": "Disk full on db-1",
    "status": "Resolved",
    "severity": "Warning",
    "createdAt": "2026-01-01T10:00:00Z",
    "lastUpdatedAt": "2026-01-01T10:30:00Z",
    "teams": ["Platform"],
    "unattended": False,
}


@pytest.mark.anyio
async def test_list_incidents_maps_filters_to_api_params(client, api):
    api.response = httpx2.Response(200, json={"incidents": [], "hasMore": False})
    await call(
        client,
        "list_incidents",
        statuses=["Open"],
        severities=["Critical", "Warning"],
        team_ids=["t-1"],
        search_term="disk",
        unattended=True,
        limit=5,
        offset=10,
    )
    [req] = api.requests
    assert req.url.path == "/api/public/v1/incident/search/list"
    p = req.url.params
    assert p.get_list("Statuses") == ["Open"]
    assert p.get_list("Severities") == ["Critical", "Warning"]
    assert p.get_list("TeamIds") == ["t-1"]
    assert (p["SearchTerm"], p["Unattended"], p["Limit"], p["Offset"]) == (
        "disk",
        "true",
        "5",
        "10",
    )
    assert "UserIds" not in p


@pytest.mark.anyio
async def test_list_incidents_defaults_to_small_page(client, api):
    api.response = httpx2.Response(200, json={"incidents": [], "hasMore": False})
    await call(client, "list_incidents")
    p = api.requests[0].url.params
    assert (p["Limit"], p["Offset"]) == ("20", "0")


@pytest.mark.anyio
async def test_list_incidents_returns_current_state_and_next_page(client, api):
    api.response = httpx2.Response(200, json={"incidents": [INCIDENT], "hasMore": True})
    text, is_error = await call(client, "list_incidents", limit=5, offset=10)
    assert not is_error
    assert json.loads(text) == {
        "incidents": [COMPACT],
        "has_more": True,
        "next_offset": 15,
    }


@pytest.mark.anyio
async def test_list_incidents_last_page_has_no_next_offset(client, api):
    api.response = httpx2.Response(
        200, json={"incidents": [INCIDENT], "hasMore": False}
    )
    text, _ = await call(client, "list_incidents")
    assert json.loads(text)["next_offset"] is None


@pytest.mark.anyio
async def test_get_incident_returns_markdown(client, api):
    api.response = httpx2.Response(200, text="# Disk full on db-1\nStatus: Open")
    text, is_error = await call(client, "get_incident", incident_id="inc/1")
    assert not is_error
    assert text == "# Disk full on db-1\nStatus: Open"
    assert (
        api.requests[0].url.raw_path
        == b"/api/public/v1/incident/search/inc%2F1/markdown"
    )


@pytest.mark.anyio
async def test_create_incident_posts_only_given_fields(client, api):
    api.response = httpx2.Response(200, json=INCIDENT)
    text, is_error = await call(
        client,
        "create_incident",
        title="Disk full on db-1",
        severity="Critical",
        team_ids=["t-1"],
    )
    assert not is_error
    [req] = api.requests
    assert (req.method, req.url.path) == ("POST", "/api/public/v1/incident")
    assert json.loads(req.content) == {
        "title": "Disk full on db-1",
        "severity": "Critical",
        "status": "Open",
        "teamIds": ["t-1"],
    }
    assert json.loads(text) == COMPACT


@pytest.mark.anyio
async def test_update_incident_builds_patch_operations(client, api):
    api.response = httpx2.Response(
        200, json={"incidents": [INCIDENT], "hasMore": False}
    )
    _, is_error = await call(
        client,
        "update_incident",
        incident_id="inc-1",
        intent="Resolved",
        message="freed space",
        severity="Minor",
    )
    assert not is_error
    [req] = api.requests
    assert (req.method, req.url.path) == ("PATCH", "/api/public/v1/incident/inc-1")
    assert json.loads(req.content) == {
        "operations": {
            "appendIntent": {"intent": "Resolved", "message": "freed space"},
            "changeSeverity": {"severity": "Minor"},
        }
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "args",
    [
        {},  # nothing to change
        # appendIntent requires an intent, so the message would be dropped silently
        {"message": "orphan note", "severity": "Minor"},
    ],
)
async def test_update_incident_refuses_empty_patch(client, api, args):
    _, is_error = await call(client, "update_incident", incident_id="inc-1", **args)
    assert is_error
    assert api.requests == []


@pytest.mark.anyio
async def test_who_is_on_call_maps_filters(client, api):
    api.response = httpx2.Response(200, json={"memberships": []})
    text, is_error = await call(
        client, "who_is_on_call", team_ids=["t-1"], timestamp="2026-01-01T10:00:00Z"
    )
    assert not is_error
    assert json.loads(text) == {"memberships": []}
    [req] = api.requests
    assert req.url.path == "/api/public/v1/on-call"
    assert req.url.params.get_list("TeamIds") == ["t-1"]
    assert req.url.params["Timestamp"] == "2026-01-01T10:00:00Z"
    assert "UserIds" not in req.url.params


@pytest.mark.anyio
async def test_tool_annotations_mark_reads_and_deletes(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["call_read"].annotations.read_only_hint is True
    assert tools["call_delete"].annotations.destructive_hint is True
    assert tools["call_write"].annotations.read_only_hint is not True
