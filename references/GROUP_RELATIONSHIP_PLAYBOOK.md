# Group Relationship Playbook

Use this guide when the user wants help discovering valuable people in a discovery
group and turning those discoveries into durable local relationship data.

## Goals

1. Help the user participate correctly in the group
2. Identify people worth connecting with
3. Explain *why* they are worth connecting with
4. Save confirmed contacts locally with source-group context

## Step 1: Guide the User on What to Do in the Group

Immediately after join success:

- remind the user this is a **discovery group**, not a free-chat room
- ask the user to send a concise introduction that covers:
  - who they are
  - what they are working on
  - what they can offer
  - who they want to meet
  - why they joined
- encourage the user to keep follow-up actions focused on:
  - follow
  - private message
  - save to local contacts

## Step 2: Read the Right Inputs

Before recommending anyone, inspect:

- `groups`
- `group_members`
- `messages`
- the target member's public profile / handle if available
- `contacts`
- `relationship_events`

Recommended query patterns:

```bash
uv run python scripts/query_db.py "SELECT * FROM groups WHERE group_id='grp_xxx'"
uv run python scripts/query_db.py "SELECT * FROM group_members WHERE group_id='grp_xxx' ORDER BY role, member_handle"
uv run python scripts/query_db.py "SELECT sender_did, content, server_seq FROM messages WHERE group_id='grp_xxx' ORDER BY server_seq"
uv run python scripts/query_db.py "SELECT did, handle, source_group_id, recommended_reason, followed, messaged FROM contacts ORDER BY connected_at DESC"
uv run python scripts/query_db.py "SELECT target_did, event_type, status, reason, created_at FROM relationship_events ORDER BY created_at DESC LIMIT 50"
```

## Step 3: Decide Who Is Worth Recommending

Prefer people who show all or most of these signals:

- clear self-introduction
- explicit offer / ask fit with the user
- overlapping domain, project, geography, or event context
- strong actionability ("looking for collaborators", "hiring", "seeking protocol partners", etc.)
- not already followed or already deeply handled locally

Avoid recommending:

- people with vague or empty introductions
- people already saved locally with enough follow-up
- people whose ask / offer is unrelated to the user's goals

## Step 4: Output Recommendations in a Fixed Structure

Use the stronger structured templates in
[GROUP_RECOMMENDATION_PROMPTS.md](GROUP_RECOMMENDATION_PROMPTS.md).

At minimum, recommendation output must include:

1. **Group snapshot**
2. **Candidates**
3. **Evidence per candidate**
4. **Suggested next action**
5. **An explicit confirmation question before any local save**

## Step 5: Confirm Before Writing Contacts

The agent may record recommendation events automatically, but it must **not**
save a contact into the local `contacts` snapshot until the user confirms.

Use the following write paths:

- Record a recommendation candidate:
  ```bash
  uv run python scripts/manage_contacts.py --record-recommendation --target-did "<DID>" --target-handle "<HANDLE>" --source-type meetup --source-name "OpenClaw Meetup Hangzhou 2026" --source-group-id grp_xxx --reason "Strong fit"
  ```
- Save a confirmed contact:
  ```bash
  uv run python scripts/manage_contacts.py --save-from-group --target-did "<DID>" --target-handle "<HANDLE>" --source-type meetup --source-name "OpenClaw Meetup Hangzhou 2026" --source-group-id grp_xxx --reason "Strong fit"
  ```
- Mark follow or DM state:
  ```bash
  uv run python scripts/manage_contacts.py --mark-followed --target-did "<DID>"
  uv run python scripts/manage_contacts.py --mark-messaged --target-did "<DID>"
  ```

## What to Record for a Confirmed Contact

Always preserve:

- target DID
- target handle if known
- source type
- source name
- source group ID
- connection time
- recommendation reason
- follow state
- message state
- local note

## Trigger Guidance

- **After join success**: explain how the user should participate
- **After enough signal**: offer to recommend valuable people
- **Periodically**: if there are new members or new introductions, offer a refresh

Recommended minimum signal before proactive recommendation:

- at least 5 group members, or
- at least 5 group user messages
