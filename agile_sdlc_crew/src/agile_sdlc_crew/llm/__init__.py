"""LLM provider registry, profile resolver ve agent baglama katmani.

Mimari:
    Provider (registry.py)  : isimle kayit edilen LLM factory'leri
                              (litellm, anthropic, claude_cli, ollama, lmstudio)
    Profile  (llm_profiles.yaml)
                            : isimli LLM bundle'lari (provider + model + max_tokens)
    Resolver (resolver.py)  : agent_key -> profile_name -> LLM instance
                              (env override > agents.yaml > default mapping)
"""

from agile_sdlc_crew.llm.registry import build_llm, list_providers, register
from agile_sdlc_crew.llm.resolver import (
    build_for_agent,
    resolve_profile_name,
    resolve_spec_with_source,
    resolve_with_source,
    set_agent_override,
)

__all__ = [
    "build_llm",
    "build_for_agent",
    "list_providers",
    "register",
    "resolve_profile_name",
    "resolve_spec_with_source",
    "resolve_with_source",
    "set_agent_override",
]
