"""Embeddings, the arXiv pre-filter (spec 5.1), and dedup/clustering (spec 5.3).

Vectors are L2-normalized, so sqlite-vec's L2 distance d maps to cosine similarity 1 - d^2 / 2.
"""

import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from pydantic import BaseModel

from briefing.db import now_iso
from briefing.embed import Embedder, item_text
from briefing.llm import LLMError


def _vec_bytes(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def _vectors(conn: sqlite3.Connection, ids: list[str]) -> dict[str, np.ndarray]:
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        q = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT item_id, embedding FROM item_embeddings WHERE item_id IN ({q})", chunk
        )
        for r in rows:
            out[r["item_id"]] = np.frombuffer(r["embedding"], dtype=np.float32)
    return out


def embed_new(conn: sqlite3.Connection, embedder: Embedder) -> int:
    rows = conn.execute(
        """SELECT id, title, raw_text FROM items
           WHERE status = 'new' AND id NOT IN (SELECT item_id FROM item_embeddings)"""
    ).fetchall()
    if not rows:
        return 0
    vecs = embedder.encode([item_text(r["title"], r["raw_text"]) for r in rows])
    conn.executemany(
        "INSERT INTO item_embeddings (item_id, embedding) VALUES (?, ?)",
        [(r["id"], _vec_bytes(v)) for r, v in zip(rows, vecs, strict=True)],
    )
    conn.commit()
    return len(rows)


def prefilter_papers(
    conn: sqlite3.Connection,
    embedder: Embedder,
    profile: dict[str, Any],
    sources_cfg: dict[str, Any],
    threshold: float,
    max_papers: int,
) -> dict[str, int]:
    """Keep an arXiv paper if it is on HF Daily Papers, by a watched author/lab, or close to a
    theme. Everything else is dropped before it costs an LLM call.

    Runs before embed_new and embeds papers in memory: only kept papers get stored vectors,
    because sqlite-vec never frees the space of deleted vectors.
    """
    rows = conn.execute(
        """SELECT i.id, i.arxiv_id, i.author, i.title, i.raw_text FROM items i
           JOIN sources s ON s.id = i.source_id
           WHERE s.kind = 'arxiv' AND i.status = 'new' AND i.cluster_id IS NULL"""
    ).fetchall()
    if not rows:
        return {"papers_in": 0, "kept": 0}
    hf_ids = {
        r["arxiv_id"]
        for r in conn.execute(
            """SELECT i.arxiv_id FROM items i JOIN sources s ON s.id = i.source_id
               WHERE s.kind = 'hf_papers' AND i.arxiv_id IS NOT NULL"""
        )
    }
    watched = [
        w for w in sources_cfg.get("watched_authors", []) + sources_cfg.get("watched_labs", [])
    ]
    watched_re = re.compile("|".join(re.escape(w) for w in watched), re.I) if watched else None

    themes = [f"{t['name']}: {t['description']}" for t in profile["themes"]]
    theme_vecs = embedder.encode(themes, query=True)
    paper_vecs = embedder.encode([item_text(r["title"], r["raw_text"]) for r in rows])
    vecs = {r["id"]: v for r, v in zip(rows, paper_vecs, strict=True)}

    must, scored = [], []
    for r in rows:
        text = f"{r['author'] or ''} {r['raw_text'] or ''}"
        if r["arxiv_id"] in hf_ids or (watched_re and watched_re.search(text)):
            must.append(r["id"])
        elif r["id"] in vecs:
            sim = float(np.max(theme_vecs @ vecs[r["id"]]))
            if sim >= threshold:
                scored.append((sim, r["id"]))
    scored.sort(reverse=True)
    # Papers already on HF join the HF item's cluster and add no ranking cost, so the cap
    # applies only to papers picked by similarity.
    keep = set(must) | {i for _, i in scored[:max_papers]}
    drop = [r["id"] for r in rows if r["id"] not in keep]
    conn.executemany("UPDATE items SET status = 'dropped' WHERE id = ?", [(i,) for i in drop])
    conn.commit()
    return {"papers_in": len(rows), "kept": len(keep), "on_hf_or_watched": len(must)}


def _rebuild_vectors(conn: sqlite3.Connection) -> None:
    """Recreate the vec0 table so the space of deleted vectors is actually freed."""
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'item_embeddings'").fetchone()[
        "sql"
    ]
    rows = conn.execute("SELECT item_id, embedding FROM item_embeddings").fetchall()
    conn.execute("DROP TABLE item_embeddings")
    conn.execute(ddl)
    conn.executemany(
        "INSERT INTO item_embeddings (item_id, embedding) VALUES (?, ?)",
        [(r["item_id"], r["embedding"]) for r in rows],
    )


def purge_dropped_papers(conn: sqlite3.Connection) -> int:
    """Drop the text of pre-filtered arXiv papers, which never reach ranking.

    The row stays: its URL is what keeps the paper from being fetched again. Vectors left over
    from before prefilter_papers stopped storing them are removed, and the vector table rebuilt.
    """
    ids = [
        r["id"]
        for r in conn.execute(
            """SELECT i.id FROM items i JOIN sources s ON s.id = i.source_id
               WHERE s.kind = 'arxiv' AND i.status = 'dropped'
                 AND (i.raw_text IS NOT NULL
                      OR i.id IN (SELECT item_id FROM item_embeddings))"""
        )
    ]

    def count() -> int:
        return conn.execute("SELECT COUNT(*) FROM item_embeddings").fetchone()[0]

    before = count()
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        q = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM item_embeddings WHERE item_id IN ({q})", chunk)
        conn.execute(f"UPDATE items SET raw_text = NULL WHERE id IN ({q})", chunk)
    if count() < before:
        _rebuild_vectors(conn)
    conn.commit()
    return len(ids)


def _refresh_canonical(conn: sqlite3.Connection, cluster_id: str) -> None:
    best = conn.execute(
        """SELECT i.id FROM items i JOIN sources s ON s.id = i.source_id
           WHERE i.cluster_id = ?
           ORDER BY s.tier ASC, COALESCE(i.published_at, i.fetched_at) ASC LIMIT 1""",
        (cluster_id,),
    ).fetchone()
    conn.execute("UPDATE clusters SET canonical_item = ? WHERE id = ?", (best["id"], cluster_id))


SameEvent = Callable[[sqlite3.Row, sqlite3.Row], bool]


def cluster_new(
    conn: sqlite3.Connection,
    threshold: float,
    window_days: int,
    k: int = 50,
    borderline_low: float | None = None,
    same_event: SameEvent | None = None,
) -> dict[str, int]:
    """Assign every new, unclustered item to an existing cluster or a new one.

    Two papers with different arXiv ids are never the same event, so papers merge only on a
    matching id. For other pairs, similarity >= threshold merges; between borderline_low and
    threshold, `same_event` (an LLM yes/no check) decides.
    """
    window_start = (datetime.now(UTC) - timedelta(days=window_days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT id, arxiv_id, item_type, title, raw_text FROM items
           WHERE status = 'new' AND cluster_id IS NULL
           ORDER BY COALESCE(published_at, fetched_at)"""
    ).fetchall()
    vecs = _vectors(conn, [r["id"] for r in rows])
    joined = created = checks = 0
    for r in rows:
        target = None
        if r["arxiv_id"]:
            hit = conn.execute(
                """SELECT cluster_id FROM items
                   WHERE arxiv_id = ? AND cluster_id IS NOT NULL LIMIT 1""",
                (r["arxiv_id"],),
            ).fetchone()
            target = hit["cluster_id"] if hit else None
        if target is None and r["id"] in vecs:
            neighbors = conn.execute(
                """SELECT e.item_id, e.distance, i.cluster_id, i.item_type, i.title, i.raw_text
                   FROM item_embeddings e JOIN items i ON i.id = e.item_id
                   WHERE e.embedding MATCH ? AND k = ?
                     AND COALESCE(i.published_at, i.fetched_at) >= ?
                   ORDER BY e.distance""",
                (_vec_bytes(vecs[r["id"]]), k, window_start),
            ).fetchall()
            for n in neighbors:
                if n["item_id"] == r["id"] or n["cluster_id"] is None:
                    continue
                if r["item_type"] == "paper" and n["item_type"] == "paper":
                    continue
                sim = 1 - n["distance"] ** 2 / 2
                if sim >= threshold:
                    target = n["cluster_id"]
                elif same_event and borderline_low is not None and sim >= borderline_low:
                    checks += 1
                    if same_event(r, n):
                        target = n["cluster_id"]
                break  # sorted by distance: the closest eligible clustered item decides
        if target:
            conn.execute("UPDATE items SET cluster_id = ? WHERE id = ?", (target, r["id"]))
            _refresh_canonical(conn, target)
            joined += 1
        else:
            conn.execute(
                "INSERT INTO clusters (id, canonical_item, created_at) VALUES (?, ?, ?)",
                (r["id"], r["id"], now_iso()),
            )
            conn.execute("UPDATE items SET cluster_id = ? WHERE id = ?", (r["id"], r["id"]))
            created += 1
        conn.commit()
    return {
        "clustered": len(rows),
        "joined_existing": joined,
        "new_clusters": created,
        "llm_checks": checks,
    }


class SameEventOutput(BaseModel):
    same_event: bool


def llm_same_event(llm: Any, prompt: Any, model: str, effort: str | None = None) -> SameEvent:
    """Build the borderline-pair check on top of the LLM wrapper."""
    system = prompt.render()

    def check(a: sqlite3.Row, b: sqlite3.Row) -> bool:
        def show(label: str, row: sqlite3.Row) -> str:
            return f"Item {label}: {row['title']}\n{(row['raw_text'] or '')[:600]}"

        try:
            out = llm.parse(
                model=model,
                system=system,
                user=f"{show('A', a)}\n\n{show('B', b)}",
                schema=SameEventOutput,
                max_tokens=2000,
                effort=effort,
            )
        except LLMError:
            return False  # when unsure, keep them apart
        return out.same_event

    return check
