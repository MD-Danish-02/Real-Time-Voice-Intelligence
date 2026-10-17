"""
Knowledge Engine: turns uploaded documents / website text / FAQs into
retrievable chunks in pgvector. Two embedding backends are supported:

- "local" (default): a deterministic, dependency-free hashing embedder.
  It is a REAL, working embedding function (character n-gram hashing +
  L2 normalization, similar in spirit to feature hashing / random
  projection) - not a mock that returns zeros - so retrieval genuinely
  works end-to-end in this sandbox with no model download required.
- "sentence_transformers": swaps in a real pretrained embedding model
  (all-MiniLM-L6-v2, 384-dim) for materially better retrieval quality in
  production. Requires the `sentence-transformers` package + model
  download, so it is opt-in via KNOWLEDGE_EMBEDDING_MODE.
"""
import hashlib
import json
import math
import re
import uuid

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.enums import KnowledgeStatus
from app.models.knowledge import EMBEDDING_DIM, KnowledgeChunk, KnowledgeDocument
from app.security.ssrf import UnsafeOutboundURL, validate_public_url

_WORD_RE = re.compile(r"[a-zA-Z0-9']+")
_sentence_transformer_model = None


def _local_hash_embedding(text_value: str) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    tokens = _WORD_RE.findall(text_value.lower())
    for token in tokens:
        for n in (2, 3):
            for i in range(max(len(token) - n + 1, 1)):
                gram = token[i:i + n]
                digest = hashlib.md5(gram.encode()).digest()
                index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                vector[index] += sign
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def _sentence_transformer_embedding(text_value: str) -> list[float]:
    global _sentence_transformer_model
    if _sentence_transformer_model is None:
        from sentence_transformers import SentenceTransformer  # heavy, optional dependency
        _sentence_transformer_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _sentence_transformer_model.encode(text_value, normalize_embeddings=True).tolist()


def embed_text(text_value: str) -> list[float]:
    if settings.KNOWLEDGE_EMBEDDING_MODE == "sentence_transformers":
        return _sentence_transformer_embedding(text_value)
    return _local_hash_embedding(text_value)


def chunk_text(raw_text: str, *, chunk_size: int = 800, overlap: int = 120) -> list[str]:
    """Simple sliding-window chunker on whitespace-normalized text."""
    normalized = re.sub(r"\s+", " ", raw_text or "").strip()
    if not normalized:
        return []
    chunks = []
    start = 0
    while start < len(normalized):
        end = min(start + chunk_size, len(normalized))
        chunks.append(normalized[start:end])
        if end == len(normalized):
            break
        start = end - overlap
    return chunks


async def ingest_document(
    db: AsyncSession, tenant_id: uuid.UUID, document: KnowledgeDocument, raw_text: str,
) -> KnowledgeDocument:
    document.status = KnowledgeStatus.PROCESSING.value
    await db.commit()

    try:
        # remove any previous chunks (reprocess case)
        await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))

        chunks = chunk_text(raw_text)
        for index, chunk_content in enumerate(chunks):
            embedding = embed_text(chunk_content)
            db.add(KnowledgeChunk(
                tenant_id=tenant_id, document_id=document.id, chunk_index=index,
                content=chunk_content, metadata_json=json.dumps({"title": document.title}),
                embedding=embedding,
            ))
        document.chunk_count = len(chunks)
        document.status = KnowledgeStatus.PROCESSED.value
        document.error = None
    except Exception as exc:  # noqa: BLE001
        document.status = KnowledgeStatus.FAILED.value
        document.error = str(exc)[:2000]

    await db.commit()
    await db.refresh(document)
    return document


async def search_knowledge(
    db: AsyncSession, tenant_id: uuid.UUID, query: str, *, top_k: int = 5, similarity_threshold: float = 0.15,
) -> list[dict]:
    """
    Cosine-similarity top-k search scoped strictly to the tenant. pgvector's
    `<=>` operator computes cosine distance; similarity = 1 - distance.
    """
    query_embedding = embed_text(query)
    embedding_literal = "[" + ",".join(f"{v:.6f}" for v in query_embedding) + "]"

    result = await db.execute(
        text(
            """
            SELECT kc.content, kc.metadata_json, kc.document_id, kd.title,
                   1 - (kc.embedding <=> :embedding) AS similarity
            FROM knowledge_chunks kc
            JOIN knowledge_documents kd ON kd.id = kc.document_id
            WHERE kc.tenant_id = :tenant_id
            ORDER BY kc.embedding <=> :embedding
            LIMIT :top_k
            """
        ),
        {"embedding": embedding_literal, "tenant_id": str(tenant_id), "top_k": top_k},
    )
    rows = result.mappings().all()
    return [
        {"content": row["content"], "score": float(row["similarity"]), "document_title": row["title"],
         "document_id": row["document_id"]}
        for row in rows if float(row["similarity"]) >= similarity_threshold
    ]


async def ingest_website(db: AsyncSession, tenant_id: uuid.UUID, document: KnowledgeDocument, url: str) -> KnowledgeDocument:
    """
    Fetches and ingests a website's text content, with SSRF protections:
    only http(s), no redirects to private/internal IP ranges, size and
    timeout caps.
    """
    import httpx
    try:
        await validate_public_url(url)
    except UnsafeOutboundURL as exc:
        document.status = KnowledgeStatus.FAILED.value
        document.error = str(exc)
        await db.commit()
        return document

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False, max_redirects=0, trust_env=False) as client:
            resp = await client.get(url, headers={"User-Agent": "VoxAI-KnowledgeBot/1.0"})
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}")
            content_type = resp.headers.get("content-type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                raise RuntimeError(f"Unsupported content-type: {content_type}")
            raw_bytes = resp.content[:2_000_000]  # 2MB cap
            html = raw_bytes.decode(errors="ignore")
            text_only = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
            text_only = re.sub(r"<[^>]+>", " ", text_only)
            document.raw_text = text_only
            return await ingest_document(db, tenant_id, document, text_only)
    except Exception as exc:  # noqa: BLE001
        document.status = KnowledgeStatus.FAILED.value
        document.error = str(exc)[:2000]
        await db.commit()
        return document
