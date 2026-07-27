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
import signal
import subprocess
import threading
import time
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


# ── Tool'suz (emit) mod ──────────────────────────────────────────────────
# clear_repo_ctx() sadece --add-dir/--max-budget-usd'yi kaldirir; ama claude -p
# varsayilan Bash/Read araclariyla home dizinine (~/.crew_repos) yine erisip
# repoyu okuyabiliyor. Gercekten tool'suz bir "emit" cagrisi icin bu bayrak
# --disallowedTools ile kesif/dosya araclarini KAPATIR → model mecburen
# context'ten cevap uretir (architect JSON planini yazmak zorunda kalir).
# Kesif→emit iki-fazli architect akisinda emit fazi bunu kullanir.
_TOOLLESS_DENY = "Bash,Read,Grep,Glob,LS,Edit,Write,WebFetch,WebSearch,Task,NotebookEdit,MultiEdit"


def _get_toolless() -> bool:
    return bool(getattr(_cli_ctx, "toolless", False))


def set_toolless(on: bool = True) -> None:
    """Bu thread'deki sonraki claude_cli cagrilari kesif/dosya araclarini
    KULLANAMASIN (--disallowedTools). clear ile kapatilmali (try/finally)."""
    _cli_ctx.toolless = bool(on)


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


def _kill_proc_tree(proc) -> None:
    """claude -p alt-surec AGACINI oldur. claude parent'i kill etmek yetmiyor:
    Node/engine cocuk surecleri stdout pipe'ini acik tutup okuma dongusunu
    kilitliyordu (23 dk'lik hang'in sebebi — timeout kesemiyordu). Kendi
    process-group'unda (start_new_session) baslatip TUM grubu SIGKILL'liyoruz."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_streaming(cmd: list, env: dict, timeout_s: int, meta: dict | None = None,
                   idle_s: int = 0) -> str:
    """stream-json modunda calistir, event'leri canli logla, final metni dondur.
    Iki watchdog (sure asilinca TUM process-group SIGKILL → pipe EOF → dongu biter):
      • toplam-omur: timeout_s (hard limit).
      • idle: idle_s>0 ise, event-arasi sessizlik idle_s'yi asarsa oldur. Gerekce:
        claude -p bazen aginda sessizce (HIC stream event uretmeden) dakikalarca
        takiliyor ve kendi ic retry'si bizim toplam-omur watchdog'undan HEMEN once
        'gracefully' bitip SIGKILL'e firsat vermiyordu (job 169: 283s tam sessizlik).
        Idle watchdog bu stall'i ~idle_s icinde yakalar.
    meta verilirse cost/turns/tool_calls/model oraya toplanir."""
    if meta is None:
        meta = {}
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, stdin=subprocess.DEVNULL, env=env, bufsize=1,
        start_new_session=True,  # kendi process-group'u → tum agac oldurulebilir
    )
    done = threading.Event()
    start = time.monotonic()
    last = {"t": start}
    reason = {"v": None}

    def _watchdog():
        poll = min(5, idle_s) if idle_s > 0 else timeout_s
        while not done.wait(poll):
            now = time.monotonic()
            if now - start >= timeout_s:
                reason["v"] = f"toplam-sure {timeout_s}s"
                break
            if idle_s > 0 and (now - last["t"]) >= idle_s:
                reason["v"] = f"idle {idle_s}s (event yok)"
                break
        if reason["v"] is not None:
            log.warning(f"  ⏱️ Hard timeout ({reason['v']}) — claude surec grubu SIGKILL")
            _kill_proc_tree(proc)

    wd = threading.Thread(target=_watchdog, daemon=True)
    wd.start()

    final_text: str | None = None
    text_parts: list = []
    try:
        for line in proc.stdout:
            last["t"] = time.monotonic()  # idle watchdog icin: her satirda tazele
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
            _kill_proc_tree(proc)

    if reason["v"] is not None:
        log.warning(f"  Claude CLI timeout ({reason['v']})")
    # Kesik/hatali/timeout sonuc BOS donebilir (result event is_error → "").
    # Bos donmek CrewAI'da "Invalid response - None or empty" → retry firtinasi
    # tetikliyor. Bunun yerine stream sirasinda biriken assistant metnini kurtar:
    # kesilen kesfin bulgulari (ve varsa yazilmis JSON) bir sonraki faza tasinsin.
    if final_text is not None and final_text.strip():
        return final_text
    salvaged = "".join(text_parts).strip()
    if salvaged:
        log.info(f"  ♻️ Bos/kesik sonuc — {len(salvaged)} char stream metni kurtarildi")
        return salvaged
    return final_text or ""


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
        idle_s = int(_pc.get("CREW_CLAUDE_CLI_IDLE_TIMEOUT") or 0)
    except Exception:
        timeout_s = int(os.environ.get("CREW_CLAUDE_CLI_TIMEOUT", "300"))
        idle_s = int(os.environ.get("CREW_CLAUDE_CLI_IDLE_TIMEOUT", "0") or 0)

    # ── FAZ-FARKINDALI TIMEOUT ────────────────────────────────────────────
    # Sabit idle timeout, uzun dusunen modelle bagdasmyor: Opus 5'te
    # thinking.display varsayilani "omitted" → model dusunurken stream'e event
    # GITMIYOR. Watchdog bunu "hang" saniyor. Job #179: 13.5 dakikalik verimli
    # kesif (StockSource/Registry/INTERNAL_MERCHANT_IDS/Merchant/migration
    # taramasi tamamlanmis) tam dusunme aninda SIGKILL edildi:
    #   ⏱️ Hard timeout (idle 90s (event yok)) — claude surec grubu SIGKILL
    #   Faz A kesif hatasi (None or empty) — bulgusuz devam
    # Sonuc sadece sure kaybi degil KALITE kaybi: plan bulgusuz uretildi →
    # completeness gate AC5/AC6/FR3 bosluğu buldu → $1.27/280s amend → hala
    # TR3 eksik. Tek config degeri hem 13.5 dk hem ~$1.5 hem kalite bosluğu.
    #
    # Watchdog KALDIRILMIYOR (gercek hang'ler icin gerekli) — faza gore
    # kalibre ediliyor. Faz, cagrinin kendi baglamindan tespit edilir:
    #   repo-aracli (--add-dir) = kesif/implement → uzun dusunme NORMAL
    #   tool'suz emit           = JSON yazmali    → hizli olmali
    #   haiku denetci           = zaten hizli
    _phase_add_dirs, _ = _get_repo_ctx()
    if _phase_add_dirs:
        _phase, _p_idle, _p_hard = "repo-araclı", 240, 900
    elif "haiku" in (model or "").lower():
        _phase, _p_idle, _p_hard = "denetci", 60, 120
    elif _get_toolless():
        _phase, _p_idle, _p_hard = "tool'suz emit", 90, 300
    else:
        _phase, _p_idle, _p_hard = "", 0, 0
    if _p_idle:
        # Dashboard'dan gelen deger daha comertse ONA saygi duy (kullanici
        # bilerek yukseltmis olabilir); yalnizca cok siki olani gevset.
        _new_idle = max(idle_s, _p_idle) if idle_s else _p_idle
        _new_hard = max(timeout_s, _p_hard)
        if (_new_idle, _new_hard) != (idle_s, timeout_s):
            log.info(
                f"  ⏳ Faz '{_phase}': idle {idle_s}s→{_new_idle}s, "
                f"hard {timeout_s}s→{_new_hard}s"
            )
        idle_s, timeout_s = _new_idle, _new_hard

    cmd = ["claude", "-p", prompt]
    if system:
        cmd.extend(["--system-prompt", system])
    if model:
        cmd.extend(["--model", model])

    # Efor + advisor: claude -p, kullanicinin ~/.claude/settings.json ayarlarini
    # okur (effortLevel, advisorModel). Otomatik pipeline cagrilarinda global
    # 'high/xhigh' efor + 'fable' advisor DEVRALINMASIN — her cagriyi cok
    # yavaslatir/pahalastirir (23dk hang + asiri thinking sebeplerinden).
    #   --effort: dusuk efora zorla. NOT: haiku efor DESTEKLEMEZ → sadece efor
    #     destekli modellerde (opus/sonnet/…) ekle.
    #   --settings: advisorModel'i bosalt → bu cagrilar advisor'a danismasin.
    try:
        from agile_sdlc_crew import pipeline_config as _pc_cli2
        _effort = str(_pc_cli2.get("CREW_CLI_EFFORT") or "").strip().lower()
        _arch_effort = str(_pc_cli2.get("CREW_CLI_EFFORT_ARCHITECT") or "").strip().lower()
        _disable_adv = bool(_pc_cli2.get("CREW_CLI_DISABLE_ADVISOR"))
    except Exception:
        _effort, _arch_effort, _disable_adv = "low", "high", True
    # Yazilim mimari en kritik agent (plani o uretiyor) → daha YUKSEK efor.
    # Diger agent'lar (BA/reviewer/kickoff persona/haiku-denetci) baseline'da (low)
    # kalir. Agent, call-context'ten okunur (_step_start → TASK_AGENTS eslemesi;
    # kickoff persona'lari crew.py set_call_agent ile).
    _agent = (_get_call_context()[2] or "")
    if _agent == "software_architect" and _arch_effort:
        _effort = _arch_effort
    _model_l = (model or "").lower()
    if _effort in ("low", "medium", "high", "xhigh", "max") and "haiku" not in _model_l:
        cmd.extend(["--effort", _effort])
    if _disable_adv:
        cmd.extend(["--settings", '{"advisorModel":""}'])

    # Repo-tool baglami varsa: klonlanmis repoyu ve yerel arac iznini gecir.
    # Boylece bu cagri gercek repoyu kesfedebilir (halusinasyon yerine).
    add_dirs, allowed_tools = _get_repo_ctx()
    for d in add_dirs:
        cmd.extend(["--add-dir", d])
    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])
    # Tool'suz (emit) mod: --add-dir YOK ama claude'un varsayilan Bash/Read'i
    # home'a (~/.crew_repos) erisip repoyu yine okuyabiliyor. --disallowedTools
    # ile kesif/dosya araclarini kapat → model context'ten cevap uretmek
    # zorunda (architect emit fazi). Sadece repo-tool'u OLMAYAN cagrilarda.
    if _get_toolless() and not add_dirs:
        cmd.extend(["--disallowedTools", _TOOLLESS_DENY])
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
                cmd + ["--output-format", "stream-json", "--verbose"], env, timeout_s, meta,
                idle_s=idle_s,
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
