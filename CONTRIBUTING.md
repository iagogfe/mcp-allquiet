# Contributing

## Layout

```
src/mcp_allquiet/server.py     the whole server: config, HTTP client, tools
src/mcp_allquiet/openapi.json  bundled All Quiet OpenAPI spec (minified)
tests/test_server.py           in-memory MCP client against a fake All Quiet API
```

## Setup

```bash
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check && uv run ty check
```

Tests never touch the network: `server._transport` is swapped for an `httpx2.MockTransport`.

## Rules

- **Synthetic data only.** Never commit a real API key, a real API response, or real IDs, names or emails from an All Quiet organization, not even in a test fixture. Build fixtures by hand from the schemas in `openapi.json`.
- Write the failing test first.
- A new curated tool needs a reason the generic tools don't cover (a multi-step flow, or output that must be compacted). Otherwise `call_read` / `call_write` already reach it.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org) (`feat:`, `fix:`, `docs:`...).

## Pull requests

Open a PR against `main`. CI (lint, types, tests on Python 3.10 and 3.14) and the security checks must pass.

## Releases

Maintainers bump `version` in `pyproject.toml`, merge, then publish a GitHub release tagged `vX.Y.Z`. The release workflow checks the tag against the version and publishes to PyPI through trusted publishing.
