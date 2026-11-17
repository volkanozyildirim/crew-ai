# Dil Sürümü Yükseltme — Uygulama Planı

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teknik borç sekmesinde her repo × dil için "üretim X → hedef Y" yükseltmesinde kırılacak yerleri bulmak, bunları iş olarak açıp mevcut araç/pipeline hattından düzeltmek ve bulgu kalmayınca sürüm değişikliği PR'larını (test hattı → üretim imajı → kod reposu) sırayla açmak.

**Architecture:** Yeni kaynak adı ve yeni tarayıcı yok: mevcut katalog (`Rule`) iki alan kazanır (`breaks`, `fix_min`), `quality/upgrade.py` hedef sürümü, sürüm yerlerini, hedef araçla doğrulamayı ve engelleri hesaplayıp bulguları **mevcut** `build_groups` → `_open_tasks` → `modern_fix`/pipeline hattına verir. Sürüm adımı `quality/upgrade_bump.py` (`job_kind=upgrade_bump`): bir iş tek adım atar; hangi adımın sırada olduğu saklanmaz, Azure'dan (sabit dal adı, PR, build) okunur. Otomatik ilerletme tek bir sunucu iş parçacığıdır.

**Tech Stack:** Python 3 (FastAPI sunucu, pymysql), PHP `token_get_all` tarayıcı (`php_modern_scan.php`), Azure DevOps REST (`AzureDevOpsClient`), tek dosya `web/index.html`.

**Spec:** `docs/superpowers/specs/2026-11-17-language-upgrade-design.md` — her task'tan önce ilgili bölümü okuyun.

**Çalışma dizini:** `agile_sdlc_crew/` (worktree kökünün altı). **Testler:** `tests/test_katman0_gates.py`, `check(ad, koşul, ayrıntı)` / `skip(ad, neden)`; yeni testler dosyanın sonundaki `main()` listesine eklenir.
**Tek test:** `/Users/volkan.ozyildirim/devel/crewai/agile_sdlc_crew/.venv/bin/python -c "import sys; sys.path.insert(0,'tests'); import test_katman0_gates as t; t.<TEST>(); sys.exit(1 if t.FAIL else 0)"`
**Tüm dosya:** `/Users/volkan.ozyildirim/devel/crewai/agile_sdlc_crew/.venv/bin/python tests/test_katman0_gates.py` (son satır `GEÇEN n · BAŞARISIZ 0`).

## Global Constraints

- Kullanıcıya görünen her metin (kart, Task, PR, yorum, log) **Türkçe**; kod yorumları mevcut dosyanın diliyle.
- Yeni ayarlar `pipeline_config.py`'ye kaydedilir; varsayılanlar: `CREW_TECHDEBT_UPGRADE` kapalı, `CREW_TECHDEBT_UPGRADE_AUTO` kapalı, `CREW_TECHDEBT_UPGRADE_BUMP` açık, `CREW_TECHDEBT_UPGRADE_POLL_MIN` 30, `CREW_TECHDEBT_UPGRADE_MAX_OPEN` 3, `CREW_TECHDEBT_UPGRADE_SPRINT` `next`, `CREW_TECHDEBT_UPGRADE_CI_WAIT_H` 72.
- `CREW_UPGRADE_PHP_BINS` / `CREW_UPGRADE_PYTHON_BINS` **yalnız ortamdan** okunur (`sürüm=yol,…`), `pipeline_config`'e kaydedilmez; dosya adı `php…` / `python3…` ile başlamalı.
- Tempo hiçbir PR'ı merge etmez. Sürüm PR'ları **taslak** açılır.
- FLODevOps / `yml-files`'a giden değişiklik: kayıttaki sitenin o tek satırında sürüm metninin yer değiştirmesi. Başka diff → push yok.
- Depo kodu **çalıştırılmaz** (lint / derleme / `ast`); alt süreçler `verify._env()` ortamıyla, cwd deponun dışında ya da atılabilir worktree'de.
- Sürüm metni `^\d+\.\d+$`; repo adı `modern_fix._REPO`; yol mutlak / `..` / `-` ile başlayan olamaz.
- Azure tek kayıt: yükseltme durumu DB'ye yazılmaz. Yeni tablo / kolon yok.
- Testler çalışan sunucunun DB'sine iş yazmaz: `Deps` / `queue_fn` enjekte edilir (bkz. `test_php_rector_fix`).
- Dal adı: `tempo/upgrade-<dil>-<repo>-<hedef>` (her üç repoda aynı). Etiketler: `tempo-upgrade` (iş), `tempo-upgrade-track` (izleme). İzleme satırı: `tempo-upgrade-key: <dil>:<repo>:<hedef>`.

## Review Focus

1. **Russia kayıt deposunda hedef imaj yok** (`10.131.167.220:5000/prod-ar/php-8-4:1.0` hiçbir serviste geçmiyor) → o site `blocked`, üretim adımı **hiçbir bölge** için açılmaz, DevOps isteği açılır. (Task 3 testi.)
2. **Hedef sürüm aracı yerelde yok** → probe atlanır, tarama katalogla yine sonuç verir, yanıt `probe.ran=false` + neden taşır; hata fırlatmaz. (Task 4 testi.)
3. **Aynı kural hem modernleştirme hem yükseltme taramasında** → parmak izi aynı (`php_modern:<repo>:<kural>:<dosya>`), board'da açık iş varsa ikinci kez açılmaz. (Task 5 testi.)
4. **`yml-files` PR'ı merge edildi ama ondan sonra build yok / eski build yeşil** → üretim adımı açılmaz; merge'den **önce** kuyruğa girmiş build kanıt sayılmaz. (Task 8 testi.)
5. **Site satırı tarama ile iş arasında değişti** (biri elle 8.3 yaptı) → iş `failed`, hiçbir şey push edilmez. (Task 7 testi.)

## Dosyalar

| Dosya | Sorumluluk |
|---|---|
| Create `src/agile_sdlc_crew/quality/upgrade.py` | hedef sürüm, siteler, probe, engeller, tarama/alım, durum, izleme Task'ı, otomatik ilerletme |
| Create `src/agile_sdlc_crew/quality/upgrade_bump.py` | sürüm adımı: sıradaki adım, tek satır değişikliği, PR, DevOps isteği |
| Create `src/agile_sdlc_crew/quality/upgrade_probe.py` (betik) | hedef Python yorumlayıcısıyla `compile()` sürücüsü (`-I` ile çalışır, depo dışından) |
| Create `src/agile_sdlc_crew/config/upgrade_targets.yaml` | Go / Next.js tavan sürümleri |
| Create `src/agile_sdlc_crew/config/modern_python_upgrade_rules.yaml` | kaldırılan stdlib modül / API kataloğu |
| Modify `quality/php_modern.py` | `Rule.breaks`, `Rule.fix_min`, `load_rules` doğrulaması |
| Modify `quality/php_modern_scan.php`, `config/php_modern_rules.yaml` (v4), `config/modern_nextjs_rules.yaml` | yeni kurallar |
| Modify `quality/model.py`, `quality/render.py` | `Group.extra_tags`, yükseltme başlığı / Task metni |
| Modify `quality/modern_fix.py` | kapsamda `upgrade_to` → hedef araçla ek lint |
| Modify `tools/azure_devops_base.py` | yazma çağrılarında `project=`, dal → PR arama |
| Modify `server.py`, `pipeline_config.py`, `web/index.html`, `CLAUDE.md` | uçlar, işçi, ayarlar, kart, belge |
| Test `tests/test_katman0_gates.py`, fikstür `tests/fixtures/upgrade/` | |

---

## Aşama A — hedef, katalog, tarama, alım

### Task 1: Katalog alanları (`breaks`, `fix_min`) ve kural süzgeci

**Files:** Modify `quality/php_modern.py` (`Rule`, `load_rules`), `config/php_modern_rules.yaml`; Create `quality/upgrade.py`; Test `tests/test_katman0_gates.py`

**Interfaces — Produces:**
- `php_modern.Rule.breaks: str = ""` (`""|"error"|"behavior"|"deprecation"`), `Rule.fix_min: tuple[int,int] = (7, 0)`; `BREAKS = ("error", "behavior", "deprecation")`.
- `upgrade.upgrade_rules(rules: list[Rule], current: str, target: str) -> list[Rule]` — `breaks` dolu ve `current < since ≤ target`.
- `upgrade.blocking(rule: Rule, *, auto: bool) -> bool` — `error` → True; `behavior` → True; `deprecation` → `auto`; diğer → False.

- [ ] **Step 1: Testi yaz** — `test_upgrade_rules()`:

```python
from agile_sdlc_crew.quality import php_modern as pm, upgrade
rules = pm.load_rules()
by = {r.key: r for r in rules}
check("removed_functions hedefte çalışmaz", by["removed_functions"].breaks == "error")
check("implicit_nullable deprecation", by["implicit_nullable"].breaks == "deprecation")
check("mekanik kural yükseltme kuralı değil", by["str_contains"].breaks == "")
keys = {r.key for r in upgrade.upgrade_rules(rules, "8.2", "8.4")}
check("8.2→8.4: 8.3 ve 8.4 kuralları", {"get_class_noarg", "deprecated_83", "implicit_nullable", "deprecated_84"} <= keys, str(keys))
check("8.2→8.4: 8.2 ve öncesi yok", not ({"dynamic_properties", "utf8_encode", "removed_functions"} & keys))
check("7.4→8.4 removed_functions'ı içerir", "removed_functions" in {r.key for r in upgrade.upgrade_rules(rules, "7.4", "8.4")})
check("error engeller", upgrade.blocking(by["removed_functions"], auto=False))
check("otomatik deprecation engeller, otomatik olmayan engellemez",
      upgrade.blocking(by["implicit_nullable"], auto=True) and not upgrade.blocking(by["implicit_nullable"], auto=False))
```

Ayrıca geçici bir YAML ile: `breaks: saçma` → `load_rules` `ValueError`; `fix_min: "8.4"` → `(8, 4)`.

- [ ] **Step 2: Çalıştır, başarısız olduğunu gör** (`AttributeError: breaks` / `No module named upgrade`).
- [ ] **Step 3: Uygula.** Katalog: 11 `deprecation` kuralına `breaks: deprecation`; `removed_functions` → `breaks: error`. `version: 4`. `load_rules` bilinmeyen `breaks` değerinde `ValueError` atar.
- [ ] **Step 4: Testi ve tüm dosyayı çalıştır** — yeşil.
- [ ] **Step 5: Commit** — `feat(techdebt): katalogda breaks / fix_min, yükseltme kural süzgeci`

### Task 2: PHP kırılma kuralları (7.4 → 8.4)

**Files:** Modify `quality/php_modern_scan.php`, `config/php_modern_rules.yaml`; Create `tests/fixtures/upgrade/php/*.php`; Test

**Interfaces — Produces:** yeni kural anahtarları (tarayıcı `hit()` ile, katalogda Türkçe metin, hepsi `unit: file`):

| Anahtar | since | breaks | auto | Tespit (token) |
|---|---|---|---|---|
| `removed_functions` (genişler) | 8.0 | error | true | + `get_magic_quotes_gpc`, `get_magic_quotes_runtime`, `ezmlm_hash`, `restore_include_path`, `hebrevc`, `convert_cyr_string`, `fgetss`, `gzgetss`, `image2wbmp`, `png2wbmp`, `jpeg2wbmp`, `ldap_sort`, `read_exif_data` |
| `curly_offset` | 8.0 | error | true | değişken / `]` / `)` ardından `{…}` erişimi (`$s{0}`) |
| `removed_casts` | 8.0 | error | true | `(real)`, `(unset)` cast token'ları |
| `required_after_optional` | 8.0 | deprecation | true | varsayılanlı parametreden sonra varsayılansız (variadic ve `= null` + tip hariç) |
| `legacy_autoload` | 8.0 | error | false | `function __autoload(` |
| `php4_constructor` | 8.0 | behavior | false | sınıf adıyla aynı adlı metot, sınıfta `__construct` yok, namespace yok |
| `loose_compare_numeric` | 8.0 | behavior | false | `==` / `!=` bir yanı sayı literali, öteki sayısal olmayan string literali |
| `null_to_internal` | 8.1 | behavior | false | `strlen`, `strpos`, `str_replace`, `trim`, `explode`, `htmlspecialchars`, `strtolower`, `strtoupper`, `substr` çağrısında literal `null` argüman |
| `removed_81` | 8.1 | deprecation | true | `strftime`, `gmstrftime`, `date_sunrise`, `date_sunset`, `mhash`, `mhash_count`, `mhash_get_block_size`, `mhash_get_hash_name`, `mhash_keygen_s2k`, `odbc_result_all` |
| `serializable_only` | 8.1 | deprecation | false | `implements … Serializable` ve sınıfta `__serialize` yok |
| `moved_to_pecl_84` | 8.4 | error | false | `imap_*`, `pspell_*`, `oci_*` çağrısı; `requires_if` ile eklenti (`imap`, `pspell`, `oci8`) |
| `noncanonical_casts_85` | 8.5 | deprecation | true | `(boolean)`, `(integer)`, `(double)`, `(binary)` cast token'ları → `(bool)`, `(int)`, `(float)`, `(string)` |
| `backtick_exec_85` | 8.5 | deprecation | true | backtick operatörü → `shell_exec()` |
| `deprecated_85` | 8.5 | deprecation | false | 8.5'te kullanımdan kalkan fonksiyon / sabitler — liste uygulama sırasında php.net `migration85.deprecated` sayfasından alınır, ezberden yazılmaz |

8.5 kuralları kullanıcının asıl hedefi (8.2 → 8.5) için eklenir. Listeyi yazmadan önce `https://www.php.net/manual/en/migration85.php` (incompatible + deprecated) okunur; orada olup tabloda olmayan ve token'la güvenilir bulunabilen her madde aynı biçimde kural olur, bulunamayanlar `kind: report`.

`line_local: false`: `legacy_autoload`, `php4_constructor`, `serializable_only`. `fix_min` hepsinde varsayılan (`7.0`), `removed_casts` dahil — karşılıkları 7.x'te çalışır.

- [ ] **Step 1: Fikstür + test** — `tests/fixtures/upgrade/php/break.php` her kural için bir pozitif satır, `ok.php` karşı örnekler: string / heredoc içinde `each(`, `$a->{'x'}` (dinamik property — `curly_offset` değil), `function f($a = null, $b)` yerine `function f(?T $a = null, $b)` (istisna), `0 == $x` (değişken — bulgu değil), `__construct`'ı olan sınıfta eş adlı metot. `test_upgrade_php_rules()` `pm.scan_repo(fixture_dir)` çalıştırır (`php` yoksa `skip`):

```python
hits = {(h["rule"], h["file"]) for h in res["hits"]}
for k in ("removed_functions", "curly_offset", "removed_casts", "required_after_optional", "legacy_autoload",
          "php4_constructor", "loose_compare_numeric", "null_to_internal", "removed_81", "serializable_only", "moved_to_pecl_84"):
    check(f"{k} bulundu", (k, "break.php") in hits)
check("karşı örneklerde yükseltme bulgusu yok",
      not [h for h in res["hits"] if h["file"] == "ok.php" and by[h["rule"]].breaks], ...)
```

- [ ] **Step 2: Başarısız gör.**
- [ ] **Step 3: Tarayıcı + katalog.** Güvenilir tespit edilemeyen kural kataloğa `kind: report` olarak girer (spec "Yapıcıya bırakılanlar"); testteki listeden çıkarılır ve commit mesajında adı geçer. Katalog metni mevcut kurallarla aynı biçimde: `title`, `summary`, `advantages`, `risks`, `before`, `after`, `ref` (php.net migration sayfası).
- [ ] **Step 4: Test + `test_php_modern_82_84` + tüm dosya** — yeşil (mevcut kurallarda regresyon yok).
- [ ] **Step 5: Commit** — `feat(techdebt): PHP 8.0–8.4 kırılma kuralları`

### Task 3: Hedef sürüm ve sürüm yerleri

**Files:** Modify `quality/upgrade.py`; Create `config/upgrade_targets.yaml` (`php: ["8.0","8.1","8.2","8.3","8.4","8.5"]`, `python: ["3.10","3.11","3.12","3.13","3.14"]`, `go: ["1.22","1.23","1.24","1.25","1.26","1.27"]`, `nextjs: ["13.0","14.0","15.0"]`); Test

**Interfaces — Consumes:** `modern.target(client, repo, root, lang, index=) -> {"version","source","images","per_src"?}`, `modern.build_index(client)`, `app_map._read(client, devrepo, path)`, `app_map.DEVOPS_PROJECT/DEVOPS_BRANCH`.

**Interfaces — Produces:**
- `upgrade.known_versions(lang: str) -> list[str]` — `upgrade_targets.yaml`'daki liste, artan; biçimi bozuk girdi atılır.
- `upgrade.published(lang: str, index: dict) -> set[str]` — build dizinindeki `php-X-Y` / `python-X-Y` imaj sürümleri (Go / Next.js: boş küme, "yayın" kavramı yok → hepsi `published=True`).
- `upgrade.default_target(lang: str, current: str, index: dict) -> str | None` — PHP/Python: yayınlanmış en yüksek sürüm (`> current` ise); Go/Next.js: `known_versions` sonuncusu.
- `upgrade.selectable(lang: str, current: str, index: dict) -> list[dict]` — `known_versions` içinde `current < v` olanlar, artan; her biri `{"version","published": bool,"catalog": bool,"tool": bool}` (`catalog`: o sürümde `breaks` kuralı var; `tool`: `tool_bins` / yerel Go o sürümü karşılıyor).
- `upgrade.image_for(image: str, lang: str, to: str) -> str` — `…/php-8-2:1.0` → `…/php-8-4:1.0` (önek ve etiket korunur).
- `upgrade.sites(client, repo: str, root: Path, lang: str, current: str, to: str, index: dict, *, read=None, yml_files: dict | None = None) -> list[dict]` — her biri `{"where": "devops"|"ci"|"code", "repo", "project", "branch", "path", "line", "old", "new", "blocked": ""|neden}`. `read(devrepo, path) -> str` ve `yml_files: {path: text}` test enjeksiyonu; varsayılanları Azure API.
- `upgrade.plan(client, repo: str, lang: str, to: str | None = None, *, deps=None) -> dict` — `{"repo","lang","current","target","target_source","selectable","sites","per_src","up_to_date": bool}`; `to` seçilebilir değilse `ValueError("Hedef sürüm seçilemez: …")`.

Kurallar (spec §1–2): devops sitesi yalnız sürümü hedefin altındaki servisler; yeni imaj metni aynı FLODevOps reposunun dizininde başka bir serviste geçmiyorsa `blocked="imaj yayınlanmamış"`. CI sitesi: `yml-files` `Test Pipelines/*.yml` içinde repoyu adıyla anan dosyalardaki `phpVersion:` satırı; hiçbir dosya anmıyorsa site yok, birden çok dosya anıyorsa hepsi. Kod sitesi: `composer.json` `require.php` ve `config.platform.php`, `go.mod` `go` / `toolchain`, `pyproject.toml` `requires-python`, `.python-version`, repo kökü `Dockerfile` `FROM`.

- [ ] **Step 1: Test** — `test_upgrade_plan_sites()`, sahte `index`:

```python
index = {"core": [{"src": "anatolia/build/lojistik", "images": ["eu/prod-ar/php-8-2:1.0"]},
                  {"src": "russia/build/lojistik", "images": ["10.1.1.1:5000/prod-ar/php-8-2:1.0"]}],
         "web":  [{"src": "anatolia/build/web", "images": ["eu/prod-ar/php-8-4:1.0"]}]}
check("varsayılan hedef: yayınlanmış en yüksek imaj", upgrade.default_target("php", "8.2", index) == "8.4")
sel = upgrade.selectable("php", "8.2", index)
check("seçilebilir: listedeki her üst sürüm", [s["version"] for s in sel] == ["8.3", "8.4", "8.5"])
check("8.5 seçilebilir ama imajı yok, katalog kuralı yok",
      sel[-1]["published"] is False and sel[-1]["catalog"] is False and sel[1]["published"] is True)
p85 = upgrade.plan(None, "core", "php", to="8.5", deps=deps)
check("yayınlanmamış hedef: bütün üretim siteleri engelli",
      all("yayınlanmamış" in s["blocked"] for s in p85["sites"] if s["where"] == "devops"))
check("imaj adı", upgrade.image_for("eu/prod-ar/php-8-2:1.0", "php", "8.4") == "eu/prod-ar/php-8-4:1.0")
ss = upgrade.sites(None, "core", root, "php", "8.2", "8.4", index, read=fake_read, yml_files=fake_yml)
dv = {s["path"]: s for s in ss if s["where"] == "devops"}
check("anatolia sitesi açık", dv["/build/lojistik/Dockerfile@anatolia"]["blocked"] == "")   # anahtar biçimi uygulayana ait
check("russia imajı yayınlanmamış → engelli", "yayınlanmamış" in <russia sitesi>["blocked"])
check("CI sitesi yalnız core'u anan dosya", [s["path"] for s in ss if s["where"] == "ci"] == ["Test Pipelines/core.yml"])
check("kod sitesi: composer require.php", any(s["where"] == "code" and s["path"] == "composer.json" for s in ss))
```

+ `current ≥ target` → `plan()["up_to_date"] is True`, `sites == []`; `to="9.9"` → `ValueError`; `to="8.4; rm"` → `ValueError`.

- [ ] **Step 2–4:** başarısız gör → uygula → yeşil.
- [ ] **Step 5: Commit** — `feat(techdebt): yükseltme hedefi ve sürüm yerleri`

### Task 4: Hedef araçla doğrulama ve bağımlılık engelleri

**Files:** Modify `quality/upgrade.py`; Create `quality/upgrade_probe.py`; Test

**Interfaces — Produces:**
- `upgrade.tool_bins(env_name: str, prefix: str) -> dict[str, str]` — `CREW_UPGRADE_PHP_BINS` / `CREW_UPGRADE_PYTHON_BINS` ayrıştırır; adı `prefix` ile başlamayan ya da var olmayan yol atılır.
- `upgrade.probe(root: Path, lang: str, current: str, to: str, *, files: list[str] | None = None, timeout: int = 300) -> tuple[list[dict], dict]` — hit'ler `{"rule","file","line","text","message"}`; durum `{"ran": bool, "tool": str, "reason": str}`.
  - PHP: `<bin> -d error_reporting=-1 -d display_errors=1 -l <dosya>`; `Parse error` / `Fatal error` → kural `probe:error`; `Deprecated:` → `probe:deprecation`. Dosya listesi `php_modern` taramasının dışlamalarıyla aynı.
  - Go: atılabilir `git worktree` içinde `go.mod`'da `go <to>` yazıp `go build ./...` + `go vet ./...` (`verify._env()`, `GOTOOLCHAIN=local`); yerel Go < hedef ise `ran=False`.
  - Python: `<bin> -I <upgrade_probe.py> <kök> <dosyalar…>`, cwd depo dışı; `SyntaxError` → `probe:error`, `SyntaxWarning` / `DeprecationWarning` → `probe:deprecation`.
  - Next.js: `ran=False, reason="araç yok"`.
- `upgrade.probe_rules(hits) -> list[Rule]` — `probe:error` (`breaks="error"`, `auto=True`, `line_local=True`), `probe:deprecation` (`breaks="deprecation"`, `auto=False`); başlık / özet araç mesajından.
- `upgrade.blockers(root: Path, lang: str, to: str) -> list[dict]` — `{"kind": "dependency"|"peer", "name", "constraint", "detail"}`. PHP: `composer.lock` `packages[].require.php` kısıtı hedefi dışlıyorsa. Kısıt değerlendirme `upgrade.php_constraint_allows(constraint: str, version: str) -> bool` (`^`, `~`, `>=`, `<`, `||`, `|`, boşlukla VE, `*`); anlaşılmayan kısıt **engel sayılmaz**.

- [ ] **Step 1: Test** — `test_upgrade_probe_and_blockers()`:

```python
check("^7.3|^8.0 8.4'e izin verir", upgrade.php_constraint_allows("^7.3|^8.0", "8.4"))
check("~8.1.0 8.4'e izin vermez", not upgrade.php_constraint_allows("~8.1.0", "8.4"))
check(">=7.4 <8.3 izin vermez", not upgrade.php_constraint_allows(">=7.4 <8.3", "8.4"))
check("anlaşılmayan kısıt engel değil", upgrade.php_constraint_allows("dev-main as 1.0", "8.4"))
# composer.lock fikstürü: a/ok (^8.0), b/old (~8.1.0)
check("yalnız b/old engel", [b["name"] for b in upgrade.blockers(root, "php", "8.4")] == ["b/old"])
check("ad denetimi: php ile başlamayan yol atılır", upgrade.tool_bins(...) == {...})   # monkeypatch os.environ
hits, st = upgrade.probe(root, "php", "8.2", "8.4")          # CREW_UPGRADE_PHP_BINS boş
check("araç yokken probe atlanır, hata yok", hits == [] and st["ran"] is False and "araç" in st["reason"])
```

Yerelde `php` varsa (yoksa `skip`): `CREW_UPGRADE_PHP_BINS=<sürüm>=<shutil.which('php')>` ile sözdizimi bozuk bir dosyada `probe:error` hit'i; depo köküne konmuş `py_compile.py` / `ruff.py` tuzağı Python probe'unda **çalışmaz** (dosya bir işaret dosyası yazmaya çalışır; test onun oluşmadığını doğrular).

- [ ] **Step 2–4.**
- [ ] **Step 5: Commit** — `feat(techdebt): hedef sürüm aracıyla doğrulama, bağımlılık engelleri`

### Task 5: Yükseltme taraması, alım, Task metni

**Files:** Modify `quality/upgrade.py`, `quality/model.py`, `quality/render.py`, `quality/modern_fix.py`; Create `config/modern_python_upgrade_rules.yaml`; Modify `config/modern_nextjs_rules.yaml`; Test

**Interfaces — Consumes:** `php_modern.scan_cached`, `php_modern.build_groups(scan, rules, version, repo=, board_index=, now=, cfg=, ext_ok=, source=, tool_fix=, tool_rules=)`, `php_modern._row`, `php_modern.gid_of`, `php_modern._rector_queue`, `modern._hits`, `modern.LANGS`, `modern.scope_of`, `intake._open_tasks`, `intake.resolve_story`, `intake._LOCK`.

**Interfaces — Produces:**
- `model.Group.extra_tags: list[str] = []`; `render.task_tags` sonuna ekler.
- `upgrade.TAG = "tempo-upgrade"`.
- `upgrade.scan(client, repo: str, lang: str, *, to: str | None = None, team: str = "", force: bool = False, deps=None) -> dict` — `modern.scan` çıktısıyla aynı anahtarlar (`auto` / `infra` / `human` / `rules` / `skipped` / `errors`) + `current`, `target`, `selectable`, `sites`, `blockers`, `probe`, `counts: {"error","behavior","deprecation"}`, `blocking: int`. Her satırda ek `breaks`, `blocking: bool`, `pre_fixable: bool` (`fix_min ≤ current`).
- `upgrade.take(client, repo: str, lang: str, group_ids: list[str], *, to: str | None = None, team: str = "", sprint: str = "", deps=None) -> dict` — `modern.take` ile aynı yanıt biçimi.

Davranış: kurallar = `upgrade_rules(dilin kataloğu)` + `probe_rules`; `build_groups`'a sürüm `"99.0"` verilir (süzme zaten yapıldı). Gruplama sonrası: `breaks == "behavior"` → `needs_human=True`, neden "Davranış değişikliği: otomatik düzeltilemez"; `fix_min > current` → `needs_human=True`, neden "Düzeltme yalnız <hedef> ve sonrasında çalışır — sürüm adımıyla birlikte ele alınmalı". Grup başlığı önekli: `Sürüm yükseltme <cur>→<to>: <başlık>`; `extra_tags=["tempo-upgrade"]`. Parmak izi ve `source` dilin mevcut kaynağıdır (**değişmez**). Task açıklamasındaki hedef satırı: `<cur> → <to> (sürüm yükseltme; düzeltme <cur> sürümünde de çalışmalı)`. Araç işlerinin kapsamına `"upgrade_to": "<to>"` eklenir; `modern_fix` bunu görürse `verify`'dan sonra değişen dosyaları `upgrade.probe(..., files=)` ile hedef araçla da denetler, `probe:error` çıkarsa o dosya PR'dan düşer (`dropped_why`).

Rector `withPhpVersion` bugün de `modern.target` (= şimdiki üretim sürümü) ile çağrılıyor; bu korunur — test `inspect_source(modern_fix.run)` / `_run_scoped` içinde sürümün `upgrade_to`'dan **alınmadığını** doğrular. Go kataloğu: `go 1.22` döngü değişkeni semantiği ve `GODEBUG` varsayılan değişimleri `kind: report` (iş açmaz, kartta sayı olarak görünür); Go'da asıl bulgu probe'dan gelir.

Python kataloğu (`ast.Import` / `ast.ImportFrom` / `ast.Attribute`, depo kodu çalıştırılmadan), her satır `{key, since, breaks, auto: false, title, summary, …}`: 3.12 → `imp`, `distutils`, `asyncore`, `asynchat`, `smtpd`; `datetime.utcnow` / `utcfromtimestamp` (deprecation); 3.13 → `cgi`, `cgitb`, `pipes`, `telnetlib`, `nntplib`, `crypt`, `audioop`, `aifc`, `chunk`, `imghdr`, `sndhdr`, `sunau`, `uu`, `xdrlib`, `msilib`, `nis`, `ossaudiodev`, `spwd`, `lib2to3`. Next.js kataloğuna `breaks` eklenir: `next/legacy/image` (13.0, deprecation), senkron `cookies()` / `headers()` / `params` (15.0, behavior), `next lint` betiği (15.0 sonrası için `report`).

- [ ] **Step 1: Test** — `test_upgrade_scan_take()` (geçici repo, `scan_fn` / `Deps` enjeksiyonu, DB'ye yazmaz):

```python
res = upgrade.scan(client, "core", "php", to="8.4", deps=deps)   # current 8.2 (sahte index)
check("başlık önekli", all(r["title"].startswith("[core] Sürüm yükseltme 8.2→8.4:") for r in res["auto"]))
check("parmak izi modernleştirme ile aynı", res["auto"][0]["fingerprints"][0].startswith("php_modern:core:"))
check("behavior insan işi ve engelleyen", all(r["needs_human"] and r["blocking"] for r in res["human"] if r["breaks"] == "behavior"))
check("mekanik kural yok", not [r for r in res["auto"] if r["rule"] == "str_contains"])
check("sayımlar", res["counts"]["error"] >= 1 and res["blocking"] >= 1)
# board'da aynı parmak izi açık → ikinci kez listelenmez
res2 = upgrade.scan(client, "core", "php", to="8.4", deps=deps_with_open_fp)
check("board'da açık parmak izi atlanır", res2["skipped_counts"].get("açık WI") and not res2["auto"])
out = upgrade.take(client, "core", "php", [res["auto"][0]["gid"]], to="8.4", deps=deps)
check("Task etiketi", "tempo-upgrade" in client.created[0]["System.Tags"])
check("Task: şimdiki sürümde de çalışmalı", "8.2 sürümünde de çalışmalı" in client.created[0]["System.Description"])
check("araç kapsamı upgrade_to taşır", json.loads(queued_payloads[0])["upgrade_to"] == "8.4")
check("güncel repo taranmaz", upgrade.scan(client, "web", "php", deps=deps)["up_to_date"] is True)
```

- [ ] **Step 2–4.** Step 4'te ayrıca `test_php_rector_fix`, `test_modern_multilang`, `test_techdebt_lane` yeşil kalmalı.
- [ ] **Step 5: Commit** — `feat(techdebt): yükseltme taraması ve alımı`

### Task 6: Uçlar, ayarlar, kart (tarama + alım)

**Files:** Modify `server.py`, `pipeline_config.py`, `web/index.html`; Test

**Interfaces — Produces:**
- `GET /api/techdebt/upgrade/repos` → `{"enabled": true, "items": [{"repo","lang","label","current","target","up_to_date","stage"}]}` (bu task'ta `stage` = `"hazırlık"` sabit; Task 8 gerçek değeri bağlar).
- `GET /api/techdebt/upgrade/scan?repo&lang&to&team&force` → `upgrade.scan`.
- `POST /api/techdebt/upgrade/take` gövde `{repo, lang, to, group_ids, team, sprint}` → `upgrade.take`; iş kuyruğa girdiyse `_ensure_worker()`.
- `_upgrade_off()` — `_modern_off()` + `CREW_TECHDEBT_UPGRADE` (403 "Sürüm yükseltme kapalı (CREW_TECHDEBT_UPGRADE)").
- Kart: `#tdModernCard` içinde `Modernleştirme | Sürüm yükseltme` anahtarı (`localStorage.tempo_modern_mode`); yükseltme görünümü spec §8 taslağındaki satırlar: **hedef sürüm seçici** (`selectable`'dan; seçenek metni sürüm + durum: "8.4", "8.5 — imaj yok · yalnız araç doğrulaması"; seçim değişince `upgradeScan()` yeni `to` ile yeniden çağrılır; son seçim repo × dil başına `localStorage.tempo_upgrade_to`'da; aktif izleme işi varken seçici kilitli ve izleme işinin hedefini gösterir), sayım satırı, engeller, sürüm yerleri, üç liste, **İşleri aç**. JS fonksiyonları: `loadUpgradeRepos()`, `upgradeScan(force)`, `renderUpgrade()`, `upgradeTake()`. Arayüz eşik hesaplamaz.

- [ ] **Step 1: Test** — `test_upgrade_api_surface()`: `pipeline_config` şemasında yedi ayar varsayılanlarıyla; `CREW_UPGRADE_PHP_BINS` şemada **yok**; `server.app.routes` içinde beş yol (bu task üçünü, Task 8 ikisini ekler — test Task 8'de tamamlanır); `index.html` `loadUpgradeRepos` / `upgradeScan` / `renderUpgrade` / `upgradeTake` ve `tempo_modern_mode` içerir; `TestClient` ile bayrak kapalıyken `/api/techdebt/upgrade/repos` → 403.
- [ ] **Step 2–4.** Step 4'te `.venv/bin/python -c "from agile_sdlc_crew.server import app"` hatasız.
- [ ] **Step 5: Commit** — `feat(techdebt): sürüm yükseltme uçları ve kartı`

---

## Aşama C — sürüm adımı ve izleme

### Task 7: Dış repolara tek satırlık sürüm değişikliği

**Files:** Modify `tools/azure_devops_base.py`; Create `quality/upgrade_bump.py`; Test

**Interfaces — Produces:**
- `AzureDevOpsClient.create_branch(repo, branch_name, source_branch="main", project=None)`, `push_changes(repo, branch, changes, commit_message, project=None)`, `create_pull_request(repo, source_branch, target_branch="main", title="", description="", work_item_ids=None, draft=False, project=None)`, `list_branches(repo, project=None)` — `project` verilmezse bugünkü davranış.
- `AzureDevOpsClient.find_pull_request(repo, source_branch, *, project=None) -> dict | None` — o daldan açılmış en yeni PR (`status=all`): `{"id","status","url","closed_at","merge_commit","target"}`.
- `upgrade_bump.branch_name(lang, repo, to) -> str` → `tempo/upgrade-<lang>-<repo>-<to>`.
- `upgrade_bump.apply_site(text: str, site: dict) -> str` — `site["line"]` satırında `site["old"]` tam bir kez geçmeli; geçmiyorsa `SiteChanged(ValueError)`. Sonuç, o satır dışında girdiyle bayt bayt aynı.
- `upgrade_bump.push_sites(client, sites: list[dict], *, branch: str, title: str, body: str, wi_id: int | None) -> dict` — aynı `(project, repo)` siteleri tek commit + tek taslak PR; `{"prs": [{"repo","id","url"}], "fallback": [{"repo","diff"}]}`. Yazma 403 dönerse PR yerine unified diff `fallback`'e girer.

- [ ] **Step 1: Test** — `test_upgrade_bump_sites()`:

```python
src = "FROM eu/prod-ar/php-8-2:1.0\nRUN x\n"
site = {"line": 1, "old": "php-8-2:1.0", "new": "php-8-4:1.0", ...}
out = bump.apply_site(src, site)
check("yalnız o satır değişir", out == "FROM eu/prod-ar/php-8-4:1.0\nRUN x\n")
try: bump.apply_site("FROM eu/prod-ar/php-8-3:1.0\nRUN x\n", site); ok = False
except bump.SiteChanged: ok = True
check("satır değişmişse push yok", ok)
check("dal adı", bump.branch_name("php", "core", "8.4") == "tempo/upgrade-php-core-8.4")
r = bump.push_sites(fake_client, [s_anatolia_a, s_anatolia_b, s_russia], branch=..., title=..., body=..., wi_id=7)
check("bölge reposu başına tek PR, taslak", [p["repo"] for p in r["prs"]] == ["anatolia", "russia"] and all(c["draft"] for c in fake_client.prs))
check("project parametresi geçer", all(c["project"] == "FLODevOps" for c in fake_client.pushes))
check("403 → diff DevOps isteğine", bump.push_sites(forbidden_client, [s_anatolia_a], ...)["fallback"][0]["diff"].count("\n+") == 1)
```

- [ ] **Step 2–4.**
- [ ] **Step 5: Commit** — `feat(techdebt): sürüm satırı değişikliği ve çapraz proje PR`

### Task 8: Durum, izleme Task'ı, sıradaki adım

**Files:** Modify `quality/upgrade.py`, `quality/upgrade_bump.py`, `server.py`; Test

**Interfaces — Consumes:** `find_pull_request`, `AzureDevOpsClient.get_pr_build` (`queue_time`, sonuç), `devops_request.open_request(client, team=, title=, body_html=, source_wi=, key=, …)`, `db.create_job(..., job_kind=, job_payload=)`.

**Interfaces — Produces:**
- `upgrade.STAGES = ("hazırlık", "engelli", "hazır", "test hattı", "üretim", "tamam")`.
- `upgrade.status(client, repo: str, lang: str, *, to: str | None = None, deps=None, scan: dict | None = None) -> dict` — `{"stage","current","target","blocking","blockers","open_tasks","prs": {"ci","devops": [...],"code"},"next": {"step": "ci"|"prod"|"code"|None,"ready": bool,"reason": str}}` (spec §5–6 tabloları).
- `upgrade_bump.ci_evidence(client, repo: str, since: str, *, wait_h: int) -> dict` — `{"state": "green"|"red"|"none"|"no_pipeline","build": {...}|None,"summary": str}`; yalnız `queue_time > since` olan build sayılır.
- `upgrade.start(client, repo, lang, *, to=None, team: str, deps=None) -> dict` / `upgrade.stop(client, repo, lang, *, to=None, team: str) -> dict` — izleme Task'ı (takım alanı, kök iteration, etiket `tempo-upgrade-track`, `tempo-upgrade-key:` satırı); açık olan varken ikinci açılmaz (aynı id döner).
- `upgrade.tracked(client, team: str) -> list[dict]` — `{"id","lang","repo","to"}`.
- `upgrade_bump.queue_step(client, repo, lang, *, to=None, team: str, deps=None, create_job=None) -> dict` — ön koşul yoksa `ValueError(status["next"]["reason"])`; yoksa `job_kind="upgrade_bump"` işi, kapsam (`lang, repo, from, to, step, sites, track_id, team`) INSERT'te.
- `upgrade_bump.run(job_id: int) -> dict` — bir adım: yeniden doğrula (engelleyen bulgu yok, engel yok, siteler `old` değerinde) → `push_sites` → izleme Task'ına yorum; `prod` adımında DevOps isteği (anahtar `upgrade:<dil>:<repo>:<hedef>`). `CREW_TECHDEBT_UPGRADE_BUMP` kapalıysa PR yerine diff DevOps isteğine yazılır.
- `POST /api/techdebt/upgrade/start`, `/stop`, `/bump`; işçi `job_kind == "upgrade_bump"` → `upgrade_bump.run`; yeniden deneme (`/api/jobs/{id}/retry`) türü ve kapsamı korur; `_wi_tracked` bu türü **izlemez**; retro / sprint planı / daily sorguları zaten `pipeline` süzer.

Adım kuralları: `ci` ön koşulu aşama `hazır`; `prod` ön koşulu CI PR'ı `completed` ve `ci_evidence.state == "green"` (`no_pipeline` da geçer; PR açıklamasının ilk satırı "Test kanıtı yok"); `code` ön koşulu `modern.target ≥ hedef`. CI sitesi yoksa `ci` atlanır; devops sitesi yoksa `code` doğrudan `hazır`'da açılır. Herhangi bir devops sitesi `blocked` ise `prod` hiçbir bölge için açılmaz.

- [ ] **Step 1: Test** — `test_upgrade_status_and_steps()` (sahte client: PR / build / WIQL kayıtları):

```python
check("engelleyen bulgu → hazırlık", st(scan_with_blocking)["stage"] == "hazırlık")
check("yalnız composer engeli → engelli", st(scan_clean, blockers=[b])["stage"] == "engelli")
check("temiz → hazır, sıradaki ci", st(scan_clean)["stage"] == "hazır" and st(scan_clean)["next"] == {"step": "ci", "ready": True, ...})
check("CI PR'ı açık → test hattı, prod hazır değil", s["stage"] == "test hattı" and not s["next"]["ready"])
check("merge'den ÖNCE kuyruğa girmiş yeşil build kanıt değil", bump.ci_evidence(c_old_build, "core", merged_at, wait_h=72)["state"] == "none")
check("merge'den sonra yeşil → prod hazır", s2["next"] == {"step": "prod", "ready": True, ...})
check("merge'den sonra kırmızı → prod yok, özet var", ev["state"] == "red" and ev["summary"])
check("russia engelli → prod hiçbir bölgeye açılmaz", "imaj" in s3["next"]["reason"] and not s3["next"]["ready"])
check("üretim hedefte → sıradaki code", s4["next"]["step"] == "code")
check("ikinci start aynı izleme işini döner", upgrade.start(c, "core", "php", team="T")["id"] == first_id and len(c.created) == 1)
# run: site satırı değişmiş → failed, push yok
check("site değişmişse iş başarısız, push yok", raised_site_changed and not c.pushes)
check("upgrade_bump WI yaşam döngüsünde izlenmez", not server._wi_tracked({"job_kind": "upgrade_bump"}))
```

- [ ] **Step 2–4.** `test_upgrade_api_surface` beş yolu da doğrular.
- [ ] **Step 5: Commit** — `feat(techdebt): yükseltme durumu, izleme işi, sürüm adımı`

### Task 9: Kartta aşama şeridi ve düğmeler

**Files:** Modify `web/index.html`, `server.py` (`/upgrade/repos` gerçek `stage`); Test

**Interfaces — Consumes:** `upgrade.status`, `/start`, `/stop`, `/bump`.

- [ ] **Step 1: Test** — `test_upgrade_card()`: `index.html` `upgradeStart`, `upgradeStop`, `upgradeBump` fonksiyonlarını ve dört aşama etiketini (`Hazırlık`, `Hazır`, `Test hattı`, `Üretim`) içerir; `/api/techdebt/upgrade/scan` yanıtı `status` bloğu taşır (sahte `Deps` ile `TestClient`).
- [ ] **Step 2–4.** Aşama şeridi, PR bağlantıları, `next.reason` metni sunucudan gelir; **Sürüm adımını başlat** düğmesi yalnız `next.ready` iken etkin. Kart mevcut tema değişkenleri ve bileşenleriyle; kabuk kromunda emoji yok.
- [ ] **Step 5: Commit** — `feat(techdebt): yükseltme kartında aşama ve sürüm adımı`

---

## Aşama D — otomatik ilerletme

### Task 10: `upgrade.advance` ve sunucu yoklayıcısı

**Files:** Modify `quality/upgrade.py`, `server.py`; Test

**Interfaces — Produces:**
- `upgrade.advance(client, *, team: str, deps=None, create_job=None, open_jobs=None) -> list[dict]` — her izleme işi için `{"repo","lang","took": [gid…],"queued_step": str|None,"closed": bool,"note": str}`. Kurallar spec §7: repo başına açık iş/PR sayısı `CREW_TECHDEBT_UPGRADE_MAX_OPEN`'ı aşmaz (ve `max_take`'i); alımlar `CREW_TECHDEBT_UPGRADE_SPRINT` sprint'ine; aynı repo × dil için kuyrukta / çalışan `upgrade_bump` varken ikinci açılmaz; aşama `tamam` ve kod PR'ı kapalıysa izleme işi kapatılır; `CREW_TECHDEBT_UPGRADE_CI_WAIT_H` aşılmış ve build yoksa izleme işine **bir kez** not (yorumda `tempo-upgrade-ci-wait` işareti).
- `upgrade.start_poller(get_client, *, interval_min: int)` — tek daemon iş parçacığı; `CREW_TECHDEBT_UPGRADE_AUTO` her turda yeniden okunur; bir turun hatası bir sonrakini durdurmaz. Takımlar: açık izleme işlerinin alanından.

- [ ] **Step 1: Test** — `test_upgrade_advance()`:

```python
r = upgrade.advance(c, team="T", deps=deps, create_job=fake_job, open_jobs=lambda repo: 2)   # MAX_OPEN 3
check("tavan: 2 açıkken 1 iş alınır", len(r[0]["took"]) == 1)
check("otomatik alım bir sonraki sprint'e", c.created[-1]["System.IterationPath"] == next_sprint)
check("tavan doluyken alım yok", upgrade.advance(c, ..., open_jobs=lambda repo: 3)[0]["took"] == [])
check("hazır → ci adımı kuyruğa", r_ready[0]["queued_step"] == "ci")
check("kuyrukta bump varken ikinci açılmaz", r_dup[0]["queued_step"] is None)
check("tamam → izleme işi kapanır", r_done[0]["closed"])
check("CI bekleme notu bir kez", notes == 1)   # advance iki kez çağrılır
```

- [ ] **Step 2–4.**
- [ ] **Step 5: Commit** — `feat(techdebt): sürüm yükseltmede otomatik ilerletme`

### Task 11: Belge ve uçtan uca kuru doğrulama

**Files:** Modify `CLAUDE.md` ("Notes for editing" altına bir madde), `docs/superpowers/specs/2026-11-17-language-upgrade-design.md` (Durum satırı)

- [ ] **Step 1:** Tüm test dosyası yeşil; `.venv/bin/python -c "from agile_sdlc_crew.server import app"` hatasız.
- [ ] **Step 2: Salt-okunur gerçek veri doğrulaması** (Azure'a yazma **yok**): `upgrade.plan` ve `upgrade.scan` gerçek `AzureDevOpsClient` ile `core` (PHP 8.2), `gocrons` (Go) ve bir Python reposu için çalıştırılır; çıktıdan hedef, site listesi, engelleyen bulgu sayısı ve probe durumu not edilir. `CREW_UPGRADE_PHP_BINS=8.4=/opt/homebrew/opt/php@8.4/bin/php` ile.
- [ ] **Step 3:** CLAUDE.md maddesi: ne yapar, bayraklar, üç tuzak (kod reposu kısıtı en son; merge'den önceki build kanıt değil; site satırı değişmişse push yok) ve Step 2'de ölçülen sayılar.
- [ ] **Step 4: Commit** — `docs: sürüm yükseltme — CLAUDE.md, ölçümler`
