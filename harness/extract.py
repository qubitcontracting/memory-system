#!/usr/bin/env python3
"""
Extract transcripts from Claude Code JSONL session files.
Outputs plain text files for benchmark ingestion.

For large sessions, chunks into segments of ~50 user exchanges.
"""

import json
import os
import sys
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
CORPUS_DIR = Path.home() / "memory-benchmark" / "corpus"
TEXT_DIR = CORPUS_DIR / "text"

# Max exchanges per chunk for large sessions
CHUNK_SIZE = 50
# Min file size to bother with (skip tiny subagent stubs)
MIN_FILE_SIZE = 5000  # 5KB


def extract_messages(jsonl_path):
    """Parse JSONL, return list of (role, text) tuples."""
    messages = []
    with open(jsonl_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type")
            if msg_type not in ("user", "assistant"):
                continue

            msg = obj.get("message")
            if not msg:
                continue

            content = msg.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(
                    b.get("text", "") for b in content if b.get("type") == "text"
                )
            else:
                continue

            text = text.strip()
            if not text:
                continue
            # Skip system reminders and tool response headers that are just noise
            if text.startswith("# Tool Response") and len(text) < 200:
                continue
            if text.startswith("# SESSION ENDING"):
                continue

            role = "USER" if msg_type == "user" else "ASSISTANT"
            messages.append((role, text))

    return messages


def chunk_messages(messages, chunk_size=CHUNK_SIZE):
    """Split messages into chunks at user message boundaries."""
    chunks = []
    current = []
    user_count = 0

    for role, text in messages:
        if role == "USER":
            user_count += 1
            if user_count > chunk_size and current:
                chunks.append(current)
                current = []
                user_count = 1
        current.append((role, text))

    if current:
        chunks.append(current)

    return chunks


def format_transcript(messages):
    """Convert (role, text) pairs to plain text."""
    lines = []
    for role, text in messages:
        lines.append(f"[{role}]: {text}\n")
    return "\n".join(lines)


def select_sessions():
    """Find all substantive sessions across all projects."""
    sessions = []

    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue

        for jsonl_path in proj_dir.glob("*.jsonl"):
            size = jsonl_path.stat().st_size
            if size < MIN_FILE_SIZE:
                continue

            sid = jsonl_path.stem
            sessions.append({
                "id": sid,
                "project": proj_dir.name,
                "path": str(jsonl_path),
                "size_bytes": size,
            })

    sessions.sort(key=lambda x: -x["size_bytes"])
    return sessions


def main():
    TEXT_DIR.mkdir(parents=True, exist_ok=True)

    sessions = select_sessions()
    print(f"Found {len(sessions)} sessions with files > {MIN_FILE_SIZE} bytes")

    manifest = []
    total_files = 0

    for sess in sessions:
        sid = sess["id"]
        jsonl_path = Path(sess["path"])
        size_mb = sess["size_bytes"] / 1048576

        print(f"\nProcessing {sid[:12]}... ({size_mb:.1f}MB)")

        messages = extract_messages(jsonl_path)
        if not messages:
            print(f"  No messages extracted, skipping")
            continue

        user_msgs = sum(1 for r, _ in messages if r == "USER")
        print(f"  {len(messages)} messages ({user_msgs} user)")

        # Chunk large sessions
        if user_msgs > CHUNK_SIZE * 1.5:
            chunks = chunk_messages(messages)
            print(f"  Chunking into {len(chunks)} segments")
            for i, chunk in enumerate(chunks):
                text = format_transcript(chunk)
                chunk_id = f"{sid}_chunk{i:03d}"
                out_path = TEXT_DIR / f"{chunk_id}.txt"
                out_path.write_text(text)
                total_files += 1
                manifest.append({
                    "id": chunk_id,
                    "session_id": sid,
                    "project": sess["project"],
                    "chunk": i,
                    "total_chunks": len(chunks),
                    "messages": len(chunk),
                    "user_messages": sum(1 for r, _ in chunk if r == "USER"),
                    "size_bytes": len(text),
                })
        else:
            text = format_transcript(messages)
            out_path = TEXT_DIR / f"{sid}.txt"
            out_path.write_text(text)
            total_files += 1
            manifest.append({
                "id": sid,
                "session_id": sid,
                "project": sess["project"],
                "chunk": None,
                "total_chunks": 1,
                "messages": len(messages),
                "user_messages": user_msgs,
                "size_bytes": len(text),
            })

    # Write manifest
    manifest_path = CORPUS_DIR / "sessions.json"
    json.dump(manifest, open(manifest_path, "w"), indent=2)

    print(f"\n{'='*60}")
    print(f"Corpus prepared: {total_files} text files")
    print(f"Manifest: {manifest_path}")
    total_bytes = sum(m["size_bytes"] for m in manifest)
    print(f"Total corpus size: {total_bytes / 1048576:.1f}MB")


if __name__ == "__main__":
    main()
