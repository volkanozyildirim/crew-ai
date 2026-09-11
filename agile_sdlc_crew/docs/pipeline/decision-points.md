# Pipeline Karar Noktaları (Decision Points)

Bu doküman, pipeline boyunca dallanma yaratan tüm karar noktalarını tek yerde
toplar. Her karar `KN-NN` koduyla etiketlidir; adım dokümanlarının "Karar
noktaları" bölümlerinden bu koda referans verilir.

Her giriş şu yapıdadır: **Nerede** (adım + kaynak satır) · **Karar** (hangi soru)
· **Girdi** (neye bakar) · **Sonuç** (dallar) · **Neden** (tasarım gerekçesi).

> Karar noktaları kabaca 3 sınıfa ayrılır:
> - **Akış kapısı** (pipeline'ı durdurabilir/dallandırır): KN-04, KN-22, KN-27, KN-31
> - **Repo/dosya seçimi** (kalite-kritik): KN-08, KN-11, KN-14, KN-15, KN-17, KN-25
> - **Optimizasyon/idempotency** (resume, cache, skip, toggle): KN-03, KN-16, KN-20, KN-21, KN-24, KN-28

---

## KN-01 — Dry-run modu seçimi
- **Nerede:** `00-initialize` · `flow.py:912-924`
- **Karar:** Bu job dry-run mı (push/PR/review/test/UAT atla, sonuç local kalsın)?
- **Girdi:** DB `jobs.dry_run` satırı VEYA `CREW_DRY_RUN` env.
- **Sonuç:** İkisinden biri true → `state.dry_run=True`. Sonraki adımlar remote işlemleri atlar.
- **Neden:** Geliştirme/test sırasında remote'a dokunmadan tam pipeline koşturmak.

## KN-02 — Workspace cleanup kapsamı
- **Nerede:** `00-initialize` · `flow.py:948-986`
- **Karar:** Hangi repolardaki artıklar temizlensin?
- **Girdi:** Repo'da `feature/<wi>` (bu WI'nın) branch'i var mı?
- **Sonuç:** Varsa o repo `origin/main`'e hard reset + `clean -fd` (REPO_SUMMARY.md korunur). Başka WI branch'lerine DOKUNULMAZ.
- **Neden:** Paralel çalışan başka job'ların branch'lerini bozmamak; sadece kendi artığını temizlemek.

## KN-03 — Resume (cache'ten adım atlama)
- **Nerede:** `requirements`, `kickoff`, `test`, `uat` · `_try_resume_step` `flow.py:345`
- **Karar:** Bu adımın önceki bir job'dan başarılı çıktısı var mı, atlanabilir mi?
- **Girdi:** `CREW_ENABLE_RESUME` (dashboard/env) + DB `get_cached_step_output(step_key, wi)` (>20 char).
- **Sonuç:** Varsa adım çalıştırılmadan cache çıktısı state'e yüklenir, done işaretlenir.
- **Neden:** Tekrar çalıştırmada tamamlanmış adımları atlayıp maliyet/süre tasarrufu. Vendor/yeni context için kapatılabilir.

## KN-04 — Yetersizlik kontrolü (içerik eşiği)  ⛔ AKIŞ KAPISI
- **Nerede:** `01-requirements` · `flow.py:1356-1374`
- **Karar:** Work item otomatik geliştirme için yeterli içerik taşıyor mu?
- **Girdi:** Plain-text içerik uzunluğu (başlık+açıklama+AC+media) vs `CREW_MIN_WI_CONTENT_CHARS` (default 100).
- **Sonuç:** Altındaysa → WI'ya "Insufficient" yorumu + `RuntimeError` (pipeline durur).
- **Neden:** **Python kararı** — ajana "INSUFFICIENT de" denmiyor, küçük modeller keyword'ü kopyalayıp yanlış karar veriyordu.

## KN-05 — Kabul kriteri kaynağı (4 katman)
- **Nerede:** `01-requirements` · `flow.py:1417-1454`
- **Karar:** Bağlayıcı kabul kriterleri nereden alınsın?
- **Girdi:** Sırasıyla: (1) BA JSON `acceptance_criteria` (ID'li), (2) WI AC alanı, (3) WI description maddeleri, (4) BA serbest metin maddeleri.
- **Sonuç:** İlk dolu kaynak kazanır; en fazla 15 kriter `state.acceptance_criteria`'ya.
- **Neden:** Kriterler pipeline boyunca (tasarım/geliştirme/inceleme/UAT) bağlayıcı tek kaynak.

## KN-06 — Mevcut PR seçimi
- **Nerede:** `01-requirements` · `flow.py:1222-1269`
- **Karar:** WI'ya bağlı PR'lardan hangisi (varsa) baz alınsın?
- **Girdi:** WI relations'daki PR bağlantıları, PR statüleri (active/completed/abandoned).
- **Sonuç:** En yeniden eskiye: ilk **active** → yoksa ilk **completed** → tümü **abandoned** ise yeni PR. Seçilen PR'ın resolve edilmemiş yorumları context'e + `_pr_threads_to_respond`'a.
- **Neden:** Önceki denemeden kalan insan feedback'ini sürdürmek; abandoned'ları görmezden gelmek.
- **NOT:** PR-link url ayracı `%2F` (büyük) olabildiğinden parse regex `re.IGNORECASE` ile çalışır (aksi halde bu org'daki WI'ların mevcut PR'ı/yorumları bulunamıyordu).

## KN-07 — HAL: değişiklik yoksa followup
- **Nerede:** `01a-hal-planning` · `flow.py:1081-1098`
- **Karar:** HAL ilk analizde hiç değişiklik döndürmediyse ne yapılsın?
- **Girdi:** `plan["changes"]` boş mu?
- **Sonuç:** Boşsa aynı sohbette followup ile dosya yolları + kod blokları istenir.
- **Neden:** HAL bazen ilk turda detay vermiyor; sohbet bağlamını koruyarak ikinci tur.

## KN-08 — Repo adı çözümü (`_resolve_repo_name`)
- **Nerede:** `01a-hal`, `05-technical-design` · `flow.py:1054`, `flow.py:2276-2281`
- **Karar:** Plan/HAL'in döndürdüğü repo adı geçerli mi, değilse hangisine eşlenir?
- **Girdi:** `repo_name` known_repos'ta mı; değilse `_resolve_repo_name` (isim eşleştirme + fallback).
- **Sonuç:** Geçerli isim `state.repo_name`'e yazılır.
- **Neden:** LLM bazen tam olmayan/yanlış repo adı üretir; known_repos'a sabitlemek gerekir.

## KN-09 — Scrum Master Review kapısı
- **Nerede:** `requirements`, `technical_design`, `review`, `test`, `uat` · `_scrum_review` `flow.py:706`
- **Karar:** Adım çıktısı kalite kapısından geçti mi, iyileştirme gerekli mi?
- **Girdi:** `CREW_SM_REVIEW` (default kapalı) açıksa SM crew çıktıyı değerlendirir; "IMPROVE"/"IYILESTIR" token'ı aranır.
- **Sonuç:** IMPROVE → adım SM feedback ile bir kez yeniden çalıştırılır. APPROVE → devam.
- **Neden:** Opsiyonel ekstra kalite katmanı; her çağrı ek API maliyeti olduğundan default kapalı.

## KN-10 — Kickoff devre dışı bırakma
- **Nerede:** `02-kickoff` · `flow.py:1475-1478`
- **Karar:** Kickoff toplantısı çalışsın mı?
- **Girdi:** `CREW_KICKOFF_MEETING` (default açık).
- **Sonuç:** Kapalıysa adım "Devre dışı" işaretlenip atlanır.
- **Neden:** Maliyet/süre kısıtında kickoff'u kapatabilmek.

## KN-11 — Kickoff hedef repo tahmini (4 katman)
- **Nerede:** `02-kickoff` · `flow.py:1493-1579`
- **Karar:** Kickoff tartışması hangi repo bağlamında yapılsın?
- **Girdi:** Katman 0/1 `_select_repo_by_name` (tam isim → parça), Katman 2 kod grep (teknik terimler), Katman 3 vector search (score ≥ 0.1).
- **Sonuç:** İlk eşleşen katman kazanır; reponun özeti + dosya yapısı context'e eklenir.
- **Neden:** Kickoff'un anlamlı olması için hedef repo bağlamı gerekir; ucuzdan pahalıya katmanlı.

## KN-12 — Kickoff grading + retry
- **Nerede:** `02-kickoff` · `run_kickoff_meeting` (`flow.py:1606`)
- **Karar:** Kickoff per-task çıktıları yeterli kalitede mi, retry gerekli mi?
- **Girdi:** `CREW_KICKOFF_GRADING` — açıkken task-by-task + Haiku grading + retry; kapalıyken klasik tek-Crew.
- **Sonuç:** Düşük grade alan task'lar yeniden üretilir. Grade geçmişi debug JSON'a yazılır.
- **Neden:** Küçük/ucuz modellerle kickoff kalitesini grade-and-retry ile yükseltmek.

## KN-13 — Kickoff-only modu  ⛔ AKIŞ KAPISI
- **Nerede:** `02-kickoff` / `05-technical-design` başı · `flow.py:1691-1693`
- **Karar:** Pipeline sadece kickoff debug'ı için mi koşuyor?
- **Girdi:** `state.kickoff_only` (main.run_kickoff_only setler).
- **Sonuç:** True ise step4 başında `_KickoffOnlyStop` ile pipeline durur (başarı sayılır).
- **Neden:** Kickoff kalitesini izole debug etmek; tüm pipeline'ı koşmadan.

## KN-14 — Discover öneri doğrulama
- **Nerede:** `03-discover-repos` · `flow.py:514-517`
- **Karar:** LLM'in önerdiği `target_repo` kabul edilebilir mi?
- **Girdi:** `target` known_repos'ta mı?
- **Sonuç:** Değilse görmezden gelinir (öneri boş).
- **Neden:** LLM halüsinasyon repo adı üretebilir; sadece gerçek repolar öneri olabilir.

## KN-15 — Discover repo seçimi öncelik sırası
- **Nerede:** `03-discover-repos` · `flow.py:469-482` (prompt) + `_grep_symbol_evidence`
- **Karar:** Birden çok aday varken hangi repo önerilsin?
- **Girdi:** (1) Bir sembol YALNIZCA tek repoda mı geçiyor (exclusive), (2) tablo/model/dosya sahipliği, (3) repo adı benzerliği.
- **Sonuç:** Exclusive symbol kanıtı en güçlü — repo adı benzerliğini EZER.
- **Neden:** "stock_api_list" sembolü "stock-api" reposunda değil, gerçek sahibinde geçebilir; isim benzerliği yanıltıcı.

## KN-16 — Plan cache geçerlilik (brace-balance)
- **Nerede:** `05-technical-design` · `flow.py:1799-1842`
- **Karar:** Önceki job'ın plan JSON cache'i kullanılabilir mi?
- **Girdi:** `_looks_complete_json` — `{` == `}` sayısı, "changes" içeriyor mu? Sonra `_parse_architect_output`.
- **Sonuç:** Dengesiz/truncate cache DB'den silinir, agent çalışır. Geçerliyse parse edilip kullanılır.
- **Neden:** Eskiden `[:3000]` ile kesilmiş bozuk JSON cache'i tekrar okunuyordu; artık tam (≤50K) saklanıp doğrulanıyor.

## KN-17 — Prefetch hedef repo tahmini (katmanlı)
- **Nerede:** `05-technical-design` · `flow.py:1870-1963`
- **Karar:** Dosya pre-fetch için hangi repo baz alınsın? (architect'in son kararından bağımsız ön-tahmin)
- **Girdi:** Katman -1 exclusive symbol-grep, Katman 0/1 `_select_repo_by_name`, Katman 2 kod grep (eşleşen dosyalar pre-fetch'e girer), Katman 3 vector.
- **Sonuç:** İlk karar veren katman; sonuç sadece context hint'i (architect override edebilir).
- **Neden:** Doğru dosyaları context'e koyup architect'in tool çağırmasını (token şişmesi) önlemek.

## KN-18 — Architect JSON guardrail
- **Nerede:** `05-technical-design` · `flow.py:2206-2227`, `guardrails.architect_json_guardrail`
- **Karar:** Architect çıktısı geçerli JSON plan mı?
- **Girdi:** `CREW_TASK_GUARDRAILS` açıksa CrewAI guardrail `_parse_architect_output` ile doğrular.
- **Sonuç:** Başarısızsa agent otomatik retry; retry'lar tükenirse guardrail'siz fallback crew.
- **Neden:** Plan parse garantisi; geçersiz JSON'da agent kendini düzeltsin.

## KN-19 — Plan parse hatası → retry
- **Nerede:** `05-technical-design` · `flow.py:2234-2257`
- **Karar:** `_parse_architect_output` ValueError verirse?
- **Girdi:** Parse exception.
- **Sonuç:** Önceki çıktı context'e eklenip guardrail'siz architect ile tekrar (tool'suz, sade JSON iste).
- **Neden:** Guardrail kapalıyken birincil kurtarma; placeholder/format hatalarını düzeltmek.

## KN-20 — Deps install (env-toggle)
- **Nerede:** `06-create-branch` · `flow.py:2343-2351`
- **Karar:** Hedef repo'da bağımlılıklar (composer/npm/go) kurulsun mu?
- **Girdi:** `CREW_INSTALL_DEPS` (default kapalı).
- **Sonuç:** Açıksa `install_dependencies` — vendor/ oluşur, ajanlar 3rd-party kodu okuyabilir. İlk install yavaş.
- **Neden:** vendor okuma ihtiyacı olan işler için; maliyet/süre nedeniyle default kapalı. (composer hang fix uygulanmış.)

## KN-21 — Vendor/plan embed (env-toggle)
- **Nerede:** `06-create-branch` · `flow.py:2356-2382`
- **Karar:** Plan dosyaları + vendor allowlist vector'e index'lensin mi?
- **Girdi:** `CREW_VENDOR_INDEX` (default kapalı), deps install başarılı mı.
- **Sonuç:** Açıksa tüm repo yerine **hedef odaklı** embed (plan dosyalarının parent dizinleri + vendor allowlist).
- **Neden:** 4000+ dosya yerine ~20 dosya embed; semantic search framework kodunda da arasın.

## KN-22 — Budget guard  ⛔ AKIŞ KAPISI
- **Nerede:** Her crew kickoff sonrası · `_track_and_check_budget` `flow.py:644`
- **Karar:** Kümülatif LLM maliyeti limiti aştı mı?
- **Girdi:** Harici (local olmayan) adımların token toplamı × fiyat vs `CREW_MAX_JOB_COST` (default 5.0). Local adımlar (kickoff, requirements, local developer) sayılmaz.
- **Sonuç:** Aşılırsa WI'ya "Maliyet Limiti Aşıldı" yorumu + `RuntimeError` (pipeline durur).
- **Neden:** Kaçak maliyet koruması. Fiyatlar `CREW_PRICE_INPUT/OUTPUT_USD_PER_M` ile ayarlanır.

## KN-23 — Dry-run dallanması (push/PR/review/test/uat)
- **Nerede:** `06`–`11` · `flow.py:2395`, `2716`, `2865`, `3057`, `3189`
- **Karar:** Remote işlem yapılsın mı?
- **Girdi:** `state.dry_run`.
- **Sonuç:** Branch local oluşturulur; PR/review/test/UAT atlanır veya local kalır.
- **Neden:** Remote'a dokunmadan tam akış denemesi.

## KN-24 — Skip: aynı içerik zaten push edilmiş
- **Nerede:** `07-implement` · `flow.py:2479-2490`
- **Karar:** Bu dosya zaten doğru içerikle push edilmiş mi?
- **Girdi:** Branch'teki içerik `new_code` ile **TAM** eşleşiyor mu (strip sonrası).
- **Sonuç:** Tam eşleşme → atla. (Prefix eşleşmesi kullanılmaz — yeni branch'te main içeriği döner, yanlış pozitif.)
- **Neden:** Idempotency; tekrar push'u önlemek ama yanlış-pozitif skip'ten kaçınmak.

## KN-25 — Uygulama stratejisi (direct-edit / LLM blok / append)
- **Nerede:** `07-implement` · `flow.py:2534-2622`
- **Karar:** Değişiklik nasıl uygulanır?
- **Girdi:** `change_type`, `full_content`, `new_code`, `current_code` varlığı; `_try_direct_edit` sonucu.
- **Sonuç:** add→append/yeni dosya; full+new+current→direct-edit (Python) → olmazsa LLM MODE B blok → olmazsa append; new_code yok→LLM tam dosya.
- **Neden:** Mümkün olduğunca LLM çağırmadan (ucuz, deterministik) düzenleme; küçük modeller tam dosyada başarısız.

## KN-26 — Push öncesi güvenlik kontrolleri (kod kaybı koruması)
- **Nerede:** `07-implement` · `flow.py:2548`, `2661-2685`; retry'da `flow.py:800-812`
- **Karar:** Üretilen içerik push edilmeye güvenli mi?
- **Girdi:** Satır/char sayıları: add'de kısalma; edit'te orijinal >500 char ve yeni <%50; <50 char veya <3 satır.
- **Sonuç:** Herhangi biri tetiklenirse push İPTAL (dosya atlanır).
- **Neden:** Agent truncate/parça çıktısı tam dosyayı silip production'ı bozmasın.

## KN-27 — Plan-push coverage kontrolü  ⛔ AKIŞ KAPISI
- **Nerede:** `08-create-pr` · `flow.py:2729-2762`
- **Karar:** Plan yeterince uygulandı mı, PR açılsın mı?
- **Girdi:** Hiç push yok → abort. coverage = pushed/expected.
- **Sonuç:** coverage < 0.7 → WI'ya "Plan Eksik Uygulandı" yorumu + `RuntimeError`. Yarım PR açılmaz.
- **Neden:** Eksik/yarım PR açıp insanları yanıltmaktansa durmak.

## KN-28 — Mevcut aktif PR yeniden kullanımı
- **Nerede:** `08-create-pr` · `flow.py:2769-2790`
- **Karar:** Branch'te zaten aktif PR var mı?
- **Girdi:** `find_active_pr_by_branch`.
- **Sonuç:** Varsa yenisi açılmaz, mevcut PR id/url kullanılır.
- **Neden:** Azure DevOps 409 + retry + SSL hata domino'sunu önlemek.

## KN-29 — PR oluşturma retry + SSL recovery
- **Nerede:** `08-create-pr` · `flow.py:2807-2848`
- **Karar:** PR oluşturma hatası geçici mi, PR aslında oluştu mu?
- **Girdi:** 3 deneme + exponential backoff; tümü başarısızsa `find_active_pr_by_branch` ile "aslında oluştu mu" kontrolü.
- **Sonuç:** Başarılı sonuç veya `RuntimeError`.
- **Neden:** Azure DevOps transient `UNEXPECTED_EOF` hataları PR'ı oluştursa bile hata döndürebiliyor.

## KN-30 — Reviewer verdict tespiti
- **Nerede:** `09-code-review` · `flow.py:3000-3006` (+ retry `flow.py:843-850`)
- **Karar:** Reviewer onayladı mı, değişiklik mi istiyor?
- **Girdi:** Çıktı upper-case'inde token araması: `CHANGES_REQUIRED`, `REJECTED`, `VERDICT: REJECT` + Türkçe eşdeğerleri.
- **Sonuç:** RED token'ı → retry döngüsü (KN-31). Yoksa onay.
- **Neden:** Verdict pipeline-kritik; hem İngilizce (yeni) hem Türkçe (legacy) token desteklenir.

## KN-31 — Review retry döngüsü + max retry  ⛔ AKIŞ KAPISI
- **Nerede:** `09-code-review` · `_review_retry_loop` (`flow.py`), `step8_code_review`
- **Karar:** RED sonrası tekrar geliştirme yapılsın mı, kaç kez?
- **Girdi:** `_review_attempt` vs `CREW_REVIEW_MAX_RETRIES` (**default 1**).
- **Sonuç:** Limit altında → reviewer'ın bahsettiği dosyaları yeniden implement+push+review. Limit aşılırsa WI'ya hata yorumu + `RuntimeError`.
- **Neden:** Otomatik düzeltme döngüsü; sonsuz döngüyü ve fake-APPROVE'u önlemek. Her retry pahalı (developer+reviewer yeniden çalışır), bu yüzden default 1'e çekildi (eskiden 2).

## KN-34 — Review PR pre-fetch (claude_cli adım/maliyet azaltma)
- **Nerede:** `09-code-review` · `_prefetch_pr_changes_context` (`flow.py`), step8 + retry döngüsü
- **Karar:** Reviewer PR'daki değişen dosyaları tool ile mi okusun yoksa context'te hazır mı bulsun?
- **Girdi:** `state.plan.changes` + `state.all_pushes`'tan değişen dosyalar; feature-branch içerikleri.
- **Sonuç:** Değişen dosyaların içerikleri "PR DEĞİŞİKLİKLERİ" bloğu olarak context'e konur; tasks.yaml reviewer'a "bunlar varsa get_pr_changes/browse_repo çağırma" der.
- **Neden:** claude_cli'da her ReAct tool adımı ayrı subprocess (yavaş). Pre-fetch adım sayısını → subprocess sayısını → inceleme süresini düşürür (architect KN-17 pre-fetch deseni).

## KN-32 — Tamamlanma: dry-run rapor vs WI yorumu
- **Nerede:** `12-completion-report` · `flow.py:3259-3261`, `_write_dry_run_report`
- **Karar:** Rapor nereye yazılsın?
- **Girdi:** `state.dry_run`.
- **Sonuç:** Dry-run → `<repo>/.dry_run_<job_id>.md` (diff dahil), WI'ya yorum YOK. Normal → WI'ya "Tamamlanma Raporu" yorumu.
- **Neden:** Dry-run'da remote WI'ya yazmamak; sonucu local incelenebilir bırakmak.

## KN-33 — Geçmiş-iş repo önerisi (advisory)
- **Nerede:** `02-kickoff` (cascade), `03-discover` (prompt+candidate), `05-technical-design` (cascade+context) · `flow.py` + `VectorStore.suggest_repo_from_history`
- **Karar:** Başarılı geçmiş işlerden bu WI'ya benzer olanlar hangi repo(lar)da yapılmış?
- **Girdi:** `CREW_REPO_HISTORY_SUGGEST` açık + `/repo-decisions` scope'unda hybrid arama; repo'ya göre gruplanmış skor vs `CREW_REPO_HISTORY_MIN_SCORE`.
- **Sonuç:** Önerilen repo candidate listesine zorla dahil + context/prompt'a kanıt bloğu; kickoff/technical-design cascade'inde isim eşleşmesi yoksa seçilebilir. **Architect son kararı verir** (advisory).
- **Neden:** "Bu tür dosyalar/route'lar daha önce şu repoda değişti" sinyali repo adı benzerliğinden güçlü; geçmiş başarılı kararlardan öğrenir. Yazma yalnızca başarılı PR'da (KN-32 öncesi step11). İndeks ayrıca iki backfill ile doldurulabilir: (1) bu sistemin MySQL'deki başarılı koşumları (`backfill_repo_decisions`), (2) **Azure DevOps geçmişi** — dashboard board butonu, takımın done+merge'li işlerini tarar (`azure_backfill.AzureBackfillRunner`, `/api/backfill/start`). Filtreler: repo ∉ known_repos elenir, kendi WI'sı önerilmez.

---

## Repo seçim kararlarının birleşik görünümü

Repo seçimi pipeline'da **üç ayrı yerde** olur ve birbirini tamamlar; "geçmiş
işlerden repo önerisi" (KN-33) bu zincirin her üç noktasına da advisory sinyal
olarak eklenmiştir:

| Sıra | Adım | Karar | Bağlayıcı mı? |
|------|------|-------|----------------|
| 1 | `02-kickoff` (KN-11) | Tartışma bağlamı için repo tahmini | Hayır (sadece context) |
| 2 | `03-discover-repos` (KN-14, KN-15) | LLM repo **önerisi** | Hayır (sadece öneri) |
| 3 | `05-technical-design` (KN-17 ön-tahmin, KN-08 çözüm) | Architect **kesin** `repo_name` | **Evet** (`state.repo_name`) |

Üçünün de ortak sinyalleri: `_select_repo_by_name` (isim/parça), kod grep,
`_grep_symbol_evidence` (exclusive symbol), vector search. **Exclusive symbol
kanıtı** her zaman en güçlü; **repo adı benzerliği** en zayıf sinyaldir.

**Geçmiş-iş önerisi (KN-33)** üç noktaya da advisory olarak eklenir: önce başarılı
PR'lar `/repo-decisions` indeksine yazılır, sonra benzer WI'larda o repolar aday
olarak öne çıkar.

---

## KN-35 — WI yaşam döngüsü: durum geçişi yapılsın mı, hangi ada?
- **Nerede:** `01-requirements-analysis` (`_wi_begin`, WI okunduktan hemen sonra; HAL yolunda `hal_planning` başı) · `08-create-pr` (`_wi_transition("review")`) · `main.run_pipeline` except (`wi_lifecycle_on_exception`) · `12-completion-report` (`_wi_transition("handoff")`). Saf mantık: `wi_lifecycle.plan_transition / plan_revert`.
- **Karar:** Bu olayda (`start | review | wait | handoff | revert`) WI'ın `System.State`'i değişsin mi; değişecekse takımın sürecindeki hangi ada?
- **Girdi:** `CREW_WI_LIFECYCLE` (varsayılan kapalı; dry-run ve kickoff-only'de her zaman kapalı) · tipin `workitemtypes/{type}/states` listesi (ad + kategori) · WI'ın mevcut durumu · olay başına tercih listesi (`STATE_PREFS`: start → In Progress/Active/…, review → Code Review/In Review/…, wait → Blocked/On Hold/…, handoff → QA To Do/QA/Ready for Test/Resolved/…) · PR var mı (revert için).
- **Sonuç:** Tercih listesinde süreçte bulunan ilk ad hedef olur. Hedef yoksa / WI zaten hedefteyse / WI **sahiplik aralığı dışındaysa** (Proposed kategorisi + In Progress/Code Review/Blocked dışında: QA, UAT, Preprod, Ready for Production, Done…) → **dokunma**, logla. Azure geçişi reddederse (400) → logla, pipeline devam. Revert yalnızca PR yokken ve yalnızca pipeline'ın koyduğu durumdan başlangıca.
- **Neden:** Pipeline bugüne kadar hiç durum yazmadı; 73121 (job #189) ve 73061 elle taşındı. Adlar sabit kodlanamaz (FLO süreci özel: Task'ta `QA To Do`, User Story'de yok; Agile şablonunda `Active/Resolved`). Sahiplik aralığı, insanın ilerlettiği bir işi geri çekme riskini sıfırlar. Spec: `docs/superpowers/specs/2026-09-10-scrum-functions-design.md`.

## KN-36 — Definition of Done değerlendirmesi
- **Nerede:** `12-completion-report` · `step11_completion_report` → `wi_lifecycle.evaluate_dod / render_dod`
- **Karar:** İş gerçekten "bitti" mi? Hangi maddeler geçti, hangileri kaldı, hangileri doğrulanamadı?
- **Girdi:** `_review_approved(review_text)` · `state.review_issues` açık madde sayısı · `state.build_status` (gate'in yazdığı: succeeded/failed/…/no_pipeline/disabled/skipped) · `parse_uat(uat_text)` (Overall ACCEPTED/REJECTED + madde başına ilk PASS/FAIL) · `all_pushes` yollarında test deseni · `CREW_REQUIRE_TESTS` · `pr_id`.
- **Sonuç:** Her madde ✅/❌/⚪. Zorunlu bir madde ❌ ise **DoD geçilemedi**; ⚪ (doğrulanamadı) bloklamaz, raporlanır. Tablo tamamlanma yorumuna eklenir (`CREW_DOD_CHECKLIST`, varsayılan açık). LLM yok.
- **Neden:** Job #189 `completed` bitti ama UAT raporu REJECTED (AC2 FAIL) idi; terminal sözleşme (review onayı + build yeşil) UAT'ı görmüyordu. Görünürlük önce, zorlama sonra (KN-37).

## KN-37 — DoD geçilemezse iş ne olur?
- **Nerede:** `12-completion-report` · `step11_completion_report`, DoD yorumundan hemen sonra
- **Karar:** DoD ❌ → `needs_human` mı, yine `completed` mı?
- **Girdi:** `CREW_DOD_ENFORCE` (varsayılan kapalı) · DoD sonucu.
- **Sonuç:** Açıksa `db.needs_human_job` + `NeedsHumanReview` (PR açık kalır; KN-35 açıksa WI → Blocked; `handoff` geçişi yapılmaz). Kapalıysa iş `completed`, tablo "zorlama kapalı" notuyla uyarır ve WI `handoff` ile QA'ya devredilir.
- **Neden:** UAT ajanı yalnızca PR diff'ini görür; #189'daki AC2 FAIL, reviewer R1 gibi yanlış pozitif olabilir (ru_RU anahtarları repoda zaten vardı). Zorlamayı açmadan önce birkaç koşuda tablo izlenmeli — "bilmiyorum ≠ geçti" ilkesi ⚪ ile korunur, ama "ajan yanıldı" riski insan kararına bırakılır.

## KN-38 — Story point tahmini: kaynak ve yazma
- **Nerede:** `01-requirements-analysis` (`_estimate("requirements")`, BA çıktısı state'e alınır alınmaz) · `05-technical-design` (`_after_plan_finalized` → `_estimate("plan")`; normal, resume ve HAL yollarının üçünde) · `12-completion-report` (tahmin + gerçekleşen satırı). Saf mantık: `estimation.py`.
- **Karar:** WI kaç story point? Azure'a yazılsın mı?
- **Girdi:** BA JSON `estimate {story_points, confidence, rationale}` (eski cache/bloğu atlayan model → None) · yapısal sinyaller: FR+TR+AC sayısı, plan dosya sayısı, keşif gerekti mi (`_apply_envelope` ile aynı girdiler) · önceki aşamanın değeri · `CREW_ESTIMATE` (açık) · `CREW_WI_WRITE_ESTIMATE` (kapalı) · WI'daki mevcut SP.
- **Sonuç:** nihai = max(BA, yapısal, önceki) → Fibonacci; **yalnızca yükselir**. Loglanır, `jobs.estimate_sp`'ye yazılır. Yazma knob'u açık, dry-run/kickoff-only değil ve WI'da SP **boşsa** sırayla `Custom.StoryPoints` → `Microsoft.VSTS.Scheduling.StoryPoints` → `Effort` yazılır (alan tipte yoksa 400 → sonraki); doluysa "insan tahmini ezilmedi" ve tamamlanma yorumunda takım tahmini ile pipeline tahmini yan yana.
- **Neden:** Takım SP'yi Task seviyesinde ve `Custom.StoryPoints` alanında tutuyor (son 45 gün: 153 Task'ın 150'sinde dolu; `Microsoft…StoryPoints` şablon artığı, değerlerin %99'u 3.0; Effort/OriginalEstimate hiç yok; 73121 = 2 SP, 73061 = 3 SP) ve board/sprint raporu/velocity bu alanı okuyor. Tek kaynağa güvenmemek için BA + yapısal; aşağı düzeltme, zarftaki gibi, yarıda kalan işi "küçük" gösterme riski taşır.

## KN-39 — Plandan alt iş kaydı (child Task) açılsın mı?
- **Nerede:** `05-technical-design` (`_after_plan_finalized` → `_create_child_tasks`) · `07-implement-code` (`_complete_child_task`, üç push yolunda: gerçek push, resume-branch, skip-exists). Saf mantık: `wi_children.py`.
- **Karar:** Plan değişiklikleri parent WI'ın altında child Task olsun mu; hangi child ne zaman kapanır?
- **Girdi:** `CREW_WI_CHILD_TASKS` (kapalı) · WI tipi (yalnızca User Story/Bug/Feature/Epic/Improvement) · plan `changes[]` · `CREW_WI_CHILD_TASKS_MAX` (8) · parent'ta `crew-generated` etiketli child var mı · parent alan/iterasyon.
- **Sonuç:** Task tipi WI → hiçbir şey (takımın olağan durumu). Parent tipi → değişiklik başına child (`[repo] Düzenle Dosya.php — açıklama`, etiket `crew-generated`, alan/iterasyon kopyası); max aşılırsa dizine göre grup. Üretilmiş child varsa **yeniden açmaz**, mevcutları bağlar. Dosya push edilince ilgili child `Done` (tipin sürecinde `complete` tercihi). Dry-run/kickoff-only → kapalı.
- **Neden:** Takımda hiyerarşi User Story → Task (%73 parent'lı). Task altına Task board'u kirletir; etiket + idempotency retry/resume'da çift kayıt riskini kaldırır.

## KN-40 — Retrospektif: sonuç sınıflandırması ve kural önerisi eşikleri
- **Nerede:** pipeline dışı · `retrospective.py` (`classify_outcome`, `analyze`, `suggest_rules`, `suggest_config`) · `GET /api/retro` · dashboard 🔁 Retro.
- **Karar:** Bir iş neden bitmedi / nereye kadar geldi; hangi tekrarlayan neden bir kılavuz kuralına dönüşmeli?
- **Girdi:** `jobs.status` + `error_message` anahtar kelimeleri (NEEDS_INFO, Review, build, DoD, sunucu yeniden, plan-push, bütçe…) · `job_steps.status` · `review_pr_task` çıktısındaki "N düzeltme turu" / REVIEW_DECISION · `pr_build_gate` çıktısı · `uat_task` → `parse_uat` · `jobs.estimate_sp` · pencere: sprint WI'ları (`get_iteration_work_items`) ya da son N gün. kickoff-only ve dry-run işler dışarıda.
- **Sonuç:** Deterministik rapor (özet tablosu, nedenler, kırılan adımlar, kalite kapıları, SP başına dk/$, tekrar koşan WI'lar, en pahalı işler). Kural önerileri yalnızca eşik aşıldığında: needs_info ≥ %20 → AC türetme kuralı; review ilk-tur onay < %50 → mevcut testleri oku kuralı; build kırmızı ≥ 1 → test dosyası plana; UAT red ≥ 1 → repo kanıtı; teknik tasarım ≥ 3 kırılma → gerçek dosya oku; tekrar koşan WI → önceki yorumları oku. Kurallar **insan onayıyla** (`＋ Ekle` → `POST /api/kickoff-guidance`, `source_wi=retro`) kılavuza girer; pipeline kendi kuralını yazmaz.
- **Neden:** Retrospektifin değeri sayıların tartışılmaz olmasında; LLM özeti yerine doğrudan kayıt. Öğrenmenin akacağı yer zaten var (`kickoff_guidance`), eksik olan sprint sonu bakışı ve öneriyi üreten mekanizmaydı.

## KN-41 — Sprint planı: hangi WI pipeline adayı, hangi sırada, kaç tanesi?
- **Nerede:** pipeline dışı · `sprint_planning.py` (`classify_candidates`, `order_candidates`, `select_by_capacity`, `effort_estimate`, `queue_selected`) · `GET /api/sprint-plan`, `POST /api/sprint-plan/queue` · dashboard 🗓️ Planla.
- **Karar:** Sprintin iş kalemlerinden hangileri pipeline'a verilebilir; kapasite kadar hangileri ön-işaretlenir; toplu kuyruğa alma güvenli mi?
- **Girdi:** `get_iteration_work_items` (durum, tip, öncelik, `Custom.StoryPoints`) · tipin süreç durumları (Proposed kategorisi; yoksa ad tabanlı yedek) · WI'nın en son işi (`jobs`: queued/running → açık; completed → yapıldı; needs_info/needs_human → insan bekliyor; failed → yeniden denenebilir) · kapasite (kullanıcı; yoksa takımın son 3 sprint Done SP ortalaması — Analytics) · retrospektif ortalamaları (SP başına $/dk, iş başına $/dk).
- **Sonuç:** Uygun = Proposed durum ∧ tip ∈ {Task, Bug, User Story, PBI, Improvement} ∧ açık/tamamlanmış/bekleyen iş yok. Sıra: öncelik ↑, SP ↑ (0 = bilinmiyor sona), id ↑. Kapasite varsa kümülatif SP aşmadan ön-işaret (SP'siz iş kapasiteyi tüketmez, uyarı alır). Kuyruğa alma yalnızca insan butonuyla; açık işi olan WI atlanır (çift kuyruk yok).
- **Neden:** Board salt okunurdu, işler tek tek elle giriyordu. Karar insanın (checkbox), sistem sırayı/toplamı/tahmini gösterir; "başlanmış" durumlardaki WI'lara dokunulmaz (Faz 1 sahiplik ilkesi).

## KN-42 — Günlük özet: ne girer, nereye gider?
- **Nerede:** pipeline dışı · `daily.py` (`collect`, `render_markdown`, `publish_daily`, `start_scheduler`) · `GET /api/daily`, `POST /api/daily/send` · dashboard ☀️ Günlük · sunucu startup zamanlayıcısı.
- **Karar:** Son N saatin hangi hareketleri özetlenir; özet nereye yazılır/gönderilir?
- **Girdi:** `jobs` (finished_at penceresi; running; queued) · **açık engeller** = WI'nın en son işi needs_info/needs_human (retry edilmemiş) · `CREW_DAILY_ENABLED` (knob, kapalı) · env `CREW_DAILY_TIME` (09:00), `CREW_DAILY_DIR`, `CREW_DAILY_TELEGRAM_TOKEN/CHAT_ID`.
- **Sonuç:** Markdown: özet satırı (biten/koşan/kuyruk/maliyet), Bitenler (durum, WI linki, PR, SP, dk, $; hata özeti), Şu an koşan (adım + süre), Kuyruk, İnsan bekleyenler (kaç gün). Dosyaya her zaman; Telegram yalnızca token+chat varsa (düz metin). Zamanlayıcı bayrağı her turda okur → dashboard'dan kapatınca susar. kickoff-only/dry-run işler dışarıda.
- **Neden:** Daily Scrum'ın sorusu "ne bitti, ne koşuyor, ne engel var" — hepsi DB'de vardı, kimse okumuyordu. Dışa gönderim yaptığı için zamanlayıcı varsayılan kapalı; anlık özet her zaman açık.

## KN-43 — İş tipine göre akış: Bug / Story / Spike
- **Nerede:** `01-requirements-analysis` (`_wi_begin` → `state.flow_kind`) · `_build_step_context` (her adımın context'ine tip kılavuzu) · `05-technical-design` (spike dalı, B-first plan üretiminden **önce**: `_run_spike` → `_SpikeStop`) · `12-completion-report` (Bug'da DoD test zorunlu). Saf mantık: `type_flow.py`.
- **Karar:** Bu WI hangi akış türünde (`bug | story | task | spike | other`) ve bu türe göre ne değişir?
- **Girdi:** `System.WorkItemType`, `System.Tags`, `System.Title` · `CREW_TYPE_FLOW` (açık). Spike **yalnızca açık işaretle**: tip Spike/Research, etiket `spike|poc|research|araştırma` (etiket listesi içinde tam kelime), ya da başlık `[Spike] …` / `Spike: …` / `POC - …`. Başlıkta geçen "spike" kelimesi (ör. "trafik spike'ı") spike DEĞİLDİR.
- **Sonuç:** Bug → context'e "reproduce-first, regresyon testi zorunlu, minimal fix, reviewer testsiz düzeltmeyi reddeder, UAT komşu senaryo" kılavuzu; DoD'da test dosyası zorunlu. Story → "her değişiklik bir AC'ye izlenir, dikey dilim, kullanıcıya görünen değişiklik Türkçe yazılır". Spike → plan üretilmez; klon varsa bir kez mimar keşfi, `spike_report` (kapsam + bulgular + açık sorular + takip tahmini) WI yorumu, `technical_design_task` done, kalan 8 adım "Atlandı — spike", `_SpikeStop` (`_KickoffOnlyStop` alt sınıfı → job completed). Task/other → değişiklik yok.
- **Neden:** Tip okunuyor ama kullanılmıyordu; Bug ve Story aynı talimatı alıyordu, araştırma işi kod üretmeye zorlanıyordu. Kılavuz `parts` sonuna eklenir (iş-değişmezi) → prompt cache prefix'i bozulmaz.

## KN-44 — Product Owner değerlendirmesi (danışma)
- **Nerede:** `01-requirements-analysis` sonu (`_po_assessment`, hazırlık kapısı geçildikten sonra, kickoff'tan önce) · `crew.create_po_crew` (tool'suz PO ajanı, `po_assessment_task`) · `type_flow.parse_po / render_po_comment`.
- **Karar:** İşin iş değeri, aciliyeti, önceliği (P1-P4) ve GO/HOLD; kapsam kararları.
- **Girdi:** `CREW_PO_ASSESSMENT` (kapalı; tek LLM çağrısı ~$0.1-0.3) · WI + BA analizi (context) · spike değil · kickoff-only değil.
- **Sonuç:** JSON parse → `state.po_json`, ham metin `state.po_text` → kickoff/teknik tasarım context'ine "PO Değerlendirmesi" bloğu (requirements adımına girmez) + WI yorumu (tablo: değer/aciliyet/öncelik/karar, kapsam kararları, gecikme riski, gerekçe). **HOLD kapı değil**: loglanır, pipeline devam eder. Parse edilemezse ham metin context'e, yorum yazılmaz.
- **Neden:** Yedi ajanda değer/öncelik kararı veren rol yoktu. Kapı yapılmadı çünkü PO'nun HOLD'u insan kararı gerektirir; ilk koşularda yorumun kalitesi izlenip sonra gate'e (needs_human) bağlanabilir (faz 5.1).

## KN-45 — PR inceleme katkısı: insanın PR'ına danışma review'ı
- **Nerede:** pipeline dışı · `pr_review.py` (`resolve_pr`, `build_pr_context`, `run_pr_review`) · `POST /api/pr-review` · board kartı 🔍 (başlanmış durumlar: In Progress, Code Review, QA To Do, QA, UAT, Preprod Check, Active, Resolved).
- **Karar:** Sahiplik kuralı gereği dokunulmayan (başlanmış) bir WI'ın PR'ına pipeline nasıl katkı verir; hangi PR, ne yazılır, ne yazılmaz?
- **Girdi:** `CREW_PR_REVIEW` (açık; insan butona basar, otomatik tetik yok) · WI relations'daki PR bağlantıları (en yeni **aktif**; yoksa en son tamamlanmış; abandoned atlanır) · PR'ın son iteration'ındaki değişen dosyalar (silinenler ve klasörler hariç; en çok 12 dosya × 6000 karakter, feature branch içeriği) · WI başlık/açıklama/AC düz metin (BA çağrısı yok) · `code_reviewer` ajanı, `review_pr_task` (aynı prompt; context'e "İNCELEME KAPSAMI: danışma" notu).
- **Sonuç:** Tek reviewer çağrısı → karar (`_review_approved/_review_rejected`) + `REVIEW_ISSUES_JSON` maddeleri. PR'a genel özet yorumu (karar, madde tablosu, "danışma" notu), blocker/major maddeler için en çok 8 dosya/satır yorumu, WI'a aynı özet. **Kod değişmez, push yok, oy yok, WI durumu değişmez, retry/düzeltme döngüsü yok.** İş kaydı `jobs.job_kind='pr_review'`; review metni `review_pr_task` adımına yazılır; maliyet `llm_calls`'a job_id ile. Retro / sprint planı / günlük "bekleyen" sorguları `job_kind='pipeline'` filtreler → inceleme işi WI'ı "tamamlandı" saydırmaz.
- **Neden:** Kullanıcı: "ilerlemiş işlere müdahale edemiyoruz ama review sürecine katkı sağlayacak bir alan açalım." Sahiplik ilkesi (KN-35/41) korunur; katkı yalnızca review sürecine, insan tetikli ve geri alınabilir (yorum).

## KN-46 — Backlog refinement: Definition of Ready (pipeline dışı, LLM'siz)
- **Nerede:** pipeline dışı · `backlog_refinement.py` (`fetch_candidates`, `assess`, `build_report`, yazma eylemleri, `generate_questions`) · `GET /api/refinement`, `POST /api/refinement/action` · dashboard **Refinement** sekmesi + Genel bakış sprint kartında "hazır N/M" satırı.
- **Karar:** Backlog'daki (Proposed: Backlog / To Do / New) bir iş sprint'e girmeye hazır mı; değilse ne eksik, SP önerisi ne, hangi sorular sorulmalı?
- **Girdi:** `CREW_BACKLOG_REFINEMENT` (açık) · kapsam: seçili sprint (`IterationPath =`) ya da takımın alan yolu altındaki tüm Proposed işler (`AreaPath UNDER`, `get_team_area_path`) · tip User Story / PBI / Bug / Task / Improvement · Proposed durum adları tipin süreç listesinden (`category == Proposed`), yoksa ad tabanlı yedek · WI alanları: açıklama, kabul kriteri, repro adımları, SP (Custom.StoryPoints → StoryPoints → Effort), öncelik, etiket, oluşturma tarihi, ebeveyn ilişkisi · eşik `CREW_REFINEMENT_MIN_SCORE` (70), bayatlama `CREW_REFINEMENT_STALE_DAYS` (30), kısa açıklama eşiği `CREW_MIN_WI_CONTENT_CHARS`.
- **Sonuç:** Her iş için ceza listesi ve skor 0-100: açıklama yok −30 / kısa −15; kabul kriteri yok −25 (Bug'da repro adımı yok −25; spike'ta araştırma sorusu yok −25, AC aranmaz); SP yok −10; SP > 8 "bölünmeli" −10; Task/Bug'da ebeveyn yok −10; öncelik yok −5; başlık < 15 karakter −5; bayat −5. Skor ≥ eşik → hazır. SP yoksa `estimation.structural_estimate` ile AC sayısından öneri (spike hariç). Rapor: satırlar (hazır olmayan önce, öncelik, skor) + özet (toplam/hazır/eksik, SP toplam, SP'siz sayısı, eksik sayaçları, tipe göre). **Salt okunur.** Yazma eylemleri (`CREW_REFINEMENT_WRITE`, kapalı) yalnızca insan tıklamasıyla ve tek WI için: eksikleri Türkçe yorum (`*Tempo — Backlog Refinement*` imzası, `is_bot_comment` tanır), boşsa önerilen SP (insan tahmini ezilmez), `needs-refinement` etiketi ekle/kaldır (diğer etiketler korunur). LLM (`CREW_REFINEMENT_LLM`, kapalı): İş Analisti tool'suz tek çağrı (`refinement_questions_task`) → 3-6 netleştirme sorusu + taslak AC (Türkçe JSON); sekmede gösterilir, WRITE açıksa yorum olur; iş kaydı `job_kind='refinement'` (retro/plan/günlük saymaz). **Durum değişmez, kuyruğa alınmaz, toplu yazma yok.**
- **Neden:** Hazırlık skoru yalnızca pipeline içinde ve ücretli BA çağrısından sonra hesaplanıyordu (KN-30); sprint planlama adayları sıralıyor ama hazırlığa bakmıyordu. Refinement töreni için sıfır maliyetli, deterministik bir ön kontrol gerekiyordu; LLM ve yazma insan kararıyla açılır.
