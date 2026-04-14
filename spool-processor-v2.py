#!/usr/bin/env python3
"""
Spool Processor (v2 — Librarian)

Single-threaded processor that reads spool files, extracts structured
knowledge via Ollama, writes enriched markdown to Basic Memory, stores
legacy summary in Chroma, syncs to remote, and cleans up.

Designed to run via cron every 10 minutes. Only one instance runs at a time
(lockfile prevents overlap).

Spool files are JSON with: session_id, checkpoint, cwd, hostname, timestamp, transcript

Output:
  ~/basic-memory/summaries/{session}_{checkpoint}.md — session summary with [[wiki-links]]
  ~/basic-memory/entities/{entity}.md — appended facts per entity
  ~/basic-memory/decisions/{date}.md — appended decisions with reasoning
  ~/.claude-mem/chroma — legacy Chroma upsert (transition period)
"""

import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
CLAUDE_MEM = HOME / ".claude-mem"
SPOOL_DIR = CLAUDE_MEM / "spool"
CHROMA_PATH = str(CLAUDE_MEM / "chroma")
COLLECTION = "claude_memories"

# Basic Memory output dirs
BM_DIR = HOME / "basic-memory"
BM_SUMMARIES = BM_DIR / "summaries"
BM_ENTITIES = BM_DIR / "entities"
BM_DECISIONS = BM_DIR / "decisions"

LOG_FILE = CLAUDE_MEM / "spool-processor-log.txt"
LOCK_FILE = CLAUDE_MEM / "spool-processor.lock"

# LLM config — reads from central config, falls back to defaults
try:
    import json as _json
    _llm_cfg = _json.load(open(CLAUDE_MEM / "llm-config.json"))
    OLLAMA_URL = _llm_cfg["ollama"]["url"] + "/api/generate"
    OLLAMA_MODEL = _llm_cfg["ollama"]["models"]["summarizer"]
except Exception:
    OLLAMA_URL = "http://192.168.0.126:11434/api/generate"
    OLLAMA_MODEL = "qwen2.5-coder:7b"
OLLAMA_TIMEOUT = 90

SYNC_SCRIPT = CLAUDE_MEM / "sync-to-remote.py"

HOSTNAME = socket.gethostname()

# Circuit breaker: stop trying Ollama after N consecutive failures
MAX_CONSECUTIVE_FAILURES = 3
FAILURE_STATE_FILE = CLAUDE_MEM / "spool-processor-failures.json"


def log(msg):
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(LOG_FILE, "a") as f:
            f.write(f"{ts} [spool] {msg}\n")
    except Exception:
        pass


def read_failure_state():
    try:
        return json.loads(FAILURE_STATE_FILE.read_text())
    except Exception:
        return {"consecutive_failures": 0, "last_failure": None, "backoff_until": None}


def write_failure_state(state):
    FAILURE_STATE_FILE.write_text(json.dumps(state, indent=2))


def check_circuit_breaker():
    """Returns True if we should skip Ollama (circuit is open)."""
    state = read_failure_state()
    failures = state.get("consecutive_failures", 0)

    if failures < MAX_CONSECUTIVE_FAILURES:
        return False

    backoff_until = state.get("backoff_until")
    if backoff_until:
        if datetime.now(timezone.utc).isoformat() < backoff_until:
            return True

    return False


def record_failure():
    state = read_failure_state()
    state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    state["last_failure"] = datetime.now(timezone.utc).isoformat()

    n = state["consecutive_failures"] - MAX_CONSECUTIVE_FAILURES
    backoff_min = min(10 * (2 ** max(0, n)), 60)
    backoff_time = datetime.now(timezone.utc).timestamp() + (backoff_min * 60)
    state["backoff_until"] = datetime.fromtimestamp(
        backoff_time, tz=timezone.utc
    ).isoformat()

    write_failure_state(state)
    log(f"Ollama failure #{state['consecutive_failures']}, backoff {backoff_min} min")


def record_success():
    write_failure_state(
        {"consecutive_failures": 0, "last_failure": None, "backoff_until": None}
    )


# ── Ollama extraction ─────────────────────────────────────────────────────

def extract_via_ollama(transcript):
    """Call Ollama for structured extraction (Librarian prompt)."""
    system_prompt = (
        "You are a structured data extractor. You extract facts from conversation "
        "logs and produce structured reports. You write in past tense. You never "
        "generate dialogue, questions, or proposals. You omit details you are "
        "unsure about rather than guessing. Output ONLY the requested format."
    )

    prompt = (
        "Extract structured information from this conversation transcript. "
        "Do not fabricate any details.\n\n"
        "<CONVERSATION_LOG>\n"
        + transcript
        + "\n</CONVERSATION_LOG>\n\n"
        "Output EXACTLY this format:\n\n"
        "## Summary\n"
        "One paragraph: what this session was about and what was accomplished.\n\n"
        "## Facts\n"
        "- [entity_name] key: value\n"
        "(List every specific fact mentioned: IP addresses, port numbers, "
        "passwords, model names, file paths, versions, cron schedules, "
        "RAM sizes, machine names. Be exhaustive.)\n\n"
        "## Entities\n"
        "- entity_name | type | one-line description\n"
        "(Types: machine, service, project, tool, config, person)\n\n"
        "## Relationships\n"
        "- entity_a -> relationship -> entity_b\n"
        "(e.g., deal-finder -> deployed_on -> docker-lxc)\n\n"
        "## Decisions\n"
        "- decision made | reason why\n"
        "(Only if explicit choices were made. Write 'None' if no decisions.)\n\n"
        "## Problems Solved\n"
        "- problem description | how it was fixed\n"
        "(Only if errors were encountered and resolved. Write 'None' if no problems.)\n"
    )

    body = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 2000},
        }
    ).encode()

    req = urllib.request.Request(
        OLLAMA_URL,
        data=body,
        headers={"Content-Type": "application/json"},
    )

    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
        data = json.loads(resp.read())
        response = data.get("response", "")
        response = re.sub(r"<think>[\s\S]*?</think>", "", response).strip()
        return response


# ── Parse extraction output ───────────────────────────────────────────────

def parse_section(text, section_name):
    """Extract content between ## section_name and the next ## heading."""
    pattern = rf"## {re.escape(section_name)}\s*\n(.*?)(?=\n## |\Z)"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


def parse_facts(facts_text):
    """Parse facts into list of (entity, key, value) tuples."""
    facts = []
    for line in facts_text.split("\n"):
        line = line.strip()
        if not line.startswith("-"):
            continue
        line = line[1:].strip()
        # Format 1: [entity_name] key: value
        m = re.match(r"\[([^\]]+)\]\s*(.+?):\s*(.+)", line)
        if m:
            facts.append((m.group(1).strip(), m.group(2).strip(), m.group(3).strip()))
            continue
        # Format 2: "Entity Name key: value" or "Entity Name: description"
        if ": " in line:
            key_part, _, value = line.partition(": ")
            key_part = key_part.strip()
            value = value.strip()
            if not value or not key_part:
                continue
            words = key_part.split()
            if len(words) >= 2:
                # Check if last word looks like a key (port, IP, model, ram, etc.)
                last_word = words[-1].lower()
                key_words = {"port", "ip", "model", "ram", "version", "path",
                             "host", "url", "cron", "schedule", "password",
                             "user", "name", "type", "size", "address"}
                if last_word in key_words:
                    entity = " ".join(words[:-1])
                    facts.append((entity, words[-1], value))
                else:
                    # Whole key_part is the entity name, value is a description
                    facts.append((key_part, "info", value))
            else:
                facts.append((key_part, "info", value))
    return facts


def parse_entities(entities_text):
    """Parse entities into list of (name, type, description) tuples."""
    entities = []
    for line in entities_text.split("\n"):
        line = line.strip()
        if not line.startswith("-"):
            continue
        line = line[1:].strip()
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            entities.append((parts[0], parts[1], parts[2]))
        elif len(parts) == 2:
            entities.append((parts[0], parts[1], ""))
    return entities


def parse_relationships(rel_text):
    """Parse relationships into list of (source, rel, target) tuples."""
    rels = []
    for line in rel_text.split("\n"):
        line = line.strip()
        if not line.startswith("-"):
            continue
        line = line[1:].strip()
        parts = [p.strip() for p in line.split("->")]
        if len(parts) == 3:
            rels.append((parts[0], parts[1], parts[2]))
    return rels


def parse_decisions(dec_text):
    """Parse decisions into list of (decision, reason) tuples."""
    decisions = []
    for line in dec_text.split("\n"):
        line = line.strip()
        if not line.startswith("-"):
            continue
        line = line[1:].strip()
        if line.lower() == "none":
            continue
        parts = [p.strip() for p in line.split("|", 1)]
        if len(parts) == 2:
            decisions.append((parts[0], parts[1]))
        else:
            decisions.append((line, ""))
    return decisions


# ── Markdown writers ──────────────────────────────────────────────────────

def slugify(name):
    """Convert entity name to a safe filename slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug or "unknown"


def write_summary(session_id, checkpoint, cwd, date_str, extraction, entities, relationships):
    """Write session summary as markdown with [[wiki-links]]."""
    BM_SUMMARIES.mkdir(parents=True, exist_ok=True)

    summary_text = parse_section(extraction, "Summary")
    problems_text = parse_section(extraction, "Problems Solved")

    # Build wiki-links from entities
    entity_links = ", ".join(f"[[{slugify(e[0])}]]" for e in entities[:10])
    project_name = cwd.replace("/", "-").strip("-") if cwd else "unknown"

    md = f"""---
title: Session {session_id[:12]} ({checkpoint}%)
permalink: summaries/{session_id}_{checkpoint}
tags: summary, {project_name}
date: {date_str}
---

# Session {session_id[:12]} — {checkpoint}% checkpoint

- **Date**: {date_str}
- **Project**: {project_name}
- **Related entities**: {entity_links}

## Summary

{summary_text}

## Problems Solved

{problems_text if problems_text and problems_text.lower() != 'none' else 'No problems encountered.'}

## Relationships

"""
    for src, rel, tgt in relationships:
        md += f"- [[{slugify(src)}]] {rel} [[{slugify(tgt)}]]\n"

    md_path = BM_SUMMARIES / f"{session_id}_{checkpoint}.md"
    tmp_path = md_path.with_suffix(".md.tmp")
    tmp_path.write_text(md)
    tmp_path.rename(md_path)
    return md_path


def write_entity_note(entity_name, entity_type, description, facts, relationships, date_str):
    """Create or append to an entity note."""
    BM_ENTITIES.mkdir(parents=True, exist_ok=True)

    slug = slugify(entity_name)
    md_path = BM_ENTITIES / f"{slug}.md"

    if md_path.exists():
        existing = md_path.read_text()

        # Append new facts, skipping duplicates
        new_lines = []
        for _, key, value in facts:
            fact_line = f"- [fact] {key}: {value}"
            if fact_line not in existing:
                new_lines.append(fact_line)

        # Append new relationships
        for src, rel, tgt in relationships:
            rel_line = f"- [[{slugify(src)}]] {rel} [[{slugify(tgt)}]]"
            if rel_line not in existing:
                new_lines.append(rel_line)

        if new_lines:
            # Add date marker and new content
            addition = f"\n### Updated {date_str}\n" + "\n".join(new_lines) + "\n"
            existing = existing.rstrip() + "\n" + addition
            md_path.write_text(existing)
            return md_path, len(new_lines)
        return md_path, 0
    else:
        # Create new entity note
        # Build relationship links
        rel_links = []
        for src, rel, tgt in relationships:
            other = tgt if slugify(src) == slug else src
            rel_links.append(f"- {rel} [[{slugify(other)}]]")

        md = f"""---
title: {entity_name}
permalink: entities/{slug}
tags: entity, {entity_type}
---

# {entity_name}

{description}

## Details
- **Type**: {entity_type}

## Facts
"""
        for _, key, value in facts:
            md += f"- [fact] {key}: {value}\n"

        if rel_links:
            md += "\n## Relationships\n"
            md += "\n".join(rel_links) + "\n"

        md_path.write_text(md)
        return md_path, len(facts)


def write_decisions(date_str, decisions, session_id):
    """Append decisions to the daily decisions log."""
    if not decisions:
        return None

    BM_DECISIONS.mkdir(parents=True, exist_ok=True)
    md_path = BM_DECISIONS / f"{date_str}.md"

    if md_path.exists():
        existing = md_path.read_text()
    else:
        existing = f"""---
title: Decisions — {date_str}
permalink: decisions/{date_str}
tags: decisions, log
date: {date_str}
---

# Decisions — {date_str}

"""

    # Append new decisions
    addition = f"\n### Session {session_id[:12]}\n"
    for decision, reason in decisions:
        reason_part = f" — *{reason}*" if reason else ""
        addition += f"- {decision}{reason_part}\n"

    # Check for duplicates
    if addition.strip() not in existing:
        existing = existing.rstrip() + "\n" + addition
        md_path.write_text(existing)
        return md_path

    return None


# ── Legacy Chroma storage ─────────────────────────────────────────────────

def store_in_chroma(doc_id, document, metadata):
    """Store in local Chroma via Python client (legacy, transition period)."""
    try:
        import chromadb
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        col = client.get_collection(COLLECTION)
        col.upsert(ids=[doc_id], documents=[document], metadatas=[metadata])
        return col.count()
    except Exception as e:
        log(f"Chroma upsert failed (non-fatal): {e}")
        return -1


def sync_to_remote(doc_id):
    """Fire-and-forget sync to remote Chroma."""
    if SYNC_SCRIPT.exists():
        subprocess.Popen(
            ["python3", str(SYNC_SCRIPT), "--id", doc_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


# ── Main processing ───────────────────────────────────────────────────────

def process_spool_file(spool_path):
    """Process a single spool file: Ollama extract -> BM markdown + Chroma -> delete."""
    data = json.loads(spool_path.read_text())

    session_id = data["session_id"]
    checkpoint = data["checkpoint"]
    cwd = data["cwd"]
    hostname = data.get("hostname", HOSTNAME)
    timestamp = data.get("timestamp", datetime.now(timezone.utc).isoformat())
    transcript = data["transcript"]

    if not transcript.strip():
        log(f"Empty transcript in {spool_path.name}, removing")
        spool_path.unlink()
        return True

    log(f"Processing {session_id[:8]}... checkpoint={checkpoint}% ({len(transcript)} chars)")

    # Extract structured information via Ollama
    extraction = extract_via_ollama(transcript)

    if not extraction.strip():
        log(f"Empty extraction for {session_id[:8]}..., removing spool file")
        spool_path.unlink()
        return True

    log(f"Got extraction: {len(extraction)} chars")

    # Parse the structured output
    date_str = timestamp[:10]
    facts = parse_facts(parse_section(extraction, "Facts"))
    entities = parse_entities(parse_section(extraction, "Entities"))
    relationships = parse_relationships(parse_section(extraction, "Relationships"))
    decisions = parse_decisions(parse_section(extraction, "Decisions"))

    log(f"Parsed: {len(facts)} facts, {len(entities)} entities, "
        f"{len(relationships)} rels, {len(decisions)} decisions")

    # ── Write to Basic Memory ──────────────────────────────────────────

    # 1. Session summary
    summary_path = write_summary(
        session_id, checkpoint, cwd, date_str,
        extraction, entities, relationships
    )
    log(f"BM summary: {summary_path.name}")

    # 2. Entity notes
    entities_written = 0
    for ent_name, ent_type, ent_desc in entities:
        # Gather facts for this entity
        ent_facts = [(e, k, v) for e, k, v in facts if slugify(e) == slugify(ent_name)]
        # Gather relationships involving this entity
        ent_rels = [(s, r, t) for s, r, t in relationships
                    if slugify(s) == slugify(ent_name) or slugify(t) == slugify(ent_name)]
        ent_path, count = write_entity_note(
            ent_name, ent_type, ent_desc, ent_facts, ent_rels, date_str
        )
        if count > 0:
            entities_written += 1
            log(f"BM entity: {ent_path.name} (+{count} facts)")

    # 3. Decisions log
    dec_path = write_decisions(date_str, decisions, session_id)
    if dec_path:
        log(f"BM decisions: {dec_path.name}")

    # ── Legacy Chroma storage (transition) ─────────────────────────────

    doc_id = f"autosave_{hostname}_{session_id}_{checkpoint}"
    document = (
        f"## Auto-Save: {checkpoint}% checkpoint\n"
        f"Host: {hostname}\n"
        f"Project: {cwd}\n"
        f"Date: {date_str}\n"
        f"Session: {session_id}\n\n"
        f"{extraction}"
    )
    metadata = {
        "type": "autosave",
        "session_id": session_id,
        "source_host": hostname,
        "project_path": cwd,
        "timestamp": timestamp,
        "date": date_str,
        "checkpoint": str(checkpoint),
        "model": OLLAMA_MODEL,
    }

    count = store_in_chroma(doc_id, document, metadata)
    if count >= 0:
        log(f"Chroma upsert OK, count: {count}")
        sync_to_remote(doc_id)

    # Clean up spool file
    spool_path.unlink()
    log(f"Processed and removed {spool_path.name} "
        f"({entities_written} entities, {len(decisions)} decisions)")

    return True


def get_spool_files():
    """Get spool files, deduped by session (latest wins), sorted oldest first."""
    if not SPOOL_DIR.exists():
        return []

    files = {}
    for f in SPOOL_DIR.glob("*.json"):
        if f.name.endswith(".tmp"):
            continue
        try:
            data = json.loads(f.read_text())
            sid = data.get("session_id", f.stem)
            ts = data.get("timestamp", "")
            if sid not in files or ts > files[sid][1]:
                files[sid] = (f, ts)
        except (json.JSONDecodeError, KeyError):
            log(f"Invalid spool file {f.name}, removing")
            f.unlink()

    sorted_files = sorted(files.values(), key=lambda x: x[1])
    return [f for f, _ in sorted_files]


def main():
    # Ensure dirs exist
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    BM_SUMMARIES.mkdir(parents=True, exist_ok=True)
    BM_ENTITIES.mkdir(parents=True, exist_ok=True)
    BM_DECISIONS.mkdir(parents=True, exist_ok=True)

    # Single instance lock
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Another processor instance is running, exiting")
        return

    try:
        spool_files = get_spool_files()
        if not spool_files:
            return

        log(f"Found {len(spool_files)} spool file(s) to process")

        if check_circuit_breaker():
            state = read_failure_state()
            log(
                f"Circuit breaker open ({state['consecutive_failures']} failures), "
                f"backoff until {state.get('backoff_until', '?')}"
            )
            return

        processed = 0
        for spool_path in spool_files:
            try:
                process_spool_file(spool_path)
                record_success()
                processed += 1
            except Exception as e:
                error_msg = str(e)
                if "timed out" in error_msg.lower() or "urlopen" in error_msg.lower():
                    record_failure()
                    log(f"Ollama timeout for {spool_path.name}: {error_msg}")
                    log("Stopping batch — Ollama appears unavailable")
                    break
                else:
                    log(f"Error processing {spool_path.name}: {error_msg}")

        if processed > 0:
            log(f"Batch complete: {processed} processed")

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
