"""Claude CLI uzerinden LLM cagrisi — API key gerektirmez, OAuth session kullanir.

subprocess ile `claude -p "<prompt>"` calistirir. CrewAI LLM sinifi litellm
uzerinden calistigindan, bunu litellm custom provider olarak degil, dogrudan
completion fonksiyonu olarak kullaniyoruz.

CANLI GORUNURLUK: CREW_CLAUDE_CLI_STREAM=1 (default) iken `--output-format
stream-json --verbose` ile calisir; Claude'un her aksiyonu (tool cagrisi,
dusunme, ara yanit) olustugu anda pipeline log'una yazilir. =0 yapilirsa eski
capture_output (sessiz, tek seferde) davranisina doner.
"""

import json
import logging
import os
import subprocess
import threading

log = logging.getLogger("pipeline")


def _brief_input(inp: dict) -> str:
    """Tool input'undan kisa, okunur bir ozet cikar."""
    if not isinstance(inp, dict):
        return str(inp)[:60]
    for k in ("file_path", "path", "command", "pattern", "query", "url", "prompt"):
        if k in inp and inp[k]:
            return f"{k}={str(inp[k])[:60]}"
    return ",".join(list(inp.keys())[:3])


def _log_stream_event(ev: dict, text_parts: list) -> str | None:
    """Tek stream-json event'ini canli logla. result event'inde final metni dondur."""
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        model = ev.get("model", "?")
        ntools = len(ev.get("tools", []) or [])
        log.info(f"  🤖 claude basladi: model={model}, {ntools} tool")
        return None
    if t == "assistant":
        for b in ev.get("message", {}).get("content", []) or []:
            bt = b.get("type")
            if bt == "text":
                txt = b.get("text", "") or ""
                text_parts.append(txt)
                snip = " ".join(txt.split())[:100]
                if snip:
                    log.info(f"  💬 {snip}")
            elif bt == "tool_use":
                log.info(f"  🔧 {b.get('name', '?')}({_brief_input(b.get('input', {}))})")
            elif bt == "thinking":
                log.info("  🤔 düşünüyor…")
        return None
    if t == "user":
        for b in ev.get("message", {}).get("content", []) or []:
            if b.get("type") == "tool_result":
                content = b.get("content", "")
                n = len(content) if isinstance(content, str) else len(str(content))
                log.info(f"  ↳ tool sonucu ({n} char)")
        return None
    if t == "result":
        if ev.get("is_error"):
            log.warning(f"  ⚠️ claude hata: {str(ev.get('result', ''))[:160]}")
        dur = int(ev.get("duration_ms", 0) or 0)
        turns = ev.get("num_turns", "?")
        cost = ev.get("total_cost_usd")
        cost_s = f", ${cost:.4f}" if isinstance(cost, (int, float)) else ""
        log.info(f"  ✓ claude bitti ({turns} tur, {dur}ms{cost_s})")
        return ev.get("result", "") or ""
    return None


def _run_streaming(cmd: list, env: dict, timeout_s: int) -> str:
    """stream-json modunda calistir, event'leri canli logla, final metni dondur.
    Timeout watchdog thread ile uygulanir (kill → pipe EOF → dongu biter)."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, stdin=subprocess.DEVNULL, env=env, bufsize=1,
    )
    timed_out = {"v": False}
    done = threading.Event()

    def _watchdog():
        if not done.wait(timeout_s):
            timed_out["v"] = True
            try:
                proc.kill()
            except Exception:
                pass

    wd = threading.Thread(target=_watchdog, daemon=True)
    wd.start()

    final_text: str | None = None
    text_parts: list = []
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            res = _log_stream_event(ev, text_parts)
            if res is not None:
                final_text = res
    finally:
        done.set()
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    if timed_out["v"]:
        log.warning(f"  Claude CLI timeout ({timeout_s}s)")
    if final_text is not None:
        return final_text
    return "".join(text_parts).strip()


def claude_cli_completion(
    prompt: str, max_tokens: int = 4096, model: str = "", system: str = ""
) -> str:
    """Claude CLI ile tek prompt calistir, sonucu string olarak dondur.

    system: agent personasi. Verilirse `--system-prompt` ile gercek system
    prompt olarak gecirilir; boylece Claude CLI kendi default kimligi
    ("You are Claude Code...") yerine bizim persona gibi davranir.

    Timeout pipeline_config'dan okunur (CREW_CLAUDE_CLI_TIMEOUT, default 300s)."""
    try:
        from agile_sdlc_crew import pipeline_config as _pc
        timeout_s = int(_pc.get("CREW_CLAUDE_CLI_TIMEOUT"))
    except Exception:
        timeout_s = int(os.environ.get("CREW_CLAUDE_CLI_TIMEOUT", "300"))

    cmd = ["claude", "-p", prompt]
    if system:
        cmd.extend(["--system-prompt", system])
    if model:
        cmd.extend(["--model", model])
    env = {**os.environ, "CLAUDE_CODE_ENTRYPOINT": "cli"}

    stream = os.environ.get("CREW_CLAUDE_CLI_STREAM", "1") != "0"
    if stream:
        try:
            return _run_streaming(
                cmd + ["--output-format", "stream-json", "--verbose"], env, timeout_s
            )
        except FileNotFoundError:
            log.warning("  Claude CLI bulunamadi (PATH'te 'claude' yok)")
            return ""
        except Exception as e:
            log.warning(f"  Claude CLI stream hatasi ({e}), capture moduna donuluyor")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, env=env,
        )
        if result.returncode != 0:
            log.warning(f"  Claude CLI hata: {result.stderr.strip()[:200]}")
            return ""
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        log.warning(f"  Claude CLI timeout ({timeout_s}s)")
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
