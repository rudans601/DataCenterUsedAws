"""OpenAI 키가 설정되면 OpenAI를 사용하고, 그 외에는 기존 Azure 설정을 사용한다."""
from __future__ import annotations

from functools import lru_cache

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings, ChatOpenAI, OpenAIEmbeddings

from .config import settings

_SCOPE = "https://cognitiveservices.azure.com/.default"
_OPENAI_API_BASE = "https://api.openai.com/v1"


def _azure_auth_kwargs() -> dict:
    if settings.aoai_api_key:
        return {"api_key": settings.aoai_api_key}
    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return {"azure_ad_token_provider": get_bearer_token_provider(credential, _SCOPE)}


def is_configured() -> bool:
    return bool(settings.openai_api_key or settings.aoai_endpoint)


@lru_cache(maxsize=1)
def chat_model() -> ChatOpenAI | AzureChatOpenAI:
    if settings.openai_api_key:
        return ChatOpenAI(
            api_key=settings.openai_api_key,
            base_url=_OPENAI_API_BASE,
            model=settings.openai_chat_model,
            temperature=0,
            max_retries=2,
            timeout=90,
        )
    return AzureChatOpenAI(
        azure_endpoint=settings.aoai_endpoint,
        api_version=settings.aoai_api_version,
        azure_deployment=settings.chat_deployment,
        temperature=0,
        max_retries=2,
        timeout=90,
        **_azure_auth_kwargs(),
    )


@lru_cache(maxsize=1)
def embeddings() -> OpenAIEmbeddings:
    if settings.openai_api_key:
        return OpenAIEmbeddings(
            api_key=settings.openai_api_key,
            base_url=_OPENAI_API_BASE,
            model=settings.openai_embedding_model,
        )
    return AzureOpenAIEmbeddings(
        azure_endpoint=settings.aoai_endpoint,
        api_version=settings.aoai_api_version,
        azure_deployment=settings.embed_deployment,
        **_azure_auth_kwargs(),
    )
