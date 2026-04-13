# Memory System Architecture v2

## Stack

| Layer | Software | Purpose | Install |
|-------|----------|---------|---------|
| Text storage + search | Basic Memory (stock) | Markdown files, FTS5, FastEmbed vectors, wiki-link graph | `uv tool install basic-memory` |
| Graph database | FalkorDBLite (embedded) | Typed relationships, dependency chains, temporal queries | `pip install falkordblite` |
| Entity extraction | Cognee (library only) | Entity resolution, relationship discovery from raw text | `pip install cognee` in venv |
| LLM backend | Ollama on vader | Powers Cognee extraction + Librarian fallback prompts | Already running |
| LLM routing | litellm (via Cognee) | Ollama connection, fallback chains | Comes with Cognee |
| Retrieval | Custom MCP server | Queries BM + FalkorDBLite, merges results | New, lightweight |
| Spool queue | Existing spool system | Filesystem queue with circuit breaker | Already running |

## Data Flow

```
Claude Code Session
       │
       ▼
┌──────────────────────────────────────┐
│  Hook: context-autosave.js           │
│  (PostToolUse, <1s, no LLM)         │
│                                      │
│  1. Write raw transcript             │
│     → ~/basic-memory/sessions/       │
│                                      │
│  2. Write spool file                 │
│     → ~/.claude-mem/spool/           │
└──────────┬──────────┬────────────────┘
           │          │
           ▼          ▼
    Basic Memory    Spool Dir
    indexes it      (queue)
    immediately      │
    (FTS5 +          │
     vectors)        │
           │         ▼
           │  ┌─────────────────────────────────┐
           │  │  Tier 1: Librarian               │
           │  │  (on vader, pulls when idle)     │
           │  │                                   │
           │  │  1. Check Ollama idle             │
           │  │     curl /api/ps → 0 active      │
           │  │                                   │
           │  │  2. Read spool file               │
           │  │                                   │
           │  │  3. Feed to Cognee extraction     │
           │  │     cognee.add(transcript)        │
           │  │     cognee.cognify()              │
           │  │     → entities, relationships     │
           │  │                                   │
           │  │  4. Write results:                │
           │  │     summaries/{session}.md         │
           │  │     entities/{entity}.md (append)  │
           │  │     decisions/{date}.md (append)   │
           │  │     → all to ~/basic-memory/      │
           │  │                                   │
           │  │  5. Write to FalkorDBLite:        │
           │  │     nodes + typed edges            │
           │  │                                   │
           │  │  6. Delete spool file             │
           │  │     (only on full success)         │
           │  │                                   │
           │  │  Fallback: if Cognee fails,       │
           │  │  use direct Ollama prompt          │
           │  └─────────────────────────────────┘
           │         │
           ▼         ▼
┌──────────────────────────────────────┐
│  ~/basic-memory/                     │
│                                      │
│  sessions/                           │
│    {session}_{checkpoint}.md         │
│    (raw transcripts — Tier 0)       │
│                                      │
│  summaries/                          │
│    {session}_{checkpoint}.md         │
│    (Cognee-extracted — Tier 1)      │
│                                      │
│  entities/                           │
│    vader.md                          │
│    deal-finder.md                    │
│    docker-lxc.md                     │
│    (living docs, appended — Tier 1) │
│                                      │
│  decisions/                          │
│    2026-04-13.md                     │
│    (why choices were made — Tier 1) │
│                                      │
│  connections/                        │
│    (cross-references — Tier 2)      │
│                                      │
│  overviews/                          │
│    mlx-cluster-architecture.md       │
│    (synthesized — Tier 3)           │
│                                      │
│  All files have [[wiki-links]]      │
│  All indexed by BM automatically    │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│  FalkorDBLite                        │
│  (embedded, same process as tiers)  │
│                                      │
│  ~/.claude-mem/graph.db              │
│                                      │
│  Nodes:                              │
│    vader [type:machine, ip:...]     │
│    deal-finder [type:service, ...]  │
│                                      │
│  Edges:                              │
│    deal-finder ─[deployed_on]─▶      │
│      docker-lxc {port:8200}         │
│    spool-processor ─[depends_on]─▶  │
│      vader/ollama {critical:true}   │
│                                      │
│  Written by: Tier 1, 2, 3          │
│  Maintained by: Tier 4             │
└──────────────────────────────────────┘


## Tier Schedule

┌─────────────────────────────────────────────────────┐
│  All tiers run on vader                             │
│  (LAN to Ollama, always on, 256GB RAM)             │
│                                                     │
│  Tier 1: Librarian                                  │
│    Trigger: spool file exists + Ollama idle         │
│    Daemon: watches spool dir, checks /api/ps        │
│    Software: Cognee (extraction) + Python (writes)  │
│    Writes: summaries, entities, decisions, graph    │
│                                                     │
│  Tier 2: Connector                                  │
│    Trigger: hourly or on idle                       │
│    Software: Python script reading BM SQLite        │
│    Writes: [[wiki-links]] added to existing notes   │
│    Writes: new edges to FalkorDBLite                │
│    No LLM needed                                    │
│                                                     │
│  Tier 3: Synthesizer                                │
│    Trigger: daily or on idle                        │
│    Software: Python + Ollama                        │
│    Writes: overview/architecture notes              │
│    Writes: visual graph export (HTML)               │
│                                                     │
│  Tier 4: Gardener                                   │
│    Trigger: weekly                                  │
│    Software: Python + Ollama (for conflict resolve) │
│    Writes: updates stale facts, merges duplicates   │
│    Writes: prunes dead links, archives old notes    │
│    Writes: maintains FalkorDBLite consistency       │
└─────────────────────────────────────────────────────┘


## Retrieval

```
Claude Code MCP query
       │
       ▼
┌──────────────────────────────────────┐
│  Retrieval MCP Server                │
│  (thin wrapper, runs locally)       │
│                                      │
│  Routes query to:                    │
│  ├── Basic Memory (text + vectors)  │
│  └── FalkorDBLite (relationships)   │
│                                      │
│  Merges results                      │
│  Returns combined response           │
│                                      │
│  If FalkorDBLite down:              │
│    returns BM results only           │
│  If BM down:                         │
│    returns graph results only        │
│  If both down:                       │
│    returns "no memory available"     │
└──────────────────────────────────────┘
```


## Sync (multi-machine)

```
WSL / skynet / macbook / any machine
       │
       │ Hook writes raw transcript locally
       │ to ~/basic-memory/sessions/
       │
       ▼
   Syncthing ◄──────────────────► vader
   (bidirectional file sync)     ~/basic-memory/
                                      │
                                 Tiers process,
                                 write enriched
                                 files back
                                      │
                                      ▼
                                 Syncthing pushes
                                 enriched files
                                 to all machines
```


## Failure Modes

| What fails | Impact | Recovery |
|-----------|--------|----------|
| Ollama down | Spool files queue up, raw transcripts still searchable (85%) | Tiers process backlog when Ollama returns |
| Cognee fails | Librarian falls back to direct Ollama prompt | Slightly lower extraction quality |
| FalkorDBLite corrupt | Relationship queries fail, BM text search still works | Rebuild from entity notes (markdown is source of truth) |
| Vader offline | No enrichment, spool queues on local machines | Processes when vader returns |
| BM index corrupt | Reindex from markdown files on disk | `basic-memory reindex` |
| Syncthing down | Machines work independently, sync when reconnected | No data loss, just delay |
| Everything down | Raw transcripts on local disk, not indexed | Manual search if desperate |


## What Exists vs What's New

| Component | Status |
|-----------|--------|
| Basic Memory | DONE — Installed (v0.20.3), project "main" at ~/basic-memory/, MCP in settings.json |
| context-autosave.js | DONE — writes raw transcript to ~/basic-memory/sessions/ + spool file |
| Spool directory | Exists, unchanged |
| Spool processor | Exists, needs rewrite (Chroma → markdown + FalkorDBLite) |
| Circuit breaker | Exists, unchanged |
| LLM config | Exists, unchanged |
| Ollama on vader | Exists, unchanged |
| FalkorDBLite | VERIFIED — pip install works, embedded Cypher queries tested |
| Cognee | VERIFIED — works with Ollama after adapter patch, needs transformers pip dep |
| Tier 2 (Connector) | New Python script |
| Tier 3 (Synthesizer) | New Python script |
| Tier 4 (Gardener) | New Python script |
| Retrieval MCP server | New, thin wrapper |
| Syncthing | Deferred — start with remote-only (server-side BM) |
| FalkorDB Browser | Optional, for visual exploration |

## Cognee + Ollama Integration (VERIFIED 2026-04-13)

Cognee 1.0.0 works with local Ollama after one code patch and proper config.

### Adapter patch
File: `cognee/.../ollama/adapter.py` — append `/v1` to endpoint, strip `ollama/` from model name.

### Config
```python
cognee.config.set_llm_config({
    'llm_provider': 'ollama',
    'llm_model': 'ollama/qwen2.5:32b',  # or qwen2.5-coder:7b — both work
    'llm_endpoint': 'http://192.168.0.126:11434',
    'llm_api_key': 'ollama',
})
cognee.config.set_embedding_config({
    'embedding_provider': 'ollama',
    'embedding_model': 'nomic-embed-text',
    'embedding_endpoint': 'http://192.168.0.126:11434/api/embed',
    'embedding_api_key': 'ollama',
    'embedding_dimensions': 768,
    'huggingface_tokenizer': 'nomic-ai/nomic-embed-text-v1',
})
os.environ['COGNEE_SKIP_CONNECTION_TEST'] = 'true'
os.environ['OLLAMA_API_BASE'] = 'http://192.168.0.126:11434'
```

### Test results
- "What port does Deal Finder run on?" → `Deal Finder runs on port 8200`
- "What depends on Ollama?" → `spool processor`
- "What IP is Vader?" → `192.168.0.126`

Full patch: `~/memory-benchmark/cognee-ollama-patch.py` and `cognee-patch-notes.md`
