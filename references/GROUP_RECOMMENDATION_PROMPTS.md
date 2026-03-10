# Group Recommendation Prompt Templates

Use these templates when the user wants stronger, more structured group-based
connection guidance.

## 1. Post-Join Participation Guidance

Use this template right after join success. The goal is **not** to recommend
people yet. The goal is to help the user participate correctly.

```markdown
You have just joined a low-noise discovery group.

First, guide the user to participate correctly.

Output exactly these sections:

## What this group is for
- 1-2 sentences summarizing the group goal

## What you should do now
- step 1
- step 2
- step 3

## Recommended introduction draft
- a 5-part draft that follows the group's `message_prompt`
  1. who you are
  2. what you are working on
  3. what you can offer
  4. who you want to meet
  5. why you joined

## What not to do
- 2-4 bullets reminding the user that this is not a free-chat group

Do not recommend specific people yet unless the available group signal is already strong.
```

## 2. Structured Recommendation Analysis

Use this template when:

- the user explicitly asks for recommendations, or
- the group has enough signal (recommended default: at least 5 members or 5 group-user messages), or
- a periodic refresh is needed because new people or new introductions appeared

```markdown
Analyze the discovery group and recommend valuable people for the user to meet.

Before analysis, refresh state through the scripts:
- `python scripts/manage_group.py --get --group-id <GROUP_ID>`
- `python scripts/manage_group.py --members --group-id <GROUP_ID>`
- `python scripts/manage_group.py --list-messages --group-id <GROUP_ID>`

Source-of-truth rule:
- prefer remote group detail, member list, member profiles, and group messages
- use local SQLite mainly for `contacts` and `relationship_events`

For candidate inspection:
- if a member handle is available, fetch the member profile with `python scripts/get_profile.py --handle <local_part>`
- otherwise fetch the member profile with `python scripts/get_profile.py --did <DID>`

You must inspect:
- group goal and rules
- group message prompt
- group members
- group messages
- structured system events in message metadata when available
- public profile / handle info when available
- local contacts
- local relationship events

Prefer people with:
- clear self-introduction
- explicit offer / ask fit
- strong actionability
- novelty (not already deeply handled locally)

Output exactly this structure:

## Group Snapshot
- group_name:
- group_goal:
- source_type:
- source_name:
- total_members_observed:
- total_group_messages_observed:
- recommendation_basis:

## Recommended Connections

### Candidate 1
- target_handle:
- target_did:
- fit_score: 0-100
- why_this_person:
  - bullet 1
  - bullet 2
  - bullet 3
- evidence:
  - profile_signal:
  - group_message_signal:
  - local_relationship_signal:
- suggested_next_action:
  - save_local | follow | dm | wait
- source_context:
  - group_id:
  - group_name:
  - source_type:
  - source_name:
- save_command:
  - `uv run python scripts/manage_contacts.py --save-from-group --target-did "<DID>" --target-handle "<HANDLE>" --source-type <TYPE> --source-name "<NAME>" --source-group-id <GROUP_ID> --reason "<SHORT_REASON>"`

### Candidate 2
- same structure

### Candidate 3
- same structure

## Do Not Prioritize Yet
- 1-3 people or patterns that are currently weak-signal, with short reasons

## Suggested User Decision
- a short direct question asking whether to save any of the recommended people locally

Rules:
- Do not save contacts automatically
- Do not suggest follow or DM when the fit is still weak
- Prefer concise evidence over long summaries
```

## 3. Periodic Refresh Prompt

Use this template when the user is already in the group and new signals appeared.

```markdown
Re-evaluate this discovery group with a refresh mindset.

Focus on:
- newly joined members
- new introduction messages
- changes since the last recommendation cycle

Before writing the analysis:
- refresh the group snapshot
- refresh the member snapshot
- fetch public profiles for newly joined members by handle or DID
- refresh group messages again so any new introductions are available locally
- treat local SQLite as secondary support data, mainly for contacts / relationship history

Output exactly:

## What changed since last review
- bullet list

## Newly interesting people
- use the same candidate structure as the structured recommendation analysis

## Already handled people
- short list of people already saved / followed / messaged locally

## Suggested next step
- one short recommendation for the user
```

## 4. Confirmation Prompt Before Writing Contacts

Use this template before any `--save-from-group` write.

```markdown
I found the following people worth saving locally:

1. <handle or DID> — <one-line reason>
2. <handle or DID> — <one-line reason>

Would you like me to save any of them into your local contacts with the group source attached?

If yes, I will record:
- source_type
- source_name
- source_group_id
- connected_at
- recommendation reason
- optional note

I will not follow or DM anyone unless you explicitly ask.
```

## 5. Recommendation Quality Checklist

Before using any recommendation output, verify:

- the recommendation reason is specific, not generic
- the evidence points to actual profile / message / local-state signals
- if a system message is involved, the recommendation uses `system_event` instead of only parsing the text content
- the action is proportional to confidence
- the person is not already over-handled locally
- the final user question is explicit and easy to answer
