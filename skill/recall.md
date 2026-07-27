---
name: recall
description: Search the knowledge repository — query 41K+ archived emails and 22K+ attachment summaries by person, topic, keyword, date, decisions, or action items. Returns synthesized context from your institutional memory.
---

# /recall — Knowledge Repository Query

Search and synthesize knowledge from your email archive (41K+ emails, 22K+ attachment summaries, 4+ years as AGM at ACME).

> **Primary path: `mcp__second-brain__recall(query)`** — single fan-out call across mail, attachments, standalone documents, conversations, decisions, actions, inline images, plus auto-pulled person/topic context. Use this first; only drop to the per-kind tools or CLI when you need to filter or paginate beyond what the unified call returns.
>
> The knowledge store is also available via per-kind MCP tools (`mcp__second-brain__search_emails`, `search_attachments`, `search_conversations`, `person_context`, etc.) for targeted queries.

## Usage

`/recall <query>` where query can be:

- **Person**: `/recall Duarte` or `/recall christina.ivanova@example.com`
- **Topic**: `/recall cards migration` or `/recall digital banking`
- **Keyword**: `/recall budget approval Q4`
- **Date range**: `/recall March 2025` or `/recall 2025-01-01 to 2025-03-31`
- **Decisions**: `/recall decisions about cards` or `/recall what did we decide about UX`
- **Action items**: `/recall open actions for Okafor`
- **Person history**: `/recall history with Duarte` or `/recall context for Chen`
- **Topic deep-dive**: `/recall everything about cards migration`
- **Combined**: `/recall Chen digital banking 2025`

## How to Execute

### Step 1: Parse the Query

Determine what the user is looking for:

| Pattern | Query Type | CLI Command |
|---------|-----------|-------------|
| Person name or email | person | `query person <name>` |
| Project/initiative name | topic | `query topic <topic>` |
| General keywords | keyword | `query keyword <keyword>` |
| Date references | date | `query date <start> <end>` |
| "decisions about..." | decisions | `query decisions --topic <T>` |
| "actions for..." | actions | `query actions --owner <O>` |
| "what did we decide about X" | recent decisions | Context API: `get_recent_decisions` + keyword filter |
| "history with Person Y" | person context | Context API: `get_person_context` |
| "pending actions for Topic Z" | topic context | Context API: `get_topic_context` |
| Conceptual/fuzzy questions | semantic | `query semantic "<query>"` |
| Attachment content | keyword | `query keyword <keyword>` (searches attachment text + summaries via FTS5) |
| Multiple filters | combined | `query combined --person P --topic T ...` |

If ambiguous, run multiple query types and merge results.

### Step 2: Run the Query

**Default path — unified MCP recall** (covers mail, attachments, documents, conversations, decisions, actions, images, person/topic context in one call):

```
mcp__second-brain__recall(query="4Q25 PGA", limit_per_kind=5)
```

Drop to per-kind tools or CLI only when you need filters the unified tool doesn't expose (date range, status='completed', etc.):

```bash
cd ~/SourceCode/plessas-second-brain
source .venv/bin/activate
python -m src.cli query <type> <args> --limit 20 -v
```

For combined queries:
```bash
python -m src.cli query combined --person "Duarte" --topic "cards" --start 2025-01-01 --end 2025-12-31 --limit 20 -v
```

For stats overview:
```bash
python -m src.cli stats
```

### Step 3: Synthesize Results

Do NOT dump raw results. Instead, synthesize into a contextual briefing:

**For person queries**, present:
- Communication pattern (frequency, sentiment distribution)
- Key topics discussed
- Recent decisions involving them
- Open action items assigned to/from them
- Relationship context (their role, how they relate to Nikos)

**For topic queries**, present:
- Timeline of key events/decisions
- Key people involved and their roles
- Current status (latest emails)
- Open action items
- Key facts and references

**For keyword/general queries**, present:
- Most relevant results with context
- Related topics and people
- Decision trail if applicable

**For decision queries**, present:
- Decision timeline (chronological)
- Who decided, who was involved
- Context from surrounding emails
- Any follow-up actions

**For semantic queries**, present:
- Most semantically similar results with similarity scores
- Group by theme if results span multiple topics
- Highlight the most relevant findings
- Suggest narrowing with keyword/person/topic filters if too broad

**For action item queries**, present:
- Grouped by status (open/completed)
- Deadline proximity
- Owner and context

### Step 4: Offer Follow-ups

After presenting results, suggest:
- "Want me to dig deeper into [specific topic]?"
- "Should I check for related decisions?"
- "Want to see the full email thread for [specific result]?"

## Database Location

`~/SourceCode/plessas-second-brain/data/brain.db`

## Context API (Rich Queries)

For deeper queries, use the context retrieval API in `src.store.context`:

- **`get_person_context(conn, name_or_email, days=90)`** — full profile: email history, topics, sentiment, decisions, open actions, communication pattern
- **`get_topic_context(conn, topic, days=90)`** — topic overview: emails, key people, decisions, open actions, key facts
- **`get_conversation_context(conn, email_id)`** — thread view: all emails in conversation, participants, decisions, action items
- **`get_recent_decisions(conn, days=30, limit=20)`** — recent decisions with email context and topics

These return structured dicts ready for synthesis. Use them for questions like:
- "What did we decide about X?" — `get_recent_decisions` + keyword search on results
- "What's my history with Person Y?" — `get_person_context`
- "Pending action items for Topic Z" — `get_topic_context` (check `open_actions`)

## Important Notes

- The database stores email summaries, original content, AND 22K+ attachment summaries (PDFs, Word, Excel, PowerPoint)
- Email and attachment content is searchable via FTS5 full-text indexes on summaries, content, key facts, and attachment text
- Semantic search uses embedding similarity for both emails and attachments (requires `python -m src.cli embed` to build index)
- Topics are normalized (lowercase, collapsed whitespace)
- People are deduplicated by email address
- FTS5 search supports standard SQLite full-text query syntax
- All dates are ISO 8601 format
- Results are ordered by date (most recent first) unless otherwise specified
