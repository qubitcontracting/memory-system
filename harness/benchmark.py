#!/usr/bin/env python3
"""
Memory System Benchmark Harness

Usage:
    python3 benchmark.py ingest <system>    # Ingest corpus into a system
    python3 benchmark.py evaluate <system>  # Run 20 questions against a system
    python3 benchmark.py evaluate-all       # Run all Round 1 systems
    python3 benchmark.py report             # Print comparison table
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Add harness dir to path
sys.path.insert(0, str(Path(__file__).parent))
import config
from score import score_response

QUESTIONS = json.loads((config.HARNESS_DIR / "questions.json").read_text())
MANIFEST = None


def load_manifest():
    global MANIFEST
    if MANIFEST is None:
        MANIFEST = json.loads((config.CORPUS_DIR / "sessions.json").read_text())
    return MANIFEST


# ── Ollama helper ──────────────────────────────────────────────────────────

def summarize_via_ollama(transcript):
    """Call Ollama for summarization (reused from spool-processor)."""
    system_prompt = (
        "You are a log analyzer. You extract facts from conversation logs and produce "
        "structured reports. You write in past tense. You never generate dialogue."
    )
    prompt = (
        "Extract facts from this conversation log. Do not fabricate any details.\n\n"
        "<CONVERSATION_LOG>\n" + transcript + "\n</CONVERSATION_LOG>\n\n"
        "Write a report:\n## Title\n## Done\n## Files Changed\n## Problems Solved\n"
        "## Key Facts (IPs, ports, passwords, configs mentioned)\n"
        "## Discussed but Not Done\nTags: (keywords)"
    )
    body = json.dumps({
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "system": system_prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 1500},
    }).encode()
    req = urllib.request.Request(
        config.OLLAMA_URL,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=config.OLLAMA_TIMEOUT) as resp:
        data = json.loads(resp.read())
        response = data.get("response", "")
        response = re.sub(r"<think>[\s\S]*?</think>", "", response).strip()
        return response


# ── INGEST ADAPTERS ─────────���──────────────────────────────────────────────

def ingest_claude_mem_fixed():
    """Ingest corpus into sandboxed Chroma with per-chunk doc IDs."""
    import chromadb

    manifest = load_manifest()
    chroma_path = config.CLAUDE_MEM_FIXED_CHROMA
    os.makedirs(chroma_path, exist_ok=True)

    client = chromadb.PersistentClient(path=chroma_path)
    # Delete existing benchmark collection if present
    try:
        client.delete_collection(config.CLAUDE_MEM_FIXED_COLLECTION)
    except Exception:
        pass
    col = client.create_collection(config.CLAUDE_MEM_FIXED_COLLECTION)

    print(f"Ingesting {len(manifest)} corpus files via Ollama summarization...")
    ingested = 0
    failed = 0

    for entry in manifest:
        doc_id = entry["id"]
        text_path = config.TEXT_DIR / f"{doc_id}.txt"
        if not text_path.exists():
            continue

        transcript = text_path.read_text()
        if not transcript.strip():
            continue

        # Truncate to 60KB (matches spool processor behavior)
        if len(transcript) > 60 * 1024:
            transcript = transcript[-(60 * 1024):]

        print(f"  [{ingested+1}/{len(manifest)}] {doc_id[:30]}...", end=" ", flush=True)
        try:
            t0 = time.time()
            summary = summarize_via_ollama(transcript)
            elapsed = time.time() - t0
            if not summary.strip():
                print(f"empty summary ({elapsed:.1f}s)")
                failed += 1
                continue

            col.upsert(
                ids=[f"bench_{doc_id}"],
                documents=[summary],
                metadatas=[{
                    "source_id": doc_id,
                    "session_id": entry["session_id"],
                    "project": entry["project"],
                    "type": "benchmark",
                }],
            )
            ingested += 1
            print(f"OK ({elapsed:.1f}s, {len(summary)} chars)")
        except Exception as e:
            print(f"ERROR: {e}")
            failed += 1

    print(f"\nIngested: {ingested}, Failed: {failed}, Collection count: {col.count()}")
    return ingested


def ingest_basic_memory():
    """Ingest corpus into Basic Memory as markdown notes."""
    manifest = load_manifest()
    bm_dir = config.BASIC_MEMORY_SANDBOX
    notes_dir = bm_dir / "sessions"
    notes_dir.mkdir(parents=True, exist_ok=True)

    print(f"Writing {len(manifest)} markdown notes for Basic Memory...")
    written = 0

    for entry in manifest:
        doc_id = entry["id"]
        text_path = config.TEXT_DIR / f"{doc_id}.txt"
        if not text_path.exists():
            continue

        transcript = text_path.read_text()
        if not transcript.strip():
            continue

        # Write as markdown with frontmatter
        session_id = entry["session_id"]
        project = entry["project"]
        chunk_info = f" (chunk {entry['chunk']})" if entry.get("chunk") is not None else ""

        md_content = f"""---
title: Session {session_id[:12]}{chunk_info}
permalink: sessions/{doc_id}
tags: session, {project}, benchmark
---

# Session {session_id[:12]}{chunk_info}

- **Project**: {project}
- **Messages**: {entry['messages']}
- **User messages**: {entry['user_messages']}

## Transcript

{transcript}
"""
        md_path = notes_dir / f"{doc_id}.md"
        md_path.write_text(md_content)
        written += 1

    print(f"Written: {written} markdown files to {notes_dir}")

    # Now initialize and sync Basic Memory
    print("Initializing Basic Memory project...")
    result = subprocess.run(
        ["basic-memory", "project", "add", "benchmark", str(bm_dir)],
        capture_output=True, text=True
    )
    print(f"  project add: {result.stdout.strip() or result.stderr.strip()}")

    print("Syncing (building index)...")
    result = subprocess.run(
        ["basic-memory", "sync", "--project", "benchmark"],
        capture_output=True, text=True, timeout=300
    )
    print(f"  sync: {result.stdout.strip()[:200] or result.stderr.strip()[:200]}")

    return written


def ingest_mempalace():
    """Ingest corpus into MemPalace sandbox."""
    manifest = load_manifest()
    mp_dir = config.MEMPALACE_SANDBOX
    staging_dir = mp_dir / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    print(f"Writing {len(manifest)} text files for MemPalace staging...")
    written = 0

    for entry in manifest:
        doc_id = entry["id"]
        text_path = config.TEXT_DIR / f"{doc_id}.txt"
        if not text_path.exists():
            continue

        transcript = text_path.read_text()
        if not transcript.strip():
            continue

        out_path = staging_dir / f"{doc_id}.txt"
        out_path.write_text(transcript)
        written += 1

    print(f"Written: {written} staging files")

    # Try to run mempalace mine with sandbox palace
    # MemPalace needs to be invoked via its venv
    mp_venv_python = Path.home() / ".mempalace" / "venv" / "bin" / "python"
    if not mp_venv_python.exists():
        print("ERROR: MemPalace venv not found")
        return 0

    print("Running MemPalace ingestion...")
    try:
        result = subprocess.run(
            [str(mp_venv_python), "-m", "mempalace.cli",
             "--palace", str(mp_dir / "palace"),
             "mine", str(staging_dir), "--mode", "convos", "--wing", "benchmark"],
            capture_output=True, text=True, timeout=600,
            env={**os.environ, "MEMPALACE_PALACE": str(mp_dir / "palace")}
        )
        print(f"  mine: {result.stdout.strip()[:300]}")
        if result.stderr:
            print(f"  stderr: {result.stderr.strip()[:300]}")
    except subprocess.TimeoutExpired:
        print("  Timed out after 600s")
    except Exception as e:
        print(f"  Error: {e}")

    return written


# ── QUERY ADAPTERS ─────────────────────────���───────────────────────────────

def query_claude_mem_old(question_text, n_results=5):
    """Query production Chroma (existing 564 docs)."""
    import chromadb

    client = chromadb.PersistentClient(path=config.PROD_CHROMA_PATH)
    col = client.get_collection(config.PROD_COLLECTION)
    results = col.query(query_texts=[question_text], n_results=n_results)
    docs = results.get("documents", [[]])[0]
    return "\n\n---\n\n".join(docs)


def query_claude_mem_fixed(question_text, n_results=5):
    """Query sandboxed Chroma with fixed doc IDs."""
    import chromadb

    client = chromadb.PersistentClient(path=config.CLAUDE_MEM_FIXED_CHROMA)
    try:
        col = client.get_collection(config.CLAUDE_MEM_FIXED_COLLECTION)
    except Exception:
        return ""
    results = col.query(query_texts=[question_text], n_results=n_results)
    docs = results.get("documents", [[]])[0]
    return "\n\n---\n\n".join(docs)


def query_basic_memory(question_text, n_results=5):
    """Query Basic Memory via CLI search-notes tool (benchmark sandbox)."""
    try:
        result = subprocess.run(
            ["basic-memory", "tool", "search-notes", question_text,
             "--project", "benchmark"],
            capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip()
    except Exception as e:
        return f"Error: {e}"


def query_basic_memory_main(question_text, n_results=5):
    """Query Basic Memory main project (production)."""
    try:
        result = subprocess.run(
            ["basic-memory", "tool", "search-notes", question_text,
             "--project", "main"],
            capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip()
    except Exception as e:
        return f"Error: {e}"


def query_mempalace(question_text, n_results=5):
    """Query MemPalace via its venv CLI."""
    mp_venv_python = Path.home() / ".mempalace" / "venv" / "bin" / "python"
    mp_dir = config.MEMPALACE_SANDBOX
    try:
        result = subprocess.run(
            [str(mp_venv_python), "-m", "mempalace.cli",
             "--palace", str(mp_dir / "palace"),
             "search", question_text, "--results", str(n_results)],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "MEMPALACE_PALACE": str(mp_dir / "palace")}
        )
        return result.stdout.strip()
    except Exception as e:
        return f"Error: {e}"


def query_knowledge_graph(question_text, n_results=5):
    """Query knowledge-graph via tsx CLI, then read top matching files."""
    vault_path = os.path.expanduser("~/memory-benchmark/sandboxes/knowledge-graph/vault")
    try:
        result = subprocess.run(
            ["npx", "tsx", "src/cli/index.ts",
             "--vault-path", vault_path,
             "--data-dir", os.path.expanduser("~/memory-benchmark/sandboxes/knowledge-graph/data"),
             "search", question_text, "--limit", str(n_results)],
            capture_output=True, text=True, timeout=30,
            cwd="/tmp/knowledge-graph-repo"
        )
        # Parse JSON results and read actual file content for top matches
        search_output = result.stdout.strip()
        try:
            results = json.loads(search_output)
            texts = []
            for r in results[:3]:  # Top 3 results
                node_id = r.get("nodeId", "")
                file_path = os.path.join(vault_path, node_id)
                if os.path.exists(file_path):
                    content = open(file_path).read()
                    # Take first 2000 chars of each file
                    texts.append(content[:2000])
            return "\n\n---\n\n".join(texts) if texts else search_output
        except json.JSONDecodeError:
            return search_output
    except Exception as e:
        return f"Error: {e}"


def query_memsearch(question_text, n_results=5):
    """Query MemSearch via its venv CLI."""
    ms_venv = Path.home() / "memory-benchmark" / "sandboxes" / "memsearch" / "venv" / "bin" / "python"
    ms_dir = Path.home() / "memory-benchmark" / "sandboxes" / "memsearch"
    try:
        result = subprocess.run(
            [str(ms_venv), "-m", "memsearch", "search", question_text],
            capture_output=True, text=True, timeout=30,
            cwd=str(ms_dir)
        )
        return result.stdout.strip()
    except Exception as e:
        return f"Error: {e}"


QUERY_ADAPTERS = {
    "claude-mem-old": query_claude_mem_old,
    "claude-mem-fixed": query_claude_mem_fixed,
    "basic-memory": query_basic_memory,
    "basic-memory-main": query_basic_memory_main,
    "mempalace": query_mempalace,
    "knowledge-graph": query_knowledge_graph,
    "memsearch": query_memsearch,
}

INGEST_ADAPTERS = {
    "claude-mem-fixed": ingest_claude_mem_fixed,
    "basic-memory": ingest_basic_memory,
    "mempalace": ingest_mempalace,
}


# ── EVALUATION ─────────────────────────────────────────────────────────────

def evaluate_system(system_name):
    """Run all 20 questions against a system and score."""
    query_fn = QUERY_ADAPTERS.get(system_name)
    if not query_fn:
        print(f"Unknown system: {system_name}")
        print(f"Available: {list(QUERY_ADAPTERS.keys())}")
        return None

    print(f"\n{'='*60}")
    print(f"Evaluating: {system_name}")
    print(f"{'='*60}\n")

    results = []
    for q in QUESTIONS:
        qid = q["id"]
        question = q["question"]
        category = q["category"]

        print(f"  Q{qid:2d} [{category:10s}] {question[:60]}...", end=" ", flush=True)

        t0 = time.time()
        try:
            response = query_fn(question)
        except Exception as e:
            response = f"ERROR: {e}"
        latency_ms = (time.time() - t0) * 1000

        pts, explanation = score_response(q, response)
        print(f"{'**' if pts==2 else '* ' if pts==1 else '  '} {pts}/2 ({latency_ms:.0f}ms)")

        results.append({
            "question_id": qid,
            "question": question,
            "category": category,
            "ground_truth": q["ground_truth"],
            "response": response[:2000],  # Truncate for storage
            "score": pts,
            "max_score": 2,
            "explanation": explanation,
            "latency_ms": round(latency_ms, 1),
        })

    # Summary
    total = sum(r["score"] for r in results)
    max_total = sum(r["max_score"] for r in results)
    by_cat = {}
    for r in results:
        cat = r["category"]
        if cat not in by_cat:
            by_cat[cat] = {"score": 0, "max": 0, "count": 0}
        by_cat[cat]["score"] += r["score"]
        by_cat[cat]["max"] += r["max_score"]
        by_cat[cat]["count"] += 1

    avg_latency = sum(r["latency_ms"] for r in results) / len(results)

    print(f"\n  TOTAL: {total}/{max_total} ({total/max_total*100:.0f}%)")
    for cat, stats in sorted(by_cat.items()):
        print(f"    {cat:12s}: {stats['score']}/{stats['max']} ({stats['score']/stats['max']*100:.0f}%)")
    print(f"  Avg latency: {avg_latency:.0f}ms")

    return {
        "system": system_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_score": total,
        "max_score": max_total,
        "pct": round(total / max_total * 100, 1),
        "by_category": by_cat,
        "avg_latency_ms": round(avg_latency, 1),
        "results": results,
    }


def save_results(eval_result):
    """Save evaluation results to a timestamped run directory."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    system = eval_result["system"]
    run_dir = config.RESULTS_DIR / f"run-{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)

    out_path = run_dir / f"{system}.json"
    out_path.write_text(json.dumps(eval_result, indent=2))
    print(f"\nResults saved to {out_path}")
    return run_dir


def print_report():
    """Print comparison table from latest results for each system."""
    # Find latest result file per system
    latest = {}
    for run_dir in sorted(config.RESULTS_DIR.iterdir()):
        if not run_dir.is_dir():
            continue
        for result_file in run_dir.glob("*.json"):
            data = json.loads(result_file.read_text())
            system = data["system"]
            latest[system] = data

    if not latest:
        print("No results found. Run 'benchmark.py evaluate <system>' first.")
        return

    print(f"\n{'='*80}")
    print(f"  MEMORY SYSTEM BENCHMARK RESULTS")
    print(f"{'='*80}")
    print(f"\n{'System':<20} {'Total':>8} {'Factual':>10} {'Procedural':>12} {'Synthesis':>11} {'Latency':>10}")
    print(f"{'-'*20} {'-'*8} {'-'*10} {'-'*12} {'-'*11} {'-'*10}")

    for system in sorted(latest.keys()):
        data = latest[system]
        cats = data.get("by_category", {})
        factual = cats.get("factual", {})
        procedural = cats.get("procedural", {})
        synthesis = cats.get("synthesis", {})

        f_str = f"{factual.get('score',0)}/{factual.get('max',0)}" if factual else "n/a"
        p_str = f"{procedural.get('score',0)}/{procedural.get('max',0)}" if procedural else "n/a"
        s_str = f"{synthesis.get('score',0)}/{synthesis.get('max',0)}" if synthesis else "n/a"

        print(f"{system:<20} {data['total_score']:>3}/{data['max_score']:<3} "
              f"{f_str:>10} {p_str:>12} {s_str:>11} {data['avg_latency_ms']:>7.0f}ms")

    print(f"\nScoring: 2=correct, 1=partial, 0=wrong. Max 40 points.")
    print(f"Decision: 75%+ viable, 50-74% needs work, <50% not suitable.\n")


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "ingest":
        if len(sys.argv) < 3:
            print(f"Usage: benchmark.py ingest <system>")
            print(f"Systems: {list(INGEST_ADAPTERS.keys())}")
            return
        system = sys.argv[2]
        if system not in INGEST_ADAPTERS:
            print(f"Unknown system: {system}")
            print(f"Available: {list(INGEST_ADAPTERS.keys())}")
            return
        INGEST_ADAPTERS[system]()

    elif cmd == "evaluate":
        if len(sys.argv) < 3:
            print(f"Usage: benchmark.py evaluate <system>")
            print(f"Systems: {list(QUERY_ADAPTERS.keys())}")
            return
        system = sys.argv[2]
        result = evaluate_system(system)
        if result:
            save_results(result)

    elif cmd == "evaluate-all":
        for system in QUERY_ADAPTERS:
            result = evaluate_system(system)
            if result:
                save_results(result)

    elif cmd == "report":
        print_report()

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
