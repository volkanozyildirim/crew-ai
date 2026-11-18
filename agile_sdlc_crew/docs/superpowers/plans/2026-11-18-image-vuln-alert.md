# Taban imaj güvenlik uyarısı — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Üretim servislerinin kullandığı taban imajlarda bilinen (düzeltmesi yayınlanmış) güvenlik açığı, geride kalmış yama sürümü ya da desteği bitmiş sürüm varsa Tempo kendiliğinden göstersin ve uyarsın.

**Architecture:** Tek modül `quality/image_vuln.py`: envanter (FLODevOps `FROM` satırları → dayanak resmî imaj) → üç işaret (Trivy taraması, yama geride, destek bitmiş) → imaj başına sonuç, dosya önbelleği. Sunucuda bir okuma ucu + günlük yoklayıcı; Teknik borç sekmesinde bir kart; isteğe bağlı iş açma ve Telegram. Yayınlanmış imajın kendisi taranmaz (registry erişimi yok) — sonuç alt sınırdır ve kart bunu söyler.

**Tech Stack:** Python 3, `subprocess` (Trivy; kabuk yok), requests (endoflife.date, Docker Hub — `upgrade_registry.Resolver`), FastAPI, `web/index.html`, testler `tests/test_katman0_gates.py`.

**Spec:** `docs/superpowers/specs/2026-11-17-language-upgrade-design.md` — "Ek (2026-11-18): taban imaj güvenlik uyarısı".

## Global Constraints

- Varsayılan KAPALI: `CREW_TECHDEBT_IMAGE_VULN` (off; `CREW_TECHDEBT` da gerekir), `CREW_TECHDEBT_IMAGE_VULN_SCAN_H` (24), `CREW_TECHDEBT_IMAGE_VULN_TASK` (off), `CREW_TECHDEBT_IMAGE_VULN_NOTIFY` (off). `CREW_TRIVY_BIN` yalnız ortam değişkeni, dosya adı `trivy` ile başlamalı (pipeline_config'e KAYDEDİLMEZ — `CREW_PHP_BIN` kararıyla aynı).
- İmaj adı repo içeriğidir (güvenilmez): alt sürece gitmeden `^[a-z0-9][a-z0-9._/-]*(:[\w][\w.-]*)?$` (ASCII, ≤ 200) ile doğrulanır; `subprocess.run([...], shell=False)`, `verify._env()`, zaman aşımı; eşleşmeyen imaj "taranamadı".
- Bilinmeyen sessizce "temiz" sayılmaz: tarayıcı yok / zaman aşımı / bozuk JSON / dayanak çözülemedi → imajın `scan.state` alanı nedenini taşır, kart gösterir.
- Uyarı yalnız **düzeltmesi yayınlanmış** (`FixedVersion` dolu) CRITICAL / HIGH açıklara dayanır.
- TR + global + RU birlikte: her imaj için kullanan servisler bölge reposu başına listelenir.
- Testler ağa, Docker'a, Azure'a, MySQL'e gitmez: `run=` / `fetch=` / sahte client enjeksiyonu.
- Kullanıcının gördüğü her metin Türkçe.

## Review Focus

- Kötü niyetli `FROM` satırı (`php:8.4; rm -rf /`, `--flag`, boşluk, `$(…)`) alt sürece ulaşmaz.
- Trivy çıktısı boş / `Results` yok / `Vulnerabilities: null` → 0 bulgu, çökme yok; dönüş kodu ≠ 0 → "tarama başarısız".
- `ARG PHP_VERSION=8.4.27` + `FROM php:${PHP_VERSION}-fpm-bookworm` çözülür; varsayılanı olmayan `ARG` → "taranamadı".
- Aynı dayanak imajı kullanan iki taban dizini / üç bölge tek tarama yapar (imaj başına bir).
- Açık işi olan imaj için ikinci iş açılmaz; yoklayıcı hatası sunucuyu düşürmez.

---

### Task 1: `image_vuln.py` — envanter, işaretler, önbellek

**Files:**
- Create: `src/agile_sdlc_crew/quality/image_vuln.py`
- Modify: `src/agile_sdlc_crew/pipeline_config.py`
- Test: `tests/test_katman0_gates.py` → `test_image_vuln_inventory`, `test_image_vuln_signals`

**Interfaces — Consumes:** `modern.build_index(client)` (`{repo: [{"src": "anatolia/build/x", "images": [...]}]}`), `upgrade_registry.Resolver.latest_patch`, `config/upgrade_image.yaml` (`repo`, `project`), client `get_file_content(repo, path, branch=, project=)`, `get_items_in_path`, `get_last_commit_date(repo, path, branch, project=)`, `verify._env()`.

**Interfaces — Produces:**
- `inventory(client, index, *, read=None) -> list[dict]` — dayanak imaj başına bir kayıt: `{"image": "php:8.4.27-fpm-bookworm", "lang": "php"|"python"|"go"|"node"|"", "version": "8.4.27", "via": ["php-8-4"] (kendi taban dizinlerimiz; genel imajda []), "users": {"anatolia": ["build/channel", …], "russia": […]}, "touched": "2026-10-21" | "", "error": ""}`. Kendi imajımız (`…/<dizin>:<etiket>`, kayıt deposu önekli) → `base-images@production:/<dizin>/Dockerfile` (ad büyük/küçük: `Dockerfile` ya da `dockerfile`) ilk `FROM` satırı, aynı dosyadaki `ARG AD=değer` varsayılanlarıyla `${AD}` / `$AD` yerine konur. Çözülemeyen → `image` = özgün ad, `error` = neden. Dil olmayan imajlar (`debian:…`, `base`) da envantere girer (`lang == ""`), yalnız Trivy işareti alır; çok aşamalı derlemenin takma adları (`FROM builder`) atlanır.
- `valid_image(name: str) -> bool`
- `trivy_cmd() -> list[str] | None` — `CREW_TRIVY_BIN` (mutlak yol, ad `trivy…`, çalıştırılabilir) → `[bin]`; yoksa PATH'te `trivy`; yoksa `docker` varsa `["docker","run","--rm","-v",f"{cache}:/root/.cache/","aquasec/trivy:latest"]`; hiçbiri → None.
- `scan_image(image: str, *, run=None, timeout=600) -> dict` — `{"state": "tamam"|"tarayıcı yok"|"geçersiz imaj"|"zaman aşımı"|"tarama başarısız", "critical": int, "high": int, "packages": [{"name","installed","fixed","severity","cves": [..≤5]}], "os": "debian 12.15", "created": "2026-11-05"}`; komut: `<cmd> image --quiet --platform linux/amd64 --scanners vuln --severity HIGH,CRITICAL --format json <image>` (JSON stdout'tan); yalnız `FixedVersion` dolu olanlar sayılır, paket başına en yüksek önem.
- `patch_lag(lang, version, *, resolver) -> dict | None` — `{"latest": "8.4.30"}` (aynı minörde daha büyük yama varsa), yoksa / bilinmiyorsa None. `go` → Hub adı `golang`, `node` → `node`.
- `eol(lang, version, *, fetch=None, today=None) -> dict | None` — `https://endoflife.date/api/<php|python|go|nodejs>.json`; minör döngünün `eol` tarihi geçmişse `{"eol": "2025-12-31"}`; okunamadı → None (bilinmiyor, uyarı değil). 24 sa bellek önbelleği.
- `report(client, *, index=None, force=False, deps…) -> dict` — `{"scanned_at", "scanner": bool, "images": [{…envanter…, "scan": {...}, "patch": {...}|None, "eol": {...}|None, "level": "kritik"|"yüksek"|"bilgi"|"temiz"|"bilinmiyor", "actions": ["yeniden üret", "yama sürümünü yükselt (8.4.30)", "sürüm yükselt (destek 2025-12-31'de bitti)"]}], "note": "<sınır cümlesi>"}`. `level`: düzeltmeli CRITICAL ya da EOL → kritik; düzeltmeli HIGH ya da yama geride → yüksek; tarama tamam ve hiçbiri yok → temiz; tarama yapılamadı ve başka işaret yok → bilinmiyor. İmaj başına sonuç `config/quality_image_vuln_cache/` altında (anahtar: imaj adı; TTL `CREW_TECHDEBT_IMAGE_VULN_SCAN_H`), `force` önbelleği atlar. Sıralama: kritik → yüksek → bilinmiyor → temiz, sonra kullanan servis sayısı.
- Dört ayar `pipeline_config.py`'de (komşu `CREW_TECHDEBT_UPGRADE_*` girdilerinin biçiminde).

**Tests:** envanter (kendi imaj → ARG çözümü; küçük harfli `dockerfile`; varsayılansız ARG → error; aynı dayanak iki dizinden tek kayıt, `via` iki dizin; bölge başına users; takma ad atlanır); `valid_image` kötü girdiler; `scan_image` sahte `run` ile: düzeltmeli/düzeltmesiz ayrımı, `Vulnerabilities: null`, dönüş kodu 1, zaman aşımı, tarayıcı yok, geçersiz imajda `run` hiç çağrılmaz; `patch_lag` / `eol` (geçmiş, gelecek, `false`, okunamadı); `report` seviye tablosu + önbellek (ikinci çağrıda `run` çağrılmaz, `force` çağırır).

Commit: `feat(techdebt): taban imaj güvenliği — envanter, Trivy, yama/destek işaretleri`

---

### Task 2: Uç, yoklayıcı, uyarı, kart, belgeler

**Files:**
- Modify: `src/agile_sdlc_crew/server.py`, `src/agile_sdlc_crew/quality/image_vuln.py`, `src/agile_sdlc_crew/web/index.html`, `CLAUDE.md`
- Test: `tests/test_katman0_gates.py` → `test_image_vuln_alerts`

**Davranış:**
- `GET /api/techdebt/images?force=0` → `report`; ayar kapalıysa 403 (Türkçe neden). Tarama uzun sürebilir (imaj başına ~20 sn–dakikalar): uç önbellekten döner, önbellek yoksa/`force` ise taramayı arka plan iş parçacığında başlatır ve `{"scanning": true, …eldeki kısım…}` döner; aynı anda tek tarama (kilit).
- `image_vuln.alert(client, rep, *, team, deps, open_fn=None, notify_fn=None) -> list[dict]`: `level == "kritik"` her imaj için — `_TASK` açıksa parmak izi `image_vuln:<imaj>` ile açık iş yoksa **bir** `needs-human` Task (etiketler `tempo-quality; tempo-techdebt; tech-debt; tempo-image-vuln; needs-human`, başlık `Taban imaj güvenliği: <imaj>`; gövde: işaretler, ilk 15 paket, kullanan servisler bölge başına, önerilen eylem, sınır cümlesi) — teknik borç hattının mevcut hikâye / iş açma yardımcılarıyla (`intake.resolve_story(lane="techdebt")`, `render`, `dedup`); `_NOTIFY` açıksa ve günlük özetin Telegram ayarı tanımlıysa imaj başına bir mesaj, aynı imaj + aynı işaret kümesi için tekrar gönderilmez (önbellek dosyasındaki `notified` alanı).
- Yoklayıcı: sunucu başlarken `CREW_TECHDEBT` + `CREW_TECHDEBT_IMAGE_VULN` açıksa günlük (`_SCAN_H`) `report(force=True)` + `alert`; istisna loglanır, döngü sürer; çalışan pipeline işini etkilemez (ayrı iş parçacığı, düşük öncelik gerekmez).
- Kart (`web/index.html`, Teknik borç sekmesi, "Taban imaj güvenliği"): imaj satırları — seviye rozeti, imaj, `via` dizinleri, bölge başına servis sayısı (tooltip: servis yolları), kritik/yüksek sayıları, yama / destek işareti, önerilen eylemler; satır açılınca paket listesi; "Yeniden tara" düğmesi; tarama sürerken durum; üstte sınır cümlesi ("Yayınlanmış imaj değil, dayandığı resmî imaj taranır…").
- `CLAUDE.md`: yeni madde (modül, uç, ayarlar, sınır, güvenlik kuralı). Spec ekine "uygulandı" notu.

**Tests:** `alert` — kritik + `_TASK` açık → bir iş (enjekte `open_fn`), açık iş varken ikinci çağrı açmaz, `_TASK` kapalı → hiç; `_NOTIFY` aynı işaret kümesinde bir kez, işaret değişince yeniden; uç: kapalı 403, önbellek dolu → 200 + images, boş → `scanning: true` (enjekte tarama).

Commit: `feat(techdebt): taban imaj güvenliği kartı, günlük yoklayıcı ve uyarı`
