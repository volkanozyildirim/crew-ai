# 01 — requirements_analysis_task (İş Analizi)

## Kimlik
- **step_key:** `requirements_analysis_task`
- **Flow metodu:** `crew_step1_requirements` (`flow.py:1133`)
- **Ajan:** `business_analyst`
- **Görünen ad:** İş Analizi
- **Tetikleyici:** `@listen("crew_planning")` — CrewAI yolunun **İLK** adımı
- **Sonraki:** `step0_kickoff_meeting`

> Sıra önemli: requirements kickoff'tan ÖNCE çalışır. Böylece kickoff
> toplantısında ajanlar iş analizini, kabul kriterlerini ve hedef repoyu zaten
> bilerek tartışır (kör tartışma olmaz).

## Ne yapar
Work item'ı (başlık + açıklama + kabul kriterleri) analiz eder, yapılandırılmış
JSON üretir (FR/TR/AC). Aynı zamanda **yetersizlik kontrolü**, **görsel/link
analizi** ve **mevcut PR yorumlarının okunması** burada yapılır.

## Girdi
- `state.work_item_id`
- WI alanları: `System.Title`, `System.Description`,
  `Microsoft.VSTS.Common.AcceptanceCriteria` (HTML stripleniyor)
- WI relations → mevcut PR bağlantıları (active > completed, abandoned atlanır)
- env/knob: `CREW_MIN_WI_CONTENT_CHARS` (default 100), `CREW_ANALYZE_WI_MEDIA`,
  `CREW_SM_REVIEW`, `CREW_ENABLE_RESUME`

## İşleyiş
1. **Resume kontrolü** (`flow.py:1146`) — önceki job'dan BA çıktısı varsa kabul
   kriterleri parse edilip atlanır (KN-03).
2. WI içeriği Python'da okunur, context'e eklenir (ajan tool çağırmak zorunda
   kalmasın — küçük local modeller tool'u iyi çağıramıyor).
3. **Plain-text içerik uzunluğu** hesaplanır → yetersizlik kontrolünün girdisi.
4. **Mevcut PR yorumları** okunur (`flow.py:1200`): resolve edilmemiş insan
   yorumları context'e eklenir, implement sonrası yanıtlanmak üzere
   `_pr_threads_to_respond`'a kaydedilir.
5. **Görsel/link analizi** (`CREW_ANALYZE_WI_MEDIA`, `flow.py:1326`) — description
   içindeki inline media textual'a çevrilip context'e eklenir.
6. BA crew kickoff → `requirements_text`.
7. **Yetersizlik kontrolü** (KN-04, `flow.py:1356`) — Python kararı: içerik
   `MIN_CONTENT_CHARS` altındaysa pipeline durur, WI'ya yorum atılır. (Ajan'a
   "INSUFFICIENT de" denmez; küçük modeller keyword'ü kopyalıyor.)
8. **SM Review** (KN-09) — `CREW_SM_REVIEW` açıksa kalite kapısı.
9. **BA JSON parse + kabul kriteri çıkarımı** (KN-05, `flow.py:1417`) — 4 katmanlı
   öncelik: BA JSON AC → WI AC alanı → WI description maddeleri → BA serbest metin.

## Çıktı
- `state.requirements_text` (BA çıktısı, JSON)
- `state.acceptance_criteria` (en fazla 15, pipeline boyunca **bağlayıcı**)
- `self._pr_threads_to_respond`, `_pr_repo_for_threads`, `_pr_id_for_threads`
- DB + vector store: `requirements_analysis_task` çıktısı (ilk 3000 char)

## Karar noktaları
- **KN-03** — Resume (cache'ten atla). Bkz. [decision-points.md#kn-03](../decision-points.md#kn-03)
- **KN-04** — Yetersizlik kontrolü (içerik eşiği → abort). Bkz. [decision-points.md#kn-04](../decision-points.md#kn-04)
- **KN-05** — Kabul kriteri kaynağı seçimi (4 katman). Bkz. [decision-points.md#kn-05](../decision-points.md#kn-05)
- **KN-06** — Mevcut PR seçimi (active/completed/abandoned). Bkz. [decision-points.md#kn-06](../decision-points.md#kn-06)
- **KN-09** — SM Review kapısı. Bkz. [decision-points.md#kn-09](../decision-points.md#kn-09)

## Resume / dry-run
- Resume: BA çıktısı + kabul kriterleri cache'ten okunur.
- Dry-run: bu adımı etkilemez (analiz her durumda çalışır).

## Kaynak
- `flow.py:1133-1463` (`crew_step1_requirements`)
- `tasks.yaml:305-359` (`requirements_analysis_task`)
- `agents.yaml:9-36` (`business_analyst`)
