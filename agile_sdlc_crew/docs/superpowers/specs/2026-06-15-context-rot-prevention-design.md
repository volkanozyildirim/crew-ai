# Context Rot Önleme — Tasarım Belgesi

**Tarih:** 2026-06-15
**Kapsam:** Pipeline'da context-rot'a karşı **deterministik** (ekstra LLM çağrısı YOK) önlemler. Üç boyut: (1) iş-içi context kalitesi, (2) token/maliyet şişmesi, (3) uzun-ömürlü server / cross-job hijyeni.
**Kapsam dışı:** LLM-tabanlı özetleme/compaction (kullanıcı kararı: deterministik). Bu Claude Code oturumunun context'i (pipeline değil).

## İlke

**Önce görünür kıl, sonra sınırla.** Hiç ekstra LLM çağrısı yok; tüm sınırlar env-ayarlanabilir; toggle-kapalı davranış mevcutla aynı; geri alınabilir.

## Problem Analizi (kanıtlanmış bulgular)

1. **Ölü accumulator:** `self.state.previous_context`, `_append_context()` ile her adımda ≤5000 char eklenerek ~65KB'ye büyüyor (flow.py:218-229, 27 çağrı yeri). **Hiçbir yerde okunmuyor** — kickoff'a giden `previous_context` input'u aslında `_build_step_context()` çıktısı olan yerel `ctx`. Yani saf bellek israfı + gelecekte yanlışlıkla okunursa 65KB bloat riski. Ayrıca daha önce eklenen `_state_lock` (flow.py:214,918) **yalnızca** bu ölü alanın `+=`'sini koruyor → accumulator kalkınca lock da gereksiz.
2. **Ajanlara giden gerçek context:** `_build_step_context()` (flow.py:231-353) yapısal alanlardan derleniyor, dağınık magic-number truncation'larla (`[:5000]`, `[:4000]`, `[:3000]`, `[:2500]`). Asıl token/kalite etkisi burada — ama **boyutu hiçbir yerde loglanmıyor**, rot görünmez.
3. **Sınırsız final append'ler:** review/test/uat çıktıları `_append_context`'e truncation'sız gidiyor (flow.py:3052, 3234, 3302) — ölü alana gittiği için zararsız ama Bölüm 2 ile birlikte temizlenir.
4. **Cross-job büyüme:** `save_step_output` (flow.py:398, vector_store.py:606) her adım çıktısını vector DB'ye yazıyor → job'lar arası sınırsız büyüme.
5. **Özetleme/compaction yok:** her şey ham concatenation + slicing.

## Önlemler

### M1 — `context_budget.py`: merkezi ölçüm + sınır (çekirdek)

Yeni modül `src/agile_sdlc_crew/context_budget.py`:
- **Named caps** (magic number'ların yerine), her biri env-override: `CREW_CTX_KICKOFF` (default 4000), `CREW_CTX_KICKOFF_QA` (2500), `CREW_CTX_KICKOFF_REVIEW` (2000), `CREW_CTX_REQUIREMENTS` (3000), `CREW_CTX_REVIEW`/`CREW_CTX_TEST`/`CREW_CTX_UAT` (2500), `CREW_CTX_PLAN_CHANGES` (10, adet), `CREW_CTX_AC` (15, adet), `CREW_CTX_TOTAL_WARN` (24000, hard guard eşiği).
- **`measure(label, text) -> str`**: assemble edilen context'in char/≈token boyutunu pipeline log'una yazar (`📏 context[label]: 18.2K char ≈4.5K tok`); `CREW_CTX_TOTAL_WARN` aşılırsa `⚠️` uyarısı + son N char'a kırpar (defense-in-depth; default kapalı kırpma, sadece uyarı — `CREW_CTX_HARD_TRUNCATE=1` ile kırpma açılır).
- Tek sorumluluk: context boyut politikası + gözlemlenebilirlik. Diğer modüller buradan okur.

### M2 — Ölü `previous_context` accumulator'ını kaldır

- `flow.py`: `previous_context` alanı (175), `_append_context` metodu (218-229) ve **27 çağrı yeri** kaldırılır.
- `_state_lock` (214, 226, 918) kaldırılır — yalnızca `_append_context` kullanıyordu (kanıtlandı: grep ile tek kullanıcı). Bu, daha önceki race-fix'i geçersiz kılmaz; o fix zaten okunmayan bir alanı koruyordu.
- Sonuç: ~65KB monotonik birikim + gereksiz lock + ölü kod gider. Davranış değişmez (ajanlar bu alanı kullanmıyordu).
- **Doğrulama önkoşulu:** kaldırmadan önce `grep -rn "state.previous_context"` ile yalnızca yazım olduğu (okuma yok) bir kez daha teyit edilir.

### M3 — `_build_step_context` truncation'larını merkezileştir + gözlemle

- `_build_step_context` içindeki magic number'lar M1'deki named cap'lerle değiştirilir.
- Her step'in `crew.kickoff(... previous_context=ctx ...)` çağrısından önce `ctx = context_budget.measure("<step_key>", ctx)` ile sarmalanır → ajana giden gerçek context boyutu loglanır + guard uygulanır. (Çağrı yerleri: flow.py 2256, 3029, 3213, 3282, 3344, 1361, 1631 vb.)

### M4 — Cross-job / server hijyeni

- **Per-job reset:** `initialize()` içinde tek `_reset_job_state()` — `_job_*_tokens` sıfırla, `reset_tool_cache()` (zaten var) çağrısını buraya topla. Böylece bir job'ın artığı sonrakine sızmaz (tek, denetlenebilir yer).
- **Vector DB retention:** `save_step_output` için deterministik sınır — `CREW_STEP_OUTPUT_RETENTION` (default örn. 500 kayıt) aşılınca en eski kayıtları buda, veya yazımı `CREW_SAVE_STEP_OUTPUT=1` toggle'ına bağla (default açık). vector_store.py'de basit budama/sayım.

### M5 — Kickoff bayatlama görünürlüğü (düşük öncelik)

- Kickoff çıktısı 4 kez tekrar taşınıyor; M1 cap'leri ile sınırlı. Ek olarak `len(kickoff_text)` üretildiğinde loglanır (`📏 kickoff: N char`) — büyürse fark edilir. Özetleme yok.

## Kararlar

- **M2:** `previous_context` tamamen kaldırılır (ring-buffer'a bağlanmaz) — ölü olduğu kanıtlandı; kaldırmak hem rot'u hem lock'u hem ölü kodu eler.
- **Deterministik:** hiçbir önlemde LLM çağrısı yok.
- **Toggle:** tüm cap'ler env-override; kırpma (`CREW_CTX_HARD_TRUNCATE`) default kapalı (önce gözlemle, sonra zorla).

## Test / Doğrulama

Pytest yok (CLAUDE.md). Doğrulama:
1. **Import:** `context_budget` + `flow` + `crew` + `server` temiz import.
2. **M2 regresyon:** `previous_context`/`_append_context`/`_state_lock` kalmadığını grep ile doğrula; bir WI dry-run uçtan uca aynı sonucu üretsin (alan okunmadığı için çıktı değişmemeli).
3. **M1/M3 gözlem:** bir WI çalıştır; `/tmp/crew_pipeline.log`'da her adımda `📏 context[...]` satırları görünsün; boyutlar makul (<24K) olsun.
4. **M4:** ikinci job çalıştır; `_reset_job_state` ile token sayaçlarının sıfırdan başladığı, vector DB kayıt sayısının sınır içinde kaldığı doğrulanır.

## Rollout Sırası

1. **M1** (context_budget modülü — temel, bağımsız).
2. **M3** (build_step_context + kickoff çağrılarını measure'a bağla — gözlemlenebilirlik aktif).
3. **M2** (ölü accumulator + lock kaldır — en çok dosya dokunuşu, en sona güvenli).
4. **M4** (cross-job hijyen).
5. **M5** (kickoff log — trivial).

## Riskler

- **M2 (27 çağrı yeri silme):** mekanik ama hacimli; yanlışlıkla komşu kod silme riski → her silme dar, sadece `self._append_context(...)` satırı. `_state_lock` kaldırınca init'te kalan referans kalmadığı doğrulanmalı.
- **M4 vector budama:** mevcut benzer-iş aramasını bozmamalı; budama yalnızca eski kayıtlarda, retention yüksek default.
- Cap'ler fazla sıkı olursa ajan bağlam kaybedebilir → default'lar mevcut magic-number'larla aynı; sıkılaştırma env ile opsiyonel.
