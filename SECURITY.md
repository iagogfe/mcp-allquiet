# Security Policy

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Use GitHub's [private vulnerability reporting](https://github.com/iagogfe/mcp-allquiet/security/advisories/new). Do not open a public issue.

Include the version, your MCP client and the steps to reproduce. **Never include a real API key or real incident data**. Redact them or use synthetic values.

You should get a first answer within 7 days.

## Scope

In scope: anything in this repository, for example a way to make the server send the API key to another host, reach a path outside the bundled spec, or leak the key in output or logs.

Out of scope: the All Quiet API itself (report to All Quiet), and actions a model takes with a key that already has the scopes for them.
