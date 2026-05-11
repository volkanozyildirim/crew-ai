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
            prompt_parts = []
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                if role == "system":
                    prompt_parts.append(f"[System]: {content}")
                elif role == "assistant":
                    prompt_parts.append(f"[Assistant]: {content}")
                else:
                    prompt_parts.append(content)
            prompt = "\n\n".join(prompt_parts)

            cli_model = model.split("/", 1)[1] if "/" in model else ""
            max_tokens = kwargs.get("max_tokens", 4096)
            result = claude_cli_completion(prompt, max_tokens=max_tokens, model=cli_model)

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
