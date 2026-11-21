"""운영 문서·장애 사례 검색 (RAG).

knowledge/runbooks(런북)와 knowledge/incidents(장애 사례)의 Markdown을 문서 단위로 임베딩한다.
런북은 증상·원인·조치·검증이 한 묶음이라 잘게 쪼개면 조치 부분이 빠지기 쉬워서 통째로 쓴다.
검색은 런북과 장애 사례를 따로 뽑고, 일치하는 경고 이벤트가 명시된 런북을 먼저 찾고, 벡터 유사도와 관측 키워드를 함께 사용한다.
문서나 임베딩 설정이 바뀌면 다음 검색 때 자동으로 다시 색인한다.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import tempfile
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.vectorstores import InMemoryVectorStore

from .config import settings
from .llm import embeddings

_lock = threading.Lock()
_cache: dict = {"fingerprint": None, "store": None}


RUNBOOK_VERSION = "2.0"
ACTIVE_DIR = "v2"


def _files():
    active = settings.knowledge_dir / "runbooks" / ACTIVE_DIR
    incidents = (settings.knowledge_dir / "incidents").glob("*.md")
    return sorted(active.glob("*.md")) + sorted(p for p in incidents if not p.name.startswith("auto-"))


def corpus_revision() -> str:
    h = hashlib.sha256()
    for file in _files():
        h.update(str(file.relative_to(settings.knowledge_dir)).encode())
        h.update(file.read_bytes())
    return h.hexdigest()[:12]


def cited_procedures(doc, quote):
    """현행 문서의 실행 절차 문장 전체를 인용했을 때만 허용 범위를 반환한다."""
    return [tag for tag, sentence in re.findall(r"^\[(auto\.\w+|approval\.\w+)\] (.+)$", doc, re.M)
            if sentence.strip() in quote]


def _fingerprint() -> str:
    h = hashlib.sha256()
    if settings.openai_api_key:
        embedding_config = (settings.llm_provider, settings.openai_embedding_model)
    else:
        embedding_config = (settings.llm_provider, settings.aoai_endpoint, settings.embed_deployment)
    h.update(json.dumps(embedding_config).encode())
    h.update(corpus_revision().encode())
    return h.hexdigest()


def load_chunks() -> list[Document]:
    docs = []
    for path in _files():
        text = path.read_text(encoding="utf-8").strip()
        title_match = re.search(r"^# (.+)$", text, re.M)
        title = title_match.group(1).strip() if title_match else path.stem
        docs.append(Document(
            page_content=text,
            metadata={"source": "knowledge/" + path.relative_to(settings.knowledge_dir).as_posix(), "title": title, "section": title,
                      "kind": "runbooks" if path.parent.name == ACTIVE_DIR else "incidents",
                      "version": RUNBOOK_VERSION if path.parent.name == ACTIVE_DIR else "case"},
        ))
    return docs


def get_store(force: bool = False) -> InMemoryVectorStore:
    with _lock:
        fp = _fingerprint()
        if not force and _cache["store"] is not None and _cache["fingerprint"] == fp:
            return _cache["store"]
        base = settings.index_path
        path = base.with_name(f"{base.stem}-{fp[:16]}{base.suffix}")
        if not force and path.exists():
            store = InMemoryVectorStore.load(str(path), embeddings())
        else:
            store = InMemoryVectorStore(embeddings())
            store.add_documents(load_chunks())
            path.parent.mkdir(parents=True, exist_ok=True)
            # 웹 재색인과 실험 프로세스가 동시에 실행돼도 완성된 같은 버전의 색인만 읽는다.
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
                temp_path = Path(temp.name)
            try:
                store.dump(str(temp_path))
                temp_path.replace(path)
            finally:
                temp_path.unlink(missing_ok=True)
        _cache.update(fingerprint=fp, store=store)
        return store


def _keywords(signals: list[str]) -> set[str]:
    words = set()
    for s in signals:
        words.add(s.lower())
        words.update(w.lower() for w in re.split(r"\W+", s) if len(w) >= 5)
    return words


def search(query: str, signals: list[str] | None = None, k_runbooks: int = 3, k_incidents: int = 2,
           *, event_reasons: set[str] | None = None) -> list[dict]:
    store = get_store()
    keywords = _keywords(signals or [])
    observed_events = {reason.casefold() for reason in event_reasons or ()}

    def matched_events(doc):
        if doc.metadata["kind"] != "runbooks" or not observed_events:
            return []
        line = re.search(r"^이벤트: (.+)$", doc.page_content, re.M)
        names = line[1].split(",") if line else []
        return [name.strip() for name in names if name.strip().casefold() in observed_events]

    def ranked(kind: str, k: int):
        # 이벤트 일치는 유사도 상위 후보 밖의 런북에서도 확인한다.
        limit = len(store.store) if kind == "runbooks" and observed_events else max(k * 3, 6)
        hits = store.similarity_search_with_score(query, k=limit, filter=lambda d: d.metadata["kind"] == kind)
        scored = []
        for doc, score in hits:
            text = doc.page_content.lower()
            bonus = min(0.06, 0.015 * sum(1 for kw in keywords if kw in text))
            scored.append((doc, float(score) + bonus))
        return sorted(scored, key=lambda hit: (len(matched_events(hit[0])), hit[1]), reverse=True)[:k]

    # 공통 권한 문서와 증상별 절차를 원문 단위로 함께 전달한다.
    policy_doc = next(d for d in store.store.values()
                      if d["metadata"]["source"].endswith("/operations-policy.md"))
    policy = Document(page_content=policy_doc["text"], metadata=policy_doc["metadata"])
    procedures = [hit for hit in ranked("runbooks", k_runbooks + 1)
                  if hit[0].metadata["source"] != policy.metadata["source"]][:k_runbooks]
    # 공통 정책이 위임한 두 자동 절차를 빠짐없이 제공한다. 적용 여부는 관측값으로 판단한다.
    seen = {doc.metadata["source"] for doc, _ in procedures}
    for item in store.store.values():
        src = item["metadata"]["source"]
        if src not in seen and src.endswith(("/high-load-scaling.md", "/revision-recovery.md")):
            procedures.append((Document(page_content=item["text"], metadata=item["metadata"]), None))
    results = [(policy, None), *procedures, *ranked("incidents", k_incidents)]
    return [
        {
            "id": f"D{i + 1}",
            "title": doc.metadata["title"],
            "section": doc.metadata["section"],
            "source": doc.metadata["source"],
            "kind": doc.metadata["kind"],
            "score": round(score, 3) if score is not None else None,
            "matched_events": matched_events(doc),
            "version": doc.metadata["version"],
            "content": doc.page_content,
        }
        for i, (doc, score) in enumerate(results)
    ]
