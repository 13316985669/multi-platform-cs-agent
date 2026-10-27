"""知识入库：多格式解析、按语义成块、相邻块重叠、增量更新。

策略：
- FAQ：问答对整体作为一个 chunk，不拆散
- 文档：按章节、完整清单、空行隔开的话术成块。1、2. 这类条目留在清单里
- 相邻块重叠上一块正文的 10% 到 20%，只在完整句子或完整条目处对齐
- 没有标题也没有空行的长段：嵌入可用时按相邻句相似度下降处切开，否则保持整段
- 增量更新：文档重新上传时旧 chunk 全部失效重建
"""
import io
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from ..database import SessionLocal
from ..models import KnowledgeChunk, KnowledgeDoc
from . import bm25, cache as answer_cache, embeddings, vectorstore

logger = logging.getLogger(__name__)

INDEX_VERSION = "5"  # 切块策略变化时递增，启动时触发重建
OVERLAP_TARGET = 0.15
OVERLAP_MIN = 0.10
OVERLAP_MAX = 0.20
_SIMILARITY_DROP = 0.1

_SECTION_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+\S|[一二三四五六七八九十百]+、\s*\S|（[一二三四五六七八九十百]+）\s*\S|\([一二三四五六七八九十百]+\)\s*\S)"
)
_LIST_LINE_RE = re.compile(r"^(?:\d+\s*[.、．:：)）]|[①②③④⑤⑥⑦⑧⑨⑩])\s*\S")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？])")
_MIN_CHUNK_LEN = 10


@dataclass
class Section:
    heading: str
    text: str  # 标题 + 正文，标题在前


# ============ 文档解析 ============

def parse_file(filename: str, data: bytes) -> str:
    """按扩展名解析文档为纯文本。整理后的文本交给同一套语义切分。"""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return _parse_pdf(data)
    if name.endswith(".docx"):
        return _parse_docx(data)
    if name.endswith(".doc"):
        raise ValueError("旧版 .doc 无法解析，请另存为 docx 后再上传")
    return data.decode("utf-8", errors="ignore")


def _parse_docx(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    counters: dict[tuple[int, int], int] = {}
    lines: list[str] = []
    for paragraph in doc.paragraphs:
        text = (paragraph.text or "").strip()
        if not text and not lines:
            continue
        if not text:
            lines.append("")
            continue
        if _is_word_heading(paragraph):
            lines.append("")
            lines.append(f"## {text}")
            lines.append("")
            continue
        prefix = _word_list_prefix(paragraph, counters)
        lines.append(f"{prefix}{text}" if prefix else text)
    return "\n".join(lines).strip()


def _is_word_heading(paragraph) -> bool:
    name = ""
    try:
        name = paragraph.style.name or ""
    except AttributeError:
        name = ""
    folded = name.lower()
    return folded.startswith("heading") or name.startswith("标题")


def _word_list_prefix(paragraph, counters: dict[tuple[int, int], int]) -> str:
    """自动编号写回正文。读不到具体数字时用间隔号，避免条目变成普通句子。"""
    try:
        p_pr = paragraph._p.pPr
        num_pr = p_pr.numPr if p_pr is not None else None
    except AttributeError:
        return ""
    if num_pr is None:
        return ""
    try:
        num_id = int(num_pr.numId.val)
        level = int(num_pr.ilvl.val) if num_pr.ilvl is not None and num_pr.ilvl.val is not None else 0
    except (TypeError, ValueError, AttributeError):
        return "· "
    fmt = _word_num_fmt(paragraph, num_id, level)
    if fmt not in ("decimal", "decimalZero", "chineseCounting", "japaneseCounting"):
        return "· "
    key = (num_id, level)
    counters[key] = counters.get(key, 0) + 1
    for deeper in [item for item in counters if item[0] == num_id and item[1] > level]:
        counters.pop(deeper, None)
    return f"{counters[key]}、"


def _word_num_fmt(paragraph, num_id: int, level: int) -> str | None:
    try:
        numbering = paragraph.part.numbering_part._element
    except Exception:  # noqa: BLE001
        return None
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    num = numbering.find(f".//w:num[@w:numId='{num_id}']", ns)
    if num is None:
        return None
    abstract = num.find("w:abstractNumId", ns)
    if abstract is None:
        return None
    abstract_id = abstract.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val")
    abstract_node = numbering.find(f".//w:abstractNum[@w:abstractNumId='{abstract_id}']", ns)
    if abstract_node is None:
        return None
    lvl = abstract_node.find(f"w:lvl[@w:ilvl='{level}']", ns)
    if lvl is None:
        return None
    fmt = lvl.find("w:numFmt", ns)
    if fmt is None:
        return None
    return fmt.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val")


def _parse_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return _unwrap_pdf_text(_strip_repeated_page_edges(pages))


def _strip_repeated_page_edges(pages: list[str]) -> list[str]:
    """相邻页开头或结尾完全相同的一行视为页眉页脚。"""
    if len(pages) < 2:
        return pages

    def edges(page: str) -> tuple[str, str]:
        rows = [line.strip() for line in page.splitlines() if line.strip()]
        if not rows:
            return "", ""
        return rows[0], rows[-1]

    heads, tails = zip(*(edges(page) for page in pages))
    header = heads[0] if heads[0] and all(item == heads[0] for item in heads) else ""
    footer = tails[0] if tails[0] and tails[0] != header and all(item == tails[0] for item in tails) else ""
    cleaned: list[str] = []
    for page in pages:
        rows = page.splitlines()
        if rows and header and rows[0].strip() == header:
            rows = rows[1:]
        if rows and footer and rows[-1].strip() == footer:
            rows = rows[:-1]
        cleaned.append("\n".join(rows))
    return cleaned


def _unwrap_pdf_text(text: str) -> str:
    """不以编号或章节开头的折行接回上一行。"""
    merged: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            merged.append("")
            continue
        if (
            merged
            and merged[-1]
            and not _SECTION_HEADING_RE.match(line)
            and not _LIST_LINE_RE.match(line)
        ):
            merged[-1] = merged[-1].rstrip() + line
            continue
        merged.append(line)
    return "\n".join(merged).strip()


# ============ 切分 ============

def split_text(text: str) -> list[str]:
    """按语义块切分，相邻块带 10% 到 20% 重叠。"""
    return [section.text for section in split_sections(text)]


def split_sections(text: str) -> list[Section]:
    """纯文本语义块。无嵌入时不把长段再切开。过短碎片不返回。"""
    return _apply_overlap(_merge_adjacent_list_blocks(_structural_sections(text)))


def _has_list_line(text: str) -> bool:
    return any(_LIST_LINE_RE.match(line.strip()) for line in (text or "").splitlines() if line.strip())


def _is_trailing_narrative(text: str) -> bool:
    """编号清单后面另起的叙述，以句号收束且自身没有编号。"""
    body = (text or "").strip()
    if not body or _has_list_line(body):
        return False
    return bool(re.search(r"[。！？]$", body))


def _merge_adjacent_list_blocks(sections: list[Section]) -> list[Section]:
    """同一标题下，相邻编号块和夹在中间的字段行合成一块。后面的叙述话术不并进去。"""
    merged: list[Section] = []
    index = 0
    while index < len(sections):
        current = sections[index]
        if not _has_list_line(_body_of(current)):
            merged.append(current)
            index += 1
            continue
        parts = [_body_of(current)]
        heading = current.heading
        nxt = index + 1
        while nxt < len(sections) and sections[nxt].heading == heading:
            body = _body_of(sections[nxt])
            if _is_trailing_narrative(body):
                break
            parts.append(body)
            nxt += 1
        text = f"{heading}\n" + "\n".join(part for part in parts if part) if heading else "\n".join(parts)
        merged.append(Section(heading=heading, text=text.strip()))
        index = nxt
    return merged


def _structural_sections(text: str) -> list[Section]:
    """按章节标题和空行成块。编号清单留在同一块，不按句号拆开。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    sections: list[Section] = []
    heading = ""
    for block in re.split(r"\n\s*\n", normalized):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        buf: list[str] = []

        def flush() -> None:
            nonlocal buf
            if not buf:
                return
            body = "\n".join(buf).strip()
            buf = []
            if not body:
                return
            chunk = f"{heading}\n{body}" if heading else body
            if len(chunk) < _MIN_CHUNK_LEN:
                return
            sections.append(Section(heading=heading, text=chunk))

        for line in lines:
            if _SECTION_HEADING_RE.match(line):
                flush()
                heading = line
                continue
            buf.append(line)
        flush()
    return sections


def _body_of(section: Section) -> str:
    if section.heading and section.text.startswith(section.heading):
        return section.text[len(section.heading):].lstrip("\n")
    return section.text


def _boundary_pieces(text: str) -> list[str]:
    """切成可对齐的句子或清单条目，拼回去等于原文。"""
    if not text:
        return []
    pieces: list[str] = []
    lines = text.split("\n")
    for index, line in enumerate(lines):
        newline = "\n" if index < len(lines) - 1 else ""
        stripped = line.strip()
        if not stripped or _LIST_LINE_RE.match(stripped) or _SECTION_HEADING_RE.match(stripped):
            pieces.append(line + newline)
            continue
        sentences = [part for part in _SENTENCE_SPLIT_RE.split(line) if part]
        if len(sentences) <= 1:
            pieces.append(line + newline)
            continue
        for sentence_index, sentence in enumerate(sentences):
            if sentence_index == len(sentences) - 1:
                pieces.append(sentence + newline)
            else:
                pieces.append(sentence)
    return [piece for piece in pieces if piece]


def _overlap_tail(text: str) -> str:
    """上一块末尾的 10% 到 20%。只取完整句子或完整条目。"""
    pieces = _boundary_pieces(text)
    if not pieces:
        return ""
    total = len(text)
    if len(pieces) == 1:
        return pieces[0]
    target = total * OVERLAP_TARGET
    taken = 0
    cut = len(pieces)
    for index in range(len(pieces) - 1, -1, -1):
        taken += len(pieces[index])
        cut = index
        if taken >= target:
            break

    def tail_from(index: int) -> str:
        return "".join(pieces[index:])

    tail = tail_from(cut)
    while len(tail) > total * OVERLAP_MAX and cut < len(pieces) - 1:
        cut += 1
        tail = tail_from(cut)
    if len(tail) < total * OVERLAP_MIN and cut > 0:
        cut -= 1
        tail = tail_from(cut)
    return tail


def _apply_overlap(sections: list[Section]) -> list[Section]:
    """下一块开头抄上上一块末尾，清单块本身保持完整。"""
    if len(sections) < 2:
        return sections
    overlapped = [sections[0]]
    for prev, curr in zip(sections, sections[1:]):
        tail = _overlap_tail(_body_of(prev)).strip()
        body = _body_of(curr)
        if tail and not body.startswith(tail):
            body = f"{tail}\n{body}"
        text = f"{curr.heading}\n{body}" if curr.heading else body
        overlapped.append(Section(heading=curr.heading, text=text))
    return overlapped


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


async def _split_by_similarity(text: str) -> list[str]:
    """无结构长段：相邻句子相似度明显下降时切开。失败则保持整段。"""
    pieces = [part for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
    if len(pieces) < 4 or not settings.embedding_configured:
        return [text]
    vectors = await embeddings.embed_texts(pieces)
    if not vectors or len(vectors) != len(pieces):
        return [text]
    scores = [_cosine(vectors[index], vectors[index + 1]) for index in range(len(vectors) - 1)]
    median = sorted(scores)[len(scores) // 2]
    groups: list[list[str]] = [[pieces[0]]]
    for index, score in enumerate(scores):
        if score < median - _SIMILARITY_DROP:
            groups.append([pieces[index + 1]])
        else:
            groups[-1].append(pieces[index + 1])
    if len(groups) == 1:
        return [text]
    return ["".join(group) for group in groups]


async def _refine_unstructured(sections: list[Section]) -> list[Section]:
    refined: list[Section] = []
    for section in sections:
        if section.heading or not settings.embedding_configured:
            refined.append(section)
            continue
        parts = await _split_by_similarity(section.text)
        if len(parts) <= 1:
            refined.append(section)
            continue
        refined.extend(
            Section(heading="", text=part)
            for part in parts
            if len(part) >= _MIN_CHUNK_LEN
        )
    return refined


async def build_sections(text: str) -> list[Section]:
    """入库用语义块。嵌入可用时先拆无结构长段，再做相邻重叠。"""
    sections = await _refine_unstructured(_structural_sections(text))
    return _apply_overlap(sections)


def _index_text(doc_title: str, heading: str, text: str) -> str:
    """检索文本带上文档标题和小节标题，后半段窗口也不会丢掉主题。"""
    body = text
    if heading and body.startswith(heading):
        body = body[len(heading):].lstrip("\n")
    parts = [f"【{doc_title}】"]
    if heading:
        parts.append(heading)
    if body:
        parts.append(body)
    return "\n".join(parts)


# ============ 入库 ============

def load_similar_questions(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


def faq_index_text(doc: KnowledgeDoc) -> str:
    """检索用文本。相似问不写回 doc.content，避免破坏「问/答」解析。"""
    first, _, rest = (doc.content or "").partition("\n")
    question = first.removeprefix("问：")
    answer = rest.removeprefix("答：")
    similars = load_similar_questions(doc.similar_questions)
    lines = [f"问：{question}"]
    if similars:
        lines.append("相似：" + "；".join(similars))
    lines.append(f"答：{answer}")
    return "\n".join(lines)


async def ingest_faq(doc_id: int):
    """FAQ 问答对整体作为一个 chunk 索引。content 格式：第一行问题，其余为答案。"""
    db = SessionLocal()
    try:
        doc = db.get(KnowledgeDoc, doc_id)
        if doc is None or doc.status != "active":
            return
        # 清旧 chunk（增量更新）
        _delete_chunks(db, doc_id)
        chunk = KnowledgeChunk(doc_id=doc_id, parent_id=None, chunk_index=0, content=faq_index_text(doc))
        db.add(chunk)
        db.commit()
        db.refresh(chunk)
        await _index_chunks([chunk], doc)
    finally:
        db.close()


async def ingest_document(doc_id: int):
    """文档按语义块入库。每块都可检索，不再做固定长度滑窗。"""
    db = SessionLocal()
    try:
        doc = db.get(KnowledgeDoc, doc_id)
        if doc is None or doc.status != "active":
            return
        _delete_chunks(db, doc_id)

        index_chunks: list[KnowledgeChunk] = []
        for i, section in enumerate(await build_sections(doc.content or "")):
            index_chunks.append(KnowledgeChunk(
                doc_id=doc_id, parent_id=None, chunk_index=i,
                content=_index_text(doc.title, section.heading, section.text),
            ))
        db.add_all(index_chunks)
        db.commit()
        for chunk in index_chunks:
            db.refresh(chunk)
        await _index_chunks(index_chunks, doc)
    finally:
        db.close()


_EMBED_BATCH = 32


@dataclass
class _VectorPiece:
    chunk_id: int
    doc_id: int
    parent_id: int
    title: str
    content: str


def _delete_chunk_rows(db, doc_ids: list[int]) -> None:
    """清掉这些文档的切片、回答缓存和向量。向量删除失败只记日志。"""
    if not doc_ids:
        return
    from . import pipeline  # 延迟导入，避免循环依赖
    pipeline.invalidate_faq_pins()  # 精确 FAQ 命中缓存随之失效
    answer_cache.invalidate_docs(doc_ids)
    db.query(KnowledgeChunk).filter(KnowledgeChunk.doc_id.in_(doc_ids)).delete(synchronize_session=False)
    db.commit()
    for doc_id in doc_ids:
        try:
            vectorstore.delete_by_doc(doc_id)
        except Exception:  # noqa: BLE001
            logger.warning("向量库删除 doc_id=%s 失败（可能未启用 embedding）", doc_id)


def _delete_chunks(db, doc_id: int):
    _delete_chunk_rows(db, [doc_id])


async def _write_active_chunks(db, doc: KnowledgeDoc) -> list[_VectorPiece]:
    """按当前正文重写切片。FAQ 一条，文档沿用章节切块。先切块再删旧切片，切块失败时旧索引还在。"""
    if doc.doc_type == "faq":
        pending = [KnowledgeChunk(
            doc_id=doc.id, parent_id=None, chunk_index=0, content=faq_index_text(doc),
        )]
    else:
        pending = []
        for index, section in enumerate(await build_sections(doc.content or "")):
            pending.append(KnowledgeChunk(
                doc_id=doc.id, parent_id=None, chunk_index=index,
                content=_index_text(doc.title, section.heading, section.text),
            ))
    _delete_chunk_rows(db, [doc.id])
    if pending:
        db.add_all(pending)
    db.commit()
    for chunk in pending:
        db.refresh(chunk)
    return [
        _VectorPiece(
            chunk_id=chunk.id,
            doc_id=doc.id,
            parent_id=chunk.parent_id or 0,
            title=doc.title,
            content=chunk.content,
        )
        for chunk in pending
    ]


async def _upsert_vectors(pieces: list[_VectorPiece]) -> None:
    """分批向量化后一次写入。接口失败则保留切片，交给 BM25。"""
    if not pieces:
        return
    ids: list[str] = []
    vectors_out: list[list[float]] = []
    documents: list[str] = []
    metadatas: list[dict] = []
    for start in range(0, len(pieces), _EMBED_BATCH):
        batch = pieces[start:start + _EMBED_BATCH]
        vectors = await embeddings.embed_texts([piece.content for piece in batch])
        if not vectors or len(vectors) != len(batch):
            logger.warning("批量向量化失败，这 %d 条只保留关键词索引", len(batch))
            continue
        for piece, vector in zip(batch, vectors):
            ids.append(f"chunk_{piece.chunk_id}")
            vectors_out.append(vector)
            documents.append(piece.content)
            metadatas.append({
                "doc_id": piece.doc_id,
                "chunk_id": piece.chunk_id,
                "parent_id": piece.parent_id,
                "title": piece.title,
            })
    if not ids:
        return
    try:
        vectorstore.upsert_chunks(ids, vectors_out, documents, metadatas)
    except Exception:  # noqa: BLE001
        logger.warning("向量写入失败，保留切片并使用关键词索引", exc_info=True)


async def sync_doc_index(doc_ids: list[int]) -> tuple[int, list[int]]:
    """按当前状态对齐多篇文档。启用的重建切片，其余清掉。BM25 只重建一次。"""
    unique_ids = list(dict.fromkeys(doc_ids))
    if not unique_ids:
        return 0, []
    failed: list[int] = []
    pieces: list[_VectorPiece] = []
    db = SessionLocal()
    try:
        docs = db.query(KnowledgeDoc).filter(KnowledgeDoc.id.in_(unique_ids)).all()
        by_id = {doc.id: doc for doc in docs}
        failed.extend(doc_id for doc_id in unique_ids if doc_id not in by_id)
        inactive = [doc.id for doc in docs if doc.status != "active"]
        if inactive:
            _delete_chunk_rows(db, inactive)
        for doc in docs:
            if doc.status != "active":
                continue
            try:
                pieces.extend(await _write_active_chunks(db, doc))
            except Exception:  # noqa: BLE001
                logger.exception("对齐文档索引失败 doc_id=%s", doc.id)
                db.rollback()
                failed.append(doc.id)
        await _upsert_vectors(pieces)
    finally:
        db.close()
    rebuild_bm25_from_db()
    failed_set = set(failed)
    updated = len([doc_id for doc_id in unique_ids if doc_id not in failed_set])
    return updated, failed


async def _index_chunks(chunks: list[KnowledgeChunk], doc: KnowledgeDoc):
    """写入向量库（可降级）并重建 BM25。父块不进索引。"""
    if not chunks:
        rebuild_bm25_from_db()
        return
    vectors = await embeddings.embed_texts([c.content for c in chunks])
    if vectors is not None:
        vectorstore.upsert_chunks(
            ids=[f"chunk_{c.id}" for c in chunks],
            embeddings=vectors,
            documents=[c.content for c in chunks],
            metadatas=[{"doc_id": doc.id, "chunk_id": c.id, "parent_id": c.parent_id or 0,
                        "title": doc.title} for c in chunks],
        )
    rebuild_bm25_from_db()


def _indexable(rows: list[KnowledgeChunk]) -> list[KnowledgeChunk]:
    """有子块的父块只用于生成上下文，不参与检索。"""
    parent_ids = {row.parent_id for row in rows if row.parent_id}
    return [row for row in rows if row.id not in parent_ids]


def rebuild_bm25_from_db():
    """从 DB 全量重建 BM25 索引（启动时/知识变更后调用）。"""
    db = SessionLocal()
    try:
        rows = (
            db.query(KnowledgeChunk)
            .join(KnowledgeDoc, KnowledgeDoc.id == KnowledgeChunk.doc_id)
            .filter(KnowledgeDoc.status == "active")
            .all()
        )
        bm25.rebuild([
            {"chunk_id": f"chunk_{c.id}", "doc_id": c.doc_id, "content": c.content}
            for c in _indexable(rows)
        ])
    finally:
        db.close()


def _index_version_path() -> Path:
    return Path(settings.chroma_dir) / "index_version.txt"


def index_version_stale() -> bool:
    path = _index_version_path()
    if not path.is_file():
        return True
    return path.read_text(encoding="utf-8").strip() != INDEX_VERSION


def _write_index_version() -> None:
    path = _index_version_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(INDEX_VERSION, encoding="utf-8")


async def reindex_all() -> None:
    """切块版本变化后，重嵌入全部生效文档。失败则不写版本号，下次启动再试。"""
    answer_cache.clear()
    db = SessionLocal()
    try:
        docs = (
            db.query(KnowledgeDoc)
            .filter(KnowledgeDoc.status == "active")
            .all()
        )
        jobs = [(doc.id, doc.doc_type) for doc in docs]
    finally:
        db.close()
    logger.info("开始重建知识库索引，版本 %s，文档 %d 篇", INDEX_VERSION, len(jobs))
    for doc_id, doc_type in jobs:
        try:
            if doc_type == "faq":
                await ingest_faq(doc_id)
            else:
                await ingest_document(doc_id)
        except Exception:  # noqa: BLE001
            logger.exception("重建文档失败 doc_id=%s，保留旧索引版本以便重试", doc_id)
            return
    _write_index_version()
    logger.info("知识库索引已重建到版本 %s", INDEX_VERSION)
