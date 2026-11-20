# Taban imaj şablonu + yama sürümü çözümü — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Sürüm yükseltmede "imaj yayınlanmamış" engelini Tempo'nun kendisi ilerletsin: kendi taban imajımız eksikse `base-images` reposuna şablon PR'ı açsın; üç parçalı (yama sürümlü) etiketleri elle düzenleme yerine hedefin güncel yama sürümüyle yazsın.

**Architecture:** Docker Hub'dan yama sürümü / etiket varlığı okuyan küçük bir çözücü (`quality/upgrade_registry.py`); `upgrade.py` site üretimi bu çözücüyle üç parçalı etiketleri ve genel (library) imajları çözer; ayrı bir modül (`quality/upgrade_image.py`) eksik taban imaj dizinini en yakın alt sürümden türetip taslak PR açar; durum, uç ve kart buna bağlanır. Hiçbir şey saklanmaz: şablonun durumu `base-images` dalı + sabit adlı PR'dan türetilir.

**Tech Stack:** Python 3, requests, FastAPI, tek dosyalık `web/index.html`, testler `tests/test_katman0_gates.py` (`check()`), Azure DevOps REST.

**Spec:** `docs/superpowers/specs/2026-11-17-language-upgrade-design.md` — "Ek (2026-11-18): taban imaj şablonu ve yama sürümü" bölümü.

## Global Constraints

- Yeni davranış varsayılan KAPALI: `CREW_TECHDEBT_UPGRADE_PATCH` (off) ve `CREW_TECHDEBT_UPGRADE_IMAGE` (off). Kapalıyken bugünkü çıktı bayt bayt aynı kalır (mevcut testler değişmeden geçer).
- Bilinmeyen her şey kapalı kalır (fail-closed): Docker Hub okunamadı, yama sürümü bulunamadı, etiket yok, sürüm satırı tek değil → site bugünkü gibi `CI_HAND` / `UNPUBLISHED` kalır, şablon PR'ı açılmaz.
- Docker Hub'a yalnız genel uç okunur (`https://hub.docker.com/v2/repositories/library/<ad>/tags…`), kimlik/özel veri gönderilmez, `timeout=10`, `User-Agent: tempo-upgrade`. Yanıt güvenilmez girdidir: etiketler `^\d+\.\d+\.\d+$` / `[\w.\-]+` ile doğrulanır.
- DevOps işleri TR + global + RU'yu birlikte kapsar: şablon PR açıklaması bekleyen servisleri bölge reposu başına listeler ve her bölgenin Jenkins bayrağını yazar (anatolia→`mainLocation`, global→`globalLocation`, russia→`yandexLocation`).
- `push_sites` kuralları aynen geçerli (yalnız Tempo'nun dalı, sabitlenmiş objectId, ham bayt, tek satırda yalnız rakam dizileri). Şablon PR'ı ayrı yoldan gider ama aynı ilkeler: dal `tempo/base-image-<dizin>`, var olan dala asla yazılmaz, taslak PR.
- Testler Azure'a, MySQL `jobs` tablosuna ve ağa (Docker Hub dahil) ASLA gitmez: sahte client, `fetch=` enjeksiyonu.
- Kullanıcının gördüğü her metin Türkçe.
- Node kapsam dışı. Go'nun taban imajı yok (yalnız yama sürümü çözümü).

## Review Focus

- Docker Hub yanıtı bozuk / boş / 500 / zaman aşımı → hiçbir site "itilebilir" olmaz.
- `golang:1.24.4-alpine` → hedef yama sürümünde `-alpine` etiketi yoksa site `CI_HAND` kalır (etiket uydurulmaz).
- Aynı dizin için iki bölge farklı etiket isterse (`:1.0` ve `:1.1`) şablon PR'ı açılmaz, neden gösterilir.
- Terk edilmiş (abandoned) şablon PR'ı yeniden açılmaz; açık PR varken ikinci PR açılmaz; dizin hedef dalda zaten varsa PR açılmaz.
- `tag.txt` satır sonu içermez (Jenkins ham okur, satır sonu etiketi bozar).

---

### Task 1: Yama sürümü çözücü + üç parçalı etiketler

**Files:**
- Create: `src/agile_sdlc_crew/quality/upgrade_registry.py`, `src/agile_sdlc_crew/config/upgrade_image.yaml`
- Modify: `src/agile_sdlc_crew/quality/upgrade.py`, `src/agile_sdlc_crew/quality/upgrade_track.py` (`_PLAN_KW` += `"resolver"`), `src/agile_sdlc_crew/pipeline_config.py`
- Test: `tests/test_katman0_gates.py` → `test_upgrade_registry`, `test_upgrade_patch_sites`

**Interfaces — Produces:**
- `upgrade_registry.Resolver(fetch=None, patches: dict | None = None, ttl_s=3600)` with
  - `latest_patch(name: str, minor: str) -> str | None` — `name` ∈ `php|python|golang`; `("php","8.5") → "8.5.12"`. Önce `patches[name][minor]` (yaml `patch:`), yoksa Hub: `GET …/library/<name>/tags?name=<minor>.&page_size=100`, `results[].name` içinden tam `^<A>\.<B>\.(\d+)$` eşleşenlerin en büyüğü. Hata / eşleşme yok → `None`.
  - `tag_exists(name: str, tag: str) -> bool | None` — `GET …/library/<name>/tags/<tag>`: 200 → True, 404 → False, diğer / hata → None. `tag` `^[\w][\w.\-]*$` değilse None.
  - `fetch(url) -> (status:int, json:dict|None)` enjekte edilebilir; varsayılan `requests.get`. Sonuçlar (olumsuz dahil, None hariç) `ttl_s` süresince bellekte.
- `upgrade_registry.default() -> Resolver` (modül tekili, `config/upgrade_image.yaml` `patch:` ile).
- `config/upgrade_image.yaml` (Task 2 de okur):
  ```yaml
  repo: base-images
  project: FLODevOps
  branch: staging
  regions: {anatolia: mainLocation, global: globalLocation, russia: yandexLocation}
  patch: {}            # elle sabitleme, ör. {php: {"8.5": "8.5.12"}}
  fixes:
    php:
      "8.5":
        - drop: 'docker-php-ext-(install|enable)\s+opcache\b'
          note: "Opcache 8.5'te çekirdeğe gömülü; kurulum adımı hata verir."
  ```
- `upgrade.image_for_full(image: str, lang: str, full: str) -> str` — `golang:1.24.4-alpine`,`1.27.3` → `golang:1.27.3-alpine`; `…/python-3-13-7:1.0`,`3.14.2` → `…/python-3-14-2:1.0`; eşleşme yoksa aynen.
- `upgrade.is_library_image(image: str, lang: str) -> bool` — kayıt deposu öneki olmayan dil imajı (`golang:…`, `python:…`, `php:…`; `docker.io/library/` öneki de sayılır).
- `plan(..., resolver=None)`; `resolver=None` ve `CREW_TECHDEBT_UPGRADE_PATCH` açıksa `upgrade_registry.default()`, kapalıysa çözücü yok. `scan`/`status` aynı anahtarı `plan`'a geçirir.

**Davranış (çözücü varken; yokken bugünkü davranış):**
- `_devops_sites` ve `_code_sites` (Dockerfile `FROM`), `_ci_image_pin`: imaj üç parçalıysa `full = resolver.latest_patch(<ad>, to)`; `new = image_for_full(img, lang, full)`. `full` yok → bugünkü `CI_HAND`, `old == new`.
- Engelsiz sayılma (devops + `selectable.published` aynı yüklem, `_image_ok(..., resolver=)`):
  - yeni imaj aynı bölge reposunda kullanılıyorsa → engelsiz (bugünkü kural, artık üç parçalıda da);
  - değilse ve `is_library_image(new)` ve `resolver.tag_exists(<ad>, <new'in etiketi>) is True` → engelsiz;
  - değilse: kendi imajımız → `UNPUBLISHED`; library imajı ve etiket yok/bilinmiyor → üç parçalıda `CI_HAND` (ve `new = old`), iki parçalıda `UNPUBLISHED`.
- İki parçalı library imajı (`python:3.11-slim` → `python:3.13-slim`) da aynı Hub kanıtıyla engelsiz olur.
- Kod reposu Dockerfile'ı ve CI `image:`: library imajında etiket Hub'da varsa `new` yazılır; yoksa `CI_HAND`.
- `_ci_value_pin` (GoTool `version: '1.24.4'`, `versionSpec`): üç parçalı değer → `resolver.latest_patch(<lang adı>, to)` ile `'1.27.3'`; yok → `CI_HAND`. (`php`→`php`, `python`→`python`, `go`→`golang`.)
- `pipeline_config.py`: `CREW_TECHDEBT_UPGRADE_PATCH` (bool, default off) — açıklama: "Üç parçalı (yama sürümlü) etiketleri ve genel imajları Docker Hub'dan doğrulayıp otomatik yazar; kapalıyken bu satırlar elle düzenlenir."

**Tests (sahte `fetch`, ağ yok):**
- `test_upgrade_registry`: en büyük yama (`8.5.2, 8.5.12, 8.5.9-fpm` → `8.5.12`); yaml `patch` Hub'a gitmeden döner; 500 / bozuk JSON / istisna → `None`; `tag_exists` 200/404/500 → True/False/None; önbellek ikinci çağrıda `fetch` çağırmaz; geçersiz etiket → None, `fetch` çağrılmaz.
- `test_upgrade_patch_sites` (sahte client + `resolver=`): `golang:1.24.4` devops sitesi → `golang:1.27.3`, engelsiz, `upgrade_bump.pushable` True; `golang:1.24.4-alpine` ve Hub'da `1.27.3-alpine` yok → `CI_HAND`, `old == new`; `…/prod-ar/python-3-13-7:1.0` → `…/python-3-14-2:1.0` + `UNPUBLISHED`; aynı imaj bölgede kullanılıyorsa engelsiz; `python:3.11-slim` → `python:3.13-slim` Hub'da var → engelsiz, yok → `UNPUBLISHED`; GoTool `'1.24.4'` → `'1.27.3'`; `latest_patch` None → hepsi bugünkü gibi; `resolver=None` → mevcut `test_upgrade_plan_sites` beklentileri değişmez; `selectable.published` site yüklemiyle aynı.

Commit: `feat(techdebt): yama sürümü çözücü — üç parçalı etiketler ve genel imajlar Docker Hub kanıtıyla`

---

### Task 2: Taban imaj şablonu (`upgrade_image.py`)

**Files:**
- Create: `src/agile_sdlc_crew/quality/upgrade_image.py`
- Test: `tests/test_katman0_gates.py` → `test_upgrade_image_template`

**Interfaces — Consumes:** `upgrade_registry.Resolver`, `config/upgrade_image.yaml`, `upgrade.UNPUBLISHED`, client: `get_items_in_path(repo, path, branch=, recursion_level=, project=)`, `get_file_bytes`, `list_branches(repo, project=)`, `create_branch(..., source_object_id=)`, `push_changes(..., old_object_id=)` (base64 `contentType`), `create_pull_request(..., draft=True, project=)`, `find_pull_request(repo, branch, project=)`.

**Interfaces — Produces:**
- `load_cfg(path=CFG_PATH) -> dict`
- `needed(sites: list[dict], lang: str) -> list[dict]` — `where == "devops"` ve `blocked == UNPUBLISHED` ve `new` kendi taban imajımız (`…/<ad>-A-B(-C)?:<etiket>`, kayıt deposu önekli) olan siteler dizin adına göre gruplanır: `{"dir": "php-8-5", "tag": "1.0", "regions": ["anatolia", "russia"], "sites": [...], "error": ""}`; aynı dizin için farklı etiketler → `error = "bölgeler farklı etiket istiyor: 1.0, 1.1"`. Go / library imajları → `[]`.
- `branch_name(dir_name) -> "tempo/base-image-<dir>"`
- `state(client, entry, *, cfg=None) -> dict` — `{"dir","tag","regions","state","reason","pr"}`; `state` ∈
  - `"şablon var"` — dizin `cfg.branch` dalında var (reason: "DevOps imajı üretip yayınlamalı: <bölge → bayrak>");
  - `"pr açık"` / `"pr reddedildi"` — sabit dalın PR'ı active / abandoned;
  - `"yok"` — dizin yok, PR yok (ya da dal yok);
  - `"belirsiz"` — `entry.error`, PR completed ama dizin yok, ya da okuma hatası (reason dolu). Yalnız `"yok"` PR açmaya izin verir.
- `build(client, lang: str, to: str, entry: dict, *, cfg=None, resolver=None) -> dict` → `{"source": "php-8-4", "full": "8.5.12", "files": {"/php-8-5/Dockerfile": bytes, "/php-8-5/tag.txt": b"1.0"}, "notes": [str]}`; `ImageTemplateError` (Türkçe mesaj) fırlatır.
  - Kaynak: `cfg.branch` kökünde `^<ad>-(\d+)-(\d+)(?:-(\d+))?$` dizinlerinden hedeften küçük en büyük sürüm; yoksa hata.
  - Tam sürüm: dizin adı üç parçalıysa addan (`python-3-14-2`), değilse `resolver.latest_patch(<ad>, to)`; yoksa hata.
  - Kaynak dizinin bütün dosyaları ham bayt kopyalanır (dosya adı korunur — `php-8-2/dockerfile` küçük harf).
  - Sürüm satırı (Dockerfile, büyük/küçük ad): PHP → tam bir `^ARG PHP_VERSION=\d+\.\d+\.\d+\s*$`; Python → tam bir `^FROM python:(\d+\.\d+\.\d+)(-\S+)?\s*$`. Tam bir değilse hata. PHP'de `FROM php:${PHP_VERSION}<sonek>` satırından sonek alınır. `resolver.tag_exists(<ad>, "<tam><sonek>") is True` değilse hata ("Docker Hub'da … etiketi yok / doğrulanamadı").
  - `cfg.fixes[lang][to]`: her `drop` regex'i eşleşen satırları çıkarır; eşleşen her satır `\` ile bitmeli (RUN zincirinin ortası), aksi halde hata; hiç eşleşmezse `notes`'a "düzeltme uygulanmadı: …"; eşleşirse `note` metni `notes`'a.
  - `tag.txt` = `entry.tag` baytları, satır sonu YOK.
- `open_pr(client, lang, to, entry, *, cfg=None, resolver=None, repo: str = "") -> dict` — modül kilidi altında: `state` yeniden okunur (`"yok"` değilse o durumla döner, yazma yok); `cfg.branch` baş objectId sabitlenir; `create_branch(source_object_id=)`; tek push (hepsi `add`, base64, `old_object_id=`); taslak PR → `cfg.branch`. Dönen: `{"state": "pr açık", "pr": {"id","url"}, "notes": [...]}`.
  - Başlık: `<dizin> taban imaj şablonu (<Dil> <tam>)`.
  - Açıklama (≤ 4000): imajın üretilmediği; kaynak dizin + yapılan değişiklikler (`notes`); "derleme doğrulanmadı"; **Yayın** bölümü: `baseImage=<dizin>` ve bekleyen her bölge için bayrak + o bölgedeki site yolları (russia varsa ayrıca "RU registry'sine itilmezse russia servisleri geçemez" cümlesi); isteyen repo (`repo`).

**Tests (sahte client, kayıt tutan):** `needed` gruplama + etiket çatışması + library imajı hariç; `state` beş durum; `build` PHP (8.4 kaynağından 8.5: `ARG` satırı değişti, opcache satırları yok, `tag.txt == b"1.0"`, diğer dosyalar bayt eşit, küçük harfli `dockerfile` adı korunur) ve Python (`python-3-13-18` → `python-3-14-2`, `requirements.txt` kopyalandı); hata yolları: kaynak yok, iki sürüm satırı, `latest_patch` None, `tag_exists` False/None, `drop` satırı `\` ile bitmiyor; `open_pr`: `"yok"` → bir branch + bir push + bir taslak PR (hedef `staging`, sabit objectId, açıklamada `yandexLocation` ve russia yolu); `"pr açık"` / `"pr reddedildi"` / `"şablon var"` → hiç yazma yok; ikinci çağrı ikinci PR açmaz.

Commit: `feat(techdebt): yayınlanmamış taban imaj için base-images şablon PR'ı`

---

### Task 3: Bağlama — durum, uç, otomatik ilerletme, kart, belgeler

**Files:**
- Modify: `src/agile_sdlc_crew/pipeline_config.py`, `quality/upgrade_track.py`, `server.py`, `web/index.html`, `CLAUDE.md`
- Test: `tests/test_katman0_gates.py` → `test_upgrade_image_wiring`

**Interfaces — Consumes:** Task 2'nin `needed` / `state` / `open_pr`.

**Davranış:**
- `CREW_TECHDEBT_UPGRADE_IMAGE` (bool, default off): "Yayınlanmamış taban imaj için base-images reposuna şablon PR'ı açar (imajı üretmez; DevOps yayınlar)."
- `upgrade_track._status` çıktısına `images: list[dict]` (her biri `upgrade_image.state` sonucu). Yalnız ayar açıkken ve taramada `UNPUBLISHED` devops sitesi varken hesaplanır; aksi halde `[]`. Okuma istisnası → `[{"state": "belirsiz", "reason": …}]`, durum hesabı düşmez. `_blocked_dv_reason` metnine imaj durumu eklenir (ör. "… — php-8-5: şablon PR'ı #46551 açık").
- `POST /api/techdebt/upgrade/image` gövde `{repo, lang, to, team, dir}`: `_upgrade_step_check` + ayar kapalıysa 403; taze durumdan `dir`'i bulur (yoksa 404), `state != "yok"` → 409 + neden; `open_pr` sonucu döner. İzleme Task'ı varsa PR bağlantısı yorum olarak düşülür (mevcut `_comment` yardımcıları).
- Otomatik ilerletme (`_advance_one`): ayar + `_AUTO` açıkken `state == "yok"` olan her imaj için `open_pr` bir kez; sonuç `res` notuna yazılır. İdempotentlik PR'ın kendisidir (sabit dal); hata izleme Task'ına mevcut hata yorumu yoluyla gider, adım akışını durdurmaz.
- Kart (`web/index.html`, yükseltme modu, aşama şeridinin altı): her imaj için satır — dizin, bölgeler, durum, PR bağlantısı; `state == "yok"` iken **"Şablon PR'ı aç"** düğmesi (onay kutusu: hangi repoya, hangi dala, bekleyen bölgeler). İstek sürerken düğme kapalı, bitince tarama yenilenir.
- `CLAUDE.md` "Language version upgrade" maddesine iki ayar, iki modül, RU kuralı ve "An `imaj yayınlanmamış` site does not open a DevOps image request yet" cümlesinin güncellenmesi. Spec ekinin "uygulandı" notu.

**Tests:** ayar kapalı → `images == []` ve `open_pr` çağrılmaz; açık + UNPUBLISHED site → `images` dolu; `state` istisnası → `belirsiz`, `status` düşmez; `_advance_one` `"yok"` → `open_pr` bir kez, `"pr açık"` → hiç; uç: kapalı 403, bilinmeyen dizin 404, `"pr açık"` 409, başarılı 200 (enjekte `open_pr`).

Commit: `feat(techdebt): imaj şablonu adımı karta, uca ve otomatik ilerletmeye bağlandı`
