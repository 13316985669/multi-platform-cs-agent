"""BM25 稀疏检索：jieba 中文分词 + rank_bm25，内存索引，启动/变更时重建。

客服知识库规模（几千~几万 chunk）下内存计算完全够用，零外部依赖。
"""
import logging
import threading

import jieba
from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)


class _BM25Okapi(BM25Okapi):
    """覆写 idf：非正 idf 兜底为小的正值（Lucene 风格恒正 idf）。

    rank_bm25 原生实现中「出现在 ≥ 一半文档」的词 idf ≤ 0；
    小语料冷启动（知识库只有 1~2 个 chunk）时所有词 idf 全为 0，检索恒空。
    """

    def _calc_idf(self, nd):
        super()._calc_idf(nd)
        for word, val in self.idf.items():
            if val <= 0:
                self.idf[word] = 1e-3


_lock = threading.Lock()
_bm25: _BM25Okapi | None = None
_corpus_meta: list[dict] = []  # 与 BM25 语料同序: {chunk_id, doc_id, content}


_STOPWORDS = {"的", "了", "吗", "呢", "啊", "呀"}


def _tokenize(text: str) -> list[str]:
    return [t for t in jieba.lcut(text.lower()) if t.strip() and t not in _STOPWORDS]


def rebuild(chunks: list[dict]):
    """全量重建索引。chunks: [{chunk_id, doc_id, content}]"""
    global _bm25, _corpus_meta
    with _lock:
        _corpus_meta = chunks
        tokenized = [_tokenize(c["content"]) for c in chunks]
        _bm25 = _BM25Okapi(tokenized) if tokenized else None
    logger.info("BM25 索引已重建，共 %d 个 chunk", len(chunks))


def query(text: str, top_k: int = 20) -> list[dict]:
    """BM25 检索。返回 [{chunk_id, doc_id, content, score}]，score 为 BM25 原始分（越大越好）。"""
    with _lock:
        if _bm25 is None or not _corpus_meta:
            return []
        scores = _bm25.get_scores(_tokenize(text))
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:top_k]
        return [
            {**_corpus_meta[i], "score": float(s)}
            for i, s in ranked if s > 0
        ]


def total_chunks() -> int:
    return len(_corpus_meta)
