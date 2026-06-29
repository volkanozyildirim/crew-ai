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
from contextlib import contextmanager

log = logging.getLogger("pipeline")

# ── Repo-tool baglami (Part B: architect'i "gor kil") ──────────────────
# claude_cli normalde duz metin LLM gibi cagrilir; CrewAI'in Python tool'lari
# (browse_repo/search_code) subprocess'e ulasmaz. Ama `claude -p` ZATEN Claude
# Code'dur — ona --add-dir ile klonlanmis repoyu ve --allowedTools ile yerel
# Read/Grep/Glob araclarini verirsek gercek kodu kesfeder. Bu thread-local,
# bir crew kickoff'u oncesinde set edilir; o pencerede claude cagrilarina
# --add-dir/--allowedTools eklenir.
_cli_ctx = threading.local()


def _get_repo_ctx() -> tuple[list, str]:
    return getattr(_cli_ctx, "add_dirs", []), getattr(_cli_ctx, "allowed_tools", "")


def set_repo_ctx(add_dirs: list, allowed_tools: str = "Read,Grep,Glob,LS") -> None:
    """Bu thread'deki sonraki claude_cli cagrilari verilen repolari --add-dir
    ile gorsun. clear_repo_ctx ile temizlenmeli (genelde try/finally)."""
    _cli_ctx.add_dirs = [str(d) for d in (add_dirs or [])]
    _cli_ctx.allowed_tools = allowed_tools or ""


def clear_repo_ctx() -> None:
    _cli_ctx.add_dirs = []
    _cli_ctx.allowed_tools = ""


@contextmanager
def repo_tools_context(add_dirs: list, allowed_tools: str = "Read,Grep,Glob,LS"):
    """Bu blok icindeki claude_cli cagrilari verilen repolari --add-dir ile
    gorur ve allowed_tools'u kullanabilir. Thread-local — ic ice/paralel
    kickoff'larda yalniz set eden thread'i etkiler."""
    prev = _get_repo_ctx()
    _cli_ctx.add_dirs = [str(d) for d in (add_dirs or [])]
    _cli_ctx.allowed_tools = allowed_tools or ""
    try:
        yield
    finally:
        _cli_ctx.add_dirs, _cli_ctx.allowed_tools = prev


# ── Cagri muhasebesi (per-job/per-agent maliyet & arac sayimi) ──────────
# Flow her crew kickoff'undan once set_call_context ile (job_id, step_key,
# agent) baglar; her claude -p cagrisi tamamlaninca olculen cost/turns/tool
# sayisi kayitli sink'e gonderilir (db.record_llm_call). Thread-local —
# paralel step'lerde izole.
_acct_ctx = threading.local()
_call_sink = None


def set_call_context(job_id=None, step_key: str = "", agent: str = "") -> None:
    _acct_ctx.job_id = job_id
    _acct_ctx.step_key = step_key or ""
    _acct_ctx.agent = agent or ""


def clear_call_context() -> None:
    _acct_ctx.job_id = None
    _acct_ctx.step_key = ""
    _acct_ctx.agent = ""


def set_call_agent(agent: str) -> None:
    """Sadece agent alanini guncelle (job_id/step_key korunur). Kickoff gibi
    cok-personali adimlarda her persona icin ayri atif yapmaya yarar."""
    _acct_ctx.agent = agent or ""


def _get_call_context() -> tuple:
    return (getattr(_acct_ctx, "job_id", None),
            getattr(_acct_ctx, "step_key", ""),
            getattr(_acct_ctx, "agent", ""))


def register_call_sink(fn) -> None:
    """db.record_llm_call gibi bir alici bagla; her cagri sonrasi cagrilir."""
    global _call_sink
    _call_sink = fn


# Mid-step budget cap: sink limit asildigini gorunce bayragi set eder;
# sonraki claude cagrilari kisa-devre yapip bos doner (kickoff gibi tek
# adimda 30+ cagri yapilan yerde asimi ~1 cagriyla sinirlar).
_budget_exceeded = False


def signal_budget_exceeded() -> None:
    global _budget_exceeded
    _budget_exceeded = True


def reset_budget_flag() -> None:
    global _budget_exceeded
    _budget_exceeded = False


def is_budget_exceeded() -> bool:
    return _budget_exceeded


def _emit_call_record(model: str, meta: dict) -> None:
    if _call_sink is None:
        return
    job_id, step_key, agent = _get_call_context()
    try:
        _call_sink({
            "job_id": job_id,
            "step_key": step_key,
            "agent": agent,
            "model": (meta.get("model") or model or "")[:60],
            "provider": "claude_cli",
            "turns": int(meta.get("turns") or 0),
            "tool_calls": int(meta.get("tool_calls") or 0),
            "cost_usd": float(meta.get("cost_usd") or 0.0),
            "duration_ms": int(meta.get("duration_ms") or 0),
            "input_tokens": int(meta.get("input_tokens") or 0),
            "output_tokens": int(meta.get("output_tokens") or 0),
            "cache_read_tokens": int(meta.get("cache_read_tokens") or 0),
            "cache_creation_tokens": int(meta.get("cache_creation_tokens") or 0),
        })
    except Exception:
        pass


def _brief_input(inp: dict) -> str:
    """Tool input'undan kisa, okunur bir ozet cikar."""
    if not isinstance(inp, dict):
        return str(inp)[:60]
    for k in ("file_path", "path", "command", "pattern", "query", "url", "prompt"):
        if k in inp and inp[k]:
            return f"{k}={str(inp[k])[:60]}"
    return ",".join(list(inp.keys())[:3])


def _log_stream_event(ev: dict, text_parts: list, meta: dict | None = None) -> str | None:
    """Tek stream-json event'ini canli logla. result event'inde final metni dondur.
    meta verilirse model/turns/cost/duration/tool_calls oraya toplanir."""
    if meta is None:
        meta = {}
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        model = ev.get("model", "?")
        meta["model"] = model
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
                meta["tool_calls"] = meta.get("tool_calls", 0) + 1
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
        meta["duration_ms"] = dur
        if isinstance(turns, (int, float)):
            meta["turns"] = int(turns)
        if isinstance(cost, (int, float)):
            meta["cost_usd"] = float(cost)
        # Token kullanimi — claude -p result.usage bloğu
        u = ev.get("usage") or {}
        if isinstance(u, dict):
            meta["input_tokens"] = int(u.get("input_tokens", 0) or 0)
            meta["output_tokens"] = int(u.get("output_tokens", 0) or 0)
            meta["cache_read_tokens"] = int(u.get("cache_read_input_tokens", 0) or 0)
            meta["cache_creation_tokens"] = int(u.get("cache_creation_input_tokens", 0) or 0)
        cost_s = f", ${cost:.4f}" if isinstance(cost, (int, float)) else ""
        log.info(f"  ✓ claude bitti ({turns} tur, {dur}ms{cost_s})")
        return ev.get("result", "") or ""
    return None


def _run_streaming(cmd: list, env: dict, timeout_s: int, meta: dict | None = None) -> str:
    """stream-json modunda calistir, event'leri canli logla, final metni dondur.
    Timeout watchdog thread ile uygulanir (kill → pipe EOF → dongu biter).
    meta verilirse cost/turns/tool_calls/model oraya toplanir."""
    if meta is None:
        meta = {}
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
            res = _log_stream_event(ev, text_parts, meta)
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
    # Budget asildiysa daha fazla pahali cagri yapma — kisa-devre.
    if _budget_exceeded:
        log.warning("  ⛔ Budget asildi — claude cagrisi atlandi (kisa-devre)")
        return ""
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

    # Repo-tool baglami varsa: klonlanmis repoyu ve yerel arac iznini gecir.
    # Boylece bu cagri gercek repoyu kesfedebilir (halusinasyon yerine).
    add_dirs, allowed_tools = _get_repo_ctx()
    for d in add_dirs:
        cmd.extend(["--add-dir", d])
    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])
    # Repo-tool'lu cagrilar (architect/implement) otonom derin kesife dalip
    # 27-tur/$1.6 gibi sisebiliyor. Cagri-basi $ cap ile sinirla (hard limit;
    # claude --max-budget-usd sadece --print ile calisir). CREW_CLI_CALL_MAX_USD.
    if add_dirs:
        try:
            from agile_sdlc_crew import pipeline_config as _pc_cap
            _cap = float(_pc_cap.get("CREW_CLI_CALL_MAX_USD") or 0)
        except Exception:
            _cap = 0.0
        if _cap > 0:
            cmd.extend(["--max-budget-usd", str(_cap)])

    env = {**os.environ, "CLAUDE_CODE_ENTRYPOINT": "cli"}

    stream = os.environ.get("CREW_CLAUDE_CLI_STREAM", "1") != "0"
    if stream:
        meta: dict = {}
        try:
            text = _run_streaming(
                cmd + ["--output-format", "stream-json", "--verbose"], env, timeout_s, meta
            )
            _emit_call_record(model, meta)
            return text
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
