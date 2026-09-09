---
name: recall
description: Search the knowledge repository by person, topic, keyword, date, decision or action item, and return synthesized context rather than raw rows.
---

# recall: knowledge repository query

Reference prompt for querying the second-brain store. **Nothing installs this
file.** It is not a slash command and not a discovered skill: Claude Code finds
commands under `.claude/commands/` and skills under `.claude/skills/<name>/SKILL.md`
or a plugin, and a bare `skill/recall.md` at a repo root is none of those. Copy
it to one of those locations, or into a plugin, if you want it to load. Until
then it is documentation of the intended workflow.

> **Primary path: `mcp__second-brain__recall(query)`.** One fan-out call across
> every text-bearing index, plus auto-pulled person and topic context. Use it
> first; drop to the per-kind tools only when you need a filter it does not
> expose.
>
> Per-kind tools: `search_emails`, `search_attachments`, `search_teams`,
> `search_conversations`, `query_calendar_events`, `query_decisions`,
> `query_actions`. Dossier tools: `person_context`, `topic_context`,
> `sender_brief`, `meeting_prep`. Corpus size, date range and freshness:
> `stats`.

## Usage

Query forms that route sensibly:

- **Person**: `Duarte`, `christina.ivanova@example.com`
- **Topic**: `cards migration`, `digital banking`
- **Keyword**: `budget approval Q4`
- **Date range**: `March 2025`, `2025-01-01 to 2025-03-31`
- **Decisions**: `decisions about cards`, `what did we decide about UX`
- **Action items**: `open actions for Okafor`
- **Person history**: `history with Duarte`, `context for Chen`
- **Topic deep-dive**: `everything about cards migration`
- **Combined**: `Chen digital banking 2025`

## Step 1: parse the query

| Pattern | Query type | Tool |
| --- | --- | --- |
| Person name or email | person | `person_context` |
| Project or initiative name | topic | `topic_context` |
| General keywords | keyword | `recall`, or `search_emails` |
| Date references | date | `query_emails` with `start_date` / `end_date` |
| "decisions about..." | decisions | `query_decisions` |
| "actions for..." | actions | `query_actions` |
| Conceptual or fuzzy questions | semantic | `search_emails(search_type="semantic")` |
| Attachment content | attachments | `search_attachments` |
| Multiple filters | combined | `query_emails` |

If it is ambiguous, call `recall` and let the fan-out decide.

## Step 2: run the query

Default path, one call:

```text
mcp__second-brain__recall(query="cards migration", limit_per_kind=5)
```

It returns nine buckets, keyed exactly as listed: `emails` (which also covers
standalone documents and news, since they share the `emails` table),
`attachments`, `conversations`, `decisions`, `actions`, `commitments`,
`inline_images`, `teams`, `calendar_events`. `summary.kinds_with_results` names
the ones that matched. Only the `emails` bucket fuses keyword and semantic
ranking; the rest are keyword-only.

**Check freshness before answering about anything recent.** The database is
usually a replica of a machine that builds it elsewhere, so it lags. `recall`
attaches `_stale_warning` and `data_as_of` when the copy is behind, and `stats`
always returns `data_as_of`, `age_hours` and `stale`. For mail newer than the
replica, use `outlook_live_search`.

The CLI mirrors the same queries when no MCP session is available:

```bash
python -m src.cli query keyword "budget approval" --limit 20 -v
python -m src.cli query combined --person "Duarte" --topic "cards" \
  --start 2025-01-01 --end 2025-12-31 --limit 20 -v
python -m src.cli stats
```

## Step 3: synthesize

Do not dump raw results. Turn them into a briefing.

**Person queries:**

- Communication pattern (frequency, sentiment distribution)
- Key topics discussed
- Recent decisions involving them
- Open action items assigned to or from them
- Their role, and how they relate to the user (`BRAIN_USER_NAME`)

**Topic queries:**

- Timeline of key events and decisions
- Key people involved and their roles
- Current status, from the most recent items
- Open action items
- Key facts and references

**Keyword or general queries:**

- Most relevant results, with context
- Related topics and people
- The decision trail, if there is one

**Decision queries:**

- Decision timeline, chronological
- Who decided, who was involved
- Context from the surrounding emails
- Any follow-up actions

**Semantic queries:**

- Most similar results, with similarity scores
- Grouped by theme when results span several topics
- A suggestion to narrow by keyword, person or topic if the spread is too wide

**Action item queries:**

- Grouped by status (open, completed)
- Deadline proximity
- Owner and context

Cite dates and provenance. A fact from a 2019 email and a fact from last week
carry different weight, and the reader cannot tell them apart unless you say so.

## Step 4: offer follow-ups

- "Want me to dig deeper into [topic]?"
- "Should I check for related decisions?"
- "Want the full thread for [result]?"

## Context API

For deeper queries, `src.store.context` is what the dossier tools call:

- **`get_person_context(conn, name_or_email, days=365, limit=20)`**: email
  history, topics, sentiment, decisions, open actions, communication pattern
- **`get_topic_context(conn, topic, days=365, limit=20)`**: emails, key people,
  decisions, open actions, key facts
- **`get_conversation_context(conn, email_id)`**: all emails in a thread, with
  participants, decisions and action items
- **`get_recent_decisions(conn, days=365, limit=20)`**: recent decisions with
  email context and topics

The first two cap every list at `limit` and return a `<name>_total` sibling
(`topics_total`, `decisions_total`, `open_actions_total`, and so on) with the
real count, so a truncated answer is distinguishable from a complete one. Say
which one you have.

## Notes

- The store holds email summaries, original content, and LLM summaries of
  attachments (PDF, Word, Excel, PowerPoint), plus Teams messages, calendar
  events, news digests and past Claude Code conversations. Call `stats` for
  current counts and the date range; do not quote a number from memory.
- FTS5 full-text indexes cover summaries, content, key facts and attachment text.
- Semantic search uses embedding similarity and needs an index built by
  `python -m src.cli embed`. Without it, search degrades to keyword-only.
- Topics are normalized (lowercase, collapsed whitespace, accent-folded).
- People are deduplicated by email address, then by a Greek-aware name
  canonicalization pass.
- All dates are ISO 8601.
- Results are ordered most recent first unless stated otherwise.
