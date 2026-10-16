---
name: second-brain
description: Answer questions from the owner's own work history (mail, attachments, figures from recurring reports, Teams, WhatsApp, calendar, decisions, actions, people, live topics, past Claude Code sessions) through the second-brain MCP tools. Use it whenever a task needs a fact, a document, a number, a status or a person from that history, including research teammates grounding a deliverable.
---

# second-brain: how to ask it, and how to answer from it

The `second-brain` MCP server (`mcp__second-brain__*`) holds the owner's work history,
extracted and indexed. This file is the routing and answering contract. To load it in
Claude Code, link or copy it to `~/.claude/skills/second-brain/SKILL.md` (or a plugin's
`skills/`); the repository's copy is the source of truth.

## 1. Route by what is asked

| The question | First call | Then, if needed |
| --- | --- | --- |
| A number or KPI ("digital share of loans in August", "contact-centre calls last week") | `figures(query, period=...)` | `search_attachments` for the report, `get_item(kind="attachment", ref, offset)` for the cell's context |
| Find a document or an email | `search_attachments(query)` / `search_emails(query)` | `get_item` on the hit; a `matched_in: "chunk"` row names the passage offsets |
| Status or timeline of a live topic | `topic_page(topic)` | `topic_context(topic)`, `query_decisions(topic=...)`, then primary items for anything load-bearing |
| Who is X, history with X | `person_context(name_or_email, detail="card")` | `detail="full"` for the dossier; pass an address when you have one |
| Prepare a meeting | `meeting_prep(people=[...], topic=...)` | `topic_page` for its topic |
| What did we decide about Y | `query_decisions(topic=Y)` | `get_item` on the source row for the exact wording |
| What do I owe / what is owed to me | `query_actions(status="open")`, `stale_threads()` | the source email through `get_item` |
| A task from an emailed brief | `search_emails` for the brief, `get_item(kind="email", ref)` for the whole mail | `recall` for related material |
| Anything else, or unsure | `recall(query)` | the per-kind tool that matched |
| Mail from the last hours | `outlook_live_search(since_minutes=...)` | the store lags the mailbox |
| Counts, trends, aggregates, full text | `sql_schema()`, then `sql_query(sql)` | its description names the key columns (`emails.date_received`, not `date`) |

`recall` fans out over every index (email, attachments, Teams, WhatsApp, conversations,
decisions, actions, commitments, images, calendar), each bucket keyword plus semantic
plus recency, and opens with `summary`. Use `detail="concise"` when you only need to
see what exists, `kinds=[...]` to narrow, and `include_context=true` only for a person
or topic dossier in the same answer.

## 2. Write the query like the corpus

- Keyword bags of 4 to 6 words, not sentences.
- Most text is Greek with English business terms. Add the Greek word for a Greek subject
  ("δείκτης μετάπτωσης καταναλωτικά", not only "digital migration consumer loans").
  Names match in either script, and Greek words in any inflection.
- For full-text search through `sql_query`, use the FTS tables with `MATCH`, not
  `LIKE '%...%'`; fold both sides with `sb_fold()`.
- Name the period for "latest" questions; ranking already prefers the newest edition and
  lists older ones under `older_editions`.

## 3. Read what the result says about itself

| Field | Meaning | Do |
| --- | --- | --- |
| `data_as_of`, `stale`, `_stale_warning` | The store is a replica and can lag | For anything recent, say how fresh it is; use `outlook_live_search` |
| `reason`, `coverage` on an empty list | What was searched and the dates each source covers | An empty result inside coverage is evidence; outside it, it is a gap, not an absence |
| `excluded` | Matches the default filter hid (news, the owner's own automation mail) | Ask again with `include_news` / `include_automation` when they could answer |
| `history_left_out` | Duplicate or superseded items hidden | `include_history=true` when the history itself is the question |
| `truncated` | Rows cut to fit the answer budget | Narrow the query or lower `limit` |
| `partial_match` | Only some words matched | Treat as weaker |
| `semantic` other than `ok` | Semantic search was unavailable; the answer is keyword-only | Say so if the question was conceptual |

`email_thread`, `teams_thread_context`, `conversation_context`, `sender_brief` and
`teams_chat_summary` are deprecated: use `get_item` and `person_context(detail="card")`.

## 4. Answer rules

1. **Every figure names its source and period**: the report, the period it holds for,
   and the `kind:ref` of the item (e.g. `attachment:1234, sheet KYC, 2026-08`). If the
   period asked is not held, say "not held; latest held: <period>". Never estimate a
   figure the store does not hold, and never fill a gap from memory.
2. **Cite dates and refs** for facts and decisions. A two-year-old fact and last week's
   carry different weight; say which you have.
3. **Topic pages and findings are derived.** Use them to orient; verify any number or
   decision you will put in a deliverable against the primary item they cite.
4. **Say what you could not see**: spoken meetings and calls, mailboxes the store does
   not hold, and anything before a source's coverage start (`stats().coverage`).

## 5. File back what you verified

A research run that settles a claim writes it into its notes in a findings block, so
the next run starts from it. Each line is one JSON object; only `claim` and
`source_ref` are required:

````text
```findings
{"claim": "Share of loan applications made online, year to date", "value": "64.0%", "as_of": "2026-08", "source_ref": "attachment:1234", "quote": "online applications 64.0% of the total"}
```
````

`source_ref` is a primary row's `kind:ref` (email, news, document, attachment, teams,
whatsapp, calendar), `quote` is 12 to 300 characters copied from it holding the value,
`as_of` a date or period not in the future, and `supersedes` an earlier finding with the
same claim (`finding:<id>`). A finding is accepted only when its quote or value occurs in
that source; anything else is stored as rejected and never shown. README "Topic pages
and findings" has the full rules.

## Without an MCP session

`python -m src.cli tool <name> '<json>'` calls any tool from a shell and prints its JSON
answer, for example `python -m src.cli tool figures '{"query": "μετάπτωση καταναλωτικά"}'`.
