"""Turning files, links and text into a searchable knowledge base, and picking what the model sees."""
from __future__ import annotations

import io
import re

import httpx
from bs4 import BeautifulSoup

from . import config, db

SUPPORTED_EXT = (".pdf", ".docx", ".txt", ".md", ".csv", ".json", ".html", ".htm")


class ExtractError(Exception):
    pass


def extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
        if len(text.strip()) < 30:
            raise ExtractError("PDF ichida matn topilmadi (skan qilingan bo'lishi mumkin).")
        return text
    if name.endswith(".docx"):
        from docx import Document
        doc = Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(parts)
    if name.endswith((".html", ".htm")):
        return _html_to_text(data.decode("utf-8", errors="ignore"))[1]
    if name.endswith((".txt", ".md", ".csv", ".json")):
        return data.decode("utf-8", errors="ignore")
    raise ExtractError("Bu fayl turini o'qiy olmayman. PDF, DOCX, TXT, MD yoki CSV yuboring.")


def _html_to_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    title = (soup.title.string or "").strip() if soup.title else ""
    text = soup.get_text("\n")
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return title, text.strip()


async def fetch_url(url: str) -> tuple[str, str]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=20,
                                 headers={"User-Agent": "Mozilla/5.0 SupportBot"}) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    title, text = _html_to_text(resp.text)
    if len(text) < 50:
        raise ExtractError("Sahifadan matn olinmadi.")
    return title or url, text


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        while len(para) > size:  # very long paragraph: hard split
            head, para = para[:size], para[size - overlap:]
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head)
        if len(current) + len(para) + 2 <= size:
            current = f"{current}\n\n{para}" if current else para
        else:
            if current:
                chunks.append(current)
            tail = current[-overlap:] if current else ""
            current = f"{tail}\n\n{para}" if tail else para
    if current:
        chunks.append(current)
    return chunks


async def add(owner_id: int, title: str, kind: str, text: str) -> int:
    text = text.strip()
    if not text:
        raise ExtractError("Matn bo'sh.")
    return await db.add_source(owner_id, title[:200], kind, text, chunk_text(text))


# --- retrieval ---

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _tsquery(text: str) -> str | None:
    """OR-query tolerant of Uzbek/Russian suffixes: 'kurslarimiz' must still find 'kurs'.
    Each word adds a short prefix term (recall) and the exact word (ranks exact matches higher)."""
    terms: list[str] = []
    for word in _WORD.findall(text.lower()):
        if len(word) < 3 or word.isdigit() and len(word) < 4:
            continue
        for term in ([f"{word[:4]}:*", word] if len(word) > 4 else [f"{word}:*"]):
            if term not in terms:
                terms.append(term)
        if len(terms) >= 60:
            break
    return " | ".join(terms) if terms else None


async def context_for(owner_id: int, query: str) -> tuple[str, bool]:
    """Returns (knowledge_text, is_full). Full knowledge is cache-friendly; retrieved is per-question."""
    size = await db.knowledge_size(owner_id)
    if size == 0:
        return "", True
    if size <= config.FULL_CONTEXT_CHARS:
        rows = await db.all_sources(owner_id)
        return "\n\n".join(f"### {r['title']}\n{r['content']}" for r in rows), True
    tsq = _tsquery(query)
    if not tsq:
        return "", False
    rows = await db.search_chunks(owner_id, tsq, config.RETRIEVAL_CHUNKS)
    return "\n\n".join(f"### {r['title']}\n{r['content']}" for r in rows), False


# --- knowledge shipped with the code (knowledge/*.md) ---

PLAYBOOK_FILE = "00_playbook.md"


def read_playbook() -> str:
    path = config.KNOWLEDGE_DIR / PLAYBOOK_FILE
    return path.read_text(encoding="utf-8") if path.exists() else ""


def seed_files() -> list[tuple[str, str]]:
    if not config.KNOWLEDGE_DIR.exists():
        return []
    out = []
    for path in sorted(config.KNOWLEDGE_DIR.glob("*.md")):
        if path.name == PLAYBOOK_FILE:
            continue
        text = path.read_text(encoding="utf-8").strip()
        if text:
            out.append((path.stem, text))
    return out


async def seed(owner_id: int) -> int:
    """Replace the owner's file-backed knowledge with what is in KNOWLEDGE_DIR now."""
    items = [(title, text, chunk_text(text)) for title, text in seed_files()]
    await db.ensure_tenant(owner_id)
    await db.replace_seed_sources(owner_id, items)
    return len(items)
