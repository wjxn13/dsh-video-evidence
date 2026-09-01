"""字幕分块与相关性打分。

中文没有空格分词，这里用「单字 + 相邻二元组(bigram)」作为词项，
配合 BM25。零依赖、确定性、无需 embedding 模型，符合 0-token 设计。
"""
import math
import re
from collections import Counter

CJK = r"\u4e00-\u9fff"


def build_chunks(segments, window: float = 45.0, stride: float = 30.0):
    """把 ASR 细片段按滑窗合并成可阅读的片段（保留时间戳）。"""
    if not segments:
        return []
    segs = sorted(segments, key=lambda s: s["start"])
    end = max(s["end"] for s in segs)
    chunks, t = [], segs[0]["start"]
    while t < end:
        t2 = t + window
        parts = [s["text"] for s in segs if s["start"] < t2 and s["end"] > t]
        if parts:
            text = re.sub(r"\s+", "", "".join(parts))
            if text:
                chunks.append({"start": round(float(t), 1),
                               "end": round(min(t2, end), 1),
                               "text": text})
        t += stride
    return chunks


def tokenize(text: str):
    text = re.sub(r"\s+", "", text or "")
    zh = [c for c in text if "\u4e00" <= c <= "\u9fff"]
    toks = list(zh) + [zh[i] + zh[i + 1] for i in range(len(zh) - 1)]
    for m in re.findall(r"[A-Za-z0-9]+", text):
        toks.append(m.lower())
    return toks


def bm25(query: str, docs_tokens, k1: float = 1.5, b: float = 0.75):
    """docs_tokens: List[List[str]]，返回每个文档的 BM25 分数。"""
    n = len(docs_tokens)
    if n == 0:
        return []
    df = Counter()
    for d in docs_tokens:
        df.update(set(d))
    avgdl = sum(len(d) for d in docs_tokens) / n
    q = tokenize(query)
    scores = []
    for d in docs_tokens:
        tf = Counter(d)
        dl = len(d) or 1
        s = 0.0
        for term in q:
            f = tf.get(term, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        scores.append(s)
    return scores
