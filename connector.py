#!/usr/bin/env python3
"""
Connector (Tier 2)

Reads Basic Memory entity notes, finds missed cross-references,
and adds [[wiki-links]] between related entities.

No LLM needed — uses keyword co-occurrence and shared fact matching.
Designed to run hourly or on idle.

Relationships discovered:
- Entities that share IPs (same machine)
- Entities that reference each other's names in their facts
- Services that share a deployed-on target
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
BM_ENTITIES = HOME / "basic-memory" / "entities"
LOG_FILE = HOME / ".claude-mem" / "connector-log.txt"


def log(msg):
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(LOG_FILE, "a") as f:
            f.write(f"{ts} [connector] {msg}\n")
    except Exception:
        pass


def slugify(name):
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-") or "unknown"


def read_entity_notes():
    """Read all entity notes, return dict of slug -> {content, facts, links}."""
    entities = {}
    if not BM_ENTITIES.exists():
        return entities

    for md_path in BM_ENTITIES.glob("*.md"):
        slug = md_path.stem
        content = md_path.read_text()

        # Extract existing wiki-links
        existing_links = set(re.findall(r"\[\[([^\]]+)\]\]", content))

        # Extract facts
        facts = {}
        for line in content.split("\n"):
            line = line.strip()
            if not line.startswith("- [fact]"):
                continue
            # Parse: - [fact] key: value (from [[session]])
            m = re.match(r"- \[fact\] ([a-z][a-z0-9_-]*):\s*(.+?)(?:\s*\(from \[\[)", line)
            if m:
                key = m.group(1)
                value = m.group(2).strip()
                if key not in facts:
                    facts[key] = []
                facts[key].append(value)
            else:
                # Fallback: - [fact] info: value
                m = re.match(r"- \[fact\] (\w+):\s*(.+?)(?:\s*\(from \[\[)", line)
                if m:
                    key = m.group(1)
                    value = m.group(2).strip()
                    if key not in facts:
                        facts[key] = []
                    facts[key].append(value)

        entities[slug] = {
            "path": md_path,
            "content": content,
            "facts": facts,
            "links": existing_links,
        }

    return entities


def find_ip_connections(entities):
    """Find entities that share IP addresses (likely same machine or co-located)."""
    ip_to_entities = {}

    for slug, data in entities.items():
        for ip_val in data["facts"].get("ip", []):
            # Extract IP from value
            ips = re.findall(r"\d+\.\d+\.\d+\.\d+", ip_val)
            for ip in ips:
                if ip not in ip_to_entities:
                    ip_to_entities[ip] = set()
                ip_to_entities[ip].add(slug)

    connections = []
    for ip, slugs in ip_to_entities.items():
        if len(slugs) > 1:
            slug_list = sorted(slugs)
            for i in range(len(slug_list)):
                for j in range(i + 1, len(slug_list)):
                    connections.append((slug_list[i], slug_list[j], f"shares IP {ip}"))

    return connections


def find_name_references(entities):
    """Find entities whose facts mention other entity names."""
    connections = []

    entity_slugs = set(entities.keys())

    for slug, data in entities.items():
        # Search all fact values for references to other entities
        all_fact_text = " ".join(
            v for vals in data["facts"].values() for v in vals
        ).lower()

        for other_slug in entity_slugs:
            if other_slug == slug:
                continue
            # Skip very short slugs (avoid false matches like "n8n" in random text)
            if len(other_slug) < 4:
                continue
            # Check if other entity's name appears in this entity's facts
            if other_slug in all_fact_text:
                connections.append((slug, other_slug, "references"))

    return connections


def find_deployed_on_connections(entities):
    """Find services that share a deployed-on target."""
    deployed_on = {}

    for slug, data in entities.items():
        for val in data["facts"].get("deployed-on", []):
            target = slugify(val)
            if target not in deployed_on:
                deployed_on[target] = set()
            deployed_on[target].add(slug)

    connections = []
    for target, services in deployed_on.items():
        if len(services) > 1:
            service_list = sorted(services)
            for i in range(len(service_list)):
                for j in range(i + 1, len(service_list)):
                    connections.append(
                        (service_list[i], service_list[j],
                         f"both deployed on {target}")
                    )

    return connections


def add_links_to_entity(entity_path, content, new_links, date_str):
    """Append wiki-links to an entity note."""
    additions = []
    for target_slug, reason in new_links:
        link_line = f"- [[{target_slug}]] ({reason})"
        if link_line not in content and f"[[{target_slug}]]" not in content:
            additions.append(link_line)

    if not additions:
        return 0

    # Add under a Connections section
    section_header = "\n## Connections (auto-discovered)\n"
    if "## Connections (auto-discovered)" not in content:
        content = content.rstrip() + "\n" + section_header
    else:
        # Find the section and append
        pass

    content = content.rstrip() + "\n" + "\n".join(additions) + "\n"
    entity_path.write_text(content)
    return len(additions)


def main():
    entities = read_entity_notes()
    if not entities:
        log("No entity notes found")
        return

    log(f"Read {len(entities)} entity notes")

    # Find all connections
    all_connections = []
    all_connections.extend(find_ip_connections(entities))
    all_connections.extend(find_name_references(entities))
    all_connections.extend(find_deployed_on_connections(entities))

    log(f"Found {len(all_connections)} potential connections")

    # Deduplicate — keep one connection per pair
    seen_pairs = set()
    unique_connections = []
    for a, b, reason in all_connections:
        pair = tuple(sorted([a, b]))
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            unique_connections.append((a, b, reason))

    log(f"Unique connections: {len(unique_connections)}")

    # Apply links — add to both entities in each connection
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    links_added = 0

    # Group links by entity
    entity_links = {}
    for a, b, reason in unique_connections:
        if a not in entity_links:
            entity_links[a] = []
        entity_links[a].append((b, reason))
        if b not in entity_links:
            entity_links[b] = []
        entity_links[b].append((a, reason))

    for slug, new_links in entity_links.items():
        if slug not in entities:
            continue
        data = entities[slug]
        count = add_links_to_entity(data["path"], data["content"], new_links, date_str)
        if count > 0:
            links_added += count
            log(f"  {slug}.md: +{count} links")

    log(f"Connector complete: {links_added} links added across {len(entity_links)} entities")


if __name__ == "__main__":
    main()
