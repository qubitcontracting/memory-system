# Memory System Benchmark Report
**Date:** 2026-04-12
**Corpus:** 165 text files (3.8MB) extracted from 11 weeks of Claude Code sessions

## Results

| Rank | System | Total | % | Factual | Procedural | Synthesis | Avg Latency | Verdict |
|------|--------|-------|---|---------|------------|-----------|-------------|---------|
| 1 | **Basic Memory** | **34/40** | **85%** | 17/20 (85%) | 9/10 (90%) | 8/10 (80%) | 7,326ms | **VIABLE** |
| 2 | claude-mem (old) | 29/40 | 72% | 13/20 (65%) | 8/10 (80%) | 8/10 (80%) | 692ms | Needs work |
| 3 | MemSearch | 27/40 | 68% | 12/20 (60%) | 8/10 (80%) | 7/10 (70%) | 2,812ms | Needs work |
| 4 | MemPalace | 25/40 | 62% | 13/20 (65%) | 6/10 (60%) | 6/10 (60%) | 4,096ms | Needs work |
| 5 | claude-mem (fixed) | 23/40 | 57% | 12/20 (60%) | 4/10 (40%) | 7/10 (70%) | 234ms | Needs work |
| 6 | Knowledge-Graph | 19/40 | 48% | 10/20 (50%) | 5/10 (50%) | 4/10 (40%) | 4,640ms | Not suitable |

## Key Findings

### Basic Memory is the clear winner
- Only system to cross the 75% viability threshold
- Strongest across ALL categories (factual, procedural, synthesis)
- Stores full markdown text with YAML frontmatter — no compression, no information loss
- Uses FastEmbed (local ONNX) for vector search + SQLite FTS5 for keyword search
- Zero external dependencies (no Ollama, no cloud API, no cron jobs)
- MCP server built in (`basic-memory mcp`)

### Summary-based systems fundamentally limited
- claude-mem (both versions) lose specific facts during Ollama summarization
- 60KB transcripts compressed to ~1KB summaries = 98% information loss
- Port numbers, IP addresses, passwords, cron schedules are first casualties
- More documents doesn't help if the summaries don't contain the answers

### claude-mem old vs fixed
- Old (29/40) beat fixed (23/40) because production has 564 accumulated docs vs 165 fresh summaries
- The doc_id overwrite fix helps future accumulation but doesn't fix the compression problem
- Fixed version's procedural score (40%) is notably poor — summaries miss "how-to" details

### MemSearch — promising but incomplete
- Good procedural and synthesis scores (80%, 70%)
- Weak on factual precision (60%) — hybrid BM25+vector helps but chunks may split key facts
- 30 files failed indexing (context length exceeded nomic-embed-text)
- Fast latency (2.8s) via Ollama embeddings

### Knowledge-Graph — wrong tool for this job
- Only 1 edge indexed (no wiki-links in test data)
- Graph features (paths, communities, PageRank) are useless without link structure
- Would perform better on an Obsidian vault with deliberate [[wiki-links]]

### Maintenance Burden
| System | Cron | LLM dep | External DB | Failure modes |
|--------|------|---------|-------------|---------------|
| Basic Memory | None | None (local FastEmbed) | None (SQLite) | Minimal |
| claude-mem (old) | */10 spool + */15 sync | Ollama (vader) | Chroma (Docker LXC) | Circuit breaker, sync failures |
| MemSearch | Watch mode optional | Ollama (for embeddings) | Milvus-lite (local) | Embedding failures on large files |
| MemPalace | None | None (local ChromaDB embeddings) | ChromaDB (local) | ChromaDB version conflicts |
| Knowledge-Graph | None | None (HF transformers local) | SQLite | Build errors (TypeScript) |

## Basic Memory — Partial Match Analysis (6 questions at 1/2)

| Q# | Question | Why partial? | Fix possible? |
|----|----------|-------------|---------------|
| Q1 | Deal Finder MCP port (8200) | Search returns MCP-related chunks but "8200" is in a different chunk than the top result | Better chunking or cross-reference |
| Q6 | Default MLX model (Qwen3-Coder-Next) | "coder-next" appears in transcripts but search returns general MLX discussion first | Keyword boost in search |
| Q10 | Engineering RAG port (8100) | Similar to Q1 — port number in a different section than the top-ranked chunk | Same fix as Q1 |
| Q15 | PersonaPlex MLX conversion | "moshi" keyword found but full conversion details span multiple chunks | Larger chunk overlap |
| Q16 | MCP services deployed where | Partial — finds some services but not all in one response | Multi-result aggregation |
| Q18 | AliExpress scraping problems | "captcha" and "block" not in top 3 search results | Better keyword matching |

**Root causes:**
1. **Chunk boundary splits** — key facts land in chunks that don't rank highest for the query
2. **Top-K truncation** — search returns top 10 results but scoring only sees what fits in response
3. **Semantic vs keyword mismatch** — "port 8200" is a keyword match, not a semantic one

**Potential improvements:**
- Increase chunk overlap from 2 lines to 10-20 lines
- Use `--vector` flag for hybrid semantic+keyword search
- Return more results (top 10 instead of default)
- Add wiki-links between related sessions to leverage graph traversal

## Recommendation
**Basic Memory** as the primary memory system. It scores highest, has zero external dependencies, stores full text (no lossy compression), and has native MCP integration.

### Next Steps
1. Test `--vector` search mode and tune chunk overlap for better coverage
2. Set up as MCP server in Claude Code settings (replace claude-mem)
3. Build auto-ingestion pipeline (session transcripts → Basic Memory markdown notes)
4. Add wiki-links between related sessions to unlock graph features

## Systems Skipped
- **claude-mem v12.1.0**: Requires cloud API (Anthropic/Gemini/OpenRouter). `CLAUDE_MEM_PROVIDER` defaults to `claude`, no Ollama option.
- **Cognee**: Depends on litellm + openai for knowledge graph extraction. Could potentially use Ollama via litellm but needs investigation.
- **obsidian-second-brain** (eugeniughelbur): Requires Claude API.
