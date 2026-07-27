"""Claude CLI provider — `claude -p ...` subprocess uzerinden, OAuth session ile.

litellm.custom_provider_map'a tek seferlik kaydedilir; sonraki cagrilar
ayni handler'i kullanir. API key gerektirmez."""

import logging

from crewai import LLM

NAME = "claude_cli"
_LITELLM_PROVIDER_NAME = "claude-cli"  # litellm tarafinda gorunen ad
_registered = False

# Claude CLI OAuth session uzerinden calisir, credential gerektirmez.
CREDS_SCHEMA: list[dict] = []

log = logging.getLogger("pipeline")


def _register_litellm_handler() -> None:
    global _registered
    if _registered:
        return

    import litellm

    from agile_sdlc_crew.tools.claude_cli_llm import claude_cli_completion

    class ClaudeCLIHandler(litellm.CustomLLM):
        def completion(self, model, messages, **kwargs):
            # system role'unu AYRI topla — `--system-prompt` ile gercek system
            # prompt olarak gecirilecek. User metnine gomulurse (eski davranis)
            # Claude CLI kendi default kimligini koruyup persona'yi "yapistirilmis
            # metin" sanyor ve rolu kiriyor (BA/Architect yerine meta-yorum).
            system_parts = []
            prompt_parts = []
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                if role == "system":
                    system_parts.append(content)
                elif role == "assistant":
                    prompt_parts.append(f"[Assistant]: {content}")
                else:
                    prompt_parts.append(content)
            prompt = "\n\n".join(prompt_parts)
            system = "\n\n".join(p for p in system_parts if p)

            # litellm, custom provider handler'ina model'i PREFIX'I SOYARAK
            # gecirir (get_llm_provider: model = model.split("/", 1)[1]), yani
            # burada 'claude-cli/sonnet' degil 'sonnet' gelir. Eski kod prefix
            # yoksa "" donuyordu → `--model` bayragi HIC eklenmiyor, `claude -p`
            # kendi default modelini (opus) kullaniyordu. Sonuc: agents.yaml /
            # agent_llm_overrides.yaml'daki sonnet/haiku ayarlari SESSIZCE
            # yok sayiliyor, her agent opus'ta kosuyordu (job #179: sonnet
            # ayarli business_analyst'in cagrisi claude-opus-5[1m] olarak kayitli,
            # 379 token'lik repo secimi $0.55). Prefix varsa soy, yoksa oldugu
            # gibi kullan.
            cli_model = model.split("/", 1)[1] if "/" in model else model
            max_tokens = kwargs.get("max_tokens", 4096)
            result = claude_cli_completion(
                prompt, max_tokens=max_tokens, model=cli_model, system=system
            )

            from litellm import Choices, Message, ModelResponse, Usage
            return ModelResponse(
                choices=[Choices(
                    message=Message(role="assistant", content=result),
                    index=0,
                    finish_reason="stop",
                )],
                model=model,
                usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            )

    handler = ClaudeCLIHandler()
    existing = list(litellm.custom_provider_map or [])
    if not any(p.get("provider") == _LITELLM_PROVIDER_NAME for p in existing):
        existing.append({"provider": _LITELLM_PROVIDER_NAME, "custom_handler": handler})
    litellm.custom_provider_map = existing
    _registered = True


def build(model: str, max_tokens: int = 4096, **kwargs) -> LLM:
    """model: 'sonnet' / 'opus' / 'haiku' veya 'claude-cli/<id>'."""
    _register_litellm_handler()
    if not model.startswith(f"{_LITELLM_PROVIDER_NAME}/"):
        model = f"{_LITELLM_PROVIDER_NAME}/{model}"
    return LLM(model=model, max_tokens=max_tokens)
