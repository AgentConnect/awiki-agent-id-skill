# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Documentation Language Policy

All documentation in this repository MUST be written in English, including:
- Markdown files (.md)
- Python docstrings and module headers
- Code comments
- CLI help text and user-facing messages

The only exception is `README_zh.md`, which is the Chinese translation of the README.

## Project Overview

DID (Decentralized Identifier) identity interaction Skill. Built on the ANP protocol, it provides Claude Code with DID identity management, messaging, social relationships, and E2EE end-to-end encrypted communication capabilities. Runs as a Claude Code Skill, configured via SKILL.md.

## Commands

All scripts must be run from the project root (`python scripts/<name>.py`). Python automatically adds `scripts/` to `sys.path` for resolving `from utils import ...` imports. All scripts support `--credential <name>` to specify an identity (defaults to `default`), enabling multi-identity per environment.

```bash
# Install dependencies
pip install -r requirements.txt

# Identity management
python scripts/setup_identity.py --name "AgentName"          # Create identity
python scripts/setup_identity.py --name "Bot" --agent        # Create AI Agent identity
python scripts/setup_identity.py --load default               # Load identity (auto-refresh expired JWT)
python scripts/setup_identity.py --list                       # List identities
python scripts/setup_identity.py --delete myid                # Delete identity

# Profile management
python scripts/get_profile.py                                 # View own Profile
python scripts/get_profile.py --did "<DID>"                   # View another user's public Profile
python scripts/get_profile.py --resolve "<DID>"               # Resolve DID document
python scripts/update_profile.py --nick-name "Name" --bio "Bio" --tags "tag1,tag2"

# Messaging (requires identity creation first)
python scripts/send_message.py --to "<DID>" --content "hello"
python scripts/check_inbox.py
python scripts/check_inbox.py --history "<DID>"               # Chat history with a specific user
python scripts/check_inbox.py --mark-read msg_id_1 msg_id_2

# Social relationships
python scripts/manage_relationship.py --follow "<DID>"
python scripts/manage_relationship.py --unfollow "<DID>"
python scripts/manage_relationship.py --status "<DID>"
python scripts/manage_relationship.py --following
python scripts/manage_relationship.py --followers

# Group management
python scripts/manage_group.py --create --group-name "GroupName" --description "Description"
python scripts/manage_group.py --invite --group-id GID --target-did "<DID>"
python scripts/manage_group.py --join --group-id GID --invite-id IID
python scripts/manage_group.py --members --group-id GID

# E2EE encrypted communication
python scripts/e2ee_messaging.py --handshake "<DID>"
python scripts/e2ee_messaging.py --process --peer "<DID>"
python scripts/e2ee_messaging.py --send "<DID>" --content "secret"

# Unified status check
python scripts/check_status.py                              # Basic status check
python scripts/check_status.py --auto-e2ee                  # With E2EE auto-processing
python scripts/check_status.py --credential alice            # Specify credential

# WebSocket listener (background service management)
python scripts/ws_listener.py install --credential default --mode smart  # Install and start
python scripts/ws_listener.py install --credential default --config service/listener.json  # Install with config
python scripts/ws_listener.py status                        # View service status
python scripts/ws_listener.py stop                          # Stop service
python scripts/ws_listener.py start                         # Start installed service
python scripts/ws_listener.py uninstall                     # Uninstall service
python scripts/ws_listener.py run --credential default --mode smart -v  # Run in foreground for debugging

# Local data queries (read-only SQL against SQLite)
python scripts/query_db.py "SELECT * FROM threads LIMIT 10"
python scripts/query_db.py "SELECT * FROM contacts"
python scripts/query_db.py --credential alice "SELECT COUNT(*) as cnt FROM messages"
```

## Architecture

Three-layer architecture: CLI script layer -> Persistence layer -> Core utility layer.

### scripts/utils/ — Core Utility Layer (pure async)

- **config.py**: `SDKConfig` dataclass, reads service addresses from environment variables. `data_dir` field resolves `AWIKI_DATA_DIR` env var with `<SKILL_DIR>/.data/` fallback. `load()` class method reads `<DATA_DIR>/settings.json` with env var overrides
- **identity.py**: `DIDIdentity` data class + `create_identity()` wrapping ANP's `create_did_wba_document_with_key_binding()`. Generates secp256k1 key pair + E2EE key pairs (key-2 secp256r1 for signing + key-3 X25519 for key agreement). Public key fingerprint auto-constructs key-bound DID path (k1_{fp}) + DID document + WBA proof
- **auth.py**: Complete authentication pipeline — `create_authenticated_identity()` chains: create identity -> `register_did()` register -> `get_jwt_via_wba()` obtain JWT
- **client.py**: httpx AsyncClient factory (`create_user_service_client`, `create_molt_message_client`), 30s timeout, `trust_env=False`
- **rpc.py**: JSON-RPC 2.0 client wrapper, `rpc_call()` sends requests, `JsonRpcError` wraps errors
- **e2ee.py**: `E2eeClient` — Uses HPKE (RFC 9180, X25519 key agreement + chain Ratchet forward secrecy). One-step initialization (no multi-step handshake). Key separation: key-2 secp256r1 for signing + key-3 X25519 for key agreement (separate from DID's secp256k1). Supports `export_state()`/`from_state()` for cross-process state recovery
- **ws.py**: `WsClient` — WebSocket client wrapper. Uses JWT query parameter authentication to connect to molt-message `/message/ws` endpoint. Supports JSON-RPC request/response, push notification reception, application-layer heartbeat (ping/pong)
- **resolve.py**: `resolve_to_did()` — Handle-to-DID resolution. If input starts with `did:`, returns as-is. Otherwise calls `GET /user-service/.well-known/handle/{local_part}` (no auth required). Always resolves via server, no local cache
- **`__init__.py`**: Package entry point, centralized export of all public APIs (`SDKConfig`, `DIDIdentity`, `rpc_call`, `E2eeClient`, `resolve_to_did`, etc.)

### scripts/ — CLI Script Layer

- **credential_store.py** / **e2ee_store.py**: Credential and E2EE state persistence to `.credentials/` directory (JSON format, 600 permissions)
- **local_store.py**: SQLite local storage — contacts + messages two tables, threads/inbox/outbox views. WAL mode for concurrent read/write. Sync API (sqlite3 stdlib), ws_listener wraps via `asyncio.to_thread()`. Schema versioned via `PRAGMA user_version`
- **query_db.py**: Read-only SQL query CLI — accepts a SELECT statement, executes against local SQLite, returns JSON. Rejects write operations and multi-statement queries
- **check_status.py**: Unified status check entry point — chains identity verification, inbox classification summary, E2EE auto-handshake processing. Outputs structured JSON. Called by Agent session startup protocol and heartbeat
- **listener_config.py**: `ListenerConfig` + `RoutingRules` — WebSocket listener configuration module. Defines dual webhook endpoints, routing modes (agent-all/smart/wake-all), message routing rules and E2EE transparent processing parameters. Supports unified settings.json (`listener` sub-object) + legacy JSON file + environment variables + CLI four-level override
- **e2ee_handler.py**: `E2eeHandler` — E2EE transparent handler for WebSocket listener. Intercepts E2EE messages before `classify_message`: protocol messages (init/rekey/error) are handled internally without forwarding, encrypted messages (e2ee_msg) are decrypted and forwarded as plaintext. asyncio.Lock protects concurrency, periodic state saving
- **ws_listener.py**: WebSocket listener — persistent background process + cross-platform service lifecycle management. Reuses `WsClient` to connect to molt-message WebSocket. E2EE messages handled transparently by `E2eeHandler` (optional). Received messages stored to local SQLite via `local_store`. Others routed via `classify_message()` (agent/wake/discard) and forwarded to corresponding localhost webhook endpoints. Subcommands: `run` (foreground debug), `install` (install background service), `uninstall`, `start`/`stop`/`status` (management). Service management delegated to `service_manager.py`
- **service_manager.py**: `ServiceManager` base class + `MacOSServiceManager` (launchd) / `LinuxServiceManager` (systemd) / `WindowsServiceManager` (Task Scheduler) + `get_service_manager()` factory. Handles install/uninstall/start/stop/status for each platform
- Other scripts are CLI entry points for each feature, wrapping async calls via `asyncio.run()`

### service/ — Cross-Platform Service Management

- **listener.example.json**: Routing rules + E2EE configuration example (webhook URLs, whitelist, blacklist, keywords, E2EE toggle, etc.)
- **settings.example.json**: Unified configuration template (service URLs + listener config in one file, replaces separate listener.json)
- **README.md**: Cross-platform deployment guide (macOS launchd / Linux systemd / Windows Task Scheduler)

### tests/ — Unit Tests

- **test_local_store.py**: Local SQLite storage unit tests (schema, CRUD, idempotent dedup, thread_id generation, read-only SQL safety)
- Listener and resolve tests have been migrated to `awiki-system-test/tests/` (listener/ and did/test_resolve.py).

## Source File Header Convention

All source files must include a structured header comment:

```python
"""Brief module description.

[INPUT]: External dependencies and inputs
[OUTPUT]: Exported functions/classes
[POS]: Module's position in the architecture

[PROTOCOL]:
1. Update this header when logic changes
2. Check the containing folder's CLAUDE.md after updates
"""
```

When modifying code logic, the corresponding file's `[INPUT]/[OUTPUT]/[POS]` header must be updated accordingly.

## Key Design Decisions

**Three-Key System**: DID identity uses secp256k1 key-1 (identity proof + WBA signing). E2EE uses secp256r1 key-2 (proof signing) + X25519 key-3 (HPKE key agreement). Three key sets are stored separately and support independent rotation.

**E2EE State Persistence**: `E2eeClient.export_state()` serializes ACTIVE session state (with version="hpke_v1" marker), `from_state()` restores it. Legacy formats are automatically discarded. ACTIVE sessions expire after 24 hours. One-step initialization means no PENDING concept.

**E2EE Inbox Processing Order**: Dual sorting by `created_at` timestamp + protocol type (init < rekey < e2ee_msg < error) ensures initialization is processed before encrypted messages.

**RPC Endpoint Paths**: Authentication via `/user-service/did-auth/rpc`, messaging via `/message/rpc`, Profile via `/user-service/profile/rpc`, groups/relationships via `/user-service/did/relationships/rpc`. The `/user-service` prefix supports nginx reverse proxy.

## Constraints

- **ANP >= 0.6.1** is a hard dependency, providing DID and E2EE (HPKE) cryptographic primitives
- **Python >= 3.10**
- All network operations must use async/await (httpx AsyncClient)
- `.credentials/` directory must remain gitignored, private key files with 600 permissions
- `.data/` directory must remain gitignored (local SQLite databases and settings)
- API reference documents are in the `references/` directory (did-auth-api.md, profile-api.md, messaging-api.md, relationship-api.md, e2ee-protocol.md, local-store-schema.md)

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `AWIKI_DATA_DIR` | `<SKILL_DIR>/.data/` | DATA_DIR path (SQLite, settings.json) |
| `E2E_USER_SERVICE_URL` | `https://awiki.ai` | user-service address |
| `E2E_MOLT_MESSAGE_URL` | `https://awiki.ai` | molt-message address |
| `E2E_DID_DOMAIN` | `awiki.ai` | DID domain (proof binding) |
