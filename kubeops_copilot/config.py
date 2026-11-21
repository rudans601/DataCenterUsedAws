"""환경 설정과 운영 정책(허용 범위)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _path(value: str) -> str:
    p = Path(value)
    return str(p if p.is_absolute() else ROOT / p)


@dataclass(frozen=True)
class Settings:
    kubeconfig: str = _path(os.getenv("KUBEOPS_KUBECONFIG", ".kube/agent.kubeconfig"))
    namespace: str = os.getenv("KUBEOPS_NAMESPACE", "demo")
    prometheus_url: str = os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
    openai_api_key: str = field(default=os.getenv("OPENAI_API_KEY", "").strip(), repr=False)
    openai_chat_model: str = os.getenv("OPENAI_CHAT_MODEL", "").strip() or "gpt-4.1-mini"
    openai_embedding_model: str = os.getenv("OPENAI_EMBEDDING_MODEL", "").strip() or "text-embedding-3-small"
    aoai_endpoint: str = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    aoai_api_version: str = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    aoai_api_key: str = field(default=os.getenv("AZURE_OPENAI_API_KEY", ""), repr=False)
    chat_deployment: str = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4.1-mini")
    embed_deployment: str = os.getenv("AZURE_OPENAI_EMBED_DEPLOYMENT", "text-embedding-3-small")
    verify_seconds: int = int(os.getenv("KUBEOPS_VERIFY_SECONDS", "60"))
    # 검증에 성공한 변경을 운영자 검토용 후보 문서로 저장한다.
    save_incidents: bool = os.getenv("KUBEOPS_SAVE_INCIDENTS", "1") == "1"
    db_path: str = _path(os.getenv("KUBEOPS_DB", "data/kubeops.db"))
    knowledge_dir: Path = ROOT / "knowledge"
    index_path: Path = ROOT / "data" / "rag_index.json"

    @property
    def llm_provider(self) -> str:
        return "OpenAI" if self.openai_api_key else "Azure OpenAI"

    @property
    def llm_model(self) -> str:
        return self.openai_chat_model if self.openai_api_key else self.chat_deployment


@dataclass(frozen=True)
class Policy:
    """운영자가 승인해도 이 범위를 벗어나는 조치는 실행하지 않는다."""

    allowed_actions: frozenset[str] = frozenset({"restart", "rollback", "scale"})
    allowed_namespaces: frozenset[str] = frozenset({"demo"})
    allowed_deployments: frozenset[str] = frozenset({"api", "web"})
    min_replicas: int = 1
    max_replicas: int = 6
    descriptions: dict[str, str] = field(
        default_factory=lambda: {
            "restart": "Deployment 재시작 (Pod를 순차적으로 새로 띄움)",
            "rollback": "직전 리비전으로 롤백",
            "scale": "복제본(replicas) 수 조정",
        }
    )


settings = Settings()
policy = Policy()
