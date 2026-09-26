#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent Long-Term Memory Engine (SQLite FTS5)
Provides persistent storage, retrieval, and full-text search across sessions.
"""

import argparse
import datetime
import os
import pathlib
import re
import sqlite3
import sys
from typing import Dict, List, Optional, Tuple

WORKSPACE_ROOT = pathlib.Path(__file__).resolve().parents[4]
DEFAULT_DB_PATH = WORKSPACE_ROOT / ".agent_memory" / "memory.db"
VAULT_DIR = WORKSPACE_ROOT / ".agent_memory"


def get_db(db_path: pathlib.Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    init_schema(con)
    return con


def init_schema(con: sqlite3.Connection) -> None:
    with con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                key TEXT UNIQUE NOT NULL,
                title TEXT,
                content TEXT NOT NULL,
                tags TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        con.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                key,
                title,
                content,
                tags,
                content='memories',
                content_rowid='id'
            );
            """
        )
        con.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, key, title, content, tags)
                VALUES (new.id, new.key, new.title, new.content, new.tags);
            END;
            """
        )
        con.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, key, title, content, tags)
                VALUES('delete', old.id, old.key, old.title, old.content, old.tags);
            END;
            """
        )
        con.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, key, title, content, tags)
                VALUES('delete', old.id, old.key, old.title, old.content, old.tags);
                INSERT INTO memories_fts(rowid, key, title, content, tags)
                VALUES (new.id, new.key, new.title, new.content, new.tags);
            END;
            """
        )


def store_memory(
    con: sqlite3.Connection,
    category: str,
    key: str,
    content: str,
    title: Optional[str] = None,
    tags: Optional[str] = None,
) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if not title:
        first_line = content.strip().splitlines()[0] if content.strip() else key
        title = first_line.lstrip("#").strip()[:100]

    with con:
        con.execute(
            """
            INSERT INTO memories (category, key, title, content, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                category=excluded.category,
                title=excluded.title,
                content=excluded.content,
                tags=excluded.tags,
                updated_at=excluded.updated_at;
            """,
            (category.lower(), key.strip(), title, content.strip(), tags or "", now, now),
        )


def get_memory(con: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
    cur = con.execute("SELECT * FROM memories WHERE key = ?", (key.strip(),))
    return cur.fetchone()


def delete_memory(con: sqlite3.Connection, key: str) -> bool:
    with con:
        cur = con.execute("DELETE FROM memories WHERE key = ?", (key.strip(),))
        return cur.rowcount > 0


def list_memories(
    con: sqlite3.Connection, category: Optional[str] = None, limit: int = 50
) -> List[sqlite3.Row]:
    if category:
        cur = con.execute(
            "SELECT id, category, key, title, tags, updated_at FROM memories WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
            (category.lower(), limit),
        )
    else:
        cur = con.execute(
            "SELECT id, category, key, title, tags, updated_at FROM memories ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
    return cur.fetchall()


def recall_memories(
    con: sqlite3.Connection,
    query: str,
    category: Optional[str] = None,
    limit: int = 5,
) -> List[Dict]:
    cleaned_query = re.sub(r"[^\w\s-]", " ", query).strip()
    if not cleaned_query:
        return []

    tokens = cleaned_query.split()
    fts_query = " OR ".join(f'"{t}"*' for t in tokens)

    results = []
    try:
        sql = """
            SELECT m.id, m.category, m.key, m.title, m.content, m.tags, m.updated_at,
                   snippet(memories_fts, 2, '[[', ']]', '...', 20) as snippet,
                   bm25(memories_fts) as rank
            FROM memories_fts f
            JOIN memories m ON m.id = f.rowid
            WHERE memories_fts MATCH ?
        """
        params = [fts_query]
        if category:
            sql += " AND m.category = ?"
            params.append(category.lower())

        sql += " ORDER BY rank ASC LIMIT ?"
        params.append(limit)

        cur = con.execute(sql, params)
        for row in cur.fetchall():
            results.append(dict(row))
    except sqlite3.OperationalError:
        # Fallback to standard LIKE matching if FTS expression parsing fails
        like_term = f"%{cleaned_query}%"
        sql = """
            SELECT id, category, key, title, content, tags, updated_at,
                   substr(content, 1, 140) as snippet,
                   0 as rank
            FROM memories
            WHERE (title LIKE ? OR content LIKE ? OR key LIKE ? OR tags LIKE ?)
        """
        params = [like_term, like_term, like_term, like_term]
        if category:
            sql += " AND category = ?"
            params.append(category.lower())
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        cur = con.execute(sql, params)
        for row in cur.fetchall():
            results.append(dict(row))

    return results


def sync_markdown_vault(con: sqlite3.Connection) -> int:
    """Scan .agent_memory/*.md files and index each major section."""
    count = 0
    if not VAULT_DIR.exists():
        return count

    files_mapping = {
        "decisions.md": "decision",
        "architecture.md": "architecture",
        "bugs_and_gotchas.md": "bug",
        "session_log.md": "session",
    }

    for filename, cat in files_mapping.items():
        filepath = VAULT_DIR / filename
        if not filepath.exists():
            continue

        raw_text = filepath.read_text(encoding="utf-8")
        # Split by level 2 or level 3 markdown headers
        sections = re.split(r"\n(?=#{2,3}\s+)", raw_text)
        for idx, section in enumerate(sections):
            lines = [line.strip() for line in section.strip().splitlines() if line.strip()]
            if not lines:
                continue
            header = lines[0].lstrip("#").strip()
            if not header or header.lower().startswith("structure"):
                continue

            slug = re.sub(r"[^\w-]", "-", f"{cat}-{header.lower()}").strip("-")
            slug = re.sub(r"-+", "-", slug)[:80]
            store_memory(
                con=con,
                category=cat,
                key=slug,
                content=section.strip(),
                title=header,
                tags=f"vault,{cat}",
            )
            count += 1

    return count


def format_cli_output(data) -> str:
    lines = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                lines.append(f"[{item.get('category', '').upper()}] {item.get('key')}")
                if item.get("title"):
                    lines.append(f"  Title: {item['title']}")
                if item.get("tags"):
                    lines.append(f"  Tags: {item['tags']}")
                if item.get("snippet"):
                    lines.append(f"  Snippet: {item['snippet']}")
                elif item.get("content"):
                    preview = item["content"].splitlines()[0][:120]
                    lines.append(f"  Preview: {preview}")
                lines.append(f"  Updated: {item.get('updated_at', '')}")
                lines.append("-" * 60)
            else:
                lines.append(str(item))
    elif isinstance(data, dict):
        for k, v in data.items():
            lines.append(f"{k}: {v}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Agent Long-Term Memory CLI (SQLite FTS5)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=pathlib.Path,
        default=DEFAULT_DB_PATH,
        help="Path to memory SQLite database",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # store command
    p_store = subparsers.add_parser("store", help="Store or update a memory entry")
    p_store.add_argument("key", help="Unique identifier or slug")
    p_store.add_argument("content", help="Memory text or markdown content")
    p_store.add_argument(
        "--category",
        "-c",
        default="note",
        choices=["decision", "architecture", "bug", "preference", "session", "note"],
        help="Category of memory",
    )
    p_store.add_argument("--title", "-t", help="Optional title")
    p_store.add_argument("--tags", help="Comma-separated tags")

    # recall command
    p_recall = subparsers.add_parser("recall", help="Search memory using FTS5")
    p_recall.add_argument("query", help="Keywords or phrase to recall")
    p_recall.add_argument("--category", "-c", help="Filter by category")
    p_recall.add_argument("--limit", "-n", type=int, default=5, help="Result limit")

    # get command
    p_get = subparsers.add_parser("get", help="Get exact memory entry by key")
    p_get.add_argument("key", help="Key to retrieve")

    # list command
    p_list = subparsers.add_parser("list", help="List stored memory keys")
    p_list.add_argument("--category", "-c", help="Filter by category")
    p_list.add_argument("--limit", "-n", type=int, default=50, help="Max entries to return")

    # delete command
    p_delete = subparsers.add_parser("delete", help="Delete a memory entry")
    p_delete.add_argument("key", help="Key to delete")

    # sync command
    subparsers.add_parser("sync", help="Synchronize markdown vault files into FTS5 index")

    args = parser.parse_args()
    con = get_db(args.db)

    if args.command == "store":
        store_memory(
            con=con,
            category=args.category,
            key=args.key,
            content=args.content,
            title=args.title,
            tags=args.tags,
        )
        print(f"STORED: [{args.category.upper()}] {args.key}")

    elif args.command == "recall":
        hits = recall_memories(
            con=con,
            query=args.query,
            category=args.category,
            limit=args.limit,
        )
        if not hits:
            print(f"NO MEMORY MATCH FOR: '{args.query}'")
        else:
            print(f"RECALLED {len(hits)} RELEVANT MEMORIES:\n")
            print(format_cli_output(hits))

    elif args.command == "get":
        row = get_memory(con, args.key)
        if not row:
            print(f"KEY NOT FOUND: '{args.key}'")
            sys.exit(1)
        print(f"KEY: {row['key']}")
        print(f"CATEGORY: {row['category']}")
        print(f"TITLE: {row['title']}")
        print(f"TAGS: {row['tags']}")
        print(f"UPDATED: {row['updated_at']}\n")
        print("CONTENT:")
        print(row["content"])

    elif args.command == "list":
        rows = list_memories(con, category=args.category, limit=args.limit)
        if not rows:
            print("NO MEMORIES FOUND.")
        else:
            print(f"STORED MEMORIES ({len(rows)} entries):")
            for r in rows:
                print(f"- [{r['category'].upper()}] {r['key']} | {r['title']} ({r['updated_at'][:10]})")

    elif args.command == "delete":
        ok = delete_memory(con, args.key)
        if ok:
            print(f"DELETED: {args.key}")
        else:
            print(f"KEY NOT FOUND: {args.key}")
            sys.exit(1)

    elif args.command == "sync":
        indexed_count = sync_markdown_vault(con)
        print(f"SYNC COMPLETED: {indexed_count} vault sections indexed into SQLite FTS5.")


if __name__ == "__main__":
    main()
