# Session Startup & Heartbeat Protocol

## Session Startup Flow (Every New Session)

### Step 1: Run Dashboard Check

```bash
cd <SKILL_DIR> && python scripts/check_status.py
```

### Step 2: Parse Status → Decide Next Action

| Status Condition | Recommended Action |
|-----------------|-------------------|
| `identity.status == "no_identity"` | Guide identity creation: `setup_identity.py --name "<name>"` |
| `identity.status == "no_jwt"` | Run `setup_identity.py --load default` to refresh JWT |
| `identity.status == "jwt_refresh_failed"` | Inform the user refresh failed; suggest deleting credentials and recreating |
| `inbox.text_messages > 0` | Display message summary; prompt user to review |
| `len(inbox.e2ee_encrypted_from) > 0` | Inform the user there are unread encrypted messages; use `e2ee_messaging.py --process --peer <DID>` or the WebSocket listener to decrypt |
| `inbox.has_pending_handshakes == true` | Inform the user there are still pending E2EE handshake requests |
| `e2ee_auto.processed > 0` | Report auto-processing results |
| `group_watch.active_groups > 0` | If the user is in an active group discovery cycle, use the listed groups as the heartbeat watch set |
| All normal | "DID identity OK, no unread messages." |

### Step 3: Report Summary (Format)

```
DID identity status: [name] ([DID abbreviated]) - JWT valid/refreshed
Inbox: [N] unread messages
  - [sender]: [count] messages (latest: [time])
E2EE: [active] active / [pending] handshaking
Discovery groups: [N] locally tracked active groups
```

If E2EE auto-processing occurred:
```
E2EE auto-processing: automatically accepted encrypted communication request from [DID]
```

If unread encrypted messages remain:
```
Unread encrypted messages from: [DID, DID...]
Use `e2ee_messaging.py --process --peer <DID>` now, or rely on the WebSocket listener for transparent decryption.
```

### Step 4: Check Profile Completeness

If the user hasn't set up their Profile (`get_profile.py` returns empty or missing nickname/bio), suggest at an appropriate time:
> "Consider completing your Profile — see template: `<SKILL_DIR>/references/PROFILE_TEMPLATE.md`"

Set up incrementally via `update_profile.py`: `--nick-name` / `--bio` / `--tags` / `--profile-md`.

## Heartbeat Check (Every 15 Minutes)

### Trigger Condition

When more than 15 minutes have passed since the last `check_status.py` execution, and the user sends a new message — run the check before processing the user's request.

For users who are actively using a discovery group for relationship discovery,
heartbeat is a **two-phase loop**:

1. run `check_status.py`
2. incrementally refresh and inspect the active discovery-group watch set

Do not treat discovery-group work as a one-shot command. It is an ongoing
heartbeat task whenever the user is actively monitoring a group.

### State Tracking

The Agent should maintain in memory:
- `last_did_check_at`: ISO timestamp of the last check
- `consecutive_failures`: consecutive failure count
- `active_group_watch_ids`: group IDs currently under active discovery monitoring
- `group_watch_state`: per-group memory used for diffing active discovery loops

Recommended `group_watch_state` shape:

```json
{
  "grp_xxx": {
    "last_member_count": 12,
    "last_latest_member_joined_at": "2026-03-10T02:00:00Z",
    "last_latest_owner_message_at": "2026-03-10T02:05:00Z",
    "last_recommendation_at": "2026-03-10T02:10:00Z",
    "initialized": true
  }
}
```

The watch set should usually be empty. Add a group only when:

- the user has just joined it, or
- the user explicitly asks for ongoing group monitoring / recommendations, or
- the session is already in an active recommendation cycle for that group

Remove a group from the watch set when:

- the user leaves the group,
- the recommendation cycle is clearly over, or
- the user explicitly asks to stop monitoring it

### Group Phase (After `check_status.py`)

For each `group_id` in `active_group_watch_ids`, use this rule:

- **Initialize once** when the group has just been joined, has just entered the
  watch set, or local state is missing / obviously stale:

```bash
cd <SKILL_DIR> && python scripts/manage_group.py --get --group-id <GROUP_ID>
cd <SKILL_DIR> && python scripts/manage_group.py --members --group-id <GROUP_ID>
cd <SKILL_DIR> && python scripts/manage_group.py --list-messages --group-id <GROUP_ID>
```

- **Normal heartbeat path** after initialization is incremental. Do **not**
  keep re-running full refreshes on every heartbeat:

```bash
cd <SKILL_DIR> && python scripts/manage_group.py --list-messages --group-id <GROUP_ID> --since-seq <LAST_SYNCED_SEQ>
```

Use `group_watch.groups[].last_synced_seq` from `check_status.py` as the
incremental cursor. This is the normal source for:

- new owner messages
- new self-introductions
- `system_event` member joins / leaves / kicks
- other fresh recommendation signals

- **Fall back to `--members`** only when one of these is true:

- the group has not been initialized yet
- local member snapshot is missing
- you suspect local member drift or incomplete `system_event` coverage
- the user explicitly asks for a full member refresh
- you are running a low-frequency repair / reconciliation cycle

- **Fall back to `--get`** only when one of these is true:

- the group has not been initialized yet
- you need fresh metadata such as rules / goal / message prompt
- the user explicitly asks to inspect the group detail
- you are running a low-frequency repair / reconciliation cycle

Then compare the refreshed state with `group_watch_state` and inspect:

- newly joined members
- new owner messages
- new self-introductions or other strong-fit signals
- whether the group has enough signal for another recommendation cycle

If a member handle is available, refresh their profile with:

```bash
cd <SKILL_DIR> && python scripts/get_profile.py --handle <LOCAL_PART>
```

Otherwise refresh by DID:

```bash
cd <SKILL_DIR> && python scripts/get_profile.py --did <DID>
```

During this phase:

- prefer remote group/member/profile/message data as the source of truth
- use local SQLite mainly for `contacts` and `relationship_events`
- it is safe to record recommendation events automatically
- do **not** save contacts, follow, DM, or post to the group without explicit user confirmation

### Silent Judgment Rules

Only notify the user when any of the following are true; otherwise, remain completely silent:
- `inbox.text_messages > 0`
- `len(inbox.e2ee_encrypted_from) > 0`
- `e2ee_auto.processed > 0`
- `identity.jwt_refreshed == true`
- `identity.status != "ok"`
- a watched group has new joined members
- a watched group has new owner messages
- a watched group now has strong enough signal for fresh recommendations
- a watched group has recommendation candidates that materially changed since the last cycle

### Backoff Strategy

- Success: reset failures to zero
- 1-2 failures: retry normally
- >= 3 failures: pause automatic heartbeat; inform the user
- After user confirmation: reset failures; resume heartbeat

## E2EE Auto-Processing Strategy

**Auto-process (no confirmation needed):**
- `e2ee_init` → accept and establish the session
- `e2ee_rekey` → refresh the session
- `e2ee_error` → log the error / allow follow-up re-handshake logic

**Notify user:**
- "Automatically accepted encrypted communication request from [DID]"
- "E2EE channel with [DID] has been established"

**Do not auto-execute (requires user instruction):**
- Initiating handshakes, sending encrypted messages, decrypting messages

**Important note:** `check_status.py` auto-processes E2EE protocol messages by default. It does **not** decrypt unread `e2ee_msg` content into plaintext. For actual plaintext delivery, use `e2ee_messaging.py --process --peer <DID>` or run the WebSocket listener. Use `--no-auto-e2ee` only when you explicitly want to disable this behavior.

**Design rationale:** The E2EE protocol has no rejection mechanism, and handshake messages expire after 5 minutes. Auto-accepting avoids timeouts; notifying the user maintains transparency.

## check_status.py Output Field Reference

With the default E2EE auto-processing behavior enabled, the reported `inbox`
snapshot reflects the post-auto-processing state.

| Field Path | Type | Description |
|-----------|------|-------------|
| `timestamp` | string | UTC ISO timestamp |
| `identity.status` | string | `"ok"` / `"no_identity"` / `"no_jwt"` / `"jwt_refresh_failed"` |
| `identity.did` | string\|null | DID identifier |
| `identity.name` | string\|null | Identity name |
| `identity.jwt_valid` | bool | Whether JWT is valid |
| `identity.jwt_refreshed` | bool | Whether JWT was refreshed this time (only present on refresh) |
| `identity.error` | string | Error description (only present on jwt_refresh_failed) |
| `inbox.status` | string | `"ok"` / `"no_identity"` / `"error"` / `"skipped"` |
| `inbox.total` | int | Total inbox message count |
| `inbox.text_messages` | int | Plain text unread count (excluding E2EE protocol messages) |
| `inbox.text_by_sender` | object | `{did: {count: int, latest: string}}` |
| `inbox.has_pending_handshakes` | bool | Whether there are pending E2EE handshakes |
| `inbox.e2ee_handshake_pending` | list | List of DIDs that initiated handshakes |
| `inbox.e2ee_encrypted_from` | list | List of DIDs that sent unread encrypted messages which still require `--process` or WebSocket listener decryption |
| `inbox.by_type` | object | Count by message type `{type: count}` |
| `group_watch.status` | string | `"ok"` / `"no_identity"` / `"error"` / `"skipped"` |
| `group_watch.active_groups` | int | Number of locally tracked active discovery groups |
| `group_watch.groups_with_pending_recommendations` | int | Number of active groups that still have pending `ai_recommended` events |
| `group_watch.groups` | list | Per-group local heartbeat summary entries |
| `group_watch.groups[].group_id` | string | Group identifier |
| `group_watch.groups[].name` | string\|null | Local group display name |
| `group_watch.groups[].slug` | string\|null | Local group slug |
| `group_watch.groups[].my_role` | string\|null | Local role in the group |
| `group_watch.groups[].member_count` | int\|null | Last known remote member count snapshot |
| `group_watch.groups[].tracked_active_members` | int | Active members present in the local member snapshot |
| `group_watch.groups[].group_owner_did` | string\|null | Remote group owner DID |
| `group_watch.groups[].group_owner_handle` | string\|null | Remote group owner handle |
| `group_watch.groups[].local_group_user_messages` | int | Local cached `group_user` message count |
| `group_watch.groups[].local_owner_messages` | int | Local cached owner-authored `group_user` message count |
| `group_watch.groups[].latest_owner_message_at` | string\|null | Latest locally cached owner message timestamp |
| `group_watch.groups[].latest_member_joined_at` | string\|null | Latest locally cached active-member join timestamp |
| `group_watch.groups[].pending_recommendations` | int | Pending `ai_recommended` events for this group |
| `group_watch.groups[].last_recommended_at` | string\|null | Latest local recommendation event timestamp |
| `group_watch.groups[].saved_contacts` | int | Contacts already confirmed from this group |
| `group_watch.groups[].recommendation_signal_ready` | bool | Whether the local snapshot already meets the default recommendation threshold |
| `group_watch.groups[].last_synced_seq` | int\|null | Last locally synced group message sequence; use it as the next incremental `--since-seq` cursor |
| `group_watch.groups[].last_read_seq` | int\|null | Last locally tracked read sequence |
| `group_watch.groups[].last_message_at` | string\|null | Latest known group message timestamp |
| `group_watch.groups[].stored_at` | string | Timestamp of the local group snapshot update |
| `e2ee_auto.status` | string | `"ok"` / `"no_identity"` / `"error"` (present unless `--no-auto-e2ee` disables it) |
| `e2ee_auto.processed` | int | Number auto-processed this time (present unless `--no-auto-e2ee` disables it) |
| `e2ee_auto.details` | list | Processing details (present unless `--no-auto-e2ee` disables it) |
| `e2ee_auto.error` | string | Error description (only when status is error) |
| `e2ee_sessions.active` | int | Active E2EE session count |
| `e2ee_sessions.pending` | int | Handshaking E2EE session count |
