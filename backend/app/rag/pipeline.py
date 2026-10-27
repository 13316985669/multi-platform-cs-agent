"""RAG 检索管线：查询改写 → 分路混合召回 → RRF 融合 → Rerank → 相对分数过滤。

精排返回空结果或调用失败时，退回融合结果，按余弦或 BM25 分差决定是否生成。
精排有分数但低于阈值时仍不生成。"""
import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass, field

from ..config import settings
from ..database import SessionLocal
from ..models import KnowledgeChunk, KnowledgeDoc
from . import bm25, embeddings, ingest, rerank, rewrite, vectorstore

logger = logging.getLogger(__name__)

RRF_K = 60  # RRF 平滑常数
DENSE_MIN_SCORE = 0.3  # 余弦相似度低于此值的向量命中不参与融合
DENSE_ABSOLUTE_MIN = 0.45  # 没配精排时，第一名低于此值直接未命中
RELATIVE_SCORE_RATIO = 0.65  # 相对最高分过低的命中不送进生成
DEGRADED_SCORE_GAP = 1.5  # 纯 BM25：第一名须达到其他资料的倍数，否则视为含糊
DENSE_SCORE_GAP = 0.08  # 余弦：第一名须比另一篇至少高出该差值
PARENT_CONTEXT_MAX = 800  # 父块短于该长度才整段替换命中片段


@dataclass
class RetrievalResult:
    contexts: list[dict] = field(default_factory=list)  # [{content, source, score}]
    rewritten_queries: list[str] = field(default_factory=list)
    rerank_top_score: float | None = None
    passed: bool = False
    degraded: bool = False  # 纯 BM25 降级模式
    reason: str = "no_hits"  # passed | no_hits | rerank_below_threshold | rerank_unavailable | dense_gap | bm25_gap
    dense_count: int = 0
    bm25_count: int = 0
    fused_top: list[dict] = field(default_factory=list)
    rerank_status: str = "skipped"  # used | unavailable | skipped
    facets: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    exact_faq: bool = False
    query_vector: list[float] | None = None  # 原句向量，供回答缓存复用
    stage_ms: dict = field(default_factory=dict)  # rewrite_ms / embed_ms / rerank_ms


_ASK_HINT = re.compile(
    r"多少钱|什么价|价格|几块|几元|包邮|运费|邮费|发货|几天|退货|退换|换货|质保|发票|库存|有货|保修|优惠|折扣"
)
_CONNECTOR = re.compile(r"还有|另外|以及|顺便")


async def retrieve(query: str, history: list[dict] | None = None, top_k: int = 3,
                   summary: str = "", *, broaden: bool = True) -> RetrievalResult:
    """完整检索管线。返回带引用的知识片段；未过阈值时 passed=False（上层触发转人工）。"""
    result = RetrievalResult()

    # 1. 查询改写（指代消解 + 同义多路）。精确 FAQ 也要留下改写，提示词里的「实际在问」用它。
    rewrite_started = time.monotonic()
    queries = await rewrite.rewrite_query(query, history or [], summary=summary)
    result.rewritten_queries = queries
    result.stage_ms["rewrite_ms"] = _elapsed_ms(rewrite_started)
    pinned = _exact_faq_context(query)
    if pinned is not None:
        result.contexts = [pinned]
        result.passed = True
        result.reason = "passed"
        result.gaps = []
        result.exact_faq = True
        return result

    # 2. 每条改写单独成路：向量批量编码，BM25 各查各的
    embed_started = time.monotonic()
    rankings, degraded, vectors = await _collect_rankings(queries)
    result.stage_ms["embed_ms"] = _elapsed_ms(embed_started)
    result.query_vector = _vector_for_original(query, queries, vectors)
    result.degraded = degraded
    result.dense_count, result.bm25_count = _path_counts(rankings)
    if not rankings:
        result.reason = "no_hits"
        return await _finish_retrieve(result, query, history or [], summary, top_k, broaden)

    # 3. RRF 融合（每路独立计名次，同一路内重复 chunk 只计一次）
    fused = _rrf_fuse(rankings)
    candidates = _without_blocked(fused[:20], query)
    result.fused_top = _fused_preview(candidates)
    if not candidates:
        result.reason = "no_hits"
        return await _finish_retrieve(result, query, history or [], summary, top_k, broaden)

    # 4. Rerank 精排。原句权重大于改写句。空结果或调用失败退回融合分差。
    if settings.rerank_configured:
        rerank_started = time.monotonic()
        reranked = await _rerank_weighted(queries, [c["content"] for c in candidates], top_n=top_k)
        result.stage_ms["rerank_ms"] = _elapsed_ms(rerank_started)
        if reranked:
            scored = [
                {**candidates[r["index"]], "score": r["score"]} for r in reranked
                if 0 <= r["index"] < len(candidates)
            ]
            result.rerank_status = "used"
            result.rerank_top_score = scored[0]["score"] if scored else None
            selected = _filter_reranked(scored)
            if not selected:
                result.reason = "rerank_below_threshold"
        else:
            result.rerank_status = "unavailable"
            result.rerank_top_score = None
            has_dense = any(hit.get("dense_score") is not None for hit in candidates)
            selected = _select_dense_gap(candidates) if has_dense else _select_degraded(candidates)
            if not selected:
                result.reason = "rerank_unavailable"
    else:
        has_dense = any(hit.get("dense_score") is not None for hit in candidates)
        result.rerank_status = "skipped"
        selected = _select_dense_gap(candidates) if has_dense else _select_degraded(candidates)
        result.rerank_top_score = None
        if not selected:
            result.reason = "dense_gap" if has_dense else "bm25_gap"

    if not selected and result.reason in (
        "dense_gap", "bm25_gap", "rerank_unavailable", "rerank_below_threshold",
    ):
        selected = _exact_phrase_hits(query, candidates)

    if not selected:
        result.passed = False
        return await _finish_retrieve(result, query, history or [], summary, top_k, broaden)

    # 5. 父子索引：短父块才整段替换；否则用带标题的命中片段
    resolved = _resolve_hits(selected)
    result.contexts = _dedupe_contexts(resolved)[:top_k]
    result.passed = bool(result.contexts)
    result.reason = "passed" if result.passed else "no_hits"
    return await _finish_retrieve(result, query, history or [], summary, top_k, broaden)


async def _finish_retrieve(result: RetrievalResult, query: str, history: list[dict], summary: str,
                           top_k: int, broaden: bool) -> RetrievalResult:
    """第一轮没过阈值时，用更宽的问法再检一次。"""
    return await _maybe_broaden(result, query, history or [], summary, top_k, broaden)


_NON_ANSWER_CATEGORIES = {"留资话术", "跟进话术"}
_MARKET_CATEGORY = "营销话术"
_OFFER_QUERY = re.compile(r"多少钱|费用|收费|价格|优惠|活动|3980|周年")


def _blocked_categories(query: str) -> set[str]:
    """普通回答不拿留资和跟进。不问费用时也不拿营销话术。"""
    blocked = set(_NON_ANSWER_CATEGORIES)
    if not _OFFER_QUERY.search(query or ""):
        blocked.add(_MARKET_CATEGORY)
    return blocked


def _categories_for(doc_ids: set[int]) -> dict[int, str]:
    if not doc_ids:
        return {}
    db = SessionLocal()
    try:
        rows = db.query(KnowledgeDoc.id, KnowledgeDoc.category).filter(KnowledgeDoc.id.in_(doc_ids)).all()
        return {row.id: row.category or "" for row in rows}
    finally:
        db.close()


def _without_blocked(hits: list[dict], query: str) -> list[dict]:
    blocked = _blocked_categories(query)
    categories = _categories_for({hit.get("doc_id") for hit in hits if isinstance(hit.get("doc_id"), int)})
    return [hit for hit in hits if categories.get(hit.get("doc_id"), "") not in blocked]


_faq_pins_lock = threading.Lock()
_faq_pins: dict[str, int] | None = None  # compact 问题/标题/相似问 -> doc_id


def invalidate_faq_pins() -> None:
    """知识库变更后清掉精确命中缓存（ingest 写路径调用）。"""
    global _faq_pins
    with _faq_pins_lock:
        _faq_pins = None


def _labels_of(doc) -> set[str]:
    """一条 FAQ 的全部可钉住标签：标题、问句、相似问。"""
    labels = {_compact(doc.title or "")}
    first, _, _rest = (doc.content or "").partition("\n")
    labels.add(_compact(first.removeprefix("问：").removeprefix("问:")))
    labels.update(_compact(item) for item in ingest.load_similar_questions(doc.similar_questions))
    labels.discard("")
    return labels


def _build_faq_pins() -> dict[str, int]:
    """全量启用 FAQ 的 标签 -> doc_id。同一标签先建的优先。"""
    db = SessionLocal()
    try:
        docs = db.query(KnowledgeDoc).filter(
            KnowledgeDoc.doc_type == "faq",
            KnowledgeDoc.status == "active",
        ).all()
    finally:
        db.close()
    pins: dict[str, int] = {}
    for doc in docs:
        for label in _labels_of(doc):
            pins.setdefault(label, doc.id)
    return pins


def _exact_faq_context(query: str) -> dict | None:
    """用户原句等于 FAQ 的问题、标题或相似问时，返回这条 FAQ 切片。

    标签->doc_id 走进程内哈希；命中后再实时读这条文档校验状态和标签，
    缓存过期（改库未触发失效）时自动重建。
    """
    phrase = _compact(query)
    if len(phrase) < 2:
        return None
    global _faq_pins
    with _faq_pins_lock:
        if _faq_pins is None:
            _faq_pins = _build_faq_pins()
        doc_id = _faq_pins.get(phrase)
    if doc_id is None:
        return None
    db = SessionLocal()
    try:
        doc = db.get(KnowledgeDoc, doc_id)
        if doc is None or doc.doc_type != "faq" or doc.status != "active" or phrase not in _labels_of(doc):
            invalidate_faq_pins()
            return None
        category = doc.category or ""
        # 原句等于留资或跟进时，这条就是答案。模糊检索仍排除它们。
        if category in _blocked_categories(query) and category not in _NON_ANSWER_CATEGORIES:
            return None
        chunk = (
            db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.doc_id == doc.id)
            .order_by(KnowledgeChunk.chunk_index)
            .first()
        )
        content = chunk.content if chunk is not None else ingest.faq_index_text(doc)
        return {
            "content": _with_title(doc.title, content),
            "source": doc.title,
            "doc_id": doc.id,
            "doc_type": "faq",
            "parent_id": None,
            "chunk_id": f"chunk_{chunk.id}" if chunk is not None else None,
            "score": None,
        }
    finally:
        db.close()


async def _maybe_broaden(result: RetrievalResult, query: str, history: list[dict], summary: str,
                         top_k: int, broaden: bool) -> RetrievalResult:
    """第一轮没过阈值时，用更宽的问法再检一次。"""
    if result.passed or not broaden:
        return result
    broaden_started = time.monotonic()
    wider = await rewrite.broaden_query(query, result.rewritten_queries, result.reason, history, summary)
    _add_stage_ms(result.stage_ms, {"rewrite_ms": _elapsed_ms(broaden_started)})
    if not wider:
        return result
    second = await retrieve(wider, history=history, summary=summary, top_k=top_k, broaden=False)
    if second.passed:
        second.rewritten_queries = list(result.rewritten_queries) + list(second.rewritten_queries)
        _add_stage_ms(second.stage_ms, result.stage_ms)
        # 缓存余弦用用户原句的向量，不用放宽后的问法。
        second.query_vector = result.query_vector
        return second
    _add_stage_ms(result.stage_ms, second.stage_ms)
    return result


def split_facets(query: str) -> list[str]:
    """一句里有两件独立的事才拆开。单问返回空列表，调用方只检索一次。"""
    text = (query or "").strip()
    if not text:
        return []
    qmarks = len(re.findall(r"[？?]", text))
    if qmarks >= 2:
        raw = re.split(r"[？?]", text)
    elif _CONNECTOR.search(text):
        raw = _CONNECTOR.split(text)
    elif re.search(r"和|跟", text):
        raw = re.split(r"和|跟", text)
    elif ("，" in text or "," in text) and len(_ASK_HINT.findall(text)) >= 2:
        raw = re.split(r"[，,]", text)
    else:
        return []
    parts = []
    for part in raw:
        cleaned = part.strip(" ，,、。！!？? ")
        if len(cleaned) >= 2:
            parts.append(cleaned)
    if len(parts) < 2:
        return []
    hinted = sum(1 for part in parts if _ASK_HINT.search(part))
    if hinted >= 2 or qmarks >= 2 or _CONNECTOR.search(text):
        return parts[:3]
    return []


def facet_covered(facet: str, contexts: list[dict]) -> bool:
    """这条资料写到了该子问题的关键词，或子问题原文就在正文里。"""
    blob = "\n".join((item.get("content") or "") for item in contexts)
    compact_blob = _compact(blob)
    phrase = _compact(facet)
    if len(phrase) >= 2 and phrase in compact_blob:
        return True
    hints = _ASK_HINT.findall(facet or "")
    return bool(hints) and all(hint in blob for hint in hints)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _add_stage_ms(into: dict, extra: dict) -> None:
    for key, value in extra.items():
        into[key] = int(into.get(key, 0)) + int(value or 0)


def _vector_for_original(query: str, queries: list[str], vectors: list | None) -> list[float] | None:
    """批量编码里对应用户原句的那一条。改写句不拿来做缓存余弦。"""
    if not vectors:
        return None
    index = None
    for i, text in enumerate(queries):
        if text == query:
            index = i
    if index is None or index >= len(vectors) or not vectors[index]:
        return None
    return vectors[index]


def _exact_phrase_hits(query: str, candidates: list[dict]) -> list[dict]:
    """原句完整出现在切片或文档标题里时，分差规则不能把这一条丢掉。"""
    phrase = _compact(query)
    if len(phrase) < 4:
        return []
    matched: list[dict] = []
    title_checks: dict[int, dict] = {}
    for hit in candidates:
        if phrase in _compact(hit.get("content") or ""):
            matched.append(hit)
            continue
        doc_id = hit.get("doc_id")
        if isinstance(doc_id, int):
            title_checks.setdefault(doc_id, hit)
    if not title_checks:
        return matched
    db = SessionLocal()
    try:
        rows = (
            db.query(KnowledgeDoc.id, KnowledgeDoc.title)
            .filter(KnowledgeDoc.id.in_(list(title_checks)))
            .all()
        )
        for row in rows:
            if phrase in _compact(row.title or ""):
                matched.append(title_checks[row.id])
        return matched
    finally:
        db.close()


def _path_counts(rankings: list[list[dict]]) -> tuple[int, int]:
    dense: set[str] = set()
    sparse: set[str] = set()
    for ranking in rankings:
        for hit in ranking:
            key = str(hit.get("chunk_id"))
            if hit.get("score_kind") == "dense":
                dense.add(key)
            elif hit.get("score_kind") == "bm25":
                sparse.add(key)
    return len(dense), len(sparse)


def _fused_preview(candidates: list[dict]) -> list[dict]:
    preview = []
    for hit in candidates[:3]:
        preview.append({
            "chunk_id": hit.get("chunk_id"),
            "doc_id": hit.get("doc_id"),
            "content": (hit.get("content") or "")[:80],
            "score": hit.get("score"),
            "dense_score": hit.get("dense_score"),
            "bm25_score": hit.get("bm25_score"),
            "rrf_score": hit.get("rrf_score"),
        })
    return preview


def _dense_hits(vec: list[float]) -> list[dict]:
    """向量路检索 + 阈值过滤（同步，经 to_thread 调用）。"""
    return [
        {**hit, "score_kind": "dense"}
        for hit in _safe_dense_hits(vec)
        if (hit.get("score") or 0.0) >= settings.rag_dense_min_score
    ]


def _sparse_hits(query: str) -> list[dict]:
    """BM25 路检索（同步，经 to_thread 调用）。"""
    return [{**hit, "score_kind": "bm25"} for hit in bm25.query(query, top_k=20)]


async def _collect_rankings(queries: list[str]) -> tuple[list[list[dict]], bool, list | None]:
    """每条查询各产出一路向量、一路 BM25。向量不可用时 degraded=True。

    Chroma/BM25 均为同步调用，经 to_thread 执行，避免并发答复时阻塞事件循环。
    """
    vectors = await embeddings.embed_texts(queries)
    degraded = not vectors or len(vectors) != len(queries)
    usable = None if degraded else vectors
    if degraded:
        vectors = [None] * len(queries)

    rankings: list[list[dict]] = []
    for query, vec in zip(queries, vectors):
        if vec is not None:
            dense = await asyncio.to_thread(_dense_hits, vec)
            if dense:
                rankings.append(dense)
        sparse = await asyncio.to_thread(_sparse_hits, query)
        if sparse:
            rankings.append(sparse)
    return rankings, degraded, usable


def _safe_dense_hits(vec: list[float]) -> list[dict]:
    """向量库故障不让整个任务失败重试：本路降级纯 BM25，只记日志。"""
    try:
        return vectorstore.query(vec, top_k=20)
    except Exception:  # noqa: BLE001
        logger.warning("向量检索失败，本路降级纯 BM25", exc_info=True)
        return []


async def _rerank_one(text: str, documents: list[str], top_n: int) -> list[dict] | None:
    try:
        return await rerank.rerank(text, documents, top_n=top_n)
    except Exception:  # noqa: BLE001
        logger.warning("精排一路失败", exc_info=True)
        return None


async def _rerank_weighted(queries: list[str], documents: list[str], top_n: int) -> list[dict] | None:
    """原句权重 1，改写句权重 0.5。两路同时打。只拿到其中一路时用那一路的分。"""
    original = queries[-1] if queries else ""
    lead = queries[0] if queries else ""
    pairs = [(original, 1.0)]
    if lead and lead != original:
        pairs.append((lead, 0.5))
    each = max(top_n, len(documents))
    ranked_lists = await asyncio.gather(*[_rerank_one(text, documents, each) for text, _weight in pairs])
    combined: dict[int, float] = {}
    weights: dict[int, float] = {}
    any_ok = False
    for (_text, weight), ranked in zip(pairs, ranked_lists):
        if not ranked:
            continue
        any_ok = True
        for item in ranked:
            index = item["index"]
            combined[index] = combined.get(index, 0.0) + item["score"] * weight
            weights[index] = weights.get(index, 0.0) + weight
    if not any_ok:
        return None
    merged = [
        {"index": index, "score": combined[index] / weights[index]}
        for index in combined
    ]
    return sorted(merged, key=lambda item: item["score"], reverse=True)


def _filter_reranked(hits: list[dict]) -> list[dict]:
    """保留不低于绝对阈值、且不低于最高分一定比例的命中。"""
    if not hits:
        return []
    top_score = hits[0].get("score") or 0.0
    if top_score < settings.rag_rerank_threshold:
        return []
    floor = top_score * settings.rag_relative_score_ratio
    kept = []
    for hit in hits:
        score = hit.get("score") or 0.0
        if score < settings.rag_rerank_threshold or score < floor:
            continue
        kept.append(hit)
    return kept


def _select_dense_gap(hits: list[dict]) -> list[dict]:
    """未配精排、有向量结果：只看融合第一名的余弦，须比另一篇至少高 DENSE_SCORE_GAP。"""
    if not hits:
        return []
    top = hits[0]
    top_score = top.get("dense_score")
    if not isinstance(top_score, (int, float)):
        logger.info("融合第一名没有向量分，判为未命中")
        return []
    if float(top_score) < settings.rag_dense_absolute_min:
        logger.info("向量检索第一名绝对分过低，判为未命中")
        return []
    rival_score: float | None = None
    for hit in hits[1:]:
        if _same_doc(top, hit):
            continue
        score = hit.get("dense_score")
        if not isinstance(score, (int, float)):
            continue
        rival_score = float(score) if rival_score is None else max(rival_score, float(score))
    if rival_score is None or float(top_score) >= rival_score + settings.rag_dense_score_gap:
        return [top]
    logger.info("向量检索第一名未明显高于其他资料，判为未命中")
    return []


def _bm25_score(hit: dict) -> float:
    raw = hit.get("bm25_score")
    if not isinstance(raw, (int, float)):
        raw = hit.get("score")
    return float(raw) if isinstance(raw, (int, float)) else 0.0


def _doc_group(hit: dict) -> tuple:
    doc_id = hit.get("doc_id")
    if doc_id is not None:
        return ("doc", doc_id)
    return ("chunk", hit.get("chunk_id"))


def _select_degraded(hits: list[dict]) -> list[dict]:
    """纯 BM25：按每篇资料自己的最高 BM25 分排序，第一名须达到另一篇的 1.5 倍。"""
    if not hits:
        return []
    best: dict[tuple, dict] = {}
    for hit in hits:
        key = _doc_group(hit)
        current = best.get(key)
        if current is None or _bm25_score(hit) > _bm25_score(current):
            best[key] = hit
    ordered = sorted(best.values(), key=_bm25_score, reverse=True)
    top = ordered[0]
    if len(ordered) == 1:
        return [top]
    top_score = _bm25_score(top)
    rival_score = _bm25_score(ordered[1])
    if rival_score <= 0 or top_score >= rival_score * settings.rag_degraded_score_gap:
        return [top]
    logger.info("降级检索第一名未明显高于其他资料，判为未命中")
    return []


def _same_doc(a: dict, b: dict) -> bool:
    doc_id = a.get("doc_id")
    return doc_id is not None and doc_id == b.get("doc_id")


def _dedupe_contexts(contexts: list[dict]) -> list[dict]:
    """同一父块或同一 FAQ 只留分数更高的一条（调用前已按分降序）。"""
    seen: set[tuple] = set()
    kept = []
    for item in contexts:
        if item.get("parent_id"):
            key = ("parent", item["parent_id"])
        elif item.get("doc_type") == "faq" and item.get("doc_id") is not None:
            key = ("faq", item["doc_id"])
        else:
            key = ("chunk", item.get("chunk_id"))
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return kept


def _rrf_fuse(rankings: list[list[dict]]) -> list[dict]:
    """Reciprocal Rank Fusion。每一路单独计名次，路内重复 chunk 不重复加分。"""
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}
    dense_scores: dict[str, float] = {}
    bm25_scores: dict[str, float] = {}
    raw_scores: dict[str, float] = {}
    for ranking in rankings:
        seen_in_list: set[str] = set()
        rank = 0
        for hit in ranking:
            key = hit["chunk_id"]
            if key in seen_in_list:
                continue
            seen_in_list.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
            rank += 1
            items.setdefault(key, dict(hit))
            raw = hit.get("score")
            if not isinstance(raw, (int, float)):
                continue
            kind = hit.get("score_kind")
            if kind == "dense":
                dense_scores[key] = max(dense_scores.get(key, 0.0), float(raw))
            elif kind == "bm25":
                bm25_scores[key] = max(bm25_scores.get(key, 0.0), float(raw))
            else:
                raw_scores[key] = max(raw_scores.get(key, 0.0), float(raw))
    fused = sorted(items.values(), key=lambda h: scores[h["chunk_id"]], reverse=True)
    for hit in fused:
        key = hit["chunk_id"]
        hit["rrf_score"] = scores[key]
        if key in dense_scores:
            hit["dense_score"] = dense_scores[key]
        if key in bm25_scores:
            hit["bm25_score"] = bm25_scores[key]
        # 余弦和 BM25 不写成同一个 score，避免后面拿去比倍数
        if key in dense_scores and key in bm25_scores:
            hit.pop("score", None)
        elif key in dense_scores:
            hit["score"] = dense_scores[key]
        elif key in bm25_scores:
            hit["score"] = bm25_scores[key]
        elif key in raw_scores:
            hit["score"] = raw_scores[key]
    return fused


def _resolve_hits(hits: list[dict]) -> list[dict]:
    db = SessionLocal()
    try:
        return [_resolve_parent(db, hit) for hit in hits]
    finally:
        db.close()


def _with_title(title: str, content: str) -> str:
    prefix = f"【{title}】"
    if content.startswith(prefix):
        return content
    return f"{prefix}\n{content}"


def _resolve_parent(db, hit: dict) -> dict:
    """短父块整段作为上下文；长父块保留带标题的命中片段，避免无关正文稀释答案。"""
    chunk_id_raw = hit.get("chunk_id")
    try:
        chunk_id = int(str(chunk_id_raw).replace("chunk_", ""))
    except (TypeError, ValueError):
        return {
            "content": hit.get("content") or "",
            "source": "未知",
            "score": hit.get("score"),
            "chunk_id": chunk_id_raw,
        }
    chunk = db.get(KnowledgeChunk, chunk_id)
    if chunk is None:
        return {
            "content": hit.get("content") or "",
            "source": "未知",
            "score": hit.get("score"),
            "chunk_id": chunk_id_raw,
        }
    content = chunk.content
    parent_id = chunk.parent_id
    if parent_id:
        parent = db.get(KnowledgeChunk, parent_id)
        if parent and len(parent.content) <= PARENT_CONTEXT_MAX:
            content = parent.content
    doc = db.get(KnowledgeDoc, chunk.doc_id)
    title = doc.title if doc else f"doc#{chunk.doc_id}"
    return {
        "content": _with_title(title, content),
        "source": title,
        "doc_id": doc.id if doc else chunk.doc_id,
        "doc_type": doc.doc_type if doc else "",
        "parent_id": parent_id,
        "chunk_id": chunk_id_raw,
        "score": hit.get("score"),
    }
