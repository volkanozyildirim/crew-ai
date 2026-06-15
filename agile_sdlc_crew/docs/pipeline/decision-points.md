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
- **Nerede:** `09-code-review` · `_review_retry_loop` `flow.py:729-880`, `flow.py:3007-3035`
- **Karar:** RED sonrası tekrar geliştirme yapılsın mı, kaç kez?
- **Girdi:** `_review_attempt` vs `CREW_REVIEW_MAX_RETRIES` (default 2).
- **Sonuç:** Limit altında → reviewer'ın bahsettiği dosyaları yeniden implement+push+review. Limit aşılırsa WI'ya hata yorumu + `RuntimeError`.
- **Neden:** Otomatik düzeltme döngüsü; sonsuz döngüyü ve fake-APPROVE'u önlemek.

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
- **Neden:** "Bu tür dosyalar/route'lar daha önce şu repoda değişti" sinyali repo adı benzerliğinden güçlü; geçmiş başarılı kararlardan öğrenir. Yazma yalnızca başarılı PR'da (KN-32 öncesi step11). Filtreler: repo ∉ known_repos elenir, kendi WI'sı önerilmez.

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
