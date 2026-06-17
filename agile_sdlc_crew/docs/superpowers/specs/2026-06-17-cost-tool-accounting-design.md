# Per-Job / Per-Agent Maliyet & Araç-Çağrı Muhasebesi — Tasarım

**Tarih:** 2026-06-17
**Amaç:** Her iş ve her agent için LLM maliyetini, tur sayısını ve araç (tool)
çağrı sayısını hesaplayıp kalıcı tut; dashboard/API'de göster; budget guard'ı
gerçek maliyetle besle.

## Mevcut durum
- Gerçek maliyet `claude_cli_llm.py`'de zaten parse ediliyor (`total_cost_usd`,
  `num_turns`) ama **sadece loglanıyor**.
- claude_cli provider CrewAI'a `Usage(0,0,0)` veriyor → `_track_and_check_budget`
  token-yaklaşımı claude_cli'de ~0; budget guard **kör** (#150 $9.78'e çıktı, guard
  tetiklenmedi).
- DB'de cost/token/tool kolonu yok; tool çağrıları (`🔧`) loglanıyor, sayılmıyor.

## Veri modeli (db.py)
- **Yeni tablo `llm_calls`** (çağrı-başı 1 satır):
  `id, job_id, step_key, agent, model, provider, turns, tool_calls, cost_usd,
  duration_ms, created_at`.
- **Denormalize toplamlar**:
  - `jobs`: `total_cost_usd, total_llm_calls, total_tool_calls, total_turns`
  - `job_steps`: `cost_usd, tool_calls, turns`
- `record_llm_call(rec)`: `llm_calls` INSERT + jobs/job_steps'te atomik
  `col = col + x` UPDATE.
- Şema: `CREATE TABLE IF NOT EXISTS` + idempotent `ALTER ADD COLUMN` (mevcut
  auto-create stiline uygun).

## Yakalama + atıf (attribution)
- **`claude_cli_llm`**: `_run_streaming` `tool_use` event'lerini sayar; çağrı
  sonunda `{model, cost_usd, turns, tool_calls, duration_ms}` kaydını
  **kayıtlı bir sink callback**'e gönderir (db ile decoupling).
- **Thread-local çağrı bağlamı**: `set_call_context(job_id, step_key, agent)` /
  `clear_call_context()` (repo-tool bağlamı deseniyle aynı). Sink, bağlamı +
  ölçümü birleştirip `record_llm_call` çağırır.
- **flow.py**: her step crew kickoff'undan önce bağlamı set eder (agent =
  step'in agent'ı); register_call_sink ile db sink'ini bağlar (initialize'da).
- **crew.py kickoff orchestrator**: 4 persona için bağlamı persona-başı set
  eder → kickoff per-agent doğru kırılır.
- Paralel step'ler (step9/step10 ayrı thread) thread-local sayesinde izole.

## Budget guard
- Sink job'un gerçek maliyetini biriktirir (`self._job_real_cost_usd`).
- `_track_and_check_budget` karşılaştırmada `max(gerçek_cost, token_yaklaşımı)`
  kullanır. `CREW_MAX_JOB_COST` claude_cli'de gerçekten çalışır.

## Görünürlük
- **API**: `/api/jobs`, `/api/jobs/{id}` → toplam cost/calls/tools/turns; job
  detayında per-agent kırılım (`GROUP BY agent`). `/api/status` per-step alanları.
- **Dashboard**: job kartında toplam maliyet/tur/araç rozeti; job detayında
  per-agent tablo.
- **Log**: job sonunda özet satırı.

## Dokunulan dosyalar
`db.py`, `tools/claude_cli_llm.py`, `flow.py`, `crew.py`, `server.py`,
`dashboard.py` + `web/`.

## Kapsam / notlar
- Birincil maliyet kaynağı claude_cli `total_cost_usd` (gerçek, OAuth-eşdeğeri).
  litellm yolunda token×fiyat fallback (mevcut `_track_and_check_budget`).
- Ölçüm pür gözlem (davranış değiştirmez) → toggle gerekmez. Budget-guard'ın
  gerçek maliyete geçişi daha doğru olduğundan koşulsuz uygulanır (eski token
  yaklaşımı fallback kalır).

## Kapsam dışı (YAGNI)
- Token-bazlı maliyet kırılımı (claude_cli token vermiyor; gerçek $ yeterli).
- Geçmiş job'lar için geriye dönük maliyet doldurma.
- Maliyet grafiği/zaman serisi (sadece toplam + per-agent kırılım).
