# mcp-allquiet

[![CI](https://github.com/iagogfe/mcp-allquiet/actions/workflows/ci.yml/badge.svg)](https://github.com/iagogfe/mcp-allquiet/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/iagogfe/mcp-allquiet)](LICENSE)

An [MCP](https://modelcontextprotocol.io) server for [All Quiet](https://allquiet.app), the incident management and on-call platform. It lets Claude, Cursor or any MCP client triage incidents, check who is on call and reach every operation of the All Quiet public API.

Built on the official MCP Python SDK v2 (`mcp` 2.x). Not affiliated with All Quiet.

## Why

All Quiet has a complete public API (136 operations) but no official MCP server. Exposing one tool per operation would put 136 tool schemas into the model's context before the first question. This server ships **10 tools**:

- **5 incident tools** for the on-call loop, returning compact summaries.
- **5 generic tools** driven by the bundled OpenAPI spec, which reach every other operation (teams, schedules, overrides, integrations, routing, status pages, maintenance windows...).

## Install

Requires [uv](https://docs.astral.sh/uv/) and an All Quiet API key (the API needs a Pro or Enterprise plan).

Claude Code:

```bash
claude mcp add allquiet -e ALLQUIET_API_KEY=your-key -- uvx mcp-allquiet
```

Claude Desktop, Cursor and other clients (`mcpServers` JSON):

```json
{
  "mcpServers": {
    "allquiet": {
      "command": "uvx",
      "args": ["mcp-allquiet"],
      "env": { "ALLQUIET_API_KEY": "your-key" }
    }
  }
}
```

## Configuration

| Variable | Required | Default | |
|---|---|---|---|
| `ALLQUIET_API_KEY` | yes | | Organization API key or personal access token |
| `ALLQUIET_BASE_URL` | no | `https://allquiet.app` | Use `https://allquiet.eu` for the EU region. Must be `https://` |

### The API key decides what the model can do

All Quiet keys carry per-resource scopes (`incidents:list`, `teams:update`...), can be limited to teams, and can expire. The server has no read-only switch on purpose: **create a key with only `list`/`get` scopes** and every write fails at the API. A personal access token can never do more than its user can in the UI.

Keys spanning several organizations need the `organizationId` query parameter on many operations. The API says so in its error, and the model can pass it through the generic tools.

## Tools

| Tool | What it does | Kind |
|---|---|---|
| `list_incidents` | Filter by status, severity, team, user, text, unattended, date. Compact summaries, paged | read |
| `get_incident` | Full incident as markdown: timeline, attributes, assignees | read |
| `create_incident` | Open an incident | write |
| `update_incident` | Record an action (resolve, acknowledge, comment...) and/or change severity | write |
| `who_is_on_call` | Current (or past) on-call per team, with escalation tier | read |
| `list_operations` | Search the 136 API operations, with the key scope each needs | read |
| `describe_operation` | Parameters and request body schema of one operation | read |
| `call_read` | `GET` any operation | read |
| `call_write` | `POST` / `PUT` / `PATCH` any operation (JSON bodies) | write |
| `call_delete` | `DELETE` any operation | destructive |

Tools carry MCP annotations (`readOnlyHint`, `destructiveHint`), so clients can ask before writes and deletes.

## Security

- The key is read from the environment and only sent to the configured `https://` host. It never appears in tool output or error messages (tested).
- Generic tools accept only path templates that exist in the bundled spec. Path parameters are percent-encoded as a single segment, so a value cannot rewrite the path.
- Responses are truncated at 40k characters and error bodies at 2k.
- **Incident content is untrusted input.** Titles, messages and attributes often come from alert payloads written by third parties. Treat what the model reads there as data, and keep write scopes off keys you use with untrusted incident sources.

Report vulnerabilities through [SECURITY.md](SECURITY.md).

## Limitations

- stdio transport only.
- `POST /inbound-integration/{id}/call-routing-media/{purpose}` needs `multipart/form-data` and is not supported.
- No retries. A `429` returns the `Retry-After` value to the model.

## Development

```bash
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check && uv run ty check
```

Try it with the MCP Inspector:

```bash
ALLQUIET_API_KEY=your-key npx @modelcontextprotocol/inspector uv run mcp-allquiet
```

Refresh the bundled OpenAPI spec:

```bash
curl -s https://allquiet.app/api/swagger/public-v1/swagger.json \
  | python -c "import json,sys; json.dump(json.load(sys.stdin), sys.stdout, separators=(',',':'))" \
  > src/mcp_allquiet/openapi.json
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
