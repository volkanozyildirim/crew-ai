# 02 — kickoff_meeting_task (Kickoff Toplantısı)

## Kimlik
- **step_key:** `kickoff_meeting_task`
- **Flow metodu:** `step0_kickoff_meeting` (`flow.py:1466`)
- **Ajan:** `scrum_master` (moderatör) + 4 persona (BA/Deniz, Team Lead/Can,
  Developer/Baris, QA/Ece) + UAT
- **Görünen ad:** Kickoff Toplantısı
- **Tetikleyici:** `@listen(crew_step1_requirements)` — requirements'tan SONRA
- **Sonraki:** `crew_step4_technical_design`

## Ne yapar
"Sanal Odak Grubu" (Design Review Simulation): 4 uzman kendi perspektifinden 3
kritik soru/endişe paylaşır, birbirlerine yanıt verir, sonunda SM bir **Kritik
Tasarım İnceleme tutanağı** (Risk Tablosu + Edge Case'ler + Backlog Adayları)
üretir. Bu tutanak sonraki adımlara taşınır (architect risk tablosunu görür, QA
test perspektifini, UAT backlog adaylarını).

## Girdi
- `state.requirements_text`, `state.acceptance_criteria` (context'e dahil)
- env/knob: `CREW_KICKOFF_MEETING` (default açık), `CREW_KICKOFF_GRADING`,
  `CREW_ENABLE_RESUME`
- `kickoff_guidance` (geçmiş WI'lardan öğrenilmiş yönergeler) + bu run için
  `state.kickoff_feedback`

## İşleyiş
1. **Devre dışı kontrolü** (KN-10) — `CREW_KICKOFF_MEETING=0` ise atlanır.
2. **Resume** (KN-03) — önceki kickoff çıktısı varsa atla.
3. **Hedef repo tahmini (4 katman)** (`flow.py:1493`, KN-11):
   - Katman 0/1: `_select_repo_by_name` — tam isim eşleşmesi → parça eşleşmesi
   - Katman 2: kod grep — WI teknik terimleri repo kodlarında geçiyor mu
   - Katman 3: vector semantic search (son çare, score ≥ 0.1)
   Bulunan reponun özeti + dosya yapısı context'e eklenir.
4. **Guidance + feedback enjeksiyonu** (`flow.py:1583`) — öğrenilmiş yönergeler ve
   kullanıcı feedback'i context'in başına eklenir.
5. **`run_kickoff_meeting`** (`flow.py:1606`) — yeni varsayılan: task-by-task +
   Haiku grading + retry (KN-12). `CREW_KICKOFF_GRADING=0` ile klasik tek-Crew.
6. Budget check (KN-22). Kickoff **local** sayılır (budget'a dahil değil).
7. Per-agent çıktı + grade geçmişi `/tmp/crew_kickoff/job_<id>.json`'a yazılır
   (debug UI okur).

## Çıktı
- `state.kickoff_text` (tutanak) → sonraki adımların context'inde kullanılır
- DB + vector: `kickoff_meeting_task` (ilk 3000 char)
- `/tmp/crew_kickoff/job_<id>.json` (per-agent debug)

## Karar noktaları
- **KN-03** — Resume. Bkz. [decision-points.md#kn-03](../decision-points.md#kn-03)
- **KN-10** — Kickoff devre dışı bırakma. Bkz. [decision-points.md#kn-10](../decision-points.md#kn-10)
- **KN-11** — Kickoff hedef repo tahmini (4 katman). Bkz. [decision-points.md#kn-11](../decision-points.md#kn-11)
- **KN-12** — Kickoff grading + retry. Bkz. [decision-points.md#kn-12](../decision-points.md#kn-12)
- **KN-13** — Kickoff-only modu (step4'te durur). Bkz. [decision-points.md#kn-13](../decision-points.md#kn-13)

## Resume / dry-run
- Resume: kickoff metni cache'ten okunur.
- Dry-run: etkilemez (kickoff her durumda çalışabilir).
- **Kickoff-only:** `state.kickoff_only` true ise step0 çıktıları kaydedilir,
  `crew_step4_technical_design` `_KickoffOnlyStop` ile pipeline'ı durdurur (KN-13).

## Kaynak
- `flow.py:1466-1633` (`step0_kickoff_meeting`), `flow.py:34-71` (`_select_repo_by_name`)
- `tasks.yaml:10-271` (kickoff_*_task tanımları)
- `agents.yaml` (scrum_master, business_analyst, software_architect, senior_developer, qa_engineer, uat_specialist)
