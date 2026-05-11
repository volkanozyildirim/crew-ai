"""Vision (image-to-text) provider registry — WI description'undaki gorselleri
textual aciklamaya cevirir.

LLM/embed paketlerinin paralel mimarisi:
    Provider (registry.py)  : isimle kayitli analyze factory'leri
                              (ollama_vision, openai_vision)
    Resolver (resolver.py)  : config'ten provider/model/base_url/api_key cozumler
"""

from agile_sdlc_crew.vision.registry import (
    analyze_image,
    list_providers,
    register,
)
from agile_sdlc_crew.vision.resolver import (
    KNOWN_VISION_MODELS,
    get_api_key,
    get_base_url,
    get_model,
    get_provider,
    load_config,
    save_config,
)

__all__ = [
    "analyze_image",
    "get_api_key",
    "get_base_url",
    "get_model",
    "get_provider",
    "list_providers",
    "load_config",
    "register",
    "save_config",
    "KNOWN_VISION_MODELS",
]
