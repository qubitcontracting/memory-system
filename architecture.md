# Memory System Architecture

## Components

```
Claude Code Session
       │
       ▼
┌─────────────────────────────────┐
│  PostToolUse Hook (<1s)         │
│  context-autosave.js            │
│                                 │
│  At checkpoint thresholds:      │
│  1. Write raw transcript.md     │
│     → ~/basic-memory/sessions/  │
│     (immediately searchable)    │
│                                 │
│  2. Write spool file            │
│     → ~/.claude-mem/spool/      │
│     (enrichment queue)          │
└─────────┬───────────┬───────────┘
          │           │
          ▼           ▼
   Basic Memory    Spool Dir
   (local, always)  (queue)
          │           │
          │           ▼
          │    ┌──────────────────────┐
          │    │  Spool Processor     │
          │    │  cron */10           │
          │    │                      │
          │    │  • Lock file         │
          │    │  • Circuit breaker   │
          │    │  • Reads spool file  │
          │    │  • Calls Ollama      │
          │    │  • Extracts:         │
          │    │    - summary.md      │
          │    │    - facts → entities│
          │    │    - decisions.md    │
          │    │  • Writes to:        │
          │    │    ~/basic-memory/   │
          │    │  • Deletes spool     │
          │    │    only on success   │
          │    └──────────────────────┘
          │           │
          ▼           ▼
┌─────────────────────────────────┐
│  ~/basic-memory/                │
│                                 │
│  sessions/                      │
│    {session}_{checkpoint}.md    │
│    (raw transcripts)            │
│                                 │
│  summaries/                     │
│    {session}_{checkpoint}.md    │
│    (Ollama-generated)           │
│                                 │
│  entities/                      │
│    vader.md                     │
│    deal-finder.md               │
│    mlx-cluster.md               │
│    (appended to over time)      │
│                                 │
│  decisions/                     │
│    2026-04-12.md                │
│    (why choices were made)      │
│                                 │
│  All indexed by Basic Memory:   │
│  • FTS5 (keyword search)        │
│  • FastEmbed (vector search)    │
│  • Wiki-links (graph traversal) │
└─────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────┐
│  Basic Memory MCP Server        │
│  basic-memory mcp               │
│                                 │
│  Claude Code queries via:       │
│  search-notes, build-context,   │
│  read-note, recent-activity     │
└─────────────────────────────────┘
```

## File Flow

### Immediate (hook, no LLM, <1s)
```
Hook fires at 20/40/60/80% context
  → Reads JSONL transcript
  → Writes ~/basic-memory/sessions/{session}_{checkpoint}.md
  → Writes ~/.claude-mem/spool/{session}.json
```

### Background (cron, needs Ollama)
```
Spool processor runs every 10 min
  → Picks up spool file
  → Sends transcript to Ollama
  → Ollama returns structured extraction:
      ## Summary
      ## Facts
      ## Decisions
      ## Entities mentioned
  → Processor writes:
      ~/basic-memory/summaries/{session}_{checkpoint}.md
      ~/basic-memory/entities/{entity}.md  (append)
      ~/basic-memory/decisions/{date}.md   (append)
  → Deletes spool file
```

### Failure Modes
```
Ollama down:
  → Spool file stays on disk
  → Circuit breaker activates (3 failures → backoff)
  → Raw transcript already in Basic Memory, searchable
  → Enrichment happens when Ollama returns

LXC down (future remote setup):
  → Everything runs locally
  → Enriched notes sync when LXC returns

Disk full:
  → Hook fails to write → logged, session continues
  → No data in retrieval, but no crash either

Basic Memory MCP down:
  → Claude Code can't query, but files are on disk
  → Restart: basic-memory mcp
```

## What Changes From Current Setup

| Component | Current | New |
|-----------|---------|-----|
| Hook | context-autosave.js → spool only | Same hook → spool + raw transcript to Basic Memory dir |
| Processor | spool-processor.py → Ollama → Chroma upsert | Same processor → Ollama → markdown files to Basic Memory dir |
| Storage | Chroma (summaries only, lossy) | Basic Memory dir (raw + enriched, lossless) |
| Retrieval | Chroma vector search via MCP | Basic Memory FTS5 + vector + graph via MCP |
| Spool | ~/.claude-mem/spool/ | Same, unchanged |
| Circuit breaker | Same | Same |
| Lock file | Same | Same |
| Cron | */10 spool-processor.py | Same schedule, updated processor |
| LLM config | ~/.claude-mem/llm-config.json | Same |

## What Stays Exactly The Same
- Spool directory and file format
- Circuit breaker logic
- Lock file mechanism
- Cron schedule
- LLM config file
- Checkpoint thresholds (20/40/60/80/+2%)
- Hook trigger (PostToolUse)

## What Gets Modified
1. **context-autosave.js** — add one function: write raw transcript as markdown to Basic Memory dir
2. **spool-processor.py** — change output from Chroma upsert to markdown file writes
3. **~/.claude/settings.json** — add Basic Memory MCP server, keep claude-mem for transition
4. **Ollama prompt** — update to return structured output (summary + facts + entities + decisions)
