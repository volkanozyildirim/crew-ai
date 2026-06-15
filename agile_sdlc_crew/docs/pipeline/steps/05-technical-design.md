# 05 — technical_design_task (Teknik Tasarım)

## Kimlik
- **step_key:** `technical_design_task`
- **Flow metodu:** `crew_step4_technical_design` (`flow.py:1686`)
- **Ajan:** `software_architect`
- **Görünen ad:** Teknik Tasarım
- **Tetikleyici:** `@listen(step0_kickoff_meeting)`
- **Sonraki:** `step5_create_branch` (`or_(hal_planning, crew_step4_technical_design)`)

## Ne yapar
Pipeline'ın **en karmaşık** adımı. Hedef repoyu kesinleştirir ve JSON geliştirme
planı (`changes[]`, her biri `file_path`/`current_code`/`new_code`/
`covers_requirements`) üretir. Architect'in tool çağırmadan plan üretebilmesi
için Python tarafı agresif **dosya pre-fetch** yapar (token tasarrufu).

## Girdi
- `state.requirements_text`, `state.kickoff_text`, `state.acceptance_criteria`
- WI detayı (başlık/açıklama/AC), aday repo özetleri
- env/knob: `CREW_ENABLE_RESUME`, `CREW_TASK_GUARDRAILS`, `CREW_KNOWLEDGE_RAG`, `CREW_SM_REVIEW`

## İşleyiş (özet)
1. `dependency_analysis_task` "atlandı" işaretlenir.
2. **Aday repo hazırlığı** (`flow.py:1700`): vector top-15 + **path/URL mining**
   (WI metninde geçen repo adlarını zorla ekle) + **symbol-grep kanıtı**.
3. **`_run_discover_repos`** çağrılır (adım 03 — öneri üretir).
4. **Resume / cache** (KN-16, `flow.py:1791`): önceki job'dan plan JSON'u varsa,
   **brace-balance** kontrolünden geçerse parse edilip kullanılır; truncate/bozuk
   cache silinir.
5. **Hedef repo prefetch tahmini (katmanlı)** (KN-17, `flow.py:1870`):
   - Katman -1: exclusive symbol-grep kanıtı (en güçlü, isim eşleşmesini ezer)
   - Katman 0/1: `_select_repo_by_name`
   - Katman 2: kod grep (eşleşen dosyalar pre-fetch'e girer)
   - Katman 3: vector search
6. **Dosya pre-fetch** (`flow.py:1969`): grep eşleşen dosyalar → PR ref dosyalar →
   WI ipucu dosyaları → repo-içi grep → manifest + dizin yapısı. Hepsi context'e.
7. **Architect crew kickoff** (`flow.py:2206`). Guardrail açıksa (`CREW_TASK_GUARDRAILS`)
   `architect_json_guardrail` devrede (KN-18). Hata olursa guardrail'siz fallback.
8. **Parse + retry** (KN-19): `_parse_architect_output` başarısızsa önceki çıktıyı
   context'e ekleyip guardrail'siz retry.
9. **SM Review** (KN-09).
10. **Repo adı çözümü** (KN-08): plan'daki `repo_name` known_repos'ta yoksa
    `_resolve_repo_name` ile çözülür.

## Çıktı
- `state.repo_name` (KESİN hedef repo — buradan itibaren bağlayıcı)
- `state.plan` (parse edilmiş JSON plan)
- DB + vector: `technical_design_task` ham JSON (≤50K char — cache parse edilebilsin)

## Karar noktaları
- **KN-08** — Repo adı çözümü. Bkz. [decision-points.md#kn-08](../decision-points.md#kn-08)
- **KN-09** — SM Review. Bkz. [decision-points.md#kn-09](../decision-points.md#kn-09)
- **KN-16** — Plan cache geçerlilik (brace-balance). Bkz. [decision-points.md#kn-16](../decision-points.md#kn-16)
- **KN-17** — Prefetch hedef repo tahmini (katmanlı). Bkz. [decision-points.md#kn-17](../decision-points.md#kn-17)
- **KN-18** — Architect JSON guardrail. Bkz. [decision-points.md#kn-18](../decision-points.md#kn-18)
- **KN-19** — Plan parse hatası → retry. Bkz. [decision-points.md#kn-19](../decision-points.md#kn-19)
- **KN-22** — Budget guard. Bkz. [decision-points.md#kn-22](../decision-points.md#kn-22)

## Resume / dry-run
- Resume: tam JSON plan cache'ten parse edilip kullanılır (brace-balance şartı).
- Dry-run: etkilemez (planlama her durumda çalışır).
- **Kickoff-only:** `state.kickoff_only` true ise bu metod başında `_KickoffOnlyStop`
  ile pipeline durur.

## Kaynak
- `flow.py:1686-2289` (`crew_step4_technical_design`)
- `tasks.yaml:361-509` (`technical_design_task`)
- `agents.yaml:38-90` (`software_architect`)
