# Workflows

Workflows is an outcome journal for the background work in your Möbius chats. The journal leads with completed outcomes and groups activity by day. “Needs your input” gathers only unresolved work that calls for an owner decision, and pairs every item with a reason and a concrete next step.

Open an entry to see a chronological execution timeline. Time flows downward on a fixed main-agent lane; concurrent helpers occupy temporary side lanes, and nested helpers connect to the helper that launched them. Each lane shows the task and lifecycle at a glance, while the full prompt stays one click away.

## What it records

The journal records three layers of background activity:

- The outcome and status of each chat with background activity.
- A time-based main-agent timeline with concurrent and nested helper lanes.
- Each helper’s task summary, full prompt, duration, and honestly recorded lifecycle.

## Evidence and status

Workflows derives status from recorded artifacts. It never asks a model to judge whether its own work succeeded. A background-launch acknowledgement never counts as a completion report, and active work stays separate from owner attention. A failed helper remains visible in the timeline without alarming the owner when the main agent subsequently delivered and verified the requested result. Explicit root failures, explicit stops, and unconfirmed results remain actionable. Later terminal evidence can still resolve them.

The parser scrubs secret-shaped values from free text and caps it before publication. The interface omits missing fields instead of inventing values. Sequential retries with the same parent and exact full prompt become one task lane only when the preceding attempt was unresolved; overlapping launches and work repeated after completion stay distinct. Collapsed retries retain an attempt count and state mix. Resolved records without a usable task summary are summarized at the skim layer. Retention metadata reports both, while distinct work and unresolved attempt states remain visible. If the storage safety cap removes an old helper page, the chat history stays visible without a broken drill-in.

## How Workflows builds the journal

The scheduled and on-demand refresh job consumes the platform’s normalized lifecycle feed and incrementally scans local Claude and Codex traces for prompt and fallback evidence. It joins sessions to chats only through explicit link evidence, then publishes bounded schema-v4 documents to the app’s storage. The interface reads only those documents. Job diagnostics retain unlinked traces instead of guessing which chat owns them.

The scan budget splits large histories across multiple runs. Separate timeouts bound metadata reads and storage writes. A full refresh can therefore take longer than the scan budget.

## Install

Install Workflows from the Möbius **App Store**, or point an instance at this manifest:

```text
https://raw.githubusercontent.com/mobius-os/app-workflows/main/mobius.json
```

MIT licensed.
