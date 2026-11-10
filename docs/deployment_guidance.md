# Deployment Guidance

## Core-first deployment

ChemClaw Core is a standalone Python package with no runtime dependencies. It owns:

- event validation and persistence;
- deterministic policy;
- confirmation state;
- audit records;
- guarded SQLite transactions.

Run the local core first:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
chemclaw init-db
chemclaw seed-demo
chemclaw status
chemclaw ask-inventory "Where is KBr?"
chemclaw serve-api
```

On Windows, activate with:

```powershell
.venv\Scripts\Activate.ps1
```

## Gateway boundary

WebChat, QQ, WeChat, OpenClaw, or another assistant runtime should call the same core
contract. Every gateway must provide:

- a stable source name;
- a stable source message ID;
- the original user-visible message.

Gateways must not write SQLite state directly or bypass local policy. Version 0.3
provides this boundary at `http://127.0.0.1:8765`.

Version 0.4 also exposes `POST /v1/inventory/questions` for read-only inventory
questions. Query execution does not create messages, events, audits, or transactions.

Create external identity mappings before accepting gateway messages:

```bash
chemclaw map-identity openclaw CHANNEL_USER_ID user_001
```

Loopback binding may run without a key for local development. Any non-loopback host
requires an API key supplied through an environment variable:

```bash
export CHEMCLAW_API_KEY="replace-with-a-secret"
chemclaw serve-api --host 0.0.0.0 --api-key-env CHEMCLAW_API_KEY
```

## OpenClaw

OpenClaw is one optional gateway/orchestration runtime. It is useful when an existing
assistant runtime should handle messaging, tools, and sessions, but ChemClaw remains
usable and testable without it.

## Production boundary

v0.4 is a local research prototype. Before production use, add:

- backup and recovery;
- secret management;
- structured operational logging;
- reverse-proxy TLS and network controls;
- real-lab policy configuration and review.
