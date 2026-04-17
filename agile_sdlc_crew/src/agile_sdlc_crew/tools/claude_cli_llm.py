"""Claude CLI uzerinden LLM cagrisi — API key gerektirmez, OAuth session kullanir.

subprocess ile `claude -p "<prompt>" --no-input` calistirir.
CrewAI LLM sinifi litellm uzerinden calistigindan, bunu litellm custom provider
olarak degil, dogrudan completion fonksiyonu olarak kullaniyoruz.
"""

import json
import logging
import os
import subprocess

log = logging.getLogger("pipeline")


def claude_cli_completion(prompt: str, max_tokens: int = 4096, model: str = "") -> str:
    """Claude CLI ile tek prompt calistir, sonucu string olarak dondur."""
    cmd = ["claude", "-p", prompt]
    if model:
        cmd.extend(["--model", model])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "cli"},
        )
        if result.returncode != 0:
            err = result.stderr.strip()[:200]
            log.warning(f"  Claude CLI hata: {err}")
            return ""
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        log.warning("  Claude CLI timeout (120s)")
        return ""
    except FileNotFoundError:
        log.warning("  Claude CLI bulunamadi (PATH'te 'claude' yok)")
        return ""
    except Exception as e:
        log.warning(f"  Claude CLI hatasi: {e}")
        return ""


def is_claude_cli_available() -> bool:
    """Claude CLI kurulu ve calisabiliyor mu?"""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False
