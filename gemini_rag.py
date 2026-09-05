"""
Gemini File API RAG replacement for NotebookLM.
Two modes: full-context (upload_docs/query) and real RAG (build_index/rag_query).

Install: pip install google-genai pypdf openpyxl python-dotenv --break-system-packages
Auth: set GEMINI_API_KEY in .env or environment (get one at aistudio.google.com/apikey)

NOTE: model IDs and free-tier quotas shift fast and vary per account/project.
Trust your own dashboard (aistudio.google.com/rate-limit) over any number in
this file's comments.
"""

import os
import csv
import json
import time
import random
import tempfile
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY not found. Set it in your .env file or environment.")

client = genai.Client(api_key=api_key)

# --- Model config: verify against YOUR dashboard, not this comment ---
MODEL = "gemini-3.1-flash-lite"   # per your dashboard: 500 RPD / 15 RPM free tier
EMBED_MODEL = "gemini-embedding-2"  # per your dashboard: 1,000 RPD / 100 RPM free tier
THROTTLE_SECONDS = 4.5  # keeps you under 15 RPM with margin; tune to your actual RPM cap

MAX_FILE_MB = 2048   # File API hard cap (2GB)
WARN_FILE_MB = 50    # flag anything over this before burning a request on it


def _call_with_backoff(fn, *args, max_retries: int = 5, **kwargs):
    """Wraps any client.* call. Retries on 429 with exponential backoff + jitter.
    Does NOT help if you've hit RPD (daily) — that only resets at midnight Pacific,
    backoff will just spin uselessly until max_retries, so check RPD first."""
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                wait = (2 ** attempt) + random.uniform(0, 1)
                print(f"rate limited, backing off {wait:.1f}s (attempt {attempt+1}/{max_retries})")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("exceeded max retries — likely hit RPD (daily cap), not RPM. check dashboard.")


# ---------------------------------------------------------------------------
# Local file inspection (zero API cost — run before every upload/index)
# ---------------------------------------------------------------------------

def inspect_file(path: str, sample_rows: int = 3) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)

    size_mb = p.stat().st_size / (1024 * 1024)
    info = {"path": path, "size_mb": round(size_mb, 2), "ext": p.suffix.lower(), "approx_tokens": None}
    ext = info["ext"]

    try:
        if ext in (".txt", ".md"):
            text = p.read_text(errors="ignore")
            lines = text.splitlines()
            info["lines"] = len(lines)
            info["approx_tokens"] = len(text) // 4
            info["sample"] = "\n".join(lines[:sample_rows])

        elif ext == ".csv":
            with p.open(newline="", errors="ignore") as f:
                rows = list(csv.reader(f))
            info["rows"] = len(rows) - 1 if rows else 0
            info["columns"] = len(rows[0]) if rows else 0
            info["column_names"] = rows[0] if rows else []
            info["sample"] = rows[1:1 + sample_rows]
            info["approx_tokens"] = p.stat().st_size // 4

        elif ext in (".xlsx", ".xls"):
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            info["sheets"] = {n: {"rows": wb[n].max_row, "columns": wb[n].max_column} for n in wb.sheetnames}
            info["approx_tokens"] = p.stat().st_size // 4

        elif ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(path)
            info["pages"] = len(reader.pages)
            sample_text = "".join((pg.extract_text() or "")[:500] for pg in reader.pages[:sample_rows])
            info["sample"] = sample_text
            info["approx_tokens"] = size_mb * 1024 * 1024 // 4  # weak proxy for scanned PDFs

        elif ext == ".json":
            data = json.loads(p.read_text(errors="ignore"))
            info["top_level_type"] = type(data).__name__
            info["top_level_count"] = len(data) if hasattr(data, "__len__") else None
            info["sample"] = str(data)[:500]
            info["approx_tokens"] = p.stat().st_size // 4

        else:
            info["sample"] = "(no structured inspector for this extension — inspect manually)"

    except Exception as e:
        info["inspect_error"] = str(e)

    return info


def print_inspection(info: dict) -> None:
    print(f"--- {info['path']} ---")
    print(f"  size: {info['size_mb']} MB", "⚠️ LARGE" if info["size_mb"] > WARN_FILE_MB else "")
    for k, v in info.items():
        if k in ("path", "size_mb", "sample"):
            continue
        print(f"  {k}: {v}")
    if info.get("sample") is not None:
        print(f"  sample: {info['sample']!r}"[:300])
    print()


def preflight(paths: list[str]) -> list[dict]:
    reports = []
    for path in paths:
        info = inspect_file(path)
        print_inspection(info)
        if info["size_mb"] > MAX_FILE_MB:
            raise ValueError(f"{path} exceeds {MAX_FILE_MB}MB File API limit")
        reports.append(info)
    total_tokens = sum(r["approx_tokens"] or 0 for r in reports)
    print(f"estimated total tokens across batch: {total_tokens:,.0f}")
    if total_tokens > 900_000:
        print("⚠️  approaching/exceeding model context window — consider chunking or real RAG")
    return reports


# ---------------------------------------------------------------------------
# xlsx -> text conversion (File API rejects raw .xlsx binary)
# ---------------------------------------------------------------------------

def _convert_xlsx_to_text(path: str) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    chunks = []
    for sheetname in wb.sheetnames:
        sheet = wb[sheetname]
        chunks.append(f"=== Sheet: {sheetname} ===")
        for row in sheet.iter_rows(values_only=True):
            if any(row):
                chunks.append(" | ".join(str(c).strip() if c is not None else "" for c in row))
        chunks.append("")
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Mode 1: full-context (upload whole files, no retrieval step)
# ---------------------------------------------------------------------------

def upload_docs(paths: list[str]) -> list:
    """Inspect, then upload files once; returns File objects to reference in prompts."""
    preflight(paths)
    files = []
    temp_files = []
    try:
        for path in paths:
            ext = Path(path).suffix.lower()
            upload_path = path
            upload_config = None

            if ext in (".xlsx", ".xls"):
                print(f"converting {path} to structured text for File API ingestion...")
                content = _convert_xlsx_to_text(path)
                tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
                tmp.write(content)
                tmp.close()
                temp_files.append(tmp.name)
                upload_path = tmp.name
                upload_config = types.UploadFileConfig(mime_type="text/plain", display_name=Path(path).name)

            elif ext == ".csv":
                upload_config = types.UploadFileConfig(mime_type="text/plain", display_name=Path(path).name)

            f = _call_with_backoff(client.files.upload, file=upload_path, config=upload_config)
            while f.state.name == "PROCESSING":
                time.sleep(2)
                f = client.files.get(name=f.name)
            if f.state.name == "FAILED":
                raise RuntimeError(f"upload failed: {path}")
            files.append(f)
            print(f"uploaded: {path} -> {f.name}")
            time.sleep(THROTTLE_SECONDS)
    finally:
        for tmp_path in temp_files:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return files


def query(files: list, question: str, system: str | None = None) -> str:
    resp = _call_with_backoff(
        client.models.generate_content,
        model=MODEL,
        contents=files + [question],
        config={"system_instruction": system} if system else None,
    )
    return resp.text


def list_uploaded() -> None:
    for f in client.files.list():
        print(f.name, f.display_name, f.state.name)


def delete_all() -> None:
    for f in client.files.list():
        client.files.delete(name=f.name)


# ---------------------------------------------------------------------------
# Mode 2: real RAG (chunk + embed + retrieve top-k, no full-context upload)
# Persisted to SQLite so the index survives a restart.
#
# Scaling path: once brute-force cosine over all rows gets slow (thousands+
# of chunks) or you need concurrent access from multiple processes, migrate
# this table to Postgres + pgvector. Same schema, same _cosine logic, except
# the ranking becomes a SQL query:
#     SELECT text, source FROM chunks ORDER BY vector <-> %s LIMIT %s
# (pgvector's <-> operator does the ANN search server-side instead of
# pulling every row into Python.) Not needed at your current scale.
# ---------------------------------------------------------------------------

import sqlite3

DB_PATH = "rag_index.db"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            text TEXT NOT NULL,
            vector TEXT NOT NULL  -- JSON-encoded list[float]
        )
    """)
    return conn


def _chunk_text(text: str, chunk_size: int = 800, overlap: int = 100) -> list[str]:
    chunks = []
    i = 0
    while i < len(text):
        chunks.append(text[i:i + chunk_size])
        i += chunk_size - overlap
    return chunks


def _embed(texts: list[str], task_type: str) -> list[list[float]]:
    resp = _call_with_backoff(
        client.models.embed_content,
        model=EMBED_MODEL,
        contents=texts,
        config={"task_type": task_type},  # "RETRIEVAL_DOCUMENT" or "RETRIEVAL_QUERY"
    )
    return [e.values for e in resp.embeddings]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _extract_text_for_index(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in (".txt", ".md", ".csv"):
        return Path(path).read_text(errors="ignore")
    if ext in (".xlsx", ".xls"):
        return _convert_xlsx_to_text(path)
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        return "\n".join((pg.extract_text() or "") for pg in reader.pages)
    raise ValueError(f"{path}: no text extractor wired up for this filetype yet")


def build_index(paths: list[str], reindex: bool = False) -> None:
    """Chunk + embed docs, persist to SQLite. Does NOT touch the File API.

    reindex=False (default): skips any source path already indexed, so
    restarting the script doesn't re-embed and re-burn API calls on files
    you've already processed.
    reindex=True: deletes and re-embeds the given paths (use after editing
    a source file).
    """
    conn = _db()
    for path in paths:
        existing = conn.execute("SELECT COUNT(*) FROM chunks WHERE source = ?", (path,)).fetchone()[0]
        if existing and not reindex:
            print(f"skipping {path} — already indexed ({existing} chunks). pass reindex=True to redo it.")
            continue
        if existing and reindex:
            conn.execute("DELETE FROM chunks WHERE source = ?", (path,))

        text = _extract_text_for_index(path)
        chunks = _chunk_text(text)
        vectors = _embed(chunks, task_type="RETRIEVAL_DOCUMENT")
        for chunk, vec in zip(chunks, vectors):
            conn.execute(
                "INSERT INTO chunks (source, text, vector) VALUES (?, ?, ?)",
                (path, chunk, json.dumps(vec)),
            )
        conn.commit()
        print(f"indexed {path}: {len(chunks)} chunks")
        time.sleep(THROTTLE_SECONDS)
    conn.close()


def index_stats() -> None:
    conn = _db()
    rows = conn.execute("SELECT source, COUNT(*) FROM chunks GROUP BY source").fetchall()
    conn.close()
    if not rows:
        print("index is empty")
        return
    for source, count in rows:
        print(f"  {source}: {count} chunks")


def clear_index() -> None:
    conn = _db()
    conn.execute("DELETE FROM chunks")
    conn.commit()
    conn.close()


def rag_query(question: str, k: int = 3) -> str:
    conn = _db()
    rows = conn.execute("SELECT source, text, vector FROM chunks").fetchall()
    conn.close()
    if not rows:
        raise RuntimeError("index is empty — call build_index() first")

    q_vec = _embed([question], task_type="RETRIEVAL_QUERY")[0]
    scored = sorted(
        ({"source": s, "text": t, "score": _cosine(q_vec, json.loads(v))} for s, t, v in rows),
        key=lambda c: c["score"],
        reverse=True,
    )
    top = scored[:k]
    print("retrieved chunks from:", [(c["source"], round(c["score"], 3)) for c in top])
    context = "\n\n".join(f"[{c['source']}]\n{c['text']}" for c in top)
    resp = _call_with_backoff(
        client.models.generate_content,
        model=MODEL,
        contents=[f"Context:\n{context}\n\nQuestion: {question}"],
    )
    return resp.text


if __name__ == "__main__":
    docs = upload_docs([
        "docs/spec_v1.pdf",
        "docs/schema.md",
    ])
    answer = query(
        docs,
        "Summarize the key architectural decisions across these documents "
        "and flag any contradictions between them.",
        system="You are a technical analyst. Be precise, cite which document "
               "each claim comes from, and flag ambiguity rather than guessing.",
    )
    print(answer)