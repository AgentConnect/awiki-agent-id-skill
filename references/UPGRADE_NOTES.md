# Upgrade Notes

Recent upgrades in this release line are grouped into core features and optimizations.

## Core Features

- **Discovery group management**: groups are now presented as a core collaboration feature. Users can create discovery groups with `name`, `slug`, `description`, `goal`, `rules`, and `message_prompt`; get or refresh the global 6-digit join code; enable or disable joining; join with `--passcode`; list members and messages; post group messages; and fetch the public Markdown entry document.
- **Handle recovery flow**: `recover_handle.py` can rebind a Handle to a new DID, back up the old local credential, migrate local message ownership, and clear stale E2EE state.

## Optimizations

- **Simpler encrypted send-first flow**: `e2ee_messaging.py --send` now auto-initializes the session more reliably, so users usually do not need a manual pre-handshake.
- **Safer local persistence**: credentials now use an indexed per-credential layout, local SQLite data is isolated by `owner_did`, and missing database indexes can self-heal during migration.
- **Messaging quality-of-life updates**: messages support an optional `title`, failed encrypted sends are tracked in `e2ee_outbox`, and retry/drop actions are available for later repair.
- **Better operations visibility**: daily rotating data logs and `check_status.py` make upgrades, listener troubleshooting, and credential migration easier to verify.
