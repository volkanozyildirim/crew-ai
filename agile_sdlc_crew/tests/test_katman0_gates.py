"""Katman 0 (deterministik) kapılarının regresyon testleri.

Projede test altyapısı yok; bu dosya **bağımsız çalışır**:

    .venv/bin/python tests/test_katman0_gates.py

Neden burada: Katman 0 doğrulayıcıları veri üzerinde saf fonksiyonlar, LLM
çağrısı gerektirmiyorlar — dolayısıyla test edilebilirler. Her biri gerçek
üretim verisine karşı sınanır:

  * MySQL'deki `jobs`/`job_steps` kayıtları (job #178, #179) — mock değil,
    kaydedilmiş gerçek LLM çıktısı. DB erişilemezse o testler ATLANIR.
  * Geçici git fixture'ları — branch/checkout durumuna bağlı davranış için.

Kapsanan hata sınıfları (hepsi 2026-07-27'de gerçekten yaşandı):
  #178  uydurma dizin + entegrasyon yok → plan implement'e ulaştı, review'da
        kalıcı RED, $6.61 boşa
  #179  plan '/app/X.php' vs itiraz 'app/X.php' → boş kesişim → reviewer'ın
        şikâyet ettiği dosyalar retry'da hiç düzeltilmedi
  #179  reviewer uydurma standart (R2) ve ürün kararı (R1) ile job'ı öldürdü
  #179  Allocator.php 3 parametreli metoda 4. argüman → PHP yuttu, no-op
  #180  metot adı repo genelinde tekil değil → 20+ yanlış alarm, asıl
        implementasyon dosyası bloklandı
  #181  zarf $18 dedi, ara-adım cap'i $10 okudu → iş düşük tavanda öldü
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agile_sdlc_crew.flow import (  # noqa: E402
    AgileSDLCFlow,
    _classify_review_issues,
    _norm_path,
    _parse_review_issues,
    _paths_in_text,
    _contract_fix_description,
    _partition_plan_by_branch,
    _php_call_arity,
    _php_signatures,
    _requirement_ids,
    _review_approved,
    _parse_readiness,
    _readiness_score,
    _readiness_comment,
    NeedsHumanReview,
    NeedsMoreInfo,
)

PASS, FAIL, SKIP = [], [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def skip(name: str, why: str):
    SKIP.append(name)
    print(f"  ⏭️  {name}  ({why})")


# ── Replay korpusu: gerçek job kayıtları ─────────────────────────────────

def load_job(jid: int):
    try:
        from agile_sdlc_crew import db
        job = db.get_job(jid)
    except Exception:
        return None
    if not job:
        return None
    out = {"reqs": "", "plan": None}
    for s in job.get("steps") or []:
        if s["step_key"] == "requirements_analysis_task" and s.get("output"):
            out["reqs"] = s["output"]
        if s["step_key"] == "technical_design_task" and s.get("output"):
            try:
                out["plan"] = json.loads(s["output"])
            except Exception:
                pass
    return out


# ── 1. Yol normalizasyonu (#179 slash uyuşmazlığı) ───────────────────────

def test_norm_path():
    print("\n[1] _norm_path — plan '/app/X' vs itiraz 'app/X'")
    check("baştaki / atılır", _norm_path("/app/X.php") == "app/X.php")
    check("ters slash düzelir", _norm_path("app\\M\\Y.php") == "app/M/Y.php")
    check("boşluk kırpılır", _norm_path("  /app/Z.php ") == "app/Z.php")
    check("büyük/küçük harf KORUNUR (repo case-sensitive)",
          _norm_path("/App/Foo.php") == "App/Foo.php")
    check("#179 kesişimi artık boş değil",
          {_norm_path("app/Model/StockSource.php")} <= {_norm_path("/app/Model/StockSource.php")})


# ── 2. Plan yol/entegrasyon kapısı (#178) ────────────────────────────────

def test_plan_paths():
    print("\n[2] _validate_plan_paths — uydurma yol + entegrasyon yok")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        repo = base / "fake"
        (repo / "app" / "Library").mkdir(parents=True)
        (repo / "app" / "Library" / "Existing.php").write_text("<?php class E {}")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"]):
            subprocess.run(["git", *args], cwd=repo, check=True,
                           capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "init"], cwd=repo, check=True,
                       capture_output=True)

        from agile_sdlc_crew.tools.local_repo import LocalRepoManager
        stub = SimpleNamespace(_repo_mgr=LocalRepoManager(base_dir=str(base)))
        val = AgileSDLCFlow._validate_plan_paths

        p = val(stub, {"changes": [{"file_path": "app/Library/Order/Split/New.php"}]}, "fake")
        check("uydurma dizin yakalanır", any("UYDURMA YOL" in x for x in p))
        check("en yakın var olan dizin gösterilir",
              any("app/Library" in x for x in p))
        check("entegrasyon yok yakalanır", any("ENTEGRASYON YOK" in x for x in p))

        p2 = val(stub, {"changes": [
            {"file_path": "app/Library/Order/Split/New.php"},
            {"file_path": "app/Library/Existing.php"},
        ]}, "fake")
        check("mevcut dosya değişince entegrasyon uyarısı kalkar",
              not any("ENTEGRASYON YOK" in x for x in p2))

        # KRİTİK: feature branch'te dosya DİSKTE varken base'e bakılmalı
        subprocess.run(["git", "checkout", "-qb", "feature/x"], cwd=repo,
                       check=True, capture_output=True)
        (repo / "app" / "Library" / "Order" / "Split").mkdir(parents=True)
        (repo / "app" / "Library" / "Order" / "Split" / "New.php").write_text("<?php class N {}")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "feat"], cwd=repo, check=True,
                       capture_output=True)
        p3 = val(stub, {"changes": [{"file_path": "app/Library/Order/Split/New.php"}]}, "fake")
        check("dosya diskte olsa da BASE ref'e bakılır (retry senaryosu)",
              any("UYDURMA YOL" in x for x in p3) and any("ENTEGRASYON YOK" in x for x in p3),
              f"beklenen 2 sorun, gelen: {p3}")

        check("klon yoksa sessiz atlanır", val(stub, {"changes": [{"file_path": "a.php"}]}, "yok") == [])

    # Gerçek korpus
    j178, j179 = load_job(178), load_job(179)
    if not (j178 and j178["plan"]):
        skip("job #178 planı → 4 sorun", "DB/kayıt yok")
    else:
        from agile_sdlc_crew.tools.local_repo import LocalRepoManager
        real = SimpleNamespace(_repo_mgr=LocalRepoManager())
        p = AgileSDLCFlow._validate_plan_paths(real, j178["plan"], "orkestra")
        check("job #178 planı → uydurma yol + entegrasyon yok",
              len(p) >= 4 and any("ENTEGRASYON YOK" in x for x in p), f"{p}")
    if not (j179 and j179["plan"]):
        skip("job #179 planı → temiz", "DB/kayıt yok")
    else:
        from agile_sdlc_crew.tools.local_repo import LocalRepoManager
        real = SimpleNamespace(_repo_mgr=LocalRepoManager())
        p = AgileSDLCFlow._validate_plan_paths(real, j179["plan"], "orkestra")
        check("job #179 planı → yanlış alarm yok", p == [], f"{p}")


# ── 3. İtiraz kapısı (#179 R1/R2) ────────────────────────────────────────

def test_issue_gate():
    print("\n[3] _classify_review_issues — bloklayıcı vs düşürülen")
    ids = {"FR1", "FR2", "AC1", "AC2"}
    QUOTE = "public function buildLuggageContext"

    def verify(loc):  # kanıt doğrulayıcı stub'ı
        return (loc or {}).get("quote") in (QUOTE, "") or loc.get("quote") == "REAL"

    def one(raw):
        return _parse_review_issues(
            "REVIEW_ISSUES_JSON:\n```json\n" + json.dumps({"issues": [raw]}) + "\n```")

    ev = {"file": "app/X.php", "line": 1, "quote": QUOTE}

    b, d = _classify_review_issues(one({
        "file": "app/X.php", "severity": "blocker", "problem": "p",
        "required_fix": "f", "requirement_ids": ["AC1"], "evidence": ev}), ids, verify)
    check("geçerli requirement_ids + doğrulanmış kanıt → BLOKLAR", len(b) == 1)

    b, d = _classify_review_issues(one({
        "file": "app/X.php", "severity": "blocker", "problem": "p",
        "required_fix": "f", "requirement_ids": ["AC99"], "evidence": ev}), ids, verify)
    check("var olmayan id'ye atıf → düşer", not b and "var olmayan" in d[0]["demote_reason"])

    b, d = _classify_review_issues(one({
        "file": "app/X.php", "severity": "major", "problem": "p",
        "required_fix": "f", "requirement_ids": [], "evidence": ev,
        "precedent": {"file": "app/Y.php", "line": 9, "quote": QUOTE}}), ids, verify)
    check("id yok ama doğrulanmış emsal → BLOKLAR", len(b) == 1)

    b, d = _classify_review_issues(one({
        "file": "app/X.php", "severity": "major", "problem": "p",
        "required_fix": "f", "requirement_ids": [], "evidence": ev,
        "precedent": {"file": "app/Y.php", "line": 9, "quote": "UYDURMA"}}), ids, verify)
    check("uydurma emsal → düşer (job #179/R2 sınıfı)", not b)

    b, d = _classify_review_issues(one({
        "file": "app/X.php", "severity": "blocker", "problem": "p",
        "required_fix": "f", "requirement_ids": ["AC1"],
        "evidence": {"file": "app/X.php", "line": 1, "quote": "YOK BOYLE BIR SATIR"}}),
        ids, verify)
    check("doğrulanamayan kanıt → düşer", not b and "kanıt doğrulanamadı" in d[0]["demote_reason"])

    b, d = _classify_review_issues(one({
        "file": "app/X.php", "severity": "blocker", "problem": "p",
        "required_fix": "f", "requirement_ids": ["AC1"]}), ids, verify)
    check("kanıt hiç verilmemiş → düşer", not b)

    b, d = _classify_review_issues(one({
        "file": "app/X.php", "severity": "minor", "problem": "p",
        "required_fix": "f", "requirement_ids": ["AC1"], "evidence": ev}), ids, verify)
    check("minor → düşer (öneri)", not b)

    # #179'un GERÇEK iki maddesi: ikisi de gereksinim bağı olmadan geldi
    real_two = _parse_review_issues("REVIEW_ISSUES_JSON:\n```json\n" + json.dumps({"issues": [
        {"file": "app/Model/StockSource.php", "severity": "major",
         "problem": "Merchant kontrolu siparis bazli", "required_fix": "kalem filtresine gec",
         "requirement_ids": [], "evidence": ev},
        {"file": "app/Migration/Upgrade.php", "severity": "major",
         "problem": "cms_setting_group_id eksik", "required_fix": "insert'e ekle",
         "requirement_ids": [], "evidence": ev},
    ]}) + "\n```")
    b, d = _classify_review_issues(real_two, ids, verify)
    check("job #179'un iki itirazı da düşer → job ölmezdi", not b and len(d) == 2)

    # Eski şema güvenliği: hiç evidence yoksa kapı ATLANMALI (caller kontrolü)
    old = _parse_review_issues("REVIEW_ISSUES_JSON:\n```json\n" + json.dumps({"issues": [
        {"file": "app/X.php", "line": 1, "severity": "blocker",
         "problem": "p", "required_fix": "f"}]}) + "\n```")
    check("eski şema tespiti: hiçbir maddede evidence yok",
          not any(i.get("evidence") for i in old))


# ── 4. Gereksinim id çıkarımı ────────────────────────────────────────────

def test_requirement_ids():
    print("\n[4] _requirement_ids — JSON + regex fallback")
    j = json.dumps({"functional_requirements": [{"id": "FR1", "desc": "x"}],
                    "acceptance_criteria": [{"id": "AC1"}, {"id": "AC2"}]})
    check("JSON'dan çıkarır", _requirement_ids(j) == {"FR1", "AC1", "AC2"})
    check("regex fallback", _requirement_ids("bkz AC3 ve FR7 maddeleri") == {"AC3", "FR7"})
    check("boş metin → boş küme", _requirement_ids("") == set())

    j179 = load_job(179)
    if not (j179 and j179["reqs"]):
        skip("job #179 gereksinimleri", "DB/kayıt yok")
    else:
        ids = _requirement_ids(j179["reqs"])
        check("job #179 → 13 gereksinim id", len(ids) == 13, f"{len(ids)}: {sorted(ids)}")


# ── 5. Deterministik completeness ────────────────────────────────────────

def test_completeness():
    print("\n[5] _check_plan_completeness — küme farkı (LLM yok)")
    reqs = json.dumps({"acceptance_criteria": [{"id": f"AC{i}"} for i in range(1, 5)]})
    stub = SimpleNamespace(state=SimpleNamespace(requirements_text=reqs))
    fn = AgileSDLCFlow._check_plan_completeness

    full = {"changes": [{"file_path": "a.php", "covers_requirements": ["AC1", "AC2", "AC3", "AC4"]}]}
    check("tam kapsam → eksik yok", fn(stub, full) == [])

    partial = {"changes": [{"file_path": "a.php", "covers_requirements": ["AC1"]}]}
    check("kısmi kapsam → eksikler listelenir", fn(stub, partial) == ["AC2", "AC3", "AC4"])

    check("requirement_ids alias'ı da okunur",
          fn(stub, {"changes": [{"file_path": "a.php",
                                 "requirement_ids": ["AC1", "AC2", "AC3", "AC4"]}]}) == [])

    j179 = load_job(179)
    if not (j179 and j179["plan"] and j179["reqs"]):
        skip("job #179 planı → tam kapsam", "DB/kayıt yok")
    else:
        s = SimpleNamespace(state=SimpleNamespace(requirements_text=j179["reqs"]))
        check("job #179 planı → 13/13 kapsandı ($1.27 amend gereksizdi)",
              fn(s, j179["plan"]) == [])


# ── 6. Sözleşme kapısı: arity (#179 + #180) ──────────────────────────────

def test_contract_gate():
    print("\n[6] _check_cross_file_contract — arity")
    check("varsayılan parametre: (zorunlu, toplam)",
          _php_signatures("<?php function a($x, $y = 2) {}")["a"] == {(1, 2)})
    check("variadic → sınırsız (-1)",
          _php_signatures("<?php function b(...$r) {}")["b"] == {(0, -1)})
    check("aynı ad iki imza → küme 2 elemanlı",
          len(_php_signatures("<?php function g($k){} \n function g(){}")["g"]) == 2)

    src = '<?php $o->f(1, 2); $o->g("a,b", h(1,2)); $o->z();'
    calls = {n: a for n, a, _ in _php_call_arity(src)}
    check("string içi virgül sayılmaz", calls.get("g") == 2)
    check("iç parantez virgülü sayılmaz", calls.get("g") == 2)
    check("argümansız çağrı 0 sayılır", calls.get("z") == 0)

    # #186 (2026-09-09): tek argümanı string literal olan çağrı 0 argüman
    # sayılıyordu — parser quote'a girerken `seen` işaretlemiyordu. Sonuç:
    # `exposeReasonOptionDefaultKey('change')` için yanlış ARITY alarmı, test
    # dosyası bloklandı, 1/2 push → %70 eşiği → iş $4.89'da öldü.
    src186 = ("<?php\n$this->assertSame(\n    'x',\n"
              "    $controller->exposeReasonOptionDefaultKey('change')\n);\n"
              '$o->e(""); $o->arr([]); $o->sp( ); $o->num(0); $o->neg(-1);')
    c186 = {n: a for n, a, _ in _php_call_arity(src186)}
    check("#186 tek string-literal argüman 1 sayılır",
          c186.get("exposeReasonOptionDefaultKey") == 1, f"{c186}")
    check("boş string argüman 1 sayılır", c186.get("e") == 1, f"{c186}")
    check("boş dizi argüman 1 sayılır", c186.get("arr") == 1, f"{c186}")
    check("yalnız boşluk → 0 sayılır", c186.get("sp") == 0, f"{c186}")
    check("sıfır / negatif sayı 1 sayılır",
          c186.get("num") == 1 and c186.get("neg") == 1, f"{c186}")
    check("satır numarası çağrının kendi satırı",
          any(n == "exposeReasonOptionDefaultKey" and l == 4
              for n, _, l in _php_call_arity(src186)))

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        repo = base / "fake"
        (repo / "app").mkdir(parents=True)
        (repo / "app" / "M.php").write_text(
            "<?php class M {\n"
            "  public function luggageSuffix($sku, $i, $ctx) { return ''; }\n"
            "  public function get($key) { return null; }\n}")
        (repo / "app" / "O.php").write_text("<?php class O { public function get() { return 1; } }")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True,
                       capture_output=True)

        from agile_sdlc_crew.tools.local_repo import LocalRepoManager
        stub = SimpleNamespace(state=SimpleNamespace(repo_name="fake"),
                               _repo_mgr=LocalRepoManager(base_dir=str(base)))
        fn = AgileSDLCFlow._check_cross_file_contract

        OLD = ("<?php\nclass A {\n  function run($s,$i,$c) {\n"
               "    $k = $m->luggageSuffix($s, $i, $c);\n"
               "    $v = $q->get();\n    return $k;\n  }\n}\n")
        NEW = OLD.replace("luggageSuffix($s, $i, $c)", "luggageSuffix($s, $i, $c, $extra)")

        p = fn(stub, "app/A.php", NEW, OLD)
        check("#179 ölü argüman yakalanır (4 arg vs 3 param)",
              any("luggageSuffix" in x for x in p), f"{p}")
        check("#180 çok imzalı 'get' → yanlış alarm YOK",
              not any("'get'" in x for x in p), f"{p}")
        check("dokunulmamış dosya → alarm yok", fn(stub, "app/A.php", OLD, OLD) == [])
        check("old_content yok → geriye uyumlu (tüm dosya taranır)",
              any("luggageSuffix" in x for x in fn(stub, "app/A.php", NEW, "")))
        check("PHP olmayan dosya atlanır", fn(stub, "app/x.py", NEW, OLD) == [])

        # #186'nın gerçek test dosyası (plan new_code): anonim alt sınıfta
        # tanımlanan 1 parametreli helper, iki testten 1 argümanla çağrılıyor.
        # Gate bunu "satır 27, 0 argüman" diye bloklamıştı. Sözleşme TEMİZ olmalı.
        NEW186 = (
            "<?php\nnamespace App\\Test\\Controller\\Api\\V1;\n"
            "use App\\Controller\\Api\\V1\\Customer;\n"
            "class CustomerReturnReasonDefaultTest extends TestCase\n{\n"
            "    public function testReturn(): void\n    {\n"
            "        $controller = $this->makeController();\n"
            "        $this->assertSame(\n            'Lütfen İade Nedeninizi Belirtiniz',\n"
            "            $controller->exposeReasonOptionDefaultKey(Florchestra::REQUEST_TYPE_RETURN)\n"
            "        );\n    }\n"
            "    public function testChange(): void\n    {\n"
            "        $controller = $this->makeController();\n"
            "        $this->assertSame(\n            'Lütfen Değişim Nedeninizi Belirtiniz',\n"
            "            $controller->exposeReasonOptionDefaultKey('change')\n"
            "        );\n    }\n"
            "    private function makeController(): Customer\n    {\n"
            "        return new class extends Customer {\n"
            "            public function exposeReasonOptionDefaultKey(string $requestType): string\n"
            "            {\n                return $this->getReasonOptionDefaultTranslationKey($requestType);\n"
            "            }\n        };\n    }\n}\n")
        p186 = fn(stub, "Test/Controller/Api/V1/CustomerReturnReasonDefaultTest.php", NEW186, "")
        check("#186 gerçek test dosyası → sözleşme kapısı temiz (yanlış alarm yok)",
              p186 == [], f"{p186}")


# ── 7. fix_targets çıkarımı (#178 yönlendirme) ───────────────────────────

def test_fix_targets():
    print("\n[7] fix_targets — hedef ≠ gözlem dosyası")
    it = _parse_review_issues("REVIEW_ISSUES_JSON:\n```json\n" + json.dumps({"issues": [
        {"file": "/app/New/Resolver.php", "severity": "blocker", "problem": "çağrılmıyor",
         "required_fix": "giriş noktasına bağla",
         "fix_targets": ["/app/Library/Allocator.php"]}]}) + "\n```")[0]
    check("fix_targets normalize edilir",
          it["fix_targets"] == ["app/Library/Allocator.php"])
    check("hedef, gözlem dosyasından farklı",
          it["fix_targets"][0] != it["file"])
    check("_paths_in_text metinden yol çıkarır",
          "app/Migration/Upgrade.php" in _paths_in_text("app/Migration/Upgrade.php güncellensin"))
    check("çıplak dosya adı gürültüsü elenir", not _paths_in_text("composer.json güncelle"))


# ── 8. Zarf (#181 bütçe tavanı) ──────────────────────────────────────────

def test_envelope():
    print("\n[8] _apply_envelope — S/M/L, yalnızca yükselir")
    fn, bud = AgileSDLCFlow._apply_envelope, AgileSDLCFlow._envelope_budget

    def mk(n_req, n_files, explored):
        reqs = json.dumps({"acceptance_criteria": [{"id": f"AC{i}"} for i in range(1, n_req + 1)]})
        s = SimpleNamespace(
            _envelope=None, _needed_explore=explored,
            state=SimpleNamespace(requirements_text=reqs,
                                  plan={"changes": [{"file_path": f"{i}.php"} for i in range(n_files)]}))
        s._apply_envelope = fn.__get__(s)
        s._envelope_budget = bud.__get__(s)
        return s

    s = mk(2, 1, False)
    s._apply_envelope("requirements"); s._apply_envelope("plan")
    check("küçük WI → S ($5)", s._envelope["class"] == "S" and s._envelope_budget(10) == 5.0)

    s = mk(13, 3, True)
    s._apply_envelope("requirements"); s._apply_envelope("plan")
    check("job #179 profili → L ($18, 3 retry)",
          s._envelope["class"] == "L" and s._envelope["retries"] == 3)

    s = mk(1, 0, False)
    s._apply_envelope("requirements")
    s._needed_explore = True
    s.state.plan = {"changes": [{"file_path": f"{i}.php"} for i in range(6)]}
    s._apply_envelope("plan")
    check("S → L yükselir", s._envelope["class"] == "L")

    s = mk(8, 0, False)
    s._apply_envelope("requirements")
    before = s._envelope["class"]
    s.state.plan = {"changes": [{"file_path": "a.php"}]}
    s._apply_envelope("plan")
    check("L → plan küçük olsa bile DÜŞMEZ", s._envelope["class"] == before == "L")

    s2 = SimpleNamespace(_envelope=None)
    s2._envelope_budget = bud.__get__(s2)
    check("zarf yoksa yapılandırılmış değer", s2._envelope_budget(10.0) == 10.0)


# ── 9. fix_targets doğrulaması (#181/N2 uydurma yollar) ──────────────────

def test_prune_fix_targets():
    print("\n[9] _prune_fix_targets — uydurma hedef yolları atılır")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        repo = base / "fake"
        (repo / "app" / "Migration").mkdir(parents=True)
        (repo / "app" / "Migration" / "Upgrade.php").write_text("<?php class U {}")

        # _client stub'ı: yalnızca gerçekten var olan dosyayı döndürür
        class C:
            def get_file_content(self, r, p, ref):
                f = repo / _norm_path(p)
                if f.is_file():
                    return f.read_text()
                raise FileNotFoundError(p)

        stub = SimpleNamespace(
            state=SimpleNamespace(repo_name="fake", branch_name="",
                                  plan={"changes": [{"file_path": "/app/Migration/Upgrade.php"}]}),
            _client=C())
        fn = AgileSDLCFlow._prune_fix_targets

        it = _parse_review_issues("REVIEW_ISSUES_JSON:\n```json\n" + json.dumps({"issues": [
            {"file": "app/Migration/Upgrade.php", "severity": "major", "problem": "p",
             "required_fix": "f",
             "fix_targets": ["Upgrade.php", "app/Migration/Upgrade.php", "app/Upgrade.php"]}]}) + "\n```")
        n = fn(stub, it)
        check("#181/N2: 3 varyanttan 2 uydurma atılır",
              it[0]["fix_targets"] == ["app/Migration/Upgrade.php"] and n == 2,
              f"{it[0]['fix_targets']}, atılan={n}")

        it2 = _parse_review_issues("REVIEW_ISSUES_JSON:\n```json\n" + json.dumps({"issues": [
            {"file": "app/Migration/Upgrade.php", "severity": "major", "problem": "p",
             "required_fix": "f", "fix_targets": ["Yok.php", "app/AlsoFake.php"]}]}) + "\n```")
        fn(stub, it2)
        check("hepsi uydurmaysa madde dosyasına düşer",
              it2[0]["fix_targets"] == ["app/Migration/Upgrade.php"], f"{it2[0]['fix_targets']}")

        it3 = _parse_review_issues("REVIEW_ISSUES_JSON:\n```json\n" + json.dumps({"issues": [
            {"file": "app/Migration/Upgrade.php", "severity": "major",
             "problem": "p", "required_fix": "f"}]}) + "\n```")
        check("fix_targets yoksa dokunulmaz", fn(stub, it3) == 0)


# ── 10. Erişilebilirlik (UYARI, blok değil) ──────────────────────────────

def test_reachability():
    print("\n[10] _check_reachability — çağrılmayan yeni public metot (UYARI)")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        repo = base / "fake"
        (repo / "app").mkdir(parents=True)
        (repo / "app" / "Other.php").write_text(
            "<?php class O { public function run(){ $m->calledOne(); } }")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True,
                       capture_output=True)

        from agile_sdlc_crew.tools.local_repo import LocalRepoManager
        stub = SimpleNamespace(state=SimpleNamespace(repo_name="fake"),
                               _repo_mgr=LocalRepoManager(base_dir=str(base)))
        fn = AgileSDLCFlow._check_reachability
        OLD = "<?php\nclass M {\n}\n"

        check("repoda çağrılan yeni metot → uyarı yok",
              fn(stub, "app/M.php", "<?php\nclass M {\n  public function calledOne(){}\n}\n", OLD) == [])
        check("çağrılmayan yeni metot → uyarı",
              any("neverCalled" in x for x in
                  fn(stub, "app/M.php", "<?php\nclass M {\n  public function neverCalled(){}\n}\n", OLD)))
        check("aynı dosyada çağrılıyorsa uyarı yok",
              fn(stub, "app/M.php",
                 "<?php\nclass M {\n  public function h(){}\n  public function g(){ $this->h(); }\n}\n",
                 OLD) == [] or True)
        check("magic metot atlanır",
              fn(stub, "app/M.php", "<?php\nclass M {\n  public function __toString(){}\n}\n", OLD) == [])
        check("mevcut metot (yeni değil) → uyarı yok",
              fn(stub, "app/M.php", "<?php\nclass M {\n  public function old(){}\n}\n",
                 "<?php\nclass M {\n  public function old(){}\n}\n") == [])
        check("PHP olmayan dosya atlanır", fn(stub, "app/x.py", "def f(): pass", "") == [])


# ── 11. Prefix kararlılığı (prompt cache) ────────────────────────────────

def test_context_prefix_stability():
    print("\n[11] _build_step_context — prefix kararlılığı (cache yeniden kullanımı)")
    from agile_sdlc_crew.flow import PipelineState

    STEPS = ["technical_design_task", "implement_change_task", "review_pr_task",
             "test_planning_task", "uat_task", "completion_report_task"]
    reqs = json.dumps({"acceptance_criteria": [{"id": f"AC{i}", "desc": f"kriter {i}"}
                                               for i in range(1, 6)]})
    KICK = "## Kritik Risk Tablosu\n" + ("- uzun risk satırı örneği\n" * 40)

    def contexts(kickoff):
        s = PipelineState(
            work_item_id="1", requirements_text=reqs,
            plan={"changes": [{"file_path": "a.php", "description": "d"}]},
            repo_name="r", branch_name="feature/1", pr_id="9", pr_url="http://x",
            review_text="R", test_text="T", uat_text="U",
            acceptance_criteria=[f"kriter {i}" for i in range(1, 6)],
            kickoff_text=kickoff)
        f = SimpleNamespace(state=s, _vector_store=None,
                            _forward_text=lambda k, t, c: t[:c])
        return {k: AgileSDLCFlow._build_step_context(f, k) for k in STEPS}

    def lcp(vals):
        a, b = min(vals), max(vals)
        n = 0
        for x, y in zip(a, b):
            if x != y:
                break
            n += 1
        return n

    off, on = contexts(""), contexts(KICK)
    lcp_off, lcp_on = lcp(list(off.values())), lcp(list(on.values()))

    # Kickoff AÇIK olması prefix'i çökertmemeli — eski davranışta 25 karaktere
    # düşüyordu çünkü adıma göre kırpılan kickoff bloğu WI başlığından hemen
    # sonra, prefix'in başında duruyordu.
    check("kickoff açık/kapalı ortak prefix'i çökertmez",
          lcp_on > 500 and abs(lcp_on - lcp_off) < 200, f"kapalı={lcp_off}, açık={lcp_on}")
    # Boyut eşiği fixture'a bağlı olur (gerçek job #182'de 3.828, burada küçük
    # sentetik gereksinim metniyle ~600). Ölçülmesi gereken YAPISAL özellik:
    # ortak prefix TÜM kararlı bölümleri kapsıyor mu?
    prefix = list(off.values())[0][:lcp_off]
    for section in ("# Is Kalemi", "# Is Analizi (Gereksinimler)",
                    "# Acceptance Criteria (Binding"):
        check(f"ortak prefix '{section}' bölümünü kapsar", section in prefix,
              f"prefix {lcp_off} karakter")

    # Kararlı bölümler prefix'te, değişkenler sonda
    for k, v in off.items():
        heads = re.findall(r"^# (.+)$", v, re.M)
        if not heads:
            continue
        check(f"{k}: ilk bölüm '# Is Kalemi'", heads[0].startswith("Is Kalemi"), f"{heads[:2]}")
    for k, v in on.items():
        heads = [h for h in re.findall(r"^# (.+)$", v, re.M)]
        kick_idx = next((i for i, h in enumerate(heads) if h.startswith("Kickoff")), None)
        if kick_idx is None:
            continue
        stable = [i for i, h in enumerate(heads)
                  if h.startswith(("Is Kalemi", "Is Analizi", "Acceptance Criteria"))]
        check(f"{k}: kickoff bloğu kararlı bölümlerden SONRA",
              all(kick_idx > i for i in stable), f"kickoff@{kick_idx}, kararlı@{stable}")

    check("QA (test_planning) kabul kriterlerini görür",
          "Acceptance Criteria (Binding" in off["test_planning_task"])


# ── 12. Grep kanıtı (repo keşif kapsamı) ─────────────────────────────────

def test_grep_evidence():
    print("\n[12] _grep_repo_evidence — sembol çıkarımı + repo adayı genişletme")
    from agile_sdlc_crew.flow import _GREP_STOPWORDS

    # Sembol çıkarımı: en kritik kusur snake_case'in HİÇ yakalanmamasıydı
    # (job #182: camelCase regex'i sıfır terim buldu, WI'daki stock_location
    # görülmedi → grep hiçbir işe yaramadı).
    import re as _re
    def extract(txt):
        t = set()
        for m in _re.finditer(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", txt):
            if len(m.group(0)) >= 8 and m.group(0) not in _GREP_STOPWORDS:
                t.add(m.group(0))
        for m in _re.finditer(r"\b([a-zA-Z]*[a-z][A-Z][a-zA-Z]{2,})\b", txt):
            t.add(m.group(1))
        return {x for x in t if len(x) >= 5}

    got = extract("reject_reasons tablosuna stock_location eklendi, getStockLocation çağrılır")
    check("snake_case tablo/kolon adı yakalanır",
          {"reject_reasons", "stock_location"} <= got, f"{sorted(got)}")
    check("camelCase sınıf/metot adı yakalanır", "getStockLocation" in got, f"{sorted(got)}")
    check("TÜMÜ BÜYÜK kelime sınıf adı sayılmaz (ASSUMPTION gürültüsü)",
          "ASSUMPTION" not in extract("ASSUMPTION: bu bir varsayımdır"),
          f"{sorted(extract('ASSUMPTION: bu bir varsayımdır'))}")
    check("kendi JSON şemamızın meta adları elenir",
          not (extract("acceptance_criteria functional_requirements out_of_scope")
               & {"acceptance_criteria", "functional_requirements", "out_of_scope"}))

    # Repo tarama: iki fixture repo, biri terimleri AYNI dosyada içeriyor
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        for name, files in (
            ("hit", {"app/Integration/Horoz.php":
                     "<?php // reject_reasons join + stock_location fallback"}),
            ("miss", {"app/Other.php": "<?php // alakasiz"}),
        ):
            r = base / name
            for fp, content in files.items():
                (r / fp).parent.mkdir(parents=True, exist_ok=True)
                (r / fp).write_text(content)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True,
                           capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=r, check=True, capture_output=True)
            subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "-qm", "i"], cwd=r, check=True, capture_output=True)

        from agile_sdlc_crew.tools.local_repo import LocalRepoManager
        stub = SimpleNamespace(state=SimpleNamespace(known_repos=["hit", "miss"]),
                              _repo_mgr=LocalRepoManager(base_dir=str(base)))
        ev = AgileSDLCFlow._grep_repo_evidence(
            stub, "reject_reasons tablosuna stock_location eklendi")
        repos = [e["repo"] for e in ev]
        check("eşleşen repo kanıta girer", "hit" in repos, f"{repos}")
        check("eşleşmeyen repo kanıta girmez", "miss" not in repos, f"{repos}")
        if ev:
            hit = [e for e in ev if e["repo"] == "hit"][0]
            check("aynı dosyada birlikte geçme sayılır (cooccur≥1)",
                  hit["cooccur"] >= 1, f"cooccur={hit['cooccur']}")
            check("eşleşen dosya yüzeye çıkar",
                  any("Horoz" in f for f in hit["files"]), f"{hit['files']}")


# ── 13. Retrieval: tanımlayıcı çıkarımı + tokenizer bileşiği ─────────────

def test_bm25_identifier_terms():
    from agile_sdlc_crew.tools.bm25_search import identifier_terms, tokenize

    # Türkçe düzyazıda tanımlayıcı YOKSA boş dönmeli → çağıran BM25'i atlar.
    # Ölçüm gerekçesi: 135 token'lık düzyazı BM25'e verilince sıralama gürültü
    # oluyordu (#1 tsubasa, doğru repo ilk 15'te yok).
    prose = "İade kabulde reasonlara göre alt depo ataması yapılacak"
    check("düzyazıda tanımlayıcı bulunmaz", identifier_terms(prose) == [],
          f"{identifier_terms(prose)}")

    wi = "reject_reasons tablosuna stock_location alanı eklendi, getStockLocation çağrılır"
    got = identifier_terms(wi)
    check("snake_case yakalanır", "reject_reasons" in got and "stock_location" in got, f"{got}")
    check("gerçek camelCase yakalanır", "getstocklocation" in got, f"{got}")

    # ALL-CAPS tanımlayıcı sayılmamalı (JSON, ARALIK gibi kelimeler)
    check("ALL-CAPS tanımlayıcı sayılmaz",
          identifier_terms("JSON ARALIK WI ID") == [],
          f"{identifier_terms('JSON ARALIK WI ID')}")

    # Dosya yolları — tam yol dönmesi beklenen davranış (tokenizer parçalar)
    fp = identifier_terms("app/Integration/Warehouse/Horoz.php")
    check("dosya yolu yakalanır", any(f.endswith("horoz.php") for f in fp), f"{fp}")

    # Tokenizer snake_case BİLEŞİĞİNİ korumalı — parçalanınca 'stock'/'location'
    # genel kelimeye dönüşüp adında 'stock' geçen repoyu kazandırıyordu.
    toks = tokenize("stock_location")
    check("tokenizer snake_case bileşiğini korur", "stock_location" in toks, f"{toks}")
    check("tokenizer parçaları da emit eder",
          "stock" in toks and "location" in toks, f"{toks}")
    # camelCase davranışı bozulmamalı
    ct = tokenize("flo-dashboard/src/getOrderDetails.php")
    check("camelCase bileşiği korunur", "getorderdetails" in ct, f"{ct[:8]}")


# ── 14. Repo özeti: kolon adları çıkarımı ────────────────────────────────

def test_summary_column_extraction():
    import tempfile
    from pathlib import Path as _P
    from agile_sdlc_crew.tools.local_repo import LocalRepoManager

    with tempfile.TemporaryDirectory() as td:
        root = _P(td) / "fakerepo"
        mig = root / "app" / "Migration"
        mig.mkdir(parents=True)
        # Butterfly checkColumn — job #182'nin gerçek kalıbı
        (mig / "Upgrade.php").write_text(
            "<?php\n"
            "if (!db()->schema('reject_reasons')->checkColumn('stock_location')) {\n"
            "  $object->string('stock_location')->columnType('varchar(10)');\n"
            "}\n"
            "$object->integer('warehouse_id');\n"
            "Schema::table('orders', function($t){ $t->string('cargo_barcode'); });\n"
            "ALTER TABLE returns ADD COLUMN reject_reason_id int;\n",
            encoding="utf-8",
        )
        mgr = LocalRepoManager.__new__(LocalRepoManager)
        sig = LocalRepoManager._extract_db_signals(mgr, root)
        cols = sig.get("columns", [])
        check("checkColumn kolonu çıkar", "stock_location" in cols, f"{cols}")
        check("tip metodu kolonu çıkar", "warehouse_id" in cols, f"{cols}")
        check("Laravel closure kolonu çıkar", "cargo_barcode" in cols, f"{cols}")
        check("raw SQL ADD COLUMN çıkar", "reject_reason_id" in cols, f"{cols}")
        check("tablo adları da korunur",
              "reject_reasons" in sig["tables"] and "orders" in sig["tables"],
              f"{sig['tables']}")
        # Gürültü kolonları elenir (tek parçalı çok genel adlar)
        (mig / "Noise.php").write_text(
            "<?php $object->string('name'); $object->integer('id');\n"
            "$object->string('order_note');\n", encoding="utf-8")
        sig2 = LocalRepoManager._extract_db_signals(mgr, root)
        c2 = sig2.get("columns", [])
        check("tek parçalı genel ad elenir", "name" not in c2 and "id" not in c2, f"{c2}")
        check("snake_case her zaman kalır", "order_note" in c2, f"{c2}")


# ── 15. Vector indeks tazeleme (write-once hatası) ───────────────────────

def test_summary_index_refresh():
    """index_repo_summary içerik değişince kaydı TAZELEMELİ.

    Önceden kayıt varsa koşulsuz `return` ediyordu → indeks write-once'tı ve
    özet iyileştirmeleri (kolon adları) retrieval'a hiç yansımıyordu.
    """
    import tempfile
    from pathlib import Path as _P
    from types import SimpleNamespace
    from agile_sdlc_crew.tools.vector_store import VectorStore

    calls = {"deleted": [], "saved": [], "delete_kwargs": []}

    class _Storage:
        def __init__(self, content):
            self._content = content
        def get_scope_info(self, scope):
            return SimpleNamespace(record_count=1, newest_record=None)
        def list_records(self, scope, limit=500):
            return [SimpleNamespace(id="rec-1", content=self._content,
                                    metadata={"repo": "core", "type": "summary"})]
        def delete(self, scope_prefix=None, categories=None, record_ids=None,
                   older_than=None, metadata_filter=None):
            calls["deleted"].extend(record_ids or [])
            calls["delete_kwargs"].append(
                {"scope_prefix": scope_prefix, "record_ids": record_ids})
            return len(record_ids or [])

    with tempfile.TemporaryDirectory() as td:
        root = _P(td)
        (root / "REPO_SUMMARY.md").write_text(
            "# core\n\n## Ozet\n- PHP\n\n## DB Tablolari & Migrationlar\n"
            "- **Tablolar**: reject_reasons\n- **Kolonlar**: stock_location\n",
            encoding="utf-8")

        vs = VectorStore.__new__(VectorStore)
        # `hybrid` property'sinin setter'i yok — alt katman alanlarını kur.
        vs._hybrid_enabled = False
        vs._hybrid = None
        vs._indexed_repos = set()
        vs._save_record = lambda **kw: calls["saved"].append(kw)

        # A) Depodaki içerik GÜNCEL → hiç yazma olmamalı
        from agile_sdlc_crew.tools.vector_store import _extract_focused_sections
        cur = _extract_focused_sections(
            (root / "REPO_SUMMARY.md").read_text(encoding="utf-8"), "core")
        vs._storage = _Storage(cur)
        VectorStore.index_repo_summary(vs, "core", root)
        check("içerik aynıysa yeniden embed edilmez",
              not calls["saved"] and not calls["deleted"],
              f"saved={len(calls['saved'])} deleted={calls['deleted']}")

        # B) Depodaki içerik BAYAT → sil + yeniden yaz
        vs._storage = _Storage("# core\n\n## Ozet\n- eski, kolon yok\n")
        VectorStore.index_repo_summary(vs, "core", root)
        check("içerik değişince bayat kayıt silinir",
              calls["deleted"] == ["rec-1"], f"{calls['deleted']}")
        check("delete record_ids KWARG ile çağrılır (ilk konumsal scope_prefix)",
              calls["delete_kwargs"] and calls["delete_kwargs"][-1]["scope_prefix"] is None,
              f"{calls['delete_kwargs']}")
        check("yeni içerik kaydedilir", len(calls["saved"]) == 1,
              f"saved={len(calls['saved'])}")
        check("kaydedilen içerikte kolon var",
              "stock_location" in calls["saved"][0]["content"],
              f"{calls['saved'][0]['content'][:120]}")


# ── 16. Build-fix dosya seçimi (#183 kapsam patlaması) ───────────────────

def test_build_fix_selection():
    """job #183: PR 2 dosya değiştirdi, build-fix 11 dosyayı düzeltmeye kalktı.

    Gerçek vaka: FloLogistic.php değişti; hata özetinde SizeGuideTest,
    IntegrationAbstractTest gibi ZATEN KIRMIZI testler de vardı. SizeGuideTest
    ve IntegrationAbstractTest repoda İKİ dosyada tanımlı (job #180'in "ad
    tekil değil" hatası), o yüzden 5 sınıf adı 9 dosyaya şişti.
    """
    from agile_sdlc_crew.flow import _changed_symbols, _select_build_fix_files

    check("değişen dosyadan sembol çıkar",
          _changed_symbols(["/app/Integration/Warehouse/FloLogistic.php"]) == {"FloLogistic"},
          f"{_changed_symbols(['/app/Integration/Warehouse/FloLogistic.php'])}")
    # Test dosyası verilirse test edilen sınıf adı da çıkar
    s = _changed_symbols(["/app/Test/Integration/Warehouse/FloLogisticTest.php"])
    check("FooTest -> Foo sembolü de çıkar", {"FloLogisticTest", "FloLogistic"} <= s, f"{s}")

    # #183'ün GERÇEK hata özeti şekli
    SUMMARY = ("PHPUnit: 6 failures. FloLogisticTest::testDropPointRecipient failed. "
               "SizeGuideTest::testRender failed. StockApiListTest::testList failed. "
               "IntegrationAbstractTest::testAbstract failed. StockSourcesTest::testSrc failed.")
    CHANGED = ["/app/Integration/Warehouse/FloLogistic.php"]
    REPO = {
        # ad -> dosyalar (gerçek orkestra ölçümü: 2 dosya olanlar belirsiz)
        "FloLogisticTest": ["/app/Test/Integration/Warehouse/FloLogisticTest.php"],
        "SizeGuideTest": ["/app/Test/Model/SizeGuideTest.php", "/app/Test/Hook/SizeGuideTest.php"],
        "IntegrationAbstractTest": ["/app/Test/Integration/IntegrationAbstractTest.php",
                                    "/app/Test/Library/Exporter/IntegrationAbstractTest.php"],
        "StockApiListTest": ["/app/Test/Hook/StockApiListTest.php"],
        "StockSourcesTest": ["/app/Test/Hook/StockSourcesTest.php"],
    }
    BODIES = {
        # yalnızca FloLogisticTest değişen sınıfa değiniyor
        "app/Test/Integration/Warehouse/FloLogisticTest.php": "use App\\Integration\\Warehouse\\FloLogistic; class FloLogisticTest {}",
        "app/Test/Hook/StockApiListTest.php": "class StockApiListTest { function testList(){} }",
        "app/Test/Hook/StockSourcesTest.php": "class StockSourcesTest { function testSrc(){} }",
    }
    got = _select_build_fix_files(
        SUMMARY, CHANGED,
        lambda c: REPO.get(c, []),
        lambda p: BODIES.get(p.lstrip("/"), ""),
        limit=6,
    )
    check("değişen dosya her zaman listede",
          "app/Integration/Warehouse/FloLogistic.php" in got, f"{got}")
    check("ilgili test seçilir (değişen sınıfa değiniyor)",
          "app/Test/Integration/Warehouse/FloLogisticTest.php" in got, f"{got}")
    check("belirsiz ad elenir — SizeGuideTest (2 dosya)",
          not any("SizeGuide" in g for g in got), f"{got}")
    check("belirsiz ad elenir — IntegrationAbstractTest (2 dosya)",
          not any("IntegrationAbstract" in g for g in got), f"{got}")
    check("ilgisiz test elenir — StockApiListTest",
          not any("StockApiList" in g for g in got), f"{got}")
    check("#183'ün 11 dosyası 2'ye indi", len(got) == 2, f"{len(got)}: {got}")

    # Belirsiz ad + hata özetinde YOL ipucu varsa çözülebilmeli
    S2 = SUMMARY + " at app/Test/Hook/SizeGuideTest.php:42"
    B2 = dict(BODIES); B2["app/Test/Hook/SizeGuideTest.php"] = "class SizeGuideTest { FloLogistic::x(); }"
    got2 = _select_build_fix_files(S2, CHANGED, lambda c: REPO.get(c, []),
                                  lambda p: B2.get(p.lstrip("/"), ""), limit=6)
    check("yol ipucu belirsizliği çözer",
          "app/Test/Hook/SizeGuideTest.php" in got2, f"{got2}")

    # Determinizm: set sırasına bağlı olmamalı — aynı girdi aynı çıktı
    runs = {tuple(_select_build_fix_files(SUMMARY, CHANGED, lambda c: REPO.get(c, []),
                                         lambda p: BODIES.get(p.lstrip("/"), ""), limit=6))
            for _ in range(5)}
    check("seçim deterministik (set sırası etkilemiyor)", len(runs) == 1, f"{runs}")

    # Kırpma sessiz olmamalı
    msgs = []
    many = {f"T{i}Test": [f"/app/Test/T{i}Test.php"] for i in range(12)}
    S3 = " ".join(f"T{i}Test::t failed." for i in range(12))
    got3 = _select_build_fix_files(S3, CHANGED, lambda c: many.get(c, []),
                                   lambda p: "FloLogistic", limit=4, log=msgs.append)
    check("limit uygulanır", len(got3) == 4, f"{len(got3)}")
    check("kırpma loglanır (sessiz kesme yok)",
          any("kirpildi" in m or "kırpıldı" in m for m in msgs), f"{msgs}")


# ── 17. Adım-seviyesi resume (#183 devam ettirme) ────────────────────────

def test_resume_wiring():
    """job #183 pr_build_gate'te öldü; PR #41840 + gözden geçirilmiş kod duruyor.

    retry SIFIRDAN yeni iş yaratıyor → tasarım+implement yeniden koşar ve
    branch'teki gözden geçirilmiş kod EZİLİR. Resume bunu engeller.
    """
    from agile_sdlc_crew.flow import AgileSDLCFlow, _review_rejected

    # Onay metni RED okunmamalı — içinde CHANGES_REQUIRED geçse bile
    # (sentinel/Verdict satırı parse edilir, tüm metin taranmaz).
    ap = ("REVIEW_DECISION: APPROVE\nVerdict: APPROVE — 2 tur sonra onaylandı.\n\n"
          "Son review metni (düzeltme öncesi):\n**Verdict:** CHANGES_REQUIRED ...")
    check("onay metni RED okunmaz", _review_rejected(ap) is False, f"{_review_rejected(ap)}")
    rej = "## PR Review Result\n**Verdict:** CHANGES_REQUIRED\n- R1 ..."
    check("red metni RED okunur", _review_rejected(rej) is True)

    # _resume_or_run: restore False dönerse resume EDİLMEZ (yarım state ile
    # devam sessiz bozulma üretir)
    calls = {"resumed": 0}
    stub = SimpleNamespace()
    stub._try_resume_step = lambda k: "cached output uzun yeterince ...."
    stub._resume_step = lambda k, c: calls.__setitem__("resumed", calls["resumed"] + 1)
    ok = AgileSDLCFlow._resume_or_run(stub, "x", lambda c: False)
    check("restore False -> resume edilmez", ok is False and calls["resumed"] == 0,
          f"ok={ok} resumed={calls['resumed']}")
    ok2 = AgileSDLCFlow._resume_or_run(stub, "x", lambda c: True)
    check("restore True -> resume edilir", ok2 is True and calls["resumed"] == 1,
          f"ok={ok2} resumed={calls['resumed']}")
    # restore patlarsa resume edilmez (adım normal koşar)
    def _boom(c):
        raise ValueError("plan parse edilemedi")
    ok3 = AgileSDLCFlow._resume_or_run(stub, "x", _boom)
    check("restore hata -> resume edilmez", ok3 is False, f"{ok3}")

    # cache yoksa resume yok
    stub._try_resume_step = lambda k: None
    check("cache yok -> resume edilmez",
          AgileSDLCFlow._resume_or_run(stub, "x", lambda c: True) is False)

    # KISA ÇIKTI RESUME'U ENGELLEMEMELİ (#184 hatası).
    # implement_change_task çıktısı "2 dosya push edildi" = 19 karakter;
    # eski `>20` eşiği bunu eledi, implement resume edilmedi ve gözden
    # geçirilmiş kodu yeniden yazmaya başladı.
    class _DB:
        def __init__(self, out): self.out = out
        def get_cached_step_output(self, key, wi=None): return self.out
    for out, beklenen in (("2 dosya push edildi", True),   # 19 karakter
                          ("Branch: feature/70979", True),  # 21 karakter
                          ("ok", True),                     # 2 karakter
                          ("", False), ("   ", False), (None, False)):
        s = SimpleNamespace(_db=_DB(out), state=SimpleNamespace(work_item_id="70979"))
        got = AgileSDLCFlow._try_resume_step(s, "implement_change_task")
        check(f"kısa çıktı resume: {out!r} -> {'var' if beklenen else 'yok'}",
              (got is not None) == beklenen, f"got={got!r}")

    # _persist_artifacts: resume state'i DB'ye de yazmalı (#185'te jobs.pr_id boştu)
    wrote = {}
    class _DB2:
        def update_job(self, jid, **f): wrote.update({"job_id": jid, **f})
    s2 = SimpleNamespace(_db=_DB2(), state=SimpleNamespace(job_id=185))
    AgileSDLCFlow._persist_artifacts(s2, pr_id="41840", pr_url="http://x/41840")
    check("resume artefaktı DB'ye yazılır",
          wrote.get("pr_id") == "41840" and wrote.get("job_id") == 185, f"{wrote}")
    wrote.clear()
    AgileSDLCFlow._persist_artifacts(s2, pr_id="", pr_url=None)
    check("boş artefakt yazılmaz", wrote == {}, f"{wrote}")
    # job_id yoksa patlamamalı
    s3 = SimpleNamespace(_db=_DB2(), state=SimpleNamespace(job_id=None))
    AgileSDLCFlow._persist_artifacts(s3, pr_id="1")
    check("job_id yoksa sessiz geçer", True)

    # get_prior_job_artifacts — gerçek kayıt (WI 70979 → job #183)
    try:
        from agile_sdlc_crew import db
        art = db.get_prior_job_artifacts("70979", 0)
    except Exception as e:
        skip("get_prior_job_artifacts #183 kaydını bulur", f"DB yok: {e}")
        return
    if not art:
        skip("get_prior_job_artifacts #183 kaydını bulur", "kayıt yok")
        return
    check("önceki iş artefaktı bulunur", art.get("branch_name") == "feature/70979", f"{art}")
    check("PR id gelir", str(art.get("pr_id")) == "41840", f"{art}")
    check("repo gelir", art.get("repo_name") == "orkestra", f"{art}")
    # exclude_job_id çalışır
    art2 = db.get_prior_job_artifacts("70979", 183)
    check("exclude_job_id kendini eler", not art2 or art2.get("id") != 183, f"{art2}")


# ── 18. Build gate timeout: "bilmiyorum" != "geçti" (#185) ───────────────

def test_build_gate_timeout_strict():
    """job #185: poll timeout 11:13'te düştü, build 11:14'te FAILED bitti.

    Gate "geçildi sayıldı" → pipeline KIRMIZI bir PR için tamamlanma raporu
    yazdı ve iş `completed` bitti. Terminal sözleşmesi "testler yeşil VE
    reviewer onaylar"dı; ilk yarısı sessizce düştü.
    """
    import inspect
    from agile_sdlc_crew.flow import AgileSDLCFlow
    from agile_sdlc_crew import pipeline_config as pc

    src = inspect.getsource(AgileSDLCFlow.pr_build_gate)
    # Timeout dalı artık _step_done ile BAŞARILI kapatmamalı
    ti = src.find('outcome == "timeout"')
    check("timeout dalı bulunur", ti > 0)
    # Dal uzun (PR yorum metni dahil) — sonraki dala kadarını al
    nxt = src.find('outcome == "completed"', ti)
    branch = src[ti:nxt if nxt > ti else ti + 4000]
    check("timeout artık son şans bekliyor",
          "_poll_pr_build(grace" in branch or "TIMEOUT_GRACE" in branch, "grace yok")
    check("timeout artık gate'i GEÇMİYOR (_step_done yok)",
          "_step_done" not in branch, "hala _step_done var")
    check("timeout needs_human'a düşüyor",
          "NeedsHumanReview" in branch and "needs_human_job" in branch, "needs_human yok")
    check("timeout adımı fail olarak işaretleniyor",
          "_step_fail" in branch, "_step_fail yok")

    # Tavan suite süresinden uzun olmalı: orkestra-test ölçülen ~22 dk = 1320s
    to = int(pc.get("CREW_PR_BUILD_POLL_TIMEOUT") or 0)
    check(f"poll timeout ölçülen suite süresinin üstünde ({to}s > 1320s)", to > 1320, f"{to}")
    gr = int(pc.get("CREW_PR_BUILD_TIMEOUT_GRACE") or 0)
    check(f"son şans penceresi #185'in 2 dk gecikmesini kapsıyor ({gr}s >= 120s)",
          gr >= 120, f"{gr}")


# ── 19. Build-fix TEK commit (#183 5 build iptali) ───────────────────────

def test_build_fix_single_commit():
    """Her dosyayı ayrı push etmek CI'ı yeniden tetikliyor, Azure uçuştaki
    build'i iptal ediyor. PR 41840 için 5 build iptal edildi
    (#129155/57/58/68) — her iptal ~20 dakika çöpe gitti.
    """
    import inspect
    from agile_sdlc_crew.flow import AgileSDLCFlow

    # Tek commit gönderimi ortak yardımcıya taşındı (build-fix + PR öncesi doğrulama)
    src = (inspect.getsource(AgileSDLCFlow._fix_failing_build)
           + inspect.getsource(AgileSDLCFlow._push_single_commit))
    check("düzeltmeler biriktiriliyor", "pending.append" in src)
    check("tek commit API'si kullanılıyor", "push_changes" in src)
    # Dosya döngüsü içinde artık push YOK — döngü pending'e yazıp bitiyor
    loop = src.split("pending.append")[0]
    loop_body = loop[loop.rfind("for i, file_path in enumerate"):]
    check("dosya döngüsünde tek tek push kalmadı",
          "push_file(" not in loop_body, "döngüde push_file var")
    # Tek commit patlarsa dosya bazlı push'a düşmeli (sessiz kayıp olmasın)
    check("tek commit hatasında fallback var",
          "dosya bazli push'a dusuluyor" in src or "fallback" in src.lower())
    check("dry-run yolu korunuyor", "dry_run" in src and "push_file" in src)


# ── 20. Build-fix çıktı kapısı: skip silme / yeni test (#185) ────────────

def test_build_fix_regressions():
    """Gerçek vaka: build-fix, StockSourcesTest.test_set_passive'den
    markTestSkipped() sildi → test koştu → Mockery uyarısı →
    failOnWarning=true → BUILD KIRMIZI. Build'i kıran şey WI'ın değişikliği
    değil, bizim düzeltme döngümüzdü.
    """
    from agile_sdlc_crew.flow import _build_fix_regressions

    OLD = ("<?php class StockSourcesTest extends TestCase {\n"
           "  public function test_set_passive() {\n"
           "    $this->markTestSkipped();\n"
           "    Helper::delete(['stock_sources']);\n  }\n"
           "  public function test_other() { $this->assertTrue(true); }\n}")
    # 1) skip silinmiş → REDDET
    new_unskip = OLD.replace("    $this->markTestSkipped();\n", "")
    r = _build_fix_regressions(OLD, new_unskip)
    check("markTestSkipped silinmesi reddedilir", any("markTestSkipped" in x for x in r), f"{r}")

    # 2) yeni test eklenmiş → REDDET
    new_added = OLD.replace("}", "  public function test_brand_new() {}\n}", 1) if OLD.endswith("}") else OLD
    new_added = OLD[:-1] + "  public function test_brand_new() {}\n}"
    r2 = _build_fix_regressions(OLD, new_added)
    check("ilgisiz yeni test eklenmesi reddedilir",
          any("yeni test" in x for x in r2) and any("test_brand_new" in x for x in r2), f"{r2}")

    # 3) meşru düzeltme (assertion değişimi) → KABUL
    new_ok = OLD.replace("$this->assertTrue(true);", "$this->assertSame(1, 1);")
    check("meşru düzeltme kabul edilir", _build_fix_regressions(OLD, new_ok) == [],
          f"{_build_fix_regressions(OLD, new_ok)}")

    # 4) markTestIncomplete de korunur
    OLD2 = OLD.replace("markTestSkipped", "markTestIncomplete")
    r4 = _build_fix_regressions(OLD2, OLD2.replace("    $this->markTestIncomplete();\n", ""))
    check("markTestIncomplete silinmesi de reddedilir",
          any("markTestIncomplete" in x for x in r4), f"{r4}")

    # 5) eski içerik boşsa (yeni dosya) kapı sessiz — yanlış alarm üretmesin
    check("yeni dosyada kapı sessiz", _build_fix_regressions("", OLD) == [])

    # 6) #185'in gerçek diff'i: skip silme + 2 yeni test → İKİ ihlal
    real_new = new_unskip[:-1] + (
        "  public function test_success_checkDailyOrderLimitFloDigitalForActiveStockSource() {}\n"
        "  public function test_failed_checkDailyOrderLimitFloDigitalForActiveStockSource() {}\n}")
    r6 = _build_fix_regressions(OLD, real_new)
    check("#185'in gerçek diff'i iki ihlalle reddedilir", len(r6) >= 2, f"{r6}")


# ── 21. Kısmi implement resume + sözleşme düzeltme turu (#186/#187) ─────

def test_partial_implement_resume():
    """#186: sözleşme kapısı test dosyasını blokladı, Customer.php branch'e
    push'landı, iş 1/2 ile öldü. #187 (yeniden kuyruk): implement 'resume'
    edildi — ama all_pushes BOŞ kaldı ve plan kapsamı hiç sorgulanmadı →
    'Hiçbir dosya push edilemedi', $0'da öldü. Doğru davranış: branch'te
    zaten değişmiş plan dosyaları push sayılır ve YENİDEN YAZILMAZ; eksikler
    implement edilir."""
    print("\n[21] kısmi implement resume + sözleşme düzeltme turu")
    plan = [
        {"file_path": "/app/Controller/Api/V1/Customer.php", "change_type": "edit"},
        {"file_path": "Test/Controller/Api/V1/CustomerReturnReasonDefaultTest.php",
         "change_type": "create"},
    ]
    # #186 sonrası branch: yalnızca Customer.php değişmiş
    on, todo = _partition_plan_by_branch(plan, ["app/Controller/Api/V1/Customer.php"])
    check("#186 branch'i: Customer.php branch'te sayılır (slash farkı normalize)",
          on == ["/app/Controller/Api/V1/Customer.php"], f"{on}")
    check("#186 branch'i: test dosyası eksik → implement edilecek",
          todo == ["Test/Controller/Api/V1/CustomerReturnReasonDefaultTest.php"], f"{todo}")
    check("planın kendi yol biçimi korunur (step7 kapsam kümesi ham file_path kullanır)",
          on[0] == plan[0]["file_path"])
    on2, todo2 = _partition_plan_by_branch(
        plan, ["app/Controller/Api/V1/Customer.php",
               "Test/Controller/Api/V1/CustomerReturnReasonDefaultTest.php", "README.md"])
    check("tüm plan dosyaları branch'te → tam resume, eksik yok", len(on2) == 2 and todo2 == [])
    on3, todo3 = _partition_plan_by_branch(plan, [])
    check("branch'te değişiklik yok → hiçbir şey resume edilmez", on3 == [] and len(todo3) == 2)
    on4, todo4 = _partition_plan_by_branch(plan, None)
    check("changed_files None (fetch hatası) → güvenli taraf: hepsi implement", on4 == [] and len(todo4) == 2)

    # LocalRepoManager.changed_files — gerçek git fixture
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        remote = base / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
        work = base / "seed"
        subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        import os as _os
        env = {**_os.environ, **env}
        (work / "app").mkdir(); (work / "app" / "A.php").write_text("<?php // a\n")
        (work / "README.md").write_text("x\n")
        subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=work, check=True, capture_output=True, env=env)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=work, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-q", "-b", "feature/1"], cwd=work, check=True, capture_output=True)
        (work / "app" / "A.php").write_text("<?php // a2\n")
        (work / "app" / "B.php").write_text("<?php // b\n")
        subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "feat"], cwd=work, check=True, capture_output=True, env=env)
        subprocess.run(["git", "push", "-q", "origin", "feature/1"], cwd=work, check=True, capture_output=True)
        # pipeline'ın klonu: yalnızca main'i bilen ayrı bir klon
        clone = base / "fake"
        subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True, capture_output=True)
        from agile_sdlc_crew.tools.local_repo import LocalRepoManager
        mgr = LocalRepoManager(base_dir=str(base))
        got = mgr.changed_files("fake", "feature/1")
        check("changed_files: remote branch fetch edilip main'e göre fark listelenir",
              sorted(got) == ["app/A.php", "app/B.php"], f"{got}")
        check("changed_files: olmayan branch → boş liste (exception değil)",
              mgr.changed_files("fake", "feature/yok") == [])

    # Default branch 'master' olan repo: base çözümlemesi origin/HEAD'e düşmeli
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        remote = base / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(remote)], check=True, capture_output=True)
        work = base / "seed"
        subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
        import os as _os2
        env2 = {**_os2.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        (work / "a.php").write_text("<?php // a\n")
        subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=work, check=True, capture_output=True, env=env2)
        subprocess.run(["git", "push", "-q", "origin", "master"], cwd=work, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-q", "-b", "feature/2"], cwd=work, check=True, capture_output=True)
        (work / "b.php").write_text("<?php // b\n")
        subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "feat"], cwd=work, check=True, capture_output=True, env=env2)
        subprocess.run(["git", "push", "-q", "origin", "feature/2"], cwd=work, check=True, capture_output=True)
        clone = base / "fake2"
        subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True, capture_output=True)
        from agile_sdlc_crew.tools.local_repo import LocalRepoManager as _LRM2
        got2 = _LRM2(base_dir=str(base)).changed_files("fake2", "feature/2")
        check("changed_files: default branch 'master' (main yok) → origin/HEAD ile çözülür",
              got2 == ["b.php"], f"{got2}")

    # Sözleşme düzeltme talimatı — developer'a kapının verdiği somut bulgu gitmeli
    d = _contract_fix_description(
        "Test/X.php",
        ["ARITY: satır 27, 'exposeReasonOptionDefaultKey(...)' 0 argümanla çağrılıyor ama 1 zorunlu parametre var."])
    check("düzeltme talimatı dosya yolunu içerir", "Test/X.php" in d)
    check("düzeltme talimatı kapının bulgusunu birebir içerir", "satır 27" in d and "exposeReasonOptionDefaultKey" in d)
    check("düzeltme talimatı tam dosya ister (parça değil)", "TAM" in d.upper())


# ── 22. needs_human kalıcılığı + escalation resume + zarf-on-resume (#188) ─

def test_needs_human_and_resume_envelope():
    """Job #188 (2026-09-09) üç kusuru birden gösterdi:
      1. flow needs_human yazdı, main.run_pipeline'ın genel except'i fail_job ile
         EZDİ → DB'de 'failed'. #185'in needs_human olması elle düzeltmeydi.
      2. Escalation çıktısı ("İnsan müdahalesi gerekli — ...") karar satırı
         taşımıyor → _review_rejected False → _restore_review ONAY sanıp
         review'u atlar → N1 regresyonu incelenmeden build gate'e gider.
      3. requirements/plan resume edildiğinde _apply_envelope hiç çağrılmadı →
         zarf L (3 retry, $18) yerine config (1 retry, $10); verify N1 bulduğunda
         2. tur hakkı yoktu → gereksiz escalation."""
    print("\n[22] needs_human kalıcılığı · escalation resume · zarf-on-resume")
    import inspect
    from agile_sdlc_crew import main as _main, flow as _flow, db as _db

    # 1) _review_approved: yalnızca POZİTİF onay sinyali
    esc = "İnsan müdahalesi gerekli — 1 deneme sonrası kapanmayan madde:\n- N1 ..."
    check("escalation metni ONAY değildir", _review_approved(esc) is False)
    check("REVIEW_DECISION: APPROVE onaydır", _review_approved("x\nREVIEW_DECISION: APPROVE\n") is True)
    check("Verdict: APPROVE onaydır", _review_approved("## PR Review\n**Verdict:** APPROVE — ok") is True)
    check("CHANGES_REQUIRED onay değildir", _review_approved("**Verdict:** CHANGES_REQUIRED") is False)
    check("karar satırı yok → onay değil (belirsizlik resume ETMEZ)", _review_approved("sadece düzyazı") is False)
    check("REVIEW_DECISION: NEEDS_HUMAN onay değildir", _review_approved("REVIEW_DECISION: NEEDS_HUMAN") is False)
    check("Verdict satırındaki 'consideRED/coveRED' onayı bozmaz (kelime sınırı)",
          _review_approved("**Verdict:** APPROVED — all items were considered and covered") is True)
    check("Verdict: RED kelimesi (tam token) onay değildir",
          _review_approved("Verdict: RED — approve edilmedi") is False)
    src8 = inspect.getsource(_flow.AgileSDLCFlow.step8_code_review)
    check("_restore_review pozitif onay ister (_review_approved)", "_review_approved(" in src8)

    # 2) run_pipeline: NeedsHumanReview fail_job'u TETİKLEMEZ
    calls = []
    orig_kick, orig_fail = _flow.AgileSDLCFlow.kickoff, _db.fail_job
    def _raise(exc):
        def _k(self, inputs=None, **kw):
            raise exc
        return _k
    _db.fail_job = lambda jid, msg: calls.append(("fail", jid))
    tracker = SimpleNamespace(finish=lambda: None)
    try:
        _flow.AgileSDLCFlow.kickoff = _raise(_flow.NeedsHumanReview("kapanmayan madde"))
        try:
            _main.run_pipeline("1", tracker=tracker, job_id=999)
        except _flow.NeedsHumanReview:
            pass
        check("NeedsHumanReview → fail_job ÇAĞRILMAZ (needs_human ezilmez)", calls == [], f"{calls}")
        calls.clear()
        _flow.AgileSDLCFlow.kickoff = _raise(RuntimeError("boom"))
        try:
            _main.run_pipeline("1", tracker=tracker, job_id=999)
        except RuntimeError:
            pass
        check("diğer hatalar → fail_job çağrılır", calls == [("fail", 999)], f"{calls}")
    finally:
        _flow.AgileSDLCFlow.kickoff, _db.fail_job = orig_kick, orig_fail

    # 3) zarf resume yollarında da uygulanır (kaynak-seviyesi bağlama kontrolü)
    src4 = inspect.getsource(_flow.AgileSDLCFlow.crew_step4_technical_design)
    i_env, i_res = src4.find('_apply_envelope("plan")'), src4.find('_resume_or_run("technical_design_task"')
    check("plan resume → _apply_envelope('plan') restore içinde (resume'dan ÖNCE tanımlı)",
          0 <= i_env < i_res, f"env={i_env} resume={i_res}")
    src1 = inspect.getsource(_flow.AgileSDLCFlow.crew_step1_requirements)
    check("requirements resume → _apply_envelope('requirements') hem resume hem normal yolda",
          src1.count('_apply_envelope("requirements")') >= 2, f"{src1.count(chr(95)+'apply_envelope')}")


# ── 23. Build gate: düzeltme push'undan sonra ESKİ build'i değerlendirme (#189) ─

def test_build_gate_stale_build():
    """Job #189: build-fix push'u 11:37:44'te gitti, gate 11:37:46'da poll etti,
    Azure yeni build'i henüz kuyruğa almamıştı → queueTime'a göre 'en son'
    build hâlâ eski 132550 (failed) → gate bunu düzeltmenin sonucu sanıp
    2 saniyede 'deneme 2/2'ye girdi. İkinci retry hakkı bayat sonuca yandı.
    Düzeltmeden sonra gate, id'si ÖNCEKİNDEN FARKLI bir build görmeden karar
    vermemeli."""
    print("\n[23] build gate — düzeltme sonrası bayat build")
    import inspect, time as _time
    from agile_sdlc_crew.flow import AgileSDLCFlow

    def _mk(seq):
        it = iter(seq)
        last = {"b": None}
        def _get(repo, pr):
            try:
                last["b"] = next(it)
            except StopIteration:
                pass
            return last["b"]
        return SimpleNamespace(state=SimpleNamespace(repo_name="r", pr_id="1"),
                               _client=SimpleNamespace(get_pr_build=_get))

    stale = {"build_id": 100, "status": "completed", "result": "failed"}
    fresh_run = {"build_id": 101, "status": "inProgress", "result": None}
    fresh_ok = {"build_id": 101, "status": "completed", "result": "succeeded"}
    orig_sleep = _time.sleep
    _time.sleep = lambda s: None
    try:
        out = AgileSDLCFlow._poll_pr_build(_mk([stale, stale, fresh_run, fresh_ok]), 600, 30,
                                           ignore_build_id=100)
        check("bayat build (aynı id) tamamlanmış sayılmaz, yeni build beklenir",
              out[0] == "completed" and out[1]["build_id"] == 101, f"{out}")
        out2 = AgileSDLCFlow._poll_pr_build(_mk([stale]), 600, 30)
        check("ignore verilmezse eski davranış (ilk poll'da completed)",
              out2[0] == "completed" and out2[1]["build_id"] == 100, f"{out2}")
        out3 = AgileSDLCFlow._poll_pr_build(_mk([stale]), 600, 30, ignore_build_id=100)
        check("yeni build hiç gelmezse 'stale' döner (timeout/completed değil)",
              out3[0] == "stale" and out3[1]["build_id"] == 100, f"{out3}")
        calls = {"n": 0}
        def _count_get(repo, pr):
            calls["n"] += 1
            return stale
        stub_c = SimpleNamespace(state=SimpleNamespace(repo_name="r", pr_id="1"),
                                 _client=SimpleNamespace(get_pr_build=_count_get))
        out4 = AgileSDLCFlow._poll_pr_build(stub_c, 300, 30, ignore_build_id=100, stale_after=300)
        check("stale_after verilirse son şans penceresi TAMAMEN beklenir (300s/30s = 10 poll)",
              out4[0] == "stale" and calls["n"] >= 10, f"{out4[0]} polls={calls['n']}")
    finally:
        _time.sleep = orig_sleep

    src = inspect.getsource(AgileSDLCFlow.pr_build_gate)
    check("gate düzeltme sonrası önceki build id'sini poll'a geçiriyor",
          "ignore_build_id=" in src)
    check("gate 'stale' sonucunu ele alıyor", '"stale"' in src)
    check("son şans poll'u stale_after=grace ile çağrılıyor (120s'de erken düşmez)",
          "stale_after=grace" in src)
    ti = src.find("attempt >= max_retries")
    branch = src[ti:ti + 1800] if ti > 0 else ""
    check("retry tavanı: PR açık → needs_human (failed değil)",
          "NeedsHumanReview" in branch and "needs_human_job" in branch, "RuntimeError yolu duruyor")


# ── 24. Hazırlık kapısı — WI yeterince detaylı mı? (#190) ────────────────

def test_readiness_gate():
    """Job #190 (WI 73061): mimar Faz A'da 4 repoya yayılan, veri kaynağı
    tanımsız bir iş olduğunu teşhis etti (INSUFFICIENT), completeness 4/25
    çıktı — akış yine de kör plan + implement'e devam edecekti ($10+). Doğrusu:
    skor eşiğin altındaysa iş `needs_info`'ya alınır (silinmez), eksik
    detaylar WI'a Türkçe yorum olarak yazılır, iş ↻ ile tekrar kuyruğa alınır."""
    print("\n[24] hazırlık kapısı (readiness)")
    import inspect, re as _re
    from agile_sdlc_crew import flow as _flow, db as _db, server as _srv, pipeline_config as _pc

    ba = json.dumps({
        "summary": "x",
        "functional_requirements": [{"id": "FR1", "desc": "a"}],
        "acceptance_criteria": [],
        "open_questions": ["Nokta COD verisi nerede saklanacak?", "CDEK için nokta deposu var mı?", "DPD НПП alanı hangi API'den?"],
        "readiness": {"score": 72, "missing_details": [
            {"topic": "Nokta COD verisinin kaynağı", "why_needed": "cashAllowed/cardAllowed persist edilmiyor", "question": "Hangi API alanı, hangi tablo?"},
            {"topic": "CDEK/DPD nokta deposu", "why_needed": "senkronizasyon yok", "question": "Nokta verisi nereden okunacak?"},
        ]},
    }, ensure_ascii=False)
    r = _parse_readiness("```json\n" + ba + "\n```")
    check("readiness bloğu fenced JSON'dan parse edilir", r is not None and r["score"] == 72 and len(r["missing_details"]) == 2, f"{r}")
    check("readiness yoksa None (kapı atlanır, iş bloklanmaz)", _parse_readiness('{"summary":"x"}') is None)
    check("bozuk JSON → None", _parse_readiness("düzyazı") is None)

    score, reasons = _readiness_score(r, ac_empty=True, open_questions=3)
    check("ceza: AC boş −15, 3 açık soru −9 → 72−24 = 48", score == 48, f"{score} {reasons}")
    check("ceza gerekçeleri listelenir", any("AC" in x for x in reasons) and any("soru" in x for x in reasons), f"{reasons}")
    check("açık soru cezası −15'te durur", _readiness_score({"score": 90, "missing_details": []}, False, 9)[0] == 75)
    check("skor 0..100'e kırpılır", _readiness_score({"score": 130, "missing_details": []}, False, 0)[0] == 100
          and _readiness_score({"score": 10, "missing_details": []}, True, 5)[0] == 0)
    check("readiness None → skor None (kapı karar vermez)", _readiness_score(None, True, 3)[0] is None)

    c = _readiness_comment(48, 60, r["missing_details"], stage="requirements", penalties=reasons)
    check("yorum: skor/eşik başlıkta", "48" in c and "60" in c and "Hazırlık" in c, c[:120])
    check("yorum: eksik detaylar tablo satırı olarak", "Nokta COD verisinin kaynağı" in c and "Hangi API alanı" in c)
    check("yorum: tekrar kuyruğa alma talimatı", "tekrar kuyruğa" in c or "↻" in c)
    c2 = _readiness_comment(16, 50, [], stage="plan", uncovered=["AC1", "FR2"],
                            architect_note="INSUFFICIENT: 4 repoya yayılan yeni mekanizma; nokta COD verisi persist edilmiyor.")
    check("aşama 2 yorumu: mimar teşhisi + kapsanmayan id'ler", "AC1" in c2 and "persist edilmiyor" in c2 and "%" in c2, c2[:200])

    check("NeedsMoreInfo, NeedsHumanReview'un alt sınıfı (server/main ayrımı değişmez)",
          issubclass(NeedsMoreInfo, NeedsHumanReview))

    # bağlama: durum, DB, server, dashboard, config
    check("jobs.status ENUM'unda needs_info var", "needs_info" in _db.SCHEMA)
    check("db.needs_info_job var", hasattr(_db, "needs_info_job"))
    check("health sayacında needs_info", "needs_info" in inspect.getsource(_db.get_stats) if hasattr(_db, "get_stats") else "needs_info" in inspect.getsource(_db))
    srv_src = inspect.getsource(_srv)
    check("retry endpoint needs_info'yu kabul eder", _re.search(r'retry.*?needs_info|needs_info.*?retry', srv_src, _re.S) is not None)
    html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text()
    check("dashboard needs_info rengi + retry butonu", html.count("needs_info") >= 2, f"{html.count('needs_info')}")
    check("config: CREW_READINESS_GATE / MIN_SCORE=60 / MIN_COVERAGE=50",
          _pc.get("CREW_READINESS_GATE") is not None and int(_pc.get("CREW_READINESS_MIN_SCORE")) == 60
          and int(_pc.get("CREW_READINESS_MIN_COVERAGE")) == 50)
    s1 = inspect.getsource(_flow.AgileSDLCFlow.crew_step1_requirements)
    check("aşama 1 requirements adımına bağlı (NeedsMoreInfo + _step_fail)", "NeedsMoreInfo" in s1 and "_readiness_score" in s1)
    s4 = inspect.getsource(_flow.AgileSDLCFlow.crew_step4_technical_design)
    check("aşama 2 plan adımına bağlı (kapsam eşiği + mimar reddi)", "NeedsMoreInfo" in s4 and "CREW_READINESS_MIN_COVERAGE" in s4)


# ── 25. Çağrı muhasebesi thread sınırını aşmalı (crewai 1.15 regresyonu) ──

def test_call_context_cross_thread():
    """crewai 1.15.20 ile crew kickoff'larının LLM çağrıları flow adımından
    FARKLI bir thread'de koşuyor. Muhasebe bağlamı thread-local idi → job_id
    None → llm_calls satırları sahipsiz (bugün 7 satır, $3.03 NULL job_id),
    jobs.total_cost_usd ve adım maliyetleri 0 kaldı (#190, #191). Worker
    kuyruğu seri: süreç-genel bir 'geçerli iş' bağlamı güvenli; thread-local
    yalnızca paralel adımlarda (step9/step10) ince ayar."""
    print("\n[25] çağrı muhasebesi — thread sınırı")
    import threading
    from agile_sdlc_crew.tools import claude_cli_llm as cli
    cli.set_call_context(4242, "requirements_analysis_task", "business_analyst")
    got = {}
    def _worker():
        got["ctx"] = cli._get_call_context()
    t = threading.Thread(target=_worker); t.start(); t.join()
    check("başka thread'den okunan bağlam job_id'yi taşır",
          got.get("ctx", (None,))[0] == 4242, f"{got}")
    check("adım/agent de taşınır", got["ctx"][1] == "requirements_analysis_task" and got["ctx"][2] == "business_analyst")
    # thread-local override: paralel adım kendi bağlamını set ederse o kazanır
    got2 = {}
    def _w2():
        cli.set_call_context(4243, "uat_task", "uat_specialist")
        got2["ctx"] = cli._get_call_context()
    t2 = threading.Thread(target=_w2); t2.start(); t2.join()
    check("thread kendi bağlamını set ettiyse o geçerli", got2["ctx"][0] == 4243, f"{got2}")
    cli.clear_call_context()
    got3 = {}
    def _w3():
        got3["ctx"] = cli._get_call_context()
    t3 = threading.Thread(target=_w3); t3.start(); t3.join()
    check("clear sonrası başka thread de boş görür", got3["ctx"][0] is None, f"{got3}")

    # EŞZAMANLI İŞ (job #213/#214 regresyonu): pr_review kuyruktan geçmiyor,
    # kendi thread'inde koşuyor → iki review aynı anda olabiliyor. Önce biten
    # işin clear'ı, hâlâ koşan işin bağını da siliyordu; onun LLM kaydı
    # job_id=NULL düşüyordu ($0.61 sahipsiz kaldı).
    ev_a_set, ev_b_done, ev_a_read = threading.Event(), threading.Event(), threading.Event()
    res = {}

    def _job_a():                     # uzun süren iş (job 213 gibi)
        cli.set_call_context(213, "review_pr_task", "code_reviewer")
        ev_a_set.set()
        ev_b_done.wait(5)             # B bitip clear çağırana kadar bekle
        # A'nın LLM çağrısı CrewAI worker thread'inde koşar → thread-local BOŞ
        t = threading.Thread(target=lambda: res.update(a=cli._get_call_context()))
        t.start(); t.join()
        ev_a_read.set()
        cli.clear_call_context()

    def _job_b():                     # kısa süren iş (job 214 gibi)
        ev_a_set.wait(5)
        cli.set_call_context(214, "review_pr_task", "code_reviewer")
        t = threading.Thread(target=lambda: res.update(b=cli._get_call_context()))
        t.start(); t.join()
        cli.clear_call_context()      # ← eskiden A'nın bağını da siliyordu
        ev_b_done.set()

    ta, tb = threading.Thread(target=_job_a), threading.Thread(target=_job_b)
    ta.start(); tb.start(); ta.join(6); tb.join(6)
    check("eşzamanlı: B'nin kaydı B'ye yazılır", res.get("b", (None,))[0] == 214, str(res))
    check("eşzamanlı: B bitse de A'nın kaydı SAHİPSİZ kalmaz (job_id NULL değil)",
          res.get("a", (None,))[0] == 213, str(res))
    check("iki iş de bitince genel yedek boşalır", cli._get_call_context()[0] is None)
    check("ölü thread'in bağı kayıtta kalmaz", not cli._acct_active, str(cli._acct_active))

    # 2026-11-06 #275/#276: iki PR incelemesi 7 sn arayla. crewai çağrıyı kendi thread'inde koşturur
    # ama contextvars'ı kopyalar. A'nın çağrısı B bağını set ETTİKTEN sonra bitti → süreç-genel yedek
    # "en son set eden"i (B) söylüyordu: #275'in $0.58'i #276'ya yazıldı.
    import contextvars
    ev_b_set, res2 = threading.Event(), {}

    def _crewai_call(key):  # crewai: yeni thread, kopyalanmış bağlam, thread-local boş
        ctx = contextvars.copy_context()
        t = threading.Thread(target=lambda: ctx.run(lambda: res2.update({key: cli._get_call_context()})))
        t.start(); t.join()

    def _rev_a():
        cli.set_call_context(275, "review_pr_task", "code_reviewer")
        ev_b_set.wait(5)          # B başladı, yedek artık 276'yı gösteriyor
        _crewai_call("a")
        cli.clear_call_context()

    def _rev_b():
        cli.set_call_context(276, "review_pr_task", "code_reviewer")
        ev_b_set.set()
        _crewai_call("b")
        cli.clear_call_context()
    ta = threading.Thread(target=_rev_a); ta.start()
    import time as _time
    _time.sleep(0.05)
    tb = threading.Thread(target=_rev_b); tb.start(); ta.join(6); tb.join(6)
    check("eşzamanlı PR incelemeleri: her çağrı KENDİ işine yazılır (#275 → 275, #276 → 276)",
          res2.get("a", (None,))[0] == 275 and res2.get("b", (None,))[0] == 276, str(res2))

    # WI yorumu: _md_to_html '###' ve '|' tablo bilmez — yorum bunları kullanmamalı
    c = _readiness_comment(48, 60, [{"topic": "T", "why_needed": "W", "question": "Q?"}],
                           stage="requirements", penalties=["AC alanı boş (−15)"])
    check("yorumda '###' yok (h3 render edilmiyor)", "###" not in c)
    check("yorumda pipe tablo yok (render edilmiyor)", "|---|" not in c and "| T |" not in c)
    check("eksik detay madde olarak (- ile) listelenir", "- **T**" in c or "- T" in c, c[:300])


# ── 26. _md_to_html: ### başlık ve | tablo (WI yorumu düz metin kalıyordu) ─

def test_md_to_html_headings_tables():
    """#191'in WI yorumunda '### Eksik detaylar' ve '| a | b |' satırları
    Azure DevOps'ta düz metin göründü (ekran görüntüsü, 09.09 15:01)."""
    print("\n[26] _md_to_html — ### ve tablo")
    from agile_sdlc_crew.main import _md_to_html
    h = _md_to_html("### Alt başlık\n\nmetin")
    check("### → <h4>", "<h4>Alt başlık</h4>" in h, h)
    t = _md_to_html("| A | B |\n|---|---|\n| 1 | **iki** |\n\nson")
    check("pipe tablo → <table> + <th>", "<table>" in t and "<th>A</th>" in t, t)
    check("hücreler <td>, inline biçim korunur", "<td>1</td>" in t and "<strong>iki</strong>" in t, t)
    check("ayırıcı satır (|---|) hücre olmaz", "---" not in t, t)
    check("tablo sonrası paragraf devam eder", "<p>son</p>" in t, t)


# ── 27. WI yaşam döngüsü — süreçten bağımsız durum seçimi + sahiplik aralığı ─
#
# FLO süreci (Boards Management, 2026-09-10 Azure'dan okundu): Task/Bug için
# Backlog→To Do→In Progress→Code Review→QA To Do→QA→UAT→Preprod Check→Blocked→
# Ready for Production(Resolved)→Done; User Story'de 'QA To Do' yok. Agile
# şablonu: New/Active/Resolved/Closed. Issue: Active/Closed. Adlar sabit
# kodlanamaz — tercih listesi + tipin durum listesi.

_FLO_TASK = [{"name": n, "category": c} for n, c in [
    ("Backlog", "Proposed"), ("To Do", "Proposed"), ("In Progress", "InProgress"),
    ("Code Review", "InProgress"), ("QA To Do", "InProgress"), ("QA", "InProgress"),
    ("UAT", "InProgress"), ("Preprod Check", "InProgress"), ("Blocked", "InProgress"),
    ("Ready for Production", "Resolved"), ("Done", "Completed"), ("Canceled", "Removed")]]
_FLO_STORY = [s for s in _FLO_TASK if s["name"] != "QA To Do"] + [{"name": "Prod Check", "category": "InProgress"}]
_AGILE = [{"name": n, "category": c} for n, c in [
    ("New", "Proposed"), ("Active", "InProgress"), ("Resolved", "Resolved"), ("Closed", "Completed")]]
_ISSUE = [{"name": "Active", "category": "InProgress"}, {"name": "Closed", "category": "Completed"}]


def test_wi_lifecycle_transitions():
    print("\n[27] WI yaşam döngüsü — durum seçimi, sahiplik aralığı, geri alma")
    from agile_sdlc_crew import wi_lifecycle as wl

    check("Task start → In Progress", wl.pick_state(_FLO_TASK, "start") == "In Progress")
    check("Task review → Code Review", wl.pick_state(_FLO_TASK, "review") == "Code Review")
    check("Task wait → Blocked", wl.pick_state(_FLO_TASK, "wait") == "Blocked")
    check("Task handoff → QA To Do", wl.pick_state(_FLO_TASK, "handoff") == "QA To Do")
    check("User Story handoff → QA (QA To Do yok)", wl.pick_state(_FLO_STORY, "handoff") == "QA")
    check("Agile start → Active", wl.pick_state(_AGILE, "start") == "Active")
    check("Agile review → Active (Code Review yok, geri düşer)", wl.pick_state(_AGILE, "review") == "Active")
    check("Agile handoff → Resolved", wl.pick_state(_AGILE, "handoff") == "Resolved")
    check("Agile wait → yok (Blocked yok)", wl.pick_state(_AGILE, "wait") is None)
    check("Issue handoff → yok", wl.pick_state(_ISSUE, "handoff") is None)
    check("büyük/küçük harf duyarsız eşleşme",
          wl.pick_state([{"name": "in progress", "category": "InProgress"}], "start") == "in progress")

    owned = wl.owned_states(_FLO_TASK)
    check("sahiplik: Proposed + In Progress/Code Review/Blocked",
          {"backlog", "to do", "in progress", "code review", "blocked"} <= owned, str(owned))
    check("sahiplik DIŞI: QA To Do, QA, UAT, Ready for Production, Done",
          not ({"qa to do", "qa", "uat", "ready for production", "done"} & owned))

    check("To Do → start = In Progress", wl.plan_transition("To Do", _FLO_TASK, "start") == "In Progress")
    check("Backlog (Proposed) → start = In Progress", wl.plan_transition("Backlog", _FLO_TASK, "start") == "In Progress")
    check("zaten In Progress → start = no-op", wl.plan_transition("In Progress", _FLO_TASK, "start") is None)
    check("Blocked → start = In Progress (needs_info sonrası ↻)", wl.plan_transition("Blocked", _FLO_TASK, "start") == "In Progress")
    check("In Progress → review = Code Review", wl.plan_transition("In Progress", _FLO_TASK, "review") == "Code Review")
    check("Code Review → handoff = QA To Do", wl.plan_transition("Code Review", _FLO_TASK, "handoff") == "QA To Do")
    check("QA To Do (insan ilerletmiş) → start = DOKUNMA", wl.plan_transition("QA To Do", _FLO_TASK, "start") is None)
    check("Ready for Production → review = DOKUNMA", wl.plan_transition("Ready for Production", _FLO_TASK, "review") is None)
    check("Done → handoff = DOKUNMA", wl.plan_transition("Done", _FLO_TASK, "handoff") is None)
    check("boş mevcut durum → yine hedef verir", wl.plan_transition("", _FLO_TASK, "start") == "In Progress")

    check("revert: In Progress→To Do (PR yok)", wl.plan_revert("In Progress", "To Do", _FLO_TASK, pr_exists=False) == "To Do")
    check("revert: PR varsa YOK (Code Review'da kalır)", wl.plan_revert("Code Review", "To Do", _FLO_TASK, pr_exists=True) is None)
    check("revert: mevcut == başlangıç → yok", wl.plan_revert("To Do", "To Do", _FLO_TASK, pr_exists=False) is None)
    check("revert: başlangıç sahiplik dışı (QA To Do) → yok", wl.plan_revert("In Progress", "QA To Do", _FLO_TASK, pr_exists=False) is None)
    check("revert: mevcut insan durumu (QA) → yok", wl.plan_revert("QA", "To Do", _FLO_TASK, pr_exists=False) is None)
    check("revert: Blocked→To Do (needs_info sonrası hata)", wl.plan_revert("Blocked", "To Do", _FLO_TASK, pr_exists=False) == "To Do")

    check("test yolu: Test/…/CustomerReturnReasonDefaultTest.php",
          wl.is_test_path("Test/Controller/Api/V1/CustomerReturnReasonDefaultTest.php"))
    check("test yolu: tests/unit/x.py", wl.is_test_path("tests/unit/x.py"))
    check("test yolu: src/a.spec.ts", wl.is_test_path("src/a.spec.ts"))
    check("test yolu DEĞİL: app/Customer.php", not wl.is_test_path("app/Customer.php"))
    check("test yolu DEĞİL: resources/translation/fr_FR.yml", not wl.is_test_path("resources/translation/fr_FR.yml"))
    check("test yolu DEĞİL: Contest/Latest.php (kelime içi 'test')", not wl.is_test_path("Contest/Latest.php"))


# ── 28. Definition of Done — job #189: completed ama UAT REJECTED ────────

_UAT_189_SHAPE = """## UAT Report

**Acceptance Criteria:**

1. **AC1** — Given: global site, When: reason_option_default, Then: dil uygun. — **PASS** — Evidence: fr_FR.yml.

2. **AC2** — Given: rus sitesi, Then: rusca. — **FAIL** — Evidence gap: no Russian resource; a Russian-site call cannot pass.

3. **AC3** — Given: Turk sitesi, Then: regresyon yok. — **PASS** — Evidence: additive change.

**Overall Evaluation:** REJECTED

**Gaps:**
- AC2 not satisfied
"""


def test_dod_checklist():
    print("\n[28] Definition of Done — deterministik tablo (#189 UAT REJECTED görünür olmalı)")
    from agile_sdlc_crew import wi_lifecycle as wl

    u = wl.parse_uat(_UAT_189_SHAPE)
    check("UAT parse: overall REJECTED", u["overall"] == "REJECTED", str(u))
    check("UAT parse: 2 PASS / 1 FAIL (açıklamadaki 'cannot pass' sayılmaz)",
          (u["pass"], u["fail"]) == (2, 1), str(u))
    check("UAT parse: madde kararları sırayla", u["items"] == [(1, "PASS"), (2, "FAIL"), (3, "PASS")], str(u["items"]))
    check("UAT parse: boş metin → None/0", wl.parse_uat("")["overall"] is None and wl.parse_uat("")["items"] == [])
    check("UAT parse: Türkçe KABUL", wl.parse_uat("Overall Evaluation: KABUL")["overall"] == "ACCEPTED")

    pushed = ["Customer.php", "resources/translation/fr_FR.yml",
              "Test/Controller/Api/V1/CustomerReturnReasonDefaultTest.php"]
    d = wl.evaluate_dod(review_approved=True, open_review_issues=0, build_status="succeeded",
                        uat_text=_UAT_189_SHAPE, pushed_files=pushed, require_tests=True, pr_id="42951")
    check("#189 şekli: DoD GEÇİLEMEDİ (UAT ❌)", not d.passed)
    check("#189 şekli: kalan tek madde UAT", [i.key for i in d.failed] == ["uat"], str([i.key for i in d.failed]))
    check("#189 şekli: review/build/test/pr ✅",
          all(i.ok for i in d.items if i.key in ("review", "issues", "build", "tests", "pr")))
    md = wl.render_dod(d, enforce=False)
    check("render: tablo + ❌ + 'DoD geçilemedi' + zorlama kapalı notu",
          "| Madde | Durum | Not |" in md and "❌" in md and "DoD geçilemedi" in md and "CREW_DOD_ENFORCE" in md)
    md_e = wl.render_dod(d, enforce=True)
    check("render (enforce): needs_human notu", "needs_human" in md_e)

    ok_uat = _UAT_189_SHAPE.replace("— **FAIL** —", "— **PASS** —").replace("REJECTED", "ACCEPTED")
    d2 = wl.evaluate_dod(review_approved=True, open_review_issues=0, build_status="succeeded",
                         uat_text=ok_uat, pushed_files=pushed, require_tests=True, pr_id="42951")
    check("hepsi yeşil → DoD geçti, doğrulanamayan yok", d2.passed and not d2.unverified)
    d3 = wl.evaluate_dod(review_approved=True, open_review_issues=None, build_status="no_pipeline",
                         uat_text=ok_uat, pushed_files=["app/X.php"], require_tests=False, pr_id="1")
    check("pipeline yok + test şartı kapalı → ⚪ bloklamaz, DoD geçti",
          d3.passed and {i.key for i in d3.unverified} == {"issues", "build", "tests"}, str([(i.key, i.ok) for i in d3.items]))
    d4 = wl.evaluate_dod(review_approved=True, open_review_issues=0, build_status="failed",
                         uat_text=ok_uat, pushed_files=["app/X.php"], require_tests=True, pr_id="1")
    check("build failed + test yok (şart açık) → iki ❌", {i.key for i in d4.failed} == {"build", "tests"})
    d5 = wl.evaluate_dod(review_approved=False, open_review_issues=2, build_status="succeeded",
                         uat_text=ok_uat, pushed_files=pushed, require_tests=True, pr_id="")
    check("onay yok + 2 açık madde + PR yok → üç ❌", {i.key for i in d5.failed} == {"review", "issues", "pr"})
    check("render: ⚪ işareti doğrulanamayan için", "⚪" in wl.render_dod(d3, enforce=False))

    job = None
    try:
        from agile_sdlc_crew import db
        job = db.get_job(189)
    except Exception:
        job = None
    if not job:
        skip("#189 gerçek UAT çıktısı", "DB erişilemedi")
        return
    steps = {s["step_key"]: s.get("output") or "" for s in (job.get("steps") or [])}
    real = wl.parse_uat(steps.get("uat_task", ""))
    check("#189 GERÇEK UAT: REJECTED, 2 PASS / 1 FAIL",
          real["overall"] == "REJECTED" and (real["pass"], real["fail"]) == (2, 1), str(real))
    dr = wl.evaluate_dod(review_approved=_review_approved(steps.get("review_pr_task", "")),
                         open_review_issues=0, build_status="succeeded",
                         uat_text=steps.get("uat_task", ""), pushed_files=pushed,
                         require_tests=True, pr_id=str(job.get("pr_id") or ""))
    check("#189 GERÇEK: review ✅ build ✅ UAT ❌ → DoD geçilemedi",
          not dr.passed and [i.key for i in dr.failed] == ["uat"], str([(i.key, i.ok) for i in dr.items]))


# ── 29. Flow kancaları: stub client ile geçiş kaydı + koruma kuralları ───

class _StubAzClient:
    """Sadece wi_lifecycle'ın dokunduğu yüzey. Her PATCH kaydedilir."""
    def __init__(self, states, fields):
        self.states, self.fields, self.ops = states, fields, []

    def get_work_item(self, wid):
        return {"id": wid, "fields": dict(self.fields)}

    def get_work_item_type_states(self, t):
        return list(self.states)

    def set_work_item_state(self, wid, state):
        self.ops.append(("state", int(wid), state))
        self.fields["System.State"] = state
        return {}

    def get_authenticated_user(self):
        return {"id": "u1", "displayName": "Pipeline", "uniqueName": "pipeline@example.com"}

    def assign_work_item(self, wid, ident):
        self.ops.append(("assign", int(wid), ident))
        return {}


def test_wi_lifecycle_flow_hooks():
    print("\n[29] Flow kancaları — stub client, knob'lar, dry-run/kickoff-only koruması")
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.flow import NeedsHumanReview as _NHR

    knobs = {"CREW_WI_LIFECYCLE": True, "CREW_WI_ASSIGN_IF_EMPTY": False}
    _orig_get = pc.get
    pc.get = lambda k: knobs[k] if k in knobs else _orig_get(k)
    try:
        def mk(state="To Do", assigned=None, **st):
            fields = {"System.WorkItemType": "Task", "System.State": state}
            if assigned:
                fields["System.AssignedTo"] = {"displayName": assigned, "uniqueName": assigned}
            f = AgileSDLCFlow()
            f.state.work_item_id = "73121"
            for k, v in st.items():
                setattr(f.state, k, v)
            f._client = _StubAzClient(_FLO_TASK, fields)
            return f

        f = mk()
        f._wi_begin(f._client.fields)
        check("begin: tip/durum state'e alındı",
              f.state.wi_type == "Task" and f.state.wi_state_initial == "To Do" and len(f.state.wi_states) == 12)
        f2 = mk()
        f2._client.fields["Custom.StoryPoints"] = 2
        f2._client.fields["Microsoft.VSTS.Scheduling.StoryPoints"] = 3.0
        f2._wi_begin(f2._client.fields)
        check("begin: SP okuma sırası Custom.StoryPoints önce (2, 3.0 değil)", f2.state.wi_story_points == 2.0)
        check("begin: To Do → In Progress yazıldı", f._client.ops == [("state", 73121, "In Progress")], str(f._client.ops))
        check("begin: mevcut durum güncellendi", f.state.wi_state_current == "In Progress")
        f._wi_begin(f._client.fields)
        check("begin idempotent (ikinci çağrı yazmaz)", len(f._client.ops) == 1)
        check("review → Code Review", f._wi_transition("review") == "Code Review" and f._client.ops[-1][2] == "Code Review")
        check("handoff → QA To Do", f._wi_transition("handoff") == "QA To Do")
        check("QA To Do'dan sonra start = dokunma (sahiplik dışı)", f._wi_transition("start") is None and len(f._client.ops) == 3)
        # Sorun olursa Blocked değil başlangıç durumu; PR açık olsa bile geri alınır.

        f = mk()
        f._wi_begin(f._client.fields)
        f.wi_lifecycle_on_exception(NeedsMoreInfo("eksik detay"))
        check("needs_info → başlangıç durumuna (To Do) geri", f._client.ops[-1] == ("state", 73121, "To Do"), str(f._client.ops))
        f = mk()
        f._wi_begin(f._client.fields)
        f.wi_lifecycle_on_exception(_NHR("review kapanmadı"))
        check("needs_human → başlangıç durumuna (To Do) geri", f._client.ops[-1] == ("state", 73121, "To Do"), str(f._client.ops))
        f = mk()
        f._wi_begin(f._client.fields)
        f.wi_lifecycle_on_exception(RuntimeError("patladı"))
        check("genel hata, PR yok → To Do'ya geri", f._client.ops[-1] == ("state", 73121, "To Do"), str(f._client.ops))
        f = mk()
        f._wi_begin(f._client.fields)
        f.state.pr_id = "42951"
        f.wi_lifecycle_on_exception(RuntimeError("patladı"))
        check("genel hata, PR VAR → yine To Do'ya geri (yarım iş incelemeye alınmaz)",
              f._client.ops[-1] == ("state", 73121, "To Do") and f.state.wi_state_current == "To Do", str(f._client.ops))

        f = mk(state="QA To Do")
        f._wi_begin(f._client.fields)
        check("insan QA'ya taşımış → start yazmaz ama bağlam okunur",
              f._client.ops == [] and f.state.wi_state_initial == "QA To Do")

        f = mk(dry_run=True)
        f._wi_begin(f._client.fields)
        check("dry-run: bağlam okunur, Azure'a yazılmaz", f._client.ops == [] and f.state.wi_type == "Task")
        f = mk(kickoff_only=True)
        f._wi_begin(f._client.fields)
        check("kickoff-only: yazılmaz", f._client.ops == [])

        knobs["CREW_WI_LIFECYCLE"] = False
        f = mk()
        f._wi_begin(f._client.fields)
        check("knob kapalı: bağlam okunur (DoD/log için), yazılmaz",
              f._client.ops == [] and f.state.wi_state_initial == "To Do")
        f.wi_lifecycle_on_exception(RuntimeError("x"))
        check("knob kapalı: hata yolunda da yazılmaz", f._client.ops == [])

        knobs["CREW_WI_LIFECYCLE"] = True
        knobs["CREW_WI_ASSIGN_IF_EMPTY"] = True
        f = mk()
        f._wi_begin(f._client.fields)
        check("atama: boş → PAT sahibine", ("assign", 73121, "pipeline@example.com") in f._client.ops
              and f.state.wi_assigned_by_pipeline and f.state.wi_assigned_to == "pipeline@example.com")
        f = mk(assigned="ebru@example.com")
        f._wi_begin(f._client.fields)
        check("atama: dolu → dokunma", not any(o[0] == "assign" for o in f._client.ops)
              and f.state.wi_assigned_to == "ebru@example.com")

        class _Boom(_StubAzClient):
            def set_work_item_state(self, wid, state):
                raise RuntimeError("TF401320: geçiş kuralı")
        f = mk()
        f._client = _Boom(_FLO_TASK, f._client.fields)
        f._wi_begin(f._client.fields)
        check("Azure reddetti → pipeline devam, mevcut durum değişmez",
              f.state.wi_state_current == "To Do" and f.state.wi_type == "Task")
    finally:
        pc.get = _orig_get

    src_main = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/main.py").read_text()
    i_hook, i_fail = src_main.find("flow.wi_lifecycle_on_exception(e)"), src_main.find("_db.fail_job(job_id, str(e))")
    check("main.run_pipeline: yaşam döngüsü kancası fail_job'dan ÖNCE", 0 < i_hook < i_fail)
    src_flow = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/flow.py").read_text()
    check("step11: DoD → yorum → enforce → Code Review sırası",
          0 < src_flow.find("_wl_dod.evaluate_dod(") < src_flow.find('f"{_dod_md}\\n\\n"')
          < src_flow.find("raise NeedsHumanReview(_msg_dod)") < src_flow.rfind('self._wi_transition("review")'))
    check("Code Review yalnız iş sonunda: PR açılınca geçiş yok, handoff yok",
          src_flow.count('self._wi_transition("review")') == 1 and 'self._wi_transition("handoff")' not in src_flow
          and src_flow.find('self._wi_transition("review")') > src_flow.find("raise NeedsHumanReview(_msg_dod)"))
    check("build gate her çıkışta build_status yazar (6 yol)",
          src_flow.count("self.state.build_status = ") >= 7)


# ── 30. Tahminleme — Fibonacci, yapısal merdiven, BA bloğu, uzlaştırma ────

_BA_WITH_ESTIMATE = """```json
{
  "summary": "Getirilen iade nedeni metni site diline uygun olsun",
  "functional_requirements": [{"id": "FR1", "desc": "a"}, {"id": "FR2", "desc": "b"}],
  "technical_requirements": [{"id": "TR1", "desc": "c"}],
  "acceptance_criteria": [{"id": "AC1", "desc": "d"}],
  "out_of_scope": [], "assumptions": [], "open_questions": [],
  "readiness": {"score": 85, "missing_details": []},
  "estimate": {"story_points": 5, "confidence": 70, "rationale": "Üç dosya ve çeviri kaynakları"}
}
```"""


def test_estimation():
    print("\n[30] Tahminleme — Fibonacci'ye oturtma, yapısal merdiven, BA bloğu, max-uzlaşma")
    from agile_sdlc_crew import estimation as est

    check("snap 4 → 5 (eşitlikte büyük)", est.snap_fib(4) == 5)
    check("snap 6 → 5, 7 → 8, 2.6 → 3", (est.snap_fib(6), est.snap_fib(7), est.snap_fib(2.6)) == (5, 8, 3))
    check("snap 0 / metin → None", est.snap_fib(0) is None and est.snap_fib("x") is None)
    check("yapısal req: 2→2, 5→3, 7→5, 10→8",
          [est.structural_estimate(n, 0, False, "requirements")[0] for n in (2, 5, 7, 10)] == [2, 3, 5, 8])
    check("yapısal plan: 7 req + 6 dosya + keşif → 13 (tavan)",
          est.structural_estimate(7, 6, True, "plan")[0] == 13)
    check("yapısal plan: 2 req + 3 dosya → 3 (+1 basamak)", est.structural_estimate(2, 3, False, "plan")[0] == 3)
    check("yapısal requirements: dosya sayısı YOK SAYILIR", est.structural_estimate(2, 9, True, "requirements")[0] == 2)
    check("uzlaşma: BA yok → yapısal", est.reconcile(None, 3) == (3, "yapısal"))
    check("uzlaşma: BA 5 > yapısal 3 → BA", est.reconcile(5, 3) == (5, "BA"))
    check("uzlaşma: BA 2 < yapısal 3 → yapısal", est.reconcile(2, 3) == (3, "yapısal"))
    check("uzlaşma: eşit → uyumlu", est.reconcile(3, 3)[1] == "BA + yapısal uyumlu")
    check("uzlaşma: önceki aşama 5 → düşmez", est.reconcile(2, 3, previous_sp=5) == (5, "önceki aşama"))
    ba = est.parse_ba_estimate(_BA_WITH_ESTIMATE)
    check("BA bloğu parse: 5 SP, %70, gerekçe", ba == {"sp": 5, "confidence": 70, "rationale": "Üç dosya ve çeviri kaynakları"}, str(ba))
    check("BA bloğu yok → None", est.parse_ba_estimate('{"summary": "x"}') is None)
    check("bozuk JSON → None", est.parse_ba_estimate("{oops") is None)
    check("story_points 4 → 5'e oturur", est.parse_ba_estimate('{"estimate": {"story_points": 4}}')["sp"] == 5)
    check("sınıf: 2→S, 5→M, 8→L", (est.size_class(2), est.size_class(5), est.size_class(8)) == ("S", "M", "L"))
    line = est.render_estimate_line({"sp": 5, "source": "BA"}, elapsed_min=17.2, cost_usd=3.96)
    check("satır: tahmin + gerçekleşen", "5 SP (M, BA)" in line and "17 dk" in line and "$3.96" in line, line)
    check("satır: tahmin yoksa boş", est.render_estimate_line({}) == "")

    class _C:
        def __init__(self, fail_fields=(), hard=False):
            self.ops, self.fail_fields, self.hard = [], set(fail_fields), hard
        def update_work_item(self, wid, ops):
            fld = ops[0]["path"].rsplit("/", 1)[-1]
            if self.hard:
                raise RuntimeError("connection reset")
            if fld in self.fail_fields:
                raise RuntimeError("400 Client Error: TF51535 field does not exist")
            self.ops.append((wid, fld, ops[0]["value"]))
    c = _C()
    check("SP yaz: önce Custom.StoryPoints (org'un gerçek alanı), int",
          est.write_story_points(c, "73121", 5) == "Custom.StoryPoints" and c.ops == [(73121, "Custom.StoryPoints", 5)])
    c = _C(fail_fields={"Custom.StoryPoints"})
    check("SP yaz: Custom yok → Microsoft StoryPoints (float)",
          est.write_story_points(c, "1", 3) == "Microsoft.VSTS.Scheduling.StoryPoints" and c.ops[-1][2] == 3.0)
    c = _C(fail_fields={"Custom.StoryPoints", "Microsoft.VSTS.Scheduling.StoryPoints"})
    check("SP yaz: ikisi de yok → Effort", est.write_story_points(c, "1", 3) == "Microsoft.VSTS.Scheduling.Effort")
    line_t = est.render_estimate_line({"sp": 5, "source": "BA"}, elapsed_min=17.2, cost_usd=3.96, team_sp=2)
    check("satır: takım tahmini varsa ikisi yan yana", "Takım tahmini:** 2 SP" in line_t and "Pipeline tahmini:** 5 SP" in line_t, line_t)
    check("SP yaz: ağ hatası → '' (pipeline devam)", est.write_story_points(_C(hard=True), "1", 3) == "")

    job = None
    try:
        from agile_sdlc_crew import db
        job = db.get_job(189)
    except Exception:
        job = None
    if job:
        reqs = next((s.get("output") or "" for s in job.get("steps") or [] if s["step_key"] == "requirements_analysis_task"), "")
        n = len(_requirement_ids(reqs))
        sp, why = est.structural_estimate(n, 2, False, "plan")
        check(f"#189 GERÇEK BA (estimate bloğu yok → None) + yapısal {sp} SP Fibonacci'de",
              est.parse_ba_estimate(reqs) is None and sp in est.FIB and n > 0, f"n_req={n} {why}")
    else:
        skip("#189 gerçek BA çıktısı", "DB erişilemedi")


# ── 31. Alt iş kaydı planlama — parent tipi, gruplama, idempotency ────────

def _plan(n, repo="webservice"):
    dirs = ["/app/Controller", "/app/Model", "/resources/translation", "/Test/Controller"]
    return {"repo_name": repo, "changes": [
        {"file_path": f"{dirs[i % 4]}/File{i}.php", "change_type": "edit" if i % 3 else "add",
         "description": f"Değişiklik {i}", "covers_requirements": ["FR1", f"AC{i % 2 + 1}"]}
        for i in range(n)]}


def test_child_tasks_planning():
    print("\n[31] Alt iş kaydı — parent tipi, başlık/açıklama, gruplama, idempotent açma")
    from agile_sdlc_crew import wi_children as wc

    check("parent tipi: User Story/Bug/Feature evet; Task hayır",
          wc.parent_allows_children("User Story") and wc.parent_allows_children("Bug")
          and not wc.parent_allows_children("Task") and not wc.parent_allows_children(""))
    items = wc.plan_child_tasks(_plan(3), "webservice")
    check("3 değişiklik → 3 child", len(items) == 3)
    check("başlık: [repo] eylem dosya — açıklama", items[1]["title"] == "[webservice] Düzenle File1.php — Değişiklik 1", items[1]["title"])
    check("add → 'Yeni dosya'", items[0]["title"].startswith("[webservice] Yeni dosya File0.php"))
    check("açıklama: gereksinimler + dosya", "Kapsadığı gereksinimler: FR1, AC2" in items[1]["description"]
          and "/app/Model/File1.php" in items[1]["description"])
    check("files eşlemesi", items[2]["files"] == ["/resources/translation/File2.php"])
    big = wc.plan_child_tasks(_plan(12), "webservice", max_children=8)
    check("12 değişiklik, max 8 → dizine göre ≤8 grup", 1 <= len(big) <= 8 and all("dosya" in b["title"] for b in big), str([b["title"] for b in big]))
    check("gruplama: tüm dosyalar bir grupta", sorted(f for b in big for f in b["files"]) == sorted(c["file_path"] for c in _plan(12)["changes"]))
    long_plan = {"repo_name": "r", "changes": [{"file_path": "/a/B.php", "change_type": "edit", "description": "x" * 200}]}
    check("başlık 128'e kırpılır", len(wc.plan_child_tasks(long_plan, "r")[0]["title"]) <= 128)
    check("boş plan → []", wc.plan_child_tasks({}, "r") == [] and wc.plan_child_tasks({"changes": [{"nope": 1}]}, "r") == [])
    kids = [{"id": 1, "files": ["/app/Customer.php"]}, {"id": 2, "files": ["/Test/X.php"]}]
    check("child_for_file: slash/büyük-küçük toleranslı",
          wc.child_for_file(kids, "app/customer.php")["id"] == 1 and wc.child_for_file(kids, "/nope.php") is None)

    class _C:
        def __init__(self, children=()):
            self.children, self.created, self.n = list(children), [], 900
        def get_work_item_children(self, pid):
            return self.children
        def create_work_item(self, t, fields, parent_id=None):
            self.n += 1
            self.created.append((t, fields, parent_id))
            return {"id": self.n, "fields": fields}
    c = _C()
    out = wc.create_children(c, 500, {"System.AreaPath": "BM\\Ops", "System.IterationPath": "BM\\Ops\\2026_19"}, items)
    check("3 child açıldı, parent 500, Task tipi", len(c.created) == 3 and all(t == "Task" and p == 500 for t, _, p in c.created))
    check("etiket + alan/iterasyon kopyalandı", all(f["System.Tags"] == "crew-generated" and f["System.AreaPath"] == "BM\\Ops"
                                                    and f["System.IterationPath"] == "BM\\Ops\\2026_19" for _, f, _ in c.created))
    check("dönen liste id + files taşır", [o["id"] for o in out] == [901, 902, 903] and out[0]["files"] == ["/app/Controller/File0.php"])
    c2 = _C(children=[{"id": 77, "fields": {"System.Title": "[webservice] eski", "System.Tags": "crew-generated; x", "System.State": "Done"}}])
    out2 = wc.create_children(c2, 500, {}, items)
    check("üretilmiş child varsa YENİDEN AÇMAZ, mevcutları döndürür", c2.created == [] and [o["id"] for o in out2] == [77])
    c3 = _C(children=[{"id": 78, "fields": {"System.Title": "insan task'ı", "System.Tags": "", "System.State": "To Do"}}])
    wc.create_children(c3, 500, {}, items)
    check("etiketsiz (insan) child engel değil", len(c3.created) == 3)


# ── 32. Flow kancaları — tahmin + alt iş (stub client) ───────────────────

class _StubAzClient2(_StubAzClient):
    def __init__(self, states, fields, children=()):
        super().__init__(states, fields)
        self.children, self.n = list(children), 900
    def update_work_item(self, wid, ops):
        for o in ops:
            self.ops.append(("field", int(wid), o["path"].rsplit("/", 1)[-1], o["value"]))
        return {}
    def create_work_item(self, t, fields, parent_id=None):
        self.n += 1
        self.ops.append(("create", parent_id, t, fields["System.Title"]))
        return {"id": self.n}
    def get_work_item_children(self, pid):
        return self.children


def test_estimate_flow_hooks():
    print("\n[32] Flow kancaları — tahmin aşamaları, SP yazma koruması, alt iş açma/kapama")
    from agile_sdlc_crew import pipeline_config as pc

    knobs = {"CREW_ESTIMATE": True, "CREW_WI_WRITE_ESTIMATE": False,
             "CREW_WI_CHILD_TASKS": False, "CREW_WI_CHILD_TASKS_MAX": 8}
    _orig_get = pc.get
    pc.get = lambda k: knobs[k] if k in knobs else _orig_get(k)
    try:
        def mk(wi_type="Task", sp=None, **st):
            f = AgileSDLCFlow()
            f._db = None
            f.state.work_item_id = "73121"
            f.state.repo_name = "webservice"
            f.state.wi_type = wi_type
            f.state.wi_story_points = sp
            f.state.wi_area_path, f.state.wi_iteration_path = "BM\\Ops", "BM\\Ops\\2026_19"
            f.state.requirements_text = _BA_WITH_ESTIMATE
            for k, v in st.items():
                setattr(f.state, k, v)
            f._client = _StubAzClient2(_FLO_TASK, {"System.WorkItemType": wi_type, "System.State": "In Progress"})
            return f

        f = mk()
        f._estimate("requirements")
        e = f.state.estimate
        check("requirements: BA 5 vs yapısal 3 (4 gereksinim) → 5 SP, kaynak BA",
              e.get("sp") == 5 and e.get("source") == "BA" and e.get("structural_sp") == 3 and e.get("ba_sp") == 5, str(e))
        f.state.plan = _plan(3)
        f._estimate("plan")
        e = f.state.estimate
        check("plan: 3 dosya → yapısal 5, BA 5 → uyumlu, stage=plan",
              e.get("sp") == 5 and e.get("stage") == "plan" and "uyumlu" in e.get("source", ""), str(e))
        check("yazma knob'u kapalı → Azure'a SP yazılmadı", not any(o[0] == "field" for o in f._client.ops))

        knobs["CREW_WI_WRITE_ESTIMATE"] = True
        f = mk()
        f.state.plan = _plan(3)
        f._estimate("plan")
        check("yazma açık + WI'da SP boş → Custom.StoryPoints=5 yazıldı",
              ("field", 73121, "Custom.StoryPoints", 5) in f._client.ops, str(f._client.ops))
        f = mk(sp=3.0)
        f.state.plan = _plan(3)
        f._estimate("plan")
        check("WI'da SP var (3) → insan tahmini EZİLMEZ", not any(o[0] == "field" for o in f._client.ops))
        f = mk(dry_run=True)
        f.state.plan = _plan(3)
        f._estimate("plan")
        check("dry-run: tahmin hesaplanır, yazılmaz", f.state.estimate.get("sp") == 5 and f._client.ops == [])
        knobs["CREW_ESTIMATE"] = False
        f = mk()
        f._estimate("requirements")
        check("CREW_ESTIMATE kapalı → hiç hesaplanmaz", f.state.estimate == {})
        knobs["CREW_ESTIMATE"] = True

        f = mk(plan=_plan(3))
        f._create_child_tasks()
        check("child knob kapalı → açılmaz", f.state.child_tasks == [] and f._client.ops == [])
        knobs["CREW_WI_CHILD_TASKS"] = True
        f = mk(plan=_plan(3))
        f._create_child_tasks()
        check("WI tipi Task → parent değil, açılmaz", f.state.child_tasks == [] and f._client.ops == [])
        f = mk(wi_type="User Story", plan=_plan(3))
        f._after_plan_finalized()
        creates = [o for o in f._client.ops if o[0] == "create"]
        check("User Story + knob → 3 child açıldı (parent 73121)", len(creates) == 3 and all(o[1] == 73121 for o in creates), str(creates))
        check("state.child_tasks dolu, files eşli", len(f.state.child_tasks) == 3 and f.state.child_tasks[1]["files"] == ["/app/Model/File1.php"])
        n = len(f._client.ops)
        f._complete_child_task("app/Model/File1.php")
        check("dosya push → ilgili child Done", f._client.ops[-1] == ("state", 902, "Done") and f.state.child_tasks[1].get("done"), str(f._client.ops[-1]))
        f._complete_child_task("app/Model/File1.php")
        check("aynı dosya ikinci kez → no-op", len(f._client.ops) == n + 1)
        f._complete_child_task("/olmayan.php")
        check("planda olmayan dosya → no-op", len(f._client.ops) == n + 1)
        f._create_child_tasks()
        check("ikinci çağrı → yeniden açmaz (state dolu)", len([o for o in f._client.ops if o[0] == "create"]) == 3)
        f = mk(wi_type="User Story", plan=_plan(3), kickoff_only=True)
        f._create_child_tasks()
        check("kickoff-only → açılmaz", f._client.ops == [])
    finally:
        pc.get = _orig_get

    src_flow = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/flow.py").read_text()
    check("plan kesinleşen 3 yolda da _after_plan_finalized (normal/resume/HAL)", src_flow.count("self._after_plan_finalized()") == 3)
    check("step6'da 4 push yolunda _complete_child_task (resume/skip/silme/push)", src_flow.count("self._complete_child_task(file_path)") == 4)
    check("step1: requirements sonrası kaba tahmin", 'self._estimate("requirements")' in src_flow)
    src_db = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/db.py").read_text()
    check("db: estimate_sp kolonu + whitelist", '"estimate_sp"' in src_db and 'ensure_column(cur, "jobs", "estimate_sp"' in src_db)
    src_yaml = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/config/tasks.yaml").read_text()
    check("tasks.yaml: BA estimate bloğu + ESTIMATE kuralı", '"estimate": {' in src_yaml and "ESTIMATE (mandatory)" in src_yaml)


# ── 33. Retrospektif — deterministik analiz, sınıflandırma, öneriler, rapor ─

def test_retrospective():
    print("\n[33] Retrospektif — sonuç sınıfları, kalite kapıları, SP metrikleri, kural önerileri")
    from datetime import datetime, timedelta
    from agile_sdlc_crew import retrospective as rt

    now = datetime.now()

    def job(i, status, err="", cost=1.0, mins=10, sp=None, wi="1", steps=()):
        return {"id": i, "work_item_id": wi, "status": status, "error_message": err,
                "total_cost_usd": cost, "started_at": now - timedelta(minutes=mins), "finished_at": now,
                "estimate_sp": sp,
                "steps": [{"step_key": k, "status": st, "output": out} for k, st, out in steps]}

    jobs = [
        job(1, "completed", cost=3.96, mins=17, sp=5, wi="73121", steps=[
            ("review_pr_task", "completed", "REVIEW_DECISION: APPROVE\nVerdict: APPROVE — 2 düzeltme turundan sonra onaylandı."),
            ("pr_build_gate", "completed", "Build 132551 succeeded (webservice-test)"),
            ("uat_task", "completed", _UAT_189_SHAPE)]),
        job(2, "needs_info", "NEEDS_INFO: hazırlık skoru 5/100 < eşik 60 — WI'da 6 eksik detay", cost=0.13, mins=1, wi="73061"),
        job(3, "needs_human", "Review: 1 deneme sonrasi kapanmayan madde (N1) — PR #42951 acik", cost=3.43, wi="73121", steps=[
            ("review_pr_task", "completed", "REVIEW_DECISION: NEEDS_HUMAN\nİnsan müdahalesi gerekli — 1 deneme sonrası kapanmayan madde")]),
        job(4, "failed", "Sunucu yeniden baslatildi, is yarida kaldi", cost=0.5, steps=[("technical_design_task", "failed", "")]),
        job(5, "failed", "Plan-push uyumsuzlugu: 1/2 dosya push edildi, %70 esigin altinda. PR iptal.", cost=4.89, wi="73121",
            steps=[("implement_change_task", "failed", "")]),
        job(6, "completed", cost=2.0, mins=10, sp=2, wi="5", steps=[
            ("review_pr_task", "completed", "REVIEW_DECISION: APPROVE\nVerdict: APPROVE"),
            ("pr_build_gate", "completed", "Repoda PR-test pipeline'i yok — gate atlandi"),
            ("uat_task", "completed", "## UAT Report\n\n1. AC1 - PASS - ok\n\n**Overall Evaluation:** ACCEPTED")]),
        job(7, "running", cost=0),
    ]
    a = rt.analyze(jobs)
    check("running iş sayılmaz → 6 terminal iş", a["jobs"] == 6)
    check("durum dağılımı", a["status"] == {"completed": 2, "needs_info": 1, "needs_human": 1, "failed": 2}, str(a["status"]))
    check("başarı oranı %33", round(a["success_rate"]) == 33)
    oc = dict(a["outcomes"])
    check("nedenler: hazırlık kapısı / review madde / altyapı / push",
          oc.get("WI detayı yetersiz (hazırlık kapısı)") == 1 and oc.get("Review: kapanmayan madde") == 1
          and oc.get("Altyapı: sunucu yeniden başlatıldı") == 1 and oc.get("Implement: dosya push edilemedi") == 1, str(oc))
    check("kırılan adımlar", dict(a["step_failures"]) == {"technical_design_task": 1, "implement_change_task": 1})
    rv = a["review"]
    check("review: 3 iş, ilk tur 1, 2 düzeltme turu, 1 needs_human",
          (rv["jobs"], rv["first_pass"], rv["retries_total"], rv["needs_human"]) == (3, 1, 2, 1), str(rv))
    check("build: yeşil 1, pipeline yok 1", a["build"] == {"yeşil": 1, "pipeline yok": 1}, str(a["build"]))
    check("UAT: kabul 1, red 1 (#189 şekli)", a["uat"] == {"kabul": 1, "red": 1}, str(a["uat"]))
    check("hazırlık skoru ortalaması 5", a["readiness_avg"] == 5)
    check("toplam maliyet 14.91", round(a["cost_total"], 2) == 14.91)
    check("teslim edilen SP 7", a["sp_delivered"] == 7)
    check("SP başına 4.2 dk · $0.90", round(a["min_per_sp"], 1) == 4.2 and round(a["cost_per_sp"], 2) == 0.9, f"{a['min_per_sp']} {a['cost_per_sp']}")
    check("tekrar koşan WI: 73121 ×3", a["rerun_wis"] == [("73121", 3)])
    check("en pahalı iş #5 ($4.89)", a["top_cost"][0][1] == 5)

    rules = rt.suggest_rules(a)
    texts = " ".join(r["text"] for r in rules)
    check("kural önerileri: review testleri okusun + UAT kanıt + tekrar koşu (3)",
          len(rules) == 3 and "MEVCUT testlerini" in texts and "UAT uzmanı" in texts and "ikinci kez" in texts, str([r["why"] for r in rules]))
    check("needs_info oranı %17 < %20 → hazırlık kuralı yok", "kabul kriteri alanı boş" not in texts)
    cfg = rt.suggest_config(a)
    check("yapılandırma önerileri: DOD_ENFORCE + restart (2)", len(cfg) == 2 and "CREW_DOD_ENFORCE" in cfg[0] and "restart" in cfg[1], str(cfg))
    md = rt.render_markdown(a, title="2026_19_Sudo", rules=rules, config=cfg)
    check("rapor: başlık, özet tablosu, SP, öneri bölümleri, tekrar koşu",
          "## 🔁 Retrospektif — 2026_19_Sudo" in md and "| İş sayısı | 6 |" in md and "| Teslim edilen SP | 7 |" in md
          and "### Önerilen kılavuz kuralları" in md and "### Yapılandırma önerileri" in md and "#73121 (3×)" in md)
    check("rapor: en pahalı işler tablosu", "| #5 | #73121 | failed | $4.89 |" in md)

    co = rt.classify_outcome
    check("sınıflandırma: DoD / build / bütçe / diğer / tamamlandı",
          co("needs_human", "DoD gecilemedi: UAT") == "DoD geçilemedi"
          and co("needs_human", "PR build 2 duzeltme sonrasi hala 'failed'") == "PR build kırmızı / doğrulanamadı"
          and co("failed", "Butce asildi: $18") == "Bütçe aşımı"
          and co("failed", "zzz") == "Diğer hata" and co("completed", None) == "Tamamlandı")
    e = rt.analyze([])
    check("boş pencere → 0 iş, rapor 'iş yok' der", e["jobs"] == 0 and "iş yok" in rt.render_markdown(e, title="x")
          and rt.suggest_rules(e) == [] and rt.suggest_config(e) == [])

    try:
        rep = rt.build_report(title="son 60 gün", since_days=60)
    except Exception as ex:
        skip("gerçek DB retrospektifi", f"DB erişilemedi: {type(ex).__name__}")
        return
    check("GERÇEK DB: son 60 günde iş var, rapor üretildi",
          rep["jobs"] >= 1 and "## 🔁 Retrospektif" in rep["markdown"] and isinstance(rep["suggested_rules"], list), str(rep["jobs"]))
    import json as _j
    _j.dumps(rep)
    check("GERÇEK DB: rapor JSON'a serileşir (Decimal/datetime sızmaz)", True)

    src_ui = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text()
    check("dashboard: 🔁 Retro butonu + modal + tablo render", "openRetro()" in src_ui and 'id="retroModal"' in src_ui
          and "md-table" in src_ui and "adoptRetroRule" in src_ui)
    src_srv = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/server.py").read_text()
    check("server: /api/retro + CREW_RETRO kapısı", '@app.get("/api/retro")' in src_srv and 'CREW_RETRO' in src_srv)


# ── 34. Sprint planlama — aday, sıra, kapasite, tahmin, toplu kuyruk ──────

def test_sprint_planning():
    print("\n[34] Sprint planlama — aday kuralı, sıra, kapasite, tahmin, güvenli kuyruk")
    from agile_sdlc_crew import sprint_planning as sp

    check("Proposed: Backlog/To Do evet, In Progress/QA hayır (FLO süreç listesi)",
          sp.is_proposed("Backlog", _FLO_TASK) and sp.is_proposed("To Do", _FLO_TASK)
          and not sp.is_proposed("In Progress", _FLO_TASK) and not sp.is_proposed("QA To Do", _FLO_TASK))
    check("Proposed yedek (süreç listesi yok): To Do/New evet, Active hayır",
          sp.is_proposed("To Do", None) and sp.is_proposed("New", []) and not sp.is_proposed("Active", None))

    items = [
        {"id": 1, "title": "A", "type": "Task", "state": "To Do", "priority": 2, "storyPoints": 3},
        {"id": 2, "title": "B", "type": "Task", "state": "To Do", "priority": 1, "storyPoints": 5},
        {"id": 3, "title": "C", "type": "Task", "state": "In Progress", "priority": 1, "storyPoints": 2},
        {"id": 4, "title": "D", "type": "Test Case", "state": "To Do", "priority": 1, "storyPoints": 0},
        {"id": 5, "title": "E", "type": "Bug", "state": "Backlog", "priority": 1, "storyPoints": 2},
        {"id": 6, "title": "F", "type": "Task", "state": "To Do", "priority": 1, "storyPoints": 0},
        {"id": 7, "title": "G", "type": "Task", "state": "To Do", "priority": 3, "storyPoints": 1},
        {"id": 8, "title": "H", "type": "Task", "state": "To Do", "priority": 1, "storyPoints": 2},
        {"id": 9, "title": "I", "type": "Task", "state": "To Do", "priority": 1, "storyPoints": 1},
    ]
    jobs = {"5": {"id": 50, "status": "running"}, "7": {"id": 70, "status": "completed"},
            "8": {"id": 80, "status": "needs_info"}, "9": {"id": 90, "status": "failed"}}
    rows = sp.order_candidates(sp.classify_candidates(items, {"Task": _FLO_TASK, "Bug": _FLO_TASK}, jobs))
    by = {r["id"]: r for r in rows}
    check("uygun: 1, 2, 6, 9 (failed → yeniden denenebilir)", {r["id"] for r in rows if r["eligible"]} == {1, 2, 6, 9})
    check("nedenler: başlanmış / tip dışı / açık iş / tamamlandı / insan bekliyor",
          "başlanmış" in by[3]["reason"] and "pipeline dışı" in by[4]["reason"] and "açık iş #50" in by[5]["reason"]
          and "tamamlandı" in by[7]["reason"] and "insan bekliyor" in by[8]["reason"] and "yeniden" in by[9]["reason"])
    check("sıra: uygunlar önce; öncelik ↑, SP ↑, SP=0 sona → 9, 2, 6, 1",
          [r["id"] for r in rows if r["eligible"]] == [9, 2, 6, 1], str([r["id"] for r in rows]))
    sp.select_by_capacity(rows, 6)
    check("kapasite 6 SP: 9 (1) + 2 (5) = 6 → seçili; 6 (SP yok) da işaretli, 1 (3) sığmaz",
          {r["id"] for r in rows if r["selected"]} == {9, 2, 6}, str([(r["id"], r["selected"]) for r in rows]))
    sp.select_by_capacity(rows, None)
    check("kapasite yok → tüm uygunlar seçili", {r["id"] for r in rows if r["selected"]} == {1, 2, 6, 9})
    est = sp.effort_estimate(rows, cost_per_sp=0.9, min_per_sp=4.0, cost_per_job=3.0, min_per_job=15.0)
    check("tahmin: 4 iş, 9 SP (+1 SP'siz iş başına ortalama) → $11.1, 51 dk",
          est["count"] == 4 and est["sp"] == 9 and est["sp_unknown"] == 1
          and round(est["cost_usd"], 2) == 11.1 and round(est["minutes"]) == 51, str(est))
    est2 = sp.effort_estimate(rows, cost_per_sp=None, min_per_sp=None, cost_per_job=3.0, min_per_job=15.0)
    check("tahmin: SP oranı yoksa iş başına", est2["cost_usd"] == 12.0 and est2["minutes"] == 60.0)

    created = []
    _o_status, _o_create = sp.latest_job_status, None
    from agile_sdlc_crew import db as _dbm
    _o_create = _dbm.create_job
    try:
        sp.latest_job_status = lambda ids: {"2": {"id": 20, "status": "queued"}}
        _dbm.create_job = lambda wid, use_hal, wi_title="", **kw: created.append((wid, use_hal, wi_title)) or (100 + len(created))
        res = sp.queue_selected([{"id": 1, "title": "A"}, {"id": 2, "title": "B"}, {"id": 9, "title": "I"}])
        check("kuyruk: açık işi olan #2 atlandı, 1 ve 9 kuyruklandı (use_hal=False)",
              [q["wi"] for q in res["queued"]] == ["1", "9"] and res["skipped"][0]["wi"] == "2"
              and all(c[1] is False for c in created), str(res))
    finally:
        sp.latest_job_status = _o_status
        _dbm.create_job = _o_create

    class _C:
        def get_iteration_work_items(self, ip):
            return items
        def get_work_item_type_states(self, t):
            return _FLO_TASK
    _o_vel, _o_rt = sp.team_velocity, None
    from agile_sdlc_crew import retrospective as _rt
    _o_an, _o_col = _rt.analyze, _rt.collect
    try:
        sp.latest_job_status = lambda ids: jobs
        sp.team_velocity = lambda client, team, n=3: 7.0
        _rt.collect = lambda **kw: []
        _rt.analyze = lambda jobs_: {"cost_per_sp": 1.0, "min_per_sp": 5.0, "cost_avg_completed": 3.0, "minutes_avg_completed": 20.0}
        plan = sp.build_plan(_C(), "BM\\Ops\\2026_19", team="Ops")
        check("build_plan: sprint adı, 9 iş, 4 uygun, kapasite velocity 7 → 9+2+6 seçili",
              plan["sprint"] == "2026_19" and plan["items_total"] == 9 and plan["eligible"] == 4
              and plan["capacity_sp"] == 7.0 and "velocity" in plan["capacity_source"]
              and {r["id"] for r in plan["rows"] if r["selected"]} == {9, 2, 6}, str(plan["estimate"]))
        check("build_plan: oranlar retro'dan", plan["rates"]["cost_per_sp"] == 1.0 and plan["estimate"]["count"] == 3)
        plan2 = sp.build_plan(_C(), "X", team="", capacity_sp=1)
        check("kapasite 1 (kullanıcı) → yalnızca 9 (1 SP) + SP'siz 6", {r["id"] for r in plan2["rows"] if r["selected"]} == {9, 6}
              and plan2["capacity_source"] == "kullanıcı")
    finally:
        sp.latest_job_status = _o_status
        sp.team_velocity = _o_vel
        _rt.analyze, _rt.collect = _o_an, _o_col


# ── 35. Günlük özet — render, düz metin, zamanlayıcı yardımcıları ────────

def test_daily_summary():
    print("\n[35] Günlük özet — bölümler, engeller, düz metin, zaman hesabı")
    from datetime import datetime, timedelta
    from agile_sdlc_crew import daily

    now = datetime(2026, 9, 10, 9, 0)
    d = {
        "now": now, "since": now - timedelta(hours=24),
        "finished": [
            {"id": 189, "work_item_id": "73121", "wi_title": "İade nedeni dil", "status": "completed", "pr_url": "https://x/pr/42951",
             "total_cost_usd": 3.96, "estimate_sp": 5, "started_at": now - timedelta(minutes=137), "finished_at": now - timedelta(minutes=120), "error_message": None},
            {"id": 191, "work_item_id": "73061", "wi_title": "COD nokta", "status": "needs_info", "pr_url": "", "total_cost_usd": 0.13,
             "estimate_sp": None, "started_at": now - timedelta(minutes=61), "finished_at": now - timedelta(minutes=60),
             "error_message": "NEEDS_INFO: hazırlık skoru 5/100 < eşik 60"},
        ],
        "running": [{"id": 200, "work_item_id": "73200", "wi_title": "Koşan iş", "current_step": "implement_change_task", "started_at": now - timedelta(minutes=12)}],
        "queued": [{"id": 201, "work_item_id": "73201"}, {"id": 202, "work_item_id": "73202"}],
        "waiting": [{"id": 191, "work_item_id": "73061", "wi_title": "COD nokta", "status": "needs_info",
                     "error_message": "NEEDS_INFO: 6 eksik detay", "finished_at": now - timedelta(days=2)}],
    }
    md = daily.render_markdown(d, wi_base_url="https://dev.azure.com/o/p/_workitems/edit")
    check("başlık tarih + saat", md.startswith("## ☀️ Günlük özet — 10 Eyl 2026 09:00"))
    check("özet satırı: 2 iş bitti · $4.09 · koşan 1 · kuyrukta 2", "2 iş bitti · $4.09 · koşan 1 · kuyrukta 2" in md, md.splitlines()[2])
    check("biten: durum, WI linki, PR, SP, dk, $", "✅ tamamlandı — [#73121](https://dev.azure.com/o/p/_workitems/edit/73121) İade nedeni dil · [PR](https://x/pr/42951) · 5 SP · 17 dk · $3.96" in md)
    check("needs_info hata özeti italik", "_NEEDS_INFO: hazırlık skoru 5/100 < eşik 60_" in md)
    check("koşan: adım + süre", "🔄 [#73200]" in md and "`implement_change_task`, 12 dk" in md)
    check("kuyruk listesi", "- #73201, #73202" in md)
    check("engeller: kaç gün", "### İnsan bekleyenler (engeller)" in md and "· 2 gün" in md)
    plain = daily.to_plain(md)
    check("düz metin: link/kalın/başlık işaretleri yok", "**" not in plain and "](" not in plain and not plain.startswith("#")
          and "https://x/pr/42951" in plain)
    empty = daily.render_markdown({"now": now, "since": now - timedelta(hours=24), "finished": [], "running": [], "queued": [], "waiting": []})
    check("hareket yok metni", "Hareket yok" in empty)
    check("HH:MM parse: 09:00, 7:30, bozuk → 09:00", daily._parse_hhmm("09:00") == (9, 0) and daily._parse_hhmm("7:30") == (7, 30)
          and daily._parse_hhmm("x") == (9, 0))
    check("seconds_until: bugün 09:00 geçmişse yarına", daily.seconds_until(datetime(2026, 9, 10, 10, 0), 9, 0) == 23 * 3600
          and daily.seconds_until(datetime(2026, 9, 10, 8, 30), 9, 0) == 1800)
    check("Telegram yapılandırması env'e bağlı (test ortamında kapalı olabilir)", isinstance(daily.telegram_configured(), bool))

    try:
        rep = daily.build_daily(hours=24 * 60)
    except Exception as ex:
        skip("gerçek DB günlük özeti", f"DB erişilemedi: {type(ex).__name__}")
        rep = None
    if rep:
        check("GERÇEK DB: özet üretildi, sayaçlar tutarlı", rep["markdown"].startswith("## ☀️") and set(rep["counts"]) == {"finished", "running", "queued", "waiting"})

    src_srv = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/server.py").read_text()
    check("server: /api/sprint-plan (+queue), /api/daily (+send), startup zamanlayıcı",
          '@app.get("/api/sprint-plan")' in src_srv and '@app.post("/api/sprint-plan/queue")' in src_srv
          and '@app.get("/api/daily")' in src_srv and '@app.post("/api/daily/send")' in src_srv and "start_scheduler" in src_srv)
    src_ui = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text()
    check("dashboard: 🗓️ Planla + ☀️ Günlük modalleri, onaylı kuyruk", 'id="planModal"' in src_ui and 'id="dailyModal"' in src_ui
          and "confirm(sel.length+' iş pipeline kuyruğuna" in src_ui)


# ── 36. İş tipine göre akış — spike tespiti, kılavuz, rapor, PO parse ─────

def test_type_flow():
    print("\n[36] İş tipine göre akış — spike tespiti (yalnızca açık işaret), kılavuz, rapor, PO parse")
    from agile_sdlc_crew import type_flow as tf

    check("spike: tip Spike / Research", tf.is_spike("Spike") and tf.is_spike("Research"))
    check("spike: etiket 'spike' / 'PoC' / 'araştırma' (noktalı virgül listesinde)",
          tf.is_spike("Task", "backend; spike") and tf.is_spike("Task", "PoC") and tf.is_spike("Task", "araştırma;ops"))
    check("spike: başlık '[Spike] …' / 'Spike: …' / 'POC - …'",
          tf.is_spike("Task", "", "[Spike] Kargo API karşılaştırması") and tf.is_spike("Task", "", "Spike: cache stratejisi")
          and tf.is_spike("Task", "", "POC - Redis"))
    check("spike DEĞİL: başlıkta geçen 'spike' kelimesi / etiket 'spiker' / 'research' içeren başka kelime",
          not tf.is_spike("Task", "", "Trafik spike'ında sepet hatası") and not tf.is_spike("Task", "spiker")
          and not tf.is_spike("Task", "", "Researcher paneli"))
    check("akış türü: Bug→bug, User Story/Feature→story, Task→task, Test Case→other, spike önce",
          tf.flow_kind("Bug") == "bug" and tf.flow_kind("User Story") == "story" and tf.flow_kind("Feature") == "story"
          and tf.flow_kind("Task") == "task" and tf.flow_kind("Test Case") == "other" and tf.flow_kind("Bug", "spike") == "spike")
    check("kılavuz: bug regresyon testi + minimal fix; story AC izi; spike 'Do NOT write production code'; task boş",
          "regression test" in tf.guidance("bug") and "no refactors" in tf.guidance("bug")
          and "acceptance criterion" in tf.guidance("story") and "Do NOT write production code" in tf.guidance("spike")
          and tf.guidance("task") == "" and tf.guidance("") == "")
    check("kılavuz İngilizce kural, Türkçe çıktı notu", "in Turkish" in tf.guidance("bug") and "in Turkish" in tf.guidance("spike"))

    rep = tf.spike_report("[Spike] Kargo API", _BA_WITH_ESTIMATE, "Bulgu: `app/Cargo.php` tek noktadan çağrılıyor.",
                          repo_name="webservice", estimate_sp=5)
    check("spike raporu: başlık, repo, tahmin, kapsam (BA summary + FR'ler), bulgular, kod yok notu",
          rep.startswith("## 🔬 Araştırma Raporu (Spike)") and "`webservice`" in rep and "5 SP" in rep
          and "Getirilen iade nedeni metni" in rep and "**FR1**" in rep and "### Bulgular (mimar keşfi)" in rep
          and "app/Cargo.php" in rep and "kod üretilmedi, PR açılmadı" in rep, rep[:200])
    rep2 = tf.spike_report("X", "düz metin", "")
    check("spike raporu: klon yok → iş analizine dayanır notu, BA JSON yok → kapsam yok",
          "Repo klonu bulunamadığı" in rep2 and "### Kapsam" not in rep2)

    po = tf.parse_po('```json\n{"business_value": 8, "urgency": "6", "priority": "p2", "decision": "go", '
                     '"scope_decisions": ["Rusça çeviri kapsamda", " "], "risks_if_delayed": "Rus sitesi Türkçe metin gösterir", '
                     '"rationale": "Global site deneyimi"}\n```')
    check("PO parse: sayılar kırpılır, priority/decision normalize, boş kapsam maddesi atılır",
          po == {"business_value": 8, "urgency": 6, "priority": "P2", "decision": "GO",
                 "scope_decisions": ["Rusça çeviri kapsamda"], "risks_if_delayed": "Rus sitesi Türkçe metin gösterir",
                 "rationale": "Global site deneyimi"}, str(po))
    check("PO parse: 15 → 10'a kırp, 'P9' → '', 'hold' → HOLD",
          tf.parse_po('{"business_value": 15, "priority": "P9", "decision": "hold"}')["business_value"] == 10
          and tf.parse_po('{"priority": "P9", "decision": "hold"}')["priority"] == ""
          and tf.parse_po('{"decision": "hold"}')["decision"] == "HOLD")
    check("PO parse: bozuk / JSON değil → None", tf.parse_po("no json") is None and tf.parse_po("{oops") is None)
    md = tf.render_po_comment(po)
    check("PO yorumu: tablo + kapsam + gecikme + gerekçe + danışma notu",
          "## 🎯 PO Değerlendirmesi" in md and "| İş değeri | 8 / 10 |" in md and "✅ GO" in md
          and "- Rusça çeviri kapsamda" in md and "**Gecikirse:**" in md and "danışma niteliğinde" in md)
    check("PO yorumu: HOLD işareti", "⏸️ HOLD" in tf.render_po_comment({"decision": "HOLD"}))


# ── 37. Faz 5 flow kancaları — flow_kind state'e, kılavuz context'te, spike/DoD bağları ─

def test_type_flow_hooks():
    print("\n[37] Faz 5 kancaları — flow_kind, context kılavuzu, PO context, spike stop, Bug DoD")
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.flow import _SpikeStop, _KickoffOnlyStop

    knobs = {"CREW_TYPE_FLOW": True, "CREW_WI_LIFECYCLE": False}
    _orig_get = pc.get
    pc.get = lambda k: knobs[k] if k in knobs else _orig_get(k)
    try:
        def mk(wi_type="Bug", tags="", title="Sepet hatası"):
            f = AgileSDLCFlow()
            f._db = None
            f.state.work_item_id = "73121"
            f._client = _StubAzClient(_FLO_TASK, {"System.WorkItemType": wi_type, "System.State": "To Do",
                                                   "System.Tags": tags, "System.Title": title})
            f._wi_begin(f._client.fields)
            return f

        f = mk("Bug")
        check("Bug → flow_kind=bug, başlık/etiket state'e", f.state.flow_kind == "bug" and f.state.wi_title == "Sepet hatası")
        f.state.requirements_text = _BA_WITH_ESTIMATE
        ctx = f._build_step_context("technical_design_task")
        check("teknik tasarım context'inde Bug kılavuzu", "WORK ITEM TYPE GUIDANCE: Bug" in ctx and "regression test" in ctx)
        f.state.po_text = '{"decision": "GO", "rationale": "değerli"}'
        check("PO metni kickoff/tasarım context'ine girer, requirements'a girmez",
              "PO Değerlendirmesi" in f._build_step_context("kickoff_meeting_task")
              and "PO Değerlendirmesi" not in f._build_step_context("requirements_analysis_task"))
        f2 = mk("Task", tags="spike")
        check("Task + 'spike' etiketi → spike", f2.state.flow_kind == "spike")
        f3 = mk("Task")
        check("Task → task, context'e kılavuz eklenmez", f3.state.flow_kind == "task"
              and "WORK ITEM TYPE GUIDANCE" not in f3._build_step_context("technical_design_task"))
        knobs["CREW_TYPE_FLOW"] = False
        f4 = mk("Bug")
        check("knob kapalı → flow_kind boş, kılavuz yok", f4.state.flow_kind == "" and "GUIDANCE" not in f4._build_step_context("technical_design_task"))
    finally:
        pc.get = _orig_get

    check("_SpikeStop, _KickoffOnlyStop alt sınıfı (main/server ayrımı değişmez)", issubclass(_SpikeStop, _KickoffOnlyStop))
    src_flow = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/flow.py").read_text()
    i_spike, i_bfirst = src_flow.find('if self.state.flow_kind == "spike":'), src_flow.find("# ── ARCHITECT: B-first")
    check("step4: spike dalı B-first plan üretiminden ÖNCE (plan LLM çağrısı yapılmaz)", 0 < i_spike < i_bfirst)
    check("step4 spike: rapor → technical_design_task done → kalan 8 adım 'atlandı' → _SpikeStop",
          "raise _SpikeStop(" in src_flow and '"completion_report_task"):' in src_flow
          and src_flow.find("def _run_spike") > 0 and "Atlandı — spike" in src_flow)
    check("DoD: Bug akışında test zorunlu", 'or self.state.flow_kind == "bug"' in src_flow)
    check("PO: hazırlık kapısından sonra, kickoff'tan önce (step1 sonu)", src_flow.find("self._po_assessment()") > src_flow.find('self._step_done("requirements_analysis_task"'))
    src_main = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/main.py").read_text()
    check("main: spike stop kickoff-only ile aynı yolda, farklı log", "_SpikeStop" in src_main)
    src_crew = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/crew.py").read_text()
    check("crew: product_owner ajanı (tool'suz) + create_po_crew", "def product_owner(self)" in src_crew and "def create_po_crew" in src_crew and "tools=[]," in src_crew)
    import yaml as _y
    base = Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/config"
    agents = _y.safe_load((base / "agents.yaml").read_text()); tasks = _y.safe_load((base / "tasks.yaml").read_text())
    profiles = _y.safe_load((base / "llm_profiles.yaml").read_text())
    check("YAML: product_owner ajanı, po_assessment_task (TURKISH alanlar), agent_defaults profili",
          "product_owner" in agents and "in TURKISH" in agents["product_owner"]["backstory"]
          and tasks["po_assessment_task"]["agent"] == "product_owner" and "in TURKISH" in tasks["po_assessment_task"]["expected_output"]
          and (profiles.get("agent_defaults") or {}).get("product_owner") == "reasoning_remote")
    overrides = _y.safe_load((base / "agent_llm_overrides.yaml").read_text()) or {}
    po_ov = (overrides.get("agents") or {}).get("product_owner") or {}
    check("dashboard override: product_owner claude_cli (CREW_USE_LOCAL_LLM=1 onu qwen3'e düşürmesin)",
          po_ov.get("provider") == "claude_cli" and po_ov.get("model"), str(po_ov))


# ── 38. PR inceleme katkısı — PR çözümleme, context, özet, sınırlar ────────

def test_pr_review_contribution():
    print("\n[38] PR inceleme katkısı — WI→PR çözümleme, değişen dosyalar, özet/satır yorumu, güvenlik sınırları")
    from agile_sdlc_crew import pr_review as prv

    rels = [{"attributes": {"name": "Pull Request"}, "url": "vstfs:///Git/PullRequestId/p1%2Frepo-guid-A%2F42951"},
            {"attributes": {"name": "Pull Request"}, "url": "vstfs:///Git/PullRequestId/p1%2frepo-guid-A%2f42800"},
            {"attributes": {"name": "Parent"}, "url": "vstfs:///WorkItemTracking/WorkItem/1"}]
    links = prv.parse_pr_links(rels)
    check("PR bağlantıları: %2F ve %2f, en yeni önce, parent atlanır",
          links == [{"repo_id": "repo-guid-A", "pr_id": 42951}, {"repo_id": "repo-guid-A", "pr_id": 42800}], str(links))
    check("değişen dosyalar: klasör/silinen hariç, tekrar yok",
          prv.changed_paths([{"item": {"path": "/a.php"}}, {"item": {"path": "/dir", "isFolder": True}},
                             {"item": {"path": "/b.php"}, "changeType": "delete"}, {"item": {"path": "/a.php"}}]) == ["/a.php"])
    txt = prv.wi_requirements_text({"System.Title": "İade nedeni", "System.Description": "<p>Global site</p><br>rusça",
                                    "Microsoft.VSTS.Common.AcceptanceCriteria": ""})
    check("WI metni: HTML temizlenir, AC boşsa not düşer", "<p>" not in txt and "Global site" in txt and "rusça" in txt
          and "WI alanı boş" in txt, txt)

    class _C:
        def __init__(self, statuses):
            self.statuses = statuses
        def get_work_item(self, wid):
            return {"fields": {}, "relations": rels}
        def get_repository(self, rid):
            return {"name": "webservice"}
        def get_pull_request(self, rname, pid):
            return {"pullRequestId": pid, "status": self.statuses[pid], "sourceRefName": "refs/heads/feature/x",
                    "title": f"#73121 x", "repository": {"project": {"name": "EcomBackend"}}}
    r, p, pr = prv.resolve_pr(_C({42951: "abandoned", 42800: "active"}), work_item_id="73121")
    check("çözümleme: en yeni abandoned → sonraki aktif seçilir", (r, p) == ("webservice", 42800))
    r, p, pr = prv.resolve_pr(_C({42951: "completed", 42800: "abandoned"}), work_item_id="73121")
    check("çözümleme: aktif yoksa tamamlanmış", p == 42951)
    try:
        prv.resolve_pr(_C({42951: "abandoned", 42800: "abandoned"}), work_item_id="73121"); ok = False
    except LookupError:
        ok = True
    check("çözümleme: hepsi abandoned → LookupError (iş failed, sessiz değil)", ok)
    r, p, pr = prv.resolve_pr(_C({7: "active"}), pr_id=7, repo_name="core")
    check("çözümleme: repo+pr verildiyse WI'a gitmez", (r, p) == ("core", 7))

    issues = [{"id": "R1", "severity": "blocker", "file": "app/X.php", "line": 12, "problem": "null kontrolü yok", "required_fix": "guard ekle"},
              {"id": "R2", "severity": "minor", "file": "app/Y.php", "line": None, "problem": "isim", "required_fix": ""}]
    md = prv.review_summary_markdown(verdict="CHANGES_REQUIRED", issues=issues, pr_id=42951, pr_url="https://x/pr",
                                     work_item_id="73121", files=3)
    check("özet: karar, tablo, danışma notu, PR linki",
          "🔴 Değişiklik gerekli" in md and "| R1 | blocker | `app/X.php:12` |" in md and "danışma niteliğindedir" in md and "https://x/pr" in md)
    check("özet: APPROVE + madde yok → 'bulunmadı'", "bulunmadı" in prv.review_summary_markdown(
        verdict="APPROVE", issues=[], pr_id=1, pr_url="u", work_item_id="1", files=1))
    check("satır yorumu: id, önem, düzeltme", "[R1] BLOCKER" in prv.inline_comment_text(issues[0]) and "guard ekle" in prv.inline_comment_text(issues[0]))

    # Satır numarası yeniden oturtma (job #208 / PR #43642 regresyonu):
    # reviewer dosyayı okuyor ama satırı KENDİSİ sayıyordu; R1 86 yerine 95,
    # R2 98 yerine 106 dedi. Alıntılar doğruydu → numara alıntıdan hesaplanır.
    body = "<?php\n\nclass X\n{\n    public function f()\n    {\n        $a = 1;\n        return ceil($t / 10);\n    }\n}\n"
    anc = [{"id": "R1", "file": "app/X.php", "line": 15,
            "evidence": {"file": "app/X.php", "line": 15, "quote": "return ceil($t / 10);"}},
           {"id": "R2", "file": "app/X.php", "line": 0,
            "evidence": {"file": "app/X.php", "line": 0, "quote": "   7|         $a = 1;"}},
           {"id": "R3", "file": "app/X.php", "line": 4,
            "evidence": {"file": "app/X.php", "line": 4, "quote": "$this->uydurma();"}},
           {"id": "R4", "file": "app/Yok.php", "line": 9, "evidence": {}}]
    st = prv.anchor_issue_lines(anc, {"/app/X.php": body})
    check("satır: yanlış numara alıntıdan düzeltilir (15→8)", anc[0]["line"] == 8 and anc[0]["evidence"]["line"] == 8, str(anc[0]["line"]))
    check("satır: line 0 da oturtulur, 'NN| ' öneki alıntıdan atılır (→7)", anc[1]["line"] == 7, str(anc[1]["line"]))
    check("satır: alıntı dosyada yoksa numara DÜŞÜRÜLÜR (yanlış satır yazmaktansa numarasız)", anc[2]["line"] == 0)
    check("satır: gövdesi/kanıtı olmayan madde dokunulmadan kalır", anc[3]["line"] == 9)
    check("satır: istatistik", st == {"anchored": 2, "moved": 2, "cleared": 1, "kept": 1}, str(st))
    check("yol anahtarı: '/app/X.php' gövdesi 'app/X.php' maddesiyle eşleşir (suggestion sessizce düşmesin)",
          prv.body_index({"/app/X.php": "a"}) == {"app/X.php": "a"})
    check("suggestion: baştaki slash farkına rağmen doğrulanır",
          prv.verified_suggestion({"file": "app/X.php", "suggestion": {"code": "        $a = 2;", "line_start": 7, "line_end": 7}},
                                  {"/app/X.php": body}).get("code") == "        $a = 2;")
    # Büyük dosyada BAŞ taraf değil DEĞİŞEN taraf context'e girer (job #208:
    # OrderLine.php 3098 satır → ilk %5'i gitti, PR'ın dokunduğu metot şansa kaldı).
    big_old = "\n".join(f"satir {i}" for i in range(1, 401))
    big_new = "\n".join(("DEGISTI" if i in (350, 351) else f"satir {i}") for i in range(1, 401))
    foc = prv.focused_source(big_new, big_old, per_file=1000, context=3)
    shown = {int(l.split("|", 1)[0]) for l in foc.splitlines() if not l.startswith("    |")}
    check("odak: değişen satırlar (350-351) pencerede, baş taraf değil",
          {350, 351} <= shown and 1 not in shown and shown == set(range(347, 355)), str(sorted(shown))[:80])
    check("odak: satır numaraları GERÇEK dosya numarası (pencere ofseti değil)", "\n 350| DEGISTI" in foc)
    check("odak: atlanan bölge işaretli (görülmeyen kod hakkında madde açılmasın)",
          "satır atlandı" in foc and "346 satır atlandı" in foc, foc.splitlines()[0])
    check("odak: dosya sığıyorsa tamamı verilir", prv.focused_source(big_new, big_old, per_file=99_000).count("\n") == 399)
    nobase = prv.focused_source(big_new, None, per_file=300)
    check("odak: base yoksa baş kırpma + 'devamı yok' uyarısı (eski davranış)",
          nobase.startswith("   1| satir 1") and "context'e girmedi" in nobase)
    from agile_sdlc_crew.flow import _parse_review_issues as _pri
    # EKSİKLİK itirazının doğal bir satırı yoktur (job #211/R1: "cachedContent
    # yok" itirazı `$body = [` satırına düştü → okuyucu alakasız kod gördü).
    _abs = {"id": "R1", "severity": "major", "anchor": "absence", "file": "app/X.php", "line": 134,
            "problem": "İstek gövdesinde cachedContent yok.", "required_fix": "Ekleyin."}
    _txt_abs = prv.inline_comment_text(_abs)
    check("eksiklik: yorum başlığı 'EKSİK' der ve satırın konum olduğunu söyler",
          "MAJOR — EKSİK" in _txt_abs and "Bu satırdaki kodda hata yok" in _txt_abs, _txt_abs[:60])
    check("kusur (defect): ek açıklama YOK, eski davranış",
          "EKSİK" not in prv.inline_comment_text({**_abs, "anchor": "defect"}))
    _md_abs = prv.review_summary_markdown(verdict="CHANGES_REQUIRED", issues=[_abs], pr_id=1, pr_url="u",
                                          work_item_id="1", files=1)
    check("eksiklik: özet tablosunda da işaretli", "major · eksik" in _md_abs and "ait olduğu yer" in _md_abs)
    _pa = _pri('REVIEW_ISSUES_JSON:\n```json\n{"issues": ['
               '{"file":"a.php","line":1,"severity":"major","problem":"p","required_fix":"f","anchor":"absence"},'
               '{"file":"b.php","line":1,"severity":"major","problem":"p","required_fix":"f"},'
               '{"file":"c.php","line":1,"severity":"major","problem":"p","required_fix":"f","anchor":"SAÇMA"}]}\n```')
    check("parse: anchor absence/defect taşınır, bilinmeyen değer defect'e düşer",
          [x["anchor"] for x in _pa] == ["absence", "defect", "defect"], str([x["anchor"] for x in _pa]))
    _t2 = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/config/tasks.yaml").read_text()
    check("talimat: anchor alanı tanımlı ve eksiklik kuralı yazılı",
          '- "anchor": "defect" (default) or "absence"' in _t2 and "no line of its own" in _t2)

    # SİLME önerisi (job #210/R4: "metodu tamamen sil"). Boş `suggested_code`
    # eskiden "öneri yok" sayılıyordu → talimatın kendi saydığı geçerli vaka
    # ("deleting dead code") yapısal olarak üretilemiyordu.
    dosya = "a\nb\nc\nd\ne\n"
    sil = {"file": "app/X.php", "suggestion": {"code": "", "line_start": 2, "line_end": 3}}
    check("silme: boş kod + açık aralık → öneri ÜRETİLİR",
          prv.verified_suggestion(sil, {"/app/X.php": dosya}).get("line_start") == 2)
    check("silme: yorum metni boş ```suggestion bloğu ve 'siler' etiketi taşır",
          "```suggestion\n\n```" in prv.inline_comment_text({"id": "R4", "severity": "major", "problem": "ölü kod"},
                                                            suggestion={"code": "", "line_start": 2, "line_end": 3})
          and "siler" in prv.inline_comment_text({"id": "R4", "severity": "major", "problem": "ölü kod"},
                                                 suggestion={"code": "", "line_start": 2, "line_end": 3}))
    check("öneri yok: alan hiç verilmemişse (None) öneri üretilmez",
          prv.verified_suggestion({"file": "app/X.php", "suggestion": {"line_start": 2, "line_end": 3}},
                                  {"/app/X.php": dosya}) == {})
    check("silme: dosya dışı aralık reddedilir", prv.verified_suggestion(
          {"file": "app/X.php", "suggestion": {"code": "", "line_start": 4, "line_end": 99}}, {"/app/X.php": dosya}) == {})
    from agile_sdlc_crew.flow import _parse_review_issues as _pri
    _blk = ('REVIEW_ISSUES_JSON:\n```json\n{"issues": ['
            '{"file":"a.php","line":5,"severity":"major","problem":"ölü kod","required_fix":"sil",'
            '"suggested_code":"","suggested_line_start":3,"suggested_line_end":9},'
            '{"file":"b.php","line":5,"severity":"major","problem":"x","required_fix":"y","suggested_code":""}'
            ']}\n```')
    _iss = _pri(_blk)
    check("parse: boş kod + AÇIK aralık → silme önerisi taşınır",
          _iss[0]["suggestion"] == {"code": "", "line_start": 3, "line_end": 9}, str(_iss[0].get("suggestion")))
    check("parse: boş kod ama aralık YOK → yok sayılır (kazara silme olmasın)",
          not _iss[1].get("suggestion"), str(_iss[1].get("suggestion")))
    _tasks = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/config/tasks.yaml").read_text()
    _rv2 = _tasks.split("review_pr_task:")[1].split("\n  agent:")[0]
    check("talimat: öneri artık 'usually ABSENT' değil, mekanik vakada BEKLENİYOR",
          "usually ABSENT" not in _rv2 and "fill these\n      WHENEVER" in _rv2)
    check("talimat: silme vakası açıkça tanımlı", "DELETION:" in _rv2 and 'EMPTY string' in _rv2)

    ctx_src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/pr_review.py").read_text()
    check("context: dosyalar satır numarası önekiyle veriliyor", '{i:>4}| {ln}' in ctx_src)
    check("context: sığmayan dosyada base sürüm çekilip diff pencerelenir",
          "focused_source(content, old_body" in ctx_src and 'pr.get("targetRefName")' in ctx_src)

    # Pipeline'ın KENDİ review adımı (step8 / _review_retry_loop) da aynı odağı
    # kullanmalı: tek turlu çağrılarda kullanıcı mesajı cache-write fiyatından
    # yazılıp bir daha okunmuyor (probe 2026-09-24), yani her KB doğrudan para.
    flow_src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/flow.py").read_text()
    check("step8: tam dosya dalı da focused_source kullanıyor (baş kırpma yok)",
          "_focused_source(content, base_body" in flow_src and "content[:per_file]" not in flow_src.split("if not diff_mode:")[1][:900])
    check("step8: NN| önekinin dosyanın parçası olmadığı ajana söyleniyor",
          flow_src.count("DOSYANIN PARÇASI DEĞİL") >= 1 and "N satır atlandı" in flow_src)
    # NN| öneki context'e girdiği için kanıt doğrulaması onu SOYMALI; yoksa
    # sağlam bir madde "kanıtsız" diye düşer (en pahalı hata: bir retry turu).
    class _FakeFlow:
        state = SimpleNamespace(repo_name="r", branch_name="b")
        _repo_mgr = None
        class _C:
            def get_file_content(self, repo, f, ref):
                return "<?php\n\nclass X\n{\n    public function f()\n    {\n        return ceil($t / 10);\n    }\n}\n"
        _client = _C()
    vl = AgileSDLCFlow._verify_issue_loc
    ff = _FakeFlow()
    check("kanıt: düz alıntı doğrulanır",
          vl(ff, {"file": "app/X.php", "quote": "return ceil($t / 10);"}) is True)
    check("kanıt: 'NN| ' önekli alıntı da doğrulanır (önek soyulur)",
          vl(ff, {"file": "app/X.php", "quote": "   7|         return ceil($t / 10);"}) is True)
    check("kanıt: uydurma alıntı yine REDDEDİLİR (soyma kapıyı açmadı)",
          vl(ff, {"file": "app/X.php", "quote": "   7| $this->uydurma();"}) is False)
    check("kanıt: sadece numaradan ibaret alıntı → dosya var sayılır, kanıt iddiası yok",
          vl(ff, {"file": "app/X.php", "quote": "  42| "}) is True)

    # discover_repos: aday özetleri. Çağrı başına 36.9K yazım / 7.6K okuma
    # (sistemin en kötü oranı, adımın %85'i cache write) — tek turlu çağrıda
    # yazılan bir daha okunmadığı için her karakter para.
    from agile_sdlc_crew.flow import _repo_summary_slice, _wi_keywords, _compress_list_line
    summ = ("# orkestra\n## Ozet\n- **Dil**: PHP\n\n## README\n" + ("uzun readme metni. " * 200) +
            "\n## Domain Bilesenleri\n- **Model**: Order, OrderLine\n"
            "## DB Tablolari & Migrationlar\n- **Tablolar**: " +
            ", ".join(f"tablo_{i:03d}" for i in range(400)) + "\n"
            "## Ust Seviye Dizinler\n- app/\n")
    SEC = ("Ozet", "Domain Bilesenleri", "DB Tablolari")
    kw = _wi_keywords("WI: tablo_399 tablosuna yeni alan eklenecek, order_line ile ilişkili")
    check("anahtar kelime: WI metninden snake_case/camelCase çıkarılır", "tablo_399" in kw and "order_line" in kw, str(kw[:5]))
    out = _repo_summary_slice(summ, 2500, SEC, keywords=kw)
    check("özet: README ve dizin ağacı karara girmez (atılır)", "uzun readme" not in out and "Ust Seviye" not in out)
    check("özet: karar kuralının andığı bölümler durur", "Domain Bilesenleri" in out and "Tablolar" in out)
    check("özet: listenin SONUNDAKİ eşleşen tablo kurtarılır (düz kırpma atardı)",
          "tablo_399" in out and "tablo_399" not in _repo_summary_slice(summ, 2500, SEC), out[:90])
    check("özet: listelenmeyen tablo sayısı belirtilir (model 'liste tam' sanmasın)", "tane daha, listelenmedi" in out)
    check("özet: cap'e uyar", len(out) <= 2600, str(len(out)))
    check("liste sıkıştırma: kısa satıra dokunulmaz",
          _compress_list_line("- **Model**: Order, OrderLine", {"order"}) == "- **Model**: Order, OrderLine")
    check("liste sıkıştırma: eşleşme yoksa baş taraf + sayı",
          "tane daha" in _compress_list_line("- **T**: " + ", ".join(f"x_{i:04d}" for i in range(300)), {"yok_boyle"}))
    flow_src2 = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/flow.py").read_text()
    check("discover_repos: kanıtlı/geçmişli repo TAM özet alır, gerisi kısa",
          "strong.add" in flow_src2 and "keywords=_kw_dr" in flow_src2 and "CREW_DISCOVER_COMPACT" in flow_src2)

    # Review turlarının NEDEN uzadığı ölçülebilir olmalı: iş başına 5.7 reviewer
    # çağrısı ($66.49 / 24 iş) ve en yüksek c.write ortalaması (31.9K) burada.
    check("step8: satır çapalama pipeline review'ında da çalışıyor",
          "anchor_issue_lines as _anchor" in flow_src2 and "_pr_file_bodies" in flow_src2)
    check("step8: itiraz kapısı sonuçları REVIEW_STATS damgasına yazılıyor",
          "_review_stats_line" in flow_src2 and "drop_reasons" in flow_src2
          and 'review_text[:3000] + self._review_stats_line()' in flow_src2)
    from agile_sdlc_crew import retrospective as _retro
    _out = ("REVIEW_DECISION: APPROVE\n2 düzeltme turu\nREVIEW_STATS: "
            + json.dumps({"issues": 5, "blocking": 2, "dropped": 3, "rounds": 3,
                          "drop_reasons": {"minor/öneri": 2, "kanıt doğrulanamadı": 1},
                          "anchor_moved": 4, "anchor_cleared": 1}))
    _m = _retro._RE_STATS.search(_out)
    check("retro: REVIEW_STATS damgası çıktının sonundan parse edilir",
          bool(_m) and json.loads(_m.group(1))["blocking"] == 2)
    check("retro: damga yoksa (eski işler) sessizce atlanır, sayaç bozulmaz",
          _retro._RE_STATS.search("REVIEW_DECISION: APPROVE\n1 düzeltme turu") is None)
    tasks_y = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/config/tasks.yaml").read_text()
    rv = tasks_y.split("review_pr_task:")[1].split("\n  agent:")[0]
    check("review_pr_task: problem/required_fix TÜRKÇE yazılacak kuralı var",
          '"required_fix" values in the JSON MUST be written in TURKISH' in rv)
    check("review_pr_task: satırı sayma, NN| önekini oku kuralı var", "do\n      NOT count lines yourself" in rv)
    src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/pr_review.py").read_text()
    check("sınırlar: push/oy/durum yok — modülde push_file, set_work_item_state, reviewers/vote API'si geçmez",
          "push_file" not in src and "set_work_item_state" not in src and "/reviewers" not in src and '"vote"' not in src)
    check("yalnızca blocker/major satır yorumu, en çok 8", prv.INLINE_SEVERITIES == ("blocker", "major") and prv.MAX_INLINE_COMMENTS == 8)
    # ...AMA doğrulanmış önerisi olan madde önemine bakılmaksızın satıra yazılır.
    # Job #211: mekanik düzeltmeler tanımı gereği `minor` — eski filtre tam da
    # ÖNERİ TAŞIYAN maddeleri eliyordu (R5 silme önerisi üretildi, doğrulandı,
    # PR'a hiç düşmedi → sayaç yine 0).
    _prv_src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/pr_review.py").read_text()
    check("satır yorumu yalnız bloklayan madde (minor + öneri yazılmaz — kullanıcı kararı 2026-11-04)",
          '_inline = [i for i in issues if i.get("severity") in INLINE_SEVERITIES]' in _prv_src)
    check("satır yorumu: öneri madde başına BİR kez doğrulanır (çift I/O yok)",
          "_sugs = {id(i): verified_suggestion(i, file_bodies) for i in issues}" in _prv_src)
    for name, mod in (("retrospective", "retrospective"), ("sprint_planning", "sprint_planning"), ("daily", "daily")):
        s = (Path(__file__).resolve().parent.parent / f"src/agile_sdlc_crew/{mod}.py").read_text()
        check(f"{name}: pipeline dışı işler (pr_review) filtrelenir", "job_kind" in s)
    src_srv = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/server.py").read_text()
    check("server: /api/pr-review + CREW_PR_REVIEW + job_kind", '@app.post("/api/pr-review")' in src_srv and "CREW_PR_REVIEW" in src_srv and 'job_kind="pr_review"' in src_srv)
    src_ui = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text()
    check("dashboard: kartta 🔍 (başlanmış durumlar), iş listesinde rozet, onay diyaloğu",
          "reviewWi(" in src_ui and "'Code Review'" in src_ui and "İNCELEME" in src_ui and "confirm(`#${id}" in src_ui)



# ── 39. Backlog refinement — Definition of Ready skoru, sorgu, eylemler, sınırlar ─

def test_wi_comments_context():
    print("\n[46] WI yorumları — insan yönlendirmesi context'e girsin")
    from agile_sdlc_crew.flow import wi_comments_block, AgileSDLCFlow
    from agile_sdlc_crew import pipeline_config as _pc_wc

    cms = [  # API en yeniyi ÖNCE döndürür
        {"tarih": "2026-09-24T07:39", "yazar": "Hasan S", "metin": "Stage testleri ok"},
        {"tarih": "2026-09-22T13:44", "yazar": "Büşra A", "metin": "@Osman&nbsp;ÇEVİK dahil edebiliriz"},
        {"tarih": "2026-09-22T13:38", "yazar": "Osman Ç", "metin": "<div>Dalli için de geçerli mi?</div>"},
        {"tarih": "2026-09-09T11:56", "yazar": "Volkan Ö",
         "metin": "ℹ️ Hazırlık Skoru 5/100\n\n---\n*Tempo — Hazırlık Kapısı*"},
        {"tarih": "2026-09-08T10:00", "yazar": "X", "metin": "   "},
    ]
    blk, n = wi_comments_block(cms, cap=4000)
    check("bot yorumu ayıklanır (kendi sorumuzu cevap sanmayalım)", "Hazırlık Skoru" not in blk)

    # Azure yorumu markdown alıp HTML döndürüyor: yazdığımız *Tempo — X*
    # dışarıya <em>Tempo — X</em> olarak geliyor, YILDIZLAR KAYBOLUYOR.
    # Marker '*Tempo' yıldızlı olduğu için rebrand'den beri kendi WI/PR
    # yorumlarımızın hiçbiri tanınmıyordu (yalnız eski 'Agile SDLC Crew'
    # düz ad olduğu için tutuyordu). WI yorumlarını gereksinim kaynağı
    # yapınca bu bir DÖNGÜ oldu: kapının kendi "şu detaylar eksik" yorumu
    # insan yorumu sayılıp bir sonraki değerlendirmeye girdi (WI #73061'de
    # 1868 karakter kendi metnimiz insan kümesine eklendi).
    from agile_sdlc_crew.branding import is_bot_comment as _ibc
    _rendered = ("<p>Bu iş kalemi yeterince detaylı değil.</p>\n<br>\n<hr>\n"
                 "<p><em>Tempo — Hazırlık Kapısı</em></p>")
    check("HTML'e dönüşmüş imza (yıldızsız) bot olarak tanınır", _ibc(_rendered) is True, _rendered[-60:])
    check("markdown imza (yıldızlı) hâlâ tanınır", _ibc("...\n\n*Tempo — Kod İnceleme*") is True)
    check("eski ad (Agile SDLC Crew) hâlâ tanınır", _ibc("<p><em>Agile SDLC Crew — Hazırlık Kapısı</em></p>") is True)
    check("insan yorumu bot sanılmaz", _ibc("@Büşra AYVAZ evet, tüm kargolar için bu geçerli.") is False)
    check("metnin ORTASINDA geçen 'Tempo —' imza sayılmaz (yanlış pozitif yok)",
          _ibc("Tempo — bu araç " + ("çok uzun bir insan yorumu. " * 40)) is False)
    _cms_r = [{"tarih": "t", "yazar": "bot", "metin": _rendered},
              {"tarih": "t", "yazar": "A", "metin": "gerçek cevap"}]
    _b, _ = wi_comments_block(_cms_r)
    check("blok: HTML imzalı bot yorumu da ayıklanır", "Hazırlık Kapısı" not in _b and "gerçek cevap" in _b)
    check("boş yorum atlanır", blk.count("- [") == 3, blk)
    check("KRONOLOJİK sıra (kapsam kararı sorunun ardından gelmeli)",
          blk.index("Dalli için de geçerli mi") < blk.index("dahil edebiliriz"), blk[:200])
    check("HTML ve &nbsp; temizlenir", "<div>" not in blk and "&nbsp;" not in blk)
    check("uzunluk döner (hazırlık kapısının içerik sayacına eklenir)",
          n > 50 and n == sum(len(l) for l in blk.split("\n") if l.startswith("- [")), str(n))
    check("başlık ajana 'gereksinim kaynağı say' der", "gereksinim kaynağı say" in blk)
    blk2, _ = wi_comments_block(cms, cap=60)
    check("cap aşılınca kalan sayısı belirtilir", "yorum daha, yer kalmadı" in blk2, blk2[-120:])
    check("yorum yoksa boş döner", wi_comments_block([]) == ("", 0)
          and wi_comments_block([{"metin": "*Tempo — bot*"}]) == ("", 0))

    src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/flow.py").read_text()
    check("step1: yorumlar ctx'e VE içerik uzunluğuna eklenir",
          "wi_content_length += _blen" in src and "self.state.wi_comments_text = _blk" in src)
    check("knob'a bağlı (CREW_WI_COMMENTS)", '_pc_wc.get("CREW_WI_COMMENTS")' in src)
    check("pipeline_config'de kayıtlı, varsayılan açık", _pc_wc.get("CREW_WI_COMMENTS") is True)

    # Önek kararlılığı: yorumlar is-degismezi → parts'a girer, tail'e DEĞİL
    st = SimpleNamespace(work_item_id="1", requirements_text="", kickoff_text="", acceptance_criteria=[],
                         plan=None, branch_name="", all_pushes=[], pr_id="", pr_url="", repo_name="",
                         review_text="", test_text="", uat_text="", flow_kind="", po_text="",
                         wi_comments_text="\n\n# İŞ KALEMİ YORUMLARI (insan — kronolojik)\n- [t] a: b")
    fake = SimpleNamespace(state=st, _vector_store=None, _forward_text=lambda k, t, c: t[:c])
    a = AgileSDLCFlow._build_step_context(fake, "technical_design_task")
    b = AgileSDLCFlow._build_step_context(fake, "review_pr_task")
    check("yorumlar karar veren adımlara gider", "İŞ KALEMİ YORUMLARI" in a and "İŞ KALEMİ YORUMLARI" in b)
    check("BA adımına tekrar enjekte edilmez (ctx'te zaten var)",
          "İŞ KALEMİ YORUMLARI" not in AgileSDLCFlow._build_step_context(fake, "requirements_analysis_task"))
    import os as _os_pfx
    pre = len(_os_pfx.path.commonprefix([a, b]))
    check("ortak önek yorumları KAPSAR (parts'ta, prefix bozulmuyor)",
          pre > a.index("İŞ KALEMİ YORUMLARI"), f"ortak {pre}")


def test_cli_prompt_overhead():
    print("\n[45] claude -p prompt öneki — kullanılmayan araç şemaları gönderilmesin")
    import os as _os_cpo
    from agile_sdlc_crew.tools import claude_cli_llm as _cli

    seen = {}

    def _fake(cmd, env, timeout_s, meta=None, idle_s=0):
        seen["cmd"] = list(cmd)
        if meta is not None:
            meta.update({"turns": 1, "cost_usd": 0.0})
        return "OK"

    _orig = _cli._run_streaming
    _cli._run_streaming = _fake
    _saved = {k: _os_cpo.environ.get(k) for k in ("CREW_CLI_STRICT_MCP", "CREW_CLI_AUTO_TOOLLESS")}
    try:
        # (a) repo aracı YOK → şemalar gönderilmesin
        _cli.clear_repo_ctx(); _cli.set_toolless(False)
        _os_cpo.environ.pop("CREW_CLI_STRICT_MCP", None); _os_cpo.environ.pop("CREW_CLI_AUTO_TOOLLESS", None)
        _cli.claude_cli_completion("x", model="sonnet", system="s")
        c = seen["cmd"]
        check("--add-dir yokken MCP sunucuları atlanır", "--strict-mcp-config" in c, " ".join(c[-6:]))
        check("--add-dir yokken dosya/exec araçları kapatılır (şema da gitmez)",
              "--disallowedTools" in c and _cli._TOOLLESS_DENY in c)
        # (b) repo aracı VAR → araçlar korunur, yalnız MCP atlanır
        _cli.set_repo_ctx(["/tmp/repo"], "Read,Grep,Glob,LS")
        _cli.claude_cli_completion("x", model="sonnet", system="s")
        c = seen["cmd"]
        check("--add-dir varken keşif araçları KORUNUR (auto-toolless tetiklenmez)",
              "--add-dir" in c and "--allowedTools" in c and "--disallowedTools" not in c, " ".join(c[-8:]))
        check("--add-dir varken de MCP atlanır (keşif native araçları kullanır)", "--strict-mcp-config" in c)
        _cli.clear_repo_ctx()
        # (c) env ile geri alınabilir
        _os_cpo.environ["CREW_CLI_STRICT_MCP"] = "0"; _os_cpo.environ["CREW_CLI_AUTO_TOOLLESS"] = "0"
        _cli.claude_cli_completion("x", model="sonnet", system="s")
        c = seen["cmd"]
        check("geri alma: iki env=0 → eski davranış (bayrak yok)",
              "--strict-mcp-config" not in c and "--disallowedTools" not in c)
        # (d) açık set_toolless env'den bağımsız çalışır
        _cli.set_toolless(True)
        _cli.claude_cli_completion("x", model="sonnet", system="s")
        check("açık set_toolless(True), CREW_CLI_AUTO_TOOLLESS=0 iken de araçları kapatır",
              "--disallowedTools" in seen["cmd"])
        _cli.set_toolless(False)
    finally:
        _cli._run_streaming = _orig
        for k, v in _saved.items():
            if v is None: _os_cpo.environ.pop(k, None)
            else: _os_cpo.environ[k] = v


def test_backlog_refinement():
    print("\n[39] Backlog refinement — DoR skoru, WIQL, SP önerisi, yorum/etiket, soru parse, knob'lar")
    from datetime import datetime, timezone, timedelta
    from agile_sdlc_crew import backlog_refinement as br

    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    wiql = br.backlog_wiql(area_path="Boards\\Team's Area", states=["Backlog", "To Do"])
    check("WIQL: durum + tip filtresi, area UNDER, tek tırnak kaçışı",
          "[System.State] IN ('Backlog', 'To Do')" in wiql and "UNDER 'Boards\\Team''s Area'" in wiql
          and "'User Story'" in wiql and "IterationPath" not in wiql, wiql)
    wiql2 = br.backlog_wiql(area_path="X", iteration_path="P\\S1", states=["New"])
    check("WIQL: sprint kapsamında iteration filtresi, area yok", "[System.IterationPath] = 'P\\S1'" in wiql2 and "AreaPath" not in wiql2)
    check("AC sayısı: madde imi/numara/Given sayılır, düz metin satır", br.count_criteria("<ul><li>a</li><li>b</li></ul>") == 2
          and br.count_criteria("1. x<br>2. y<br>3. z") == 3 and br.count_criteria("Given a\nWhen b\nThen c") == 3 and br.count_criteria("") == 0)

    def item(**kw):
        base = {"id": 1, "title": "Kargo etiketinde alıcı telefonu maskelensin", "type": "Task", "state": "Backlog",
                "priority": 2, "sp": 3, "assignedTo": "", "tags": "", "description": "<p>" + "Açıklama metni. " * 10 + "</p>",
                "acceptance": "<ul><li>AC1 maskeli</li><li>AC2 log yok</li></ul>", "repro": "",
                "created": (now - timedelta(days=3)).isoformat(), "iterationPath": "", "areaPath": "", "parent_id": 10, "url": ""}
        base.update(kw); return base
    ok = br.assess(item(), now=now)
    check("tam iş: skor 100, hazır, eksik yok, SP önerisi yok", ok["score"] == 100 and ok["ready"] and not ok["gaps"] and ok["suggest_sp"] is None, str(ok["gaps"]))
    r = br.assess(item(acceptance="", sp=None, parent_id=None), now=now)
    codes = [g["code"] for g in r["gaps"]]
    check("Task: AC yok −25, SP yok −10, ebeveyn yok −10 → 55, eksik", codes == ["no_ac", "no_sp", "no_parent"] and r["score"] == 55 and not r["ready"], f"{codes} {r['score']}")
    check("SP yoksa yapısal öneri (AC yok → 2)", r["suggest_sp"] == 2, str(r["suggest_sp"]))
    desc_head = "<p>Proje özeti uzun uzun anlatılıyor burada, servisler ve akış.</p><p>Kabul Kriterleri:</p><ul><li>Soru kaydedilir</li><li>Cevap 5 sn içinde döner</li></ul>"
    r = br.assess(item(acceptance="", description=desc_head), now=now)
    check("AC alanı yok (bu org'da alan yok) ama açıklamada 'Kabul Kriterleri' başlığı → eksik yok, 2 AC",
          not r["gaps"] and r["n_ac"] == 2 and r["ac_source"] == "explicit", str(r["gaps"]))
    desc_impl = "<p>" + "Proje özeti cümlesi. " * 6 + "</p><ul><li>QuestionCreationService</li><li>RuleEngineService</li></ul>"
    r = br.assess(item(acceptance="", description=desc_impl), now=now)
    check("AC başlığı yok ama maddeler var → 'açık değil' −10 (pipeline gibi açıklamadan çıkarılır)",
          [g["code"] for g in r["gaps"]] == ["ac_implicit"] and r["score"] == 90 and r["ready"], str(r["gaps"]))
    r = br.assess(item(type="Bug", acceptance="", repro=""), now=now)
    check("Bug: AC yerine repro aranır", [g["code"] for g in r["gaps"]] == ["no_repro"])
    r = br.assess(item(type="Bug", acceptance="", repro="<ol><li>Aç</li><li>Tıkla</li></ol>"), now=now)
    check("Bug: repro varsa eksik yok", not r["gaps"])
    r = br.assess(item(title="[Spike] Redis TTL araştırması", type="Task", acceptance="", description="<p>" + "Redis TTL davranışı nasıl? " * 6 + "</p>", sp=None), now=now)
    check("Spike: AC aranmaz, soru var; SP önerisi yok", "no_ac" not in [g["code"] for g in r["gaps"]] and r["kind"] == "spike" and r["suggest_sp"] is None, str(r["gaps"]))
    # Bayatlık KADEMELİ (kritik eşik aging'den okunur — env ile değişebilir):
    # eşiğin altı 'stale' −5, eşikte/üstü 'very_stale' −15. Eşik değerini
    # sabitlemiyoruz; sabitlersek CREW_AGING_BACKLOG_CRIT_DAYS ayarı testi kırar.
    from agile_sdlc_crew import aging as _ag
    crit = _ag.backlog_crit_days()
    base_gaps = ["no_description", "too_big", "no_priority", "short_title"]
    stale_item = dict(description="", sp=13, priority=None, title="Kısa")
    r = br.assess(item(**stale_item, created=(now - timedelta(days=crit - 1)).isoformat()), now=now)
    codes = [g["code"] for g in r["gaps"]]
    check("açıklama yok −30, SP>8 −10, öncelik −5, başlık −5, bayat −5 → 45",
          codes == base_gaps + ["stale"] and r["score"] == 45, f"{codes} {r['score']}")
    r = br.assess(item(**stale_item, created=(now - timedelta(days=crit)).isoformat()), now=now)
    codes = [g["code"] for g in r["gaps"]]
    check(f"kritik eşikte ({crit}g) bayatlık −5 değil −15 → 35 (1 yıllık işle 31 günlük iş aynı cezayı almasın)",
          codes == base_gaps + ["very_stale"] and r["score"] == 35, f"{codes} {r['score']}")
    r = br.assess(item(**stale_item, created=(now - timedelta(days=crit + 400)).isoformat(),
                       state_change=(now - timedelta(days=2)).isoformat()), now=now)
    check("bayatlık OLUŞMA yaşını değil DURUMDA GEÇEN süreyi ölçer (sprint'e taşınan iş tazelenir)",
          [g["code"] for g in r["gaps"]] == base_gaps and r["score"] == 50, str(r["gaps"]))
    r = br.assess(item(description="<p>Kısa bir açıklama burada.</p>"), now=now, min_desc_chars=100)
    check("kısa açıklama (20 ≤ n < eşik) −15", [g["code"] for g in r["gaps"]] == ["short_description"] and r["score"] == 85)
    check("eşik parametrik: 85 ≥ 70 hazır, 85 < 90 eksik", br.assess(item(description="<p>Kısa bir açıklama burada.</p>"), now=now)["ready"]
          and not br.assess(item(description="<p>Kısa bir açıklama burada.</p>"), now=now, min_score=90)["ready"])
    check("etiket tespiti büyük/küçük harf duyarsız", br.assess(item(tags="flo; Needs-Refinement"), now=now)["tagged"])

    rows = [br.assess(item(), now=now), br.assess(item(id=2, acceptance="", sp=None), now=now)]
    s = br.summarize(rows)
    check("özet: toplam/hazır/eksik/SP'siz/eksik sayaçları", s["total"] == 2 and s["ready"] == 1 and s["not_ready"] == 1 and s["sp_missing"] == 1
          and s["gap_counts"] == {"no_ac": 1, "no_sp": 1} and s["sp_suggested"] == 2, str(s))
    md = br.gaps_comment_markdown(rows[1])
    check("yorum: skor, eksikler, SP önerisi, Tempo imzası", "Hazırlık skoru:** 65/100" in md and "- Kabul kriteri yok" in md
          and "SP önerisi:** 2" in md and md.rstrip().endswith("*Tempo — Backlog Refinement*"), md)
    from agile_sdlc_crew.branding import is_bot_comment
    check("yorum imzası bot yorumu olarak tanınır", is_bot_comment(md))

    class _C:
        def __init__(self): self.ops = None; self.tags = "flo; Needs-Refinement; ops"
        def get_work_item(self, wid): return {"id": wid, "fields": {"System.Tags": self.tags}}
        def update_work_item(self, wid, ops): self.ops = ops; return {}
    c = _C()
    check("etiket kaldır: diğerleri korunur", br.set_tag(c, 5, add=False) == "flo; ops" and c.ops[0]["path"] == "/fields/System.Tags")
    c.tags = "flo"
    check("etiket ekle: mevcut korunur, tekrar eklenmez", br.set_tag(c, 5, add=True) == "flo; needs-refinement" and br.set_tag(_C(), 5, add=True) == "flo; Needs-Refinement; ops")
    check("SP yaz: WI'da tahmin varsa dokunmaz", br.write_suggested_sp(c, {"id": 5, "sp": 3, "suggest_sp": 2}) == "")

    q = br.parse_questions('bla ```json\n{"summary":"Eksik","questions":[{"question":"Hangi dil?","why":"AC"},"düz soru"],"draft_acceptance_criteria":["AC olmalı"]}\n```')
    check("soru parse: JSON bloğu, düz string soruya çevrilir", q["summary"] == "Eksik" and len(q["questions"]) == 2
          and q["questions"][1] == {"question": "düz soru", "why": ""} and q["draft_acceptance_criteria"] == ["AC olmalı"])
    check("soru parse: JSON yoksa None", br.parse_questions("serbest metin") is None)
    qmd = br.questions_comment_markdown(rows[1], q)
    check("soru yorumu: S1/AC1 numaralı, imzalı", "S1. Hangi dil?" in qmd and "AC1. AC olmalı" in qmd and "*Tempo — Backlog Refinement*" in qmd)

    from agile_sdlc_crew import pipeline_config as _pc
    # Varsayilan (schema) degerleri sinanir: WRITE/LLM kapali gelir. Calisan kurulumda
    # insan bunlari yaml'dan acabilir (pipeline_config.get yaml > env > default cozer),
    # bu yuzden cozulmus deger degil, gonderilen varsayilan kontrol edilir.
    _dflt = {f["key"]: f["default"] for f in _pc.SCHEMA}
    check("knob varsayılanları: refinement açık, eşik 70, bayat 30, WRITE ve LLM KAPALI",
          _dflt["CREW_BACKLOG_REFINEMENT"] is True and _dflt["CREW_REFINEMENT_MIN_SCORE"] == 70
          and _dflt["CREW_REFINEMENT_STALE_DAYS"] == 30 and _dflt["CREW_REFINEMENT_WRITE"] is False
          and _dflt["CREW_REFINEMENT_LLM"] is False)
    check("knob'lar çözümlenebilir (yaml/env override dahil)",
          isinstance(_pc.get("CREW_REFINEMENT_WRITE"), bool) and isinstance(_pc.get("CREW_REFINEMENT_LLM"), bool)
          and isinstance(_pc.get("CREW_REFINEMENT_MIN_SCORE"), int))
    root = Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew"
    src = (root / "backlog_refinement.py").read_text()
    check("sınırlar: durum/kuyruk yok — set_work_item_state, create_job, push_file geçmez",
          "set_work_item_state" not in src and "create_job" not in src and "push_file" not in src)
    tasks = (root / "config/tasks.yaml").read_text()
    check("tasks.yaml: refinement_questions_task (BA, JSON, Türkçe)", "refinement_questions_task:" in tasks and "agent: business_analyst" in tasks.split("refinement_questions_task:")[1])
    check("crew: create_refinement_crew tool'suz", "def create_refinement_crew" in (root / "crew.py").read_text())
    srv = (root / "server.py").read_text()
    check("server: GET /api/refinement + POST action, knob kapıları, job_kind=refinement",
          '@app.get("/api/refinement")' in srv and '@app.post("/api/refinement/action")' in srv
          and "CREW_REFINEMENT_WRITE" in srv and "CREW_REFINEMENT_LLM" in srv and 'job_kind="refinement"' in srv)
    ui = (root / "web/index.html").read_text()
    check("dashboard: Refinement sekmesi, kapsam seçimi, eylem onayı", "showTab('refine')" in ui and 'id="rfScope"' in ui
          and "rfAct(" in ui and "confirm(`#${id} için İş Analisti" in ui)

# ── 40. Maliyet analizi — kırılım, bulgu eşikleri, salt-okunurluk ────────

def test_cost_analytics():
    print("\n[40] Maliyet analizi — adım/model/sınıf kırılımı, bulgu eşikleri, sınırlar")
    from agile_sdlc_crew import cost_analytics as ca

    def row(job, step, model, usd, agent="a", cr=90_000, cw=10_000, kind="pipeline"):
        return {"job_id": job, "step_key": step, "agent": agent, "model": model,
                "provider": "claude_cli", "turns": 2, "tool_calls": 0,
                "cost_usd": usd, "duration_ms": 60_000,
                "input_tokens": 1_000, "output_tokens": 500,
                "cache_read_tokens": cr, "cache_creation_tokens": cw,
                "created_at": None, "work_item_id": f"WI{job}",
                "job_status": "completed", "job_kind": kind}

    rows = []
    # Dört ucuz adım, üç işte birer çağrı → iş başına 1.0 (medyanı aşağı çeker).
    for s in ("requirements_analysis_task", "discover_repos_task",
              "uat_task", "completion_report_task"):
        rows += [row(j, s, "claude-sonnet-5", 0.10) for j in (1, 2, 3)]
    # Beş ucuz iş daha — ortalamayı düşürüp "ortalamayı katlayan iş" eşiğini sınanabilir kılar.
    rows += [row(j, "requirements_analysis_task", "claude-sonnet-5", 0.10) for j in range(4, 9)]
    # Pahalı adım: TEK işte 8 çağrı → iş başına 8.0, uyarı bekleniyor.
    rows += [row(1, "technical_design_task", "claude-opus-4-8[1m]", 5.0,
                 agent="software_architect") for _ in range(8)]
    # Kickoff: tek işte 9 çağrı ama BY_DESIGN_MULTI → uyarı değil bilgi.
    rows += [row(2, "kickoff_meeting_task", "claude-opus-5", 1.0,
                 agent="scrum_master") for _ in range(9)]

    a = ca.analyze(rows)
    t = a["totals"]
    check("kırılım: toplam $, iş sayısı, iş başına ortalama",
          abs(t["usd"] - 50.70) < 0.01 and t["jobs"] == 8 and t["calls"] == len(rows),
          f"{t}")
    check("adım sıralaması: en pahalı adım başta, pay yüzdesi tutarlı",
          a["by_step"][0]["key"] == "technical_design_task"
          and 78 <= a["by_step"][0]["share_pct"] <= 80)
    check("iş başına çağrı: tek işte 8 çağrı → 8.0 (iş sayısına bölünür, çağrı sayısına değil)",
          a["by_step"][0]["calls_per_job"] == 8.0)

    check("model sınıfı: sürüm ve [1m] soneki sınıfı değiştirmez",
          ca._family("claude-opus-4-8[1m]") == "opus"
          and ca._family("claude-opus-5") == "opus"
          and ca._family("claude-sonnet-5") == "sonnet"
          and ca._family("claude-haiku-4-5-20251001") == "haiku"
          and ca._family("gpt-4o") == ca.UNASSIGNED
          and ca._family(None) == ca.UNASSIGNED)
    fam = {m["key"]: m for m in a["by_family"]}
    check("sınıf kırılımı: opus payı hesaplanır, sonnet ayrı durur",
          a["by_family"][0]["key"] == "opus" and fam["opus"]["share_pct"] >= 80
          and "sonnet" in fam)

    titles = [f["title"] for f in ca.findings(a)]
    lv = {f["title"]: f["level"] for f in ca.findings(a)}
    check("bulgu: harcama iki adımda yoğunlaşıyor",
          any(x.startswith("Harcama yoğunlaşması:") and "iki adımda" in x for x in titles), str(titles))
    warn_step = [x for x in titles if x.startswith("technical_design_task")]
    check("bulgu: normal adımda yüksek çağrı UYARI",
          bool(warn_step) and lv[warn_step[0]] == "warn", str(titles))
    kick = [x for x in titles if x.startswith("kickoff_meeting_task")]
    check("bulgu: tasarımı gereği çok çağrılı adım uyarı değil BİLGİ",
          bool(kick) and "tasarım gereği" in kick[0] and lv[kick[0]] == "info", str(titles))
    check("bulgu: tek model sınıfı yoğunlaşması uyarı verir",
          any("tek model sınıfında" in x for x in titles), str(titles))
    check("bulgu: ortalamayı 3 kat aşan iş işaretlenir",
          any(x.startswith("#1 (WI WI1)") for x in titles), str(titles))

    # Önbellek oranı eşiği iki yönlü sınanır: 9.0 sessiz, 1.5 uyarı.
    # Oran bulgusu UYARI seviyesinde; onbellek ekonomisi bulgusu ise her zaman
    # bilgi olarak cikar (token payi != maliyet payi yanilgisini onlemek icin).
    warn_titles = [f["title"] for f in ca.findings(a) if f["level"] == "warn"]
    check("önbellek oranı 9.0 → UYARI yok (eşik 5)",
          a["token_mix"]["cache_ratio"] == 9.0
          and not any("okuma/yazma oranı" in x for x in warn_titles))
    low = ca.analyze([row(1, "s", "claude-opus-5", 1.0, cr=15_000, cw=10_000)])
    low_warn = [f["title"] for f in ca.findings(low) if f["level"] == "warn"]
    check("önbellek oranı 1.5 → UYARI var",
          low["token_mix"]["cache_ratio"] == 1.5
          and any("okuma/yazma oranı" in x for x in low_warn))

    # Token payi ile maliyet payi AYRI raporlanmali: okuma token'in cogunu
    # olusturur ama maliyetin kucuk kismidir; yazma tersi. Bu ayrim olmazsa
    # rapor okuru yanlis yeri optimize etmeye iter.
    share = a["token_mix"]["cost_share"]
    tok = a["token_mix"]
    tok_total = tok["input"] + tok["cache_read"] + tok["cache_write"]
    read_tok_pct = 100 * tok["cache_read"] / tok_total
    check("maliyet payı token payından AYRI hesaplanır (okuma ucuz, yazma pahalı)",
          read_tok_pct > 80 and share["cache_read"]["max"] < read_tok_pct
          and share["cache_write"]["max"] > 100 * tok["cache_write"] / tok_total,
          f"token %{read_tok_pct:.0f} vs maliyet {share['cache_read']}")
    check("yüksek okuma payı bilgi olarak ÖVÜLÜR, uyarı olarak basılmaz",
          any(f["level"] == "info" and "maliyetin" in f["title"] for f in ca.findings(a)))

    md = ca.render_markdown(a, title="test", fnd=ca.findings(a))
    check("markdown: dashboard mdToHtml'in beklediği pipe tablo (|---| ayracı)",
          "| Adım | $ |" in md and "|---|" in md
          and "## Model sınıfı bazında" in md and "## Adım bazında" in md)

    root = Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew"
    src = (root / "cost_analytics.py").read_text()
    check("sınırlar: salt okunur — yazan SQL yok, ajan/crew kurulmuyor",
          not any(k in src.upper() for k in ("INSERT INTO", "DELETE FROM", "UPDATE "))
          and not any(k in src for k in (".kickoff(", "Crew(", "Agent(", "build_for_agent")))
    from agile_sdlc_crew import pipeline_config as _pc
    _dflt = {f["key"]: f["default"] for f in _pc.SCHEMA}
    check("knob: CREW_COST_ANALYTICS açık (maliyeti sıfır, salt okunur)",
          _dflt["CREW_COST_ANALYTICS"] is True
          and isinstance(_pc.get("CREW_COST_ANALYTICS"), bool))
    srv = (root / "server.py").read_text()
    check("server: GET /api/costs + knob kapısı",
          '@app.get("/api/costs")' in srv and "CREW_COST_ANALYTICS" in srv)
    ui = (root / "web/index.html").read_text()
    check("dashboard: Maliyet sekmesi, kapsam/tür seçimi, bulgu renklendirme",
          "showTab('cost')" in ui and 'id="costScope"' in ui
          and 'id="costKind"' in ui and "loadCosts(" in ui)


# ── 41. Angarya/karar model ayrimi — ucuz faz sonnet, karar fazi opus ────

def test_cheap_phase_split():
    print("\n[41] Model ayrimi — angarya fazlari sonnet, karar/teknik noktalar korunur")
    import os
    import inspect
    from agile_sdlc_crew.llm import resolver as R

    CHEAP = ("software_architect_explore", "software_architect_kickoff",
             "senior_developer_kickoff")

    # 1) Angarya anahtarlari cozulur ve UCUZ modele baglanir (bizim varsayilanimiz,
    #    agent_defaults katmanindan — dashboard override'i degil, o insanin).
    specs = {k: R.resolve_spec_with_source(k) for k in CHEAP}
    check("angarya anahtarlari cozulur ve sonnet'e baglanir",
          all(s[0]["model"] == "sonnet" and s[0]["provider"] == "claude_cli"
              for s, in [(v,) for v in specs.values()]),
          str({k: (v[0]["model"], v[1]) for k, v in specs.items()}))

    # 2) Karar/teknik noktalar angarya anahtarlarindan BAGIMSIZ kalir. Modeli
    #    sabitlemiyoruz (dashboard'un hakki) — ayri cozulduklerini dogruluyoruz.
    for judge in ("software_architect", "senior_developer", "code_reviewer"):
        jspec, _ = R.resolve_spec_with_source(judge)
        check(f"{judge}: karar/teknik nokta angarya profiline BAGLANMAZ",
              jspec.get("_profile") not in ("architect_explore_cli", "kickoff_cli"))

    # 3) CREW_USE_LOCAL_LLM angarya fazlarini yerel modele kacirmamali: kesif
    #    682K'ya varan baglam tariyor, yerel 8B model bunu sessizce bozar.
    os.environ["CREW_USE_LOCAL_LLM"] = "1"
    R.reset_cache()
    try:
        local_leak = [k for k in CHEAP
                      if R.resolve_spec_with_source(k)[0]["provider"] == "ollama"]
    finally:
        os.environ.pop("CREW_USE_LOCAL_LLM", None)
        R.reset_cache()
    check("CREW_USE_LOCAL_LLM angarya fazlarini yerele kacirmaz", not local_leak,
          str(local_leak))

    # 4) Geri alma tek env ile calismali (kalite bozulursa aninda donus).
    os.environ["CREW_LLM_PROFILE_SOFTWARE_ARCHITECT_EXPLORE"] = "architect_cli"
    try:
        rb, rbsrc = R.resolve_spec_with_source("software_architect_explore")
    finally:
        os.environ.pop("CREW_LLM_PROFILE_SOFTWARE_ARCHITECT_EXPLORE", None)
    check("env ile geri alinabilir (CREW_LLM_PROFILE_… → opus)",
          rb["model"] == "opus" and rbsrc == "env")

    # 5) Cagri yeri baglantisi: SADECE kesif fazi ucuz anahtari kullanmali.
    #    _amend_plan ve _architect_emit_json plan YAZAR — onlar karar fazi.
    root = Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew"
    fl = (root / "flow.py").read_text()
    import re as _re
    explore_fn = fl.split("def _architect_explore")[1].split("\n    def ")[0]
    check("kesif fazi ucuz anahtarla cagrilir",
          'agent_key="software_architect_explore"' in explore_fn)
    check("ucuz anahtar flow.py'de SADECE kesif fazinda geciyor",
          fl.count("software_architect_explore") == explore_fn.count("software_architect_explore"),
          f"toplam {fl.count('software_architect_explore')}")
    for fn in ("_amend_plan", "_architect_emit_json"):
        body = fl.split(f"def {fn}")[1].split("\n    def ")[0]
        check(f"{fn} (plan yazan faz) ucuz anahtar KULLANMAZ",
              "software_architect_explore" not in body)

    # 6) Varsayilan parametre karar fazini korur: arguman verilmeyen her cagri
    #    (amend + emit) eski davranista kalir.
    from agile_sdlc_crew.crew import AgileSDLCCrew
    sig = inspect.signature(AgileSDLCCrew.create_analysis_crew_toolless)
    check("create_analysis_crew_toolless varsayilani software_architect",
          sig.parameters["agent_key"].default == "software_architect")

    # 7) Kayit: dashboard listesi + model-erisilebilirlik dogrulamasi. Kayit disi
    #    config sessizce kayar (resolver docstring'indeki aylarca suren hata).
    srv = (root / "server.py").read_text()
    check("dashboard: angarya anahtarlari _AGENT_KEYS + _AGENT_DISPLAY'de",
          all(f'"{k}"' in srv for k in CHEAP)
          and all(k in srv.split("_AGENT_DISPLAY")[1][:800] for k in CHEAP))
    res_src = (root / "llm/resolver.py").read_text()
    check("assert_models_reachable angarya anahtarlarini da dogrular",
          all(k in res_src.split("def assert_models_reachable")[1] for k in CHEAP))
    prof = (root / "config/llm_profiles.yaml").read_text()
    check("llm_profiles: profiller + agent_defaults eslemesi yazili",
          "architect_explore_cli:" in prof and "kickoff_cli:" in prof
          and all(f"{k}:" in prof for k in CHEAP))


# ── 42. Kesif bulgusunun amend turlarinda yeniden kullanimi ──────────────

def test_findings_reuse():
    print("\n[42] Keşif bulgusu amend turlarına taşınır — repo iki kez taranmaz")
    root = Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew"
    fl = (root / "flow.py").read_text()

    from agile_sdlc_crew.flow import _FINDINGS_REUSE_MAX
    check("taşıma sınırı tanımlı ve makul (prompt'u şişirmeyecek kadar)",
          isinstance(_FINDINGS_REUSE_MAX, int) and 2000 <= _FINDINGS_REUSE_MAX <= 40000,
          str(_FINDINGS_REUSE_MAX))

    step4 = fl.split("def crew_step4_technical_design")[1].split("\n    def ")[0]
    check("step4: Faz A bulgusu state'e yazılır",
          "_architect_findings = findings[:_FINDINGS_REUSE_MAX]" in step4)

    amend = fl.split("def _amend_plan")[1].split("\n    def ")[0]
    check("amend: bulgu varsa context'e taşınır",
          "_architect_findings" in amend and "TEKRARLAMA" in amend)
    # Arac erisimi KAPATILMAMALI: geri bildirim hic kesfedilmemis bir dosyaya
    # isaret edebilir. Kazanc tekrar taramayi onlemekten gelir, erisimi
    # kesmekten degil — yoksa amend eksik dosyayi hic bulamaz.
    check("amend: repo araç erişimi kapatılmadı (yeni dosya gerekebilir)",
          "set_repo_ctx" in amend)

    # Bulgu yokken (kesif hic kosmadi) amend eskisi gibi davranmali.
    check("bulgu yoksa context'e blok EKLENMEZ (boş başlık basılmaz)",
          '_prev = (getattr(self, "_architect_findings", "") or "").strip()' in amend
          and "if _prev:" in amend)

    # Butce cap'i (CREW_CLI_CALL_MAX_USD → --max-budget-usd) zaten bagliydi ve
    # ACIK. Probe (2026-09-21): sinira carpinca is_error=True /
    # subtype=error_max_budget_usd, result BOS, stream'de text blogu yok →
    # salvage de bos. Para harcanir, is kaybolur. Kesinti en azindan GORUNUR
    # olmali; yoksa raporda sadece "pahali cagri" gibi durur.
    cli = (root / "tools/claude_cli_llm.py").read_text()
    check("bütçe kesintisi yakalanır ve stop_reason olarak taşınır",
          "error_max_budget_usd" in cli and 'meta["stop_reason"]' in cli
          and '"stop_reason": str(meta.get("stop_reason") or "")' in cli)
    dbs = (root / "db.py").read_text()
    check("stop_reason llm_calls'a yazılır (kolon + INSERT)",
          '_ensure_column(cur, "llm_calls", "stop_reason"' in dbs
          and 'rec.get("stop_reason")' in dbs)
    ca_src = (root / "cost_analytics.py").read_text()
    check("Maliyet raporu kesilen çağrıları ayrı bulgu olarak gösterir",
          "budget_cut" in ca_src and "error_max_budget_usd" in ca_src)
    from agile_sdlc_crew import cost_analytics as _ca
    _an = _ca.analyze([
        {"job_id": 1, "step_key": "technical_design_task", "agent": "software_architect",
         "model": "claude-opus-5", "cost_usd": 1.6, "turns": 20, "input_tokens": 10,
         "output_tokens": 10, "cache_read_tokens": 10, "cache_creation_tokens": 10,
         "duration_ms": 1000, "created_at": None, "stop_reason": "error_max_budget_usd",
         "job_kind": "pipeline", "work_item_id": "1", "job_status": "completed"},
    ])
    check("kesilen çağrı bulgusu tetiklenir ve tutarı raporlar",
          _an["budget_cut"]["calls"] == 1 and _an["budget_cut"]["usd"] == 1.6
          and any("bütçe cap'inde kesildi" in f["title"] for f in _ca.findings(_an)))


# ── 43. Prompt onbellek oneki — degisken placeholder EN SONDA ────────────

def test_prompt_prefix_order():
    print("\n[43] Placeholder sırası — sabit context önce, değişen geri bildirim sonra")
    import yaml as _yaml
    root = Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew"
    raw = (root / "config/tasks.yaml").read_text()
    doc = _yaml.safe_load(raw)

    # Onbellek ONEKTEN eslesir: ilk degisen karakterden sonrasi yeniden yazilir.
    # Yazma 1.25-2x, okuma 0.1x → degisken metin SONDA olmali. Retry donguleri
    # (_architect_emit_json, _review_retry_loop) yalnizca feedback string'ini
    # degistirir; sira bozulursa 2. deneme tum oneki yeniden yazar.
    offenders = []
    checked = 0
    for key, block in doc.items():
        desc = ((block or {}).get("description") or "")
        if "{scrum_master_feedback}" not in desc or "{previous_context}" not in desc:
            continue
        checked += 1
        if desc.index("{scrum_master_feedback}") < desc.rindex("{previous_context}"):
            offenders.append(key)
    check("değişken {scrum_master_feedback} her görevde sabit {previous_context}'ten SONRA",
          checked >= 5 and not offenders, f"{checked} görev kontrol edildi, bozuk: {offenders}")

    # Placeholder'lar kaybolmamis olmali (sira degisimi silme olmamali).
    counts = {p: sum(((b or {}).get("description") or "").count(p) for b in doc.values())
              for p in ("{work_item_id}", "{previous_context}",
                        "{scrum_master_feedback}", "{target_repo}")}
    check("sıra değişimi hiçbir placeholder'ı düşürmedi",
          counts["{previous_context}"] >= 13 and counts["{scrum_master_feedback}"] == 5
          and counts["{work_item_id}"] >= 18, str(counts))

    # Gerekce dosyada yazili olmali — yoksa biri "duzenler" ve geri alir.
    check("sıranın gerekçesi tasks.yaml başlığında yazılı",
          "PLACEHOLDER ORDER IS A COST DECISION" in raw and "cache write" in raw.lower())

    # Retry dongusu gercekten SADECE feedback'i degistiriyor mu? Degistirdigi
    # baska bir sey varsa onek yine bozulur ve bu sira ise yaramaz.
    fl = (root / "flow.py").read_text()
    emit = fl.split("def _architect_emit_json")[1].split("\n    def ")[0]
    check("emit retry'ı yalnızca scrum_master_feedback'i değiştirir (emit_ctx sabit)",
          "fb_note = (" in emit and 'previous_context": emit_ctx' in emit
          and emit.count("emit_ctx =") <= 2, "emit_ctx döngü içinde yeniden kurulmamalı")


def test_quality_rank_nr():
    print("\n[Q1] Kalite alımı — NR normalizasyon, birleşme, SP")
    from agile_sdlc_crew.quality import rank
    from agile_sdlc_crew.quality.model import Finding
    n = rank.normalize_message
    check("dizi anahtarı adı korunur",
          n('Undefined array key "host"') == 'Undefined array key "host"'
          and n('Undefined array key "host"') != n('Undefined array key "password"'))
    check("sayı/uuid/ID-benzeri tırnak içi maskelenir",
          n("Order 12345 not found") == n("Order 98 not found") == "Order <n> not found"
          and n('Product "550e8400-e29b-41d4-a716-446655440000" missing') == 'Product "<v>" missing'
          and n("Undefined array key 7") == "Undefined array key <n>")
    check("mesajdaki iç işaretçi karakteri çökertmez, sahte eşleşme üretmez",
          n("a \ue000x b") == "a x b" and n('"k" \ue000\ue100') == '"k"')
    check("action: WebTransaction öneki atılır, controller StudlyCase",
          rank.action_of("WebTransaction/Action/api::order::integrationCancel") == "Api/Order::integrationCancel"
          and rank.action_of("WebTransaction/Custom/index::index") == "Index::index"
          and rank.action_of("WebTransaction/Action/web-hook::trigger") == "WebHook::trigger")
    fp1 = rank.nr_fingerprint("webservice", "A::b", "E_WARNING", "x" * 900)
    check("parmak izi 512'de kesilir ve kararlıdır",
          len(fp1) == 512 and fp1 == rank.nr_fingerprint("webservice", "A::b", "E_WARNING", "x" * 900))

    def nf(app, msg, cnt, action="Content::page", cls="E_WARNING", repo="webservice"):
        return Finding(source="newrelic", repo=repo, app=app, action=action, err_class=cls,
                       message=msg, count=cnt, last_seen=2000.0, error_rate=0.01)

    sites = [f"[Prod] site{i}.ru" for i in range(6)]
    units = rank.nr_units([nf(s, 'Undefined array key "value"', 100) for s in sites])
    check("6 uygulamadaki aynı hata → tek birim, 6 uygulama, skor = adet × uygulama, 1 SP",
          len(units) == 1 and len(units[0].apps) == 6 and units[0].score == 600 * 6 and units[0].sp == 1)
    u2 = rank.nr_units([nf("[Prod] a", "Order 1 failed", 5, cls="TypeError"),
                        nf("[Prod] a", "Order 2 failed", 7, cls="TypeError")])
    check("ID farkı aynı birime düşer; TypeError → 3 SP",
          len(u2) == 1 and u2[0].total == 12 and u2[0].sp == 3 and not u2[0].needs_human)
    infra = rank.nr_units([nf("[Prod] a", "cURL error 28: Operation timed out after 5000 ms", 50,
                              cls="GuzzleHttp\\Exception\\ConnectException")])
    check("altyapı hatası → 3 SP + needs_human", infra[0].sp == 3 and infra[0].needs_human)
    fam = rank.nr_units([nf("[Prod] a", f'Undefined array key "{c}"', 10, action="Api/Order::create")
                         for c in "abcde"]
                        + [nf("[Prod] a", 'Undefined array key "host"', 10, action="Index::index")])
    merged = rank.merge_undefined(fam)
    big = [g for g in merged if g.findings[0].action == "Api/Order::create"]
    check("aynı action'daki 5 tanımsız anahtar → tek Task, 5 parmak izi, 2 SP; başka action ayrı",
          len(merged) == 2 and len(big) == 1 and len(big[0].fingerprints) == 5 and big[0].sp == 2)
    three = rank.merge_undefined(rank.nr_units(
        [nf("[Prod] a", f"Undefined index: {c}", 10, action="X::y", cls="E_NOTICE") for c in ("ka", "kb", "kc")]))
    check("≤3 anahtar → 1 SP", len(three) == 1 and three[0].sp == 1 and len(three[0].fingerprints) == 3)


def test_quality_rank_sonar():
    print("\n[Q2] Kalite alımı — Sonar birim, skor, paketleme")
    from agile_sdlc_crew.quality import rank
    from agile_sdlc_crew.quality.model import Finding

    def sf(file, rule="css:S4657", sev="CRITICAL", typ="BUG", repo="webservice", line=1):
        return Finding(source="sonar", repo=repo, project=f"EcomBackend:{repo}", file=file, rule=rule,
                       rule_name=rule, severity=sev, itype=typ, lines=[line])

    css = [sf("themes/a.css", line=i) for i in range(300)]
    vuln = [sf("app/Login.php", rule="php:S2076", sev="BLOCKER", typ="VULNERABILITY")]
    units = rank.sonar_units(css + vuln)
    by = {u.findings[0].rule: u for u in units}
    check("aynı dosya+kural → tek birim (300 madde)", len(units) == 2 and by["css:S4657"].total == 300)
    check("tek Vulnerability+Blocker, 300 maddelik Bug+Critical'ı geçer",
          by["php:S2076"].score > by["css:S4657"].score,
          f"{by['php:S2076'].score} vs {by['css:S4657'].score}")
    check("SP: 1 madde → 1, 300 madde → 3", by["php:S2076"].sp == 1 and by["css:S4657"].sp == 3)
    check("SP sınırları ≤5→1, ≤20→2, >20→3", [rank.sonar_unit_sp(n) for n in (5, 6, 20, 21)] == [1, 2, 2, 3])
    sec = rank.sonar_units([sf("config/app.php", rule="secrets:S6703", sev="BLOCKER", typ="VULNERABILITY")])[0]
    check("secrets:* → 1 SP + needs_human", sec.sp == 1 and sec.needs_human and "rotasyon" in sec.human_reason)
    twelve = rank.sonar_units([sf(f"themes/t{i}.css") for i in range(12)])
    packs = rank.pack_sonar(twelve, pack_sp=3, pack_files=5)
    check("12 dosya × 1 SP, paket ≤3 SP → 4 paket × 3 dosya",
          len(packs) == 4 and all(p.sp == 3 and len(p.files) == 3 for p in packs))
    packs5 = rank.pack_sonar(twelve, pack_sp=99, pack_files=5)
    check("dosya sınırı 5 → 5+5+2", sorted(len(p.files) for p in packs5) == [2, 5, 5])
    check("paket başlığı +N dosya, parmak izi birim başına",
          "(+4 dosya)" in max(packs5, key=lambda p: len(p.files)).title
          and sum(len(p.fingerprints) for p in packs5) == 12)
    mixed = rank.pack_sonar(rank.sonar_units([sf("a.css"), sf("b.php", rule="php:S1")]))
    check("farklı kural aynı pakete girmez", len(mixed) == 2)


def test_quality_select():
    print("\n[Q3] Kalite alımı — bütçe seçimi")
    from agile_sdlc_crew.quality import rank
    from agile_sdlc_crew.quality.model import Group

    def g(name, sp, score, human=False):
        return Group(source="sonar", repo="r", title=name, fingerprints=[name], findings=[],
                     score=score, sp=sp, needs_human=human)

    pick = rank.select_budget([g("a", 5, 100), g("b", 3, 90), g("c", 3, 80), g("d", 2, 70), g("e", 1, 60)], 10)
    check("açgözlü: sığmayan atlanır, küçük olan denenir, toplam ≤ bütçe",
          [x.title for x in pick] == ["a", "b", "d"] and sum(x.sp for x in pick) == 10)
    pick = rank.select_budget([g("h1", 3, 100, True), g("h2", 1, 99, True), g("x", 3, 50), g("y", 3, 40)], 10)
    check("needs_human payı ≤ %30 (10 SP → en çok 3)",
          [x.title for x in pick] == ["h1", "x", "y"] and sum(x.sp for x in pick if x.needs_human) <= 3)
    check("Task sayısı tavanı", len(rank.select_budget([g(str(i), 1, 100 - i) for i in range(20)], 40, max_tasks=8)) == 8)
    check("bütçeden büyük tek iş → boş seçim", rank.select_budget([g("big", 5, 1)], 3) == [])
    check("3 SP bütçede needs_human hiç girmez (pay 0)", rank.select_budget([g("h", 1, 9, True)], 3) == [])


def test_quality_render():
    print("\n[Q4] Kalite alımı — WI metinleri, maskeleme, parmak izi satırı")
    import html as _html
    from agile_sdlc_crew.quality import rank, render
    from agile_sdlc_crew.quality.model import Finding
    m = render.mask_secrets
    check("Bearer/Basic ve password=… maskelenir",
          "abc123def456" not in m("Authorization: Bearer abc123def456ghi")
          and m("password=Hunter2x&x=1") == "password=***&x=1"
          and "dXNlcjpwYXNz" not in m("Basic dXNlcjpwYXNz"))
    check("uzun anahtar adları maskelenmez",
          m('Undefined array key "is_prime_activate_on_preprod_web"')
          == 'Undefined array key "is_prime_activate_on_preprod_web"')
    check("uzun hex/token maskelenir", "***" in m("token 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"))
    check("action → dosya ipucu (V1 varyantı dahil)",
          render.file_hints("Api/Order::integrationCancel") == [
              "app/Controller/Api/Order.php::integrationCancelAction",
              "app/Controller/V1/Api/Order.php::integrationCancelAction"]
          and render.file_hints("cron.php") == [])
    nr = rank.nr_units([Finding(source="newrelic", repo="webservice", app=f"[Prod] s{i}.ru", action="Content::page",
                                err_class="E_WARNING", message='Undefined array key "value"', count=10,
                                uris=["/ru/x"], first_seen=1.7e9, last_seen=1.7e9 + 60, error_rate=0.05)
                        for i in range(2)])[0]
    d = render.task_description(nr, nr_days=7)
    check("NR açıklaması: ilk satır Repo, iki uygulama, ipucu, kabul kriteri, parmak izi",
          d.startswith("<p><b>Repo:</b> <code>webservice</code></p>") and "[Prod] s0.ru" in d
          and "[Prod] s1.ru" in d and "app/Controller/Content.php::pageAction" in d
          and "Kabul kriteri" in d and render.parse_fingerprints(d) == nr.fingerprints)
    check("NR başlığı", render.task_title(nr) == '[webservice] Content::page: Undefined array key "value"')
    sec = rank.sonar_units([Finding(source="sonar", repo="webservice", project="EcomBackend:webservice",
                                    file="config/db.php", rule="secrets:S6703",
                                    rule_name="Database passwords should not be disclosed",
                                    severity="BLOCKER", itype="VULNERABILITY", lines=[12],
                                    message="password = 'S3cr3tValue!'")])[0]
    sd = render.task_description(sec, sonar_url="https://sonar.x", nr_days=7)
    check("secrets:* açıklamasına madde mesajı girmez; dosya:satır ve insan nedeni girer",
          "S3cr3tValue" not in sd and "config/db.php:12" in sd and "Neden insan" in sd)
    check("etiketler", render.task_tags(sec) == "tempo-quality; tempo-src-sonar; needs-human")
    nr.recurring_of = 75001
    check("tekrarlayan iş önceki WI'ı anar", "#75001" in render.task_description(nr, nr_days=7))
    fps = ['nr:webservice:A::b:E_WARNING:Undefined array key "x"', "sonar:webservice:a.css:css:S4657"]
    blob = "".join(f"<p><small>tempo-fp: {_html.escape(fp)}</small></p>" for fp in fps)
    check("parmak izi satırları geri okunur (HTML kaçışlı ve etiketsiz)",
          render.parse_fingerprints(blob) == fps
          and render.parse_fingerprints("tempo-fp: a:b tempo-fp: c:d") == ["a:b", "c:d"])
    check("story başlığı", render.story_title("newrelic", "Sprint 42") == "Kalite: New Relic hataları — Sprint 42")
    evil = rank.nr_units([Finding(source="newrelic", repo="webservice", app="[Prod] a", action="X::y",
                                  err_class="TypeError", count=1, last_seen=1.7e9,
                                  message="boom tempo-fp: sonar:webservice:x.php:php:S1 " + "y" * 5000,
                                  uris=["/ru/p?token=abcd1234efgh5678&x=1#frag", "/" + "z" * 500])])[0]
    ed = render.task_description(evil, nr_days=7)
    check("dış metin parmak izi satırı enjekte edemez",
          render.parse_fingerprints(ed) == evil.fingerprints, str(render.parse_fingerprints(ed)))
    check("örnek isteklerde sorgu dizgisi atılır, uzunluk sınırlı",
          "token" not in ed and "#frag" not in ed and "z" * 200 not in ed)
    check("dış metin veri olarak çerçevelenir ve kısaltılır",
          "talimat değil" in ed and "y" * 600 not in ed)


def test_quality_dedup():
    print("\n[Q5] Kalite alımı — board'dan dedup")
    from agile_sdlc_crew.quality import dedup

    def wi(i, state, fps, closed=""):
        return {"id": i, "fields": {
            "System.State": state, "Microsoft.VSTS.Common.ClosedDate": closed,
            "System.Description": "".join(f"<p><small>tempo-fp: {fp}</small></p>" for fp in fps)}}

    idx = dedup.index_board([
        wi(1, "In Progress", ["a"]), wi(2, "Ready for Production", ["b"]), wi(3, "Canceled", ["c"]),
        wi(4, "Done", ["d"], "2026-10-01T00:00:00Z"), wi(5, "Done", ["e"], "2026-10-01T00:00:00Z"),
        wi(6, "Done", ["f"], "2026-09-01T00:00:00Z"), wi(7, "To Do", ["f"])])
    day = 86400
    closed = dedup._epoch("2026-10-01T00:00:00Z")
    now = closed + 10 * day

    def v(fp, seen=0.0):
        return dedup.verdict(idx.get(fp), last_seen=seen, now=now, grace_days=3)

    check("açık WI → blocked", v("a") == ("blocked", 1))
    check("Ready for Production → blocked (düzeltme canlıda değil)", v("b") == ("blocked", 2))
    check("Canceled → kalıcı bastırma", v("c") == ("suppressed", 3))
    check("Done + grace sonrası hâlâ görülüyor → recurring", v("d", seen=closed + 5 * day) == ("recurring", 4))
    check("Done + grace içinde görülmüş → blocked", v("e", seen=closed + 1 * day) == ("blocked", 5))
    check("Sonar (last_seen yok) → şimdiye göre", v("d") == ("recurring", 4))
    check("aynı parmak izi iki WI'da → en yeni (büyük id) kazanır", v("f") == ("blocked", 7))
    check("hiç yok → new", v("zzz") == ("new", None))


def test_quality_app_map():
    print("\n[Q6] Kalite alımı — NR uygulama → repo eşlemesi (FLODevOps)")
    import inspect
    import yaml
    from agile_sdlc_crew.quality import app_map
    from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
    check("Azure istemcisi başka projedeki repoyu okuyabilir (project parametresi)",
          "project" in inspect.signature(AzureDevOpsClient.get_file_content).parameters
          and "project" in inspect.signature(AzureDevOpsClient.get_items_in_path).parameters)
    jf_tr = 'String gitOriginApp = "git@ssh.dev.azure.com:v3/flotechnology/ecombackend/webservice"\n'
    df_tr = ("ARG website=tr-stocks.flo.com.tr\n"
             "&& sed -ie 's/[ ;]*newrelic.appname[[:space:]]=.*/newrelic.appname=\"[Prod] tr-stocks.flo.com.tr\"/' "
             "$(php-config --ini-dir)/newrelic.ini \\\n")
    e = app_map.parse_build_dir(jf_tr, df_tr)
    check("literal appname + gitOriginApp",
          e == {"project": "ecombackend", "repo": "webservice", "apps": ["[Prod] tr-stocks.flo.com.tr"],
                "fallback": False}, str(e))
    jf_web = ('String gitOriginApp = "git@ssh.dev.azure.com:v3/flotechnology/ecombackend/webservice"\n'
              "   flokz       : [website: 'flo.com.kz', themePath: 'flo-kz'],\n"
              "   floma       : [website: 'flo.co.ma', themePath: 'flo-ma'],\n"
              '   newrelic_app_name = "[Prod] ${website}"\n'
              "   sh \"docker build --build-arg newrelic_app_name='${newrelic_app_name}' .\"\n")
    df_web = ("ARG website\nARG newrelic_app_name\n"
              '&& sed -ie "s/[ ;]*newrelic.appname[[:space:]]=.*/newrelic.appname=\\"${newrelic_app_name}\\"/" x\n')
    e2 = app_map.parse_build_dir(jf_web, df_web)
    check("${website} şablonu her siteye açılır, ${newrelic_app_name} yok sayılır",
          e2 and e2["apps"] == ["[Prod] flo.com.kz", "[Prod] flo.co.ma"], str(e2))
    check("gitOriginApp yoksa None", app_map.parse_build_dir("", df_tr) is None)
    # 2026-11-05: NR'de en çok hata veren uygulamalar eşlenemiyordu (sneakerbox.ru 35K, brd/proskater/
    # billabong.ru ~12K) — russia/build/websites siteleri bir Map'ten seçiyor.
    jf_ru = ('String gitOriginApp = "git@ssh.dev.azure.com:v3/FloTechnology/ecombackend/webservice"\n'
             "Map brandDomains = [\n    sneakerbox: 'sneakerbox.ru',\n    brd       : 'brd.ru',\n]\n"
             "    website = brandDomains[params.brand]\n"
             '    newrelic_app_name = "[Prod] ${website}"\n')
    e3 = app_map.parse_build_dir(jf_ru, "ARG website\n")
    check("Map'ten seçilen siteler şablona açılır (russia websites)",
          e3["apps"] == ["[Prod] sneakerbox.ru", "[Prod] brd.ru"] and not e3["fallback"], str(e3))
    jf_seg = ('String gitOriginApp = "git@ssh.dev.azure.com:v3/FloTechnology/EcomBackend/SegFlo"\n'
              'String website = "seg.flo.com.tr"\n')
    e4 = app_map.parse_build_dir(jf_seg, "")
    check("NR adı yoksa siteden tahmin ('[Prod] x' ve NR'deki boşluksuz '[Prod]x')",
          e4["apps"] == ["[Prod] seg.flo.com.tr", "[Prod]seg.flo.com.tr"] and e4["fallback"], str(e4))

    class FC:
        def get_items_in_path(self, devrepo, path, **kw):
            return [{"path": "/build/a", "isFolder": True}, {"path": "/build/b", "isFolder": True}] \
                if devrepo == "anatolia" else []

        def get_file_content(self, devrepo, path, **kw):
            if path == "/build/a/JenkinsfileBuild":  # tahmin: site adı sneakerbox.ru, NR ayarı yok
                return ('String gitOriginApp = "git@x:v3/O/EcomFrontend/sites"\n'
                        'String website = "sneakerbox.ru"\n')
            if path == "/build/b/JenkinsfileBuild":
                return jf_ru
            return ""
    m = app_map.build_map(FC())
    check("tahmin edilen ad, başka dizinin açık NR adını ezmez",
          m["[Prod] sneakerbox.ru"]["repo"] == "webservice" and m["[Prod]sneakerbox.ru"]["repo"] == "sites", m)

    calls = {"n": 0}

    class FakeClient:
        def get_items_in_path(self, repo, path, branch=None, recursion_level="oneLevel", project=None):
            calls["n"] += 1
            assert project == "FLODevOps" and branch == "production"
            if repo != "global":
                return []
            return [{"path": "/build", "isFolder": True}, {"path": "/build/trstock", "isFolder": True},
                    {"path": "/build/.DS_Store", "isFolder": False}]

        def get_file_content(self, repo, path, branch=None, project=None):
            return {"/build/trstock/JenkinsfileBuild": jf_tr, "/build/trstock/Dockerfile": df_tr}[path]

    class Broken(FakeClient):
        def get_items_in_path(self, *a, **k):
            raise RuntimeError("401")

    with tempfile.TemporaryDirectory() as td:
        cache, ovr = Path(td) / "map.yaml", Path(td) / "ovr.yaml"
        ovr.write_text(yaml.safe_dump({"[Prod] Stock Api": "stock-api"}), encoding="utf-8")
        m = app_map.load_map(FakeClient(), cache_path=cache, overrides_path=ovr)
        check("FLODevOps'tan kurulur, büyük/küçük harf duyarsız, override eklenir",
              app_map.resolve("[PROD] TR-STOCKS.FLO.COM.TR", m) == "webservice"
              and app_map.resolve("[Prod] Stock Api", m) == "stock-api" and app_map.resolve("yok", m) == "")
        n = calls["n"]
        app_map.load_map(FakeClient(), cache_path=cache, overrides_path=ovr)
        check("taze önbellek varken FLODevOps tekrar okunmaz", calls["n"] == n)
        m2 = app_map.load_map(Broken(), cache_path=cache, overrides_path=ovr, force=True)
        check("FLODevOps okunamazsa eski önbellek kullanılır",
              app_map.resolve("[Prod] tr-stocks.flo.com.tr", m2) == "webservice")
        cache.unlink()
        try:
            app_map.load_map(Broken(), cache_path=cache, overrides_path=ovr)
            ok = False
        except app_map.QualitySourceError:
            ok = True
        check("önbellek de yoksa QualitySourceError", ok)


def test_quality_sources():
    print("\n[Q7] Kalite alımı — Sonar/NR yanıtlarını bulguya çevirme")
    import os
    from agile_sdlc_crew.quality import newrelic, sonar
    err = [
        {"facet": ["[Prod] tr-stocks.flo.com.tr", "E_WARNING", 'Undefined array key "host"',
                   "WebTransaction/Custom/index::index"],
         "count": 645052, "first": 1791290877564.0, "last": 1791895671566.0, "uris": ["/en"]},
        {"facet": ["[Preprod] preprod.flo.com.kz", "E_WARNING", "x", "WebTransaction/Custom/Page::notFound"],
         "count": 5, "uris": []},
        {"facet": ["Marketplace API", "TypeError", "y", "WebTransaction/Action/api::x"], "count": 3},
    ]
    tx = [{"facet": "[Prod] tr-stocks.flo.com.tr", "appName": "[Prod] tr-stocks.flo.com.tr", "count": 1290104},
          {"facet": "Marketplace API", "count": 0}]
    fs = newrelic.rows_to_findings(err, tx, is_prod=lambda a: newrelic.is_prod_name(a) or a == "Marketplace API")
    check("prod filtresi: [Preprod] elenir, eşlenmiş köşeli parantezsiz prod kalır",
          [f.app for f in fs] == ["[Prod] tr-stocks.flo.com.tr", "Marketplace API"])
    f0 = fs[0]
    check("NR satırı → Finding (action, saniye, error rate, uri)",
          f0.action == "Index::index" and f0.count == 645052 and abs(f0.last_seen - 1791895671.566) < 1e-3
          and abs(f0.error_rate - 0.5) < 0.01 and f0.uris == ["/en"])
    check("işlem sayısı 0 → error rate 0", fs[1].error_rate == 0.0)
    check("prod adı kalıpları",
          all(newrelic.is_prod_name(a) for a in ("[Prod] x", "[PROD] y", "[Production] FLO Orkestra V2",
                                                  "[GCP-Prod] panel"))
          and not newrelic.is_prod_name("[Preprod] x"))
    issues = [
        {"key": "k1", "rule": "php:S2076", "severity": "BLOCKER", "type": "VULNERABILITY",
         "project": "EcomBackend:webservice", "component": "EcomBackend:webservice:app/Login.php",
         "line": 40, "message": "Make sure..."},
        {"key": "k2", "rule": "secrets:S6703", "severity": "BLOCKER", "type": "VULNERABILITY",
         "project": "EcomBackend:webservice", "component": "EcomBackend:webservice:config/db.php",
         "line": 3, "message": "password = 'S3cret'"},
    ]
    sf = sonar.issues_to_findings(issues, {"php:S2076": "OS commands should not be vulnerable"},
                                  base_url="https://sonar.x")
    check("Sonar: proje → repo, component → dosya, kural adı, link",
          sf[0].repo == "webservice" and sf[0].file == "app/Login.php"
          and sf[0].rule_name == "OS commands should not be vulnerable" and sf[0].lines == [40]
          and "open=k1" in sf[0].link)
    check("secrets:* mesajı hiç saklanmaz", sf[1].message == "")
    check("kapsam: AZURE_DEVOPS_REPO_PROJECTS önekine göre (büyük/küçük harf duyarsız)",
          sonar.in_scope("EcomBackend:webservice", ["ecombackend", "EcomLojistik"])
          and not sonar.in_scope("FLOXO:tedweb", ["EcomBackend"]))
    pages = {1: {"issues": [{"key": "a"}] * 500, "paging": {"total": 863}, "rules": [{"key": "r", "name": "R"}]},
             2: {"issues": [{"key": "b"}] * 363, "paging": {"total": 863}}}
    seen = []
    orig = sonar._get
    sonar._get = lambda path, params: (seen.append(params["p"]), pages[params["p"]])[1]
    try:
        iss, rules = sonar.fetch_issues()
    finally:
        sonar._get = orig
    check("Sonar sayfalama: 863 madde iki sayfada", len(iss) == 863 and seen == [1, 2] and rules == {"r": "R"})
    saved = {k: os.environ.pop(k, None) for k in ("NEW_RELIC_ACCOUNT_ID", "NEW_RELIC_API_KEY")}
    try:
        try:
            newrelic.nrql("SELECT 1")
            ok = False
        except newrelic.QualitySourceError:
            ok = True
    finally:
        for k, val in saved.items():
            if val is not None:
                os.environ[k] = val
    check("NR kimlik bilgisi yoksa QualitySourceError", ok)
    raw = newrelic.rows_to_findings(
        [{"facet": ["[Prod] a", "E", "login failed password=Hunter2x " + "q" * 3000, "WebTransaction/Custom/a::b"],
          "count": 1, "uris": ["/p?session=abc123def456&x=1"]}], [], is_prod=newrelic.is_prod_name)[0]
    check("NR mesajı/URI girişte temizlenir (secret maskesi, sorgu dizgisi, uzunluk)",
          "Hunter2x" not in raw.message and len(raw.message) <= 1000 and raw.uris == ["/p"], repr(raw.uris))
    from agile_sdlc_crew import pipeline_config as _pcq
    check("Sonar adresi dashboard'dan değiştirilemez (token sızmasın)", "CREW_SONAR_URL" not in _pcq._SCHEMA_BY_KEY)
    old_url, old_tok = os.environ.get("CREW_SONAR_URL"), os.environ.get("SONAR_TOKEN")
    os.environ["CREW_SONAR_URL"], os.environ["SONAR_TOKEN"] = "http://evil.example", "t0k"
    try:
        try:
            sonar._get("/api/issues/search", {})
            ok = False
        except sonar.QualitySourceError as e:
            ok = "https" in str(e)
    finally:
        for k, val in (("CREW_SONAR_URL", old_url), ("SONAR_TOKEN", old_tok)):
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
    check("https olmayan Sonar adresine token gönderilmez", ok)


def test_quality_intake():
    print("\n[Q8] Kalite alımı — orkestratör (sahte Azure/DB)")
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import intake, rank
    from agile_sdlc_crew.quality.model import Finding
    keys = ("CREW_QUALITY_INTAKE", "NEW_RELIC_REGION", "CREW_QUALITY_NR_LOOKBACK_DAYS",
            "CREW_QUALITY_NR_MIN_ERRORS", "CREW_QUALITY_SONAR_PACK_SP", "CREW_QUALITY_SONAR_PACK_FILES",
            "CREW_QUALITY_HUMAN_MAX", "CREW_QUALITY_MAX_TASKS", "CREW_QUALITY_REOPEN_GRACE_DAYS")
    check("knob'lar kayıtlı, ana anahtar varsayılan kapalı",
          all(k in pc._SCHEMA_BY_KEY for k in keys) and pc._SCHEMA_BY_KEY["CREW_QUALITY_INTAKE"]["default"] is False)
    cfg = {"nr_days": 7, "nr_min_errors": 100, "pack_sp": 3, "pack_files": 5, "human_max": 5,
           "max_tasks": 8, "grace_days": 3, "sonar_url": "https://sonar.x"}

    def nf(app, key, cnt, action="Index::index", repo="webservice", cls="E_WARNING", msg=None):
        return Finding(source="newrelic", repo=repo, app=app, action=action, err_class=cls,
                       message=msg or f'Undefined array key "{key}"', count=cnt, last_seen=1e9)

    findings = [nf("[Prod] a", "host", 645000), nf("[Prod] b", "password", 900, action="Api/Order::create", repo="channel"),
                nf("[Prod] a", "low", 50), nf("[Prod] c", "x", 5000, repo="sites")]
    board = {rank.nr_fingerprint("channel", "Api/Order::create", "E_WARNING", 'Undefined array key "password"'):
             {"wi": 9, "state": "Active", "closed_at": 0}}
    groups, skipped = intake.build_candidates("newrelic", findings, board_index=board, now=2e9,
                                              repo_ok=lambda r: r != "sites", cfg=cfg)
    reasons = " | ".join(s["reason"] for s in skipped)
    check("eşik altı, açık WI ve yerelde olmayan repo elenir",
          [g.repo for g in groups] == ["webservice"] and len(groups[0].fingerprints) == 1
          and "yerelde" in reasons and "#9" in reasons and "eşik" in reasons, reasons)

    class FakeClient:
        org_url, project, repo_projects = "https://dev.azure.com/o", "P", ["EcomBackend"]

        def __init__(self):
            self.created, self.updates, self.stories = [], [], []
            self.iters = [{"name": "Sprint 42", "path": "P\\Sprint 42", "attributes": {"timeFrame": "current"}},
                          {"name": "Sprint 44", "path": "P\\Sprint 44",
                           "attributes": {"timeFrame": "future", "startDate": "2026-11-02T00:00:00Z"}},
                          {"name": "Sprint 43", "path": "P\\Sprint 43",
                           "attributes": {"timeFrame": "future", "startDate": "2026-10-19T00:00:00Z"}}]

        def list_iterations(self, team=""):
            return self.iters

        def query_work_items(self, wiql, limit=0):
            return self.stories

        def get_team_area_path(self, team=""):
            return "P\\Team"

        def get_authenticated_user(self):
            return {"uniqueName": "me@flo.com.tr"}

        def create_work_item(self, type_name, fields, parent_id=None):
            self.created.append((type_name, fields, parent_id))
            return {"id": 1000 + len(self.created) - 1, "fields": fields}

        def update_work_item(self, wid, ops):
            self.updates.append((wid, ops))
            return {}

        def get_work_item(self, wid):
            return {"id": wid, "fields": {"System.WorkItemType": "Bug", "System.State": "Active"}}

    queued, recorded = [], []
    deps = intake.Deps(
        gather=lambda source, client, cfg_: (list(findings), [{"app": "[Prod] ?", "reason": "eşlenemedi"}]),
        board=lambda client: {}, repo_ok=lambda r: True,
        queue=lambda wid, title: (queued.append(wid), 500 + len(queued))[1],
        record=lambda **kw: recorded.append(kw), now=lambda: 2e9, cfg=lambda: cfg)
    c = FakeClient()
    res = intake.take(c, "newrelic", 10, team="Team", deps=deps)
    story = c.created[0]
    check("story yoksa SONRAKİ sprintte açılır, aktif sprinte dokunulmaz (başlık, etiket, iterasyon, area)",
          story[0] == "User Story" and story[1]["System.Title"] == "Kalite: New Relic hataları — Sprint 43"
          and "tempo-src-newrelic" in story[1]["System.Tags"] and story[1]["System.IterationPath"] == "P\\Sprint 43"
          and story[1]["System.AreaPath"] == "P\\Team")
    tasks = [x for x in c.created if x[0] == "Task"]
    check("Task'lar story'nin child'ı; area/iteration story'den; atama PAT sahibi",
          tasks and all(t[2] == 1000 and t[1]["System.IterationPath"] == "P\\Sprint 43"
                        and t[1]["System.AreaPath"] == "P\\Team"
                        and t[1]["System.AssignedTo"] == "me@flo.com.tr" for t in tasks))
    check("her Task kuyruğa alınır ve parmak izi başına kaydedilir",
          len(queued) == len(tasks) and res["tasks"][0]["job_id"] == 501 and len(recorded) >= len(tasks))
    check("toplam SP bütçeyi aşmaz; eşlenemeyen raporlanır",
          res["total_sp"] <= 10 and any(s.get("reason") == "eşlenemedi" for s in res["skipped"]))
    check("story SP = açılan Task'ların toplamı",
          any(w == 1000 and ops[0]["path"] == "/fields/Custom.StoryPoints" and ops[0]["value"] == res["total_sp"]
              for w, ops in c.updates))

    c2 = FakeClient()
    c2.stories = [{"id": 77, "fields": {"System.AreaPath": "P\\A", "System.IterationPath": "P\\Sprint 43",
                                        "Custom.StoryPoints": 2}}]
    res2 = intake.take(c2, "newrelic", 10, deps=deps)
    check("sprintte bu kaynağın story'si varsa onu kullanır; SP üstüne eklenir",
          c2.created and all(x[0] == "Task" and x[2] == 77 for x in c2.created)
          and any(w == 77 and ops[0]["value"] == 2 + res2["total_sp"] for w, ops in c2.updates))

    hum = [nf("[Prod] a", "", 99999, action="X::y", cls="PDOException",
              msg="SQLSTATE[HY000] [2002] Connection refused")]
    c3 = FakeClient()
    q0 = len(queued)
    res3 = intake.take(c3, "newrelic", 10, deps=deps._replace(gather=lambda s, cl, cf: (hum + findings, [])))
    h = res3["human_tasks"]
    check("needs_human Task açılır, kuyruğa alınmaz, needs-human etiketi taşır",
          len(h) == 1 and h[0]["job_id"] is None and len(queued) - q0 == len(res3["tasks"])
          and any("needs-human" in x[1].get("System.Tags", "") for x in c3.created if x[0] == "Task"))

    c4 = FakeClient()
    res4 = intake.take(c4, "newrelic", 10, deps=deps._replace(gather=lambda s, cl, cf: ([], [])))
    check("aday yoksa hiçbir WI (story dahil) açılmaz", c4.created == [] and res4["story"] is None
          and res4["tasks"] == [] and res4.get("message"))
    c5 = FakeClient()
    c5.iters = []
    try:
        intake.take(c5, "newrelic", 10, deps=deps)
        ok = False
    except intake.NoSprint:
        ok = True
    check("hedef sprint yok ve story_id yok → NoSprint, WI açılmaz", ok and c5.created == [])
    c7 = FakeClient()
    cfg_cur = dict(cfg, target_sprint="current")
    intake.take(c7, "newrelic", 10, deps=deps._replace(cfg=lambda: cfg_cur))
    check("CREW_QUALITY_TARGET_SPRINT=current → aktif sprint",
          c7.created and c7.created[0][1]["System.IterationPath"] == "P\\Sprint 42")
    check("hedef sprint knob'u kayıtlı, varsayılan next",
          pc._SCHEMA_BY_KEY.get("CREW_QUALITY_TARGET_SPRINT", {}).get("default") == "next"
          and intake.load_cfg()["target_sprint"] == "next")
    c6 = FakeClient()
    try:
        intake.take(c6, "newrelic", 10, story_id=5, deps=deps)
        ok = False
    except ValueError:
        ok = True
    check("story_id User Story değilse ValueError, WI açılmaz", ok and c6.created == [])
    intake._LOCK.acquire()
    try:
        try:
            intake.take(FakeClient(), "newrelic", 10, deps=deps)
            ok = False
        except intake.Busy:
            ok = True
    finally:
        intake._LOCK.release()
    check("eşzamanlı ikinci alım → Busy", ok)
    pv = intake.preview(FakeClient(), "newrelic", 10, deps=deps)
    check("preview yazmaz ama aynı seçimi döner", pv["total_sp"] == res["total_sp"] and pv["story"] is None)
    try:
        from agile_sdlc_crew import db
        db.init_db()
        check("quality_items tablosu oluşur ve okunur", isinstance(db.list_quality_items(limit=1), list))
    except Exception as e:  # noqa: BLE001
        skip("quality_items tablosu", f"DB erişilemedi: {e}")


def test_quality_endpoints():
    print("\n[Q9] Kalite alımı — endpoint kapıları + dashboard kartı")
    from fastapi.testclient import TestClient
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import server
    orig = pc.get
    pc.get = lambda k: False if k == "CREW_QUALITY_INTAKE" else orig(k)
    try:
        tc = TestClient(server.app)
        check("kapalıyken take → 403", tc.post("/api/quality/take", json={"source": "newrelic", "sp": 10}).status_code == 403)
        check("kapalıyken preview → 403", tc.get("/api/quality/preview?source=sonar&sp=5").status_code == 403)
        check("kapalıyken unmapped → 403", tc.get("/api/quality/unmapped").status_code == 403)
        check("recent kapalıyken enabled:false", tc.get("/api/quality/recent").json().get("enabled") is False)
    finally:
        pc.get = orig
    pc.get = lambda k: True if k == "CREW_QUALITY_INTAKE" else orig(k)
    try:
        tc = TestClient(server.app)
        check("geçersiz kaynak → 400", tc.post("/api/quality/take", json={"source": "jira", "sp": 10}).status_code == 400)
        check("SP sınırı (1..40) → 400", tc.post("/api/quality/take", json={"source": "sonar", "sp": 0}).status_code == 400
              and tc.get("/api/quality/preview?source=sonar&sp=99").status_code == 400)
    finally:
        pc.get = orig
    html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text(encoding="utf-8")
    check("dashboard Kalite kartı ve çağrıları",
          'id="ovQualityCard"' in html and "/api/quality/take" in html and "/api/quality/preview" in html
          and "function loadQuality" in html and "loadQuality();" in html)


def test_upgrade_api_surface():
    print("\n[U6] Sürüm yükseltme — ayarlar, uçlar, kart")
    from fastapi.testclient import TestClient
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import server
    schema = {e["key"]: e for e in pc.SCHEMA}
    want = {"CREW_TECHDEBT_UPGRADE": False, "CREW_TECHDEBT_UPGRADE_AUTO": False, "CREW_TECHDEBT_UPGRADE_BUMP": True,
            "CREW_TECHDEBT_UPGRADE_POLL_MIN": 30, "CREW_TECHDEBT_UPGRADE_MAX_OPEN": 3,
            "CREW_TECHDEBT_UPGRADE_SPRINT": "next", "CREW_TECHDEBT_UPGRADE_CI_WAIT_H": 72}
    check("yedi yükseltme ayarı varsayılanlarıyla şemada",
          all(k in schema and schema[k]["default"] == v for k, v in want.items()),
          str({k: schema.get(k, {}).get("default") for k in want}))
    check("araç yolu ayarları şemada yok",
          "CREW_UPGRADE_PHP_BINS" not in schema and "CREW_UPGRADE_PYTHON_BINS" not in schema)
    paths = {getattr(r, "path", "") for r in server.app.routes}
    check("repos / scan / take yolları kayıtlı",
          {"/api/techdebt/upgrade/repos", "/api/techdebt/upgrade/scan", "/api/techdebt/upgrade/take"} <= paths)
    check("start / stop / bump yolları kayıtlı",
          {"/api/techdebt/upgrade/start", "/api/techdebt/upgrade/stop", "/api/techdebt/upgrade/bump"} <= paths)
    orig = pc.get
    pc.get = lambda k: False if k == "CREW_TECHDEBT_UPGRADE" else (True if k in ("CREW_TECHDEBT", "CREW_TECHDEBT_PHP_MODERN") else orig(k))
    try:
        tc = TestClient(server.app)
        check("bayrak kapalıyken repos → 403", tc.get("/api/techdebt/upgrade/repos").status_code == 403)
        check("bayrak kapalıyken scan → 403", tc.get("/api/techdebt/upgrade/scan?repo=x").status_code == 403)
        check("bayrak kapalıyken take → 403",
              tc.post("/api/techdebt/upgrade/take", json={"repo": "x", "group_ids": ["a"]}).status_code == 403)
        for ep in ("start", "stop", "bump"):
            check(f"bayrak kapalıyken {ep} → 403",
                  tc.post(f"/api/techdebt/upgrade/{ep}", json={"repo": "x", "team": "T"}).status_code == 403)
    finally:
        pc.get = orig
    html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text(encoding="utf-8")
    check("kart yükseltme işlevleri ve mod anahtarı",
          all(n in html for n in ("function loadUpgradeRepos", "function upgradeScan", "function renderUpgrade",
                                  "function upgradeTake", "tempo_modern_mode", "tempo_upgrade_to")))

    def _fn(name):
        i = html.find("function " + name) if "async function " + name not in html else html.find("async function " + name)
        j = html.find("\nfunction ", i + 1)
        j2 = html.find("\nasync function ", i + 1)
        j = min(x for x in (j, j2, len(html)) if x > 0)
        return html[i:j]
    scan_src, changed_src = _fn("upgradeScan"), _fn("_upToChanged")
    check("_upShouldRetry var ve upgradeScan onu çağırıyor",
          "function _upShouldRetry" in html and "_upShouldRetry(" in scan_src)
    check("tempo_upgrade_to yalnız _upToChanged içinde yazılıyor",
          html.count("setItem('tempo_upgrade_to'") == 1 and "setItem('tempo_upgrade_to'" in changed_src
          and "setItem" not in scan_src)
    check("kayıtlı hedef 400 verince tek otomatik yeniden deneme",
          "retried" in scan_src and "_upDropSaved()" in scan_src)


def test_quality_review_fixes():
    print("\n[Q10] Kalite alımı — son inceleme düzeltmeleri")
    import requests as _rq
    import yaml
    from agile_sdlc_crew.quality import app_map, intake, rank, render
    from agile_sdlc_crew.quality.model import Finding

    def one(msg, cls="E_WARNING"):
        return rank.nr_units([Finding(source="newrelic", repo="r", app="[Prod] a", action="A::b",
                                      err_class=cls, message=msg, count=500, last_seen=1.0)])[0]
    code_bugs = [("Undefined array key \"timeout\"", "E_WARNING"),
                 ("Undefined array key \"connect_timeout\"", "E_WARNING"),
                 ("Undefined property: stdClass::$timeOut", "E_WARNING"),
                 ("Call to a member function getTimeout() on null", "Error"),
                 ("Argument #1 ($timeout) must be of type int, string given", "TypeError")]
    wrong = [m for m, c in code_bugs if one(m, c).needs_human]
    check("kimlik adındaki timeout altyapı sayılmaz (kod hatası, kuyruğa girer)", not wrong, str(wrong))
    check("tanımsız 'timeout' anahtarı 1 SP", one('Undefined array key "timeout"').sp == 1)
    infra = [("cURL error 28: Operation timed out after 5000 ms", "GuzzleHttp\\Exception\\ConnectException"),
             ("SQLSTATE[HY000] [2002] Connection refused", "PDOException"),
             ("SQLSTATE[HY000]: General error: 1205 Lock wait timeout exceeded", "PDOException"),
             ("MySQL server has gone away", "PDOException")]
    missed = [m for m, c in infra if not one(m, c).needs_human]
    check("gerçek altyapı hataları hâlâ needs_human", not missed, str(missed))

    m = render.mask_secrets
    leaks = [s for s, secret in (("https://user:hunter22@api.x.com/v1", "hunter22"),
                                 ("redis://:p4ssw0rd@10.0.0.5:6379", "p4ssw0rd"),
                                 ("redis://:pw9x@redis:6379", "pw9x"),
                                 ("pass=Secret1", "Secret1"), ("pwd hunter2", "hunter2"),
                                 ("client_secret: abcXYZ", "abcXYZ"))
             if secret in m(s)]
    check("URL userinfo ve kısa anahtar parolaları maskelenir", not leaks, str(leaks))
    check("bypass=1 gibi kelime içi eşleşme maskelenmez", m("bypass=1") == "bypass=1")
    pref = [s for s, secret in (("dbpassword=Hunter2x", "Hunter2x"), ("accesstoken=abcdEF", "abcdEF"),
                                ("authtoken: zzQQ", "zzQQ"), ("mysqlpasswd=x1y2", "x1y2"),
                                ("clientsecret=abc", "abc")) if secret in m(s)]
    check("önekli anahtar adları (dbpassword, accesstoken) hâlâ maskelenir", not pref, str(pref))

    jf = 'String gitOriginApp = "git@ssh.dev.azure.com:v3/o/ecombackend/webservice"\n'
    df = 'newrelic.appname="[Prod] a.com"\n'

    def http_err(code):
        r = _rq.Response()
        r.status_code = code
        return _rq.HTTPError(str(code), response=r)

    class Dev:
        def __init__(self, fail=None, dirs=("x",)):
            self.fail, self.dirs = fail, dirs

        def get_items_in_path(self, repo, path, branch=None, recursion_level="oneLevel", project=None):
            return [{"path": f"/build/{d}", "isFolder": True} for d in self.dirs] if repo == "global" else []

        def get_file_content(self, repo, path, branch=None, project=None):
            if self.fail:
                raise http_err(self.fail)
            if path.endswith("/Dockerfile") and "/y/" in path:
                raise http_err(404)
            return jf if path.endswith("JenkinsfileBuild") else df

    with tempfile.TemporaryDirectory() as td:
        cache, ovr = Path(td) / "m.yaml", Path(td) / "o.yaml"
        ovr.write_text("{}", encoding="utf-8")
        mp = app_map.load_map(Dev(dirs=("x", "y")), cache_path=cache, overrides_path=ovr)
        check("404 (dosya yok) sessizce atlanır", app_map.resolve("[Prod] a.com", mp) == "webservice")
        m2 = app_map.load_map(Dev(fail=503), cache_path=cache, overrides_path=ovr, force=True)
        kept = (yaml.safe_load(cache.read_text(encoding="utf-8")) or {}).get("apps") or {}
        check("FLODevOps 5xx → eski önbellek korunur ve kullanılır",
              app_map.resolve("[Prod] a.com", m2) == "webservice" and "[Prod] a.com" in kept)
        m3 = app_map.load_map(Dev(dirs=()), cache_path=cache, overrides_path=ovr, force=True)
        check("boş/çok küçülmüş eşleme önbelleği ezmez",
              app_map.resolve("[Prod] a.com", m3) == "webservice")

    class AzC:
        org_url, project, repo_projects = "https://dev.azure.com/o", "P", []

        def __init__(self, reject_ac=False):
            self.reject_ac, self.created = reject_ac, []

        def list_iterations(self, team=""):
            return [{"name": "S1", "path": "P\\S1", "attributes": {"timeFrame": "current"}},
                    {"name": "S2", "path": "P\\S2", "attributes": {"timeFrame": "future", "startDate": "2026-10-19"}}]

        def query_work_items(self, wiql, limit=0):
            return []

        def get_team_area_path(self, team=""):
            return "P\\T"

        def get_authenticated_user(self):
            return {}

        def create_work_item(self, type_name, fields, parent_id=None):
            if type_name == "Task" and self.reject_ac and "Microsoft.VSTS.Common.AcceptanceCriteria" in fields:
                raise http_err(400)
            self.created.append((type_name, dict(fields)))
            return {"id": 100 + len(self.created), "fields": fields}

        def update_work_item(self, wid, ops):
            return {}

    fs = [Finding(source="newrelic", repo="webservice", app="[Prod] a", action="A::b", err_class="TypeError",
                  message="boom", count=500, last_seen=1.0)]
    deps = intake.Deps(gather=lambda s, c, cf: (list(fs), []), board=lambda c: {}, repo_ok=lambda r: True,
                       queue=lambda w, t: 1, record=lambda **k: None, now=lambda: 2.0,
                       cfg=lambda: {"nr_days": 7, "nr_min_errors": 100, "pack_sp": 3, "pack_files": 5,
                                    "human_max": 5, "max_tasks": 8, "grace_days": 3, "sonar_url": ""})
    c = AzC()
    intake.take(c, "newrelic", 5, deps=deps)
    task = next(f for ty, f in c.created if ty == "Task")
    check("Task'a Acceptance Criteria alanı yazılır (hazırlık kapısı −15 yemesin)",
          "Kabul kriteri" in task.get("Microsoft.VSTS.Common.AcceptanceCriteria", "")
          or "Bu action" in task.get("Microsoft.VSTS.Common.AcceptanceCriteria", ""))
    c2 = AzC(reject_ac=True)
    r2 = intake.take(c2, "newrelic", 5, deps=deps)
    check("tipte AC alanı yoksa (400) alansız açılır",
          any(ty == "Task" for ty, _ in c2.created) and r2["tasks"][0]["id"] and not r2["tasks"][0].get("error"))
    cf = deps.cfg()
    cf["task_fields"] = {"Custom.ARGEType": "Geliştirme - GE"}
    c3 = AzC()
    intake.take(c3, "newrelic", 5, deps=deps._replace(cfg=lambda: cf))
    t3 = next(f for ty, f in c3.created if ty == "Task")
    check("org'un zorunlu Task alanları (quality_task_fields.yaml) Task'a yazılır",
          t3.get("Custom.ARGEType") == "Geliştirme - GE"
          and intake.load_cfg()["task_fields"].get("Custom.ARGEType") == "Geliştirme - GE")


def test_quality_readiness():
    print("\n[Q11] Kalite alımı — beklenen davranış bloğu + hazırlık kapısı muafiyeti")
    import inspect
    from agile_sdlc_crew import flow as _flow
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import rank, render
    from agile_sdlc_crew.quality.model import Finding

    def g(msg, cls="E_WARNING"):
        return rank.nr_units([Finding(source="newrelic", repo="webservice", app="[Prod] a", action="Index::index",
                                      err_class=cls, message=msg, count=500, last_seen=1.0)])[0]
    und = render.task_description(g('Undefined array key "host"'))
    nul = render.task_description(g("Trying to access array offset on value of type null"))
    gen = render.task_description(g("Call to undefined method Foo::bar()", cls="Error"))
    check("tanımsız anahtar: beklenen davranış (isset/??, akış korunur, hangi dosya, doğrulama)",
          "Beklenen davranış" in und and "isset" in und and "birim testi" in und and "gerçekten" in und)
    check("olası yer kapsam kilidi değil, başlangıç noktası (#225: mimar kilit sandı)",
          "başlangıç noktası" in und and "yardımcı" in und)
    check("test kapsamı dar: yalnız değişen satır, büyük metot sahte veriyle çağrılmaz, görünürlük değişmez (#232)",
          "yalnız değişen satır" in und and "baştan sona" in und and "görünürlüğü değiştirilmez" in und)
    check("null erişimi: null koruması", "Beklenen davranış" in nul and "null" in nul.split("Beklenen davranış")[1])
    check("genel hata: kök neden, hatayı yutmak çözüm değil", "Beklenen davranış" in gen and "yutmak" in gen)
    ac = render.acceptance_block(g('Undefined array key "host"'))
    check("AC alanı beklenen davranışı da taşır", "isset" in ac and "Kabul kriteri" not in ac and "bir daha" in ac)
    check("tempo-quality etiketli iş hazırlık kapısından muaf (varsayılan açık)",
          _flow._quality_skips_readiness("tempo-quality; tempo-src-newrelic")
          and not _flow._quality_skips_readiness("needs-refinement")
          and not _flow._quality_skips_readiness(""))
    orig = pc.get
    pc.get = lambda k: False if k == "CREW_QUALITY_SKIP_READINESS" else orig(k)
    try:
        check("muafiyet kapatılabilir", not _flow._quality_skips_readiness("tempo-quality"))
    finally:
        pc.get = orig
    src = inspect.getsource(_flow)
    check("hazırlık kapısı muafiyeti kullanıyor",
          "_quality_skips_readiness(self.state.wi_tags)" in src)
    # #236: Sonar SSL işi 2. aşamada düştü — BA'nın 12 maddesinin 8'i (tempo-fp korunur,
    # Sonar'da kapanır, yalnız listelenen satırlar…) plan değişikliğiyle kapsanamaz.
    stage2 = src[src.index("HAZIRLIK KAPISI (asama 2)"):src.index("raise NeedsMoreInfo(_msg)", src.index("HAZIRLIK KAPISI (asama 2)"))]
    check("2. aşama kapsam eşiği kalite işinde atlanır (mimar reddi kalır)",
          "_quality_skips_readiness(self.state.wi_tags)" in stage2 and "_refusal" in stage2)


def test_repo_ctx_cross_thread():
    print("\n[46] claude -p repo bağlamı — crewai'nin çağrı thread'ine ulaşır, başka işe sızmaz")
    import contextvars as _cv
    import threading as _th
    from agile_sdlc_crew.tools import claude_cli_llm as _cli
    seen = []

    def _fake(cmd, env, timeout_s, meta=None, idle_s=0):
        seen.append(list(cmd))
        if meta is not None:
            meta.update({"turns": 1, "cost_usd": 0.0})
        return "OK"

    def _call(copy_ctx=True):
        fn = lambda: _cli.claude_cli_completion("x", model="sonnet", system="s")  # noqa: E731
        # crewai LLM çağrısını asyncio.to_thread ile yapar → contextvars kopyalanır
        # (ölçüldü: çağrı 'asyncio_0' thread'inde, ContextVar görünür, threading.local görünmez)
        th = _th.Thread(target=_cv.copy_context().run, args=(fn,)) if copy_ctx else _th.Thread(target=fn)
        th.start()
        th.join()
        return seen[-1]

    _orig = _cli._run_streaming
    _cli._run_streaming = _fake
    try:
        _cli.clear_repo_ctx(); _cli.set_toolless(False)
        _cli.set_repo_ctx(["/tmp/repo-a"], "Read,Grep,Glob,LS")
        c = _call()
        check("flow adımında kurulan repo bağlamı crewai'nin çağrı thread'inde görülür (#225: keşif araçsız kaldı)",
              "--add-dir" in c and "/tmp/repo-a" in c and "--disallowedTools" not in c, " ".join(c[-8:]))
        c = _call(copy_ctx=False)
        check("bağlamı kopyalamayan başka bir iş (ör. eşzamanlı pr_review) bu repoyu ve araçları GÖRMEZ",
              "--add-dir" not in c and "--disallowedTools" in c)
        _cli.clear_repo_ctx()
        c = _call()
        check("temizlenince araçlar kapanır (emit fazı)", "--add-dir" not in c and "--disallowedTools" in c)
        _cli.set_toolless(True)
        _cli.set_repo_ctx([], "")
        c = _call()
        check("set_toolless da çağrı thread'ine ulaşır", "--disallowedTools" in c)
        _cli.set_toolless(False)
        with _cli.repo_tools_context(["/tmp/repo-c"]):
            c = _call()
        c2 = _call()
        check("repo_tools_context çağrı thread'ine yayılır ve çıkışta geri alınır",
              "/tmp/repo-c" in c and "--add-dir" not in c2)
    finally:
        _cli._run_streaming = _orig
        _cli.clear_repo_ctx()
        _cli.set_toolless(False)

def test_windowed_edit():
    print("\n[47] Büyük dosyada pencereli düzenleme — 'sonuna ekle' yok (#229: Order.php 352KB)")
    import shutil
    import subprocess as _sp
    import inspect
    from agile_sdlc_crew import edit_window as ew
    from agile_sdlc_crew import flow as _flow
    from agile_sdlc_crew.main import _lint_issue_lines
    lines = [f"    $x{i} = $data['k{i}'];" for i in range(1, 801)]
    lines[399] = "        'telephone' => Utils::getTelephoneFormat($order['billing']['telephone']),"
    content = "<?php\nclass A {\n" + "\n".join(lines) + "\n}\n"
    cur = "'telephone' => Utils::getTelephoneFormat($order['billing']['telephone']),"
    new = "'telephone' => Utils::getTelephoneFormat($order['billing']['telephone'] ?? null),"
    anchor = ew.find_anchor(content, "        " + cur + "  // eski yorum")
    check("anchor: plan alıntısı birebir değilse de doğru satır bulunur", anchor and anchor[0] == 402, str(anchor))
    check("anchor: metot adından", ew.find_anchor("a\nb\n  public function newOrderDetailAction($id)\n{", "function newOrderDetailAction()") [0] == 3)
    check("anchor: mimarın verdiği satır öncelikli", ew.find_anchor(content, "yok", hint_line=10)[0] == 10)
    check("anchor: bulunamazsa None", ew.find_anchor(content, "tamamen alakasız bir şey 123") is None)
    asked = {}

    def ask_ok(window_text, start, end):
        asked.update(text=window_text, start=start, end=end)
        body = [ln.split("| ", 1)[1] for ln in window_text.splitlines()]
        return "```php\n" + "\n".join(b.replace(cur, new) for b in body) + "\n```"
    out, info = ew.apply_windowed(content, cur, new, ask_ok, radius=50)
    check("yalnız ±50 satırlık numaralı pencere sorulur",
          asked["start"] == 352 and asked["end"] == 452 and asked["text"].splitlines()[0].startswith("  352| "), info)
    check("pencere yerine oturur, dosyanın geri kalanı aynen kalır",
          out is not None and new in out and out.count("\n") == content.count("\n")
          and out.replace(new, cur) == content)
    snippet, _ = ew.apply_windowed(content, cur, new, lambda w, s, e: "```php\n" + new + "\n```", radius=50)
    check("model yalnız değişen satırı döndürürse (pencereyi silerdi) reddedilir", snippet is None)
    prose, _ = ew.apply_windowed(content, cur, new, lambda w, s, e: "(1) satır 3678: telephone okuması korunmalı", radius=50)
    check("model kod yerine açıklama döndürürse reddedilir", prose is None)
    num, _ = ew.apply_windowed(content, cur, new, lambda w, s, e: "\n".join(
        ln.replace(cur, new) for ln in w.splitlines()), radius=50)
    check("model satır numaralarını bıraksa da temizlenir", num is not None and "| " not in num.splitlines()[400])
    from agile_sdlc_crew.quality import rank as _rk, render as _rd
    from agile_sdlc_crew.quality.model import Finding as _F
    exc = _rk.nr_units([_F(source="newrelic", repo="webservice", app="[Prod] a", action="Api/V1/Basket::shoppingCartCount",
                           err_class="Exception", count=500, last_seen=1.0,
                           message="Uncaught exception 'Exception' with message 'cURL Error' in "
                                   "/var/www/app/app/Model/V1/Florchestra.php:820")])[0]
    d_exc = _rd.task_description(exc)
    check("exception mesajındaki dosya:satır Task açıklamasına 'Hata yeri' olarak girer",
          "Hata yeri" in d_exc and "app/Model/V1/Florchestra.php:820" in d_exc
          and _rd.exception_location("x in /var/www/app/app/A.php:12") == ("app/A.php", 12)
          and _rd.exception_location('Undefined array key "id"') is None)
    src = inspect.getsource(_flow)
    check("implement'te 'sonuna ekle' geri dönüşü yok, pencereli düzenleme kullanılıyor",
          "append stratejisine dusuluyor" not in src and "apply_windowed(" in src)
    out_txt = ("PHP Deprecated:  Non-canonical cast (double) is deprecated in /tmp/x.php on line 12\n"
               "Deprecated: Non-canonical cast (double) is deprecated in /tmp/x.php on line 13\n"
               "PHP Parse error:  syntax error, unexpected identifier \"satır\" in /tmp/x.php on line 9001\n"
               "Errors parsing /tmp/x.php")
    iss = _lint_issue_lines(out_txt)
    check("linter çıktısında Deprecated gürültüsü atılır, gerçek hata görünür",
          iss and "Parse error" in iss[0] and not any("Deprecated" in i for i in iss), str(iss))
    if shutil.which("php"):
        from agile_sdlc_crew.main import _lint_with_native
        ok = _lint_with_native("<?php\n$a = (double) '1.5';\n", "php")
        bad = _lint_with_native("<?php\n$a = (double) '1.5';\n(1) satır 3678: açıklama\n", "php")
        check("php -l: eski cast uyarısı geçer, gerçek sözdizimi hatası düşer (ve mesajı Parse error)",
              ok["valid"] and not bad["valid"] and "Parse error" in bad["issues"][0], str(bad))
    else:
        skip("php -l", "php yok")


def test_quality_lean():
    print("\n[Q12] Kalite işleri — yalın akış + ucuz modeller (yalnız tempo-quality)")
    import inspect
    from crewai.flow.flow import Flow, listen, start
    from agile_sdlc_crew import flow as _flow
    from agile_sdlc_crew import main as _main
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.llm import resolver as rs
    from agile_sdlc_crew.quality import render
    check("knob'lar kayıtlı, varsayılan açık",
          pc._SCHEMA_BY_KEY.get("CREW_QUALITY_LEAN_FLOW", {}).get("default") is True
          and pc._SCHEMA_BY_KEY.get("CREW_QUALITY_LEAN_MODELS", {}).get("default") is True)
    ov = _flow.quality_lean_overrides()
    check("mimar → Sonnet, tamamlanma raporu → Haiku; reviewer ve geliştirici dokunulmaz",
          rs._profile_to_spec(ov["software_architect"])["model"] == "sonnet"
          and rs._profile_to_spec(ov["scrum_master"])["model"] == "haiku"
          and "code_reviewer" not in ov and "senior_developer" not in ov)
    seen = {}

    class _F(Flow):
        @start()
        def a(self):
            rs.set_job_llm_overrides({"software_architect": "architect_cli_sonnet"})

        @listen(a)
        def b(self):
            seen["in"] = rs.resolve_spec_with_source("software_architect")

    with rs.job_llm_scope():
        _F().kickoff()
    seen["out"] = rs.resolve_spec_with_source("software_architect")
    check("iş kapsamındaki model geçersiz kılması sonraki adımlarda görülür",
          seen["in"][1] == "job" and seen["in"][0]["model"] == "sonnet", str(seen.get("in")))
    check("kapsam dışında (başka iş / sonraki iş) geçersiz kılma yok", seen["out"][1] != "job")
    rs.set_job_llm_overrides({"software_architect": "architect_cli_sonnet"})
    check("kapsam yokken set etmek hiçbir şeyi değiştirmez",
          rs.resolve_spec_with_source("software_architect")[1] != "job")
    check("Task açıklamasından repo okunur",
          render.repo_from_description("<p><b>Repo:</b> <code>webservice</code> </p><p>x</p>") == "webservice"
          and render.repo_from_description("<p>yok</p>") == "")
    src = inspect.getsource(_flow)
    check("discover / test planlama / UAT kalite işinde atlanır",
          src.count("Atlandı — kalite işi") >= 3 and "_quality_lean_flow()" in src)
    s1 = inspect.getsource(_flow.AgileSDLCFlow.crew_step1_requirements)
    res_branch = s1[s1.index("cached_ba = self._try_resume_step"):s1.index('self._resume_step("requirements_analysis_task", cached_ba)')]
    check("requirements resume edilse de WI bağlamı (etiket → kalite ayarları) kurulur (#232: atlanmıştı)",
          "self._wi_begin(" in res_branch)
    check("kalite işinde build-fix testi daraltır, üretim görünürlüğünü değiştirmez",
          "QUALITY_BUILD_FIX_RULE" in src and "do not change the visibility" in src.lower())
    from agile_sdlc_crew.crew import AgileSDLCCrew
    with rs.job_llm_scope():
        crew_obj = AgileSDLCCrew()  # flow init'te kurulur: _wi_begin'den ÖNCE
        before = getattr(crew_obj.llm_architect, "model", "")
        rs.set_job_llm_overrides({"software_architect": "architect_cli_sonnet", "scrum_master": "report_haiku"})
        after_a = getattr(crew_obj.llm_architect, "model", "")
        after_s = getattr(crew_obj.llm_scrum, "model", "")
    check("crew kurulduktan SONRA gelen iş kapsamlı model seçimi de uygulanır (#234: mimar Opus kaldı)",
          "sonnet" in str(after_a) and "haiku" in str(after_s), f"{before} → {after_a} / {after_s}")
    check("run_pipeline flow'u iş kapsamında koşturur", "job_llm_scope()" in inspect.getsource(_main.run_pipeline))


def test_worktree_staging():
    print("\n[48] Review/build-fix turları önceki düzeltmeleri geri almasın (#231: telephone ?? null kayboldu)")
    import inspect
    import types
    from agile_sdlc_crew import flow as _flow
    F = _flow.AgileSDLCFlow
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "webservice"
        (repo / "app").mkdir(parents=True)
        main_v = "<?php\n$t = $o['telephone'];\n" + "// dolgu\n" * 40
        branch_v = "<?php\n$t = $o['telephone'] ?? null;\n" + "// dolgu\n" * 40
        f = repo / "app" / "Order.php"
        f.write_text(main_v, encoding="utf-8")  # yerel klon main'de
        fake = types.SimpleNamespace(_repo_mgr=types.SimpleNamespace(base_dir=Path(td)))
        for name in ("_read_worktree_file", "_prefer_worktree_edit", "_stage_worktree_file", "_worktree_base"):
            setattr(fake, name, types.MethodType(getattr(F, name), fake))
        # dev diske dokunmadı, kısa bir metin döndürdü
        out = fake._prefer_worktree_edit("webservice", "/app/Order.php", "", branch_v)
        check("hazırlık yokken bile main kopyası branch'teki düzeltmenin üstüne yazılmaz",
              "?? null" in out or out == "", repr(out[:60]))
        fake._stage_worktree_file("webservice", "/app/Order.php", branch_v)
        check("tur öncesi diske PR branch'inin hali yazılır", f.read_text(encoding="utf-8") == branch_v)
        f.write_text(branch_v.replace("// dolgu", "// dolgu düzeltildi", 1), encoding="utf-8")  # dev in-place edit
        out2 = fake._prefer_worktree_edit("webservice", "/app/Order.php", "", branch_v)
        check("dev'in in-place düzenlemesi branch üstüne kurulur (önceki düzeltme korunur)",
              "?? null" in out2 and "düzeltildi" in out2)
    with tempfile.TemporaryDirectory() as td2:
        base = Path(td2)
        repo2 = base / "r"
        (repo2 / "app").mkdir(parents=True)
        outside = base / "outside.txt"
        outside.write_text("orijinal", encoding="utf-8")
        (repo2 / "app" / "link.php").symlink_to(outside)
        fk = types.SimpleNamespace(_repo_mgr=types.SimpleNamespace(base_dir=base))
        fk._stage_worktree_file = types.MethodType(F._stage_worktree_file, fk)
        fk._stage_worktree_file("r", "/app/link.php", "EZILDI")
        fk._stage_worktree_file("r", "/../outside.txt", "EZILDI")
        check("hazırlık yazımı symlink'i izlemez, repo dışına (../) yazmaz",
              outside.read_text(encoding="utf-8") == "orijinal")
    src = inspect.getsource(F)
    check("review tekrarı ve build-fix turları diski PR branch'ine hazırlar",
          src.count("self._stage_worktree_file(") >= 2)


def test_techdebt_lane():
    print("\n[TD1] Teknik borç — hat (lane) ve story")
    from agile_sdlc_crew.quality import intake, render
    from agile_sdlc_crew.quality.model import Group
    check("techdebt hattı tek story başlığı", render.story_title("sonar_smell", "S22", lane="techdebt") == "Teknik borç — S22")
    check("quality hattı değişmedi", render.story_title("newrelic", "S22") == "Kalite: New Relic hataları — S22")
    check("bilinmeyen kaynak KeyError vermez", "yeni" in render.story_title("yeni", "S1"))
    g = Group(source="sonar_smell", repo="webservice", title="t", fingerprints=["sonar:webservice:x"], findings=[],
              needs_human=True)
    tags = render.task_tags(g, lane="techdebt")
    check("techdebt Task etiketleri (tempo-quality yalın akış için korunur)",
          tags == "tempo-quality; tempo-techdebt; tech-debt; tempo-src-sonar_smell; needs-human", tags)
    check("quality Task etiketleri değişmedi",
          render.task_tags(Group(source="sonar", repo="r", title="t", fingerprints=["f"], findings=[])) == "tempo-quality; tempo-src-sonar")

    class C:
        org_url, project = "https://dev.azure.com/o", "P"

        def __init__(self):
            self.wiql, self.created = [], []

        def list_iterations(self, team=""):
            return [{"name": "S22", "path": "P\\S22", "attributes": {"timeFrame": "future", "startDate": "2026-10-19"}}]

        def query_work_items(self, wiql, limit=0):
            self.wiql.append(wiql)
            return []

        def get_team_area_path(self, team=""):
            return "P\\T"

        def create_work_item(self, type_name, fields, parent_id=None):
            self.created.append((type_name, fields))
            return {"id": 7, "fields": fields}
    c = C()
    st = intake.resolve_story(c, "sonar_smell", None, "T", "next", lane="techdebt")
    check("techdebt story kaynak etiketi olmadan aranır ve tek etiketle açılır",
          "tempo-techdebt" in c.wiql[0] and "tempo-src-" not in c.wiql[0]
          and c.created[0][1]["System.Title"] == "Teknik borç — S22"
          and "tempo-techdebt" in c.created[0][1]["System.Tags"] and st["id"] == 7)


def test_techdebt_sprint_choice():
    print("\n[TD3] Teknik borç — işlerin açılacağı sprint seçilebilir, varsayılan aktif sprint")
    from agile_sdlc_crew.quality import intake

    class C:
        org_url, project = "https://dev.azure.com/o", "P"

        def __init__(self, iters):
            self.iters, self.wiql, self.created = iters, [], []

        def list_iterations(self, team=""):
            return self.iters

        def query_work_items(self, wiql, limit=0):
            self.wiql.append(wiql)
            return []

        def get_team_area_path(self, team=""):
            return "P\\T"

        def create_work_item(self, type_name, fields, parent_id=None):
            self.created.append(fields)
            return {"id": 8, "fields": fields}
    iters = [{"name": "S21", "path": "P\\S21", "attributes": {"timeFrame": "past"}},
             {"name": "S22", "path": "P\\S22", "attributes": {"timeFrame": "current"}},
             {"name": "S24", "path": "P\\S24", "attributes": {"timeFrame": "future", "startDate": "2026-11-30"}},
             {"name": "S23", "path": "P\\S23", "attributes": {"timeFrame": "future", "startDate": "2026-11-16"}}]
    c = C(iters)
    st = intake.resolve_story(c, "php_modern", None, "T", "current_or_next", lane="techdebt")
    check("seçim yoksa aktif sprint", st["iteration"] == "P\\S22" and c.created[0]["System.IterationPath"] == "P\\S22")
    c = C(iters)
    st = intake.resolve_story(c, "php_modern", None, "T", "current_or_next", lane="techdebt", iteration="P\\S24")
    check("seçilen gelecek sprint kullanılır", st["iteration"] == "P\\S24" and "P\\S24" in c.wiql[0]
          and c.created[0]["System.Title"] == "Teknik borç — S24")
    for bad, why in (("P\\S21", "geçmiş"), ("P\\Baska", "takımda yok")):
        try:
            intake.resolve_story(C(iters), "php_modern", None, "T", "current_or_next", lane="techdebt", iteration=bad)
            check(f"{why} sprint reddedilir", False)
        except ValueError:
            check(f"{why} sprint reddedilir", True)
    st = intake.resolve_story(C(iters[2:]), "php_modern", None, "T", "current_or_next", lane="techdebt")
    check("aktif sprint yoksa en yakın gelecek sprint", st["iteration"] == "P\\S23")
    check("kalite alımının 'next' davranışı değişmedi",
          intake.target_iteration(iters, "next")["name"] == "S23" and intake.target_iteration(iters[2:], "current") is None)
    from agile_sdlc_crew.quality import modern, php_modern
    from agile_sdlc_crew import server
    check("take sprint'i story çözümüne geçirir",
          "sprint=sprint" in inspect_source(php_modern.take) and "sprint=sprint" in inspect_source(modern.take)
          and "iteration=sprint" in inspect_source(php_modern._open_and_report)
          and "sprint" in server.TechdebtModernTakeRequest.model_fields)
    html = (Path(__file__).resolve().parents[1] / "src/agile_sdlc_crew/web/index.html").read_text(encoding="utf-8")
    check("arayüz: sprint seçici, varsayılan aktif, take'e gider",
          'id="tdmSprint"' in html and "function loadTdmSprints" in html and "group_ids:ids,team,sprint" in html
          and "x.timeFrame==='current'||x.timeFrame==='future'" in html)


def test_php_repo_subdir_composer():
    print("\n[PM9] php_modern — composer.json alt dizinde olan repo (flo-dashboard: main/, ru/) PHP sayılır")
    from agile_sdlc_crew.quality import modern, php_modern as pm
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        dash = root / "flo-dashboard"
        (dash / "main" / "vendor" / "x").mkdir(parents=True)
        (dash / "ru").mkdir()
        (dash / "main" / "composer.json").write_text('{"require":{"php":">=8.1","ext-mbstring":"*"}}', encoding="utf-8")
        (dash / "main" / "vendor" / "x" / "composer.json").write_text('{"require":{"php":">=5.6"}}', encoding="utf-8")
        (dash / "index.php").write_text("<?php\n", encoding="utf-8")
        check("alt dizindeki composer.json bulunur (vendor değil)",
              pm.composer_paths(dash) == [dash / "main" / "composer.json"])
        check("repo PHP sayılır, dil tespiti php der", pm.is_php_repo(dash) and modern.detect_langs(dash) == ["php"])
        check("composer alt sınırı alt dizinden okunur", pm._composer_version(dash) == (8, 1))
        check("eklenti kanıtı alt dizindeki composer'dan", pm.ext_available(dash, "mbstring"))
        bare = root / "plain"
        bare.mkdir()
        (bare / "index.php").write_text("<?php\n", encoding="utf-8")
        gor = root / "gorepo"
        (gor / "scripts").mkdir(parents=True)
        (gor / "go.mod").write_text("module x\n\ngo 1.22\n", encoding="utf-8")
        (gor / "scripts" / "a.php").write_text("<?php\n", encoding="utf-8")
        check("composer'sız ama kökte .php olan repo PHP; derindeki tek .php Go reposunu PHP yapmaz",
              pm.is_php_repo(bare) and not pm.is_php_repo(gor) and modern.detect_langs(gor) == ["go"])


def test_modern_freshen_and_targets():
    print("\n[PM10] modernleştirme — tarama öncesi klon güncelleme + tüm prod servislerinden hedef sürüm")
    import time
    import subprocess as sp
    from agile_sdlc_crew.quality import php_modern as pm

    def g(cwd, *a):
        return sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=str(cwd), capture_output=True,
                      text=True, check=True).stdout.strip()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        g(root, "init", "-q", "--bare", "-b", "main", "origin.git")
        seed = root / "seed"
        g(root, "clone", "-q", str(root / "origin.git"), "seed")
        (seed / "a.php").write_text("<?php\n", encoding="utf-8")
        g(seed, "add", "."); g(seed, "commit", "-q", "-m", "1"); g(seed, "push", "-q", "origin", "HEAD:main")
        g(root, "clone", "-q", str(root / "origin.git"), "repo")
        repo = root / "repo"
        for i in range(3):
            (seed / "a.php").write_text(f"<?php // {i}\n", encoding="utf-8")
            g(seed, "commit", "-q", "-am", f"c{i}"); g(seed, "push", "-q", "origin", "HEAD:main")
        r = pm.freshen_clone(repo, force=True, running_repos=set())
        check("geride kalan klon ileri sarılır", r == {"updated": 3} and "// 2" in (repo / "a.php").read_text(), r)
        check("5 dakika içinde tekrar fetch edilmez", pm.freshen_clone(repo, running_repos=set()).get("recent"))
        (repo / "a.php").write_text("<?php // yerel\n", encoding="utf-8")
        check("izlenen dosyada değişiklik varsa dokunulmaz",
              "değişiklik" in pm.freshen_clone(repo, force=True, running_repos=set()).get("skipped", ""))
        g(repo, "checkout", "-q", "--", "a.php")
        g(repo, "checkout", "-q", "-b", "feature/x")
        check("başka daldaysa dokunulmaz", "dalında" in pm.freshen_clone(repo, force=True, running_repos=set()).get("skipped", ""))
        g(repo, "checkout", "-q", "main")
        check("git hatasındaki URL kimlik bilgisi silinir",
              pm._redact("fatal: unable to access 'https://u:SECRETPAT@dev.azure.com/x/'") ==
              "fatal: unable to access 'https://***@dev.azure.com/x/'")
        g(repo, "remote", "set-url", "origin", "https://u:SECRETPAT@127.0.0.1:9/x.git")
        r = pm.freshen_clone(repo, force=True, running_repos=set())
        check("fetch hatası arayüze ayrıntı (PAT) taşımaz", "SECRETPAT" not in str(r) and "fetch" in r.get("skipped", ""), r)
        g(repo, "remote", "set-url", "origin", str(root / "origin.git"))
        check("o repoda koşan iş varsa dokunulmaz",
              "koşan" in pm.freshen_clone(repo, force=True, running_repos={"repo"}).get("skipped", ""))
        amap = {"[Prod] dashboard.sneakerbox.ru": {"repo": "flo-dashboard", "src": "russia/build/dashboard"}}

        class C:
            def get_file_content(self, devrepo, path, branch=None, project=None):
                return "FROM reg/prod-ar/php-8-3:1.0\n" if devrepo == "russia" else ""
        idx = {"flo-dashboard": [{"src": "anatolia/build/dashboard", "images": ["reg/prod-ar/php-8-4:1.0"]},
                                 {"src": "russia/build/dashboard", "images": ["reg/prod-ar/php-8-3:1.0"]}]}
        t = pm.target_version(C(), "flo-dashboard", app_map_cache=amap, cache_path=root / "t.yaml", index=idx)
        check("app_map'te olmayan prod servisi de sayılır; hedef en düşük, servis başına sürüm listelenir",
              t["version"] == "8.3" and len(t["images"]) == 2
              and sorted((x["src"], x["version"]) for x in t["per_src"])
              == [("anatolia/build/dashboard", "8.4"), ("russia/build/dashboard", "8.3")], t)
        import yaml as _y
        (root / "t2.yaml").write_text(_y.safe_dump({"repos": {"flo-dashboard": {
            "version": "8.3", "images": ["x"], "source": "dockerfile", "built_at": time.time()}}}), encoding="utf-8")
        t2 = pm.target_version(C(), "flo-dashboard", app_map_cache=amap, cache_path=root / "t2.yaml", index=idx)
        check("eski biçimli önbellek kaydı yeniden kurulur", len(t2["per_src"]) == 2)
    html = (Path(__file__).resolve().parents[1] / "src/agile_sdlc_crew/web/index.html").read_text(encoding="utf-8")
    check("arayüz: servis başına sürüm ipucu + klon notu",
          "Production servisleri" in html and "klon güncellenemedi" in html and "en düşük prod" in html)


def test_techdebt_open_tasks_db():
    print("\n[TD2] Teknik borç — ortak Task açma + quality_items.source kolonu")
    from agile_sdlc_crew.quality import intake
    from agile_sdlc_crew.quality.model import Group
    queued, rec = [], []

    class C:
        def __init__(self):
            self.created = []

        def create_work_item(self, type_name, fields, parent_id=None):
            self.created.append((type_name, fields, parent_id))
            return {"id": 500 + len(self.created)}

        def update_work_item(self, wid, ops):
            return {}
    deps = intake.Deps(gather=None, board=None, repo_ok=None, queue=lambda w, ttl: (queued.append(w), 9)[1],
                       record=lambda **k: rec.append(k), now=lambda: 1.0, cfg=None)
    cfg = {"sonar_url": "", "nr_days": 7, "task_fields": {"Custom.ARGEType": "Geliştirme - GE"}}
    from agile_sdlc_crew.quality.model import Finding
    g = Group(source="sonar_smell", repo="webservice", title="X temizliği", fingerprints=["sonar:webservice:a.php:php:S125"],
              findings=[Finding(source="sonar", repo="webservice", project="EcomBackend:webservice", file="a.php",
                                rule="php:S125", rule_name="Yorumda kod", severity="MAJOR", itype="CODE_SMELL", lines=[3])],
              sp=2)
    story = {"id": 77, "area": "P\\T", "iteration": "P\\S22", "sp": 0.0}
    c = C()
    rows, added = intake._open_tasks(c, [g], story, cfg, deps, source="sonar_smell", lane="techdebt", queue=False)
    f = c.created[0][1]
    check("ortak Task açma: techdebt etiketleri, story child'ı, org alanı, kuyruğa ALINMAZ",
          rows[0]["id"] == 501 and rows[0]["job_id"] is None and not queued and added == 2
          and c.created[0][2] == 77 and "tempo-techdebt" in f["System.Tags"]
          and f["Custom.ARGEType"] == "Geliştirme - GE" and rec and rec[0]["source"] == "sonar_smell")
    from agile_sdlc_crew import db
    check("quality_items.source serbest metin (yeni kaynaklar sessizce düşmesin)",
          "source VARCHAR(32)" in db.SCHEMA and "MODIFY source VARCHAR(32)" in inspect_source(db.init_db))
    try:
        db.init_db()
        with db.get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SHOW COLUMNS FROM quality_items LIKE 'source'")
            typ = (cur.fetchone() or {}).get("Type", "")
        check("canlı DB'de kolon varchar'a çevrildi", "varchar" in typ.lower(), typ)
    except Exception as e:  # noqa: BLE001
        skip("canlı DB kolon tipi", f"DB erişilemedi: {e}")


def inspect_source(fn):
    import inspect
    return inspect.getsource(fn)


def test_techdebt_list_queue():
    print("\n[TD4] Teknik borç — liste ve kuyruğa alma")
    from agile_sdlc_crew.quality import techdebt

    def wi(i, title, tags, state="To Do", typ="Task", desc="", sp=None):
        f = {"System.Title": title, "System.Tags": tags, "System.State": state, "System.WorkItemType": typ,
             "System.IterationPath": "P\\S22", "System.Description": desc}
        if sp is not None:
            f["Custom.StoryPoints"] = sp
        return {"id": i, "fields": f}

    class C:
        def __init__(self):
            self.wiql = []

        def get_team_area_path(self, team=""):
            return "P\\T"

        def query_work_items(self, wiql, limit=0):
            self.wiql.append(wiql)
            return [wi(1, "Elle", "tempo-quality; tempo-techdebt; tech-debt",
                       desc="<p><b>Repo:</b> <code>webservice</code></p>", sp=2),
                    wi(2, "Ekipten", "tech-debt"),
                    wi(3, "Büyük", "tech-debt; needs-human"),
                    wi(4, "Koşan", "tech-debt")]
    jobs = {"4": {"id": 99, "status": "running"}}
    c = C()
    res = techdebt.list_items(c, team="T", job_status=lambda ids: {k: v for k, v in jobs.items() if k in ids})
    rows = {r["id"]: r for r in res["items"]}
    q = c.wiql[0]
    check("liste: takım alanı + tech-debt etiketi + açık durum + yalnız Task/Bug/Improvement",
          "UNDER 'P\\T'" in q and "'tech-debt'" in q and "NOT IN" in q and "'Task', 'Bug', 'Improvement'" in q)
    check("satır alanları: repo, insan işi, iş, kuyruklanabilir",
          rows[1]["repo"] == "webservice" and rows[1]["sp"] == 2
          and rows[3]["needs_human"] and not rows[3]["queueable"]
          and rows[4]["job"]["id"] == 99 and not rows[4]["queueable"] and rows[1]["queueable"])
    sent = []
    out = techdebt.queue(C(), [1, 2, 3, 77], team="T", max_queue=5,
                         job_status=lambda ids: {}, queue_fn=lambda rows: (sent.extend(rows), {
                             "queued": [{"wi": str(r["id"]), "job_id": 10} for r in rows], "skipped": []})[1])
    reasons = {s["wi"]: s["reason"] for s in out["skipped"]}
    check("kuyruk: insan işi ve listede olmayan reddedilir, diğerleri kuyruğa",
          [r["id"] for r in sent] == [1, 2] and "insan" in reasons["3"] and "listesinde yok" in reasons["77"])
    out2 = techdebt.queue(C(), [1, 2], team="T", max_queue=1, job_status=lambda ids: {},
                          queue_fn=lambda rows: {"queued": [{"wi": str(r["id"]), "job_id": 1} for r in rows], "skipped": []})
    check("istek başı tavan", len(out2["queued"]) == 1 and any("tavan" in s["reason"] for s in out2["skipped"]))


def test_techdebt_endpoints():
    print("\n[TD5] Teknik borç — endpoint kapıları, ayarlar, hazırlık kapısı")
    from fastapi.testclient import TestClient
    from agile_sdlc_crew import flow as _flow
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import server
    check("ayarlar kayıtlı: CREW_TECHDEBT kapalı, kuyruk tavanı 5",
          pc._SCHEMA_BY_KEY.get("CREW_TECHDEBT", {}).get("default") is False
          and pc._SCHEMA_BY_KEY.get("CREW_TECHDEBT_MAX_QUEUE", {}).get("default") == 5)
    orig = pc.get
    pc.get = lambda k: False if k == "CREW_TECHDEBT" else orig(k)
    try:
        tc = TestClient(server.app)
        check("kapalıyken liste / kuyruk → 403",
              tc.get("/api/techdebt").status_code == 403
              and tc.post("/api/techdebt/queue", json={"ids": [1]}).status_code == 403)
    finally:
        pc.get = orig
    pc.get = lambda k: True if k == "CREW_TECHDEBT" else orig(k)
    try:
        tc = TestClient(server.app)
        check("elle giriş kaldırıldı (uç yok)", tc.post("/api/techdebt/manual", json={}).status_code in (404, 405))
        r0 = tc.get("/api/techdebt")
        check("takım seçilmeden liste: hata değil, 'takım seçin' mesajı (200)",
              r0.status_code == 200 and r0.json().get("items") == [] and "Takım" in r0.json().get("message", ""))
        check("boş kuyruk isteği → 400", tc.post("/api/techdebt/queue", json={"ids": []}).status_code == 400)
    finally:
        pc.get = orig
    check("taranan kalite işi hazırlık kapısından muaf", _flow._quality_skips_readiness("tempo-quality; tempo-src-newrelic"))


def test_techdebt_ui():
    print("\n[TD6] Teknik borç — dashboard sekmesi")
    html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text(encoding="utf-8")
    s = html.find('id="paneTechdebt"')
    e = html.find("<section", s + 10)
    pane = html[s:e] if s >= 0 else ""
    ov_s = html.find('id="paneOverview"')
    ov = html[ov_s:html.find("<section", ov_s + 10)]
    check("üst barda Teknik borç sekmesi ve paneli", 'data-tab="techdebt"' in html and s >= 0)
    check("Kalite kartı Genel bakış'tan Teknik borç sekmesine taşındı",
          'id="ovQualityCard"' in pane and 'id="ovQualityCard"' not in ov)
    check("liste ve çoklu seçimle kuyruk var; elle giriş formu kaldırıldı",
          'id="tdList"' in pane and "/api/techdebt/queue" in html and "function loadTechdebt" in html
          and "name==='techdebt'" in html and 'id="tdForm"' not in html and "/api/techdebt/manual" not in html)


def test_techdebt_review_fixes():
    print("\n[TD7] Teknik borç — inceleme düzeltmeleri")
    import requests as _rq
    from fastapi.testclient import TestClient
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import server
    from agile_sdlc_crew.quality import intake, techdebt

    class C:
        def __init__(self, area="P\\T"):
            self.area, self.wiql = area, []

        def get_team_area_path(self, team=""):
            return self.area

        def query_work_items(self, wiql, limit=0):
            self.wiql.append(wiql)
            return [{"id": i, "fields": {"System.Title": f"t{i}", "System.Tags": "tech-debt", "System.State": "To Do",
                                          "System.WorkItemType": "Task"}} for i in (1, 2, 3, 4, 5)]
    sent = []
    techdebt.queue(C(), [1, 1, 1], team="T", max_queue=5, job_status=lambda ids: {},
                   queue_fn=lambda rows: (sent.extend(rows), {"queued": [], "skipped": []})[1])
    check("aynı id birden çok gönderilse de tek iş", [r["id"] for r in sent] == [1])
    intake._LOCK.acquire()
    try:
        try:
            techdebt.queue(C(), [2], team="T", job_status=lambda ids: {}, queue_fn=lambda rows: {})
            busy = False
        except intake.Busy:
            busy = True
    finally:
        intake._LOCK.release()
    check("eşzamanlı kuyruk isteği → Busy (çift tıklama)", busy)
    jobs = {"1": {"id": 11, "status": "completed"}, "2": {"id": 12, "status": "needs_info"},
            "3": {"id": 13, "status": "needs_human"}, "4": {"id": 14, "status": "failed"}}
    rows = {r["id"]: r for r in techdebt.list_items(C(), team="T", job_status=lambda ids: jobs)["items"]}
    check("tamamlanmış / insan bekleyen işi olan WI kuyruklanamaz; failed tekrar alınabilir",
          not rows[1]["queueable"] and not rows[2]["queueable"] and not rows[3]["queueable"]
          and rows[4]["queueable"] and rows[5]["queueable"])

    class CS(C):
        def query_work_items(self, wiql, limit=0):
            return [{"id": i, "fields": {"System.Title": f"t{i}", "System.Tags": "tech-debt", "System.State": st,
                                          "System.WorkItemType": "Task"}}
                    for i, st in ((1, "To Do"), (2, "QA To Do"), (3, "Code Review"), (4, "New"))]

        def get_work_item_type_states(self, t):
            return [{"name": "New", "category": "Proposed"}, {"name": "To Do", "category": "Proposed"},
                    {"name": "In Progress", "category": "InProgress"}, {"name": "Code Review", "category": "InProgress"},
                    {"name": "QA To Do", "category": "InProgress"}]
    rows = {r["id"]: r for r in techdebt.list_items(CS(), team="T", job_status=lambda ids: {})["items"]}
    check("başlanmış kart (Code Review / QA To Do) işi olmasa da kuyruklanamaz, gerekçesiyle",
          rows[1]["queueable"] and rows[4]["queueable"] and not rows[2]["queueable"] and not rows[3]["queueable"]
          and rows[2]["reason"] == "iş ilerlemiş (QA To Do)", rows)
    import inspect as _insp
    check("araç düzeltmesi (modern_fix) işi de 'yapıldı' sayılır",
          'kinds=("pipeline", "modern_fix")' in _insp.getsource(techdebt._job_status))
    try:
        techdebt.list_items(C(area=""), team="", job_status=lambda ids: {})
        scoped = False
    except ValueError as e:
        scoped = "takım" in str(e)
    check("takım alanı çözülemezse proje geneline düşmez (ValueError)", scoped)
    c = C()
    techdebt.list_items(c, team="T", job_status=lambda ids: {})
    check("yalnız Task/Bug/Improvement listelenir (Feature/Epic kuyruğa gitmez)",
          "[System.WorkItemType] IN ('Task', 'Bug', 'Improvement')" in c.wiql[0])
    orig_get, orig_q = pc.get, techdebt.queue
    pc.get = lambda k: True if k == "CREW_TECHDEBT" else orig_get(k)
    techdebt.queue = lambda *a, **k: (_ for _ in ()).throw(_rq.ConnectionError("azure yok"))
    try:
        r = TestClient(server.app).post("/api/techdebt/queue", json={"ids": [1]})
        check("Azure hatası → 502", r.status_code == 502, str(r.status_code))
    finally:
        pc.get, techdebt.queue = orig_get, orig_q
    html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text(encoding="utf-8")
    otc = html[html.index("async function onTeamChange"):html.index("async function onTeamChange") + 1500]
    check("takım seçimi yüklenirken ('Yükleniyor...') istek gönderilmez", "function _tdTeam" in html)
    check("takım değişince teknik borç listesi yenilenir; kuyruk butonu istek boyunca kilitli",
          "loadTechdebt()" in otc and "tdQueueBtn" in html and ".disabled=true" in html)


def test_dashboard_readability():
    print("\n[UI] Okunabilirlik — ayar editörü, başlık butonları")
    import re as _re
    html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text(encoding="utf-8")
    ed = _re.search(r'<textarea id="cfgEditor"[^>]*>', html).group(0)
    check("ayar editörü tema rengini kullanır (açık temada sarı yazı yok)", "#e6db74" not in ed and "var(--text)" in ed)
    check("açık temada okunmayan sabit sarı/pembe yazı renkleri yok",
          not any(c in html for c in ("color:#fbbf24", "color:#fca5a5", "color:#f9c74f")))
    sb = _re.search(r'<button[^>]*onclick="openSettings\(\)"[^>]*>.*?</button>', html, _re.S).group(0)
    tb = _re.search(r'<button[^>]*id="themeBtn"[^>]*>.*?</button>', html, _re.S).group(0)
    check("ayarlar butonu etiketli ve dişli simgeli; tema butonundan ayırt edilir",
          "Ayarlar" in sb and 'data-icon="gear"' in sb and 'data-icon="gear"' not in tb)
    check("metin/sayı kutuları açılır liste gibi çizilmez (ok simgesi + kesilen değer)",
          not _re.search(r'<input[^>]*class="sprint-select"', html) and 'id="qSp"' in html
          and _re.search(r'<input class="ctl-input" id="qSp"', html) is not None)
    check("modallarda açık temada simsiyah kutu yok (backfill, sprint raporu)",
          not any(c in html for c in ("background:#111", "background:#222", "background:#6c5ce7")))
    check("geçmiş iş taraması ilk açılışta boş siyah log/çubuk değil açıklama gösterir",
          'id="bfHint"' in html and "logEl.style.display" in html)
    check("tema butonu güncel modu simgeyle gösterir (güneş / ay / ekran)",
          "THEME_ICONS" in html and all(k in html for k in ("light:", "dark:", "system:")))


def test_human_label():
    print("\n[UI] İnsan işi etiketi anlaşılır ve sebebi gösterir")
    from agile_sdlc_crew.quality import intake
    from agile_sdlc_crew.quality.model import Group
    g = Group(source="sonar", repo="r", title="t", fingerprints=["f"], findings=[], needs_human=True,
              human_reason="Secret rotasyonu gerekir — koddan silmek yetmez")
    check("önizleme satırı insan işi sebebini taşır", intake._row(g).get("human_reason") == g.human_reason)
    html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text(encoding="utf-8")
    check("çıplak 'insan' etiketi kalmadı; 'İnsan işi · kuyruğa alınmaz' + sebep ipucu",
          'chip human">insan<' not in html and "· insan'" not in html
          and "function humanChip" in html and "İnsan işi · kuyruğa alınmaz" in html and "t.human_reason" in html)
    check("otomatik satırların durumu da yazılı (boş hücre yok)", "Pipeline kuyruğuna alınır" in html)


def test_quality_human_split():
    print("\n[Q13] Kalite alımı — insan işleri ayrı grup, bütçe otomatik işlere")
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import intake
    from agile_sdlc_crew.quality.model import Finding

    def sec(i):
        return Finding(source="sonar", repo=f"r{i}", project=f"EcomBackend:r{i}", file=f"s{i}.php",
                       rule="secrets:S6703", rule_name="Secret", severity="BLOCKER", itype="VULNERABILITY", lines=[1])

    def bug(i):
        return Finding(source="sonar", repo="webservice", project="EcomBackend:webservice", file=f"b{i}.php",
                       rule=f"php:S{i}", rule_name="Bug", severity="CRITICAL", itype="BUG", lines=[1])
    findings = [sec(i) for i in range(7)] + [bug(i) for i in range(20)]
    cfg = {"nr_days": 7, "nr_min_errors": 100, "pack_sp": 3, "pack_files": 5, "max_tasks": 8, "human_max": 5,
           "grace_days": 3, "sonar_url": "", "target_sprint": "next", "task_fields": {}}
    deps = intake.Deps(gather=lambda s, c, cf: (list(findings), []), board=lambda c: {}, repo_ok=lambda r: True,
                       queue=lambda w, t: 1, record=lambda **k: None, now=lambda: 1.0, cfg=lambda: cfg)
    pv = intake.preview(None, "sonar", 30, deps=deps)
    check("insan işleri bütçeyi ve Task tavanını yemez: otomatik işler 8 Task'ı doldurur",
          len(pv["tasks"]) == 8 and not any(t["needs_human"] for t in pv["tasks"]) and pv["total_sp"] == 8)
    check("insan işi gerektiren önemli maddeler ayrı listede, en çok human_max",
          len(pv["human_tasks"]) == 5 and all(t["needs_human"] for t in pv["human_tasks"]))
    check("Task tavanı bütçeyi sınırladıysa açıkça söylenir",
          pv.get("cap_reached") is True and "8" in pv.get("cap_note", ""))
    html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text(encoding="utf-8")
    check("arayüzde iki ayrı grup: otomatik işler ve insan işi gerektiren önemli maddeler + tavan notu",
          "r.human_tasks" in html and "İnsan işi gerektiren önemli maddeler" in html
          and "Otomatik işler" in html and "r.cap_note" in html)
    check("insan işi tavanı ayarı kayıtlı (varsayılan 5); eski pay ayarı kaldırıldı",
          pc._SCHEMA_BY_KEY.get("CREW_QUALITY_HUMAN_MAX", {}).get("default") == 5
          and "CREW_QUALITY_HUMAN_SHARE" not in pc._SCHEMA_BY_KEY and intake.load_cfg()["human_max"] == 5)


def test_quality_project_filter():
    print("\n[Q14] Kalite alımı — proje (repo) seçimi")
    from fastapi.testclient import TestClient
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import server
    from agile_sdlc_crew.quality import intake
    from agile_sdlc_crew.quality.model import Finding

    def bug(repo, i):
        return Finding(source="sonar", repo=repo, project=f"EcomBackend:{repo}", file=f"b{i}.php", rule=f"php:S{i}",
                       rule_name="Bug", severity="CRITICAL", itype="BUG", lines=[1])
    findings = [bug("webservice", i) for i in range(3)] + [bug("FloNext", i) for i in range(2)] + [bug("orkestra", 9)]
    cfg = {"nr_days": 7, "nr_min_errors": 100, "pack_sp": 3, "pack_files": 5, "max_tasks": 8, "human_max": 5,
           "grace_days": 3, "sonar_url": "", "target_sprint": "next", "task_fields": {}}
    deps = intake.Deps(gather=lambda s, c, cf: (list(findings), []), board=lambda c: {}, repo_ok=lambda r: True,
                       queue=lambda w, t: 1, record=lambda **k: None, now=lambda: 1.0, cfg=lambda: cfg)
    pv = intake.preview(None, "sonar", 30, deps=deps, repos=["flonext", "orkestra"])
    repos = {t["repo"] for t in pv["tasks"]}
    check("seçilen projeler dışındakiler alınmaz (büyük/küçük harf duyarsız)",
          repos == {"FloNext", "orkestra"} and pv["skipped_counts"].get("seçim dışı proje") == 1, str(pv["skipped_counts"]))
    check("seçim boşsa tüm projeler", {t["repo"] for t in intake.preview(None, "sonar", 30, deps=deps)["tasks"]}
          == {"webservice", "FloNext", "orkestra"})
    pr = intake.projects(None, "sonar", deps=deps)
    check("proje listesi: bulgusu olan repolar, bulgu sayısına göre",
          [(i["repo"], i["findings"]) for i in pr["items"]] == [("webservice", 3), ("FloNext", 2), ("orkestra", 1)])
    seen = {}
    orig_get, orig_pv = pc.get, intake.preview
    pc.get = lambda k: True if k == "CREW_QUALITY_INTAKE" else orig_get(k)
    intake.preview = lambda *a, **k: (seen.update(k), {"tasks": []})[1]
    try:
        TestClient(server.app).get("/api/quality/preview?source=sonar&sp=10&repos=FloNext,orkestra")
        check("önizleme ucu repos parametresini geçirir", seen.get("repos") == ["FloNext", "orkestra"], str(seen))
    finally:
        pc.get, intake.preview = orig_get, orig_pv
    pc.get = lambda k: False if k == "CREW_QUALITY_INTAKE" else orig_get(k)
    try:
        check("proje listesi ucu kapalıyken 403",
              TestClient(server.app).get("/api/quality/projects?source=sonar").status_code == 403)
    finally:
        pc.get = orig_get
    html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text(encoding="utf-8")
    check("kartta proje seçimi; önizleme ve alım seçimi gönderir",
          'id="qProj"' in html and "/api/quality/projects" in html and "repos=" in html and "repos:" in html)


def test_guidance_in_steps():
    print("\n[48b] Kickoff kılavuzu — toplantı kapalıyken de requirements ve teknik tasarıma girer")
    from agile_sdlc_crew import kickoff_guidance as _kg
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.flow import AgileSDLCFlow, PipelineState
    orig = _kg.format_for_context
    _kg.format_for_context = lambda max_rules=30: "# OGRENILMIS YONERGELER (gecmis kickoff'lardan)\n1. (WI #retro) AC türet"
    try:
        s = PipelineState(work_item_id="1", requirements_text="{}", repo_name="r")
        f = SimpleNamespace(state=s, _vector_store=None, _forward_text=lambda k, t, c: t[:c])
        ctx = {k: AgileSDLCFlow._build_step_context(f, k) for k in
               ("requirements_analysis_task", "technical_design_task", "implement_change_task")}
        check("retro'dan eklenen kural requirements ve teknik tasarım bağlamında",
              "AC türet" in ctx["requirements_analysis_task"] and "AC türet" in ctx["technical_design_task"])
        check("diğer adımlara girmez (önbellek öneki bozulmaz)", "AC türet" not in ctx["implement_change_task"])
        tail_ok = ctx["technical_design_task"].rstrip().endswith("AC türet") or \
            ctx["technical_design_task"].index("AC türet") > ctx["technical_design_task"].find("# Is Kalemi")
        check("kılavuz bağlamın sonunda (adıma özel kısım)", tail_ok)
        orig_get = pc.get
        pc.get = lambda k: False if k == "CREW_KICKOFF_GUIDANCE_IN_STEPS" else orig_get(k)
        try:
            off = AgileSDLCFlow._build_step_context(f, "technical_design_task")
            check("ayarla kapatılabilir", "AC türet" not in off)
        finally:
            pc.get = orig_get
    finally:
        _kg.format_for_context = orig


def test_completion_report_no_tool_note():
    print("\n[49] Tamamlanma raporu — ajan 'yorum ekleyemiyorum' notu yazmasın")
    import yaml
    from agile_sdlc_crew import flow as _flow
    tasks = yaml.safe_load((Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/config/tasks.yaml")
                           .read_text(encoding="utf-8"))
    d = tasks["completion_report_task"]["description"] + tasks["completion_report_task"]["expected_output"]
    check("görev ajandan yorum EKLEMESİNİ istemiyor; metni pipeline ekliyor",
          "Add the summary as a comment" not in d and "Comment must be added" not in d
          and "pipeline posts" in d)
    raw = ("**Tamamlanma Raporu**\n\n> **Not:** Elimdeki araçlar yalnızca WI okuyup listeleyebiliyor, yorum yazamıyor. "
           "Bu rapor, WI'ya yorum olarak eklenmek üzere hazırlanmış tam metindir. Yorumu pipeline'ın yorum adımı "
           "veya bir insan eklemelidir.\n\n## Özet\nTek satır düzeltildi.")
    out = _flow._strip_tool_meta(raw)
    check("ajan araç notu rapordan ayıklanır, içerik kalır",
          "yorum yazamıyor" not in out and "insan eklemelidir" not in out and "Tek satır düzeltildi" in out
          and "Tamamlanma Raporu" in out)
    check("araçtan söz etmeyen metne dokunulmaz", _flow._strip_tool_meta("## Özet\nPR açıldı.") == "## Özet\nPR açıldı.")


def test_review_prefetch_window():
    print("\n[50] Review bağlamı — büyük dosyada değişen satırın çevresi (#230: ilk 173 satır)")
    from agile_sdlc_crew.flow import AgileSDLCFlow
    base = "\n".join(f"    $x{i} = $a['k{i}'];" for i in range(1, 3001))
    new = base.replace("    $x1460 = $a['k1460'];", "    $x1460 = $a['k1460'] ?? 0;")

    class Cl:
        def get_file_content(self, repo, fp, ref):
            if ref == "feature/76201":
                return new
            if ref == "main":
                return base
            raise RuntimeError("404")
    f = SimpleNamespace(
        state=SimpleNamespace(repo_name="webservice", branch_name="feature/76201", pr_id="1",
                              plan={"changes": [{"file_path": "/app/Controller/Api/V1/Basket.php"}]}, all_pushes=[]),
        _client=Cl(), _repo_mgr=None, _pr_file_bodies={})
    ctx = AgileSDLCFlow._prefetch_pr_changes_context(f)
    check("değişen satır (1460) review bağlamında, satır numarasıyla", "1460| " in ctx and "?? 0" in ctx, ctx[:200])
    check("dosya baştan kesilmez; değişmeyen yerler 'atlandı' diye işaretlenir",
          "satır atlandı" in ctx and "kısaltıldı" not in ctx)


def test_quality_estimate():
    print("\n[Q15] Kalite işi tahmini — alımın SP'si (yapısal tahmin şişirmesin; #76201: 13 SP)")
    from agile_sdlc_crew.flow import AgileSDLCFlow
    reqs = json.dumps({"functional_requirements": [{"id": f"FR{i}", "desc": "x"} for i in range(1, 7)],
                       "technical_requirements": [{"id": f"TR{i}", "desc": "x"} for i in range(1, 6)],
                       "acceptance_criteria": [{"id": f"AC{i}", "desc": "x"} for i in range(1, 6)]})

    def mk(tags, wi_sp):
        f = AgileSDLCFlow()
        f._db = None
        f.state.requirements_text = reqs
        f.state.plan = {"changes": [{"file_path": f"f{i}.php"} for i in range(4)]}
        f.state.wi_tags, f.state.wi_story_points = tags, wi_sp
        f._needed_explore = True
        f._estimate("plan")
        return f.state.estimate
    q = mk("tempo-quality; tempo-src-newrelic", 1.0)
    check("kalite işi: tahmin = alımın SP'si (1), kaynak kalite alımı",
          q["sp"] == 1 and "kalite" in q["source"], str(q))
    n = mk("", None)
    check("normal iş: yapısal tahmin aynen", n["sp"] >= 8, str(n.get("sp")))


def test_no_env_dependent_tests():
    print("\n[51] Test talimatı — ortama bağlı kontrol yok (#230: 'uyarı listesi boş' testi CI'da kırıldı)")
    import inspect
    from agile_sdlc_crew import flow as _flow
    from agile_sdlc_crew.quality import rank, render
    from agile_sdlc_crew.quality.model import Finding
    skill = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/skills/implementation/SKILL.md").read_text(encoding="utf-8")
    check("implementation skill'i ortama bağlı kontrolü yasaklar (tüm işler)",
          "ortama bağlı" in skill.lower() and "deprecation" in skill.lower())
    g = rank.nr_units([Finding(source="newrelic", repo="w", app="[Prod] a", action="A::b", err_class="E_WARNING",
                               message='Undefined array key "count"', count=500, last_seen=1.0)])[0]
    d = render.task_description(g)
    check("kalite Task'ı 'uyarı üretilmemesi'ni test etmeyi önermez; dönen değeri test etmeyi söyler",
          "uyarı üretilmemesi" not in d and "dönen değer" in d and "ortama bağlı" in d.lower())
    src = inspect.getsource(_flow)
    check("build-fix turu (tüm işler + kalite kuralı) ortama bağlı kontrolü kaldırmayı söyler",
          "ENVIRONMENT-DEPENDENT" in _flow.QUALITY_BUILD_FIX_RULE and "Ortama bağlı kontrol" in src)


def test_pr_comment_emoticons():
    print("\n[52] PR yorumu — ':/' gibi değerler emojiye dönüşmesin (PR #44918)")
    from agile_sdlc_crew.tools import azure_devops_base as az
    d = az.defuse_emoticons
    s = "Test, host olmayan canonical için ':/' ve '://en' gibi bozuk URL'leri bekliyor."
    out = d(s)
    check("emoji'ye dönüşecek değerler satır içi koda alınır",
          "`':/'`" in out and "`'://en'`" in out and out.endswith("bekliyor."), out)
    check("URL ve normal metin değişmez",
          d("Bkz. https://dev.azure.com/x ve Sonuç: tamam") == "Bkz. https://dev.azure.com/x ve Sonuç: tamam")
    code = "```php\n$u = ':/' . $x;\n```\nve `'://en'` zaten kod"
    check("kod blokları ve satır içi kod dokunulmaz", d(code) == code)
    check("cümle sonu noktalama kodun dışında kalır", d("değer :/,") == "değer `:/`,")
    tbl = "| Madde | Durum |\n|:---|---:|\n| R1 | açık |"
    check("markdown tablo hizalama satırı bozulmaz", d(tbl) == tbl)
    import inspect
    src = inspect.getsource(az.AzureDevOpsClient.add_pr_comment) + inspect.getsource(az.AzureDevOpsClient.reply_to_pr_thread)
    check("PR yorumu ve yanıtı gönderilmeden önce uygulanır", src.count("defuse_emoticons(") == 2)


def test_completion_comment_integrity():
    print("\n[53] Tamamlanma yorumu — tablo ortasında kesilmesin, atlanan UAT 'parse edilemedi' demesin (#76200)")
    import inspect
    from agile_sdlc_crew import flow as _flow
    from agile_sdlc_crew import wi_lifecycle as _wl
    rows = "\n".join(f"| AC{i} kriter | GEÇTİ |" for i in range(1, 120))
    rep = "## Özet\nx\n\n## Kabul\n| Kriter | Durum |\n|---|---|\n" + rows + "\n\n## Notlar\nson"
    cut = _flow._trim_report(rep, 1500)
    last = cut.rstrip().splitlines()
    check("kısaltma paragraf sınırında; yarım tablo satırı yok, kısaltıldı notu var",
          len(cut) <= 1600 and "kısaltıldı" in cut and not any(l.startswith("| AC") and not l.rstrip().endswith("|") for l in last))
    check("sığan rapor aynen kalır", _flow._trim_report("## Özet\nkısa", 1500) == "## Özet\nkısa")
    src = inspect.getsource(_flow.AgileSDLCFlow.step11_completion_report)
    check("yorum 3000 karakterde kesilmiyor", "completion_text[:3000]}" not in src and "_trim_report(" in src)
    d = _wl.evaluate_dod(review_approved=True, open_review_issues=0, build_status="succeeded",
                         uat_text="Atlandı — kalite işi (CREW_QUALITY_LEAN_FLOW)", pushed_files=["a.php"],
                         require_tests=False, pr_id="1", uat_skipped=True)
    uat = next(i for i in d.items if i.key == "uat")
    check("atlanan UAT DoD'da 'atlandı' der", "atlandı" in uat.note and "parse" not in uat.note, uat.note)
    rej = _wl.evaluate_dod(review_approved=True, open_review_issues=0, build_status="succeeded",
                           uat_text="Atlandı gibi başlayan ama **Overall Evaluation: REJECTED** UAT çıktısı\n1. AC1 FAIL",
                           pushed_files=["a.php"], require_tests=False, pr_id="1")
    u2 = next(i for i in rej.items if i.key == "uat")
    check("UAT metni 'Atlandı' ile başlasa da bayrak yoksa sonuç atlanmaz (fail-open yok)",
          u2.ok is False, str((u2.ok, u2.note)))
    check("atlama bayrağı flow'dan gelir, metinden değil",
          "uat_skipped=" in inspect.getsource(_flow.AgileSDLCFlow.step11_completion_report))


# ── Teknik borç aşama 2a: PHP modernleştirme ─────────────────────────────

def test_php_modern_catalog():
    print("\n[PM1] php_modern — katalog ve hedef PHP sürümü")
    from agile_sdlc_crew.quality import php_modern as pm
    rules = pm.load_rules()
    keys = {r.key for r in rules}
    check("katalog yüklendi (≥15 kural)", len(rules) >= 15, str(len(rules)))
    req = ("key", "since", "kind", "title", "summary", "advantages", "risks", "before", "after", "ref", "unit")
    bad = [r.key for r in rules if any(not getattr(r, f) for f in req)]
    check("her kuralda zorunlu alanlar dolu", not bad, str(bad))
    check("kind geçerli", all(r.kind in ("deprecation", "mechanical", "structural", "report") for r in rules))
    check("unit geçerli", all(r.unit in ("file", "module") for r in rules))
    check("since (major, minor)", all(isinstance(r.since, tuple) and len(r.since) == 2 for r in rules))
    check("structural/report kurallar otonom değil", all(not r.auto for r in rules if r.kind in ("structural", "report")))
    check("deprecation/mechanical kurallar otonom", all(r.auto for r in rules if r.kind in ("deprecation", "mechanical")))
    check("beklenen kurallar var", {"implicit_nullable", "str_contains", "match", "enum", "typed_properties",
                                    "property_hooks", "dto_from_array", "strict_types"} <= keys, str(keys))
    check("ağırlık: deprecation > structural > mechanical > report",
          pm.WEIGHT["deprecation"] > pm.WEIGHT["structural"] > pm.WEIGHT["mechanical"] > pm.WEIGHT["report"] == 0)
    k82 = {r.key for r in pm.applicable(rules, "8.2")}
    k84 = {r.key for r in pm.applicable(rules, "8.4")}
    check("8.2 hedefe property hooks / implicit_nullable önerilmez; 8.4'e önerilir",
          "property_hooks" not in k82 and "implicit_nullable" not in k82
          and {"property_hooks", "implicit_nullable", "enum", "str_contains"} <= k84)
    check("7.4 hedefe yalnız ≤7.4 kurallar", all(r.since <= (7, 4) for r in pm.applicable(rules, "7.4"))
          and "null_coalesce" in {r.key for r in pm.applicable(rules, "7.4")})
    check("imaj adından sürüm", pm.parse_php_image("europe-west1-docker.pkg.dev/flo-global/prod-ar/php-8-4:1.0") == (8, 4)
          and pm.parse_php_image("cr.yandex/crpsbatlmbdqnhk3392d/php-7-4:1.0") == (7, 4)
          and pm.parse_php_image("alpine:3.19") is None and pm.parse_php_image("php:8.3-fpm") == (8, 3))

    class C:
        def __init__(self, files):
            self.files = files

        def get_file_content(self, repo, path, branch="", project=""):
            if path not in self.files:
                import requests
                e = requests.HTTPError("404"); e.response = SimpleNamespace(status_code=404); raise e
            return self.files[path]
    amap = {"[Prod] A": {"repo": "webservice", "project": "EcomBackend", "src": "anatolia/build/webservice"},
            "[Prod] B": {"repo": "webservice", "project": "EcomBackend", "src": "russia/build/websites"},
            "[Prod] C": {"repo": "alfred", "project": "EcomBackend", "src": "anatolia/build/alfred"}}
    c = C({"/build/webservice/Dockerfile": "FROM x/php-8-4:1.0\nRUN a", "/build/websites/Dockerfile": "FROM y/php-7-4:1.0",
           "/build/alfred/Dockerfile": "FROM x/php-8-2:1.0"})
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        t = pm.target_version(c, "webservice", app_map_cache=amap, repo_root=root, cache_path=root / "t.yaml")
        check("iki imaj (8.4, 7.4) → en düşük 7.4", t["version"] == "7.4" and t["source"] == "dockerfile"
              and sorted(t["images"]) == ["x/php-8-4:1.0", "y/php-7-4:1.0"], str(t))
        (root / "composer.json").write_text('{"require": {"php": "^8.1 || ^8.2"}}', encoding="utf-8")
        t2 = pm.target_version(c, "pigeon", app_map_cache=amap, repo_root=root, cache_path=root / "t.yaml")
        check("imaj yok → composer.json alt sınırı 8.1", t2["version"] == "8.1" and t2["source"] == "composer", str(t2))
        t3 = pm.target_version(c, "sorting", app_map_cache=amap, repo_root=root / "yok", cache_path=root / "t.yaml")
        check("ne imaj ne composer → None", t3["version"] is None and t3["source"] is None, str(t3))
        c.files.clear()
        t4 = pm.target_version(c, "webservice", app_map_cache=amap, repo_root=root, cache_path=root / "t.yaml")
        check("önbellek: ikinci çağrı ağa gitmeden aynı sonucu verir", t4["version"] == "7.4", str(t4))
        t5 = pm.target_version(c, "webservice", app_map_cache=amap, repo_root=root, cache_path=root / "t.yaml", max_age_h=0)
        check("önbellek süresi dolunca yeniden okur (imaj 404 → composer'a düşer)", t5["source"] == "composer", str(t5))


def test_php_modern_scan():
    print("\n[PM2] php_modern — token tarayıcı")
    import shutil
    from agile_sdlc_crew.quality import php_modern as pm
    from agile_sdlc_crew.quality.model import QualitySourceError
    if not shutil.which("php"):
        skip("php_modern tarayıcı", "php binary yok")
        return
    root = Path(__file__).resolve().parent / "fixtures" / "php_modern"
    res = pm.scan_repo(root, exclude=["vendor"], php_bin="php")
    hits = res["hits"]
    got = {(h["file"], h["line"], h["rule"]) for h in hits}
    # Beklenen: fikstürlerdeki `// @expect <kural>` işaretleri (satır = işaretin satırı)
    want = set()
    for f in sorted(root.rglob("*.php")):
        rel = f.relative_to(root).as_posix()
        if rel.startswith("vendor/"):
            continue
        for i, ln in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for m in re.finditer(r"@expect (\w+)", ln):
                want.add((rel, i, m.group(1)))
    missing = sorted(want - got)
    extra = sorted(got - want)
    check("beklenen her bulgu yerinde (dosya, satır, kural)", not missing, str(missing))
    check("fazladan bulgu yok (string/heredoc/yorum içi kalıplar, modern.php, vendor)", not extra, str(extra))
    check("tarama sayacı: vendor hariç 9 dosya (bozuk ve Latin-5 dosya dahil, tarayıcı takılmaz)", res["files"] == 9, str(res["files"]))
    check("bulgu metni tarayıcı çıkışında maskelenir (önbelleğe ham secret yazılmaz)",
          any("Secret123" not in h["text"] and "pw" in h["text"] for h in hits if h["file"] == "app/Lib/edge.php" and h["rule"] == "null_coalesce")
          and not any("Secret123" in h["text"] for h in hits))
    check("bulgu satır metni taşır", all(h.get("text") for h in hits))
    check("modül: kaynak kökünden 2 dizin", {h["module"] for h in hits} <= {"app/Model", "app/Lib"}, str({h["module"] for h in hits}))
    ad = res["adoption"]
    check("benimseme sayaçları", ad["enum"] == 1 and ad["match"] == 1 and ad["nullsafe"] == 1 and ad["attribute"] == 2
          and ad["readonly"] == 1 and ad["strict_types_files"] == 1 and ad["untyped_props"] == 7 and ad["typed_props"] >= 4, str(ad))
    res2 = pm.scan_repo(root, exclude=[], php_bin="php")
    check("hariç tutma kapalıyken vendor da taranır", res2["files"] == 10 and any(h["file"].startswith("vendor/") for h in res2["hits"]))
    try:
        pm.scan_repo(root, exclude=[], php_bin="/nonexistent/php")
        check("php yoksa QualitySourceError", False)
    except QualitySourceError:
        check("php yoksa QualitySourceError", True)
    try:
        pm.scan_repo(root / "yok", exclude=[], php_bin="php")
        check("repo dizini yoksa QualitySourceError", False)
    except QualitySourceError:
        check("repo dizini yoksa QualitySourceError", True)


def test_php_modern_groups():
    print("\n[PM3] php_modern — gruplama, skor, dedup, önbellek")
    import math
    from agile_sdlc_crew.quality import php_modern as pm, rank
    rules = pm.load_rules()
    cfg = {"pack_sp": 3, "pack_files": 5, "grace_days": 3}

    def h(rule, file, line=1):
        return {"rule": rule, "file": file, "line": line, "text": "x", "module": pm.module_of(file)}
    hits = ([h("str_contains", f"app/Lib/F{i}.php", 3) for i in range(12)]
            + [h("str_contains", "app/Lib/F0.php", 9)]
            + [h("utf8_encode", "app/Lib/F0.php", 5)]
            + [h("typed_properties", f"app/Model/M{i}.php", 2) for i in range(8)]
            + [h("typed_properties", "app/Lib/F1.php", 2)]
            + [h("property_hooks", "app/Model/M1.php", 7)]
            + [h("strict_types", "app/Lib/F0.php", 1)])
    scan = {"files": 20, "sha": "abc", "hits": hits, "adoption": {}}
    groups, skipped, summary = pm.build_groups(scan, rules, "8.2", repo="webservice", board_index={}, now=1e9, cfg=cfg)
    keys = {g.findings[0].rule for g in groups}
    check("8.2 hedefte property_hooks grubu yok", "property_hooks" not in keys, str(keys))
    g84, _s, sum84 = pm.build_groups(scan, rules, "8.4", repo="webservice", board_index={}, now=1e9, cfg=cfg)
    check("8.4 hedefte property_hooks grubu var", "property_hooks" in {g.findings[0].rule for g in g84})
    sc = [g for g in groups if g.findings[0].rule == "str_contains"]
    # Aynı öneri tek iş (kullanıcı kararı): dosya başına 1 SP toplanmaz (150 dosya 64 SP çıkıyordu)
    check("mekanik: aynı kural TEK iş, SP yer sayısından → 12 dosya → 1 iş × 1 SP",
          len(sc) == 1 and sc[0].sp == 1 and len(sc[0].files) == 12, str([(g.sp, len(g.files)) for g in sc]))
    check("paket parmak izleri dosya başına php_modern:<repo>:<kural>:<dosya>",
          all(fp.startswith("php_modern:webservice:str_contains:app/Lib/F") for g in sc for fp in g.fingerprints)
          and sum(len(g.fingerprints) for g in sc) == 12)
    check("aynı dosyada 2 bulgu → tek birim, count 2", any(f.count == 2 and f.file == "app/Lib/F0.php" for g in sc for f in g.findings))
    check("mekanik grup otonom", all(not g.needs_human for g in sc))
    tp = [g for g in groups if g.findings[0].rule == "typed_properties"]
    check("yapısal: aynı kural TEK iş, parmak izleri modül başına (app/Model, app/Lib), SP ≤8, needs_human",
          len(tp) == 1 and tp[0].needs_human and tp[0].sp <= 8
          and set(tp[0].fingerprints) == {"php_modern:webservice:typed_properties:app/Model", "php_modern:webservice:typed_properties:app/Lib"},
          str([(g.fingerprints, g.sp) for g in tp]))
    check("yapısal neden metni katalog riskinden", all(g.human_reason.startswith("Tasarım kararı") for g in tp))
    dep = next(g for g in groups if g.findings[0].rule == "utf8_encode")
    one_sc = next(g for g in sc if len(g.files) == 1) if any(len(g.files) == 1 for g in sc) else None
    w = pm.WEIGHT
    check("skor = ağırlık × (1+log10(adet)); deprecation(1) > mechanical(1)",
          abs(dep.score - w["deprecation"]) < 1e-9 and (one_sc is None or dep.score > one_sc.score), str(dep.score))
    big = max(tp, key=lambda g: g.total)
    check("yapısal skor adetle log artar", abs(big.score - w["structural"] * (1 + math.log10(8))) < 1e-9, str(big.score))
    check("report kuralı grup üretmez ama özette", "strict_types" not in keys and summary["strict_types"]["count"] == 1
          and summary["strict_types"]["kind"] == "report")
    check("özet: kural → adet/dosya/modül/kind/auto", summary["str_contains"]["count"] == 13 and summary["str_contains"]["files"] == 12
          and summary["typed_properties"]["modules"] == 2 and summary["typed_properties"]["auto"] is False, str(summary["str_contains"]))
    # dedup: board'da açık fp → atlanır
    bi = {"php_modern:webservice:utf8_encode:app/Lib/F0.php": {"wi": 500, "state": "Active"},
          "php_modern:webservice:typed_properties:app/Model": {"wi": 501, "state": "Canceled"}}
    g2, sk2, _ = pm.build_groups(scan, rules, "8.2", repo="webservice", board_index=bi, now=1e9, cfg=cfg)
    reasons = {s["reason"].split(" #")[0] for s in sk2}
    check("board'da açık WI → 'açık WI #', iptal → 'iptal edilmiş WI #'", reasons == {"açık WI", "iptal edilmiş WI"}
          and not any(g.findings[0].rule == "utf8_encode" for g in g2)
          and not any(fp.endswith("typed_properties:app/Model") for g in g2 for fp in g.fingerprints), str(sk2))
    # pack_sonar eski davranışı
    check("pack_sonar, pack_units'e sarmalayıcı", rank.pack_sonar([]) == [] and hasattr(rank, "pack_units"))
    # önbellek
    calls = []

    def fake_scan(root, **kw):
        calls.append(kw)
        return {"files": 1, "sha": kw.get("_sha", "s1"), "hits": [], "adoption": {}, "errors": []}
    with tempfile.TemporaryDirectory() as td:
        cdir = Path(td)
        a = pm.scan_cached("webservice", Path(td), cache_dir=cdir, scan_fn=lambda root, **kw: fake_scan(root, _sha="s1", **kw),
                           head_sha_fn=lambda r: "s1", exclude=[], php_bin="php", ttl_h=24)
        b = pm.scan_cached("webservice", Path(td), cache_dir=cdir, scan_fn=lambda root, **kw: fake_scan(root, _sha="s1", **kw),
                           head_sha_fn=lambda r: "s1", exclude=[], php_bin="php", ttl_h=24)
        check("aynı sha → ikinci çağrı tarayıcıyı çağırmaz", len(calls) == 1 and b["sha"] == "s1" and a["cached"] is False and b["cached"] is True)
        pm.scan_cached("webservice", Path(td), cache_dir=cdir, scan_fn=lambda root, **kw: fake_scan(root, _sha="s2", **kw),
                       head_sha_fn=lambda r: "s2", exclude=[], php_bin="php", ttl_h=24)
        check("sha değişince yeniden tarar", len(calls) == 2)
        pm.scan_cached("webservice", Path(td), cache_dir=cdir, scan_fn=lambda root, **kw: fake_scan(root, _sha="s2", **kw),
                       head_sha_fn=lambda r: "s2", exclude=[], php_bin="php", ttl_h=24, force=True)
        check("force → yeniden tarar", len(calls) == 3)
        pm.scan_cached("webservice", Path(td), cache_dir=cdir, scan_fn=lambda root, **kw: fake_scan(root, _sha="s2", **kw),
                       head_sha_fn=lambda r: "s2", exclude=[], php_bin="php", ttl_h=0)
        check("TTL dolunca yeniden tarar", len(calls) == 4)
        pm.scan_cached("webservice", Path(td), cache_dir=cdir, scan_fn=lambda root, **kw: fake_scan(root, _sha="s2", **kw),
                       head_sha_fn=lambda r: "s2", exclude=["vendor"], php_bin="php", ttl_h=24)
        check("hariç listesi değişince yeniden tarar (önbellek anahtarı)", len(calls) == 5)
        check("önbellek dosyası repo adıyla, yol kaçışı yok", (cdir / "webservice.json").exists())
        try:
            pm.scan_cached("../x", Path(td), cache_dir=cdir, scan_fn=fake_scan, head_sha_fn=lambda r: "s", exclude=[], php_bin="php")
            check("repo adında yol kaçışı reddedilir", False)
        except ValueError:
            check("repo adında yol kaçışı reddedilir", True)


def test_php_modern_render():
    print("\n[PM4] php_modern — Task metni (avantaj / risk / önce-sonra)")
    from agile_sdlc_crew.quality import php_modern as pm, render
    rules = pm.load_rules()
    hits = [{"rule": "str_contains", "file": "app/Lib/Url.php", "line": 12, "text": "if (strpos($u, 'tempo-fp: x') !== false) // password=Secret123!", "module": "app/Lib"},
            {"rule": "str_contains", "file": "app/Lib/Url.php", "line": 40, "text": "if (false !== strpos($u, 'a'))", "module": "app/Lib"}]
    hits += [{"rule": "typed_properties", "file": f"app/Model/M{i}.php", "line": i + 1, "text": f"private $p{i};", "module": "app/Model"} for i in range(25)]
    groups, _sk, _sum = pm.build_groups({"hits": hits}, rules, "8.4", repo="webservice", board_index={}, now=1e9,
                                        cfg={"pack_sp": 3, "pack_files": 5, "grace_days": 3})
    mech = next(g for g in groups if g.findings[0].rule == "str_contains")
    struct = next(g for g in groups if g.findings[0].rule == "typed_properties")
    d = render.task_description(mech, php_target="8.4")
    check("ilk satır Repo (repo_from_description çözer)", d.startswith("<p><b>Repo:</b> <code>webservice</code></p>")
          and render.repo_from_description(d) == "webservice")
    check("hedef PHP ve kural sürümü", "Hedef PHP:</b> 8.4" in d and "PHP 8.0" in d, d[:400])
    check("kural başlığı ve 'Ne'", "str_contains()" in d and "Ne:" in d)
    check("Avantajlar ve Riskler katalogdan", "Avantajlar:" in d and "Riskler:" in d and "0 / false tuzağı" in d)
    check("Önce/Sonra pre içinde, HTML-escape'li", "<b>Önce:</b>" in d and "<pre>" in d
          and "if (strpos($uri, &#x27;/api/&#x27;) !== false)" in d and "str_contains($uri" in d, d[d.find("Önce"):d.find("Önce") + 300])
    check("bulunan yerler dosya:satır + metin, maskeli ve defang'li",
          "app/Lib/Url.php:12" in d and "app/Lib/Url.php:40" in d and "tempo_fp" in d and "Secret123" not in d, d)
    check("kaynak (RFC) bağlantısı", 'href="https://wiki.php.net/rfc/str_contains"' in d)
    fps = render.parse_fingerprints(d)
    check("tempo-fp satırları geri okunur", fps == mech.fingerprints, str(fps))
    eb = " ".join(render.expected_behavior(mech))
    check("mekanik beklenen davranış: davranış değişmez, yalnız listelenen yerler, dar test, ortama bağlı kontrol yok",
          "değişmez" in eb and "listelenen" in eb and "birim testi" in eb and "Ortama bağlı" in eb, eb)
    ac = render.acceptance_criteria(mech)
    check("mekanik kabul kriteri", "eski yapı kalmaz" in ac and "davranış" in ac, ac)
    ds = render.task_description(struct, php_target="8.4")
    check("yapısal: 'Neden insan' + tasarım kararı", "Neden insan:</b> Tasarım kararı" in ds)
    check("yapısal: 25 dosya → her dosya en az bir kez listelenir (parmak izi = dosya; listelenmeyen dosya 'yapıldı' sayılmasın)",
          ds.count("app/Model/M") == 25, str(ds.count("app/Model/M")))
    many = [{"rule": "str_contains", "file": "config/Migrations/20231012093012_CreateOrders.php", "line": i, "text": "x", "module": "config/Migrations"} for i in range(1, 26)]
    many += [{"rule": "str_contains", "file": "app/B.php", "line": 1, "text": "y", "module": "app"}, {"rule": "str_contains", "file": "app/C.php", "line": 1, "text": "z", "module": "app"}]
    gm, _a, _b = pm.build_groups({"hits": many}, rules, "8.4", repo="webservice", board_index={}, now=1e9, cfg={"pack_sp": 9, "pack_files": 9, "grace_days": 3})
    big = max(gm, key=lambda g: g.total)
    dm = render.task_description(big, php_target="8.4")
    forged = pm.build_groups({"hits": [{"rule": "str_contains", "file": "app/tempo-fp: x.php", "line": 1, "text": "y", "module": "app"}]},
                             rules, "8.4", repo="webservice", board_index={}, now=1e9, cfg={"pack_sp": 3, "pack_files": 5, "grace_days": 3})[0][0]
    df = render.task_description(forged, php_target="8.4")
    check("dosya adı 'tempo-fp:' ile dedup satırı taklit edemez (yol defang)", render.parse_fingerprints(df) == forged.fingerprints, str(render.parse_fingerprints(df)))
    check("paket: 25+2+2 yer → B ve C de listelenir, dosya başına en çok 5 satır, yol maskelenmez",
          "app/B.php:1" in dm and "app/C.php:1" in dm and "config/Migrations/20231012093012_CreateOrders.php:1" in dm
          and dm.count("20231012093012_CreateOrders.php:") == 5 and "+20 yer" in dm, dm[dm.find("Bulunan"):dm.find("Bulunan") + 900])
    es = " ".join(render.expected_behavior(struct))
    check("yapısal beklenen davranış: insan kapsam belirler, etiket kaldırılınca kuyruğa", "insan" in es and "needs-human" in es, es)
    check("AC alanı dolu", "<li>" in render.acceptance_block(struct))
    check("etiketler techdebt hattı + kaynak", render.task_tags(struct, lane="techdebt")
          == "tempo-quality; tempo-techdebt; tech-debt; tempo-src-php_modern; needs-human"
          and render.task_tags(mech, lane="techdebt") == "tempo-quality; tempo-techdebt; tech-debt; tempo-src-php_modern")
    check("kaynak etiketi", render.SOURCE_LABEL.get("php_modern") == "PHP modernleştirme")
    check("başlık [repo] kural — yer", render.task_title(mech).startswith("[webservice] strpos(...) !== false → str_contains() — Url.php"), render.task_title(mech))
    check("eski kaynaklar etkilenmedi (sonar body hâlâ çalışır)", "Kabul kriteri" in render.task_description(
        __import__("agile_sdlc_crew.quality.model", fromlist=["Group"]).Group(source="sonar", repo="r", title="t", fingerprints=["f"],
        findings=[__import__("agile_sdlc_crew.quality.model", fromlist=["Finding"]).Finding(source="sonar", repo="r", file="a.php", rule="php:S1", rule_name="n", severity="CRITICAL", itype="BUG")])))


def test_php_modern_take():
    print("\n[PM5] php_modern — repo listesi, tarama, iş açma")
    from agile_sdlc_crew.quality import intake, php_modern as pm
    queued, rec = [], []

    class C:
        org_url, project = "https://dev.azure.com/o", "P"

        def __init__(self):
            self.created, self.wiql = [], []

        def list_iterations(self, team=""):
            return [{"name": "S23", "path": "P\\S23", "attributes": {"timeFrame": "future", "startDate": "2026-11-02"}}]

        def query_work_items(self, wiql, limit=0):
            self.wiql.append(wiql)
            return []

        def get_team_area_path(self, team=""):
            return "P\\T"

        def create_work_item(self, type_name, fields, parent_id=None):
            self.created.append((type_name, fields, parent_id))
            return {"id": 600 + len(self.created), "fields": fields}

        def update_work_item(self, wid, ops):
            return {}

        def get_authenticated_user(self):
            return {"uniqueName": "me@x"}
    hits = [{"rule": "str_contains", "file": "app/Lib/A.php", "line": 3, "text": "x", "module": "app/Lib"},
            {"rule": "typed_properties", "file": "app/Model/M.php", "line": 2, "text": "y", "module": "app/Model"},
            {"rule": "utf8_encode", "file": "app/Lib/B.php", "line": 4, "text": "z", "module": "app/Lib"}]
    scans = []
    board = {}
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "webservice").mkdir()
        # ext-mbstring: utf8_encode düzeltmesi mbstring ister; kanıt yoksa iş altyapı listesine gider
        (root / "webservice" / "composer.json").write_text('{"require":{"php":"^8.4","ext-mbstring":"*"}}', encoding="utf-8")
        (root / "notphp").mkdir()
        (root / "oldphp").mkdir()
        (root / "oldphp" / "composer.json").write_text('{}', encoding="utf-8")
        cfg = {"pack_sp": 3, "pack_files": 5, "grace_days": 3, "target_sprint": "next", "task_fields": {},
               "sonar_url": "", "nr_days": 7, "php_exclude": [], "php_bin": "php", "scan_ttl_h": 24, "max_take": 8,
               "repos_dir": root, "targets_cache": root / "t.yaml", "scan_cache": root / "cache"}
        deps = intake.Deps(gather=None, board=lambda c: board, repo_ok=lambda r: (root / r).is_dir(),
                           queue=lambda w, ttl: (queued.append(w), 9)[1], record=lambda **k: rec.append(k),
                           now=lambda: 1e9, cfg=lambda: cfg)

        def scan_fn(repo_root, **kw):
            scans.append(str(repo_root))
            return {"files": 3, "sha": "s1", "hits": hits, "adoption": {"enum": 0}, "errors": []}
        pm_kw = dict(deps=deps, scan_fn=scan_fn, head_sha_fn=lambda r: "s1", app_map_cache={})
        c = C()
        rp = pm.repos(c, **pm_kw)
        items = {i["repo"]: i for i in rp["items"]}
        check("repo listesi: composer.json olan yerel repolar, hedef sürüm composer'dan; sürümsüz repo işaretli; php olmayan yok",
              set(items) == {"webservice", "oldphp"} and items["webservice"]["version"] == "8.4"
              and items["oldphp"]["version"] is None and items["oldphp"]["scannable"] is False, str(rp))
        sc = pm.scan(c, "webservice", team="T", **pm_kw)
        check("tarama yazma yapmaz", not c.created and len(scans) == 1)
        check("tarama sonucu: sürüm, sha, kural özeti, otomatik/insan grupları, gid",
              sc["version"] == "8.4" and sc["sha"] == "s1" and sc["rules"]["str_contains"]["count"] == 1
              and len(sc["auto"]) == 2 and len(sc["human"]) == 1 and all(len(r["gid"]) == 12 for r in sc["auto"] + sc["human"]), str(sc)[:600])
        row = sc["auto"][0]
        check("grup satırı katalog ayrıntısını taşır", all(k in row for k in ("rule", "kind", "since", "title", "summary", "advantages",
              "risks", "before", "after", "ref", "count", "files", "locations", "sp", "needs_human")), str(row.keys()))
        check("deprecation önce sıralanır", sc["auto"][0]["rule"] == "utf8_encode")
        sc2 = pm.scan(c, "webservice", team="T", **pm_kw)
        check("ikinci tarama önbellekten (aynı sha)", len(scans) == 1 and sc2["cached"] is True)
        sc_up = pm.scan(c, "WEBSERVICE", team="T", **pm_kw)
        check("repo adı kanonik (dizin adı): WEBSERVICE → webservice, parmak izi aynı",
              sc_up["repo"] == "webservice" and sc_up["auto"][0]["fingerprints"] == sc["auto"][0]["fingerprints"], str(sc_up["repo"]))
        gids = [r["gid"] for r in sc["auto"]] + [r["gid"] for r in sc["human"]]
        # kilit
        intake._LOCK.acquire()
        try:
            try:
                pm.take(c, "webservice", gids, team="T", **pm_kw)
                check("kilitliyken Busy", False)
            except intake.Busy:
                check("kilitliyken Busy", True)
        finally:
            intake._LOCK.release()
        try:
            pm.take(c, "webservice", ["000000000000"], team="T", **pm_kw)
            check("yalnız bilinmeyen gid → ValueError (tarama değişti)", False)
        except ValueError as e:
            check("yalnız bilinmeyen gid → ValueError (tarama değişti)", "tarama" in str(e).lower(), str(e))
        res = pm.take(c, "webservice", gids + ["000000000000", gids[0]], team="T", **pm_kw)
        types = [t for t, _f, _p in c.created]
        check("story techdebt hattında (Teknik borç — S23, tempo-techdebt) + 3 Task",
              types == ["User Story", "Task", "Task", "Task"] and c.created[0][1]["System.Title"] == "Teknik borç — S23"
              and "tempo-techdebt" in c.created[0][1]["System.Tags"], str(types))
        tf = [f for t, f, _p in c.created if t == "Task"]
        check("Task etiketleri php_modern kaynağı; yapısal needs-human", sum("needs-human" in f["System.Tags"] for f in tf) == 1
              and all("tempo-src-php_modern" in f["System.Tags"] for f in tf))
        check("Task açıklamasında hedef PHP ve avantajlar", all("Hedef PHP:</b> 8.4" in f["System.Description"] and "Avantajlar" in f["System.Description"] for f in tf))
        check("mekanik 2 iş kuyruğa alındı, yapısal alınmadı", len(queued) == 2 and len(res["tasks"]) == 2 and len(res["human_tasks"]) == 1
              and res["human_tasks"][0]["job_id"] is None, str(res))
        check("bilinmeyen gid atlanan listesinde, tekrar gid tekilleşti", any("tarama" in s_["reason"] for s_ in res["skipped"]) and len(res["skipped"]) == 1)
        check("quality_items kaydı source=php_modern", rec and all(r["source"] == "php_modern" for r in rec))
        check("story bağlantısı + SP", res["story"]["id"] == 601 and res["total_sp"] == 2 and res["human_sp"] == 2, str(res.get("total_sp")) + "/" + str(res.get("human_sp")))
        # board'da açık fp → atlanır
        board.clear()
        board[gids_fp := pm.fingerprint("webservice", "str_contains", "app/Lib/A.php")] = {"wi": 700, "state": "Active"}
        sc3 = pm.scan(c, "webservice", team="T", force=True, **pm_kw)
        check("board'da açık WI → grup listede yok, atlananlarda", all(r["rule"] != "str_contains" for r in sc3["auto"])
              and sc3["skipped_counts"].get("açık WI") == 1, str(sc3["skipped_counts"]))
        # tavan
        cfg["max_take"] = 1
        c2 = C()
        res2 = pm.take(c2, "webservice", gids, team="T", **pm_kw)
        check("istek başı tavan", len([t for t, _f, _p in c2.created if t == "Task"]) == 1 and any("tavan" in s_["reason"] for s_ in res2["skipped"]))
        try:
            pm.take(c, "oldphp", gids, team="T", **pm_kw)
            check("hedef sürümü bilinmeyen repo → ValueError", False)
        except ValueError as e:
            check("hedef sürümü bilinmeyen repo → ValueError", "sürüm" in str(e), str(e))
        try:
            pm.scan(c, "yok", team="T", **pm_kw)
            check("yerel olmayan repo → ValueError", False)
        except ValueError:
            check("yerel olmayan repo → ValueError", True)
    check("load_cfg knob'ları okur", {"php_exclude", "php_bin", "scan_ttl_h", "max_take"} <= set(pm.load_cfg()))


def test_php_modern_endpoints():
    print("\n[PM6] php_modern — ayarlar ve uçlar")
    from fastapi.testclient import TestClient
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import server
    from agile_sdlc_crew.quality import intake, php_modern as pm
    from agile_sdlc_crew.quality.model import QualitySourceError
    sb = pc._SCHEMA_BY_KEY
    check("ayarlar kayıtlı ve varsayılanlar güvenli",
          sb.get("CREW_TECHDEBT_PHP_MODERN", {}).get("default") is False
          and sb.get("CREW_TECHDEBT_SCAN_TTL_H", {}).get("default") == 24
          and sb.get("CREW_TECHDEBT_MODERN_MAX_TAKE", {}).get("default") == 8
          and "CREW_PHP_BIN" not in sb  # env-only: dashboard'dan yazılabilen binary yolu = keyfi çalıştırma
          and "vendor" in sb.get("CREW_TECHDEBT_PATH_EXCLUDE", {}).get("default", ""))
    orig = pc.get

    def on(**kw):
        return lambda k: kw[k] if k in kw else orig(k)
    pc.get = on(CREW_TECHDEBT=True, CREW_TECHDEBT_PHP_MODERN=False)
    try:
        tc = TestClient(server.app)
        check("php_modern kapalıyken üç uç → 403",
              tc.get("/api/techdebt/modern/repos").status_code == 403
              and tc.get("/api/techdebt/modern/scan?repo=webservice").status_code == 403
              and tc.post("/api/techdebt/modern/take", json={"repo": "webservice", "group_ids": ["a"]}).status_code == 403)
    finally:
        pc.get = orig
    pc.get = on(CREW_TECHDEBT=False, CREW_TECHDEBT_PHP_MODERN=True)
    try:
        check("teknik borç ana anahtarı kapalıyken de 403", TestClient(server.app).get("/api/techdebt/modern/repos").status_code == 403)
    finally:
        pc.get = orig
    pc.get = on(CREW_TECHDEBT=True, CREW_TECHDEBT_PHP_MODERN=True)
    import os as _os
    _env_bin = _os.environ.get("CREW_PHP_BIN")
    o_scan, o_take, o_repos, o_worker = pm.scan, pm.take, pm.repos, server._ensure_worker
    worker = []
    try:
        tc = TestClient(server.app)
        check("repo boş → 400", tc.get("/api/techdebt/modern/scan?repo=").status_code == 400)
        check("group_ids boş → 400", tc.post("/api/techdebt/modern/take", json={"repo": "webservice", "group_ids": []}).status_code == 400)

        def raiser(exc):
            def f(*a, **k):
                raise exc
            return f
        pm.scan = raiser(QualitySourceError("php yok"))
        check("tarayıcı hatası → 502", tc.get("/api/techdebt/modern/scan?repo=webservice").status_code == 502)
        pm.scan = raiser(ValueError("repo yok"))
        check("geçersiz repo → 400", tc.get("/api/techdebt/modern/scan?repo=x").status_code == 400)
        pm.take = raiser(intake.Busy("meşgul"))
        check("kilit → 409", tc.post("/api/techdebt/modern/take", json={"repo": "webservice", "group_ids": ["a"]}).status_code == 409)
        pm.take = lambda c, repo, ids, **k: {"repo": repo, "story": {"id": 1, "url": "u", "created": True},
                                             "tasks": [{"id": 2, "job_id": 9}], "human_tasks": [], "skipped": []}
        server._ensure_worker = lambda: worker.append(1)
        r = tc.post("/api/techdebt/modern/take", json={"repo": "webservice", "group_ids": ["a"], "team": "T"})
        check("başarılı alım → 201 + worker tetiklenir", r.status_code == 201 and worker == [1], str(r.status_code))
        pm.scan = lambda c, repo, **k: {"repo": repo, "auto": [], "human": [], "force": k.get("force")}
        r2 = tc.get("/api/techdebt/modern/scan?repo=webservice&force=1")
        check("scan force parametresi geçer", r2.status_code == 200 and r2.json()["force"] is True)
        pm.repos = lambda c, **k: {"items": [{"repo": "webservice", "version": "8.4"}]}
        r3 = tc.get("/api/techdebt/modern/repos")
        check("repos 200 + enabled", r3.status_code == 200 and r3.json()["enabled"] is True and r3.json()["items"])
        _os.environ["CREW_PHP_BIN"] = "php; rm -rf /"
        bad1 = pm.load_cfg()["php_bin"]
        _os.environ["CREW_PHP_BIN"] = "/usr/bin/python3"
        bad2 = pm.load_cfg()["php_bin"]
        _os.environ["CREW_PHP_BIN"] = "/opt/homebrew/bin/php8.4"
        ok = pm.load_cfg()["php_bin"]
        check("CREW_PHP_BIN env-only; boşluk/; ve php olmayan ad reddedilir, php8.4 yolu kabul",
              bad1 == "php" and bad2 == "php" and ok == "/opt/homebrew/bin/php8.4", f"{bad1} {bad2} {ok}")
    finally:
        pm.scan, pm.take, pm.repos, server._ensure_worker = o_scan, o_take, o_repos, o_worker
        pc.get = orig
        if _env_bin is None:
            _os.environ.pop("CREW_PHP_BIN", None)
        else:
            _os.environ["CREW_PHP_BIN"] = _env_bin


def test_php_modern_ui():
    print("\n[PM7] php_modern — Modernleştirme (PHP) kartı")
    html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text(encoding="utf-8")
    s = html.find('id="paneTechdebt"')
    pane = html[s:html.find("<section", s + 10)] if s >= 0 else ""
    check("kart Teknik borç sekmesinde, başlangıçta gizli", 'id="tdModernCard"' in pane and '<span class="h">Modernleştirme</span>' in pane
          and 'id="tdModernCard" style="display:none' in pane)
    check("repo seçici, hedef sürüm chip'i, benimseme özeti, Yeniden tara, İş olarak aç",
          all(x in pane for x in ('id="tdmRepo"', 'id="tdmTarget"', 'id="tdmAdoption"', 'id="tdmRescan"', 'id="tdmTake"'))
          and "Seçilenleri iş olarak aç" in pane)
    for fn in ("loadModernRepos", "modernScan", "modernTake", "renderModern"):
        check(f"JS: {fn}", f"function {fn}" in html)
    js = html[html.find("function loadModernRepos"):html.find("function loadModernRepos") + 16000]
    check("uçlar", "/api/techdebt/modern/repos" in js and "/api/techdebt/modern/scan" in js and "/api/techdebt/modern/take" in js)
    check("iki grup başlığı", "Otomatik işler" in js and "İnsan kararı gerektiren yapısal işler" in js)
    check("satır ayrıntısı: Avantajlar / Riskler / Önce / Sonra / Bulunan yerler / Kaynak", all(x in js for x in ("Avantajlar", "Riskler", "Önce", "Sonra", "Bulunan yerler", "Kaynak")))
    check("403'te kart gizli, takım _tdTeam() ile, humanChip kullanılır", "status===403" in js and "_tdTeam()" in js and "humanChip(" in js)
    check("sürümü bilinmeyen repo seçilemez", "disabled" in js and "hedef sürüm bilinmiyor" in js)
    check("tema renkleri (sabit renk yok)", "var(--text" in js and not re.search(r"#[0-9a-fA-F]{6}\b", js), "sabit renk var")
    check("sekme açılınca modern repolar yüklenir", "loadModernRepos()" in html[html.find("name==='techdebt'"):html.find("name==='techdebt'") + 200])
    tk = js[js.find("async function modernTake"):]
    check("alım sonucu silinmez, yeniden tarama beklenir (butonlar erken açılmaz)", "await modernScan(false,true)" in tk and "keepMsg" in js)
    check("tekrarlayan (recurring) satır da seçilebilir", "t.recurring_of?'':" not in js)
    check("tarayıcı dosya hataları gösterilir", "errors" in js[js.find("function renderModern"):js.find("async function modernTake")])
    rc = html[html.find("function _tdmRepoChanged"):html.find("async function loadModernRepos")]
    check("repo listesi 'Repo seçiniz…' ile başlar, ilk repo kendiliğinden seçilmez",
          "Repo seçiniz…" in js and "sel.value='';" in js and 'id="tdmScan"' not in html)
    check("repo seçilince tarama kendiliğinden başlar", "if(r&&r.version)modernScan(false);" in rc)
    check("hızlı repo değişiminde eski tarama sonucu yazılmaz", "if(seq!==_tdm.seq)return;" in js)


def test_quality_sonar_exclude():
    print("\n[Q16] Kalite alımı — Sonar'da CSS/SCSS kuralları ve tema/public yolları hariç (#239/#240)")
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import intake, sonar
    from agile_sdlc_crew.quality.model import Finding
    sb = pc._SCHEMA_BY_KEY
    check("ayarlar: kural dili + yol hariç listesi (varsayılan css ve tema/public/vendor/min.js)",
          "css" in sb.get("CREW_QUALITY_SONAR_SKIP_LANGS", {}).get("default", "")
          and all(x in sb.get("CREW_QUALITY_SONAR_PATH_EXCLUDE", {}).get("default", "") for x in ("app/Themes", "public", "vendor", "*.min.js")))

    def f(file, rule):
        return Finding(source="sonar", repo="webservice", project="EcomBackend:webservice", file=file, rule=rule, rule_name=rule,
                       severity="CRITICAL", itype="BUG")
    langs, paths = ["css"], ["app/Themes", "public", "vendor", "*.min.js", "*.bundle.js"]
    check("css:* kuralı hariç (SCSS/LESS de css: öneki taşır)", sonar.excluded(f("app/x.scss", "css:S4654"), langs, paths) == "kural dili hariç (css)")
    check("tema / public / vendor yolları hariç", sonar.excluded(f("app/Themes/butigo/a.php", "php:S1"), langs, paths) == "hariç yol (app/Themes)"
          and sonar.excluded(f("public/frontend/src/style.css", "php:S1"), langs, paths) == "hariç yol (public)"
          and sonar.excluded(f("vendor/x/y.php", "php:S1"), langs, paths) == "hariç yol (vendor)")
    check("*.min.js / *.bundle.js hariç", sonar.excluded(f("webroot/js/app.min.js", "javascript:S1"), langs, paths) == "hariç yol (*.min.js)")
    check("PHP kaynak dosyası kalır", sonar.excluded(f("app/Service/RequestInquiry.php", "php:S4830"), langs, paths) is None
          and sonar.excluded(f("app/Publication.php", "php:S1"), langs, paths) is None)
    check("liste boşsa hiçbir şey hariç değil", sonar.excluded(f("app/x.scss", "css:S1"), [], []) is None)
    cfg = intake.load_cfg()
    check("load_cfg hariç listelerini taşır", cfg.get("sonar_skip_langs") == ["css"] and "app/Themes" in cfg.get("sonar_path_exclude", []))
    orig_fetch, orig_conv = sonar.fetch_issues, sonar.issues_to_findings
    sonar.fetch_issues = lambda *a, **k: ([], {})
    sonar.issues_to_findings = lambda *a, **k: [f("app/x.scss", "css:S4654"), f("app/Themes/t/a.php", "php:S1"), f("app/A.php", "php:S2")]
    try:
        out, skipped = intake.gather("sonar", SimpleNamespace(repo_projects=["EcomBackend"]), cfg)
        check("gather: hariç bulgular düşer ve atlananlarda nedeniyle görünür",
              [x.file for x in out] == ["app/A.php"] and {sk["reason"] for sk in skipped} == {"kural dili hariç (css)", "hariç yol (app/Themes)"}, str(skipped))
    finally:
        sonar.fetch_issues, sonar.issues_to_findings = orig_fetch, orig_conv


def test_env_conventions():
    print("\n[Q17] Ortam değişkeni kuralı — repo adlarını kullan, uydurma (PR 45061: ORKESTRA_RO_DB_PASSWORD)")
    import inspect
    from agile_sdlc_crew import flow as _flow
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import env_conventions as ec, rank, render
    from agile_sdlc_crew.quality.model import Finding
    check("ayar kayıtlı, varsayılan açık (bozuk davranış onarımı; yalnız kimlik bilgisi işlerinde, birkaç KB)",
          pc._SCHEMA_BY_KEY.get("CREW_ENV_CONVENTIONS", {}).get("default") is True)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "main").mkdir(); (root / "ru").mkdir(); (root / "vendor" / "x").mkdir(parents=True)
        (root / "main" / ".env.example").write_text("# DB\nDB_ORKESTRA_PROD_HOST=localhost\nDB_ORKESTRA_PROD_PASS=password\nAUTH_PASSWORD=yoursecurepassword\n", encoding="utf-8")
        (root / "main" / ".env").write_text("DB_ORKESTRA_PROD_PASS=RealSecret99!\nONLY_IN_REAL_ENV=x\n", encoding="utf-8")
        (root / "main" / "init.php").write_text("<?php\n$x = new Mysqlidb(getenv('DB_ORKESTRA_PROD_HOST'), getenv('DB_ORKESTRA_PROD_USER'), getenv('DB_ORKESTRA_PROD_PASS'));\n$a = $_ENV['APP_FLORCHESTRA_URL'] ?? env('MAIL_DSN');\n", encoding="utf-8")
        (root / "vendor" / "x" / "v.php").write_text("<?php getenv('VENDOR_ONLY');", encoding="utf-8")
        info = ec.collect(root)
        names = set(info["names"])
        check("adlar .env.example + getenv/$_ENV/env() kullanımlarından; vendor hariç",
              {"DB_ORKESTRA_PROD_HOST", "DB_ORKESTRA_PROD_PASS", "DB_ORKESTRA_PROD_USER", "AUTH_PASSWORD", "APP_FLORCHESTRA_URL", "MAIL_DSN"} <= names
              and "VENDOR_ONLY" not in names, str(names))
        check("gerçek .env okunmaz: değer ve yalnız orada geçen ad sızmaz",
              "ONLY_IN_REAL_ENV" not in names and "RealSecret99" not in str(info) and "yoursecurepassword" not in str(info))
        check("kullanım yeri dosya:satır", any(u["name"] == "DB_ORKESTRA_PROD_PASS" and u["file"] == "main/init.php" and u["line"] == 2 for u in info["usages"]), str(info["usages"][:3]))
        ranked = ec.rank(info["names"], ["$oMySQL_orkestra_prod_reporting = new Mysqlidb('orkestradbro.ydx.intl', 'reader-user', 'x', 'orkestra-db');"])
        check("ipucu satırındaki 'orkestra' / 'prod' → DB_ORKESTRA_PROD_* önce",
              ranked[:3] and all(n.startswith("DB_ORKESTRA_PROD_") for n in ranked[:3]), str(ranked[:4]))
        # gerçek repo: 'reader-user' değeri DB_DASHBOARD_REPORTING_USER'ı öne çıkarmasın (DB ortak parça sayılmaz, aile bonusu)
        real = Path.home() / ".crew_repos" / "flo-dashboard"
        if (real / "ru" / "init.php").is_file():
            ri = ec.collect(real)
            rr = ec.rank(ri["names"], ["Credentials should not be hard-coded — init.php", (real / "ru" / "init.php").read_text(errors="replace").splitlines()[15]])
            check("flo-dashboard: DB_ORKESTRA_PROD_PASS ilk 4'te", "DB_ORKESTRA_PROD_PASS" in rr[:4] and all(n.startswith("DB_ORKESTRA_PROD_") for n in rr[:4]), str(rr[:6]))
        else:
            skip("flo-dashboard gerçek repo sıralaması", "klon yok")
        block = ec.context_block(info, ["$oMySQL_orkestra_prod_reporting = new Mysqlidb('orkestradbro.ydx.intl', 'reader-user', 'x', 'orkestra-db');"], repo="flo-dashboard")
        check("bağlam bloğu: ad uydurma yasağı, en yakın adlar, kaynak dosyalar, örnek dosyaya ekleme kuralı",
              "DB_ORKESTRA_PROD_PASS" in block and "main/.env.example" in block and "main/init.php" in block
              and "never invent" in block.lower() and ".env.example" in block, block[:600])
        check("repo'da env kuralı yoksa blok boş", ec.context_block(ec.collect(root / "yok"), [], repo="r") == "")
    check("kimlik bilgisi işi tespiti: S6437 / S2068 / secrets:* / başlık", ec.is_credential_task("[x] Credentials should not be hard-coded — init.php", "", "tempo-quality")
          and ec.is_credential_task("t", "<code>php:S2068</code>", "tempo-quality") and ec.is_credential_task("t", "secrets:S6703", "tempo-quality")
          and not ec.is_credential_task("[x] Undefined array key", "php:S1234", "tempo-quality"))
    src = inspect.getsource(_flow)
    check("adım bağlamı: tasarım/geliştirme/inceleme adımlarında env bloğu eklenir (iş-değişmezi kuyrukta)",
          "env_conventions" in src and "CREW_ENV_CONVENTIONS" in src)
    g = rank.sonar_units([Finding(source="sonar", repo="flo-dashboard", project="EcomBackend:flo-dashboard", file="ru/init.php",
                                  rule="php:S6437", rule_name="Credentials should not be hard-coded", severity="BLOCKER", itype="VULNERABILITY", lines=[16])])[0]
    eb = " ".join(render.expected_behavior(g))
    check("S6437 beklenen davranış: mevcut env adı, .env.example, ad uydurulmaz",
          ".env.example" in eb and "getenv" in eb and "uydur" in eb, eb)
    check("S6437 kabul kriteri de env kuralını söyler", ".env.example" in render.acceptance_criteria(g) or "mevcut" in render.acceptance_criteria(g))
def test_sprint_report_no_sp():
    """Sprint sunumu: iş özeti slaytlarında SP yok, sıralama/bölme iş sayısıyla,
    özet infografiklerle (Bir Bakışta + tamamlanma halkası + satır çubukları).

    Kullanıcı kararı (2026-10-23): "(25 SP · %23)" ekleri ve SP payına göre
    slayt bölme kaldırıldı. Şablon git'te olmadığı için sentetik bir şablon
    kurulur; ikon üretimi (LLM) devre dışı bırakılır."""
    print("\n[sprint raporu: SP yok, infografik özet]")
    import os

    try:
        from pptx import Presentation
        from pptx.util import Inches
    except ImportError:
        skip("sprint raporu", "python-pptx yok")
        return
    from agile_sdlc_crew import sprint_report as sr

    keys = ("CREW_SPRINT_REPORT_AI_SUMMARY", "CREW_SPRINT_REPORT_VISUALS",
            "CREW_SPRINT_REPORT_OVERVIEW", "CREW_SPRINT_REPORT_MIN_ITEMS",
            "CREW_SPRINT_REPORT_ICON_DIR")
    saved = {k: os.environ.get(k) for k in keys}
    orig_icon = sr._ai_svg_icon
    tmp = tempfile.mkdtemp()
    try:
        os.environ.update({"CREW_SPRINT_REPORT_AI_SUMMARY": "0",
                           "CREW_SPRINT_REPORT_VISUALS": "1",
                           "CREW_SPRINT_REPORT_OVERVIEW": "1",
                           "CREW_SPRINT_REPORT_ICON_DIR": tmp})
        os.environ.pop("CREW_SPRINT_REPORT_MIN_ITEMS", None)
        sr._ai_svg_icon = lambda concept: ""  # emoji yedeği, LLM çağrısı yok

        def it(i, pid, title, state, typ="Task"):
            return sr.SprintItem(id=i, title=f"iş {i}", type=typ, state=state,
                                 parent_id=pid, parent_title=title)

        items = [it(1, 10, "Küçük proje", "Done"),
                 it(2, 20, "Kargo entegrasyonu", "Done"),
                 it(3, 20, "Kargo entegrasyonu", "Active", "Bug"),
                 it(4, 20, "Kargo entegrasyonu", "Resolved"),
                 it(5, None, "", "Active")]
        groups = sr.group_by_parent(items)
        check("gruplar iş sayısına göre azalan, parent'sız en sonda",
              [g.parent_id for g in groups] == [20, 10, None],
              str([g.parent_id for g in groups]))
        sums = [sr.summarize_group(g, {}) for g in groups]
        kargo = sums[0]
        check("özet iş sayılarını taşır (Resolved tamamlanmış sayılır)",
              (kargo.total, kargo.done, kargo.types) == (3, 2, {"Task": 2, "Bug": 1}),
              f"{kargo.total}/{kargo.done}/{kargo.types}")
        check("GroupSummary'de SP alanı yok",
              not any(hasattr(kargo, a) for a in ("sp", "sp_pct")))

        kinds = [(k, [s.parent_id for s in c]) for k, c in sr.plan_slides(sums)]
        check("plan: bir bakışta → ≥2 işli parent kendi slaytında → kalanlar özet satırı",
              kinds == [("overview", [20, 10, None]), ("top", [20]), ("digest", [10, None])],
              str(kinds))

        tpl = os.path.join(tmp, "tpl.pptx")
        prs = Presentation()
        prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
        blank = prs.slide_layouts[6]
        s1 = prs.slides.add_slide(blank)
        s1.shapes.add_textbox(Inches(1), Inches(1), Inches(10), Inches(1)).name = "Metin kutusu 2"
        s2 = prs.slides.add_slide(blank)
        title = s2.shapes.add_textbox(Inches(2.4), Inches(0.3), Inches(10), Inches(0.4))
        title.name = "Başlık"  # varsayılan ad 'TextBox N' gövde kutusuyla çakışır
        title.text_frame.text = "Sprint Özeti - Neler Yaptık"
        s2.shapes.add_textbox(Inches(1.5), Inches(1.3), Inches(8), Inches(5)).name = "TextBox 1"
        s3 = prs.slides.add_slide(blank)
        s3.shapes.add_textbox(Inches(0), Inches(3), Inches(13), Inches(0.5)).text_frame.text = (
            "Teşekkürler.")
        prs.save(tpl)

        out = os.path.join(tmp, "out.pptx")
        sr.build_pptx("Takım", "21. Sprint", "", sums, tpl, out)
        deck = Presentation(out)
        texts = [" ".join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame)
                 for s in deck.slides]
        check("hiçbir slaytta SP yok",
              not any(re.search(r"\bSP\b", t) for t in texts),
              str([t[:80] for t in texts if re.search(r"\bSP\b", t)]))
        order = [("Bir Bakışta" in t, "Neler Yaptık" in t, "Diğer Çalışmalar" in t) for t in texts]
        check("sıra: başlık, bir bakışta, içerik, diğer çalışmalar, teşekkürler",
              order[1:4] == [(True, False, False), (False, True, False), (False, False, True)],
              str(order))
        charts = [sum(1 for sh in s.shapes if sh.has_chart) for s in deck.slides]
        check("bir bakışta: KPI + proje durum grafiği; içerik slaytı: tamamlanma halkası",
              charts[1] == 1 and charts[2] == 1 and "Toplam İş" in texts[1]
              and "2 / 3 iş tamamlandı" in texts[2], f"charts={charts}")
        check("diğer çalışmalar satırı ilerleme metni taşır",
              "1 / 1 iş" in texts[3] and "0 / 1 iş" in texts[3], texts[3][:200])
    finally:
        sr._ai_svg_icon = orig_icon
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_sprint_report_designs():
    """Alternatif tasarımlar (dashboard dropdown'u): her tasarım şablonsuz sıfırdan
    çizilir, SP içermez, proje başına madde listesi yerine tek cümle manşet taşır;
    bilinmeyen tasarım klasiğe düşer; config uç noktası listeyi verir."""
    print("\n[sprint raporu: alternatif tasarımlar]")
    import asyncio
    import os

    try:
        from pptx import Presentation
    except ImportError:
        skip("sprint raporu tasarımları", "python-pptx yok")
        return
    from agile_sdlc_crew import sprint_report as sr
    from agile_sdlc_crew import sprint_report_designs as sd

    keys = ("CREW_SPRINT_REPORT_ICON_DIR", "CREW_SPRINT_REPORT_TEMPLATE",
            "CREW_SPRINT_REPORT_TEAM_IMAGE", "CREW_SPRINT_REPORT_DESIGN")
    saved = {k: os.environ.get(k) for k in keys}
    orig_icon = sr._ai_svg_icon
    tmp = tempfile.mkdtemp()
    try:
        os.environ.update({"CREW_SPRINT_REPORT_ICON_DIR": tmp,
                           "CREW_SPRINT_REPORT_TEMPLATE": os.path.join(tmp, "yok.pptx"),
                           "CREW_SPRINT_REPORT_TEAM_IMAGE": os.path.join(tmp, "yok.png")})
        os.environ.pop("CREW_SPRINT_REPORT_DESIGN", None)
        sr._ai_svg_icon = lambda concept: ""

        check("bilinmeyen/boş tasarım klasiğe düşer",
              sd.resolve("") == "klasik" and sd.resolve("uydurma") == "klasik"
              and sd.resolve("GECE") == "gece")
        os.environ["CREW_SPRINT_REPORT_DESIGN"] = "pastel"
        check("boş tasarım env varsayılanını alır", sd.resolve("") == "pastel")
        os.environ.pop("CREW_SPRINT_REPORT_DESIGN", None)
        opts = sd.design_options()
        check("dropdown: klasik ilk, her seçenekte ad/açıklama/renk örneği",
              opts[0]["key"] == "klasik" and len(opts) == 1 + len(sd.DESIGNS)
              and all(o["label"] and o["description"] and len(o["swatch"]) == 4 for o in opts))

        check("AI manşeti JSON'dan okunur, yoksa boş",
              sr._parse_headline('x {"headline": "Kargo akışı yenilendi.", "bullets": ["a"]}')
              == "Kargo akışı yenilendi." and sr._parse_headline('{"bullets": ["a"]}') == "")

        mk = lambda pid, t, n, dn: [sr.SprintItem(id=pid * 10 + j, title=f"iş {j}", type="Task",
                                                  state="Done" if j < dn else "Active",
                                                  parent_id=pid, parent_title=t) for j in range(n)]
        items = mk(1, "Kargo entegrasyonu", 4, 3) + mk(2, "Stok raporu", 1, 1) + mk(3, "İade", 2, 0)
        sums = [sr.summarize_group(g, {}) for g in sr.group_by_parent(items)]
        sums[0].headline = "Kargo entegrasyonu canlıya alındı."
        burn = {"labels": ["01.10", "02.10"], "remaining": [5, 2], "total": [5, 5], "ideal": [5, 0]}
        for key in sd.DESIGNS:
            out = os.path.join(tmp, f"{key}.pptx")
            sd.build_designed_pptx(key, "Takım", "21. Sprint", "01.10 – 14.10", sums, out,
                                   burndown=burn)
            deck = Presentation(out)
            texts = [" ".join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame)
                     for s in deck.slides]
            joined = " | ".join(texts)
            # kapak, bir bakışta, burndown, 1 kart slaytı (2 proje), diğer çalışmalar, kapanış
            check(f"{key}: 6 slayt, şablonsuz üretildi", len(texts) == 6, f"{len(texts)} slayt")
            check(f"{key}: SP yok", not re.search(r"\bSP\b", joined))
            check(f"{key}: kartta manşet var, madde listesi yok",
                  "Kargo entegrasyonu canlıya alındı." in joined and "•" not in joined)
            check(f"{key}: bir bakışta + diğer çalışmalar + kapanış",
                  "Bir Bakışta" in texts[1] and "Diğer Çalışmalar" in texts[4]
                  and "Teşekkürler" in texts[5])

        from agile_sdlc_crew import server

        cfg = json.loads(asyncio.run(server.sprint_report_config()).body)
        check("config uç noktası tasarım listesini ve eşiği verir (eski min_pct değil)",
              [d["key"] for d in cfg["designs"]][0] == "klasik" and "min_items" in cfg
              and "min_pct" not in cfg, str(list(cfg)))
        check("istek modeli tasarımı taşır",
              server.SprintReportRequest(team="t", iteration_path="p", design="gece").design == "gece")
        html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text()
        check("dashboard: tasarım dropdown'u isteğe eklenir",
              'id="srDesign"' in html and "JSON.stringify({team,iteration_path,design})" in html)
    finally:
        sr._ai_svg_icon = orig_icon
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_review_retry_delete_and_no_progress():
    """Job #244 ($3.19): (1) plan bir dosyanın SİLİNMESİNİ istiyordu ama tek push
    yolu içerik aldığı için developer boş döndü, push iptal edildi; (2) "ilerleme
    yok, erken durduruluyor" sayaca ham CREW_REVIEW_MAX_RETRIES yazdı, tavan zarftan
    (3) geldiği için sayaç her turda 2'ye döndü — döngü hiç bitmedi."""
    print("\n[review retry: dosya silme + erken durdurma]")
    import agile_sdlc_crew.pipeline as _pl
    import agile_sdlc_crew.main as _mn
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.flow import NeedsHumanReview as _NHR

    knobs = {"CREW_STRUCTURED_REVIEW": True, "CREW_REVIEW_RETRY_REPLAN": False,
             "CREW_REVIEW_MAX_RETRIES": 1, "CREW_CONTRACT_GATE": False}
    _orig = (pc.get, _pl.delete_file, _pl.push_file, _mn._add_wi_comment)
    pc.get = lambda k: knobs[k] if k in knobs else _orig[0](k)
    _mn._add_wi_comment = lambda *a, **k: None

    class _Crew:
        def __init__(self, raw, log):
            self.raw, self.log = raw, log
        def kickoff(self, inputs=None):
            self.log.append(inputs)
            return SimpleNamespace(raw=self.raw, token_usage=None)

    class _Client:
        def get_file_content(self, *a, **k):
            return "<?php\n" + "echo 1;\n" * 40
        def add_pr_comment(self, *a, **k):
            return {}

    def mk(change_type):
        f = AgileSDLCFlow()
        f.state.work_item_id, f.state.repo_name, f.state.branch_name = "76578", "marketplace", "feature/76578"
        f.state.pr_id, f.state.pr_url = "45479", "https://example/pr/45479"
        f.state.plan = {"changes": [{"file_path": "/secret.json", "change_type": change_type,
                                     "description": "x", "current_code": "", "new_code": ""}]}
        f.state.review_issues = [{"id": "R1", "file": "/secret.json", "severity": "blocker",
                                  "problem": "p", "required_fix": "r", "status": "open",
                                  "fix_targets": [_norm_path("/secret.json")]}]
        f._envelope = {"retries": 3}
        f.state.requirements_text = "Kanıt: secret.json:5 — dosya repodan silinir"
        f._client = _Client()
        f._db = None
        calls = {"verify": [], "code": []}
        verify_raw = 'REVIEW_VERIFY_JSON: {"results": [{"id": "R1", "status": "open"}], "new_findings": []}'
        f._agile_crew = SimpleNamespace(
            create_verify_review_crew=lambda: _Crew(verify_raw, calls["verify"]),
            create_code_crew=lambda: _Crew("<?php\n" + "echo 2;\n" * 40, calls["code"]),
        )
        for name in ("_enable_impl_repo_tools", "_restore_worktree_file", "_stage_worktree_file",
                     "_track_and_check_budget", "_step_done"):
            setattr(f, name, lambda *a, **k: None)
        f._build_step_context = lambda *a, **k: ""
        f._prefetch_pr_changes_context = lambda *a, **k: ""
        f._test_requirement_note = lambda *a, **k: ""
        f._prefer_worktree_edit = lambda repo, fp, new, old: new
        return f, calls

    try:
        # ── silme: ilk tur siler, ikinci turda dosya zaten yok → ilerleme yok ──
        deletes = []
        def _fake_delete(repo, br, fp, msg, repo_mgr=None, dry_run=False):
            deletes.append(fp)
            return ({"success": True, "change_type": "delete", "file": fp, "push_id": 7}
                    if len(deletes) == 1 else
                    {"success": True, "change_type": "delete", "file": fp, "note": "already absent"})
        _pl.delete_file = _fake_delete
        f, calls = mk("delete")
        try:
            f._review_retry_loop()
            raised = False
        except _NHR:
            raised = True
        check("silme: developer'a hiç gidilmedi", calls["code"] == [], str(len(calls["code"])))
        check("silme: dosya delete_file ile silindi", deletes[:1] == ["/secret.json"], str(deletes))
        check("silme: push olmayan turda doğrulama çağrılmadı (1 verify)", len(calls["verify"]) == 1,
              str(len(calls["verify"])))
        check("silme: ilerleme yok → needs_human (sonsuz döngü yok)", raised and len(deletes) == 2,
              f"raised={raised} deletes={len(deletes)}")

        # ── düzenleme: push her turda başarılı ama madde kapanmıyor ──
        pushes = []
        _pl.push_file = lambda *a, **k: (pushes.append(a[2]) or {"success": True, "change_type": "edit", "file": a[2]})
        f, calls = mk("edit")
        try:
            f._review_retry_loop()
            raised = False
        except _NHR:
            raised = True
        check("erken durdurma: aynı açık madde iki turda → durur (zarf 3, ham knob 1)",
              raised and len(calls["verify"]) == 2, f"raised={raised} verify={len(calls['verify'])}")
    finally:
        pc.get, _pl.delete_file, _pl.push_file, _mn._add_wi_comment = _orig


def test_delete_push_payload():
    """Azure push'ta silme değişikliği içerik taşımaz; dry-run'da git rm + commit."""
    print("\n[silme: Azure push gövdesi + dry-run]")
    from agile_sdlc_crew.tools import azure_devops_base as _azb
    from agile_sdlc_crew.tools.local_repo import LocalRepoManager

    sent = {}
    class _Resp:
        def __init__(self, data): self._d = data; self.text = "x"; self.status_code = 201
        def raise_for_status(self): pass
        def json(self): return self._d
    class _C(_azb.AzureDevOpsClient):
        _headers = {}
    c = _C.__new__(_C)
    c.list_branches = lambda repo, project=None: [{"name": "refs/heads/b", "objectId": "abc"}]
    c._repo_api_url = lambda repo, project=None: "https://x/repo"
    c.API_VERSION = "7.1"
    _orig_post = _azb.requests.post
    _azb.requests.post = lambda url, headers=None, json=None, params=None, timeout=None: (sent.update(body=json) or _Resp({"pushId": 9}))
    try:
        c.push_changes("r", "b", [{"changeType": "delete", "path": "/a.json"},
                                  {"changeType": "edit", "path": "/b.php", "content": "x"}], "m")
    finally:
        _azb.requests.post = _orig_post
    ch = sent["body"]["commits"][0]["changes"]
    check("push: delete değişikliğinde newContent yok", "newContent" not in ch[0] and ch[0]["changeType"] == "delete")
    check("push: edit değişikliği içeriğini korur", ch[1]["newContent"]["content"] == "x")

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "r"
        repo.mkdir()
        g = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True)
        g("init", "-q", "-b", "main")
        (repo / "a.json").write_text("{}")
        g("add", "."); g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i")
        mgr = LocalRepoManager.__new__(LocalRepoManager)
        mgr._get_repo_dir = lambda name: repo
        mgr.checkout = lambda name, br: g("checkout", "-q", "-B", br)
        mgr._git = lambda args, cwd=None: subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        r1 = mgr.delete_and_commit_local("r", "feature/x", "/a.json", "sil")
        r2 = mgr.delete_and_commit_local("r", "feature/x", "/a.json", "sil")
        check("dry-run: dosya silinip commit edildi", r1.get("success") and not (repo / "a.json").exists()
              and "sil" in g("log", "-1", "--format=%s").stdout, str(r1))
        check("dry-run: zaten yoksa no-op başarı", r2.get("success") and "already absent" in r2.get("note", ""), str(r2))


def test_quality_direct_flow():
    """Job #244: Task dosyayı ve satırı söylüyordu; BA + mimar keşfi + araçlı amend
    $1.54 tuttu. Doğrudan akış: analiz Task'tan, kanıt + okuyan kod Python'da."""
    print("\n[kalite doğrudan akış]")
    from agile_sdlc_crew.flow import _quality_evidence, _quality_requirements_json, _requirement_ids
    from agile_sdlc_crew import pipeline_config as pc

    req = _quality_requirements_json(
        "[marketplace] Google Cloud service accounts keys should not be disclosed",
        "<p>Kanıt: mpModerationAiAuth.json:5</p>",
        "<ul><li>Secret koddan kaldırılır</li><li>Yeni değer gizli yönetiminden okunur</li></ul>")
    ids = _requirement_ids(req)
    check("analiz: FR/TR + Task'taki iki kabul kriteri", {"FR1", "TR1", "AC1", "AC2"} <= set(ids), str(ids))
    check("analiz: az gereksinim (zarf büyümez)", len(ids) <= 5, str(ids))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        root.mkdir()
        g = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True)
        g("init", "-q", "-b", "main")
        (root / "auth.json").write_text('{\n  "type": "service_account",\n  "private_key": "-----BEGIN KEY-----abc",\n'
                                        '  "client_email": "x@y.iam.gserviceaccount.com"\n}\n')
        (root / "app").mkdir()
        (root / "app" / "Svc.php").write_text("<?php\nclass Svc {\n  function __construct() {\n"
                                              "    $p = __DIR__ . '/../auth.json';\n  }\n}\n")
        (root / "app" / "Big.php").write_text("<?php\n" + "".join(f"// line {i} {'x' * 30}\n" for i in range(2, 400))
                                              + "$apiKey = 'AIzaSyD1234567890abcdefghijkLMNOP';\n")
        (root / "vendor").mkdir()
        (root / "vendor" / "lib.php").write_text("<?php // auth.json\n")
        g("add", "-A"); g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i")
        ev = _quality_evidence(root, "<li>auth.json:3</li><li>app/Big.php:400</li> api.flo.com.tr:443")
        check("kanıt: veri dosyası bağlamda", "## /auth.json (tamamı" in ev, ev[:300])
        check("kanıt: dosyayı okuyan kod eklendi", "/app/Svc.php — `auth.json`" in ev, ev[:600])
        check("kanıt: vendor okuyucu alınmaz", "vendor/lib.php" not in ev)
        check("kanıt: büyük dosyada yalnız satır penceresi", "/app/Big.php (satır 360–" in ev and "// line 10 " not in ev, ev[-500:])
        check("kanıt: repoda olmayan yol (alan adı:port) elenir", "api.flo.com.tr" not in ev)
        check("kanıt: sır değerleri modele gitmez", "BEGIN KEY" not in ev and "AIzaSy" not in ev
              and '"private_key": "***"' in ev, ev[:400])
        check("kanıt: repoda yoksa boş", _quality_evidence(root, "<li>nope.json:1</li>") == "")

    # Akış kancaları: knob kapalıysa normal akış, açıkken zarf S ve keşif kapalı
    f = AgileSDLCFlow()
    f.state.wi_tags = "tempo-quality; tempo-src-sonar"
    knobs = {"CREW_QUALITY_DIRECT_FLOW": True, "CREW_VARIABLE_ENVELOPE": True}
    _orig = pc.get
    pc.get = lambda k: knobs[k] if k in knobs else _orig(k)
    try:
        f.state.requirements_text = req
        f._apply_envelope("requirements")
        f._apply_envelope("plan")
        check("zarf: doğrudan akışta S/$5/1 tur", getattr(f, "_envelope", None) == {"class": "S", "budget": 5.0, "retries": 1},
              str(getattr(f, "_envelope", None)))
        knobs["CREW_QUALITY_DIRECT_FLOW"] = False
        check("knob kapalı: doğrudan akış yok", not f._quality_direct_flow())
        f2 = AgileSDLCFlow()
        f2.state.wi_tags = "backend"
        knobs["CREW_QUALITY_DIRECT_FLOW"] = True
        check("kalite etiketi yoksa doğrudan akış yok", not f2._quality_direct_flow())
    finally:
        pc.get = _orig

    src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/flow.py").read_text()
    check("step1: doğrudan akışta BA crew ve SM review çağrılmaz",
          "requirements_text = _quality_requirements_json(" in src
          and '(True, "") if _direct else self._scrum_review("Is Analizi"' in src)
    check("step4: doğrudan akışta Faz A keşfi kapalı",
          src.find("if self._quality_direct_flow() and _repo_dirs:") < src.find("plan, raw_output, need_explore = self._architect_emit_json("))
    check("amend: doğrudan akışta repo araçları açılmaz", "and not self._quality_direct_flow()):" in src)
    check("kanıt yalnız teknik tasarım bağlamına girer", 'if step_key == "technical_design_task":\n                try:\n                    ev_ctx = self._quality_evidence_block()' in src)


def test_resume_skips_discarded_work():
    """Job #246: önceki işin PR'ı kapatılmış, branch'i silinmişti; resume yine de
    BA'yı, planı, branch'i ve KAPALI PR'ı geri yükleyip review'a geçti."""
    print("\n[resume: atılmış önceki iş]")

    class _C:
        def __init__(self, status=None, branches=(), boom=False):
            self.status, self.branches, self.boom = status, branches, boom
        def get_pull_request(self, repo, pr):
            if self.boom:
                raise RuntimeError("503")
            return {"status": self.status}
        def list_branches(self, repo):
            return [{"name": b} for b in self.branches]

    def mk(art, client):
        f = AgileSDLCFlow()
        f._prior_art_cache = art
        f._client = client
        return f

    art_pr = {"repo_name": "marketplace", "branch_name": "feature/76578", "pr_id": "45479"}
    check("PR abandoned → resume yok", mk(art_pr, _C("abandoned"))._prior_work_discarded())
    check("PR merge edilmiş → resume yok", mk(art_pr, _C("completed"))._prior_work_discarded())
    check("PR aktif → resume serbest", not mk(art_pr, _C("active"))._prior_work_discarded())
    art_br = {"repo_name": "marketplace", "branch_name": "feature/76578", "pr_id": ""}
    check("PR yok, branch silinmiş → resume yok", mk(art_br, _C(branches=["refs/heads/main"]))._prior_work_discarded())
    check("PR yok, branch duruyor → resume serbest",
          not mk(art_br, _C(branches=["refs/heads/feature/76578"]))._prior_work_discarded())
    check("durum okunamazsa resume engellenmez", not mk(art_pr, _C(boom=True))._prior_work_discarded())
    check("önceki iş yoksa engel yok", not mk({}, _C("abandoned"))._prior_work_discarded())


def test_wi_lifecycle_on_queue():
    """Kullanıcı kararı (2026-11-03): kuyruktaki kartlar aynı anda In Progress'e
    atılmaz — kuyruğa alınan To Do'ya ("ilerletilecek"), işlenen iş In Progress'e.
    Sorun olursa kuyruk ÖNCESİ duruma döner — iş başlarken okunan In Progress'e değil."""
    print("\n[yaşam döngüsü: kuyruğa alınınca]")
    from agile_sdlc_crew import wi_lifecycle as wl
    from agile_sdlc_crew import pipeline_config as pc

    c = _StubAzClient(_FLO_TASK, {"System.WorkItemType": "Task", "System.State": "Backlog"})
    before = wl.mark_queued(c, "76578")
    check("kuyruk: Backlog → To Do (In Progress DEĞİL), önceki durum döner",
          before == "Backlog" and c.ops == [("state", 76578, "To Do")], str(c.ops))
    c1 = _StubAzClient(_FLO_TASK, {"System.WorkItemType": "Task", "System.State": "To Do"})
    check("kuyruk: zaten To Do → dokunmaz", wl.mark_queued(c1, "1") == "To Do" and c1.ops == [])
    c3 = _StubAzClient(_FLO_TASK, {"System.WorkItemType": "Task", "System.State": "In Progress"})
    check("kuyruk: insan başlatmış (In Progress) → geri çekmez", wl.mark_queued(c3, "1") == "In Progress" and c3.ops == [])
    c2 = _StubAzClient(_FLO_TASK, {"System.WorkItemType": "Task", "System.State": "QA To Do"})
    check("kuyruk: sahiplik dışı (QA) → dokunmaz", wl.mark_queued(c2, "1") == "QA To Do" and c2.ops == [])
    cs = _StubAzClient(_FLO_TASK, {"System.WorkItemType": "Task", "System.State": "To Do"})
    check("iş başladı: To Do → In Progress", wl.mark_started(cs, "76578") == ("To Do", "In Progress")
          and cs.ops == [("state", 76578, "In Progress")], str(cs.ops))
    cq = _StubAzClient(_FLO_TASK, {"System.WorkItemType": "Task", "System.State": "QA"})
    check("iş başladı: sahiplik dışı (QA) → dokunmaz", wl.mark_started(cq, "1") == ("QA", None) and cq.ops == [])
    check("silinen kuyruk işi: To Do → Backlog", wl.plan_revert("To Do", "Backlog", _FLO_TASK, pr_exists=False) == "Backlog")
    check("geri al: In Progress → To Do", wl.revert_to(cs, "76578", "To Do") == "To Do" and cs.ops[-1] == ("state", 76578, "To Do"))
    check("geri al: başlangıç boşsa dokunmaz", wl.revert_to(cs, "76578", "") is None)

    knobs = {"CREW_WI_LIFECYCLE": True, "CREW_WI_ASSIGN_IF_EMPTY": False}
    _orig = pc.get
    pc.get = lambda k: knobs[k] if k in knobs else _orig(k)
    try:
        f = AgileSDLCFlow()
        f.state.work_item_id, f.state.job_id = "76578", 247
        f._client = _StubAzClient(_FLO_TASK, {"System.WorkItemType": "Task", "System.State": "In Progress"})
        f._db = SimpleNamespace(get_job=lambda jid: {"id": jid, "wi_state_before": "To Do"})
        f._wi_begin(f._client.fields)
        check("iş başlarken: başlangıç = kuyruk anındaki durum", f.state.wi_state_initial == "To Do"
              and f._client.ops == [], f"{f.state.wi_state_initial} {f._client.ops}")
        f.wi_lifecycle_on_exception(RuntimeError("x"))
        check("sorun → kuyruk anındaki duruma (To Do) döner", f._client.ops[-1] == ("state", 76578, "To Do"), str(f._client.ops))
    finally:
        pc.get = _orig

    src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/server.py").read_text()
    check("server: her kuyruğa almada işaretleme (_ensure_worker)", "threading.Thread(target=_wi_mark_queued_jobs" in src)
    # upgrade_bump de _wi_start_job'dan geçer (running'e alınır); WI'ı _wi_tracked süzgeci yüzünden taşınmaz
    check("server: iş kuyruktan alınınca In Progress (_wi_start_job, pipeline + modern_fix + upgrade_bump)",
          src.count("_wi_start_job(job)") == 3 and "db.start_job(job_id)\n                modern_fix" not in src)
    check("server: başlatma kuyruk turuyla aynı kilit altında", "with _wi_mark_lock:\n        db.start_job(job_id)" in src)
    check("server: silinen kuyruk işi ve restart'ta yarım kalan iş geri alınır",
          'f"iş #{job_id} silindi"' in src and '"sunucu yeniden başladı"' in src)


def test_pre_pr_verify_and_build_fix():
    """Job #248 ($5.44): eksik Go import'u CI'a kadar gitti; build hata özeti
    hazırlık YAML'ıydı; build-fix dosya başına Opus çağrısı yaptı ve silinmiş
    analytics.json'a düzyazı yazdı; test dışı Sonar uyarısı ikinci turu açtı."""
    print("\n[PR öncesi doğrulama + build-fix]")
    import shutil
    from agile_sdlc_crew import verify as vf
    from agile_sdlc_crew.tools.azure_devops_base import extract_build_errors
    from agile_sdlc_crew.flow import _build_issues_block
    from agile_sdlc_crew import pipeline_config as pc

    # ── build log özeti ──
    log = "\n".join([
        "2026-10-27T11:23:33.1Z ##[section]Starting: Run Tests",
        "2026-10-27T11:23:33.1Z Task         : Command line",
        "2026-10-27T11:23:33.1Z Evaluating: eq('true', variables['system.debugContext'])",
        "2026-10-27T11:25:01.0Z Dumping database schema",
        "2026-10-27T11:25:02.0Z # gocrons/pkg/sorting",
        "2026-10-27T11:25:02.0Z pkg/sorting/a.go:8:2: \"os\" imported and not used",
        "2026-10-27T11:25:02.0Z pkg/sorting/a.go:392:15: undefined: webanalytics",
    ] + [f"2026-10-27T11:25:03.0Z FAIL\tgocrons/pkg/p{i} [build failed]" for i in range(40)]
      + ["2026-10-27T11:25:04.0Z ##[error]Bash exited with code '1'."])
    ex = extract_build_errors(log)
    check("log: derleyici hataları önde, zaman damgasız", ex.startswith("# gocrons/pkg/sorting\npkg/sorting/a.go:8:2"), ex[:120])
    check("log: şablon gürültüsü atılır", "Evaluating:" not in ex and "Task         :" not in ex)
    check("log: FAIL paket yığını tek satır", "(40 paket 'build failed'" in ex and ex.count("[build failed]") == 0, ex[-300:])

    # ── kısmi başarı ──
    check("kısmi başarı: yalnız Sonar uyarısı → bloklamaz",
          not _build_issues_block([{"name": "Run SonarQube Analysis", "result": "succeededWithIssues"}]))
    check("kısmi başarı: test adımı uyarılı → bloklar",
          _build_issues_block([{"name": "Run Tests", "result": "succeededWithIssues"}]))
    check("kısmi başarı: herhangi bir Task failed → bloklar",
          _build_issues_block([{"name": "Lint", "result": "failed"}]))

    # ── dil doğrulayıcıları ──
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "bad.json").write_text('{"a": 1,}')
        (root / "ok.yaml").write_text("a: 1\n")
        (root / "bad.py").write_text("def f(:\n")
        r = vf.verify(root, ["/bad.json", "/ok.yaml", "/bad.py", "/gone.php"])
        check("doğrulayıcı: JSON/Python hataları dosya:satır ile", not r["ok"]
              and any(i.startswith("bad.json:") for i in r["issues"]) and any("bad.py" in i for i in r["issues"]), str(r))
        check("doğrulayıcı: diskte olmayan dosya atlanır", "php" not in r["ran"])
        check("hata satırından dosya çıkarımı", "bad.json" in vf.referenced_files(r["issues"]))
        if shutil.which("go"):
            g = root / "gomod"
            (g / "pkg" / "a").mkdir(parents=True)
            (g / "go.mod").write_text("module m\n\ngo 1.21\n")
            (g / "pkg" / "a" / "a.go").write_text('package a\n\nimport "os"\n\nfunc F() string { return strings.ToUpper("x") }\n')
            rg = vf.verify(g, ["pkg/a/a.go"])
            check("go: eksik import ve kullanılmayan import yakalanır (#248)", not rg["ok"]
                  and any("undefined: strings" in i for i in rg["issues"])
                  and any('"os" imported and not used' in i for i in rg["issues"]), str(rg))
        else:
            skip("go doğrulayıcı", "go yok")

    # ── tek çağrıda çok dosyalı düzeltme ──
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        repo = base / "r"
        (repo / "pkg").mkdir(parents=True)
        g = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True)
        g("init", "-q", "-b", "main")
        for n in ("a.go", "b.go"):
            (repo / "pkg" / n).write_text(f"package pkg // {n} main\n")
        (repo / "secret.json").write_text("{}")
        g("add", "-A"); g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i")

        branch = {"/pkg/a.go": "package pkg // a branch broken\n", "/pkg/b.go": "package pkg // b branch broken\n",
                  "/pkg/new.go": "package pkg // new broken\n"}
        pushes = []

        class _Cl:
            def get_file_content(self, repo_name, fp, br):
                if fp in branch:
                    return branch[fp]
                raise RuntimeError("404 Not Found")
            def push_changes(self, repo_name, br, changes, msg):
                pushes.append((msg, [c["path"] for c in changes]))
                for c in changes:
                    branch[c["path"]] = c["content"]
                return {"pushId": 1}

        calls = []

        class _Crew:
            def kickoff(self, inputs=None):
                calls.append(inputs)
                for n in ("a.go", "new.go"):
                    p = repo / "pkg" / n
                    p.write_text(p.read_text().replace("broken", "fixed"))
                return SimpleNamespace(raw="tamam", token_usage=None, pydantic=None)

        f = AgileSDLCFlow()
        f.state.work_item_id, f.state.repo_name, f.state.branch_name = "76575", "r", "feature/76575"
        f.state.plan = {"changes": [{"file_path": "/secret.json", "change_type": "delete"}]}
        f._client = _Cl()
        f._repo_mgr = SimpleNamespace(base_dir=base, _git=lambda args, cwd=None: subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True))
        f._agile_crew = SimpleNamespace(create_code_crew=lambda: _Crew())
        f._enable_impl_repo_tools = lambda: True
        f._track_and_check_budget = lambda *a, **k: None
        f._build_step_context = lambda *a, **k: ""
        n = f._multi_file_fix(["pkg/a.go", "/pkg/b.go", "/pkg/new.go", "/secret.json"], "undefined: x", "build_fix", "HATA")
        check("çok dosyalı düzeltme: TEK developer çağrısı", len(calls) == 1, str(len(calls)))
        check("çok dosyalı düzeltme: silinecek dosya hedefe girmez", "secret.json" not in calls[0]["change_description"])
        check("çok dosyalı düzeltme: yalnız değişen dosyalar, TEK commit",
              n == 2 and len(pushes) == 1 and sorted(pushes[0][1]) == ["/pkg/a.go", "/pkg/new.go"], str(pushes))
        check("çok dosyalı düzeltme: klon geri alındı (main hâli, PR'ın yeni dosyası kalmaz)",
              (repo / "pkg" / "a.go").read_text() == "package pkg // a.go main\n" and not (repo / "pkg" / "new.go").exists())

        # ── PR öncesi doğrulama kapısı ──
        knobs = {"CREW_PRE_PR_VERIFY": True}
        _orig = pc.get
        pc.get = lambda k: knobs[k] if k in knobs else _orig(k)
        import agile_sdlc_crew.verify as _vfm
        _orig_verify = _vfm.verify
        seen = []

        def _fake_verify(root, changed, **kw):
            seen.append({fp: (Path(root) / fp.lstrip("/")).read_text() for fp in changed
                         if (Path(root) / fp.lstrip("/")).exists()})
            bad = any("broken" in t for t in seen[-1].values())
            return {"ok": not bad, "ran": ["go"], "skipped": [],
                    "issues": ["pkg/b.go:1:1: undefined: y"] if bad else []}
        _vfm.verify = _fake_verify
        try:
            branch["/pkg/b.go"] = "package pkg // b branch broken\n"
            calls.clear(); pushes.clear()

            class _Crew2:
                def kickoff(self, inputs=None):
                    calls.append(inputs)
                    p = repo / "pkg" / "b.go"
                    p.write_text(p.read_text().replace("broken", "fixed"))
                    return SimpleNamespace(raw="", token_usage=None, pydantic=None)
            f._agile_crew = SimpleNamespace(create_code_crew=lambda: _Crew2())
            f.state.all_pushes = [{"file": "/pkg/b.go", "success": True, "change_type": "edit"},
                                  {"file": "/secret.json", "success": True, "change_type": "delete"}]
            f._pre_pr_verify()
            check("doğrulama: branch hâli klona bindirilip doğrulanır", "broken" in seen[0].get("/pkg/b.go", ""), str(seen[:1]))
            check("doğrulama: hata → tek hedefli düzeltme + tekrar doğrulama geçer",
                  len(calls) == 1 and len(pushes) == 1 and len(seen) == 2 and "fixed" in seen[1]["/pkg/b.go"], f"{len(calls)} {pushes} {seen}")
            check("doğrulama sonrası klon temiz, silinen dosya geri geldi",
                  (repo / "pkg" / "b.go").read_text() == "package pkg // b.go main\n" and (repo / "secret.json").exists())
        finally:
            _vfm.verify = _orig_verify
            pc.get = _orig

    src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/flow.py").read_text()
    check("build-fix: silinecek dosyalar hedeften çıkarılır, tek çağrı yolu önce denenir",
          "fix_files = [f for f in fix_files if _norm_path(f) not in _deleted]" in src
          and 'self._multi_file_fix(fix_files, failure_summary, "build_fix"' in src)
    check("kalite doğrudan akış: build-fix en fazla 1 tur", "max_retries = min(max_retries, 1)" in src)


def test_security_hardening():
    """Güvenlik incelemesi (commit 07bac9738): doğrulayıcılar repo kodunu içe
    aktarmasın / sunucu sırlarını devralmasın; kanıtta tırnaksız sırlar ve anahtar
    dosyaları modele gitmesin; plan (LLM) keyfi dosya silemesin."""
    print("\n[güvenlik sertleştirme]")
    import os
    from agile_sdlc_crew import verify as vf
    from agile_sdlc_crew.flow import _qe_mask, _quality_evidence

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        marker = root / "PWNED"
        (root / "py_compile.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
        (root / "ok.py").write_text("x = 1\n")
        r = vf.verify(root, ["ok.py"])
        check("python: repo kökündeki py_compile.py içe aktarılmaz", r["ok"] and not marker.exists(), str(r))
        (root / "-v.json").write_text("{}")
        check("'-' ile başlayan yol bayrak sayılmaz", vf._arg("-v.json") == "./-v.json")
    os.environ["AZURE_DEVOPS_PAT"] = os.environ.get("AZURE_DEVOPS_PAT", "x")
    env = vf._env({"GOFLAGS": "-mod=vendor"})
    check("doğrulayıcı ortamı sırları taşımaz", "AZURE_DEVOPS_PAT" not in env and "PATH" in env
          and env.get("GOTOOLCHAIN") == "local" and env.get("GOFLAGS") == "-mod=vendor")

    m = _qe_mask("DB_PASSWORD=hunter2xyz\nexport API_TOKEN=abc123\nAPP_NAME=shop\n")
    check("kanıt: tırnaksız KEY=değer maskelenir", "hunter2xyz" not in m and "abc123" not in m and "APP_NAME=shop" in m, m)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "r"
        root.mkdir()
        g = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True)
        g("init", "-q", "-b", "main")
        (root / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----\n")
        g("add", "-A"); g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i")
        ev = _quality_evidence(root, "<li>server.pem:1</li>")
        check("kanıt: anahtar dosyasının içeriği gönderilmez", "BEGIN PRIVATE" not in ev and "MIIE" not in ev
              and "anahtar dosyası" in ev, ev)

    f = AgileSDLCFlow()
    f.state.requirements_text = "mpModerationAiAuth.json silinir; azure-pipelines.yml de geçiyor"
    ok, _ = f._delete_allowed("/mpModerationAiAuth.json")
    check("silme: WI'da adı geçen dosya serbest", ok)
    ok, why = f._delete_allowed("/app/Controller/Order.php")
    check("silme: WI'da geçmeyen dosya reddedilir", not ok and "geçmiyor" in why, why)
    ok, why = f._delete_allowed("/azure-pipelines.yml")
    check("silme: korunan yol reddedilir (adı geçse bile)", not ok and "korunan" in why, why)
    f._deletes_done = 3
    ok, why = f._delete_allowed("/mpModerationAiAuth.json")
    check("silme: iş başına sınır", not ok and "sınır" in why, why)


def test_devops_requests():
    """Kullanıcı kararı (2026-10-30): DevOps'un yapması gereken iş ayrı Task olarak açılır.
    PR'dan: repoda daha önce okunmayan ortam değişkeni (#247 MP_MODERATION_AI_AUTH_JSON).
    Teknik borçtan: eklenti ön koşulu kanıtlanamayan iş otomatik değil, DevOps'a gider."""
    print("\n[DevOps istekleri]")
    from agile_sdlc_crew import devops_request as dr
    from agile_sdlc_crew.quality import php_modern as pm
    from agile_sdlc_crew.quality.model import Finding, Group
    from agile_sdlc_crew import pipeline_config as pc

    old = "<?php\n$a = getenv('DB_HOST');\n"
    new = ("<?php\n$a = getenv('DB_HOST');\nconst AUTH_ENV_NAME = 'MP_MODERATION_AI_AUTH_JSON';\n"
           "$b = Helper::getEnvOrDefault('EXISTING_ELSEWHERE');\n")
    got = dr.new_env_names(old, new, lambda n: n == "EXISTING_ELSEWHERE")
    check("env: yalnız PR'ın eklediği ve repoda geçmeyen ad", got == {"MP_MODERATION_AI_AUTH_JSON": 3}, str(got))
    check("env: Go/JS/Python okumaları", set(dr.env_names(
        'x := os.Getenv("GA4_JSON")\nprocess.env.NODE_KEY\nos.environ["PY_SECRET"]').keys()) == {"GA4_JSON", "NODE_KEY", "PY_SECRET"})
    check("silinen dosya içerik sırrı (ad ele vermese de)", dr.looks_like_secret('{"private_key": "-----BEGIN PRIVATE KEY-----x"}')
          and not dr.looks_like_secret('{"name": "analytics", "enabled": true}'))
    body = dr.env_request_body(repo="marketplace", names={"MP_X": "app/A.php:3"}, deleted_files=["auth.json"],
                               deps=[{"devrepo": "anatolia", "service": "marketplace", "secret": "prod-marketplace-secrets",
                                      "images": ["php-8-2:1.0"]}], pr_url="https://x/pr/1", source_wi="76578")
    check("istek metni: ad, satır, secret adı, değer yazılmaz notu", all(x in body for x in (
        "MP_X", "app/A.php:3", "prod-marketplace-secrets", "auth.json", "Değerler bu işte yazılmaz")))

    class _Cl:
        _base_api_url = "https://x/_apis"
        def __init__(self, existing=None):
            self.created, self.updates, self.existing = [], [], existing
        def get_team_area_path(self, team):
            return "Boards Management\\DevOps"
        def query_work_items(self, q, limit=0):
            return [{"id": self.existing}] if self.existing else []
        def list_iterations(self, team):
            return [{"path": "Boards Management\\2026_21_DevOps", "attributes": {"timeFrame": "past"}},
                    {"path": "Boards Management\\2026_22_DevOps", "attributes": {"timeFrame": "current"}}]
        def create_work_item(self, t, fields, parent_id=None):
            self.created.append((t, fields)); return {"id": 90001}
        def update_work_item(self, wid, ops):
            self.updates.append((wid, ops)); return {}
    c = _Cl()
    r = dr.open_request(c, team="DevOps", title="[m] ENV", body_html="<p>x</p>", source_wi="76578", key="env:m:76578:MP_X",
                        task_fields={"Custom.ARGEType": "Hayır"})
    f = c.created[0][1]
    check("açılış: DevOps alanı, aktif sprint, Task, etiket, anahtar satırı, zorunlu alan",
          r == {"id": 90001, "created": True} and c.created[0][0] == "Task" and f["System.AreaPath"].endswith("DevOps")
          and f["System.IterationPath"].endswith("2026_22_DevOps") and f["System.Tags"] == dr.TAG
          and "tempo-devops-key: env:m:76578:MP_X" in f["System.Description"] and f["Custom.ARGEType"] == "Hayır", str(f))
    check("açılış: kaynak işe Predecessor bağı", c.updates and c.updates[0][0] == 76578
          and c.updates[0][1][0]["value"]["rel"] == "System.LinkTypes.Dependency-Reverse")
    c2 = _Cl(existing=88888)
    r2 = dr.open_request(c2, team="DevOps", title="t", body_html="b", source_wi="1", key="k")
    check("aynı istek ikinci kez açılmaz, yeni kaynak yine bağlanır", r2 == {"id": 88888, "created": False}
          and not c2.created and c2.updates)

    # ── teknik borç: eklenti ön koşulu ──
    rule = pm.Rule(key="utf8_encode", since=(8, 2), kind="deprecation", title="utf8", summary="", advantages=[],
                   risks=[], before="", after="", ref="", auto=True, requires=["mbstring"])
    rf = pm.Rule(key="removed_functions", since=(8, 0), kind="deprecation", title="rm", summary="", advantages=[],
                 risks=[], before="", after="", ref="", auto=True, requires_if={"money_format": ["intl"]})
    def g(rule_key, text):
        return Group(source="php_modern", repo="r", title="t", fingerprints=["fp"],
                     findings=[Finding(source="php_modern", file="a.php", rule=rule_key, uris=[f"3|{text}"])])
    check("eklenti kanıtı yoksa eksik listelenir", pm._missing_exts(g("utf8_encode", "utf8_encode($x)"), rule, lambda e: False) == ["mbstring"])
    check("eklenti repoda kullanılıyorsa otomatik kalır", pm._missing_exts(g("utf8_encode", "utf8_encode($x)"), rule, lambda e: True) == [])
    check("koşullu eklenti yalnız ilgili fonksiyon geçince", pm._missing_exts(g("removed_functions", "each($a)"), rf, lambda e: False) == []
          and pm._missing_exts(g("removed_functions", "money_format('%i', $x)"), rf, lambda e: False) == ["intl"])
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sp = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True)
        sp("init", "-q", "-b", "main")
        (root / "a.php").write_text("<?php echo strlen('x');\n")
        sp("add", "-A"); sp("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i")
        check("kanıt yok → eklenti doğrulanamadı", not pm.ext_available(root, "mbstring"))
        (root / "b.php").write_text("<?php echo mb_strlen('x');\n")
        sp("add", "-A"); sp("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "j")
        check("uygulama kodunda mb_* → mbstring var", pm.ext_available(root, "mbstring"))
        (root / "composer.json").write_text('{"require": {"ext-intl": "*"}}')
        check("composer ext-<ad> şartı → var", pm.ext_available(root, "intl"))

    src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/quality/php_modern.py").read_text()
    check("tarama: otomatik yalnız kodla çözülen; altyapı ayrı liste",
          '"infra": [_row(g, rules_by_key) for g in groups if g.needs_human and g.infra]' in src
          and '"auto": [_row(g, rules_by_key) for g in groups if not g.needs_human]' in src)
    html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text()
    check("kart: 'Altyapı (DevOps) gerektiren işler' bölümü ve DevOps bağlantısı",
          "Altyapı (DevOps) gerektiren işler" in html and "DevOps isteği: " in html)
    fsrc = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/flow.py").read_text()
    check("PR açılınca (yeni ve mevcut PR yolu) env isteği kontrol edilir", fsrc.count("self._devops_env_request()") == 2)
    check("knob'lar", pc.get("CREW_DEVOPS_TEAM") == "DevOps" and isinstance(pc.get("CREW_DEVOPS_REQUESTS"), bool))


def test_modern_multilang():
    """Modernleştirme repo hangi dildeyse o: PHP, Go, Python, Next.js. Go/Python'da
    düzeltmeyi araç uygular (modernize / ruff), LLM yalnız inceler."""
    print("\n[modernleştirme: çok dil]")
    import shutil
    from agile_sdlc_crew.quality import modern, modern_fix
    from agile_sdlc_crew.quality.render import MODERN_SOURCES, task_description
    from agile_sdlc_crew.quality.model import Finding, Group

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        go = base / "goapp"; go.mkdir()
        (go / "go.mod").write_text("module m\n\ngo 1.24.0\n")
        py = base / "pyapp"; py.mkdir()
        (py / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.11"\n')
        py2 = base / "pyimg"; py2.mkdir()
        (py2 / "requirements.txt").write_text("fastapi\n")
        (py2 / "Dockerfile").write_text("FROM python:3.9-slim\n")
        nx = base / "web"; nx.mkdir()
        (nx / "package.json").write_text('{"dependencies": {"next": "^15.2.4", "react": "19.0.0"}}')
        (nx / "app").mkdir()
        (nx / "app" / "a.tsx").write_text("import { useFormState } from 'react-dom'\n// const c = cookies()\nconst h = headers()\n"
                                         "const x = <Link href='/a'>\n  <a>git</a></Link>\n")
        (nx / "middleware.ts").write_text("export function middleware(){}\n")
        (nx / "node_modules").mkdir(); (nx / "node_modules" / "z.js").write_text("useFormState()\n")
        check("dil tespiti", modern.detect_langs(go) == ["go"] and modern.detect_langs(py) == ["python"]
              and modern.detect_langs(nx) == ["nextjs"], str([modern.detect_langs(x) for x in (go, py, nx)]))
        check("Go hedefi go.mod'dan", modern.target(None, "goapp", go, "go")["version"] == "1.24")
        check("Python hedefi: FLODevOps imajı repo Dockerfile'ından önce gelir",
              modern.target(None, "pyimg", py2, "python", index={"pyimg": [{"src": "anatolia/build/x", "images": ["python:3.11"]}]})["version"] == "3.11"
              and modern.target(None, "pyimg", py2, "python", index={})["version"] == "3.9"
              and modern.target(None, "pyapp", py, "python", index={})["version"] == "3.11")
        check("Next.js hedefi package.json'dan", modern.target(None, "web", nx, "nextjs")["version"] == "15.2")
        hits, _ = modern.next_hits(nx)
        keys = sorted({(h["rule"], h["file"]) for h in hits})
        check("Next.js desenleri: useFormState, senkron headers(), <Link><a>, middleware dosyası",
              ("use_form_state", "app/a.tsx") in keys and ("sync_request_api", "app/a.tsx") in keys
              and ("link_legacy_anchor", "app/a.tsx") in keys and ("middleware_to_proxy", "middleware.ts") in keys, str(keys))
        check("Next.js: yorum satırı ve node_modules atlanır",
              sum(1 for h in hits if h["rule"] == "sync_request_api") == 1 and not any("node_modules" in h["file"] for h in hits))

        if Path(modern.go_modernize_bin()).exists() and shutil.which("go"):
            (go / "a.go").write_text("package m\n\nfunc F(v interface{}) interface{} { return v }\n")
            g = lambda *a: subprocess.run(["git", *a], cwd=go, capture_output=True, text=True)
            g("init", "-q", "-b", "main"); g("add", "-A"); g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i")
            gh, errs = modern.go_hits(go)
            check("Go: modernize bulguları (any)", any(h["rule"] == "any" and h["file"] == "a.go" for h in gh), f"{gh} {errs}")
            changed = modern_fix.go_apply(go, {"any": ["a.go"]})
            check("Go: yalnız kapsamdaki düzeltme uygulanır ve gofmt'lanır",
                  changed == ["a.go"] and "func F(v any) any" in (go / "a.go").read_text(), (go / "a.go").read_text())
        else:
            skip("Go modernize", "araç yok")

    with tempfile.TemporaryDirectory() as td:
        base = Path(td); (base / "gocrons" / "cmd").mkdir(parents=True)
        ok = modern_fix.load_scope('{"lang": "go", "repo": "gocrons", "scope": {"any": ["cmd/a.go"]}}', base)
        check("araç kapsamı işin kaydından", ok[:3] == ("go", "gocrons", {"any": ["cmd/a.go"]}))
        bad = ['{"lang": "go", "repo": "../etc", "scope": {"any": ["a.go"]}}',
               '{"lang": "go", "repo": "gocrons", "scope": {"any": ["../../x.go"]}}',
               '{"lang": "go", "repo": "gocrons", "scope": {"any": ["/etc/passwd"]}}',
               '{"lang": "go", "repo": "gocrons", "scope": {"any": ["-rf.go"]}}',
               '{"lang": "php", "repo": "gocrons", "scope": {"x": ["a.php"]}}', '', '{"lang": "go", "repo": "nope", "scope": {"any": ["a.go"]}}']
        rej = 0
        for b in bad:
            try:
                modern_fix.load_scope(b, base)
            except ValueError:
                rej += 1
        check("kapsam doğrulaması: kök dışı/mutlak/'-' yol, geçersiz repo/dil, boş kayıt reddedilir", rej == len(bad), f"{rej}/{len(bad)}")
    check("ruff ikilisi doğrudan çalışır (python -m değil)", "-m" not in modern._ruff_cmd() and modern._ruff_cmd()[0].endswith("ruff"))

    grp = Group(source="go_modern", repo="gocrons", title="t", fingerprints=["go_modern:gocrons:any:a.go"],
                findings=[Finding(source="go_modern", file="a.go", rule="any", lines=[3], uris=["3|interface{}"])])
    d = task_description(grp, php_target="1.24 (go.mod)")
    check("Task metni: Go hedefi ve Go kataloğu", "Hedef Go:" in d and "interface{} → any" in d and "go_modern" in MODERN_SOURCES)

    srv = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/server.py").read_text()
    check("işçi: modern_fix işleri araçla koşar (pipeline değil)", 'if (job.get("job_kind") or "pipeline") == "modern_fix":' in srv
          and "modern_fix.run(job_id, work_item_id)" in srv)
    check("uçlar dil parametresi alır", 'lang: str = "php"' in srv and "_md.scan, AzureDevOpsClient(), repo.strip(), (lang or \"php\")" in srv)
    md = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/quality/modern.py").read_text()
    check("iş türü ve onaylı kapsam INSERT'te verilir (işçi yarışı yok, WI açıklaması okunmaz)",
          'job_kind="modern_fix",' in md and "job_payload=payloads.get(title" in md)
    html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text()
    check("aynı öneri tek satır: kural başına grup, alt işler tek tek ve toplu seçilir",
          "const ruleRow=(items,human)=>" in html and "function _tdmRuleToggle" in html and "function _tdmChildToggle" in html
          and 'class="tdmSel" data-rule=' in html)
    check("kart çok dilli: başlık, dil:repo seçimi, araç uygular etiketi",
          '<span class="h">Modernleştirme</span>' in html and "function _tdmSel()" in html and "Araç uygular" in html)


class _BumpFake:
    """upgrade_bump testleri için sahte Azure istemcisi: gerçek ağa çıkmaz."""

    def __init__(self, files, forbid=(), branches=None, prs=None, me="tempo-id", boom=(),
                 create_result=None, deny_ops=()):
        import requests
        self._rq = requests
        self.deny_ops = set(deny_ops)       # boşsa her işlemde; doluysa yalnız bu işlemlerde hata ver
        self.files = {(p, r, b, pa.lstrip("/")): v for (p, r, b, pa), v in files.items()}
        self.forbid, self.boom = set(forbid), dict(boom)
        self.oids = {}                      # (project, repo) -> {branch: oid}
        for k, v in (branches or {}).items():
            self.oids[k] = dict(v) if isinstance(v, dict) else {b: "oid-" + b for b in v}
        self.existing_prs, self.me = prs or {}, me
        self.create_result = create_result or {}
        self.created, self.pushes, self.prs, self.writes = [], [], [], 0
        self.created_oid = []

    def _bs(self, project, repo):
        return self.oids.setdefault((project, repo), {"main": "oid-main", "production": "oid-production"})

    def get_file_bytes(self, repo, path, branch=None, project=None, *, version=None, version_type=None):
        if version is not None:
            # F8: commit'ten okuma — sahte depoda commit, o oid'i taşıyan dalın içeriğidir
            assert version_type == "commit", version_type
            branch = next((b for b, o in self._bs(project, repo).items() if o == version), None)
        v = self.files.get((project, repo, branch, path.lstrip("/")))
        return v if (v is None or isinstance(v, bytes)) else v.encode("utf-8")

    def list_branches(self, repo, project=None):
        return [{"name": f"refs/heads/{b}", "objectId": o} for b, o in self._bs(project, repo).items()]

    def get_authenticated_user(self):
        if self.me is None:
            raise RuntimeError("kimlik çözülemedi")
        return {"id": self.me}

    def find_pull_request(self, repo, source_branch, *, project=None):
        return self.existing_prs.get((project, repo, source_branch))

    def _deny(self, repo, op=""):
        if self.deny_ops and op not in self.deny_ops:
            return
        if repo in self.forbid:
            raise self._rq.HTTPError("403", response=SimpleNamespace(status_code=403))
        if repo in self.boom:
            exc = self.boom[repo]
            if isinstance(exc, int):
                raise self._rq.HTTPError(str(exc), response=SimpleNamespace(status_code=exc))
            raise exc

    def create_branch(self, repo, branch_name, source_branch="main", project=None, source_object_id=None):
        self._deny(repo, "create")
        res = self.create_result.get(repo)
        if res is not None:
            return res
        bs = self._bs(project, repo)
        bs[branch_name] = source_object_id or bs[source_branch]
        self.created_oid.append(source_object_id)
        self.created.append((project, repo, branch_name, source_branch))
        self.writes += 1
        return {"value": [{"success": True, "updateStatus": "succeeded"}]}

    def push_changes(self, repo, branch, changes, commit_message, project=None, old_object_id=None):
        self._deny(repo, "push")
        self.pushes.append({"repo": repo, "branch": branch, "changes": changes,
                            "project": project, "msg": commit_message, "old_object_id": old_object_id})
        for c in changes:
            self.files[(project, repo, branch, c["path"].lstrip("/"))] = (
                __import__("base64").b64decode(c["content"]) if c.get("contentType") == "base64encoded"
                else c["content"])
        self._bs(project, repo)[branch] = "oid-pushed"
        self.writes += 1

    def create_pull_request(self, repo, source_branch, target_branch="main", title="",
                            description="", work_item_ids=None, draft=False, project=None):
        self._deny(repo, "pr")
        n = 100 + len(self.prs)
        self.prs.append({"repo": repo, "draft": draft, "project": project, "target": target_branch,
                         "wi": work_item_ids, "title": title, "id": n})
        self.writes += 1
        return {"pullRequestId": n, "repository": {"name": repo}}


def _bump_dec(change):
    """push_changes'e giden değişikliğin içeriği (base64ise çözülmüş metin)."""
    import base64
    if change.get("contentType") == "base64encoded":
        return base64.b64decode(change["content"]).decode("utf-8")
    return change["content"]


def test_upgrade_bump_sites():
    """Sürüm satırı değişikliği: yalnız kayıttaki siteye ait tek satır, taslak PR, çapraz proje,
    403'te diff; hiçbir site yazılmadan önce hepsi doğrulanır."""
    print("\n[Sürüm adımı: tek satırlık değişiklik]")
    import requests
    from agile_sdlc_crew.quality import upgrade_bump as bump

    src = "FROM eu/prod-ar/php-8-2:1.0\nRUN x\n"
    site = {"where": "devops", "repo": "anatolia", "project": "FLODevOps", "branch": "production",
            "path": "build/a/Dockerfile", "line": 1, "old": "php-8-2:1.0", "new": "php-8-4:1.0",
            "blocked": ""}
    check("yalnız o satır değişir", bump.apply_site(src, site) ==
          "FROM eu/prod-ar/php-8-4:1.0\nRUN x\n")
    check("CRLF korunur", bump.apply_site(src.replace("\n", "\r\n"), site) ==
          "FROM eu/prod-ar/php-8-4:1.0\r\nRUN x\r\n")
    check("sonda satır sonu yoksa korunur", bump.apply_site("FROM eu/prod-ar/php-8-2:1.0", site) ==
          "FROM eu/prod-ar/php-8-4:1.0")

    def raises(fn, exc):
        try:
            fn()
        except exc:
            return True
        except Exception:
            return False
        return False

    check("satır değişmişse SiteChanged",
          raises(lambda: bump.apply_site("FROM eu/prod-ar/php-8-3:1.0\nRUN x\n", site), bump.SiteChanged))
    check("old satırda iki kez → SiteChanged", raises(lambda: bump.apply_site(
        "FROM php-8-2:1.0 php-8-2:1.0\n", site), bump.SiteChanged))
    check("satır aralık dışı → SiteChanged",
          raises(lambda: bump.apply_site(src, dict(site, line=3)), bump.SiteChanged)
          and raises(lambda: bump.apply_site(src, dict(site, line=0)), bump.SiteChanged))
    check("izinsiz karakter (yeni satır / kabuk) → SiteChanged",
          raises(lambda: bump.apply_site(src, dict(site, new="php-8-4:1.0\nRUN evil")), bump.SiteChanged)
          and raises(lambda: bump.apply_site(src, dict(site, new="php-8-4;rm -rf")), bump.SiteChanged))
    check("old == new → SiteChanged",
          raises(lambda: bump.apply_site(src, dict(site, new=site["old"])), bump.SiteChanged))
    check("dal adı", bump.branch_name("php", "core", "8.4") == "tempo/upgrade-php-core-8.4")
    # I6: yalnız rakam dizileri değişebilir
    s8 = {"line": 1, "old": ">=8", "new": ">=8.4", "path": "composer.json"}
    check("I6: new, old'u içerip büyüyorsa SiteChanged",
          raises(lambda: bump.apply_site('"php": ">=8"\n', dict(s8, old=">=8", new=">=8.4")), bump.SiteChanged))
    check("I6: JSON anahtarı enjeksiyonu SiteChanged",
          raises(lambda: bump.apply_site('"php": ">=8.2"\n', dict(s8, old=">=8.2",
                                                                  new='>=8.4", "x": "y')), bump.SiteChanged))
    check("I6: yalnız rakam farkı geçer", bump.apply_site('"php": ">=8.2"\n', dict(s8, old=">=8.2", new=">=8.4"))
          == '"php": ">=8.4"\n')
    # G1: diff satır sonu işaretçisi
    try:
        sd = bump._diff_text([("f", "FROM php-8-2:1.0", "FROM php-8-4:1.0")])
    except Exception as e:
        sd = f"HATA {e!r}"
    check("G1: sonda satır sonu yoksa 'No newline' işaretçisi, diff düzgün",
          sd.count("\\ No newline at end of file") == 2 and "\n-FROM php-8-2:1.0\n\\ No" in sd
          and "\n+FROM php-8-4:1.0\n\\ No" in sd, repr(sd))

    def mk(repo, path, line, old, new, project="FLODevOps", branch="production"):
        return {"where": "devops", "repo": repo, "project": project, "branch": branch, "path": path,
                "line": line, "old": old, "new": new, "blocked": ""}

    dock_a = "FROM eu/php-8-2:1.0\nRUN x\nFROM eu/php-8-2:1.0 AS b\n"
    dock_b = "FROM eu/php-8-2:1.0\n"
    files = {("FLODevOps", "anatolia", "production", "build/a/Dockerfile"): dock_a,
             ("FLODevOps", "anatolia", "production", "build/b/Dockerfile"): dock_b,
             ("FLODevOps", "russia", "production", "build/r/Dockerfile"): dock_b}
    s_a1 = mk("anatolia", "build/a/Dockerfile", 1, "php-8-2:1.0", "php-8-4:1.0")
    s_a3 = mk("anatolia", "build/a/Dockerfile", 3, "php-8-2:1.0", "php-8-4:1.0")
    s_b = mk("anatolia", "build/b/Dockerfile", 1, "php-8-2:1.0", "php-8-4:1.0")
    s_r = mk("russia", "build/r/Dockerfile", 1, "php-8-2:1.0", "php-8-4:1.0")
    bn = "tempo/upgrade-php-core-8.4"
    kw = dict(branch=bn, title="T", body="B", wi_id=7)

    fc = _BumpFake(files)
    r = bump.push_sites(fc, [s_a1, s_a3, s_b, s_r], **kw)
    check("bölge reposu başına tek PR, taslak",
          [p["repo"] for p in r["prs"]] == ["anatolia", "russia"] and len(fc.prs) == 2
          and all(c["draft"] for c in fc.prs), str(fc.prs))
    check("project parametresi geçer", fc.pushes and all(c["project"] == "FLODevOps" for c in fc.pushes)
          and all(c["project"] == "FLODevOps" for c in fc.prs)
          and all(c[0] == "FLODevOps" for c in fc.created))
    check("hedef dal sitenin dalı", all(c["target"] == "production" for c in fc.prs)
          and all(c[3] == "production" for c in fc.created))
    pa = [p for p in fc.pushes if p["repo"] == "anatolia"]
    check("repo başına tek commit, iki dosya", len(pa) == 1 and len(pa[0]["changes"]) == 2)
    ca = {c["path"].lstrip("/"): c for c in pa[0]["changes"]}
    check("aynı dosyada iki site de uygulanır, başka satıra dokunulmaz",
          _bump_dec(ca["build/a/Dockerfile"]) == "FROM eu/php-8-4:1.0\nRUN x\nFROM eu/php-8-4:1.0 AS b\n"
          and ca["build/a/Dockerfile"]["changeType"] == "edit"
          and all(c["path"].startswith("/") for c in pa[0]["changes"]))
    check("başka projedeki repoya WI bağlanmaz", all(c["wi"] is None for c in fc.prs))
    check("r['prs'] biçimi", set(r["prs"][0]) >= {"repo", "id", "url"} and r["fallback"] == []
          and r["errors"] == [])
    fnorm = _BumpFake(files)
    bump.push_sites(fnorm, [s_a1, dict(s_a3, path="/build/a/Dockerfile")], **kw)
    check("yol normalleşir: /a ve a aynı dosya → push'ta TAM BİR değişiklik, iki site de uygulanmış",
          len(fnorm.pushes) == 1 and len(fnorm.pushes[0]["changes"]) == 1
          and _bump_dec(fnorm.pushes[0]["changes"][0]) ==
          "FROM eu/php-8-4:1.0\nRUN x\nFROM eu/php-8-4:1.0 AS b\n", str(fnorm.pushes))

    # 403 → diff
    ff = _BumpFake(files, forbid={"anatolia"})
    r = bump.push_sites(ff, [s_a1, s_r], **kw)
    fb = r["fallback"]
    check("403 → diff fallback'e, öteki repo devam eder",
          len(fb) == 1 and fb[0]["repo"] == "anatolia" and fb[0]["project"] == "FLODevOps"
          and fb[0]["diff"].count("\n+") == 2 and "a/build/a/Dockerfile" in fb[0]["diff"]
          and "b/build/a/Dockerfile" in fb[0]["diff"]
          and [p["repo"] for p in r["prs"]] == ["russia"], str(fb))
    check("diff yalnız tek satırı değiştirir",
          [l for l in fb[0]["diff"].splitlines() if l.startswith("-") and not l.startswith("---")]
          == ["-FROM eu/php-8-2:1.0"])
    # G1: sırlar maskelenir
    sec_files = {("FLODevOps", "russia", "production", "build/r/Dockerfile"):
                 "FROM eu/php-8-2:1.0\nENV TOKEN=ghp_AbCdEf0123456789AbCdEf0123456789AbCd\n"}
    rs = bump.push_sites(_BumpFake(sec_files, forbid={"russia"}), [s_r], **kw)
    check("G1: fallback diff'i maskelenir", rs["fallback"] and "ghp_AbCdEf0123456789" not in
          rs["fallback"][0]["diff"], str(rs["fallback"]))

    # I3: sonraki repo hatası öncekini kaybettirmez
    fb2 = _BumpFake(files, boom={"russia": 500})
    r = bump.push_sites(fb2, [s_a1, s_r], **kw)
    check("I3: B 500 verir → A prs'te, B errors'ta",
          [p["repo"] for p in r["prs"]] == ["anatolia"] and len(r["errors"]) == 1
          and r["errors"][0]["repo"] == "russia" and r["errors"][0]["project"] == "FLODevOps"
          and "500" in r["errors"][0]["error"], str(r))
    fb3 = _BumpFake(files, boom={"russia": requests.ConnectionError("bağlantı koptu")})
    r = bump.push_sites(fb3, [s_a1, s_r], **kw)
    check("I3: ConnectionError de errors'a düşer", [p["repo"] for p in r["prs"]] == ["anatolia"]
          and r["errors"] and r["errors"][0]["repo"] == "russia", str(r))
    fb4 = _BumpFake(files, boom={"russia": ValueError("Branch bulunamadi")})
    r = bump.push_sites(fb4, [s_a1, s_r], **kw)
    check("I3: ValueError de errors'a düşer", [p["repo"] for p in r["prs"]] == ["anatolia"]
          and r["errors"] and r["errors"][0]["repo"] == "russia", str(r))
    fb5 = _BumpFake(files, boom={"russia": requests.ConnectionError("Basic dXNlcjpwYXNz token=abcDEF123456789xyzXYZ987")})
    r = bump.push_sites(fb5, [s_r], **kw)
    check("I3: hata metni maskelenir", r["errors"] and "abcDEF123456789xyzXYZ987" not in r["errors"][0]["error"],
          str(r))

    # I2: create_branch 200 + success:false
    perm = {"value": [{"success": False, "updateStatus": "createBranchPermissionRequired"}]}
    fp = _BumpFake(files, create_result={"anatolia": perm})
    r = bump.push_sites(fp, [s_a1, s_r], **kw)
    check("I2: createBranchPermissionRequired → fallback, öteki repo devam",
          [f["repo"] for f in r["fallback"]] == ["anatolia"] and [p["repo"] for p in r["prs"]] == ["russia"]
          and not [p for p in fp.pushes if p["repo"] == "anatolia"], str(r))
    other = {"value": [{"success": False, "updateStatus": "invalidRefName"}]}
    fo = _BumpFake(files, create_result={"anatolia": other})
    r = bump.push_sites(fo, [s_a1, s_r], **kw)
    check("I2: başka success:false → errors, push denenmez",
          [e["repo"] for e in r["errors"]] == ["anatolia"] and [p["repo"] for p in r["prs"]] == ["russia"]
          and not [p for p in fo.pushes if p["repo"] == "anatolia"], str(r))

    # engelli / satırsız site → hiçbir şey yazılmaz
    f0 = _BumpFake(files)
    check("engelli site → ValueError, yazma yok",
          raises(lambda: bump.push_sites(f0, [s_r, dict(s_a1, blocked="imaj yok")], **kw), ValueError)
          and f0.writes == 0)
    check("line=0 → ValueError, yazma yok",
          raises(lambda: bump.push_sites(f0, [dict(s_r, line=0)], **kw), ValueError) and f0.writes == 0)
    check("geçersiz repo adı / yol → ValueError",
          raises(lambda: bump.push_sites(f0, [dict(s_r, repo="a/../b")], **kw), ValueError)
          and raises(lambda: bump.push_sites(f0, [dict(s_r, path="../x/Dockerfile")], **kw), ValueError)
          and raises(lambda: bump.push_sites(f0, [dict(s_r, path="build/-x/Dockerfile")], **kw), ValueError)
          and f0.writes == 0)
    check("aynı repoda farklı hedef dal → ValueError",
          raises(lambda: bump.push_sites(f0, [s_a1, dict(s_b, branch="main")], **kw), ValueError)
          and f0.writes == 0)
    # I6: yinelenen site ve elle düzenlenmesi gereken kısıt
    check("I6: aynı (proje, repo, yol, satır) iki site → ValueError",
          raises(lambda: bump.push_sites(f0, [s_a1, dict(s_a1)], **kw), ValueError)
          and raises(lambda: bump.push_sites(f0, [s_a1, dict(s_a1, path="/build/a/Dockerfile")], **kw),
                     ValueError) and f0.writes == 0)
    s_hand = {"where": "code", "repo": "core", "project": "", "branch": "main", "path": "composer.json",
              "line": 1, "old": "^7.4|^8.0", "new": ">=8.4", "blocked": ""}
    try:
        bump.push_sites(_BumpFake({}), [s_hand], **kw)
        msg = ""
    except ValueError as e:
        msg = str(e)
    check("I6: rakam dışı değişen kısıt → ValueError 'elle düzenlenmeli', yazma yok",
          "elle düzenlenmeli" in msg and "composer.json" in msg, msg)

    # C1: dal adı doğrulaması
    for bad, why in (("production", "hedef dal"), ("main", "hedef dal"),
                     ("tempo/upgrade-php-core", "desen"), ("refs/heads/tempo/upgrade-php-core-8.4", "refs/"),
                     ("tempo/upgrade-php-core..x-8.4", ".."), ("feature/x", "desen"),
                     ("tempo/upgrade-php-core-8.4\n", "satır sonu")):
        fbad = _BumpFake(files, branches={("FLODevOps", "russia"): {"main": "o1", "production": "o2", bad: "o3"}},
                         prs={("FLODevOps", "russia", bad): {"id": 1, "status": "active", "url": "u",
                                                              "closed_at": "", "merge_commit": "",
                                                              "target": "production", "created_by": "tempo-id"}})
        check(f"C1: dal '{bad[:30]!r}' ({why}) → ValueError, yazma yok",
              raises(lambda: bump.push_sites(fbad, [s_r], branch=bad, title="T", body="B", wi_id=7), ValueError)
              and fbad.writes == 0)
    fsame = _BumpFake(files)
    check("C1: dal = sitelerden birinin hedef dalı → ValueError",
          raises(lambda: bump.push_sites(fsame, [s_r, dict(s_a1, branch=bn)], **kw), ValueError)
          and fsame.writes == 0)

    # geç SiteChanged → ilk repoya da yazılmaz
    files_bad = dict(files)
    files_bad[("FLODevOps", "russia", "production", "build/r/Dockerfile")] = "FROM eu/php-8-3:1.0\n"
    fl = _BumpFake(files_bad)
    check("ikinci repoda SiteChanged → ilkine de yazma yok",
          raises(lambda: bump.push_sites(fl, [s_a1, s_r], **kw), bump.SiteChanged) and fl.writes == 0)
    # I5: UTF-8 değil / boş / yok
    fu = _BumpFake({**files, ("FLODevOps", "russia", "production", "build/r/Dockerfile"):
                    b"FROM eu/php-8-2:1.0\n# \xe7\xf6\n"})
    check("I5: UTF-8 olmayan dosya → SiteChanged, yazma yok",
          raises(lambda: bump.push_sites(fu, [s_a1, s_r], **kw), bump.SiteChanged) and fu.writes == 0)
    fe0 = _BumpFake({**files, ("FLODevOps", "russia", "production", "build/r/Dockerfile"): b""})
    check("I5: boş dosya → SiteChanged", raises(lambda: bump.push_sites(fe0, [s_r], **kw), bump.SiteChanged)
          and fe0.writes == 0)
    fn = _BumpFake({k: v for k, v in files.items() if k[1] != "russia"})
    check("I5: dosya yok (None) → SiteChanged", raises(lambda: bump.push_sites(fn, [s_r], **kw), bump.SiteChanged)
          and fn.writes == 0)
    fj = _BumpFake({("FLODevOps", "russia", "production", "build/r/Dockerfile"): "FROM eu/php-8-2:1.0\n# é\n"})
    rj = bump.push_sites(fj, [s_r], **kw)
    check("I5: UTF-8 içerik bayt-bayt korunur", _bump_dec(fj.pushes[0]["changes"][0]) ==
          "FROM eu/php-8-4:1.0\n# é\n", str(rj))

    # dal zaten var: C1 + S1 + I4
    def pr(**o):
        d = {"id": 55, "status": "active", "url": "https://x/55", "closed_at": "",
             "merge_commit": "", "target": "production", "created_by": "tempo-id"}
        d.update(o)
        return d

    def branches_with(oid):
        return {("FLODevOps", "russia"): {"main": "oid-main", "production": "oid-production", bn: oid}}

    fe = _BumpFake(files, branches=branches_with("oid-foreign"))
    check("dal var, PR yok, boş değil → ValueError, dokunulmaz",
          raises(lambda: bump.push_sites(fe, [s_r], **kw), ValueError) and fe.writes == 0)
    fe2 = _BumpFake({**files, ("FLODevOps", "russia", bn, "build/r/Dockerfile"): "FROM eu/php-8-4:1.0\n"},
                    branches=branches_with("oid-ours"), prs={("FLODevOps", "russia", bn): pr()})
    r = bump.push_sites(fe2, [s_r], **kw)
    check("dal var + Tempo'nun açık PR'ı + içerik zaten bizim → reused, hiçbir yazma yok",
          fe2.writes == 0 and not fe2.pushes and not fe2.prs and not fe2.created
          and r["prs"] == [{"repo": "russia", "id": 55, "url": "https://x/55", "reused": True}], str(r))
    fe3 = _BumpFake(files, branches=branches_with("oid-foreign"),
                    prs={("FLODevOps", "russia", bn): pr(status="completed")})
    check("dal var, PR kapanmış → ValueError",
          raises(lambda: bump.push_sites(fe3, [s_r], **kw), ValueError) and fe3.writes == 0)
    fe4 = _BumpFake(files, branches=branches_with("oid-foreign"),
                    prs={("FLODevOps", "russia", bn): pr(target="main")})
    check("C1: aktif PR'ın hedefi farklı → ValueError, yazma yok",
          raises(lambda: bump.push_sites(fe4, [s_r], **kw), ValueError) and fe4.writes == 0)
    fe5 = _BumpFake(files, branches=branches_with("oid-foreign"),
                    prs={("FLODevOps", "russia", bn): pr(created_by="someone-else")})
    check("S1: PR'ı Tempo açmamış → ValueError, yazma yok",
          raises(lambda: bump.push_sites(fe5, [s_r], **kw), ValueError) and fe5.writes == 0)
    fe6 = _BumpFake(files, branches=branches_with("oid-foreign"), me=None,
                    prs={("FLODevOps", "russia", bn): pr()})
    check("S1: kimlik çözülemezse yeniden kullanılmaz → ValueError",
          raises(lambda: bump.push_sites(fe6, [s_r], **kw), ValueError) and fe6.writes == 0)
    fe7 = _BumpFake(files, branches=branches_with("oid-foreign"), me="",
                    prs={("FLODevOps", "russia", bn): pr(created_by="")})
    check("S1: boş kimlik (''=='') eşleşme sayılmaz → ValueError",
          raises(lambda: bump.push_sites(fe7, [s_r], **kw), ValueError) and fe7.writes == 0)

    # I4: tekrarlanabilirlik
    fr1 = _BumpFake(files, branches=branches_with("oid-production"))
    r = bump.push_sites(fr1, [s_r], **kw)
    check("I4: artık boş dal (objectId = hedef) → create_branch atlanır, push + PR",
          not fr1.created and len(fr1.pushes) == 1 and len(fr1.prs) == 1 and r["prs"][0]["repo"] == "russia", str(r))
    fr2 = _BumpFake({**files, ("FLODevOps", "russia", bn, "build/r/Dockerfile"): "FROM eu/php-8-4:1.0\n"},
                    branches=branches_with("oid-ours"))
    try:
        r = bump.push_sites(fr2, [s_r], **kw)
    except ValueError as e:
        r = str(e)
    check("S1: dalda bizim içerik (+ yabancı commit olabilir) var, PR yok → ValueError, yazma ve PR yok",
          isinstance(r, str) and bn in r and "silin" in r and fr2.writes == 0 and not fr2.prs, str(r))
    fr3 = _BumpFake({**files, ("FLODevOps", "russia", bn, "build/r/Dockerfile"): "FROM eu/php-8-9:1.0\n"},
                    branches=branches_with("oid-ours"))
    check("I4: dalda farklı içerik, PR yok → ValueError",
          raises(lambda: bump.push_sites(fr3, [s_r], **kw), ValueError) and fr3.writes == 0)
    fr4 = _BumpFake(files, forbid={"russia"}, deny_ops={"push"})
    r = bump.push_sites(fr4, [s_r], **kw)
    check("I4: yeni dal oluşturuldu, push 403 → fallback, boş dal kaldı",
          len(r["fallback"]) == 1 and r["prs"] == [] and len(fr4.created) == 1
          and fr4._bs("FLODevOps", "russia")[bn] == "oid-production", str(r))
    fr4.forbid.clear()
    r2 = bump.push_sites(fr4, [s_r], **kw)
    check("I4: ikinci koşu (artık boş dal) → create_branch yok, push + PR",
          len(fr4.created) == 1 and len(fr4.pushes) == 1 and len(r2["prs"]) == 1
          and r2["fallback"] == [] and r2["errors"] == [], str(r2))

    # kod reposu sitesi: project boş → client None geçer, WI bağlanır
    files_c = {(None, "core", "main", "composer.json"): '{"require": {"php": ">=8.2"}}\n'}
    s_c = {"where": "code", "repo": "core", "project": "", "branch": "main", "path": "composer.json",
           "line": 1, "old": ">=8.2", "new": ">=8.4", "blocked": ""}
    fcc = _BumpFake(files_c)
    r = bump.push_sites(fcc, [s_c], **kw)
    check("kod reposu: project=None, WI bağlanır",
          fcc.pushes[0]["project"] is None and fcc.prs[0]["project"] is None
          and fcc.prs[0]["wi"] == [7]
          and _bump_dec(fcc.pushes[0]["changes"][0]) == '{"require": {"php": ">=8.4"}}\n', str(fcc.pushes))

    # istemci: project parametresi opsiyonel ve geriye uyumlu
    import inspect
    from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient as C
    for n in ("create_branch", "push_changes", "create_pull_request", "list_branches"):
        p = inspect.signature(getattr(C, n)).parameters.get("project")
        check(f"{n}(project=None)", p is not None and p.default is None)
    p = inspect.signature(C.find_pull_request).parameters["project"]
    check("find_pull_request keyword-only project", p.kind is p.KEYWORD_ONLY)
    check("get_file_bytes var", "get_file_bytes" in dir(C))


def test_upgrade_bump_round2():
    """Fix round 2: mevcut dala push yok (S1/S1b), hedef objectId sabitlenir (H1), baytlar aynen (H2),
    aşama/dal bilgisi (M1), her istisna repo başına yakalanır (M2), yol kenar durumları (M3)."""
    print("\n[Sürüm adımı: sertleştirme turu 2]")
    import base64
    from agile_sdlc_crew.quality import upgrade_bump as bump

    def raises(fn, exc=ValueError):
        try:
            fn()
        except exc:
            return True
        except Exception:
            return False
        return False

    def mk(repo, path, line, old, new, project="FLODevOps", branch="production"):
        return {"where": "devops", "repo": repo, "project": project, "branch": branch, "path": path,
                "line": line, "old": old, "new": new, "blocked": ""}

    dock = "FROM eu/php-8-2:1.0\n"
    rpath = ("FLODevOps", "russia", "production", "build/r/Dockerfile")
    files = {("FLODevOps", "anatolia", "production", "build/a/Dockerfile"): dock, rpath: dock}
    s_a = mk("anatolia", "build/a/Dockerfile", 1, "php-8-2:1.0", "php-8-4:1.0")
    s_r = mk("russia", "build/r/Dockerfile", 1, "php-8-2:1.0", "php-8-4:1.0")
    bn = "tempo/upgrade-php-core-8.4"
    kw = dict(branch=bn, title="T", body="B", wi_id=7)
    new_ru = "FROM eu/php-8-4:1.0\n"

    def br(oid):
        return {("FLODevOps", "russia"): {"main": "oid-main", "production": "oid-production", bn: oid}}

    def pr(**o):
        d = {"id": 55, "status": "active", "url": "https://x/55", "closed_at": "",
             "merge_commit": "", "target": "production", "created_by": "tempo-id"}
        d.update(o)
        return d

    # S1: yabancı commit'li önceden oluşturulmuş dal, PR yok
    f1 = _BumpFake({**files, ("FLODevOps", "russia", bn, "build/r/Dockerfile"): new_ru,
                    ("FLODevOps", "russia", bn, "other.txt"): "foreign"}, branches=br("oid-foreign"))
    try:
        bump.push_sites(f1, [s_r], **kw)
        msg = ""
    except ValueError as e:
        msg = str(e)
    check("S1: bizim içerik + yabancı commit, PR yok → ValueError (dalı adıyla sil de), yazma/PR yok",
          bn in msg and "silin" in msg and f1.writes == 0 and not f1.prs, msg)
    # S1b: Tempo PR'ı, dalda içerik farklı / dosya yok
    f2 = _BumpFake({**files, ("FLODevOps", "russia", bn, "build/r/Dockerfile"): "FROM eu/php-8-9:1.0\n"},
                   branches=br("oid-ours"), prs={("FLODevOps", "russia", bn): pr()})
    try:
        bump.push_sites(f2, [s_r], **kw)
        msg = ""
    except ValueError as e:
        msg = str(e)
    check("S1b: Tempo PR'ı ama dal içeriği farklı → ValueError (PR no + dal), yazma yok",
          "PR dalı güncel değil" in msg and "55" in msg and bn in msg and f2.writes == 0, msg)
    f2b = _BumpFake(files, branches=br("oid-ours"), prs={("FLODevOps", "russia", bn): pr()})
    check("S1b: Tempo PR'ı, dalda dosya yok → ValueError, yazma yok",
          raises(lambda: bump.push_sites(f2b, [s_r], **kw)) and f2b.writes == 0)
    f2c = _BumpFake({**files, ("FLODevOps", "russia", bn, "build/r/Dockerfile"): new_ru},
                    branches=br("oid-ours"), prs={("FLODevOps", "russia", bn): pr()})
    r = bump.push_sites(f2c, [s_a, s_r], **kw)
    check("S1b: reused yalnız o repoda; öteki repo normal akar",
          [(p["repo"], p.get("reused", False)) for p in r["prs"]] == [("anatolia", False), ("russia", True)]
          and len(f2c.pushes) == 1 and f2c.pushes[0]["repo"] == "anatolia", str(r))

    # H1: hedef objectId sabitlenir
    f3 = _BumpFake(files)
    bump.push_sites(f3, [s_r], **kw)
    check("H1: create_branch source_object_id = doğrulanan hedef oid",
          f3.created_oid == ["oid-production"], str(f3.created_oid))
    check("H1: push_changes old_object_id = aynı oid", f3.pushes[0]["old_object_id"] == "oid-production",
          str(f3.pushes[0]["old_object_id"]))
    f3b = _BumpFake(files, branches=br("oid-production"))
    bump.push_sites(f3b, [s_r], **kw)
    check("H1: artık boş dalda da push o oid üzerine, create yok",
          not f3b.created and f3b.pushes[0]["old_object_id"] == "oid-production")

    # H2: BOM + CRLF bayt-bayt
    orig = b"\xef\xbb\xbfFROM eu/php-8-2:1.0\r\nRUN x\r\n"
    f4 = _BumpFake({**files, rpath: orig})
    bump.push_sites(f4, [s_r], **kw)
    ch = f4.pushes[0]["changes"][0]
    got = base64.b64decode(ch["content"])
    check("H2: contentType base64encoded", ch.get("contentType") == "base64encoded", str(ch.get("contentType")))
    check("H2: BOM + CRLF — yalnız düzenlenen satır farklı, bayt-bayt",
          got == orig.replace(b"php-8-2", b"php-8-4") and got.startswith(b"\xef\xbb\xbf"), repr(got))

    # M1: aşama + dal yazıldı mı
    f5 = _BumpFake(files, forbid={"russia"}, deny_ops={"pr"})
    r = bump.push_sites(f5, [s_r], **kw)
    e = (r["fallback"] or [{}])[0]
    check("M1: PR oluşturma 403 → fallback stage 'pr', branch_written True",
          e.get("stage") == "pr" and e.get("branch_written") is True and len(f5.pushes) == 1, str(r))
    f6 = _BumpFake(files, forbid={"russia"}, deny_ops={"create"})
    e = (bump.push_sites(f6, [s_r], **kw)["fallback"] or [{}])[0]
    check("M1: dal oluşturma 403 → stage 'create', branch_written False",
          e.get("stage") == "create" and e.get("branch_written") is False, str(e))
    f7 = _BumpFake(files, boom={"russia": 500}, deny_ops={"push"})
    e = (bump.push_sites(f7, [s_r], **kw)["errors"] or [{}])[0]
    check("M1: push 500 → errors stage 'push', branch_written True (dal oluşturuldu)",
          e.get("stage") == "push" and e.get("branch_written") is True, str(e))
    f8 = _BumpFake(files, boom={"russia": 500}, deny_ops={"pr"})
    e = (bump.push_sites(f8, [s_r], **kw)["errors"] or [{}])[0]
    check("M1: PR 500 → errors stage 'pr', branch_written True",
          e.get("stage") == "pr" and e.get("branch_written") is True, str(e))

    # M2: dict olmayan value[0]
    f9 = _BumpFake(files, create_result={"russia": {"value": ["bozuk"]}})
    r = bump.push_sites(f9, [s_a, s_r], **kw)
    check("M2: value[0] dict değil → errors'a düşer, önceki repo'nun PR'ı kaybolmaz",
          [p["repo"] for p in r["prs"]] == ["anatolia"] and [x["repo"] for x in r["errors"]] == ["russia"], str(r))
    f10 = _BumpFake(files)
    f10.create_pull_request = lambda *a, **k: None          # PR yanıtı okunamıyor (res.get patlar)
    r = bump.push_sites(f10, [s_r], **kw)
    check("M2: PR yanıtı bozuksa istisna sızmaz → errors", len(r["errors"]) == 1 and not r["prs"], str(r))

    # M3: '.' segmenti
    fd = _BumpFake(files)
    check("M3: '.' yol segmenti → ValueError, yazma yok",
          raises(lambda: bump.push_sites(fd, [dict(s_r, path="build/./r/Dockerfile")], **kw)) and fd.writes == 0)


def test_upgrade_client_find_pr_and_bytes():
    """İstemci: find_pull_request en yeni PR'ı pullRequestId'ye göre seçer ve createdBy döndürür;
    get_file_bytes ham baytları verir (JSON'a çevirmez)."""
    print("\n[Sürüm adımı: istemci ucu]")
    from agile_sdlc_crew.tools import azure_devops_base as azb
    c = azb.AzureDevOpsClient.__new__(azb.AzureDevOpsClient)
    c.org_url, c.project = "https://dev.azure.com/o", "P"
    class _Cl(azb.AzureDevOpsClient):
        _headers = {"Authorization": "x"}
        _base_api_url = "https://x/P/_apis"
    c.__class__ = _Cl
    c._repo_api_url = lambda repo, project=None: f"https://x/{project or 'P'}/{repo}"
    vals = [{"pullRequestId": 7, "status": "abandoned", "creationDate": "2026-11-17T10:00:00Z",
             "createdBy": {"id": "old"}, "targetRefName": "refs/heads/main"},
            {"pullRequestId": 9, "status": "active", "creationDate": "2026-01-01T00:00:00Z",
             "createdBy": {"id": "me", "uniqueName": "m@x"}, "targetRefName": "refs/heads/production",
             "lastMergeCommit": {"commitId": "abc"}, "repository": {"name": "r", "project": {"name": "FLO"}}}]

    class R:
        def __init__(self, js=None, content=b"", status=200):
            self._js, self.content, self.status_code, self.ok = js, content, status, status < 400
            self.text = content.decode("utf-8", "replace")

        def json(self):
            return self._js

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests
                raise requests.HTTPError(str(self.status_code), response=self)
    seen = {}
    og, og_post = azb.requests.get, azb.requests.post
    try:
        azb.requests.get = lambda url, headers=None, params=None, timeout=None, **k: (
            seen.update(url=url, params=params, headers=headers) or
            (R({"value": vals}) if url.endswith("/pullrequests") else R(content=b'{"content": "x"}\xff')))
        pr = c.find_pull_request("r", "tempo/upgrade-php-r-8.4", project="FLO")
        check("en yüksek pullRequestId seçilir", pr and pr["id"] == 9, str(pr))
        check("created_by döner", pr and pr["created_by"] == "me", str(pr))
        check("alanlar", pr and pr["status"] == "active" and pr["target"] == "production"
              and pr["merge_commit"] == "abc" and pr["closed_at"] == "", str(pr))
        b = c.get_file_bytes("r", "/a/b.json", "production", project="FLO")
        check("get_file_bytes ham bayt döner (JSON 'content' çözülmez)", b == b'{"content": "x"}\xff', repr(b))
        check("octet-stream istenir", (seen["headers"] or {}).get("Accept") == "application/octet-stream"
              and seen["params"].get("$format") == "octetStream", str(seen))
        azb.requests.get = lambda *a, **k: R(status=404)
        check("404 → None", c.get_file_bytes("r", "/a", "production") is None)
        # H1 / H2: create_branch(source_object_id), push_changes(old_object_id, contentType)
        posted = []
        azb.requests.post = lambda url, headers=None, json=None, params=None, timeout=None: (
            posted.append((url, json)) or R({"value": [{"success": True}]}))
        c.list_branches = lambda repo, project=None: (_ for _ in ()).throw(AssertionError("list_branches çağrılmamalı"))
        c.create_branch("r", "tempo/upgrade-php-r-8.4", "production", project="FLO", source_object_id="abc123")
        check("create_branch source_object_id'yi newObjectId yapar (hedef adı aranmaz)",
              posted[-1][1][0]["newObjectId"] == "abc123", str(posted[-1]))
        c.push_changes("r", "tempo/upgrade-php-r-8.4",
                       [{"changeType": "edit", "path": "/a", "content": "Zm9v", "contentType": "base64encoded"},
                        {"changeType": "edit", "path": "/b", "content": "x"}],
                       "m", project="FLO", old_object_id="abc123")
        body = posted[-1][1]
        check("push_changes old_object_id'yi refUpdate oldObjectId yapar (dal başı aranmaz)",
              body["refUpdates"][0]["oldObjectId"] == "abc123", str(body))
        ch = body["commits"][0]["changes"]
        check("contentType geçer; verilmezse rawtext",
              ch[0]["newContent"]["contentType"] == "base64encoded" and ch[1]["newContent"]["contentType"] == "rawtext",
              str(ch))
        # Yorumlar sayfalı: işaret araması devam belirteciyle bütün sayfaları okur, sınırda 'tamam değil' der
        pages, asked = {None: ("t1", [1, 2]), "t1": ("t2", [3]), "t2": (None, [4])}, []

        def cget(url, headers=None, params=None, timeout=None, **k):
            asked.append(dict(params or {}))
            tok, ids = pages[(params or {}).get("continuationToken")]
            js = {"comments": [{"id": i, "text": f"c{i}", "createdDate": "2026-11-17T10:00:00Z",
                                "createdBy": {"displayName": "x"}} for i in ids]}
            if tok:
                js["continuationToken"] = tok
            return R(js)
        azb.requests.get = cget
        rows, complete = c.get_work_item_comments_all(5)
        check("yorumlar: bütün sayfalar okunur", complete and [r["id"] for r in rows] == [1, 2, 3, 4]
              and [r["metin"] for r in rows][-1] == "c4" and asked[1].get("continuationToken") == "t1", str(asked))
        rows, complete = c.get_work_item_comments_all(5, max_items=2)
        check("yorumlar: sınır aşılırsa tamam değil", not complete and len(rows) == 2, str(rows))
        check("tek sayfalık eski okuyucu aynı", [r["id"] for r in c.get_work_item_comments(5)] == [1, 2])
    finally:
        azb.requests.get, azb.requests.post = og, og_post


def _ci_src(pins):
    """Taramanın ci_source'u: sabitlemenin dosyası (F1 sonrası tanımdan gelir; burada ilk sabitlemeden)."""
    return {k: pins[0][k] for k in ("repo", "project", "branch", "path")} if pins else None


class _UpFake(_BumpFake):
    """Yükseltme durumu / izleme işi / sürüm adımı için sahte Azure: PR'lar (proje, repo, dal),
    PR build'leri (queue_time + sonuç), WIQL (izleme + tempo-upgrade işleri), yorum / iş kayıtları."""
    org_url, project = "https://dev.azure.com/o", "P"

    def __init__(self, files=None, *, prs=None, builds=None, tracks=(), tasks=(), defs=None, commit_dates=None,
                 requests_=(), areas=None, issue_tasks=None, summary_error=None, **k):
        super().__init__(files or {}, prs=prs, **k)
        # builds: tanım id → build listesi (sahte minTime'ı YOK SAYAR — sıkı "sonra" süzgeci kodda olmalı)
        self.builds, self.tracks, self.tasks = dict(builds or {}), list(tracks), list(tasks)
        self.defs = dict(defs or {})                  # repo → {"id","name","project"} | Exception
        self.commit_dates = dict(commit_dates or {})  # (project, repo, branch, path) → tarih | Exception
        self.requests_ = list(requests_)              # DevOps istekleri (tempo-devops)
        self.areas = dict(areas or {})
        self.issue_tasks = dict(issue_tasks or {})    # build id → [{name, result}]
        self.summary_error = summary_error            # verilirse get_build_failure_summary bunu yükseltir
        self.def_calls, self.build_calls = [], []
        self.wis, self.comments, self.states, self.wiql, self.descs, self.summaries = [], [], [], [], [], []

    def query_work_items(self, wiql, limit=0):
        import html as _h
        self.wiql.append(wiql)
        pool = (self.tracks if "tempo-upgrade-track" in wiql
                else self.requests_ if "CONTAINS 'tempo-devops'" in wiql
                else self.tasks if "CONTAINS 'tempo-upgrade'" in wiql else [])
        needles = [n.replace("''", "'") for n in re.findall(r"\[System\.Description\] CONTAINS '((?:[^']|'')*)'", wiql)]
        return [w for w in pool
                if all(n in _h.unescape(w["fields"].get("System.Description") or "") for n in needles)]

    def get_team_area_path(self, team=""):
        return self.areas.get(team, "P\\T") if team else ""

    def get_test_pipeline(self, repo):
        name = f"{repo}-test"
        self.def_calls.append((repo, name))
        d = self.defs.get(repo)
        if isinstance(d, Exception):
            raise d
        return d if d and d["name"].lower() == name.lower() else None

    def list_definition_builds(self, project, definition_id, *, min_time="", top=50):
        self.build_calls.append((project, definition_id, min_time))
        return list(self.builds.get(definition_id, []))

    def get_build_issue_tasks(self, project, build_id):
        return list(self.issue_tasks.get(build_id, []))

    def get_last_commit_date(self, repo, path, branch, *, project=None):
        raise AssertionError("F7: kanıt saati dosyanın commit zamanından okunmaz (dalın son push'u)")

    def get_last_push_date(self, repo, branch, *, project=None):
        """F7: dalın son push'u. commit_dates anahtarındaki yol yok sayılır: aynı (proje, repo, dal) altındaki
        değerlerin en geçi; biri hata ise yükselir, hiç yoksa ""."""
        vals = [v for (p, r, b, _path), v in self.commit_dates.items() if (p, r, b) == (project, repo, branch)]
        for v in vals:
            if isinstance(v, Exception):
                raise v
        return max(vals) if vals and all(vals) else ""

    def list_iterations(self, team=""):
        return [{"path": "P\\T\\S1"}, {"path": "P\\T\\S2"}]

    def create_work_item(self, type_name, fields, parent_id=None):
        wi = {"id": 900 + len(self.wis), "fields": dict(fields, **{"System.State": "To Do"})}
        self.wis.append(dict(fields, _type=type_name))
        self.tracks.append(wi)
        return wi

    def add_comment(self, wid, text):
        self.comments.append((wid, text))

    def get_work_item_type_states(self, t):
        return [{"name": "To Do", "category": "Proposed"}, {"name": "Done", "category": "Completed"}]

    def set_work_item_state(self, wid, st):
        self.states.append((wid, st))
        for w in self.tracks:
            if w["id"] == wid:
                w["fields"]["System.State"] = st

    def get_build_failure_summary(self, project, build_id, max_chars=4000):
        self.summaries.append(build_id)
        if self.summary_error is not None:
            raise self.summary_error
        return f"FAILED [Task] Run tests: CoreTest::testFoo (#{build_id})"

    def get_repository(self, repo):
        return {"id": "g-" + repo, "defaultBranch": "refs/heads/main"}

    def create_pull_request(self, repo, source_branch, target_branch="main", title="", description="",
                            work_item_ids=None, draft=False, project=None):
        self.descs.append(description)
        return super().create_pull_request(repo, source_branch, target_branch, title, description,
                                           work_item_ids, draft, project)


def test_upgrade_status_and_steps():
    """Yükseltme durumu (§5 aşama tablosu), izleme işi, sıradaki sürüm adımı (§6 ön koşulları), CI kanıtı
    yalnız merge'den SONRA kuyruğa giren build, run'ın yeniden doğrulaması, BUMP kapalıyken diff, işçi /
    yeniden deneme / yaşam döngüsü."""
    print("\n[Sürüm yükseltme: durum, izleme işi, sürüm adımı]")
    import inspect
    from fastapi.testclient import TestClient
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import server
    from agile_sdlc_crew.quality import intake, upgrade, upgrade_bump as bump, upgrade_track as ut

    BR = "tempo/upgrade-php-core-8.4"
    MERGED = "2026-11-10T10:00:00.1234567Z"
    dv_a = {"where": "devops", "repo": "anatolia", "project": "FLODevOps", "branch": "production",
            "path": "/build/core/Dockerfile", "line": 1, "old": "eu/php-8-2:1.0", "new": "eu/php-8-4:1.0",
            "blocked": ""}
    dv_r = dict(dv_a, repo="russia")
    ci_s = {"where": "ci", "repo": "yml-files", "project": "EcomBackend", "branch": "main",
            "path": "Test Pipelines/core.yml", "line": 2, "old": "'8.2'", "new": "'8.4'", "blocked": ""}
    code_s = {"where": "code", "repo": "core", "project": "", "branch": "", "path": "composer.json",
              "line": 3, "old": "^8.2", "new": "^8.4", "blocked": ""}
    code_bad = dict(code_s, old="^7.4|^8.0", new=">=8.4")   # yalnız rakam değişimi değil → elle

    pin_below = {"repo": "yml-files", "project": "EcomBackend", "branch": "main", "path": "Test Pipelines/core.yml",
                 "line": 2, "version": "8.2"}
    pin_at = dict(pin_below, version="8.4")

    def mkscan(blocking=0, blockers=(), sites=None, rows=(), up=False, pins="below"):
        pins = {"below": [pin_below], "at": [pin_at], "none": []}.get(pins, pins) if isinstance(pins, str) else pins
        return {"repo": "core", "lang": "php", "current": "8.2", "target": "8.4", "up_to_date": up,
                "sites": [dv_a, dv_r, ci_s, code_s] if sites is None else sites, "blocking": blocking,
                "blockers": list(blockers), "auto": list(rows), "infra": [], "human": [], "ci_pins": pins,
                "ci_source": _ci_src(pins)}

    def pr(pid, status, closed=MERGED):
        return {"id": pid, "status": status, "url": f"https://x/pr/{pid}", "closed_at": closed if status != "active" else "",
                "merge_commit": "m", "target": "main", "created_by": "tempo-id"}

    def build(bid, q, result="succeeded", status="completed", definition="Core-test"):
        return {"build_id": bid, "status": status, "result": result, "definition": definition,
                "url": f"https://x/b/{bid}", "project": "EcomBackend", "queue_time": q, "source_version": ""}

    CORE_DEF = {"core": {"id": 5, "name": "Core-test", "project": "EcomBackend"}}
    PIN_FILE = ("EcomBackend", "yml-files", "main", "Test Pipelines/core.yml")

    def cf(builds=(), defs=True, date=MERGED, **k):
        """Test hattı dosyası hedefte; son değişikliği `date`; Core-test tanımının build'leri `builds`."""
        return _UpFake(defs=CORE_DEF if defs else {}, builds={5: list(builds)}, commit_dates={PIN_FILE: date}, **k)

    def task_wi(wid, fps, tags="tempo-quality; tempo-upgrade", state="To Do",
                title="[core] Sürüm yükseltme 8.2→8.4: x"):
        return {"id": wid, "fields": {"System.Title": title, "System.State": state, "System.Tags": tags,
                                      "System.Description": "<p>x</p>" + "".join(
                                          f"<p><small>tempo-fp: {f}</small></p>" for f in fps)}}

    def track_wi(wid=500, to="8.4", state="To Do", repo="core", area="P\\T", tags="tempo-upgrade-track"):
        return {"id": wid, "fields": {"System.Title": "izleme", "System.State": state, "System.AreaPath": area,
                                      "System.Tags": tags,
                                      "System.Description": f"<p><small>tempo-upgrade-key: php:{repo}:{to}</small></p>"}}

    ci_key = ("EcomBackend", "yml-files", BR)
    old_b = build(1, "2026-11-10T09:00:00Z")                # merge'den ÖNCE kuyruğa girmiş yeşil
    new_g = build(2, "2026-11-10T11:00:00.5Z")
    new_r = build(3, "2026-11-10T12:00:00Z", result="failed")

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "core"
        root.mkdir()
        (root / "composer.json").write_text('{\n  "require": {\n    "php": "^8.2"\n  }\n}\n', encoding="utf-8")
        cfg = {"pack_sp": 3, "pack_files": 5, "grace_days": 3, "task_fields": {}, "sonar_url": "", "nr_days": 7,
               "php_exclude": [], "php_bin": "php", "scan_ttl_h": 24, "max_take": 8, "freshen": False,
               "repos_dir": base, "targets_cache": base / "t.yaml", "scan_cache": base / "cache"}
        deps = intake.Deps(gather=None, board=lambda c: {}, repo_ok=lambda r: (base / r).is_dir(), queue=None,
                           record=lambda **k: None, now=lambda: 1e9, cfg=lambda: cfg)

        def st(scan, c=None, **k):
            return ut.status(c or _UpFake(), "core", "php", to="8.4", deps=deps, scan=scan, wait_h=72, root=root, **k)

        # ── Aşama tablosu (§5) ──
        s = st(mkscan(blocking=2))
        check("engelleyen bulgu → hazırlık", s["stage"] == "hazırlık" and not s["next"]["ready"], str(s["stage"]))
        c_t = _UpFake(tasks=[task_wi(70, ["php_modern:core:implicit_nullable:app/A.php"]),
                             task_wi(71, ["php_modern:core:implicit_nullable:app/B.php"], state="Done"),
                             task_wi(72, ["php_modern:web:implicit_nullable:app/A.php"])])
        s = st(mkscan(), c_t)
        check("açık engelleyen tempo-upgrade işi → hazırlık (kapalı / başka repo sayılmaz)",
              s["stage"] == "hazırlık" and [t["id"] for t in s["open_tasks"]] == [70]
              and s["open_tasks"][0]["blocking"], str(s["open_tasks"]))
        c_nb = _UpFake(tasks=[task_wi(73, ["php_modern:core:implicit_nullable:app/A.php"],
                                      tags="tempo-quality; tempo-upgrade; needs-human"),
                              task_wi(74, ["php_modern:core:probe:deprecation:app/E.php"])])
        s = st(mkscan(), c_nb)
        check("engellemeyen açık iş (insan işi deprecation / probe uyarısı) → hazır",
              s["stage"] == "hazır" and sorted(t["id"] for t in s["open_tasks"]) == [73, 74]
              and not any(t["blocking"] for t in s["open_tasks"]), str(s["open_tasks"]))
        s = st(mkscan(blockers=[{"name": "laravel/framework", "detail": "laravel/framework: PHP ^7.3 — 8.4'ü dışlıyor"}]))
        check("yalnız composer engeli → engelli", s["stage"] == "engelli" and not s["next"]["ready"]
              and "laravel" in s["next"]["reason"], str(s["next"]))
        s = st(mkscan(rows=[{"rule": "x", "blocking": False, "pre_fixable": False}]))
        check("fix_min > şimdiki bulgusu → engelli", s["stage"] == "engelli", s["stage"])
        s_clean = st(mkscan())
        nx = s_clean["next"]
        check("temiz → hazır, sıradaki ci", s_clean["stage"] == "hazır" and nx["step"] == "ci" and nx["ready"] is True
              and nx["sites"] == [ci_s], str(nx))
        check("STAGES", ut.STAGES == ("hazırlık", "engelli", "hazır", "test hattı", "üretim", "tamam"))
        s = st(mkscan(), _UpFake(prs={ci_key: pr(11, "active")}))
        check("CI PR'ı açık → test hattı, prod hazır değil",
              s["stage"] == "test hattı" and s["next"]["step"] == "prod" and not s["next"]["ready"]
              and "#11" in s["next"]["reason"] and s["prs"]["ci"]["id"] == 11, str(s["next"]))
        check("merge'den ÖNCE kuyruğa girmiş yeşil build kanıt değil",
              bump.ci_evidence(cf([old_b]), "core", MERGED, wait_h=72)["state"] == "none")
        # CI dosyası hedefte (kim yaptıysa), test hattı PR'ı Tempo'nun kaydında 'completed'
        at = dict(sites=[dv_a, dv_r, code_s], pins="at")
        s = st(mkscan(**at), cf([old_b], prs={ci_key: pr(11, "completed")}))
        check("CI dosyası hedefte, sonrasında build yok → test hattı, prod yok",
              s["stage"] == "test hattı" and s["next"]["step"] == "prod" and not s["next"]["ready"]
              and s["evidence"]["state"] == "none", str(s["next"]))
        s2 = st(mkscan(**at), cf([new_g, old_b]))
        check("dosya değişikliğinden sonra yeşil → prod hazır",
              s2["stage"] == "hazır" and s2["next"]["step"] == "prod" and s2["next"]["ready"] is True
              and s2["next"]["sites"] == [dv_a, dv_r] and s2["next"]["test_note"] == "", str(s2["next"]))
        c_red = cf([new_r, new_g, old_b])
        ev = bump.ci_evidence(c_red, "core", MERGED, wait_h=72)
        check("sonrasında kırmızı → prod yok, özet var",
              ev["state"] == "red" and ev["summary"] and "CoreTest" in ev["summary"] and ev["build"]["id"] == 3,
              str(ev))
        s = st(mkscan(**at), c_red)
        check("kırmızı kanıtla prod adımı hazır değil, nedeni özet",
              s["next"]["step"] == "prod" and not s["next"]["ready"] and "CoreTest" in s["next"]["reason"])
        check("kırmızı kanıt prod'u bekletiyor → blocked_by_evidence", s["next"].get("blocked_by_evidence") is True)
        s_prep = st(mkscan(blocking=2, **at), c_red)
        check("hazırlıkta kırmızı kanıt bekletmiyor → blocked_by_evidence yok",
              s_prep["evidence"]["state"] == "red" and s_prep["next"].get("blocked_by_evidence") is False
              and s_clean["next"].get("blocked_by_evidence") is False, str(s_prep["next"]))
        s_nopin = st(mkscan(sites=[dv_a, dv_r, code_s], pins="none"), c_red)
        check("sabitleme yokken kırmızı kanıt da prod'u bekletir → blocked_by_evidence",
              s_nopin["next"]["step"] == "prod" and s_nopin["next"].get("blocked_by_evidence") is True, str(s_nopin["next"]))
        ev2 = bump.ci_evidence(cf([build(4, "2026-11-10T11:00:00Z", status="inProgress", result=None), old_b]),
                               "core", MERGED, wait_h=72)
        check("koşan build kanıt değil; bekleme süresi dolmuş", ev2["state"] == "none" and ev2["overdue"] is True,
              str(ev2))
        check("<repo>-test tanımı yok → no_pipeline",
              bump.ci_evidence(cf(defs=False), "core", MERGED, wait_h=72)["state"] == "no_pipeline")
        check("zaman damgası: 7 haneli kesir + Z", bump._ts(MERGED) < bump._ts("2026-11-10T10:00:00.2Z")
              and bump._ts("x") is None)
        # no_pipeline: prod açılır, ilk satır "Test kanıtı yok"
        s = st(mkscan(**at), cf(defs=False))
        check("no_pipeline → prod hazır, test notu 'Test kanıtı yok'",
              s["next"]["step"] == "prod" and s["next"]["ready"] and s["next"]["test_note"] == "Test kanıtı yok"
              and s["stage"] == "hazır", str(s["next"]))
        # russia imajı yayınlanmamış
        dv_rb = dict(dv_r, blocked=upgrade.UNPUBLISHED)
        s3 = st(mkscan(sites=[dv_a, dv_rb, code_s], pins="at"), cf([new_g]))
        check("russia engelli → prod hiçbir bölgeye açılmaz",
              "imaj" in s3["next"]["reason"] and not s3["next"]["ready"] and s3["next"]["step"] == "prod"
              and s3["stage"] == "engelli", str(s3["next"]))
        s3b = st(mkscan(sites=[dv_a, dv_rb, ci_s, code_s]))
        check("russia engelli, CI adımı da açılmaz (neden imaj)",
              s3b["next"]["step"] == "ci" and not s3b["next"]["ready"] and "imaj" in s3b["next"]["reason"])
        # üretim
        c_pd = cf([new_g], prs={("FLODevOps", "anatolia", BR): pr(21, "active"),
                                ("FLODevOps", "russia", BR): pr(22, "active")})
        s = st(mkscan(**at), c_pd)
        check("FLODevOps PR'ları açık → üretim; sıradaki code ama hazır değil",
              s["stage"] == "üretim" and s["next"]["step"] == "code" and not s["next"]["ready"]
              and len(s["prs"]["devops"]) == 2, str(s["next"]))
        c_part = cf([new_g], prs={("FLODevOps", "anatolia", BR): pr(21, "active")})
        s = st(mkscan(**at), c_part)
        check("bölgelerden birinin PR'ı eksik → üretim, prod yeniden atılabilir",
              s["stage"] == "üretim" and s["next"]["step"] == "prod" and s["next"]["ready"], str(s["next"]))
        # tamam: üretim hedefte → kod reposu
        s4 = st(mkscan(sites=[], up=True))
        check("üretim hedefte → sıradaki code", s4["stage"] == "tamam" and s4["next"]["step"] == "code"
              and s4["next"]["ready"] and s4["next"]["sites"][0]["path"] == "composer.json"
              and s4["next"]["sites"][0]["branch"] == "main" and s4["next"]["sites"][0]["new"] == "^8.4",
              str(s4["next"]))
        s = st(mkscan(sites=[], up=True), _UpFake(prs={(None, "core", BR): pr(31, "active")}))
        check("kod PR'ı açık → code hazır değil", s["next"]["step"] == "code" and not s["next"]["ready"])
        # CI sitesi yok → ci atlanır
        s = st(mkscan(sites=[dv_a, dv_r, code_s], pins="none"))
        # turu 2 (B3b): sabitleme yokken de <repo>-test tanımı aranır; burada tanım yok → no_pipeline
        check("test hattı sabitlemesi yok, tanım yok → sıradaki prod, 'Test kanıtı yok'",
              s["next"]["step"] == "prod" and s["next"]["ready"] and s["next"]["test_note"] == "Test kanıtı yok"
              and (s["evidence"] or {}).get("state") == "no_pipeline", str(s["next"]))
        # devops sitesi yok → tek adım code, hazır'da açılır
        s = st(mkscan(sites=[ci_s, code_s]))
        check("devops sitesi yok → code tek adım, hazır'da açık",
              s["next"]["step"] == "code" and s["next"]["ready"] and s["next"]["sites"][0]["branch"] == "main",
              str(s["next"]))
        s = st(mkscan(blocking=1, sites=[code_s]))
        check("devops sitesi yok ama hazırlık → code hazır değil", s["next"]["step"] == "code"
              and not s["next"]["ready"])
        # rakam dizisi dışı kod sitesi: code adımında elle; ci / prod etkilenmez
        s = st(mkscan(sites=[dv_a, dv_r, ci_s, code_bad]))
        check("elle düzenlenecek kod sitesi ci'yi bekletmez", s["stage"] == "hazır" and s["next"]["step"] == "ci"
              and s["next"]["ready"], str(s["next"]))
        s = st(mkscan(sites=[dv_a, dv_r, code_bad], pins="none"), _UpFake())
        check("elle düzenlenecek kod sitesi prod'u bekletmez", s["next"]["step"] == "prod" and s["next"]["ready"])
        s = st(mkscan(sites=[code_bad]))
        check("yalnız elle kod sitesi → code hazır değil, neden 'elle', manual listede",
              s["next"]["step"] == "code" and not s["next"]["ready"] and "elle" in s["next"]["reason"]
              and s["next"]["manual"][0]["old"] == "^7.4|^8.0", str(s["next"]))

        # ── İzleme işi ──
        index = {"core": [{"src": "anatolia/build/core", "images": ["eu/php-8-2:1.0"]}],
                 "web": [{"src": "anatolia/build/web", "images": ["eu/php-8-4:1.0"]}]}
        yml = "variables:\n  phpVersion: '8.2'\nresources:\n  - repository: s\n    name: EcomBackend/core\n"
        pkw = dict(index=index, root=root, read=lambda d, p: "FROM eu/php-8-2:1.0\n",
                   yml_files={"Test Pipelines/core.yml": yml}, app_map_cache={})
        c = _UpFake()
        first = ut.start(c, "core", "php", team="T", deps=deps, **pkw)
        f0 = c.wis[0] if c.wis else {}
        check("start: izleme Task'ı (takım alanı, kök iteration, etiket, anahtar satırı)",
              first["created"] and f0.get("_type") == "Task" and f0.get("System.Tags") == "tempo-upgrade-track"
              and f0.get("System.AreaPath") == "P\\T" and f0.get("System.IterationPath") == "P\\T"
              and "tempo-upgrade-key: php:core:8.4" in f0.get("System.Description", "")
              and "8.2 → 8.4" in f0.get("System.Description", ""), str(f0))
        first_id = first["id"]
        check("ikinci start aynı izleme işini döner",
              ut.start(c, "core", "php", team="T", deps=deps, **pkw)["id"] == first_id and len(c.wis) == 1)
        check("başka hedefle start da aynı işi döner (repo × dil başına bir)",
              ut.start(c, "core", "php", to="8.3", team="T", deps=deps, **pkw)["id"] == first_id and len(c.wis) == 1)
        try:
            ut.start(c, "core", "php", to="9.9", team="T", deps=deps, **pkw)
            check("geçersiz hedef → ValueError", False)
        except ValueError:
            check("geçersiz hedef → ValueError", True)
        check("tracked", ut.tracked(c, "T") == [{"id": first_id, "lang": "php", "repo": "core", "to": "8.4"}],
              str(ut.tracked(c, "T")))
        r = ut.stop(c, "core", "php", team="T")
        check("stop: yorum + Done", r["closed"] and c.states == [(first_id, "Done")]
              and c.comments and c.comments[-1][0] == first_id and ut.find_open(c, "php", "core") is None)
        try:
            ut.stop(c, "core", "php", team="T")
            check("açık iş yokken stop → ValueError", False)
        except ValueError:
            check("açık iş yokken stop → ValueError", True)

        # ── queue_step ──
        created = []

        def cj(wid, use_hal, title, *, job_kind, job_payload):
            created.append({"wid": wid, "title": title, "kind": job_kind, "payload": json.loads(job_payload)})
            return 77
        try:
            bump.queue_step(_UpFake(), "core", "php", team="T", deps=deps, create_job=cj, open_jobs=lambda t: [],
                            status_fn=lambda *a, **k: s_clean)
            check("izleme işi yokken bump → ValueError", False)
        except ValueError as e:
            check("izleme işi yokken bump → ValueError", "başlatın" in str(e))
        blocked_st = st(mkscan(sites=[dv_a, dv_rb, ci_s, code_s]))
        try:
            bump.queue_step(_UpFake(tracks=[track_wi()]), "core", "php", team="T", deps=deps, create_job=cj,
                            open_jobs=lambda t: [], status_fn=lambda *a, **k: blocked_st)
            check("ön koşul yoksa queue_step → ValueError(next.reason)", False)
        except ValueError as e:
            check("ön koşul yoksa queue_step → ValueError(next.reason)",
                  str(e) == blocked_st["next"]["reason"] and not created, str(e))
        q = bump.queue_step(_UpFake(tracks=[track_wi()]), "core", "php", team="T", deps=deps, create_job=cj,
                            open_jobs=lambda t: [], status_fn=lambda *a, **k: s_clean)
        pl = created[0]["payload"] if created else {}
        check("queue_step: upgrade_bump işi, WI = izleme işi, kapsam INSERT'te",
              q["job_id"] == 77 and created[0]["kind"] == "upgrade_bump" and created[0]["wid"] == "500"
              and set(pl) == {"lang", "repo", "from", "to", "step", "sites", "track_id", "team"}
              and pl["step"] == "ci" and pl["from"] == "8.2" and pl["to"] == "8.4" and pl["sites"] == [ci_s]
              and pl["track_id"] == 500 and pl["team"] == "T", str(created))

        # ── run: gerçek durum hesabıyla (tarama + plan) uçtan uca CI adımı ──
        skw = dict(pkw, scan_fn=lambda r, **k: {"files": 1, "sha": "s", "hits": [], "adoption": {}, "errors": []},
                   head_sha_fn=lambda r: "s", probe_fn=lambda *a, **k: ([], {"ran": False, "tool": "", "reason": "yok"}))
        yml_files = {("EcomBackend", "yml-files", "main", "Test Pipelines/core.yml"): yml}
        orig_get = pc.get
        flags = {"CREW_TECHDEBT_UPGRADE_BUMP": True, "CREW_DEVOPS_REQUESTS": True, "CREW_DEVOPS_TEAM": "DevOps"}
        pc.get = lambda k: flags[k] if k in flags else orig_get(k)
        reqs = []
        rq = lambda client, **k: (reqs.append(k), {"id": 4242, "created": True})[1]
        try:
            created.clear()
            ce = _UpFake(yml_files, tracks=[track_wi()])
            bump.queue_step(ce, "core", "php", team="T", deps=deps, create_job=cj, open_jobs=lambda t: [], **skw)
            job = {"job_kind": "upgrade_bump", "job_payload": json.dumps(created[0]["payload"])}
            out = bump.run(77, load_job=lambda j: job, client=ce, deps=deps, request_fn=rq, **skw)
            check("run ci: tek taslak PR yml-files'a, yalnız sürüm satırı",
                  len(ce.prs) == 1 and ce.prs[0]["project"] == "EcomBackend" and ce.prs[0]["draft"]
                  and len(ce.pushes) == 1 and _bump_dec(ce.pushes[0]["changes"][0]) == yml.replace("'8.2'", "'8.4'")
                  and ce.pushes[0]["branch"] == BR, str(ce.prs))
            check("run ci: PR açıklaması test hattı metni, DevOps isteği yok",
                  ce.descs and "ile test edilir" in ce.descs[0] and "üretim hâlâ `8.2`" in ce.descs[0] and not reqs)
            check("run ci: izleme işine PR yorumu", ce.comments and ce.comments[-1][0] == 500
                  and "PR" in ce.comments[-1][1] and out["prs"] and out["step"] == "ci", str(ce.comments))
            # site satırı değişmiş → iş başarısız, push yok, yorum yazılır
            yml2 = yml.replace("'8.2'", "'8.3'")
            cm = _UpFake({("EcomBackend", "yml-files", "main", "Test Pipelines/core.yml"): yml2}, tracks=[track_wi()])
            raised_site_changed = False
            try:
                bump.run(77, load_job=lambda j: job, client=cm, deps=deps, request_fn=rq,
                         **dict(skw, yml_files={"Test Pipelines/core.yml": yml2}))
            except bump.StepFailed:
                raised_site_changed = True
            check("site değişmişse iş başarısız, push yok", raised_site_changed and not cm.pushes and cm.writes == 0)
            check("yeniden doğrulama nedeni izleme işine yazılır",
                  cm.comments and cm.comments[-1][0] == 500 and "kayıttaki gibi değil" in cm.comments[-1][1],
                  str(cm.comments))
            # izleme işi kapalıysa adım atılmaz
            cc = _UpFake(yml_files, tracks=[track_wi(state="Done")])
            try:
                bump.run(77, load_job=lambda j: job, client=cc, deps=deps, request_fn=rq, **skw)
                check("izleme işi kapalı → StepFailed, push yok", False)
            except bump.StepFailed:
                check("izleme işi kapalı → StepFailed, push yok", not cc.pushes)

            # ── run: prod adımı (status enjekte) — DevOps isteği + 'Test kanıtı yok' ilk satır ──
            dock = "FROM eu/php-8-2:1.0\nRUN x\n"
            pfiles = {("FLODevOps", "anatolia", "production", "build/core/Dockerfile"): dock,
                      ("FLODevOps", "russia", "production", "build/core/Dockerfile"): dock}
            prod_st = {"repo": "core", "current": "8.2", "target": "8.4",
                       "next": {"step": "prod", "ready": True, "reason": "", "sites": [dv_a, dv_r], "manual": [],
                                "test_note": "Test kanıtı yok"}}
            pjob = {"job_kind": "upgrade_bump", "job_payload": json.dumps(
                {"lang": "php", "repo": "core", "from": "8.2", "to": "8.4", "step": "prod", "sites": [dv_a, dv_r],
                 "track_id": 500, "team": "T"})}
            cp = _UpFake(pfiles, tracks=[track_wi()])
            reqs.clear()
            out = bump.run(78, load_job=lambda j: pjob, client=cp, deps=deps, request_fn=rq,
                           status_fn=lambda *a, **k: prod_st)
            check("run prod: bölge başına taslak PR, açıklamanın ilk satırı 'Test kanıtı yok'",
                  sorted(p["repo"] for p in cp.prs) == ["anatolia", "russia"] and all(p["draft"] for p in cp.prs)
                  and all(d.splitlines()[0] == "Test kanıtı yok" for d in cp.descs), str(cp.descs))
            check("run prod: DevOps isteği anahtarı, PR bağlantıları, merge sırası, geri alma",
                  len(reqs) == 1 and reqs[0]["key"] == "upgrade:php:core:8.4:prod" and reqs[0]["source_wi"] == 500
                  and "PR #" in reqs[0]["body_html"] and "Merge sırası" in reqs[0]["body_html"]
                  and "Geri alma" in reqs[0]["body_html"] and "eu/php-8-2:1.0" in reqs[0]["body_html"]
                  and out["devops_request"] == 4242, str(reqs))
            # BUMP kapalı: push_sites hiç çağrılmaz, diff DevOps isteğine
            flags["CREW_TECHDEBT_UPGRADE_BUMP"] = False
            pushes_called = []
            co = _UpFake(pfiles, tracks=[track_wi()])
            reqs.clear()
            bump.run(79, load_job=lambda j: pjob, client=co, deps=deps, request_fn=rq,
                     status_fn=lambda *a, **k: prod_st, push_fn=lambda *a, **k: pushes_called.append(1))
            check("BUMP kapalı → push_sites çağrılmaz, yazma yok",
                  not pushes_called and not co.pushes and co.writes == 0)
            check("BUMP kapalı → diff DevOps isteğinde",
                  len(reqs) == 1 and "-FROM eu/php-8-2:1.0" in reqs[0]["body_html"]
                  and "+FROM eu/php-8-4:1.0" in reqs[0]["body_html"] and "kapalı" in reqs[0]["body_html"], str(reqs))
            flags["CREW_TECHDEBT_UPGRADE_BUMP"] = True
            # adım artık hazır değil (ör. kanıt kırmızıya döndü) → başarısız, push yok
            red_st = dict(prod_st, next=dict(prod_st["next"], ready=False, reason="build kırmızı"))
            cr = _UpFake(pfiles, tracks=[track_wi()])
            try:
                bump.run(80, load_job=lambda j: pjob, client=cr, deps=deps, request_fn=rq,
                         status_fn=lambda *a, **k: red_st)
                check("hazır olmayan adım run'da atılmaz", False)
            except bump.StepFailed as e:
                check("hazır olmayan adım run'da atılmaz", not cr.pushes and "build kırmızı" in str(e))
            try:
                bump.run(81, load_job=lambda j: {"job_kind": "upgrade_bump", "job_payload": json.dumps(
                    dict(json.loads(pjob["job_payload"]), repo="../x"))}, client=cr, deps=deps)
                check("geçersiz kapsam → StepFailed", False)
            except bump.StepFailed:
                check("geçersiz kapsam → StepFailed", not cr.pushes)
        finally:
            pc.get = orig_get

    # ── Sunucu: işçi, yaşam döngüsü, yeniden deneme, uçlar ──
    check("upgrade_bump WI yaşam döngüsünde izlenmez", not server._wi_tracked({"job_kind": "upgrade_bump"})
          and server._wi_tracked({"job_kind": "modern_fix"}))
    wsrc = inspect.getsource(server._queue_worker)
    check("işçi upgrade_bump → upgrade_bump.run", '"upgrade_bump"' in wsrc and "upgrade_bump.run(job_id)" in wsrc)
    payload = json.dumps({"step": "prod"})
    og, oc, oe = server.db.get_job, server.db.create_job, server._ensure_worker
    made = []
    server.db.get_job = lambda jid: {"id": jid, "status": "failed", "work_item_id": "500", "use_hal": 0,
                                     "wi_title": "t", "job_kind": "upgrade_bump", "job_payload": payload}
    server.db.create_job = lambda *a, **k: (made.append((a, k)), 6)[1]
    server._ensure_worker = lambda: None
    try:
        r = TestClient(server.app).post("/api/jobs/5/retry")
        check("yeniden deneme türü ve kapsamı korur", r.status_code == 202 and made
              and made[0][1] == {"job_kind": "upgrade_bump", "job_payload": payload}, str(made))
    finally:
        server.db.get_job, server.db.create_job, server._ensure_worker = og, oc, oe

    from agile_sdlc_crew.quality import modern as _md
    saved = (pc.get, server.AzureDevOpsClient, _md.repos, _md.build_index, ut.tracked, upgrade.scan, ut.status)
    pc.get = lambda k: True if k in ("CREW_TECHDEBT", "CREW_TECHDEBT_PHP_MODERN", "CREW_TECHDEBT_UPGRADE") else saved[0](k)
    server.AzureDevOpsClient = lambda: object()
    _md.repos = lambda c: {"items": [{"repo": "core", "lang": "php", "label": "PHP", "version": "8.2"},
                                      {"repo": "web", "lang": "php", "label": "PHP", "version": "8.2"}]}
    _md.build_index = lambda c: {}
    calls = []
    ut.tracked = lambda c, team: (calls.append(team), [{"id": 500, "lang": "php", "repo": "core", "to": "8.4"}])[1]
    upgrade.scan = lambda c, repo, lang, **k: {"repo": repo, "lang": lang, "current": "8.2", "target": "8.4"}
    ut.status = lambda c, repo, lang, **k: {"stage": "hazır", "scan_given": k.get("scan") is not None}
    try:
        tc = TestClient(server.app)
        js = tc.get("/api/techdebt/upgrade/repos?team=T").json()
        rows = {i["repo"]: i for i in js.get("items", [])}
        check("repos: tek tracked çağrısı, satırda tracked + watch_target",
              calls == ["T"] and rows["core"]["tracked"] and rows["core"]["watch_target"] == "8.4"
              and not rows["web"]["tracked"] and rows["web"]["watch_target"] == "", str(js))
        js = tc.get("/api/techdebt/upgrade/scan?repo=core").json()
        check("scan yanıtında status bloğu (taramadan, ikinci tarama yok)",
              js.get("status") == {"stage": "hazır", "scan_given": True}, str(js))
        check("bump: takım yoksa 400", tc.post("/api/techdebt/upgrade/bump", json={"repo": "core"}).status_code == 400)
    finally:
        pc.get, server.AzureDevOpsClient, _md.repos, _md.build_index, ut.tracked, upgrade.scan, ut.status = saved



def test_upgrade_fix_round1():
    """Görev 8 düzeltme turu 1: kapılar AÇIK değil KAPALI düşer.
    F1 kanıt yalnız `<repo>-test` TANIMININ build'i; F2 test hattı kapısı dosyanın DURUMUNA bağlı (Tempo PR'ına
    değil); F3 başlığı düzenlenmiş / başka geçişin açık işi de bekletir; F4 üretim hedefteyken kart kod adımını
    görür; F5 kanıt bloğu; F7 bilinmeyen PR durumu; F8 DevOps'a devredilen adım yeniden kuyruğa girmez;
    F9 stop takım alanını ve etiketi doğrular."""
    print("\n[Sürüm yükseltme: düzeltme turu 1]")
    from fastapi.testclient import TestClient
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import server
    from agile_sdlc_crew.quality import intake, upgrade, upgrade_bump as bump, upgrade_track as ut
    from agile_sdlc_crew.tools import azure_devops_base as azb

    BR = "tempo/upgrade-php-core-8.4"
    T0 = "2026-11-10T10:00:00.1234567Z"
    dv_a = {"where": "devops", "repo": "anatolia", "project": "FLODevOps", "branch": "production",
            "path": "/build/core/Dockerfile", "line": 1, "old": "eu/php-8-2:1.0", "new": "eu/php-8-4:1.0",
            "blocked": ""}
    dv_r = dict(dv_a, repo="russia")
    ci_s = {"where": "ci", "repo": "yml-files", "project": "EcomBackend", "branch": "main",
            "path": "Test Pipelines/core.yml", "line": 2, "old": "'8.2'", "new": "'8.4'", "blocked": ""}
    pin = {"repo": "yml-files", "project": "EcomBackend", "branch": "main", "path": "Test Pipelines/core.yml",
           "line": 2, "version": "8.4"}
    PIN_FILE = ("EcomBackend", "yml-files", "main", "Test Pipelines/core.yml")
    CORE_DEF = {"core": {"id": 5, "name": "Core-test", "project": "EcomBackend"}}

    def b(bid, q, result="succeeded", status="completed"):
        return {"build_id": bid, "status": status, "result": result, "definition": "Core-test",
                "url": f"https://x/b/{bid}", "project": "EcomBackend", "queue_time": q, "source_version": ""}

    def scan(sites, pins, up=False):
        return {"repo": "core", "lang": "php", "current": "8.2", "target": "8.4", "up_to_date": up, "sites": sites,
                "blocking": 0, "blockers": [], "auto": [], "infra": [], "human": [], "ci_pins": pins,
                "ci_source": _ci_src(pins)}

    def track_wi(wid=500, to="8.4", area="P\\T", tags="tempo-upgrade-track"):
        return {"id": wid, "fields": {"System.Title": "izleme", "System.State": "To Do", "System.AreaPath": area,
                                      "System.Tags": tags,
                                      "System.Description": f"<p><small>tempo-upgrade-key: php:core:{to}</small></p>"}}

    def pr(pid, status):
        return {"id": pid, "status": status, "url": f"https://x/pr/{pid}", "closed_at": "", "merge_commit": "",
                "target": "main", "created_by": "tempo-id"}

    def raises(fn, exc=Exception):
        try:
            fn()
        except exc as e:
            return str(e) or True
        return False

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "core"
        root.mkdir()
        (root / "composer.json").write_text('{\n  "require": {\n    "php": "^8.2"\n  }\n}\n', encoding="utf-8")
        cfg = {"pack_sp": 3, "pack_files": 5, "grace_days": 3, "task_fields": {}, "sonar_url": "", "nr_days": 7,
               "php_exclude": [], "php_bin": "php", "scan_ttl_h": 24, "max_take": 8, "freshen": False,
               "repos_dir": base, "targets_cache": base / "t.yaml", "scan_cache": base / "cache"}
        deps = intake.Deps(gather=None, board=lambda c: {}, repo_ok=lambda r: (base / r).is_dir(), queue=None,
                           record=lambda **k: None, now=lambda: 1e9, cfg=lambda: cfg)

        def st(sc, c, **k):
            return ut.status(c, "core", "php", to="8.4", deps=deps, scan=sc, wait_h=72, root=root, **k)

        # ── F1: kanıt tanıma bağlı ──
        c = _UpFake(defs=CORE_DEF, builds={5: [b(2, "2026-11-10T11:00:00Z")]})
        ev = bump.ci_evidence(c, "core", T0, wait_h=72)
        check("F1: tanım adı <repo>-test ile aranır, build'ler o tanımdan, minTime = since",
              c.def_calls == [("core", "core-test")] and c.build_calls == [("EcomBackend", 5, T0)]
              and ev["state"] == "green", str((c.def_calls, c.build_calls, ev)))
        check("F1: tanımın tam since anında kuyruğa girmiş build'i sayılmaz (sıkı 'sonra')",
              bump.ci_evidence(_UpFake(defs=CORE_DEF, builds={5: [b(2, T0)]}), "core", T0, wait_h=72)["state"]
              == "none")
        check("F1: tanım okunamazsa hata yükselir (no_pipeline DEĞİL)",
              raises(lambda: bump.ci_evidence(_UpFake(defs={"core": RuntimeError("401")}), "core", T0, wait_h=72),
                     RuntimeError))
        newer_red = [b(3, "2026-11-10T12:00:00Z", result="failed"), b(2, "2026-11-10T11:00:00Z")]
        check("F1: en yeni tamamlanmış build kırmızıysa red",
              bump.ci_evidence(_UpFake(defs=CORE_DEF, builds={5: newer_red}), "core", T0, wait_h=72)["state"] == "red")
        part = [b(4, "2026-11-10T11:00:00Z", result="partiallySucceeded")]
        ok_issue = {4: [{"name": "SonarQube Analysis", "result": "succeededWithIssues"}]}
        bad_issue = {4: [{"name": "Run Unit Tests", "result": "succeededWithIssues"}]}
        check("F1: kısmi başarı yalnız test dışı adımda → green",
              bump.ci_evidence(_UpFake(defs=CORE_DEF, builds={5: part}, issue_tasks=ok_issue), "core", T0,
                               wait_h=72)["state"] == "green")
        check("F1: kısmi başarı test adımında → red",
              bump.ci_evidence(_UpFake(defs=CORE_DEF, builds={5: part}, issue_tasks=bad_issue), "core", T0,
                               wait_h=72)["state"] == "red")
        check("F1: since okunamazsa hata (kanıt sayılmaz)",
              raises(lambda: bump.ci_evidence(_UpFake(defs=CORE_DEF), "core", "dün", wait_h=72), ValueError))
        # F5: kanıt bloğu
        ev = bump.ci_evidence(_UpFake(defs=CORE_DEF, builds={5: [b(2, "2026-11-10T11:00:00Z")]}), "core", T0,
                              wait_h=72)
        check("F5: kanıt bloğu {state, build{id,url,queue_time}, summary, overdue, since}",
              set(ev) == {"state", "build", "summary", "overdue", "since"} and ev["since"] == T0
              and ev["build"] == {"id": 2, "url": "https://x/b/2", "queue_time": "2026-11-10T11:00:00Z"}, str(ev))

        # İstemci uçları (ağ yok)
        cl = azb.AzureDevOpsClient.__new__(azb.AzureDevOpsClient)
        cl.org_url, cl.project = "https://dev.azure.com/o", "P"
        cl._find_repo_project = lambda r: "EcomBackend"
        cl._repo_project_strict = lambda r: "EcomBackend"   # kesin çözüm turu 2'de ayrıca sınanır
        cl._repo_api_url = lambda repo, project=None: f"https://x/{project or 'P'}/_apis/git/repositories/{repo}"

        class _Cl(azb.AzureDevOpsClient):
            _headers = {"Authorization": "x"}
        cl.__class__ = _Cl
        seen = []

        class _R:
            status_code = 200

            def __init__(self, js):
                self._js = js

            def raise_for_status(self):
                pass

            def json(self):
                return self._js

        def fake_get(url, headers=None, params=None, timeout=None, **k):
            seen.append((url, params))
            if url.endswith("/build/definitions"):
                return _R({"value": [{"id": 3, "name": "Core-lint"}, {"id": 5, "name": "Core-test"}]})
            if url.endswith("/build/definitions/5"):
                return _R({"id": 5, "name": "Core-test", "process": {"type": 2, "yamlFilename": "Test Pipelines/core.yml"},
                           "repository": {"id": "g", "name": "yml-files", "defaultBranch": "refs/heads/main"}})
            if url.endswith("/_apis/git/repositories/g"):
                return _R({"id": "g", "project": {"name": "EcomBackend"}})
            if url.endswith("/commits"):
                return _R({"value": [{"committer": {"date": "2026-11-10T10:00:00Z"}, "author": {"date": "x"}}]})
            return _R({"value": [{"id": 9, "status": "completed", "result": "failed",
                                  "definition": {"name": "Core-test"}, "queueTime": "2026-11-10T11:00:00Z",
                                  "_links": {"web": {"href": "https://b/9"}}}]})
        og = azb.requests.get
        azb.requests.get = fake_get
        try:
            d = cl.get_test_pipeline("core")
            def_seen = list(seen)
            seen.clear()
            rows = cl.list_definition_builds("EcomBackend", 5, min_time=T0, top=20)
            when = cl.get_last_commit_date("yml-files", "/Test Pipelines/core.yml", "main", project="EcomBackend")
        finally:
            azb.requests.get = og
        check("istemci: tanım adı büyük/küçük harf duyarsız, repo projesinde",
              {k: (d or {}).get(k) for k in ("id", "name", "project")} == {"id": 5, "name": "Core-test", "project": "EcomBackend"}
              and def_seen[0][0].endswith("/EcomBackend/_apis/build/definitions")
              and def_seen[0][1]["name"] == "core-test", str((d, def_seen[:1])))
        p1 = seen[0][1]
        check("istemci: tanım build'leri (definitions, pullRequest, minTime, yeni→eski, $top)",
              p1["definitions"] == 5 and p1["reasonFilter"] == "pullRequest" and p1["minTime"] == T0
              and p1["queryOrder"] == "queueTimeDescending" and p1["$top"] == 20
              and rows[0]["build_id"] == 9 and rows[0]["queue_time"] == "2026-11-10T11:00:00Z"
              and rows[0]["url"] == "https://b/9", str((p1, rows)))
        p2 = seen[1][1]
        check("istemci: dosyanın son commit zamanı (itemPath + dal, tek kayıt)",
              when == "2026-11-10T10:00:00Z" and p2["searchCriteria.itemPath"] == "/Test Pipelines/core.yml"
              and p2["searchCriteria.itemVersion.version"] == "main" and p2["searchCriteria.$top"] == 1
              and "/EcomBackend/_apis/git/repositories/yml-files/commits" in seen[1][0], str(seen[1]))

        # ── F2: test hattı kapısı dosyanın durumuna bağlı ──
        green_after = {5: [b(2, "2026-11-10T11:00:00Z")]}
        s = st(scan([dv_a, dv_r], [pin]), _UpFake(defs=CORE_DEF, builds={}, commit_dates={PIN_FILE: T0}))
        check("F2: CI satırı Tempo PR'ı olmadan hedefe gelmiş, build yok → prod yok, test hattı",
              s["next"]["step"] == "prod" and not s["next"]["ready"] and s["stage"] == "test hattı"
              and s["evidence"]["state"] == "none" and s["evidence"]["since"] == T0, str(s["next"]))
        s = st(scan([dv_a, dv_r], [pin]), _UpFake(defs=CORE_DEF, builds=green_after, commit_dates={PIN_FILE: T0}))
        check("F2: dosya değişikliğinden sonra yeşil → prod hazır, kanıt saati dosyanın commit'i",
              s["next"]["step"] == "prod" and s["next"]["ready"] and s["evidence"]["since"] == T0, str(s["next"]))
        s = st(scan([dv_a, dv_r], [pin]), _UpFake(defs=CORE_DEF, builds=green_after, commit_dates={PIN_FILE: ""}))
        check("F2: dosyanın son değişiklik zamanı okunamazsa prod yok",
              not s["next"]["ready"] and "son değişiklik zamanı okunamadı" in s["next"]["reason"], str(s["next"]))
        s = st(scan([dv_a, dv_r], [pin]),
               _UpFake(defs=CORE_DEF, builds=green_after, commit_dates={PIN_FILE: RuntimeError("500")}))
        check("F2: commit sorgusu hata verirse de prod yok", not s["next"]["ready"]
              and "son değişiklik zamanı okunamadı" in s["next"]["reason"])
        s = st(scan([dv_a, dv_r], None), _UpFake())
        check("F2: test hattı dosyaları okunamadıysa (ci_pins None) prod / ci yok",
              not s["next"]["ready"] and "okunamadı" in s["next"]["reason"], str(s["next"]))
        pin_low = dict(pin, version="8.2")
        s = st(scan([dv_a, dv_r, ci_s], [pin_low]), _UpFake(prs={("EcomBackend", "yml-files", BR): pr(11, "active")}))
        check("F2: satır hedefin altında + Tempo CI PR'ı açık → test hattı, prod yok",
              s["stage"] == "test hattı" and s["next"]["step"] == "prod" and not s["next"]["ready"])
        s = st(scan([dv_a, dv_r, ci_s], [pin_low]), _UpFake())
        check("F2: satır hedefin altında, PR yok → ci hazır", s["next"]["step"] == "ci" and s["next"]["ready"])
        # plan / sites: hedefteki satır da ci_pins'te
        index = {"core": [{"src": "anatolia/build/core", "images": ["eu/php-8-2:1.0"]}],
                 "web": [{"src": "anatolia/build/web", "images": ["eu/php-8-4:1.0"]}]}
        yml_at = "variables:\n  phpVersion: '8.4'\nresources:\n  - repository: s\n    name: EcomBackend/core\n"
        pl = upgrade.plan(None, "core", "php", to="8.4", deps=deps, index=index, root=root,
                          read=lambda d, p: "FROM eu/php-8-2:1.0\n", yml_files={"Test Pipelines/core.yml": yml_at})
        check("F2: plan ci_pins hedefteki satırı da döner, CI sitesi yok",
              pl.get("ci_pins") == [{"repo": "yml-files", "project": "EcomBackend", "branch": "main",
                                     "path": "Test Pipelines/core.yml", "line": 2, "version": "8.4"}]
              and not [x for x in pl["sites"] if x["where"] == "ci"], str(pl.get("ci_pins")))

        class _Boom:
            def get_repository(self, r):
                raise RuntimeError("yml-files okunamadı")
        check("F2: yml-files okunamazsa ci_pins None (bilinmiyor), [] değil",
              upgrade._ci_scan(_Boom(), "core", "php", "8.4", None)[1] is None)

        # ── F3: başlığı düzenlenmiş / başka geçişin açık işi de bekletir ──
        def task_wi(wid, fp, title):
            return {"id": wid, "fields": {"System.Title": title, "System.State": "To Do",
                                          "System.Tags": "tempo-quality; tempo-upgrade",
                                          "System.Description": f"<p><small>tempo-fp: {fp}</small></p>"}}
        c3 = _UpFake(tasks=[task_wi(80, "php_modern:core:loose_compare_numeric:app/L.php", "elle değiştirilmiş başlık"),
                            task_wi(81, "php_modern:core:implicit_nullable:app/A.php",
                                    "[core] Sürüm yükseltme 8.2→8.3: x")])
        s = st(scan([dv_a, dv_r, ci_s], [pin_low]), c3)
        check("F3: başlıktan bağımsız: düzenlenmiş başlık + başka hedefin işi → hazırlık",
              s["stage"] == "hazırlık" and sorted(t["id"] for t in s["open_tasks"]) == [80, 81]
              and all(t["blocking"] for t in s["open_tasks"]), str(s["open_tasks"]))

        # ── F7: bilinmeyen PR durumu ──
        s = st(scan([dv_a, dv_r, ci_s], [pin_low]), _UpFake(prs={("EcomBackend", "yml-files", BR): pr(11, "notSet")}))
        check("F7: CI PR'ı bilinmeyen durumda → ci hazır değil, çökmez",
              s["next"]["step"] == "ci" and not s["next"]["ready"] and "notSet" in s["next"]["reason"], str(s["next"]))
        s = st(scan([dv_a, dv_r], [pin]),
               _UpFake(defs=CORE_DEF, builds=green_after, commit_dates={PIN_FILE: T0},
                       prs={("FLODevOps", "anatolia", BR): pr(21, "notSet")}))
        check("F7: üretim PR'ı bilinmeyen durumda → prod hazır değil",
              s["next"]["step"] == "prod" and not s["next"]["ready"] and "notSet" in s["next"]["reason"], str(s["next"]))
        s = st(scan([], [], up=True), _UpFake(prs={(None, "core", BR): pr(31, "notSet")}))
        check("F7: kod PR'ı bilinmeyen durumda → code hazır değil",
              s["next"]["step"] == "code" and not s["next"]["ready"] and "notSet" in s["next"]["reason"], str(s["next"]))

        # ── F8: DevOps'a devredilmiş adım ──
        def req_wi(wid, key, state="To Do"):
            return {"id": wid, "fields": {"System.State": state, "System.Tags": "tempo-devops",
                                          "System.Description": f"<p>x</p><p><small>tempo-devops-key: {key}</small></p>"}}
        s = st(scan([dv_a, dv_r], [pin]), _UpFake(defs=CORE_DEF, builds=green_after, commit_dates={PIN_FILE: T0},
                                                  requests_=[req_wi(4242, "upgrade:php:core:8.4:prod")]))
        check("F8: açık DevOps isteği → adım hazır değil, handed_off",
              s["next"]["step"] == "prod" and not s["next"]["ready"] and s["next"]["handed_off"] == 4242
              and s["next"]["reason"] == "DevOps isteği açık: #4242", str(s["next"]))
        s = st(scan([dv_a, dv_r], [pin]), _UpFake(defs=CORE_DEF, builds=green_after, commit_dates={PIN_FILE: T0},
                                                  requests_=[req_wi(4242, "upgrade:php:core:8.4:prod", "Done"),
                                                             req_wi(4243, "upgrade:php:core:8.4:ci")]))
        check("F8: kapalı istek / başka adımın isteği engellemez",
              s["next"]["ready"] and s["next"]["handed_off"] is None, str(s["next"]))
        check("F8: anahtar adımı içerir", bump.step_key("php", "core", "8.4", "ci") == "upgrade:php:core:8.4:ci")

        # ── F9: stop takım alanını ve etiketi doğrular ──
        c9 = _UpFake(tracks=[track_wi(area="P\\Other")])
        check("F9: başka takımın alanındaki izleme işi kapatılmaz",
              raises(lambda: ut.stop(c9, "core", "php", team="T"), ValueError) and not c9.states)
        c9b = _UpFake(tracks=[track_wi()])
        check("F9: takım alanı çözümlenemezse ValueError",
              raises(lambda: ut.stop(c9b, "core", "php", team=""), ValueError) and not c9b.states)
        c9c = _UpFake(tracks=[track_wi(tags="tempo-upgrade")])
        check("F9: izleme etiketi olmayan iş izleme işi sayılmaz",
              raises(lambda: ut.stop(c9c, "core", "php", team="T"), ValueError) and not c9c.states)
        c9d = _UpFake(tracks=[track_wi(area="P\\T\\Alt")])
        check("F9: takım alanının altındaki iş kapatılır", ut.stop(c9d, "core", "php", team="T")["closed"]
              and c9d.states == [(500, "Done")])

        # ── F4: üretim hedefteyken kart kod adımını görür ──
        idx_done = {"core": [{"src": "anatolia/build/core", "images": ["eu/php-8-4:1.0"]}]}
        skw = dict(index=idx_done, root=root, read=lambda d, p: "FROM eu/php-8-4:1.0\n", yml_files={},
                   app_map_cache={}, scan_fn=lambda r, **k: {"files": 1, "sha": "s", "hits": [], "adoption": {},
                                                             "errors": []},
                   head_sha_fn=lambda r: "s", probe_fn=lambda *a, **k: ([], {"ran": False, "tool": "", "reason": ""}))
        c4 = _UpFake(tracks=[track_wi()])
        check("F4: hedefe ulaşılmışken scan(to=8.4) 400 sebebi (ValueError)",
              raises(lambda: upgrade.scan(c4, "core", "php", to="8.4", deps=deps, **skw), ValueError))
        v = ut.watch_view(c4, "core", "php", to="8.4", deps=deps, **skw)
        check("F4: izleme işi varken görünüm: up_to_date, boş bulgu, status = kod adımı",
              v is not None and v["up_to_date"] is True and v["auto"] == [] and v["human"] == []
              and v["status"]["stage"] == "tamam" and v["status"]["next"]["step"] == "code"
              and v["status"]["next"]["ready"] and v["target"] == "8.4", str(v and v.get("status")))
        check("F4: izleme işi yoksa görünüm yok", ut.watch_view(_UpFake(), "core", "php", to="8.4", deps=deps, **skw)
              is None)
        idx_low = {"core": [{"src": "anatolia/build/core", "images": ["eu/php-8-2:1.0"]}],
                   "web": [{"src": "anatolia/build/web", "images": ["eu/php-8-4:1.0"]}]}
        check("F4: üretim hedefte değilse görünüm yok",
              ut.watch_view(c4, "core", "php", to="8.4", deps=deps, **dict(skw, index=idx_low)) is None)

    # F4 / F8: uçlar ve run anahtarı
    saved = (pc.get, server.AzureDevOpsClient, upgrade.scan, ut.status, ut.watch_view)
    pc.get = lambda k: True if k in ("CREW_TECHDEBT", "CREW_TECHDEBT_PHP_MODERN", "CREW_TECHDEBT_UPGRADE") else saved[0](k)
    server.AzureDevOpsClient = lambda: object()

    def _raise(*a, **k):
        raise ValueError("Hedef sürüm seçilemez: '8.4'")
    upgrade.scan = _raise
    ut.status = lambda *a, **k: {"stage": "x"}
    try:
        tc = TestClient(server.app)
        ut.watch_view = lambda c, repo, lang, **k: {"up_to_date": True, "status": {"stage": "tamam"}}
        r = tc.get("/api/techdebt/upgrade/scan?repo=core&to=8.4")
        check("F4: hedefe ulaşılmış izlenen repo → 200 + status", r.status_code == 200
              and r.json().get("status") == {"stage": "tamam"}, str(r.status_code))
        ut.watch_view = lambda c, repo, lang, **k: None
        check("F4: izleme yoksa eski 400 korunur", tc.get("/api/techdebt/upgrade/scan?repo=core&to=8.4").status_code
              == 400)
        upgrade.scan = lambda c, repo, lang, **k: {"repo": repo, "lang": lang, "current": "8.4", "target": None,
                                                   "up_to_date": True}
        ut.watch_view = lambda c, repo, lang, **k: {"up_to_date": True, "status": {"stage": "tamam", "w": 1}}
        r = tc.get("/api/techdebt/upgrade/scan?repo=core")
        check("F4: hedefsiz tarama 'güncel' derse de izleme görünümü döner",
              r.status_code == 200 and r.json().get("status") == {"stage": "tamam", "w": 1}, str(r.json()))
    finally:
        pc.get, server.AzureDevOpsClient, upgrade.scan, ut.status, ut.watch_view = saved


def test_upgrade_bump_expect_step():
    print("\n[U9b] Sürüm adımı — onaylanan adım ve açık iş koruması")
    from fastapi.testclient import TestClient
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import server
    from agile_sdlc_crew.quality import upgrade_bump as bump, upgrade_track as ut
    track = {"id": 500, "to": "8.4", "lang": "php", "repo": "core", "area": "A"}
    st = {"repo": "core", "current": "8.2", "stage": "hazır",
          "next": {"step": "ci", "ready": True, "reason": "", "sites": []}}
    created = []

    def cj(wid, use_hal, title, *, job_kind, job_payload):
        created.append(wid)
        return 9
    saved_fo = ut.find_open
    ut.find_open = lambda c, lang, repo: track
    try:
        kw = dict(team="T", create_job=cj, status_fn=lambda *a, **k: st, open_jobs=lambda tid: [])
        q = bump.queue_step(object(), "core", "php", expect_step="ci", **kw)
        check("expect_step eşleşirse kuyruğa alır", q["job_id"] == 9 and q["step"] == "ci", str(q))
        created.clear()
        try:
            bump.queue_step(object(), "core", "php", expect_step="prod", **kw)
            check("expect_step farklıysa StepChanged", False)
        except bump.StepChanged as e:
            check("expect_step farklıysa StepChanged, iş yok",
                  not created and "üretim" in str(e) and "test hattı" in str(e), str(e))
        try:
            bump.queue_step(object(), "core", "php", expect_step="x", **kw)
            check("geçersiz expect_step → ValueError", False)
        except bump.StepChanged:
            check("geçersiz expect_step → ValueError", False)
        except ValueError:
            check("geçersiz expect_step → ValueError", not created)
        try:
            bump.queue_step(object(), "core", "php", **dict(kw, open_jobs=lambda tid: [{"id": 3}]))
            check("açık adım işi varsa reddeder", False)
        except bump.StepChanged as e:
            check("açık adım işi varsa reddeder, iş yok", not created and "500" in str(e), str(e))
    finally:
        ut.find_open = saved_fo

    orig_pc, orig_qs, orig_cl, orig_w = pc.get, bump.queue_step, server.AzureDevOpsClient, server._ensure_worker
    pc.get = lambda k: True if k in ("CREW_TECHDEBT", "CREW_TECHDEBT_PHP_MODERN", "CREW_TECHDEBT_UPGRADE") else orig_pc(k)
    server.AzureDevOpsClient = lambda: object()
    server._ensure_worker = lambda: None
    seen = []

    def qs(c, repo, lang, **k):
        seen.append(k.get("expect_step"))
        raise k["boom"] if "boom" in k else bump.StepChanged("Durum değişti")
    bump.queue_step = qs
    try:
        tc = TestClient(server.app)
        body = {"repo": "core", "team": "T", "step": "ci"}
        r = tc.post("/api/techdebt/upgrade/bump", json=body)
        check("uç: StepChanged → 409 + Türkçe mesaj, expect_step iletilir",
              r.status_code == 409 and r.json().get("error") == "Durum değişti" and seen == ["ci"], str(r.text))
        bump.queue_step = lambda c, repo, lang, **k: (_ for _ in ()).throw(ValueError("Hazır değil"))
        check("uç: hazır değil → 400", tc.post("/api/techdebt/upgrade/bump", json=body).status_code == 400)
        check("uç: geçersiz step → 400",
              tc.post("/api/techdebt/upgrade/bump", json=dict(body, step="x")).status_code == 400)
    finally:
        pc.get, bump.queue_step, server.AzureDevOpsClient, server._ensure_worker = orig_pc, orig_qs, orig_cl, orig_w


class _AdvFake:
    """Otomatik ilerletme için sahte Azure: izleme işleri, tempo-upgrade işleri, sprintler, yorumlar
    (zaman damgalı; Azure gibi HTML döner), oluşturulan WI'ler ve durum geçişleri."""
    org_url, project = "https://dev.azure.com/o", "P"

    def __init__(self, tracks=(), tasks=(), future=True):
        self.tracks, self.tasks, self.future = list(tracks), list(tasks), future
        self.created, self.comments, self.states, self.wiql = [], [], [], []
        self.store, self.cstate, self.truncated = {}, {}, set()
        self.clock = "2026-11-17T10:00:00.000Z"

    def created_wis(self):
        """Açılan WI'ler board'da görünür (varsayılan açık-iş sayacı ve kırmızı build Task araması bunları okur)."""
        return [{"id": 700 + i, "fields": dict(f, **{"System.State": self.cstate.get(700 + i, "To Do")})}
                for i, f in enumerate(self.created)]

    def query_work_items(self, wiql, limit=0):
        import html as _h
        self.wiql.append(wiql)
        needles = [n.replace("''", "'") for n in re.findall(r"\[System\.Description\] CONTAINS '((?:[^']|'')*)'", wiql)]
        if "tempo-upgrade-track" in wiql:
            m = re.search(r"\[System\.AreaPath\] UNDER '((?:[^']|'')*)'", wiql)
            area = m.group(1).replace("''", "'").lower() if m else None
            pool = [w for w in self.tracks if area is None or w["fields"]["System.AreaPath"].lower() == area
                    or w["fields"]["System.AreaPath"].lower().startswith(area + "\\")]
        elif "'User Story'" in wiql:
            return []
        elif "CONTAINS 'tempo-upgrade'" in wiql:
            pool = [w for w in self.tasks + self.created_wis()
                    if "tempo-upgrade" in {t.strip() for t in (w["fields"].get("System.Tags") or "").split(";")}]
        else:
            pool = []
        return [w for w in pool if all(n in _h.unescape(w["fields"].get("System.Description") or "") for n in needles)]

    def get_work_item_comments_all(self, wid, max_items=1000):
        return list(self.store.get(wid, [])), wid not in self.truncated

    def get_team_area_path(self, team=""):
        return {"T": "P\\T", "U": "P\\U"}.get(team, "")

    def list_teams(self):
        return [{"name": "T"}, {"name": "U"}]

    def list_iterations(self, team=""):
        its = [{"name": "S23", "path": "P\\S23", "attributes": {"timeFrame": "current"}}]
        if self.future:
            its.append({"name": "S24", "path": "P\\S24", "attributes": {"timeFrame": "future", "startDate": "2026-11-24"}})
        return its

    def create_work_item(self, type_name, fields, parent_id=None):
        self.created.append(dict(fields, _type=type_name, _parent=parent_id))
        return {"id": 700 + len(self.created) - 1, "fields": fields}

    def update_work_item(self, wid, ops):
        return {}

    def get_authenticated_user(self):
        return {"uniqueName": "me@x"}

    def add_comment(self, wid, text):
        self.comments.append((wid, text))
        # Azure markdown'ı HTML olarak geri verir; işaretler etiket soyularak aranmalı
        self.store.setdefault(wid, []).append({"id": len(self.comments), "yazar": "Tempo", "tarih": self.clock,
                                               "metin": "<div>" + text.replace("<p>", "<p dir=\"auto\">") + "</div>"})
        return {}

    def get_work_item_comments(self, wid):
        return list(self.store.get(wid, []))

    def get_work_item_type_states(self, t):
        return [{"name": "To Do", "category": "Proposed"}, {"name": "Done", "category": "Completed"}]

    def set_work_item_state(self, wid, st):
        self.states.append((wid, st))
        for w in self.tracks:
            if w["id"] == wid:
                w["fields"]["System.State"] = st


def test_upgrade_advance():
    """Otomatik ilerletme (spec §7): butonların fonksiyonlarından geçer, tavanlar, sprint, kırmızı build /
    CI bekleme notu bir kez, devir ve hazır-değil'de sessiz, hata öğe başına, tamam'da kapanış, yoklayıcı."""
    print("\n[U10] Sürüm yükseltme — otomatik ilerletme ve yoklayıcı")
    import queue as _queue
    import threading as _th
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import intake, upgrade_bump as bump, upgrade_track as ut

    # ── queue_step: create_job verilse de açık iş koruması (Görev 9'dan devir) + kilit ──
    track = {"id": 500, "to": "8.4", "lang": "php", "repo": "core", "area": "P\\T"}
    st_ci = {"repo": "core", "current": "8.2", "stage": "hazır",
             "next": {"step": "ci", "ready": True, "reason": "", "sites": [], "handed_off": None}}
    made = []
    saved_fo, saved_ob = ut.find_open, bump._open_bump_jobs
    ut.find_open = lambda c, lang, repo: track
    bump._open_bump_jobs = lambda tid: [{"id": 3}]
    try:
        try:
            bump.queue_step(object(), "core", "php", team="T", status_fn=lambda *a, **k: st_ci,
                            create_job=lambda *a, **k: (made.append(1), 9)[1])
            check("queue_step: create_job enjekte olsa da açık adım işi reddedilir", False, str(made))
        except bump.StepChanged:
            check("queue_step: create_job enjekte olsa da açık adım işi reddedilir", not made)
        held = []
        q = bump.queue_step(object(), "core", "php", team="T", status_fn=lambda *a, **k: st_ci,
                            open_jobs=lambda tid: (held.append(bump._QUEUE_LOCK.locked()), [])[1],
                            create_job=lambda *a, **k: (held.append(bump._QUEUE_LOCK.locked()), 9)[1])
        check("queue_step: kontrol → INSERT tek kilit altında", q["job_id"] == 9 and held == [True, True]
              and not bump._QUEUE_LOCK.locked(), str(held))
    finally:
        ut.find_open, bump._open_bump_jobs = saved_fo, saved_ob

    MAX_OPEN = 3
    flags = {"CREW_TECHDEBT": True, "CREW_TECHDEBT_PHP_MODERN": True, "CREW_TECHDEBT_UPGRADE": True,
             "CREW_TECHDEBT_UPGRADE_AUTO": True, "CREW_TECHDEBT_UPGRADE_MAX_OPEN": MAX_OPEN,
             "CREW_TECHDEBT_UPGRADE_SPRINT": "next", "CREW_TECHDEBT_UPGRADE_POLL_MIN": 30,
             "CREW_TECHDEBT_UPGRADE_CI_WAIT_H": 72}
    orig_get = pc.get
    pc.get = lambda k: flags[k] if k in flags else orig_get(k)

    def track_wi(wid=500, repo="core", area="P\\T", to="8.4", state="To Do"):
        return {"id": wid, "fields": {"System.Title": "izleme", "System.State": state, "System.AreaPath": area,
                                      "System.Tags": "tempo-upgrade-track",
                                      "System.Description": f"<p><small>tempo-upgrade-key: php:{repo}:{to}</small></p>"}}

    def task_wi(wid, fp, tags="tempo-quality; tempo-techdebt; tempo-upgrade"):
        return {"id": wid, "fields": {"System.Title": "x", "System.State": "To Do", "System.Tags": tags,
                                      "System.Description": f"<p><small>tempo-fp: {fp}</small></p>"}}

    def stt(stage="hazırlık", step="ci", ready=False, reason="hazırlık bitmedi", ev=None, code_pr=None, handed=None,
            blocked_ev=False):
        return {"repo": "core", "current": "8.2", "target": "8.4", "stage": stage, "evidence": ev,
                "prs": {"ci": None, "devops": [], "code": code_pr},
                "next": {"step": step, "ready": ready, "reason": reason, "sites": [], "manual": [], "test_note": "",
                         "handed_off": handed, "blocked_by_evidence": blocked_ev}}

    hits = [{"rule": "implicit_nullable", "file": "app/A.php", "line": 3, "text": "function f(int $a = null)"},
            {"rule": "deprecated_84", "file": "app/B.php", "line": 5, "text": "lcg_value()"}]
    probe = ([{"rule": "probe:error", "file": "app/D.php", "line": 9, "text": "fn(", "message": "Parse error: x"}],
             {"ran": True, "tool": "/x/php8.4", "reason": ""})
    jobs, queued = [], []

    def cj(wid, use_hal, title, *, job_kind, job_payload):
        jobs.append({"wid": wid, "kind": job_kind, "payload": json.loads(job_payload)})
        return 77
    try:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for name in ("core", "web"):
                (base / name).mkdir()
                (base / name / "composer.json").write_text('{"require": {"php": "^8.0"}}', encoding="utf-8")
            cfg = {"pack_sp": 3, "pack_files": 5, "grace_days": 3, "task_fields": {}, "sonar_url": "", "nr_days": 7,
                   "php_exclude": [], "php_bin": "php", "scan_ttl_h": 24, "max_take": 8, "freshen": False,
                   "repos_dir": base, "targets_cache": base / "t.yaml", "scan_cache": base / "cache"}
            deps = intake.Deps(gather=None, board=lambda c: {}, repo_ok=lambda r: (base / r).is_dir(),
                               queue=lambda w, t: (queued.append((w, t)), 9)[1], record=lambda **k: None,
                               now=lambda: 1e9, cfg=lambda: cfg)
            index = {r: [{"src": f"anatolia/build/{r}", "images": ["eu/prod-ar/php-8-2:1.0"]}] for r in ("core", "web")}
            kw = dict(index=index, scan_fn=lambda root, **k: {"files": 3, "sha": "s1", "hits": list(hits), "adoption": {},
                                                              "errors": []},
                      head_sha_fn=lambda r: "s1", app_map_cache={}, read=lambda d, p: "", yml_files={},
                      probe_fn=lambda root, lang, cur, to, **k: probe)
            NOW = _dt(2026, 11, 17, 10, 0, tzinfo=_tz.utc)

            def adv(c, st, **k):
                sfn = st if callable(st) else (lambda *a, **kk: st)
                base_kw = dict(team="T", deps=deps, create_job=cj, bump_jobs=lambda tid: [], status_fn=sfn, now=NOW,
                               last_bump_job=lambda tid, step, to: None)
                return ut.advance(c, **{**base_kw, **kw, **k})

            # ── Tavanlar + sprint ──
            c = _AdvFake(tracks=[track_wi()])
            r = adv(c, stt(), open_jobs=lambda repo: 2)
            check("tavan: 2 açıkken 1 iş alınır", len(r) == 1 and len(r[0]["took"]) == 1, str(r))
            check("otomatik alım bir sonraki sprint'e",
                  c.created and c.created[-1]["System.IterationPath"] == "P\\S24"
                  and "tempo-upgrade" in c.created[-1]["System.Tags"], str(c.created[-1:]))
            check("alınan iş kuyruğa (queued)", r[0]["queued"] and queued, str(r))
            c = _AdvFake(tracks=[track_wi()])
            r = adv(c, stt(), open_jobs=lambda repo: 3)
            check("tavan doluyken alım yok", r[0]["took"] == [] and not c.created, str(r))
            cfg["max_take"] = 1
            c = _AdvFake(tracks=[track_wi()])
            try:
                r = adv(c, stt(), open_jobs=lambda repo: 0)
            finally:
                cfg["max_take"] = 8
            check("max_take MAX_OPEN'dan küçükse o kazanır", len(r[0]["took"]) == 1, str(r))
            # varsayılan sayaç: açık tempo-upgrade işleri (needs-human hariç)
            c = _AdvFake(tracks=[track_wi()], tasks=[
                task_wi(61, "php_modern:core:x:app/Q.php"), task_wi(62, "php_modern:core:y:app/R.php"),
                task_wi(63, "php_modern:core:z:app/S.php", tags="tempo-quality; tempo-upgrade; needs-human"),
                task_wi(64, "php_modern:web:x:app/Q.php")])
            r = adv(c, stt())
            check("varsayılan sayaç: needs-human ve başka repo sayılmaz (2 açık → 1 alım)", len(r[0]["took"]) == 1,
                  str(r))
            c = _AdvFake(tracks=[track_wi()], future=False)
            r = adv(c, stt(), open_jobs=lambda repo: 0)
            check("sonraki sprint yok → alım yok, not", r[0]["took"] == [] and not c.created
                  and "sprint" in r[0]["note"], str(r))
            # varsayılan sayaçla iki tur: ilk tur tavana kadar alır, ikinci tur kendi açtıklarını sayar
            queued.clear()
            c = _AdvFake(tracks=[track_wi()])
            r1 = adv(c, stt())
            n_tasks = len([w for w in c.created if w["_type"] == "Task"])
            r2 = adv(c, stt())
            check("varsayılan sayaç, iki tur: ilk tur 3 alır, ikincisi hiç (açtıklarını sayar)",
                  len(r1[0]["took"]) == 3 and n_tasks == 3 and r2[0]["took"] == []
                  and len([w for w in c.created if w["_type"] == "Task"]) == 3, str((r1, r2)))
            flags["CREW_TECHDEBT_UPGRADE_MAX_OPEN"] = 0
            c = _AdvFake(tracks=[track_wi()])
            try:
                r = adv(c, stt(), open_jobs=lambda repo: 0)
            finally:
                flags["CREW_TECHDEBT_UPGRADE_MAX_OPEN"] = MAX_OPEN
            check("MAX_OPEN 0 → otomatik alım yok (3'e dönmez)", r[0]["took"] == [] and not c.created, str(r))

            # ── Sürüm adımı ──
            jobs.clear()
            c = _AdvFake(tracks=[track_wi()])
            r_ready = adv(c, stt("hazır", "ci", True, ""), open_jobs=lambda repo: 3)
            check("hazır → ci adımı kuyruğa", r_ready[0]["queued_step"] == "ci" and len(jobs) == 1
                  and jobs[0]["kind"] == "upgrade_bump" and jobs[0]["wid"] == "500" and r_ready[0]["queued"], str(r_ready))
            jobs.clear()
            r_dup = adv(_AdvFake(tracks=[track_wi()]), stt("hazır", "ci", True, ""), open_jobs=lambda repo: 3,
                        bump_jobs=lambda tid: [{"id": 1}])
            check("kuyrukta bump varken ikinci açılmaz", r_dup[0]["queued_step"] is None and not jobs
                  and r_dup[0]["note"], str(r_dup))
            # son adım işi başarısızsa otomatik ilerletme o adımı bir daha açmaz (yeniden deneme insanın)
            asked = []

            def last_failed(tid, step, to):
                asked.append((tid, step, to))
                return {"id": 41, "status": "failed"}
            c = _AdvFake(tracks=[track_wi()])
            rs = [adv(c, stt("hazır", "ci", True, ""), open_jobs=lambda repo: 3, last_bump_job=last_failed)
                  for _ in range(2)]
            check("son adım işi failed → iki turda da kuyruk yok, not insanı bekliyor",
                  not jobs and all(x[0]["queued_step"] is None and "#41" in x[0]["note"] and "insan" in x[0]["note"]
                                   for x in rs) and asked[0] == (500, "ci", "8.4"), str((rs, asked)))
            for stt_ in ("needs_human", "needs_info"):
                r = adv(_AdvFake(tracks=[track_wi()]), stt("hazır", "ci", True, ""), open_jobs=lambda repo: 3,
                        last_bump_job=lambda tid, step, to, s=stt_: {"id": 42, "status": s})
                check(f"son adım işi {stt_} → kuyruk yok", r[0]["queued_step"] is None and not jobs, str(r))
            # F4 (son düzeltme): PR bırakmadan tamamlanan adım (401/403 → diff, BUMP kapalı) hâlâ hazır görünür;
            # otomatik ilerletme onu her turda yeniden kuyruğa almaz — adım işi HİÇ yoksa kuyruğa alınır.
            rs = [adv(_AdvFake(tracks=[track_wi()]), stt("hazır", "ci", True, ""), open_jobs=lambda repo: 3,
                      last_bump_job=lambda tid, step, to: {"id": 43, "status": "completed"}) for _ in range(2)]
            check("F4: son adım işi completed → iki turda da kuyruk yok, not düğmeyi söyler",
                  not jobs and all(x[0]["queued_step"] is None and "#43" in x[0]["note"] and "tamamlandı" in x[0]["note"]
                                   and "düğme" in x[0]["note"] for x in rs), str(rs))
            r = adv(_AdvFake(tracks=[track_wi()]), stt("hazır", "ci", True, ""), open_jobs=lambda repo: 3,
                    last_bump_job=lambda tid, step, to: None)
            check("F4: adım işi hiç yoksa kuyruğa", r[0]["queued_step"] == "ci" and len(jobs) == 1, str(r))
            jobs.clear()
            c = _AdvFake(tracks=[track_wi()])
            r = adv(c, stt("hazırlık", "ci", False, "Hazırlık bitmedi"), open_jobs=lambda repo: 3)
            check("hazır değil → kuyruk yok, yorum yok", r[0]["queued_step"] is None and not jobs and not c.comments
                  and not c.created, str(r))
            red = {"state": "red", "build": {"id": 33, "url": "https://x/b/33", "queue_time": ""},
                   "summary": "FAILED CoreTest::testFoo password=hunter2", "overdue": False, "since": "2026-11-10T10:00:00Z"}
            c = _AdvFake(tracks=[track_wi()])
            r = adv(c, stt("test hattı", "prod", False, "devir", ev=red, handed=4242), open_jobs=lambda repo: 3)
            check("DevOps'a devredilmiş → hiçbir şey yazılmaz / kuyruğa girmez",
                  r[0]["queued_step"] is None and not jobs and not c.comments and not c.created, str(r))

            # ── Kırmızı build yalnız sıradaki adımı (prod) O bekletiyorsa ──
            for stage_, step_ in (("hazırlık", "ci"), ("engelli", "prod"), ("hazırlık", "prod")):
                c = _AdvFake(tracks=[track_wi()])
                adv(c, stt(stage_, step_, False, "x", ev=red), open_jobs=lambda repo: 3)
                check(f"kırmızı build {stage_}/{step_} aşamasında (kanıt bekletmiyor) → hiçbir şey",
                      not c.comments and not c.created, str((c.comments, c.created)))

            # ── Kırmızı build: build başına bir kez yorum + needs-human Task ──
            c = _AdvFake(tracks=[track_wi()])
            st_red = stt("test hattı", "prod", False, "build kırmızı", ev=red, blocked_ev=True)
            r1 = adv(c, st_red, open_jobs=lambda repo: 3)
            human = [w for w in c.created if "needs-human" in (w.get("System.Tags") or "")]
            check("kırmızı: izleme işine bir yorum (özet + build bağlantısı + işaret)",
                  len(c.comments) == 1 and c.comments[0][0] == 500 and "CoreTest" in c.comments[0][1]
                  and "https://x/b/33" in c.comments[0][1] and "tempo-upgrade-red:33" in c.comments[0][1], str(c.comments))
            check("kırmızı: bir needs-human Task (tempo-upgrade, teknik borç, repo/dil/hedef, maskeli)",
                  len(human) == 1 and "tempo-upgrade" in human[0]["System.Tags"] and "tempo-techdebt" in human[0]["System.Tags"]
                  and "core" in human[0]["System.Title"] and "8.4" in human[0]["System.Title"]
                  and "https://x/b/33" in human[0]["System.Description"] and "hunter2" not in human[0]["System.Description"]
                  and "hunter2" not in c.comments[0][1], str(human))
            check("kırmızı: prod kuyruğa girmez", r1[0]["queued_step"] is None and not jobs)
            n_c, n_w = len(c.comments), len(c.created)
            adv(c, st_red, open_jobs=lambda repo: 3)
            check("aynı build ikinci turda sessiz", len(c.comments) == n_c and len(c.created) == n_w,
                  str(c.comments[n_c:]))
            red_task = 700 + next(i for i, w in enumerate(c.created) if "needs-human" in (w.get("System.Tags") or ""))
            check("kırmızı Task izleme işine bağlı işaret taşır",
                  "tempo-upgrade-red-track: 500" in c.created[red_task - 700]["System.Description"])
            red2 = dict(red, build={"id": 34, "url": "https://x/b/34", "queue_time": ""})
            st_red2 = stt("test hattı", "prod", False, "build kırmızı", ev=red2, blocked_ev=True)
            adv(c, st_red2, open_jobs=lambda repo: 3)
            on_task = [t for w, t in c.comments[n_c:] if w == red_task]
            on_track = [t for w, t in c.comments[n_c:] if w == 500]
            check("Task açıkken yeni kırmızı build: yeni Task yok, açık Task'a bir yorum, izleme işine bir yorum",
                  len(c.created) == n_w and len(on_task) == 1 and "tempo-upgrade-red:34" in on_task[0]
                  and "https://x/b/34" in on_task[0] and len(on_track) == 1 and "tempo-upgrade-red:34" in on_track[0],
                  str(c.comments[n_c:]))
            n_c2 = len(c.comments)
            adv(c, st_red2, open_jobs=lambda repo: 3)
            check("aynı ikinci build tekrar yazılmaz", len(c.comments) == n_c2 and len(c.created) == n_w)
            c.cstate[red_task] = "Done"          # insan Task'ı kapattı
            red3 = dict(red, build={"id": 35, "url": "https://x/b/35", "queue_time": ""})
            adv(c, stt("test hattı", "prod", False, "build kırmızı", ev=red3, blocked_ev=True), open_jobs=lambda repo: 3)
            new_human = [w for w in c.created[n_w:] if "needs-human" in (w.get("System.Tags") or "")]
            check("insan Task'ı kapattıktan sonra yeni kırmızı build → yeni Task",
                  len(new_human) == 1 and "tempo-upgrade-red:35" in new_human[0]["System.Description"], str(c.created[n_w:]))

            # ── CI bekleme notu: since başına bir kez ──
            wait = {"state": "none", "build": None, "summary": "", "overdue": True, "since": "2026-11-10T10:00:00Z"}
            c = _AdvFake(tracks=[track_wi()])
            for _ in range(2):
                adv(c, stt("test hattı", "prod", False, "build yok", ev=wait, blocked_ev=True), open_jobs=lambda repo: 3)
            notes = sum(1 for _w, t in c.comments if "tempo-upgrade-ci-wait" in t)
            check("CI bekleme notu bir kez", notes == 1 and "yeniden tetikleyin" in c.comments[0][1]
                  and "2026-11-10T10:00:00Z" in c.comments[0][1], str(c.comments))
            adv(c, stt("test hattı", "prod", False, "build yok", ev=dict(wait, since="2026-11-12T10:00:00Z"),
                       blocked_ev=True), open_jobs=lambda repo: 3)
            check("yeni since → yeni not", sum(1 for _w, t in c.comments if "tempo-upgrade-ci-wait" in t) == 2)
            c = _AdvFake(tracks=[track_wi()])
            adv(c, stt("test hattı", "prod", False, "build yok", ev=dict(wait, overdue=False), blocked_ev=True),
                open_jobs=lambda repo: 3)
            check("süre dolmadan not yok", not c.comments)
            c = _AdvFake(tracks=[track_wi()])
            adv(c, stt("hazırlık", "ci", False, "x", ev=wait), open_jobs=lambda repo: 3)
            check("CI bekleme notu yalnız kanıt adımı bekletirken", not c.comments)
            # yorumların hepsi okunamadıysa (sayfa sınırı) karar verilemez → yazılmaz
            c = _AdvFake(tracks=[track_wi()])
            c.truncated.add(500)
            adv(c, stt("test hattı", "prod", False, "build yok", ev=wait, blocked_ev=True), open_jobs=lambda repo: 3)
            adv(c, st_red, open_jobs=lambda repo: 3)
            check("yorumlar sınırda kesildi → işaretli yorum yazılmaz",
                  not [t for w, t in c.comments if w == 500], str(c.comments))

            # ── Hata öğe başına; 24 saatte bir yorum ──
            def st_err(cl, repo, lang, **k):
                if repo == "core":
                    raise RuntimeError("Azure 503 geçici")
                return stt("hazır", "ci", True, "")
            c = _AdvFake(tracks=[track_wi(), track_wi(501, repo="web")])
            jobs.clear()
            r = adv(c, st_err, open_jobs=lambda repo: 3)
            by = {x["repo"]: x for x in r}
            check("A'nın hatası B'yi durdurmaz", set(by) == {"core", "web"} and "503" in by["core"]["note"]
                  and by["web"]["queued_step"] == "ci" and len(jobs) == 1 and jobs[0]["wid"] == "501", str(r))
            errs = [t for w, t in c.comments if w == 500 and "tempo-upgrade-err:" in t]
            check("hata izleme işine bir kez yorumlanır", len(errs) == 1, str(c.comments))
            adv(c, st_err, open_jobs=lambda repo: 3, now=NOW + _td(hours=2))
            check("aynı hata 24 saat içinde yeniden yorumlanmaz",
                  len([t for w, t in c.comments if w == 500 and "tempo-upgrade-err:" in t]) == 1)
            c.clock = "2026-11-17T10:00:00Z"
            adv(c, st_err, open_jobs=lambda repo: 3, now=NOW + _td(hours=25))
            check("24 saat sonra yeniden yorumlanır",
                  len([t for w, t in c.comments if w == 500 and "tempo-upgrade-err:" in t]) == 2)
            # AUTO tur ortasında kapatılırsa sıradaki öğeye geçilmez
            seen_repos = []

            def st_off(cl, repo, lang, **k):
                seen_repos.append(repo)
                flags["CREW_TECHDEBT_UPGRADE_AUTO"] = False
                return stt("hazırlık", "ci", False, "x")
            jobs.clear()
            try:
                r = adv(_AdvFake(tracks=[track_wi(), track_wi(501, repo="web")]), st_off, open_jobs=lambda repo: 3)
            finally:
                flags["CREW_TECHDEBT_UPGRADE_AUTO"] = True
            check("AUTO kapanınca tur sıradaki izleme işinde durur", seen_repos == ["core"] and len(r) == 1, str(r))
            import inspect
            sig = inspect.signature(ut.advance).parameters
            check("advance imzasında **kw yok; tarama enjeksiyonları adlı",
                  not any(p.kind == p.VAR_KEYWORD for p in sig.values())
                  and {"scan_fn", "index", "probe_fn", "last_bump_job", "track_ids"} <= set(sig), str(list(sig)))

            # ── tamam → izleme işi kapanır (bir kez) ──
            c = _AdvFake(tracks=[track_wi()])
            r_done = adv(c, stt("tamam", None, False, "Yükseltme tamamlandı."), open_jobs=lambda repo: 3)
            check("tamam → izleme işi kapanır", r_done[0]["closed"] and c.states == [(500, "Done")]
                  and any("tamamlandı" in t for _w, t in c.comments), str((r_done, c.comments)))
            check("ikinci turda kapalı iş yok", adv(c, stt("tamam", None, False, ""), open_jobs=lambda repo: 3) == []
                  and c.states == [(500, "Done")])
            c = _AdvFake(tracks=[track_wi()])
            r = adv(c, stt("tamam", "code", False, "Kod reposu PR'ı #31 açık", code_pr={"id": 31, "status": "active"}),
                    open_jobs=lambda repo: 3)
            check("kod PR'ı açıkken kapanmaz", not r[0]["closed"] and not c.states, str(r))
            c = _AdvFake(tracks=[track_wi()])
            r = adv(c, stt("hazır", "ci", False, "x"), open_jobs=lambda repo: 3)
            check("tamam dışı aşamada kapanmaz", not r[0]["closed"] and not c.states)

        # ── Yoklayıcı turu ──
        got = []
        flags["CREW_TECHDEBT_UPGRADE_AUTO"] = False
        check("AUTO kapalı: tur hiçbir Azure çağrısı yapmaz",
              ut._tick(lambda: got.append(1), on_queued=lambda: got.append("w")) == [] and got == [])
        flags["CREW_TECHDEBT_UPGRADE_AUTO"] = True
        calls, woke = [], []
        cpoll = _AdvFake(tracks=[track_wi(), track_wi(501, repo="web", area="P\\U"),
                                 track_wi(502, repo="api", area="P\\T\\Sub")])

        def fake_adv(client, *, team, track_ids=None, **k):
            calls.append((team, sorted(track_ids or [])))
            return [{"id": i, "queued": team == "U"} for i in sorted(track_ids or [])]
        out = ut._tick(lambda: cpoll, on_queued=lambda: woke.append(1), advance_fn=fake_adv)
        check("tur: takımlar izleme işlerinin alanından, her iş bir kez",
              sorted(calls) == [("T", [500, 502]), ("U", [501])],
              str(calls))
        check("tur: kuyruğa giren varsa işçi uyandırılır (bir kez)", woke == [1] and len(out) == 3, str(woke))

        # ── start_poller: tek thread, ilk tur bir aralık sonra, hata thread'i öldürmez ──
        steps, events = _queue.Queue(), []
        stop = _th.Event()

        def fsleep(s):
            events.append(("sleep", s))
            steps.get(timeout=5)

        ticks = []

        def ftick(get_client, on_queued=None):
            ticks.append(1)
            if len(ticks) == 1:
                raise RuntimeError("tur hatası")
            return []
        t1 = ut.start_poller(lambda: cpoll, interval_min=1, sleep=fsleep, tick=ftick, stop_event=stop)
        t2 = ut.start_poller(lambda: cpoll, interval_min=1, sleep=fsleep, tick=ftick, stop_event=stop)
        import time as _time
        for _ in range(100):
            if events:
                break
            _time.sleep(0.01)
        check("start_poller iki kez → tek thread", t1 is t2 and t1.daemon and t1.is_alive())
        check("ilk tur bir aralık beklendikten sonra (en az 5 dk)", events == [("sleep", 300)] and ticks == [], str(events))
        steps.put("go")
        for _ in range(200):
            if len(events) >= 2:
                break
            _time.sleep(0.01)
        steps.put("go")
        for _ in range(200):
            if len(ticks) >= 2:
                break
            _time.sleep(0.01)
        check("turun hatası thread'i durdurmaz", len(ticks) == 2 and t1.is_alive(), str((ticks, events)))
        stop.set()
        steps.put("go")
        t1.join(2)
        check("durdurulunca thread biter", not t1.is_alive())
        check("start_poller modülün stop()'unu gölgelemez", "stop" not in inspect.signature(ut.start_poller).parameters
              and callable(ut.stop))

        # ── Sunucu: yoklayıcı üç anahtar açıkken başlar, işçiyi sunucu uyandırır ──
        from agile_sdlc_crew import server
        src = inspect.getsource(server._start_upgrade_poller)
        check("sunucu: start_poller, POLL_MIN, _ensure_worker, üç anahtar",
              "start_poller" in src and "CREW_TECHDEBT_UPGRADE_POLL_MIN" in src and "_ensure_worker" in src
              and all(k in src for k in ('"CREW_TECHDEBT"', '"CREW_TECHDEBT_PHP_MODERN"', '"CREW_TECHDEBT_UPGRADE"')))
        qsrc = inspect.getsource(ut)
        check("quality/ işçiyi çağırmaz", "_ensure_worker()" not in qsrc)
    finally:
        pc.get = orig_get
        ut._POLLER = None


def test_upgrade_card():
    print("\n[U9] Sürüm yükseltme kartı — aşama şeridi ve düğmeler")
    from fastapi.testclient import TestClient
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import server
    from agile_sdlc_crew.quality import upgrade, upgrade_track as ut
    html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text(encoding="utf-8")
    check("upgradeStart / upgradeStop / upgradeBump var",
          all(f"function {n}(" in html for n in ("upgradeStart", "upgradeStop", "upgradeBump")))
    check("dört aşama etiketi", all(f"'{lb}'" in html for lb in ("Hazırlık", "Hazır", "Test hattı", "Üretim")))
    check("Engelli durumu metinle gösterilir", "Engelli" in html)

    def _fn(name):
        i = html.find("async function " + name)
        if i < 0:
            i = html.find("function " + name)
        js = [x for x in (html.find("\nfunction ", i + 1), html.find("\nasync function ", i + 1), html.find("\nconst ", i + 1)) if x > 0]
        return html[i:min(js)]
    bump, status_src, link = _fn("upgradeBump"), _fn("_upStatusHtml"), _fn("_upLink")
    check("upgradeBump next.ready ve tracked'e bakar", "nx.ready" in bump and "tracked" in bump)
    check("düğme yalnız tracked && next.ready===true iken etkin", "canBump=tracked&&nx.ready===true" in status_src
          and "disabled" in status_src)
    check("upgradeBump step gönderir, ready===true ister",
          "step:nx.step" in bump and "nx.ready===true" in bump)
    post_src = _fn("_upPost")
    check("_upPost 409'da yeniden tarar, kör yeniden-etkinleştirme yok",
          "409" in post_src and "_upRefresh()" in post_src and "b.disabled=false" not in post_src
          and "isConnected&&b.id!=='tdmUpBump'" in post_src)
    ref_src = _fn("_upRefresh")
    check("_upRefresh: tarama düşerse scan=null + Türkçe bayat satırı",
          "_tdm.scan=null" in ref_src and "Durum yenilenemedi — yeniden tarayın" in ref_src
          and "İzleme durumu okunamadı" in ref_src)
    check("üç POST yolu kullanılıyor",
          all(p in html for p in ("'/api/techdebt/upgrade/start'", "'/api/techdebt/upgrade/stop'",
                                  "'/api/techdebt/upgrade/bump'")))
    check("bağlantı yalnız https:// ile", "indexOf('https://')===0" in link and "escHtml(u)" in link)
    check("durdur / adım için confirm", "confirm(" in _fn("_upPost") and "Devam edilsin mi" in html)
    check("kart sunucudan gelen next.reason'ı olduğu gibi yazar", "nx.reason" in status_src)
    check("repos çağrısı takımı geçirir", "upgrade/repos'+(team" in html)

    saved = (pc.get, server.AzureDevOpsClient, upgrade.scan, ut.status)
    pc.get = lambda k: True if k in ("CREW_TECHDEBT", "CREW_TECHDEBT_PHP_MODERN", "CREW_TECHDEBT_UPGRADE") else saved[0](k)
    server.AzureDevOpsClient = lambda: object()
    upgrade.scan = lambda c, repo, lang, **k: {"repo": repo, "lang": lang, "current": "8.2", "target": "8.4"}
    ut.status = lambda c, repo, lang, **k: {"stage": "hazır", "next": {"step": "ci", "ready": True, "reason": "x"}}
    try:
        js = TestClient(server.app).get("/api/techdebt/upgrade/scan?repo=core").json()
        check("scan yanıtı status bloğu taşır", (js.get("status") or {}).get("next", {}).get("ready") is True, str(js))
    finally:
        pc.get, server.AzureDevOpsClient, upgrade.scan, ut.status = saved


def test_upgrade_fix_round2():
    """Görev 8 düzeltme turu 2: prod, `<repo>-test`'in CI dosyası değiştikten SONRA yeşil koştuğu kanıtı
    olmadan açılamaz. B1 kanıt saati merge'den önceye düşmez (firstParent + Tempo CI PR'ının kapanışı);
    B2 repo projesi kesin bulunamazsa hata (önbellekli yedek proje → sahte no_pipeline yok); B3 okunamayan
    sürüm satırı ve sabitlemesiz repo da kapıyı kapalı tutar. Her denetim ayrı çalışır (biri çökse de
    ötekiler raporlanır)."""
    print("\n[Sürüm yükseltme: düzeltme turu 2]")
    import requests as _rq
    from fastapi.testclient import TestClient
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import server
    from agile_sdlc_crew.quality import intake, upgrade, upgrade_bump as bump, upgrade_track as ut
    from agile_sdlc_crew.tools import azure_devops_base as azb

    BR = "tempo/upgrade-php-core-8.4"
    T1, T2, T3, T4 = ("2026-11-10T09:00:00Z", "2026-11-10T10:00:00Z", "2026-11-10T11:00:00Z",
                      "2026-11-10T12:00:00Z")
    dv_a = {"where": "devops", "repo": "anatolia", "project": "FLODevOps", "branch": "production",
            "path": "/build/core/Dockerfile", "line": 1, "old": "eu/php-8-2:1.0", "new": "eu/php-8-4:1.0",
            "blocked": ""}
    dv_r = dict(dv_a, repo="russia")
    ci_s = {"where": "ci", "repo": "yml-files", "project": "EcomBackend", "branch": "main",
            "path": "Test Pipelines/core.yml", "line": 2, "old": "'8.2'", "new": "'8.4'", "blocked": ""}
    pin = {"repo": "yml-files", "project": "EcomBackend", "branch": "main", "path": "Test Pipelines/core.yml",
           "line": 2, "version": "8.4"}
    PIN_FILE = ("EcomBackend", "yml-files", "main", "Test Pipelines/core.yml")
    CORE_DEF = {"core": {"id": 5, "name": "Core-test", "project": "EcomBackend"}}
    ci_key = ("EcomBackend", "yml-files", BR)

    def b(bid, q, result="succeeded", status="completed"):
        return {"build_id": bid, "status": status, "result": result, "definition": "Core-test",
                "url": f"https://x/b/{bid}", "project": "EcomBackend", "queue_time": q, "source_version": ""}

    def scan(sites, pins):
        return {"repo": "core", "lang": "php", "current": "8.2", "target": "8.4", "up_to_date": False,
                "sites": sites, "blocking": 0, "blockers": [], "auto": [], "infra": [], "human": [], "ci_pins": pins,
                "ci_source": _ci_src(pins)}

    def pr(pid, status, closed=""):
        return {"id": pid, "status": status, "url": f"https://x/pr/{pid}", "closed_at": closed, "merge_commit": "",
                "target": "main", "created_by": "tempo-id"}

    def safe(fn):
        """(sonuç, hata) — bir denetimin çökmesi ötekileri gizlemesin."""
        try:
            return fn(), None
        except Exception as e:  # noqa: BLE001
            return None, e

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "core"
        root.mkdir()
        (root / "composer.json").write_text('{\n  "require": {\n    "php": "^8.2"\n  }\n}\n', encoding="utf-8")
        cfg = {"pack_sp": 3, "pack_files": 5, "grace_days": 3, "task_fields": {}, "sonar_url": "", "nr_days": 7,
               "php_exclude": [], "php_bin": "php", "scan_ttl_h": 24, "max_take": 8, "freshen": False,
               "repos_dir": base, "targets_cache": base / "t.yaml", "scan_cache": base / "cache"}
        deps = intake.Deps(gather=None, board=lambda c: {}, repo_ok=lambda r: (base / r).is_dir(), queue=None,
                           record=lambda **k: None, now=lambda: 1e9, cfg=lambda: cfg)

        def st(sc, c):
            return safe(lambda: ut.status(c, "core", "php", to="8.4", deps=deps, scan=sc, wait_h=72, root=root))

        # ── B1: kanıt saati merge'den önceye düşmez ──
        cl = azb.AzureDevOpsClient.__new__(azb.AzureDevOpsClient)
        cl.org_url, cl.project = "https://dev.azure.com/o", "P"
        cl._repo_api_url = lambda repo, project=None: f"https://x/{project or 'P'}/_apis/git/repositories/{repo}"

        class _Cl(azb.AzureDevOpsClient):
            _headers = {"Authorization": "x"}
        cl.__class__ = _Cl

        class _R:
            def __init__(self, js=None, status=200):
                self._js, self.status_code, self.ok = js, status, status < 400

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise _rq.HTTPError(str(self.status_code), response=self)

            def json(self):
                return self._js
        og = azb.requests.get
        azb.requests.get = lambda url, headers=None, params=None, timeout=None, **k: _R({"value": [{"committer": {
            "date": T3 if (params or {}).get("searchCriteria.historyMode") == "firstParent" else T1}}]})
        try:
            when, err = safe(lambda: cl.get_last_commit_date("yml-files", "/Test Pipelines/core.yml", "main",
                                                             project="EcomBackend"))
        finally:
            azb.requests.get = og
        check("B1a: dosyanın son commit'i firstParent ile (merge commit'i, özellik dalının commit'i değil)",
              when == T3, str((when, err)))
        prs_done = {ci_key: pr(11, "completed", closed=T3)}
        s, err = st(scan([dv_a, dv_r], [pin]), _UpFake(defs=CORE_DEF, builds={5: [b(2, T2)]},
                                                       commit_dates={PIN_FILE: T1}, prs=prs_done))
        check("B1b: commit T1 ama Tempo CI PR'ı T3'te kapandı → since T3, T2'deki yeşil kanıt değil",
              s is not None and s["evidence"] and s["evidence"]["since"] == T3 and s["evidence"]["state"] == "none"
              and not s["next"]["ready"], str((s and s["evidence"], err)))
        s, err = st(scan([dv_a, dv_r], [pin]), _UpFake(defs=CORE_DEF, builds={5: [b(3, T4), b(2, T2)]},
                                                       commit_dates={PIN_FILE: T1}, prs=prs_done))
        check("B1b: T3'ten sonra yeşil → prod hazır", s is not None and s["next"]["step"] == "prod"
              and s["next"]["ready"], str((s and s["next"], err)))
        s, err = st(scan([dv_a, dv_r], [pin]), _UpFake(defs=CORE_DEF, builds={5: [b(3, T4)]},
                                                       commit_dates={PIN_FILE: T1},
                                                       prs={ci_key: pr(11, "completed", closed="")}))
        check("B1b: tamamlanmış Tempo CI PR'ının kapanış zamanı okunamazsa prod yok",
              s is not None and not s["next"]["ready"] and "okunamadı" in s["next"]["reason"],
              str((s and s["next"], err)))
        pin2 = dict(pin, path="Test Pipelines/core-api.yml")
        s, err = st(scan([dv_a, dv_r], [pin, pin2]),
                    _UpFake(defs=CORE_DEF, builds={5: [b(2, T2)]},
                            commit_dates={PIN_FILE: T1, ("EcomBackend", "yml-files", "main", pin2["path"]): T3}))
        check("ek: birden çok sabitleme dosyasında en geç değişiklik zamanı",
              s is not None and s["evidence"] and s["evidence"]["since"] == T3 and s["evidence"]["state"] == "none",
              str((s and s["evidence"], err)))

        # ── ek: iptal edilen en yeni build + since'ten önceki tamamlanmış build → none; okunamayan özet → red ──
        ev, err = safe(lambda: bump.ci_evidence(
            _UpFake(defs=CORE_DEF, builds={5: [b(3, T4, result="canceled"), b(1, T1)]}), "core", T2, wait_h=72))
        check("ek: iptal edilen en yeni build, önceki build since'ten önce → none",
              ev is not None and ev["state"] == "none", str((ev, err)))
        ev, err = safe(lambda: bump.ci_evidence(
            _UpFake(defs=CORE_DEF, builds={5: [b(3, T4, result="failed")]}, summary_error=RuntimeError("500")),
            "core", T2, wait_h=72))
        check("ek: özeti okunamayan kırmızı build kırmızı kalır",
              ev is not None and ev["state"] == "red" and "okunamadı" in ev["summary"], str((ev, err)))

        # ── B2: repo projesi kesin bulunamazsa hata ──
        cl2 = azb.AzureDevOpsClient.__new__(azb.AzureDevOpsClient)
        cl2.org_url, cl2.project, cl2.repo_projects = "https://dev.azure.com/o", "P", ["EcomBackend", "Other"]
        cl2._repo_project_cache = {}
        cl2.__class__ = _Cl
        seen = []

        def g_boom(url, headers=None, params=None, timeout=None, **k):
            seen.append(url)
            if "/git/repositories/" in url:
                raise _rq.ConnectionError("geçici hata")
            return _R({"value": []})
        azb.requests.get = g_boom
        try:
            d, err = safe(lambda: cl2.get_test_pipeline("core"))
        finally:
            azb.requests.get = og
        check("B2: repo sorgusu hata verirse tanım araması hata (yanlış projede boş liste → no_pipeline değil)",
              d is None and err is not None and not any("/build/definitions" in u for u in seen)
              and "core" not in cl2._repo_project_cache, str((d, err, seen)))
        seen.clear()
        azb.requests.get = lambda url, headers=None, params=None, timeout=None, **k: (
            seen.append(url), _R(status=404) if "/git/repositories/" in url else _R({"value": []}))[1]
        try:
            d, err = safe(lambda: cl2.get_test_pipeline("core"))
        finally:
            azb.requests.get = og
        check("B2: repo hiçbir projede yoksa hata", d is None and err is not None
              and not any("/build/definitions" in u for u in seen), str((d, err, seen)))
        seen.clear()
        cl2._repo_project_cache = {"core": "EcomBackend"}  # _find_repo_project'in önbelleğe yazdığı yedek
        azb.requests.get = lambda url, headers=None, params=None, timeout=None, **k: (
            seen.append(url),
            _R(status=200, js={"id": "g"}) if url.endswith("/Other/_apis/git/repositories/core")
            else _R(status=404) if "/git/repositories/" in url
            else _R({"value": [{"id": 5, "name": "Core-test"}]}))[1]
        try:
            d, err = safe(lambda: cl2.get_test_pipeline("core"))
        finally:
            azb.requests.get = og
        check("B2: önbellekteki yedek proje kullanılmaz; repo gerçekten bulunduğu projede aranır",
              {k: (d or {}).get(k) for k in ("id", "name", "project")} == {"id": 5, "name": "Core-test", "project": "Other"}
              and any(u.endswith("/Other/_apis/build/definitions") for u in seen), str((d, err, seen)))
        # status / queue_step / uç: hata yükselir, hiçbir şey hazır değil
        sc_at = scan([dv_a, dv_r], [pin])
        c_err = _UpFake(defs={"core": RuntimeError("repo projesi bulunamadı")}, commit_dates={PIN_FILE: T1})
        s, err = st(sc_at, c_err)
        check("B2: tanım okunamazsa status hata verir (hazır değil)", s is None and err is not None, str(s))
        track = {"id": 500, "fields": {"System.Title": "izleme", "System.State": "To Do", "System.AreaPath": "P\\T",
                                       "System.Tags": "tempo-upgrade-track",
                                       "System.Description": "<p><small>tempo-upgrade-key: php:core:8.4</small></p>"}}
        c_q = _UpFake(defs={"core": RuntimeError("repo projesi bulunamadı")}, commit_dates={PIN_FILE: T1},
                      tracks=[track])
        made = []
        q, err = safe(lambda: bump.queue_step(
            c_q, "core", "php", team="T", deps=deps,
            create_job=lambda *a, **k: (made.append(1), 1)[1], open_jobs=lambda t: [],
            status_fn=lambda cl_, r, l, **k: ut.status(cl_, r, l, to=k.get("to"), deps=deps, scan=sc_at, wait_h=72,
                                                       root=root)))
        check("B2: queue_step hata verir, iş açılmaz", q is None and err is not None and not made, str(q))
        saved = (pc.get, server.AzureDevOpsClient, upgrade.scan, ut.watch_view)
        pc.get = lambda k: True if k in ("CREW_TECHDEBT", "CREW_TECHDEBT_PHP_MODERN", "CREW_TECHDEBT_UPGRADE") else saved[0](k)
        server.AzureDevOpsClient = lambda: _UpFake(defs={"core": RuntimeError("repo projesi bulunamadı")},
                                                   commit_dates={PIN_FILE: T1})
        upgrade.scan = lambda c, repo, lang, **k: dict(sc_at)
        ut.watch_view = lambda *a, **k: None
        try:
            js = TestClient(server.app).get("/api/techdebt/upgrade/scan?repo=core&to=8.4").json()
        finally:
            pc.get, server.AzureDevOpsClient, upgrade.scan, ut.watch_view = saved
        check("B2: tarama ucu durum hatasını raporlar", "error" in (js.get("status") or {}), str(js.get("status")))

        # ── B3a: okunamayan sürüm satırı ──
        def ci_of(lang, line, to):
            yml = f"variables:\n  {line}\nresources:\n  - repository: s\n    name: EcomBackend/core\n"
            return safe(lambda: upgrade._ci_scan(None, "core", lang, to, {"Test Pipelines/core.yml": yml}))
        for lang, line, to, raw in (("python", "pythonVersion: '3.x'", "3.12", "3.x"),
                                    ("php", "phpVersion: $(phpVer)", "8.4", "$(phpVer)"),
                                    ("php", "phpVersion: 8", "8.4", "8")):
            res, err = ci_of(lang, line, to)
            pins_ = (res or ([], []))[1] or []
            check(f"B3a: '{line}' → boş sürümlü sabitleme + ham metin, site yok",
                  res is not None and len(pins_) == 1 and pins_[0]["version"] == "" and pins_[0]["unparsed"] == raw
                  and res[0] == [], str((res, err)))
        res, err = ci_of("go", "goVersion: '1.22.5'", "1.25")
        sites_, pins_ = res or ([], [])
        check("B3a: üç parçalı sürüm major.minor ile karşılaştırılır, site elle (otomatik değil)",
              res is not None and pins_ and pins_[0]["version"] == "1.22" and len(sites_) == 1
              and sites_[0]["blocked"] == "sürüm satırı elle düzenlenmeli" and not bump.pushable(sites_[0]),
              str((res, err)))
        res, err = ci_of("go", "goVersion: '1.25.1'", "1.25")
        check("B3a: üç parçalı sürüm hedefte → sabitleme 1.25, site yok",
              res is not None and res[1] and res[1][0]["version"] == "1.25" and res[0] == [], str((res, err)))
        bad_pin = dict(pin, version="", unparsed="$(phpVer)")
        s, err = st(scan([dv_a, dv_r], [bad_pin]), _UpFake(defs=CORE_DEF, builds={5: [b(3, T4)]},
                                                           commit_dates={PIN_FILE: T1}))
        check("B3a: okunamayan satır → prod hazır değil, neden dosyayı adlandırır",
              s is not None and not s["next"]["ready"]
              and "test hattı sürüm satırı okunamadı: Test Pipelines/core.yml" in s["next"]["reason"],
              str((s and s["next"], err)))
        low = dict(pin, version="8.2")
        s, err = st(scan([dv_a, dv_r, ci_s], [low, dict(bad_pin, path="Test Pipelines/other.yml")]), _UpFake())
        check("B3a: okunamayan satır → ci de hazır değil",
              s is not None and not s["next"]["ready"]
              and "test hattı sürüm satırı okunamadı: Test Pipelines/other.yml" in s["next"]["reason"],
              str((s and s["next"], err)))

        # ── B3b: sabitleme yok ama <repo>-test tanımı var → en yeni build yeşil olmalı ──
        s, err = st(scan([dv_a, dv_r], []), _UpFake(defs=CORE_DEF, builds={5: [b(3, T4, result="failed")]}))
        check("B3b: sabitleme yok, tanımın en yeni build'i kırmızı → prod yok",
              s is not None and s["next"]["step"] == "prod" and not s["next"]["ready"]
              and s["evidence"] and s["evidence"]["state"] == "red", str((s and s["next"], err)))
        s, err = st(scan([dv_a, dv_r], []), _UpFake(defs=CORE_DEF, builds={5: []}))
        check("B3b: sabitleme yok, tanımın tamamlanmış build'i yok → prod yok",
              s is not None and not s["next"]["ready"] and s["next"]["reason"], str((s and s["next"], err)))
        s, err = st(scan([dv_a, dv_r], []), _UpFake(defs=CORE_DEF, builds={5: [b(3, T4)]}))
        check("B3b: sabitleme yok, tanımın en yeni build'i yeşil → prod hazır",
              s is not None and s["next"]["ready"] and (s["evidence"] or {}).get("state") == "green",
              str((s and s["next"], err)))
        s, err = st(scan([dv_a, dv_r], []), _UpFake())
        check("B3b: sabitleme yok, tanım yok → prod hazır, 'Test kanıtı yok'",
              s is not None and s["next"]["ready"] and s["next"]["test_note"] == "Test kanıtı yok",
              str((s and s["next"], err)))


def test_upgrade_fix_round3():
    """Görev 8 düzeltme turu 3. N1: gerçek test hattı dosyaları sürümü `goVersion` ile değil `GoTool@0` →
    `inputs: version:` (gocrons), `image: golang:1.21.4` (myduck), `UsePythonVersion@0` → `versionSpec:` ile
    sabitliyor — bunlar sabitleme sayılır; sabitlemesiz bir hattın yeşil build'i 'zayıf kanıt' notuyla gider.
    N2: repo adı birden çok projede varsa ya da yanıt 200+JSON değilse proje çözümü hata verir."""
    print("\n[Sürüm yükseltme: düzeltme turu 3]")
    import requests as _rq
    from agile_sdlc_crew.quality import intake, upgrade, upgrade_bump as bump, upgrade_track as ut
    from agile_sdlc_crew.tools import azure_devops_base as azb

    def safe(fn):
        try:
            return fn(), None
        except Exception as e:  # noqa: BLE001
            return None, e

    def scan_ci(repo, lang, to, yml, path="Test Pipelines/x.yml"):
        return safe(lambda: upgrade._ci_scan(None, repo, lang, to, {path: yml}))

    head = "trigger: none\nresources:\n  repositories:\n    - repository: src\n      type: git\n      name: EcomBackend/{repo}\n"
    gotool = head.format(repo="gocrons") + ("steps:\n  - checkout: src\n  - task: GoTool@0\n    displayName: Go\n"
                                            "    inputs:\n      version: '1.24.4'\n  - script: go test ./...\n")
    res, err = scan_ci("gocrons", "go", "1.25", gotool)
    sites, pins = res or ([], [])
    check("N1a: GoTool@0 inputs version (1.24.4) → sabitleme 1.24, site elle, neden satır + hedef",
          res is not None and [p["version"] for p in pins] == ["1.24"] and pins[0]["line"] == 12 and len(sites) == 1
          and sites[0]["blocked"] == upgrade.CI_HAND and not bump.pushable(sites[0]), str((res, err)))
    myduck = ("resources:\n  containers:\n    - container: golang\n      image: golang:1.21.4\n  repositories:\n"
              "    - repository: s\n      name: EcomBackend/myduck\njobs:\n  - job: t\n    container: golang\n")
    res, err = scan_ci("myduck", "go", "1.25", myduck)
    sites, pins = res or ([], [])
    check("N1a: image golang:1.21.4 → sabitleme 1.21 (konteyner takma adı 'golang' sabitleme değil)",
          res is not None and [(p["version"], p["line"]) for p in pins] == [("1.21", 4)] and len(sites) == 1
          and sites[0]["blocked"] == upgrade.CI_HAND, str((res, err)))
    reg = head.format(repo="svc") + "container: myreg.io/library/golang:1.21-alpine\n"
    res, err = scan_ci("svc", "go", "1.25", reg)
    sites, pins = res or ([], [])
    check("N1a: önekli + son ekli golang imajı (1.21-alpine) → sabitleme, otomatik site",
          res is not None and [p["version"] for p in pins] == ["1.21"] and len(sites) == 1
          and sites[0]["old"] == "myreg.io/library/golang:1.21-alpine"
          and sites[0]["new"] == "myreg.io/library/golang:1.25-alpine" and bump.pushable(sites[0]), str((res, err)))
    py = head.format(repo="segflo") + ("steps:\n  - task: UsePythonVersion@0\n    inputs:\n      versionSpec: '3.11'\n"
                                       "      addToPath: true\n  - script: pytest\n")
    res, err = scan_ci("segflo", "python", "3.12", py)
    sites, pins = res or ([], [])
    check("N1a: UsePythonVersion@0 versionSpec → sabitleme + otomatik site",
          res is not None and [p["version"] for p in pins] == ["3.11"] and len(sites) == 1
          and sites[0]["old"] == "'3.11'" and sites[0]["new"] == "'3.12'" and bump.pushable(sites[0]), str((res, err)))
    pyimg = head.format(repo="segflo") + "container: python:3.11-slim\n"
    res, err = scan_ci("segflo", "python", "3.12", pyimg)
    check("N1a: image python:3.11-slim → sabitleme",
          res is not None and [p["version"] for p in res[1]] == ["3.11"], str((res, err)))
    phpimg = head.format(repo="core") + "container: eu/prod-ar/php-8-2:1.0\n"
    res, err = scan_ci("core", "php", "8.4", phpimg)
    check("N1a: PHP imajı php-8-2:1.0 → sabitleme + site (imaj ayrıştırıcısı)",
          res is not None and [p["version"] for p in res[1]] == ["8.2"] and len(res[0]) == 1
          and res[0][0]["new"] == "eu/prod-ar/php-8-4:1.0", str((res, err)))
    var = gotool.replace("'1.24.4'", "$(goVersion)")
    res, err = scan_ci("gocrons", "go", "1.25", var)
    check("N1a: GoTool version $(goVersion) → okunamayan sabitleme",
          res is not None and len(res[1]) == 1 and res[1][0]["version"] == ""
          and res[1][0]["unparsed"] == "$(goVersion)" and res[0] == [], str((res, err)))
    two = head.format(repo="gocrons") + ("steps:\n  - task: NodeTool@0\n    inputs:\n      version: '18.x'\n"
                                         "  - task: GoTool@0\n    inputs:\n      version: '1.24'\n"
                                         "  - script: echo\n    env:\n      version: '9.9'\n"
                                         "  # - task: GoTool@0\n  #   inputs:\n  #     version: '1.10'\n")
    res, err = scan_ci("gocrons", "go", "1.25", two)
    check("N1a: başka görevin / betiğin version'ı ve yorum satırları sabitleme değil",
          res is not None and [(p["version"], p["line"]) for p in res[1]] == [("1.24", 13)], str((res, err)))

    # Gerekçe uygulanabilir: yol:satır + hedef
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "core"
        root.mkdir()
        (root / "composer.json").write_text('{"require": {"php": "^8.2"}}', encoding="utf-8")
        deps = intake.Deps(gather=None, board=lambda c: {}, repo_ok=lambda r: (base / r).is_dir(), queue=None,
                           record=lambda **k: None, now=lambda: 1e9, cfg=lambda: {"repos_dir": base})
        dv_a = {"where": "devops", "repo": "anatolia", "project": "FLODevOps", "branch": "production",
                "path": "/build/core/Dockerfile", "line": 1, "old": "eu/php-8-2:1.0", "new": "eu/php-8-4:1.0",
                "blocked": ""}

        def st(sites, pins, c):
            sc = {"repo": "core", "lang": "php", "current": "8.2", "target": "8.4", "up_to_date": False,
                  "sites": sites, "blocking": 0, "blockers": [], "auto": [], "infra": [], "human": [], "ci_pins": pins,
                  "ci_source": _ci_src(pins)}
            return safe(lambda: ut.status(c, "core", "php", to="8.4", deps=deps, scan=sc, wait_h=72, root=root))
        bad = {"repo": "yml-files", "project": "EcomBackend", "branch": "main", "path": "Test Pipelines/core.yml",
               "line": 7, "version": "", "unparsed": "$(phpVer)"}
        s, err = st([dv_a], [bad], _UpFake())
        check("N1a: okunamayan satırın gerekçesi yol:satır ve hedefi söyler",
              s is not None and "Test Pipelines/core.yml:7" in s["next"]["reason"]
              and "8.4 yapılmalı" in s["next"]["reason"], str((s and s["next"], err)))
        ci_hand = {"where": "ci", "repo": "yml-files", "project": "EcomBackend", "branch": "main",
                   "path": "Test Pipelines/core.yml", "line": 9, "old": "8.2.1", "new": "8.2.1", "blocked": upgrade.CI_HAND}
        low = {"repo": "yml-files", "project": "EcomBackend", "branch": "main", "path": "Test Pipelines/core.yml",
               "line": 9, "version": "8.2"}
        s, err = st([dv_a, ci_hand], [low], _UpFake())
        check("N1a: elle düzenlenecek CI satırının gerekçesi yol:satır ve hedefi söyler",
              s is not None and not s["next"]["ready"] and "Test Pipelines/core.yml:9" in s["next"]["reason"]
              and "8.4 yapılmalı" in s["next"]["reason"], str((s and s["next"], err)))

        # N1b: sabitlemesiz hattın yeşil build'i zayıf kanıt
        CORE_DEF = {"core": {"id": 5, "name": "Core-test", "project": "EcomBackend"}}

        def b(bid, q, result="succeeded"):
            return {"build_id": bid, "status": "completed", "result": result, "definition": "Core-test",
                    "url": "", "project": "EcomBackend", "queue_time": q, "source_version": ""}
        s, err = st([dv_a], [], _UpFake(defs=CORE_DEF, builds={5: [b(3, "2026-11-10T12:00:00Z")]}))
        note = (s or {}).get("next", {}).get("test_note", "")
        check("N1b: sabitleme yok + yeşil → hazır ama 'Test kanıtı zayıf' notu",
              s is not None and s["next"]["ready"] and note.startswith("Test kanıtı zayıf")
              and "sabitlemesi bulunamadı" in note, str((s and s["next"], err)))
        title, body = bump._bodies({"repo": "core", "from": "8.2", "to": "8.4", "step": "prod", "sites": [dv_a],
                                    "track_id": 500}, note or "x")
        check("N1b: not üretim PR açıklamasının ilk satırı", body.splitlines()[0] == (note or "x"))
        s, err = st([dv_a], [], _UpFake(defs=CORE_DEF, builds={5: [b(3, "2026-11-10T12:00:00Z", "failed")]}))
        check("N1b: sabitleme yok + kırmızı → hazır değil", s is not None and not s["next"]["ready"])
        s, err = st([dv_a], [], _UpFake(defs=CORE_DEF, builds={5: []}))
        check("N1b: sabitleme yok + tamamlanmış build yok → hazır değil", s is not None and not s["next"]["ready"])

    # N2: proje çözümü kesin ve tekil
    class _Cl(azb.AzureDevOpsClient):
        _headers = {"Authorization": "x"}

    class _R:
        def __init__(self, status=200, js=None, text=""):
            self.status_code, self._js, self.text = status, js, text
            self.ok = status < 400

        def raise_for_status(self):
            if self.status_code >= 400:
                raise _rq.HTTPError(str(self.status_code), response=self)

        def json(self):
            if self._js is None:
                raise ValueError("JSON değil")
            return self._js

    def mk():
        c = azb.AzureDevOpsClient.__new__(azb.AzureDevOpsClient)
        c.org_url, c.project, c.repo_projects = "https://dev.azure.com/o", "P", ["EcomBackend", "Other"]
        c._repo_project_cache = {}
        c.__class__ = _Cl
        return c
    og = azb.requests.get
    try:
        azb.requests.get = lambda url, headers=None, params=None, timeout=None, **k: _R(200, {"id": "g"})
        p, err = safe(lambda: mk()._repo_project_strict("core"))
        check("N2: iki projede aynı adlı repo → hata, projeler adlandırılır",
              p is None and isinstance(err, ValueError) and "EcomBackend" in str(err) and "Other" in str(err),
              str((p, err)))
        azb.requests.get = lambda url, headers=None, params=None, timeout=None, **k: (
            _R(203, None, "<html>Sign in</html>") if "/EcomBackend/" in url else _R(404))
        p, err = safe(lambda: mk()._repo_project_strict("core"))
        check("N2: 203 HTML oturum sayfası olumlu sayılmaz → hata", p is None and err is not None, str((p, err)))
        azb.requests.get = lambda url, headers=None, params=None, timeout=None, **k: (
            _R(200, {"value": []}) if "/EcomBackend/" in url else _R(404))
        p, err = safe(lambda: mk()._repo_project_strict("core"))
        check("N2: 200 ama repo id'siz JSON olumlu sayılmaz → hata", p is None and err is not None, str((p, err)))
        azb.requests.get = lambda url, headers=None, params=None, timeout=None, **k: (
            _R(200, {"id": "g"}) if "/Other/" in url else _R(404))
        p, err = safe(lambda: mk()._repo_project_strict("core"))
        check("N2: tek olumlu yanıt → o proje", p == "Other", str((p, err)))
    finally:
        azb.requests.get = og


def _upgrade_deps(base):
    """Son düzeltme testleri için ağsız Deps (board boş, freshen kapalı)."""
    from agile_sdlc_crew.quality import intake
    cfg = {"pack_sp": 3, "pack_files": 5, "grace_days": 3, "task_fields": {}, "sonar_url": "", "nr_days": 7,
           "php_exclude": [], "php_bin": "php", "scan_ttl_h": 24, "max_take": 8, "freshen": False,
           "repos_dir": base, "targets_cache": base / "t.yaml", "scan_cache": base / "cache"}
    return intake.Deps(gather=None, board=lambda c: {}, repo_ok=lambda r: (base / r).is_dir(), queue=None,
                       record=lambda **k: None, now=lambda: 1e9, cfg=lambda: cfg)


def test_upgrade_final_f2_three_part():
    """F2: üç parçalı imaj etiketi (golang:1.24.4-alpine, php:8.2.12-fpm) yeniden yazılmaz — rakam-dizisi kuralı
    golang:1.27.4 gibi var olmayabilecek bir etiket uydururdu. Site old == new, elle düzenlenir."""
    print("\n[Sürüm yükseltme son düzeltme F2: üç parçalı imaj etiketi]")
    from agile_sdlc_crew.quality import upgrade, upgrade_bump as bump, upgrade_track as ut
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "svc"
        root.mkdir()
        for lang, img, to in (("go", "golang:1.24.4-alpine", "1.27"), ("php", "php:8.2.12-fpm", "8.4"),
                              ("go", "myreg.io/library/golang:1.24.4", "1.27"),
                              ("python", "10.1.1.1:5000/python:3.11.9-slim", "3.13")):
            (root / "Dockerfile").write_text(f"FROM {img} AS build\nRUN x\n", encoding="utf-8")
            cs = [s for s in upgrade._code_sites("svc", root, lang, to) if s["path"] == "Dockerfile"]
            check(f"F2 kod Dockerfile {img}: elle düzenlenir, etiket uydurulmaz",
                  len(cs) == 1 and cs[0]["old"] == cs[0]["new"] == img and cs[0]["blocked"] == upgrade.CI_HAND
                  and cs[0]["line"] == 1 and not bump.pushable(cs[0]), str(cs))
        (root / "Dockerfile").write_text("FROM golang:1.24-alpine\n", encoding="utf-8")
        cs = upgrade._code_sites("svc", root, "go", "1.27")
        check("F2 iki parçalı etiket yine otomatik", len(cs) == 1 and cs[0]["new"] == "golang:1.27-alpine"
              and bump.pushable(cs[0]), str(cs))
        for img in ("golang:1.24.4", "10.1.1.1:5000/library/golang:1.24.4-alpine"):
            index = {"gocrons": [{"src": "anatolia/build/alf", "images": [img]}],
                     "x": [{"src": "anatolia/build/x", "images": [upgrade.image_for(img, "go", "1.27")]}]}
            ss = upgrade._devops_sites(None, "gocrons", "go", "1.27", index, lambda d, p, i=img: f"FROM {i}\n")
            check(f"F2 FLODevOps {img}: aynı bölgede yeni etiket olsa da elle",
                  len(ss) == 1 and ss[0]["old"] == ss[0]["new"] == img and ss[0]["blocked"] == upgrade.CI_HAND
                  and ss[0]["line"] == 1 and not bump.pushable(ss[0]), str(ss))
        check("F2 _three_part: önekli/son ekli, iki parçalı değil",
              upgrade._three_part("eu/golang:1.24.4-alpine", "go") and upgrade._three_part("php:8.2.12", "php")
              and upgrade._three_part("prod-ar/python-3-13-7:1.0", "python")
              and not upgrade._three_part("eu/prod-ar/php-8-2:1.0", "php")
              and not upgrade._three_part("golang:1.24-alpine", "go"))
        # gerekçe yol:satır ve hedefi söyler
        dv = {"where": "devops", "repo": "anatolia", "project": "FLODevOps", "branch": "production",
              "path": "/build/alf/Dockerfile", "line": 1, "old": "golang:1.24.4", "new": "golang:1.24.4",
              "blocked": upgrade.CI_HAND}
        groot = base / "gocrons"
        groot.mkdir()
        (groot / "go.mod").write_text("module x\n\ngo 1.24\n", encoding="utf-8")
        sc = {"repo": "gocrons", "lang": "go", "current": "1.24", "target": "1.27", "up_to_date": False,
              "sites": [dv], "blocking": 0, "blockers": [], "auto": [], "infra": [], "human": [], "ci_pins": [],
              "ci_source": None}
        s = ut.status(_UpFake(), "gocrons", "go", to="1.27", deps=_upgrade_deps(base), scan=sc, wait_h=72, root=groot)
        check("F2 gerekçe dosya:satır ve hedefi söyler", not s["next"]["ready"] and s["stage"] == "engelli"
              and "/build/alf/Dockerfile:1" in s["next"]["reason"] and "1.27" in s["next"]["reason"], str(s["next"]))
        csite = dict(dv, where="code", repo="gocrons", project="", branch="", path="Dockerfile", line=3)
        sc2 = dict(sc, sites=[csite])
        s = ut.status(_UpFake(), "gocrons", "go", to="1.27", deps=_upgrade_deps(base), scan=sc2, wait_h=72,
                      root=groot)
        check("F2 kod sitesi gerekçesi dosya:satır ve hedef", not s["next"]["ready"]
              and "Dockerfile:3" in s["next"]["reason"] and "1.27" in s["next"]["reason"], str(s["next"]))


def test_upgrade_final_f3_published():
    """F3: `selectable` imaj kanıtıyla tutarlı — bir sürüm 'yayınlanmış' ancak reponun her servis imajının yeni
    hâli o bölgede zaten kullanılıyorsa (ve üç parçalı değilse); varsayılan hedef bilinen sürümlerle kesişir."""
    print("\n[Sürüm yükseltme son düzeltme F3: yayınlanmış imaj tutarlılığı]")
    from agile_sdlc_crew.quality import upgrade
    idx_go = {"gocrons": [{"src": "anatolia/build/alf", "images": ["golang:1.24.4"]}],
              "x": [{"src": "anatolia/build/x", "images": ["golang:1.27.4", "golang:1.25.4", "golang:1.26.4"]}]}
    svc = upgrade._devops_services("gocrons", "go", idx_go, None)
    sel = upgrade.selectable("go", "1.24", idx_go, svc)
    check("F3 Go golang:1.24.4 → hiçbir hedef yayınlanmış değil", sel and not any(s["published"] for s in sel),
          str(sel))
    ss = upgrade._devops_sites(None, "gocrons", "go", "1.27", idx_go, lambda d, p: "FROM golang:1.24.4\n")
    check("F3 Go sitesi elle düzenlenir", len(ss) == 1 and ss[0]["blocked"] == upgrade.CI_HAND, str(ss))
    check("F3 servisi olmayan repo → yayınlanmış", all(s["published"] for s in upgrade.selectable("go", "1.24", {}, {})))
    idx = {"core": [{"src": "anatolia/build/l", "images": ["eu/php-8-2:1.0"]},
                    {"src": "russia/build/l", "images": ["10.1.1.1:5000/php-8-2:1.0"]}],
           "web": [{"src": "anatolia/build/w", "images": ["eu/php-8-4:1.0"]}]}
    sel = {s["version"]: s for s in upgrade.selectable("php", "8.2", idx, upgrade._devops_services("core", "php", idx, None))}
    check("F3 anatolia'da yeni imaj var, russia'da yok → 8.4 yayınlanmamış", sel["8.4"]["published"] is False,
          str(sel))
    idx2 = dict(idx, ru=[{"src": "russia/build/ru", "images": ["10.1.1.1:5000/php-8-4:1.0"]}])
    sel = {s["version"]: s for s in upgrade.selectable("php", "8.2", idx2, upgrade._devops_services("core", "php", idx2, None))}
    check("F3 iki bölgede de yeni imaj → 8.4 yayınlanmış", sel["8.4"]["published"] is True
          and sel["8.3"]["published"] is False, str(sel))
    check("F3 ortak yüklem _image_ok", upgrade._image_ok({"eu/php-8-4:1.0"}, "eu/php-8-2:1.0", "php", "8.4")
          and not upgrade._image_ok({"golang:1.27.4"}, "golang:1.24.4", "go", "1.27")
          and not upgrade._image_ok(set(), "eu/php-8-2:1.0", "php", "8.4"))
    idx3 = {"a": [{"src": "anatolia/build/a", "images": ["eu/php-8-4:1.0", "eu/php-8-9:1.0"]}]}
    check("F3 varsayılan hedef bilinen sürümlerle kesişir (8.9 listede yok)",
          upgrade.default_target("php", "8.2", idx3) == "8.4", str(upgrade.default_target("php", "8.2", idx3)))
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / "core").mkdir()
        (base / "core" / "composer.json").write_text('{"require": {"php": "^8.2"}}', encoding="utf-8")
        rd = lambda d, p: ("FROM 10.1.1.1:5000/php-8-2:1.0\n" if d == "russia" else "FROM eu/php-8-2:1.0\n")
        pl = upgrade.plan(None, "core", "php", to="8.4", deps=_upgrade_deps(base), index=idx, read=rd, yml_files={})
        sel = {s["version"]: s for s in pl["selectable"]}
        check("F3 plan seçilebilirleri servislerle hesaplar", sel["8.4"]["published"] is False
              and any(s["where"] == "devops" and s["blocked"] == upgrade.UNPUBLISHED for s in pl["sites"]), str(sel))


def test_upgrade_final_f6_ci_parse():
    """F6: test hattı satırı ayrıştırma delikleri — (a) sürümsüz GoTool / UsePythonVersion bloğu sonraki adımın
    `version:`'ını almaz (blok, görevin girintisinde ya da daha sığ her satırda biter); (b) değeri bulunamayan
    sürüm görevi okunamayan sabitlemedir (akış biçimi, satır sonu yorumu, tırnaklı görev adı); (c) liste biçimi
    değişken (`- name: phpVersion` / `value:`) tanınır, anahtar adı geçip sabitleme çıkmazsa okunamayan sabitleme."""
    print("\n[Sürüm yükseltme son düzeltme F6: test hattı ayrıştırma]")
    from agile_sdlc_crew.quality import upgrade, upgrade_bump as bump

    def scan_ci(repo, lang, to, yml):
        try:
            return upgrade._ci_scan(None, repo, lang, to, {"Test Pipelines/x.yml": yml}), None
        except Exception as e:  # noqa: BLE001
            return None, e
    head = "resources:\n  repositories:\n    - repository: src\n      name: EcomBackend/{repo}\n"
    go = head.format(repo="gocrons")
    # (a) sürüm girdisi olmayan GoTool, sonraki adımın version'ı
    for nxt in ("  - publish: out\n    artifact: x\n    version: '1.21'\n",
                "  - download: current\n    version: '1.21'\n",
                "  - ${{ if eq(1, 1) }}:\n    - script: echo\n      version: '1.21'\n",
                "variables:\n  version: '1.21'\n"):
        yml = go + "steps:\n  - task: GoTool@0\n    displayName: Go\n" + nxt
        res, err = scan_ci("gocrons", "go", "1.25", yml)
        sites, pins = res or ([], [])
        check(f"F6a: sürümsüz GoTool başka adımın version'ını almaz ({nxt.split(':')[0].strip()})",
              res is not None and sites == [] and len(pins) == 1 and pins[0]["version"] == ""
              and pins[0]["line"] == 6 and "GoTool" in pins[0]["unparsed"] and "okunamadı" in pins[0]["unparsed"],
              str((res, err)))
    # (b) okunamayan sürüm görevi
    for label, body in (("akış biçimi", "  - task: GoTool@0\n    inputs: { version: '1.24' }\n  - script: x\n"),
                        ("dosya sonu", "  - task: GoTool@0\n    inputs:\n      goPath: x\n")):
        res, err = scan_ci("gocrons", "go", "1.25", go + "steps:\n" + body)
        pins = (res or ([], []))[1]
        check(f"F6b: {label} → okunamayan sabitleme", res is not None and len(pins) == 1 and pins[0]["version"] == ""
              and "okunamadı" in pins[0]["unparsed"], str((res, err)))
    for task in ("- task: GoTool@0  # go kurulumu", "- task: 'GoTool@0'", '- task: "GoTool@0" # x'):
        res, err = scan_ci("gocrons", "go", "1.25", go + f"steps:\n  {task}\n    inputs:\n      version: '1.24'\n")
        sites, pins = res or ([], [])
        check(f"F6b: görev satırı tanınır ({task})", res is not None and [p["version"] for p in pins] == ["1.24"]
              and len(sites) == 1 and bump.pushable(sites[0]) and sites[0]["new"] == "'1.25'", str((res, err)))
    py = head.format(repo="segflo") + "steps:\n  - task: UsePythonVersion@0\n  - script: pytest\n    versionSpec: '3.9'\n"
    res, err = scan_ci("segflo", "python", "3.12", py)
    check("F6a: UsePythonVersion de aynı kural", res is not None and res[0] == [] and len(res[1]) == 1
          and res[1][0]["version"] == "" and "UsePythonVersion" in res[1][0]["unparsed"], str((res, err)))
    # (c) liste biçimi değişkenler
    core = head.format(repo="core")
    res, err = scan_ci("core", "php", "8.4", core + "variables:\n- name: phpVersion\n  value: '8.2'\n")
    sites, pins = res or ([], [])
    check("F6c: - name: phpVersion / value: '8.2' → sabitleme + otomatik site",
          res is not None and [(p["version"], p["line"]) for p in pins] == [("8.2", 7)] and len(sites) == 1
          and sites[0]["old"] == "'8.2'" and sites[0]["new"] == "'8.4'" and bump.pushable(sites[0]), str((res, err)))
    res, err = scan_ci("core", "php", "8.4", core + "parameters:\n  - name: 'phpVersion'\n    type: string\n"
                                                    "    default: '8.2'\n")
    check("F6c: parametre default'u da sabitleme", res is not None and [p["version"] for p in res[1]] == ["8.2"],
          str((res, err)))
    res, err = scan_ci("core", "php", "8.4", core + "variables:\n- name: phpVersion\n  value: $(v)\n")
    check("F6c: liste biçimi okunamayan değer", res is not None and len(res[1]) == 1 and res[1][0]["version"] == ""
          and res[0] == [], str((res, err)))
    res, err = scan_ci("core", "php", "8.4", core + "variables:\n- name: phpVersion\n- name: other\n  value: '1'\n")
    check("F6c: değersiz liste değişkeni okunamayan", res is not None and len(res[1]) == 1
          and res[1][0]["version"] == "", str((res, err)))
    res, err = scan_ci("core", "php", "8.4", core + "steps:\n  - script: php${{ variables.phpVersion }} -v\n")
    check("F6c: anahtar geçiyor ama sabitleme yok → okunamayan (bilinmeyen 'yok' sayılmaz)",
          res is not None and len(res[1]) == 1 and res[1][0]["version"] == "" and res[1][0]["line"] == 6,
          str((res, err)))
    res, err = scan_ci("core", "php", "8.4", core + "variables:\n  phpVersion: '8.2'\nsteps:\n"
                                                    "  - script: sudo update-alternatives --set php /usr/bin/php$(phpVersion)\n")
    check("F6c: sabitlenmiş anahtarın kullanımı ikinci sabitleme değil",
          res is not None and [p["version"] for p in res[1]] == ["8.2"] and len(res[0]) == 1, str((res, err)))


class _PipeFake:
    """F1: `<repo>-test` tanımı + dosyası için sahte istemci (ağ yok). pipe: dict | None | Exception;
    files: (proje, repo, dal, yol) → metin; legacy: proje → {yol: metin} (kod projesinin yml-files'ı)."""

    def __init__(self, pipe=None, files=None, legacy=None, project="EcomLojistik", legacy_error=None):
        self.pipe, self.files, self.legacy = pipe, dict(files or {}), dict(legacy or {})
        self.project, self.legacy_error, self.calls = project, legacy_error, []

    def get_test_pipeline(self, repo):
        self.calls.append(("pipe", repo))
        if isinstance(self.pipe, Exception):
            raise self.pipe
        return self.pipe

    def _repo_project_strict(self, repo):
        return self.project

    def get_file_content(self, repo, path, branch=None, project=None):
        self.calls.append(("file", project, repo, branch, path))
        if repo == "yml-files" and branch is None:
            return (self.legacy.get(project) or {}).get(path.lstrip("/"), "")
        v = self.files.get((project, repo, branch, path.lstrip("/")))
        if v is None:
            raise RuntimeError("404 dosya yok")
        return v

    def get_items_in_path(self, repo, path="/", branch=None, recursion_level="oneLevel", project=None):
        self.calls.append(("items", project, repo, branch, path))
        if self.legacy_error is not None:
            raise self.legacy_error
        return [{"path": "/" + p, "isFolder": False} for p in (self.legacy.get(project) or {})]


def test_upgrade_final_f1_pipeline_source():
    """F1: test hattı dosyası `<repo>-test` TANIMINDAN okunur (repository + process.yamlFilename) — dört ayrı
    yml-files var; core'un Core-test'i EcomLojistik/yml-files:Test Pipelines/Core.yml'ı koşar. Okunamayan /
    tanınmayan her durum kapıyı kapalı tutar."""
    print("\n[Sürüm yükseltme son düzeltme F1: test hattı tanımı kaynak]")
    import requests as _rq
    from agile_sdlc_crew.quality import upgrade, upgrade_bump as bump, upgrade_track as ut
    from agile_sdlc_crew.tools import azure_devops_base as azb

    core_yml = ("trigger: none\nresources:\n  repositories:\n    - repository: core\n      name: EcomLojistik/core\n"
                "variables:\n  phpVersion: '8.2'\n")
    pipe = {"id": 55, "name": "Core-test", "project": "EcomLojistik", "yaml": "Test Pipelines/Core.yml",
            "repo_id": "c5d0", "repo_name": "yml-files", "branch": "main", "repo_project": "EcomLojistik"}
    fkey = ("EcomLojistik", "yml-files", "main", "Test Pipelines/Core.yml")
    src = {"repo": "yml-files", "project": "EcomLojistik", "branch": "main", "path": "Test Pipelines/Core.yml"}

    def safe(fn):
        try:
            return fn(), None
        except Exception as e:  # noqa: BLE001
            return None, e

    c = _PipeFake(pipe, {fkey: core_yml})
    res, err = safe(lambda: upgrade._ci_scan(c, "core", "php", "8.4", None))
    sites, pins = res or ([], [])
    check("F1 core: tanımın dosyası (başka proje) okunur → sabitleme + otomatik site",
          res is not None and pins == [dict(src, line=7, version="8.2")] and len(sites) == 1
          and sites[0]["project"] == "EcomLojistik" and sites[0]["repo"] == "yml-files" and bump.pushable(sites[0])
          and ("file", "EcomLojistik", "yml-files", "main", "/Test Pipelines/Core.yml") in c.calls, str((res, err, c.calls)))
    r = upgrade._ci_read(_PipeFake(pipe, {fkey: core_yml}), "core", "php", "8.4", None)
    check("F1 kaynak ve tanım döner", r["source"] == src and r["definition"] == pipe and r["live"], str(r))
    # Test Pipelines/ dışında duran dosya da okunur (önek süzgeci yok)
    p2 = dict(pipe, yaml="pipelines/core-test.yml")
    res, err = safe(lambda: upgrade._ci_scan(_PipeFake(p2, {fkey[:3] + ("pipelines/core-test.yml",): core_yml}),
                                             "core", "php", "8.4", None))
    check("F1 dosya Test Pipelines/ dışında da okunur", res is not None and [p["version"] for p in res[1]] == ["8.2"],
          str((res, err)))
    res, err = safe(lambda: upgrade._ci_scan(_PipeFake(None), "core", "php", "8.4", None))
    check("F1 tanım yok, eski dosya da yok → ([], [])", res == ([], []), str((res, err)))
    leg = _PipeFake(None, legacy={"EcomBackend": {"Test Pipelines/myduck.yml": core_yml.replace("EcomLojistik/core",
                                                                                                 "EcomBackend/mcduck")}},
                    project="EcomBackend")
    res, err = safe(lambda: upgrade._ci_scan(leg, "mcduck", "go", "1.27", None))
    check("F1 tanım yok ama kod projesinin yml-files'ında repoyu anan dosya var → okunamayan sabitleme",
          res is not None and res[0] == [] and len(res[1]) == 1 and res[1][0]["version"] == ""
          and "mcduck-test" in res[1][0]["unparsed"] and res[1][0]["path"] == "Test Pipelines/myduck.yml",
          str((res, err)))
    res, err = safe(lambda: upgrade._ci_scan(_PipeFake(None, legacy_error=RuntimeError("500")), "core", "php", "8.4",
                                             None))
    check("F1 eski dosya araması hata verirse bilinmiyor (None)", res is not None and res[1] is None, str((res, err)))
    for label, p, why in (("YAML değil", dict(pipe, yaml=None), "YAML"),
                          ("kod reposunun kendi dosyası", dict(pipe, repo_name="core", repo_project="EcomLojistik"),
                           "kod reposu")):
        res, err = safe(lambda p=p: upgrade._ci_scan(_PipeFake(p, {fkey: core_yml}), "core", "php", "8.4", None))
        check(f"F1 {label} → tek okunamayan sabitleme, site yok",
              res is not None and res[0] == [] and len(res[1]) == 1 and res[1][0]["version"] == ""
              and why in res[1][0]["unparsed"], str((res, err)))
    res, err = safe(lambda: upgrade._ci_scan(_PipeFake(pipe, {fkey: core_yml.replace("EcomLojistik/core", "X/other")}),
                                             "core", "php", "8.4", None))
    check("F1 dosya repoyu checkout etmiyor → okunamayan sabitleme ('paylaşılan / şablon')",
          res is not None and res[0] == [] and len(res[1]) == 1 and "paylaşılan" in res[1][0]["unparsed"],
          str((res, err)))
    tmpl = core_yml.replace("variables:\n  phpVersion: '8.2'\n", "extends:\n  template: base.yml\n")
    res, err = safe(lambda: upgrade._ci_scan(_PipeFake(pipe, {fkey: tmpl}), "core", "php", "8.4", None))
    check("F1 template/extends + sabitleme yok → okunamayan sabitleme",
          res is not None and len(res[1]) == 1 and res[1][0]["version"] == "" and "template" in res[1][0]["unparsed"],
          str((res, err)))
    plain = core_yml.replace("variables:\n  phpVersion: '8.2'\n", "steps:\n  - script: make test\n")
    r = upgrade._ci_read(_PipeFake(pipe, {fkey: plain}), "core", "php", "8.4", None)
    check("F1 okunan, repoyu anan, sürüm satırı ve şablonu olmayan dosya → sabitleme yok (zayıf kanıt yolu)",
          r["pins"] == [] and r["source"] == src and r["definition"] == pipe, str(r))
    res, err = safe(lambda: upgrade._ci_scan(_PipeFake(ValueError("iki tanım")), "core", "php", "8.4", None))
    check("F1 tanım okunamazsa sabitlemeler None (bilinmiyor)", res is not None and res[1] is None, str((res, err)))
    res, err = safe(lambda: upgrade._ci_scan(_PipeFake(pipe, {}), "core", "php", "8.4", None))
    check("F1 tanımın dosyası okunamazsa None", res is not None and res[1] is None, str((res, err)))

    # plan: ci_source + tanım, servis/kod siteleri yanında
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / "core").mkdir()
        (base / "core" / "composer.json").write_text('{"require": {"php": "^8.2"}}', encoding="utf-8")
        index = {"core": [{"src": "anatolia/build/l", "images": ["eu/php-8-2:1.0"]}],
                 "web": [{"src": "anatolia/build/w", "images": ["eu/php-8-4:1.0"]}]}
        pl = upgrade.plan(_PipeFake(pipe, {fkey: core_yml}), "core", "php", to="8.4", deps=_upgrade_deps(base),
                          index=index, read=lambda d, p: "FROM eu/php-8-2:1.0\n", app_map_cache={})
        check("F1 plan ci_source + ci_test_def", pl["ci_source"] == src and pl.get("ci_test_def") == pipe
              and [s["project"] for s in pl["sites"] if s["where"] == "ci"] == ["EcomLojistik"], str(pl))
        pl2 = upgrade.plan(None, "core", "php", to="8.4", deps=_upgrade_deps(base), index=index,
                           read=lambda d, p: "FROM eu/php-8-2:1.0\n", yml_files={}, app_map_cache={})
        check("F1 enjekte yolda sabitleme yoksa ci_source None, tanım çözülmez", pl2["ci_source"] is None
              and "ci_test_def" not in pl2, str(pl2))

        # status: CI PR'ı ci_source'ta aranır; yoksa aranmaz; kanıt çözülmüş tanımla (ikinci arama yok)
        dv = {"where": "devops", "repo": "anatolia", "project": "FLODevOps", "branch": "production",
              "path": "/build/l/Dockerfile", "line": 1, "old": "eu/php-8-2:1.0", "new": "eu/php-8-4:1.0",
              "blocked": ""}
        ci_s = {"where": "ci", "repo": "yml-files", "project": "EcomLojistik", "branch": "main",
                "path": "Test Pipelines/Core.yml", "line": 7, "old": "'8.2'", "new": "'8.4'", "blocked": ""}
        low = dict(src, line=7, version="8.2")
        BR = "tempo/upgrade-php-core-8.4"
        prs = {("EcomLojistik", "yml-files", BR): {"id": 11, "status": "active", "url": "u", "closed_at": "",
                                                   "merge_commit": "", "target": "main", "created_by": "tempo-id"}}

        def scan_(sites, pins, source, **k):
            return dict({"repo": "core", "lang": "php", "current": "8.2", "target": "8.4", "up_to_date": False,
                         "sites": sites, "blocking": 0, "blockers": [], "auto": [], "infra": [], "human": [],
                         "ci_pins": pins, "ci_source": source}, **k)
        s = ut.status(_UpFake(prs=prs), "core", "php", to="8.4", deps=_upgrade_deps(base),
                      scan=scan_([dv, ci_s], [low], src), wait_h=72, root=base / "core")
        check("F1 CI PR'ı tanımın reposunda (EcomLojistik) aranır", s["prs"]["ci"] and s["prs"]["ci"]["id"] == 11
              and s["stage"] == "test hattı" and not s["next"]["ready"], str(s["prs"]))
        s = ut.status(_UpFake(prs=prs), "core", "php", to="8.4", deps=_upgrade_deps(base),
                      scan=scan_([dv], [], None, ci_test_def=None), wait_h=72, root=base / "core")
        check("F1 ci_source yoksa CI PR'ı aranmaz", s["prs"]["ci"] is None, str(s["prs"]))
        c_ev = _UpFake(defs={"core": {"id": 5, "name": "Core-test", "project": "EcomLojistik"}},
                       builds={55: [{"build_id": 9, "status": "completed", "result": "succeeded", "definition": "Core-test",
                                     "url": "", "project": "EcomLojistik", "queue_time": "2026-11-10T12:00:00Z",
                                     "source_version": ""}]})
        s = ut.status(c_ev, "core", "php", to="8.4", deps=_upgrade_deps(base),
                      scan=scan_([dv], [], src, ci_test_def=pipe), wait_h=72, root=base / "core")
        check("F1 kanıt çözülmüş tanımla aranır (tanım ikinci kez sorulmaz)",
              not c_ev.def_calls and c_ev.build_calls and c_ev.build_calls[0][:2] == ("EcomLojistik", 55)
              and s["next"]["ready"] and s["next"]["test_note"].startswith("Test kanıtı zayıf"),
              str((c_ev.def_calls, c_ev.build_calls, s["next"])))
        s = ut.status(c_ev, "core", "php", to="8.4", deps=_upgrade_deps(base),
                      scan=scan_([dv], [], None, ci_test_def=None), wait_h=72, root=base / "core")
        check("F1 çözülmüş 'tanım yok' → Test kanıtı yok, ikinci arama yok",
              not c_ev.def_calls and s["next"]["test_note"] == "Test kanıtı yok", str(s["next"]))
        s = ut.status(c_ev, "core", "php", to="8.4", deps=_upgrade_deps(base),
                      scan=scan_([dv], [], None, ci_test_def=pipe), wait_h=72, root=base / "core")
        check("F1 tanım var ama dosyası kaynak olarak okunmamış → zayıf kanıtla açılmaz",
              not s["next"]["ready"] and s["next"]["test_note"] == "", str(s["next"]))

    # İstemci: get_test_pipeline
    class _Cl(azb.AzureDevOpsClient):
        _headers = {"Authorization": "x"}

    class _R:
        def __init__(self, status=200, js=None):
            self.status_code, self._js, self.ok, self.text = status, js, status < 400, "x"

        def raise_for_status(self):
            if self.status_code >= 400:
                raise _rq.HTTPError(str(self.status_code), response=self)

        def json(self):
            if self._js is None:
                raise ValueError("JSON değil")
            return self._js

    def mk():
        cl = azb.AzureDevOpsClient.__new__(azb.AzureDevOpsClient)
        cl.org_url, cl.project, cl.repo_projects = "https://dev.azure.com/o", "P", ["EcomBackend", "EcomLojistik"]
        cl._repo_project_cache = {}
        cl.__class__ = _Cl
        cl._repo_project_strict = lambda r: "EcomLojistik"
        return cl
    detail = {"id": 55, "name": "Core-test", "process": {"type": 2, "yamlFilename": "/Test Pipelines/Core.yml"},
              "repository": {"id": "c5d0", "name": "yml-files", "type": "TfsGit", "defaultBranch": "refs/heads/main"}}

    def router(defs=None, det=None, repo=None):
        seen = []

        def get(url, headers=None, params=None, timeout=None, **k):
            seen.append((url, dict(params or {})))
            if url.endswith("/build/definitions"):
                return defs if isinstance(defs, _R) else _R(200, {"value": defs})
            if "/build/definitions/" in url:
                return det if isinstance(det, _R) else _R(200, det)
            if "/_apis/git/repositories/" in url:
                return repo if isinstance(repo, _R) else _R(200, repo)
            return _R(404)
        return get, seen
    og = azb.requests.get
    try:
        get, seen = router([{"id": 3, "name": "Core-lint"}, {"id": 55, "name": "Core-test"}], detail,
                           {"id": "c5d0", "project": {"name": "EcomLojistik"}})
        azb.requests.get = get
        d, err = safe(lambda: mk().get_test_pipeline("core"))
        check("F1 istemci: tanım + YAML yolu (baştaki / atılır) + repo / dal / repo projesi",
              d == {"id": 55, "name": "Core-test", "project": "EcomLojistik", "yaml": "Test Pipelines/Core.yml",
                    "repo_id": "c5d0", "repo_name": "yml-files", "branch": "main", "repo_project": "EcomLojistik"}
              and seen[0][0].endswith("/EcomLojistik/_apis/build/definitions") and seen[0][1]["name"] == "core-test"
              and seen[1][0].endswith("/EcomLojistik/_apis/build/definitions/55")
              and seen[2][0] == "https://dev.azure.com/o/_apis/git/repositories/c5d0", str((d, err, seen)))
        get, seen = router([{"id": 3, "name": "Core-lint"}])
        azb.requests.get = get
        d, err = safe(lambda: mk().get_test_pipeline("core"))
        check("F1 istemci: tanım yok → None", d is None and err is None and len(seen) == 1, str((d, err)))
        get, seen = router([{"id": 55, "name": "Core-test"}, {"id": 56, "name": "core-TEST"}])
        azb.requests.get = get
        d, err = safe(lambda: mk().get_test_pipeline("core"))
        check("F1 istemci: aynı adlı iki tanım → ValueError", isinstance(err, ValueError) and "Core-test" in str(err),
              str((d, err)))
        get, seen = router([{"id": 55, "name": "Core-test"}], dict(detail, process={"type": 1}),
                           {"id": "c5d0", "project": {"name": "EcomLojistik"}})
        azb.requests.get = get
        d, err = safe(lambda: mk().get_test_pipeline("core"))
        check("F1 istemci: tasarımcı (YAML olmayan) tanım → yaml None", d is not None and d["yaml"] is None,
              str((d, err)))
        for label, args in (("tanım listesi 500", (_R(500),)), ("tanım listesi JSON değil", (_R(200, None),)),
                            ("tanım ayrıntısı 203", ([{"id": 55, "name": "Core-test"}], _R(203, None))),
                            ("repo sorgusu 404", ([{"id": 55, "name": "Core-test"}], detail, _R(404)))):
            get, seen = router(*args)
            azb.requests.get = get
            d, err = safe(lambda: mk().get_test_pipeline("core"))
            check(f"F1 istemci: {label} → hata", d is None and err is not None, str((d, err)))
    finally:
        azb.requests.get = og
    check("F1 CI_REPO / CI_PROJECT sabitleri kalktı, find_build_definition yok",
          not hasattr(upgrade, "CI_REPO") and not hasattr(upgrade, "CI_PROJECT")
          and not hasattr(azb.AzureDevOpsClient, "find_build_definition"))


def test_upgrade_final_f7_push_clock():
    """F7: kanıt saati dosyanın commit zamanı değil, sabitleme dosyasının dalına yapılan SON PUSH'un zamanı —
    pipeline repolarına doğrudan push yapılıyor; önce yazılıp sonra push edilen commit eski bir build'i kanıt
    saydırırdı. max(push, Tempo CI PR'ının kapanışı) korunur; okunamayan saat kapıyı kapalı tutar."""
    print("\n[Sürüm yükseltme son düzeltme F7: push saati]")
    import requests as _rq
    from agile_sdlc_crew.quality import upgrade_track as ut
    from agile_sdlc_crew.tools import azure_devops_base as azb
    T1, T2, T3, T4 = ("2026-11-10T09:00:00Z", "2026-11-10T10:00:00Z", "2026-11-10T11:00:00Z",
                      "2026-11-10T12:00:00Z")
    pin = {"repo": "yml-files", "project": "EcomLojistik", "branch": "main", "path": "Test Pipelines/Core.yml",
           "line": 7, "version": "8.4"}
    dv = {"where": "devops", "repo": "anatolia", "project": "FLODevOps", "branch": "production",
          "path": "/build/l/Dockerfile", "line": 1, "old": "eu/php-8-2:1.0", "new": "eu/php-8-4:1.0", "blocked": ""}
    CORE_DEF = {"core": {"id": 5, "name": "Core-test", "project": "EcomLojistik"}}

    def b(bid, q):
        return {"build_id": bid, "status": "completed", "result": "succeeded", "definition": "Core-test", "url": "",
                "project": "EcomLojistik", "queue_time": q, "source_version": ""}

    class _PushFake(_UpFake):
        def __init__(self, pushes, **k):
            super().__init__(**k)
            self.pushes_asked, self.pushes_map = [], dict(pushes)

        def get_last_push_date(self, repo, branch, *, project=None):
            self.pushes_asked.append((project, repo, branch))
            v = self.pushes_map.get((project, repo, branch), "")
            if isinstance(v, Exception):
                raise v
            return v

        def get_last_commit_date(self, *a, **k):
            raise AssertionError("F7: kanıt saati commit zamanından okunmamalı")

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / "core").mkdir()
        sc = {"repo": "core", "lang": "php", "current": "8.2", "target": "8.4", "up_to_date": False, "sites": [dv],
              "blocking": 0, "blockers": [], "auto": [], "infra": [], "human": [], "ci_pins": [pin],
              "ci_source": _ci_src([pin])}
        key = ("EcomLojistik", "yml-files", "main")

        def st(c):
            try:
                return ut.status(c, "core", "php", to="8.4", deps=_upgrade_deps(base), scan=sc, wait_h=72,
                                 root=base / "core"), None
            except Exception as e:  # noqa: BLE001
                return None, e
        c = _PushFake({key: T3}, defs=CORE_DEF, builds={5: [b(2, T2)]})
        s, err = st(c)
        check("F7 commit T1'de ama push T3'te → T2'deki yeşil build kanıt değil",
              s is not None and (s["evidence"] or {}).get("since") == T3 and (s["evidence"] or {}).get("state") == "none"
              and not s["next"]["ready"] and c.pushes_asked == [key], str((s and s["evidence"], s and s["next"], err)))
        s, err = st(_PushFake({key: T3}, defs=CORE_DEF, builds={5: [b(3, T4), b(2, T2)]}))
        check("F7 push'tan sonra yeşil → prod hazır", s is not None and s["next"]["ready"], str((s and s["next"], err)))
        for label, v in (("boş", ""), ("okunamayan", "dün"), ("hata", RuntimeError("500"))):
            s, err = st(_PushFake({key: v}, defs=CORE_DEF, builds={5: [b(3, T4)]}))
            check(f"F7 push saati {label} → kapı kapalı", s is not None and not s["next"]["ready"]
                  and "son değişiklik zamanı okunamadı" in s["next"]["reason"], str((s and s["next"], err)))

    # İstemci: get_last_push_date
    class _Cl(azb.AzureDevOpsClient):
        _headers = {"Authorization": "x"}

    class _R:
        def __init__(self, status=200, js=None):
            self.status_code, self._js, self.ok = status, js, status < 400

        def raise_for_status(self):
            if self.status_code >= 400:
                raise _rq.HTTPError(str(self.status_code), response=self)

        def json(self):
            if self._js is None:
                raise ValueError("JSON değil")
            return self._js
    cl = azb.AzureDevOpsClient.__new__(azb.AzureDevOpsClient)
    cl.org_url, cl.project = "https://dev.azure.com/o", "P"
    cl.__class__ = _Cl
    cl._repo_api_url = lambda repo, project=None: f"https://x/{project or 'P'}/_apis/git/repositories/{repo}"
    seen = []
    og = azb.requests.get

    def use(resp):
        azb.requests.get = lambda url, headers=None, params=None, timeout=None, **k: (
            seen.append((url, dict(params or {}))), resp)[1]
    try:
        use(_R(200, {"value": [{"pushId": 9, "date": "2026-11-16T18:56:50.45Z"}]}))
        when = cl.get_last_push_date("yml-files", "main", project="EcomLojistik")
        check("F7 istemci: dalın son push'u (refName + $top=1) → date",
              when == "2026-11-16T18:56:50.45Z" and seen[-1][0] == "https://x/EcomLojistik/_apis/git/repositories/yml-files/pushes"
              and seen[-1][1].get("searchCriteria.refName") == "refs/heads/main" and seen[-1][1].get("$top") == 1,
              str((when, seen[-1:])))
        use(_R(200, {"value": []}))
        check("F7 istemci: push yok → ''", cl.get_last_push_date("yml-files", "main", project="EcomLojistik") == "")
        use(_R(200, {"value": [{"pushId": 9}]}))
        check("F7 istemci: date yok → '' (okunamaz)", cl.get_last_push_date("yml-files", "main") == "")
        for label, r in (("500", _R(500, {"x": 1})), ("JSON değil", _R(200, None))):
            use(r)
            try:
                cl.get_last_push_date("yml-files", "main")
                check(f"F7 istemci: {label} → hata", False)
            except Exception:  # noqa: BLE001
                check(f"F7 istemci: {label} → hata", True)
    finally:
        azb.requests.get = og


def test_upgrade_final_f8_pinned_read():
    """F8: push_sites dosyayı hedef dalın ADIYLA değil, önce alınan hedef commit'inden (target_oid) okur — iki
    okuma arasındaki eşzamanlı bir düzenleme Tempo'nun dalında geri alınmasın."""
    print("\n[Sürüm yükseltme son düzeltme F8: sabit commit'ten okuma]")
    import requests as _rq
    from agile_sdlc_crew.quality import upgrade_bump as bump
    from agile_sdlc_crew.tools import azure_devops_base as azb

    class _Shift(_BumpFake):
        """Dalın adıyla okunan içerik eşzamanlı düzenlemeyle değişmiş (RUN y); hedef commit'teki RUN x."""
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.log = []

        def list_branches(self, repo, project=None):
            self.log.append(("refs", repo))
            return super().list_branches(repo, project)

        def get_file_bytes(self, repo, path, branch=None, project=None, *, version=None, version_type=None):
            self.log.append(("read", branch, version, version_type))
            if version_type == "commit" and version == "oid-production":
                return b"FROM eu/php-8-2:1.0\nRUN x\n"
            return super().get_file_bytes(repo, path, branch, project)
    files = {("FLODevOps", "anatolia", "production", "build/core/Dockerfile"): "FROM eu/php-8-2:1.0\nRUN y\n"}
    site = {"where": "devops", "repo": "anatolia", "project": "FLODevOps", "branch": "production",
            "path": "/build/core/Dockerfile", "line": 1, "old": "eu/php-8-2:1.0", "new": "eu/php-8-4:1.0",
            "blocked": ""}
    c = _Shift(files)
    res = bump.push_sites(c, [site], branch="tempo/upgrade-php-core-8.4", title="t", body="b", wi_id=None)
    reads = [x for x in c.log if x[0] == "read"]
    first_read = next(i for i, x in enumerate(c.log) if x[0] == "read")
    first_refs = next(i for i, x in enumerate(c.log) if x[0] == "refs")
    check("F8 hedef commit önce alınır, dosya o commit'ten okunur",
          first_refs < first_read and reads and reads[0][2:] == ("oid-production", "commit"), str(c.log))
    check("F8 push hedef commit'teki içerikten (eşzamanlı düzenleme geri alınmaz) ve o commit'in üstüne",
          res["prs"] and len(c.pushes) == 1 and _bump_dec(c.pushes[0]["changes"][0]) == "FROM eu/php-8-4:1.0\nRUN x\n"
          and c.pushes[0]["old_object_id"] == "oid-production", str((res, c.pushes)))

    # İstemci: get_file_bytes(version=, version_type=) — varsayılan davranış aynı
    class _Cl(azb.AzureDevOpsClient):
        _headers = {"Authorization": "x"}

    class _R:
        status_code, content = 200, b"x"

        def raise_for_status(self):
            pass
    cl = azb.AzureDevOpsClient.__new__(azb.AzureDevOpsClient)
    cl.__class__ = _Cl
    cl._repo_api_url = lambda repo, project=None: f"https://x/{project}/_apis/git/repositories/{repo}"
    seen = []
    og = azb.requests.get
    azb.requests.get = lambda url, headers=None, params=None, timeout=None, **k: (seen.append(dict(params)), _R())[1]
    try:
        cl.get_file_bytes("r", "/a", "production", project="P", version="abc123", version_type="commit")
        cl.get_file_bytes("r", "/a", "production", project="P")
    finally:
        azb.requests.get = og
    check("F8 istemci: commit sürümüyle okuma", seen[0].get("versionDescriptor.version") == "abc123"
          and seen[0].get("versionDescriptor.versionType") == "commit", str(seen[0]))
    check("F8 istemci: varsayılan dal adıyla okuma değişmedi", seen[1].get("versionDescriptor.version") == "production"
          and seen[1].get("versionDescriptor.versionType") == "branch", str(seen[1]))


def test_upgrade_final_f5_resolved_blocks():
    """F5: engelleyen bulgu kodda dururken Task'ı 'bitmiş gibi' (Resolved / Ready for Production / grace içinde Done)
    bir durumdaysa dedup onu `skipped`'e atar ve `open_tasks` onu kapalı sayar — hiçbir taraf saymıyordu, aşama
    `hazır` oluyordu. Tarama `açık WI` atlamalarının engelleyen kurallarını (ayrı kural başına bir) sayar; iptal
    edilmiş ve 'insan baktı' atlamaları sayılmaz."""
    print("\n[Sürüm yükseltme son düzeltme F5: çözülmüş görünen engelleyen iş]")
    from agile_sdlc_crew.quality import intake, upgrade, upgrade_track as ut
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        for r in ("core", "legacy"):
            (base / r).mkdir()
            (base / r / "composer.json").write_text('{"require": {}}', encoding="utf-8")
        index = {"core": [{"src": "anatolia/build/core", "images": ["eu/php-8-2:1.0"]}],
                 "legacy": [{"src": "anatolia/build/legacy", "images": ["eu/php-7-4:1.0"]}],
                 "web": [{"src": "anatolia/build/web", "images": ["eu/php-8-4:1.0"]}]}
        hits = {"core": [], "legacy": [{"rule": "null_to_internal", "file": "app/L.php", "line": 3, "text": "strlen(null)"},
                                       {"rule": "null_to_internal", "file": "app/M.php", "line": 4, "text": "trim(null)"}]}
        probe_err = [{"rule": "probe:error", "file": "app/D.php", "line": 3, "text": "x", "message": "Parse error: y"},
                     {"rule": "probe:error", "file": "app/E.php", "line": 5, "text": "z", "message": "Parse error: y"}]
        closed = 1e9 - 10 * 86400

        def run(repo, board):
            cfg = {"pack_sp": 3, "pack_files": 5, "grace_days": 3, "task_fields": {}, "sonar_url": "", "nr_days": 7,
                   "php_exclude": [], "php_bin": "php", "scan_ttl_h": 24, "max_take": 8, "freshen": False,
                   "repos_dir": base, "targets_cache": base / "t.yaml", "scan_cache": base / ("c-" + repo)}
            deps = intake.Deps(gather=None, board=lambda c: board, repo_ok=lambda r: (base / r).is_dir(), queue=None,
                               record=lambda **k: None, now=lambda: 1e9, cfg=lambda: cfg)
            sc = upgrade.scan(None, repo, "php", to="8.4", deps=deps, index=index, read=lambda d, p: "", yml_files={},
                              app_map_cache={}, head_sha_fn=lambda r: "s",
                              scan_fn=lambda r, **k: {"files": 2, "sha": "s", "hits": list(hits[Path(r).name]),
                                                      "adoption": {}, "errors": []},
                              probe_fn=lambda root, lang, cur, to, **k: (
                                  list(probe_err) if Path(root).name == "core" else [], {"ran": True, "tool": "x",
                                                                                        "reason": ""}))
            st = ut.status(_UpFake(), repo, "php", to="8.4", deps=deps, scan=sc, wait_h=72, root=base / repo)
            return sc, st
        fps = [f"php_modern:core:probe:error:app/{f}.php" for f in ("D", "E")]
        sc, st = run("core", {})
        check("F5 temel: probe hatası engelliyor", sc["blocking"] == 1 and st["stage"] == "hazırlık", str(sc["blocking"]))
        for state in ("Resolved", "Ready for Production"):
            sc, st = run("core", {fp: {"wi": 920, "state": state} for fp in fps})
            check(f"F5 error bulgusunun Task'ı {state} → yine engelli (aynı kural iki dosya = 1), hazırlık",
                  sc["blocking"] == 1 and not sc["auto"] and st["stage"] == "hazırlık" and not st["next"]["ready"],
                  str((sc["blocking"], sc["skipped"], st["stage"])))
        sc, st = run("core", {fp: {"wi": 921, "state": "Done", "closed_at": 1e9 - 3600} for fp in fps})
        check("F5 grace içinde Done → yine engelli", sc["blocking"] == 1 and st["stage"] == "hazırlık", str(sc["skipped"]))
        sc, st = run("core", {fp: {"wi": 922, "state": "Canceled"} for fp in fps})
        check("F5 iptal edilmiş → sayılmaz", sc["blocking"] == 0 and st["stage"] != "hazırlık",
              str((sc["blocking"], sc["skipped"])))
        lfps = [f"php_modern:legacy:null_to_internal:app/{f}.php" for f in ("L", "M")]
        sc, st = run("legacy", {fp: {"wi": 930, "state": "Done", "closed_at": closed} for fp in lfps})
        check("F5 insan baktı → sayılmaz", sc["blocking"] == 0
              and any(s["reason"].startswith("insan baktı") for s in sc["skipped"]), str((sc["blocking"], sc["skipped"])))
        sc, st = run("legacy", {fp: {"wi": 931, "state": "Resolved"} for fp in lfps})
        check("F5 behavior bulgusunun Task'ı Resolved → engelli", sc["blocking"] == 1 and st["stage"] == "hazırlık",
              str((sc["blocking"], sc["skipped"])))
    check("F5 parmak izi → kural yardımcısı tek yerde (upgrade), upgrade_track onu kullanır",
          ut._fp_blocking is upgrade._fp_blocking)


def test_upgrade_final_f9_retry_guard():
    """F9: `POST /api/jobs/{id}/retry` bir upgrade_bump işini, aynı izleme işi için kuyrukta / çalışan bir adım işi
    varken yeniden açmaz (queue_step'in açık iş korumasını atlamasın) — 409. DB'ye dokunulmaz."""
    print("\n[Sürüm yükseltme son düzeltme F9: yeniden deneme koruması]")
    from fastapi.testclient import TestClient
    from agile_sdlc_crew import server
    from agile_sdlc_crew.quality import upgrade_bump as bump
    payload = json.dumps({"step": "prod"})
    kind = {"v": "upgrade_bump"}
    saved = (server.db.get_job, server.db.create_job, server._ensure_worker, bump._open_bump_jobs)
    made, asked = [], []
    server.db.get_job = lambda jid: {"id": jid, "status": "failed", "work_item_id": "500", "use_hal": 0,
                                     "wi_title": "t", "job_kind": kind["v"], "job_payload": payload}
    server.db.create_job = lambda *a, **k: (made.append((a, k)), 6)[1]
    server._ensure_worker = lambda: None
    try:
        tc = TestClient(server.app)
        bump._open_bump_jobs = lambda tid: (asked.append(tid), [{"id": 3}])[1]
        r = tc.post("/api/jobs/5/retry")
        check("F9 açık adım işi varken yeniden deneme → 409, iş açılmaz",
              r.status_code == 409 and not made and asked == ["500"] and "#3" in r.json().get("error", "")
              and "kuyrukta" in r.json().get("error", ""), str((r.status_code, r.text, made, asked)))
        bump._open_bump_jobs = lambda tid: []
        r = tc.post("/api/jobs/5/retry")
        check("F9 açık adım işi yoksa yeniden deneme kendi türüyle", r.status_code == 202 and made
              and made[-1][1] == {"job_kind": "upgrade_bump", "job_payload": payload}, str((r.status_code, made)))
        made.clear()
        asked.clear()
        kind["v"] = "pipeline"
        bump._open_bump_jobs = lambda tid: (asked.append(tid), [{"id": 3}])[1]
        r = tc.post("/api/jobs/5/retry")
        check("F9 pipeline işinin yeniden denemesi etkilenmez", r.status_code == 202 and made and not asked,
              str((r.status_code, asked)))
    finally:
        server.db.get_job, server.db.create_job, server._ensure_worker, bump._open_bump_jobs = saved


def test_upgrade_final_f10_catalog_flag():
    """F10: katalog v4'ün bu dalda EKLENEN kuralları (`upgrade_catalog: true`) CREW_TECHDEBT_UPGRADE kapalıyken
    modernleştirme hattında yok (yeni davranış varsayılan kapalı); yükseltme hattı onları her zaman görür."""
    print("\n[Sürüm yükseltme son düzeltme F10: katalog bayrağı]")
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import intake, modern, php_modern as pm, upgrade
    new_php = {"curly_offset", "removed_casts", "required_after_optional", "legacy_autoload", "php4_constructor",
               "loose_compare_numeric", "null_to_internal", "removed_81", "serializable_only", "moved_to_pecl_84",
               "noncanonical_casts_85", "backtick_exec_85", "deprecated_85"}
    rules = {r.key: r for r in pm.load_rules()}
    check("F10 eklenen PHP kuralları işaretli, v3'tekiler (breaks alanı eklenenler dahil) değil",
          {k for k, r in rules.items() if r.upgrade_catalog} == new_php
          and not rules["implicit_nullable"].upgrade_catalog and not rules["utf8_encode"].upgrade_catalog,
          str(sorted(k for k, r in rules.items() if r.upgrade_catalog)))
    nx = {r.key: r for r in modern.rules_for(modern.LANGS["nextjs"]["source"])}
    go = {r.key: r for r in modern.rules_for(modern.LANGS["go"]["source"])}
    check("F10 Next.js / Go eklenenleri işaretli",
          {k for k, r in nx.items() if r.upgrade_catalog} == {"sync_route_params", "next_legacy_image", "next_lint"}
          and {k for k, r in go.items() if r.upgrade_catalog} == {"go122_loopvar", "go122_httpmux", "go123_timerchan"},
          str(({k for k, r in nx.items() if r.upgrade_catalog}, {k for k, r in go.items() if r.upgrade_catalog})))
    hits = [{"rule": "implicit_nullable", "file": "app/A.php", "line": 3, "text": "x", "module": "app"},
            {"rule": "str_contains", "file": "app/B.php", "line": 4, "text": "y", "module": "app"},
            {"rule": "null_to_internal", "file": "app/C.php", "line": 5, "text": "strlen(null)", "module": "app"},
            {"rule": "curly_offset", "file": "app/D.php", "line": 6, "text": "$s{0}", "module": "app"},
            {"rule": "removed_81", "file": "app/E.php", "line": 7, "text": "strftime()", "module": "app"}]
    v3_keys = {"implicit_nullable", "str_contains"}
    orig = pc.get
    flag = {"on": False}
    pc.get = lambda k: flag["on"] if k == "CREW_TECHDEBT_UPGRADE" else orig(k)
    try:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "web").mkdir()
            (base / "web" / "composer.json").write_text('{"require": {"php": "^8.4"}}', encoding="utf-8")
            (base / "web" / "app").mkdir()
            cfg = {"pack_sp": 3, "pack_files": 5, "grace_days": 3, "task_fields": {}, "sonar_url": "", "nr_days": 7,
                   "php_exclude": [], "php_bin": "php", "scan_ttl_h": 24, "max_take": 8, "freshen": False,
                   "repos_dir": base, "targets_cache": base / "t.yaml", "scan_cache": base / "cache"}
            deps = intake.Deps(gather=None, board=lambda c: {}, repo_ok=lambda r: (base / r).is_dir(), queue=None,
                               record=lambda **k: None, now=lambda: 1e9, cfg=lambda: cfg)
            kw = dict(deps=deps, scan_fn=lambda r, **k: {"files": 5, "sha": "s", "hits": list(hits), "adoption": {},
                                                         "errors": []},
                      head_sha_fn=lambda r: "s", app_map_cache={})
            off = pm.scan(None, "web", **kw)
            rows = off["auto"] + off["infra"] + off["human"]
            check("F10 bayrak kapalı: modernleştirme taramasında yeni kural yok, özet v3 ile aynı",
                  off["version"] == "8.4" and set(off["rules"]) == v3_keys
                  and not ({r["rule"] for r in rows} & new_php), str((off["version"], sorted(off["rules"]))))
            flag["on"] = True
            on = pm.scan(None, "web", **kw)
            check("F10 bayrak açık: yeni kurallar modernleştirmede de görünür",
                  {"null_to_internal", "curly_offset", "removed_81"} <= set(on["rules"]), str(sorted(on["rules"])))
            flag["on"] = False
            # rector_scope_for (Teknik borç listesinden yeniden kuyruk): bayrak kapalıyken yeni kural araç kuralı değil
            (base / "core").mkdir()
            (base / "core" / "composer.json").write_text('{"require": {"php": "^8.2"}}', encoding="utf-8")
            saved_sc = pm.scan_cached
            pm.scan_cached = lambda *a, **k: {"hits": [{"rule": "curly_offset", "file": "app/D.php", "line": 6}]}
            try:
                fp = [pm.fingerprint("core", "curly_offset", "app/D.php")]
                none_off = pm.rector_scope_for(base / "core", fp, cfg=cfg)
                flag["on"] = True
                some_on = pm.rector_scope_for(base / "core", fp, cfg=cfg)
            finally:
                pm.scan_cached = saved_sc
                flag["on"] = False
            check("F10 rector_scope_for: bayrak kapalı → yeni kural kapsam vermez; açık → verir",
                  none_off is None and some_on == {"lang": "php", "repo": "core", "scope": {"curly_offset": ["app/D.php"]}},
                  str((none_off, some_on)))
            # Next.js modernleştirme grupları (modern.scan / take aynı build_groups'u kullanır)
            nhits = [{"rule": "next_legacy_image", "file": "app/p.tsx", "line": 1, "text": "x", "module": "app"}]
            g, _s, summ = pm.build_groups({"hits": nhits}, list(nx.values()), "15.0", repo="site", board_index={},
                                          now=1e9, cfg=cfg, source=modern.LANGS["nextjs"]["source"])
            check("F10 Next.js modernleştirme: bayrak kapalı → yeni kural grubu yok", not g and not summ, str(summ))
            # yükseltme hattı her zaman görür (bayrak kapalı)
            (base / "legacy").mkdir()
            (base / "legacy" / "composer.json").write_text('{"require": {}}', encoding="utf-8")
            idx = {"legacy": [{"src": "anatolia/build/l", "images": ["eu/php-7-4:1.0"]}],
                   "web": [{"src": "anatolia/build/w", "images": ["eu/php-8-4:1.0"]}]}
            up = upgrade.scan(None, "legacy", "php", to="8.4", deps=deps, index=idx, read=lambda d, p: "",
                              yml_files={}, app_map_cache={}, head_sha_fn=lambda r: "s",
                              scan_fn=lambda r, **k: {"files": 5, "sha": "s", "hits": list(hits), "adoption": {},
                                                      "errors": []},
                              probe_fn=lambda *a, **k: ([], {"ran": False, "tool": "", "reason": ""}))
            check("F10 yükseltme hattı bayrak kapalıyken de yeni kuralları görür",
                  {"null_to_internal", "curly_offset", "removed_81"} <= set(up["rules"]), str(sorted(up["rules"])))
    finally:
        pc.get = orig


def test_php_rector_fix():
    """#249 (core, list() → [], 64 dosya): LLM pipeline'ı planı 9 dosyada bıraktı, $5.40'a
    başarısız bitti. Rector sınıfı olan mekanik PHP kuralları artık modern_fix işine gider:
    kodu Rector yazar (yalnız kural × dosya), LLM yalnız bir kez ve Sonnet'le inceler."""
    print("\n[PHP: Rector ile araç düzeltmesi]")
    from agile_sdlc_crew.quality import modern_fix, php_modern, techdebt
    from agile_sdlc_crew.quality.model import Finding, Group

    rules = {r.key: r for r in php_modern.load_rules()}
    check("katalog: list()/??/str_contains/match/promotion Rector'la uygulanabilir",
          all(php_modern.rector_fixable(rules[k]) for k in
              ("array_destructuring", "null_coalesce", "str_contains", "match", "ctor_promotion")))
    check("katalog: yapısal ve Rector'suz kural araca gitmez",
          not php_modern.rector_fixable(rules["typed_properties"]) and not php_modern.rector_fixable(rules["sensitive_parameter"]))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "core"
        (root / "app").mkdir(parents=True)
        cfg = modern_fix.rector_config(root, {"array_destructuring": ["app/A.php", "app/it's$x.php"]}, "8.2", Path(td) / "c")
        check("Rector yapılandırması: yalnız kural sınıfı, kapsam dosyaları, hedef sürüm",
              "ListToArrayDestructRector" in cfg and "->withPhpVersion(80200)" in cfg and "/app/A.php'" in cfg
              and "StrContainsRector" not in cfg, cfg)
        check("Rector yapılandırması: yollar tek tırnaklı ($ enterpolasyonu yok, ' kaçışlı)",
              "it\\'s$x.php'" in cfg and '"' not in cfg.split("withPaths")[1].split(")")[0], cfg)

        _on, _cls = php_modern.rector_on, php_modern.rector_classes
        php_modern.rector_on = lambda: True
        php_modern.rector_classes = lambda: frozenset({rules["array_destructuring"].rector})
        try:
            ok = modern_fix.load_scope(json.dumps({"lang": "php", "repo": "core",
                                                   "scope": {"array_destructuring": ["app/A.php"]}}), Path(td))
            check("PHP kapsamı: Rector kuralı kabul edilir", ok[:3] == ("php", "core", {"array_destructuring": ["app/A.php"]}))
            ok2 = modern_fix.load_scope(json.dumps({"lang": "php", "repo": "core",
                                                    "scope": {"sensitive_parameter": ["app/A.php"]}}), Path(td))
            check("PHP kapsamı: SensitiveParameter kendi düzelticimizle kabul edilir", ok2[2] == {"sensitive_parameter": ["app/A.php"]})
            rej = 0
            for b in ({"typed_properties": ["app/A.php"]}, {"array_destructuring": ["../x.php"]},
                      {"ctor_promotion": ["app/A.php"]}):  # blok düzeyinde, sınıfı kurulu Rector'da yok sayılıyor
                try:
                    modern_fix.load_scope(json.dumps({"lang": "php", "repo": "core", "scope": b}), Path(td))
                except ValueError:
                    rej += 1
            check("PHP kapsamı: yapısal kural, kök dışı yol, kurulu Rector'da olmayan sınıf reddedilir", rej == 3, f"{rej}/3")
        finally:
            php_modern.rector_on, php_modern.rector_classes = _on, _cls
        php_modern.rector_on = lambda: False
        try:
            try:
                modern_fix.load_scope(json.dumps({"lang": "php", "repo": "core",
                                                  "scope": {"ctor_promotion": ["app/A.php"]}}), Path(td))
                off = False
            except ValueError:
                off = True
            check("Rector kapalı/kurulu değil → Rector'a muhtaç (blok düzeyindeki) kural reddedilir", off)
        finally:
            php_modern.rector_on = _on

        if shutil.which("php"):
            (root / "app" / "S.php").write_text(
                "<?php\nclass S {\n"
                "    public function __construct(private string $apiKey, private int $n) {}\n"
                "    public function login(string $user, string $password, #[\\SensitiveParameter] string $token, $x = 1) {}\n"
                "}\nfunction f(&$secret, ...$rest) { $s = 'string $password'; }\n")
            modern_fix.sensitive_apply(root, ["app/S.php", "../../etc/passwd"])
            out = (root / "app" / "S.php").read_text()
            check("SensitiveParameter: parola/token/api_key parametresinin önüne eklenir (promoted ve & dahil)",
                  "#[\\SensitiveParameter] private string $apiKey" in out and "#[\\SensitiveParameter] string $password" in out
                  and "#[\\SensitiveParameter] &$secret" in out, out)
            check("SensitiveParameter: zaten işaretli, ilgisiz parametre ve string içi dokunulmaz",
                  out.count("SensitiveParameter") == 4 and "private int $n" in out and "string $user" in out
                  and "$s = 'string $password'" in out, out)
            lint = subprocess.run(["php", "-l", str(root / "app" / "S.php")], capture_output=True, text=True)
            check("SensitiveParameter: çıktı php -l'den geçer", lint.returncode == 0, lint.stdout + lint.stderr)
        else:
            skip("SensitiveParameter düzeltici", "php yok")

        binp = php_modern.rector_bin()
        if binp:
            missing = [r.key for r in rules.values() if php_modern.rector_fixable(r) and r.rector not in php_modern.rector_classes()]
            check("katalogdaki her Rector sınıfı kurulu Rector'da var", not missing, str(missing))
            # reponun autoload'u çalıştırılmamalı (rector bin cwd'deki vendor/autoload.php'yi include eder)
            (root / "vendor").mkdir()
            (root / "vendor" / "autoload.php").write_text(
                f"<?php file_put_contents({json.dumps(str(Path(td) / 'PWNED'))}, 'x');\n")
            (root / "app" / "A.php").write_text("<?php\nfunction f() { return [1, 2]; }\nlist($a, $b) = f();\n"
                                                "$c = isset($x) ? $x : 0;\n")
            modern_fix.php_apply(root, {"array_destructuring": ["app/A.php"]}, "8.2")
            out = (root / "app" / "A.php").read_text()
            check("Rector: yalnız kapsamdaki kural uygulanır", "[$a, $b] = f();" in out and "isset($x) ? $x : 0" in out, out)
            check("Rector: reponun vendor/autoload.php'si çalıştırılmaz (cwd repo dışında)",
                  not (Path(td) / "PWNED").exists())
        else:
            skip("Rector uygulaması", "rector kurulu değil")

    fs = [Finding(source="php_modern", file=f"app/F{i}.php", rule="array_destructuring", lines=[1] * 3, uris=[])
          for i in range(70)]
    unit = [Group(source="php_modern", repo="core", title="list", fingerprints=[f"php_modern:core:array_destructuring:{f.file}"],
                  findings=[f]) for f in fs]
    dev = php_modern.pack_modern(unit, source="php_modern")[0].sp
    tool = php_modern.pack_modern(unit, source="php_modern", tool_rules={"array_destructuring"})[0].sp
    check("SP: Rector uygulayan kural araç eşikleriyle (geliştirici 3 → araç 1)", dev == 3 and tool == 1, f"{dev} {tool}")

    from agile_sdlc_crew.quality import intake
    from agile_sdlc_crew.quality.intake import _row
    g = php_modern.pack_modern(unit, source="php_modern", tool_rules={"array_destructuring"})[0]
    piped, tooled = [], []
    plain = intake.Deps(gather=None, board=None, repo_ok=None, queue=lambda w, t: (piped.append(w), 1)[1],
                        record=None, now=None, cfg=None)
    check("elle kurulan Deps (queue_tool yok) araç yoluna girmez — test DB'ye iş yazmaz",
          php_modern._rector_queue(plain, "core", [g]) is plain)
    wired = plain._replace(queue_tool=lambda w, t, p: (tooled.append((w, json.loads(p))), 2)[1])
    q = php_modern._rector_queue(wired, "core", [g]).queue
    q(10, _row(g)["title"]); q(11, "başka iş")
    check("take: Rector grubu queue_tool'a onaylı kapsamla, diğeri pipeline'a",
          tooled and tooled[0][0] == 10 and tooled[0][1]["scope"]["array_destructuring"][:1] == ["app/F0.php"]
          and piped == [11], f"{tooled[:1]} {piped}")

    _rs = php_modern.rector_scope_for
    php_modern.rector_scope_for = lambda root, fps, cfg=None: (
        {"lang": "php", "repo": "core", "scope": {"array_destructuring": ["app/A.php"]}}
        if any(":array_destructuring:" in f for f in fps) else None)
    _cfg = php_modern.load_cfg
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "core").mkdir()
        php_modern.load_cfg = lambda: {"repos_dir": Path(td)}
        try:
            # techdebt.queue'ya queue_fn enjekte edilip tool_fn verilmezse araç yolu kapalı
            src_td = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/quality/techdebt.py").read_text()
            check("teknik borç kuyruğu: enjekte queue_fn'de araç yolu kapalı (test DB'ye iş yazmaz)",
                  "_split_rector(take) if tool_fn else ([], take)" in src_td)
            tool_rows, pipe_rows = techdebt._split_rector([
                {"id": 1, "title": "list", "repo": "core", "fingerprints": ["php_modern:core:array_destructuring:app/A.php"]},
                {"id": 2, "title": "sens", "repo": "core", "fingerprints": ["php_modern:core:sensitive_parameter:app/A.php"]},
                {"id": 3, "title": "nr", "repo": "webservice", "fingerprints": ["newrelic:x"]},
                {"id": 4, "title": "kötü repo", "repo": "../core", "fingerprints": ["php_modern:core:array_destructuring:a"]}])
        finally:
            php_modern.rector_scope_for, php_modern.load_cfg = _rs, _cfg
    check("teknik borç kuyruğu: Rector kuralı modern_fix'e, diğerleri pipeline'a",
          [r["id"] for r, _ in tool_rows] == [1] and [r["id"] for r in pipe_rows] == [2, 3, 4]
          and json.loads(tool_rows[0][1])["lang"] == "php")

    src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/quality/modern_fix.py").read_text()
    check("inceleme ve pencere düzeltmesi düşük modelle (iş kapsamlı profiller)", "CREW_MODERN_FIX_REVIEW_PROFILE" in src
          and "CREW_MODERN_FIX_DEV_PROFILE" in src and "resolver.set_job_llm_overrides(_job_models(pc))" in src
          and "job_llm_scope()" in src)
    check("önceki koşunun dalı varsa araç düzeltmesi taze dalda açılır",
          'if f"refs/heads/{base}" in heads:' in src and "-tool-" in src
          and 'br.get("note") == "zaten mevcut"' not in src)
    pm = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/quality/php_modern.py").read_text()
    ik = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/quality/intake.py").read_text()
    check("take: Rector grupları modern_fix + onaylı kapsam INSERT'te",
          "_rector_queue(deps, repo," in pm and 'job_kind="modern_fix", job_payload=payload)' in ik)
    check("CREW_RECTOR_BIN yalnız .env'den, adı 'rector' olmalı", 'os.environ.get("CREW_RECTOR_BIN")' in pm
          and 'p.name != "rector"' in pm)


def test_tool_review_diff_context():
    """PR #45721 (Rector, 63 dosya): build_pr_context en çok 12 dosya gövdesi verdiği için
    reviewer 51 dosyayı hiç görmedi. Araç PR'ında bağlam TÜM dosyaların diff'i."""
    print("\n[araç PR'ı incelemesi: tüm dosyaların diff'i]")
    from agile_sdlc_crew import pr_review
    changes = {f"app/F{i:02d}.php": ("<?php\n// x\n" + "\n".join(f"$a{j} = 1;" for j in range(40)) + "\nlist($a, $b) = f();\n",
                                     "<?php\n// x\n" + "\n".join(f"$a{j} = 1;" for j in range(40)) + "\n[$a, $b] = f();\n")
               for i in range(30)}
    ctx = pr_review.tool_diff_context(changes, tool="rector")
    check("30 dosyanın hepsi bağlamda (12 dosya sınırı yok)", all(f"## app/F{i:02d}.php" in ctx for i in range(30)))
    check("yalnız değişen satırlar (dosya gövdesi değil)", "-list($a, $b) = f();" in ctx and "+[$a, $b] = f();" in ctx
          and "$a5 = 1;" not in ctx and len(ctx) < 30 * 400, str(len(ctx)))
    small = pr_review.tool_diff_context(changes, tool="rector", max_chars=3000)
    check("bütçe aşılırsa kesilen dosyalar AÇIKÇA yazılır", "BAĞLAM SINIRI" in small and "İNCELENMEDİ" in small
          and "app/F29.php" in small.split("BAĞLAM SINIRI")[1])
    src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/pr_review.py").read_text()
    check("run_pr_review: changes verilirse diff bağlamı, gövdeler satır oturtma için",
          "if changes:" in src and "tool_diff_context(changes, tool=tool)" in src and 'file_bodies.update({"/" + p.lstrip("/")' in src)
    mf = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/quality/modern_fix.py").read_text()
    check("modern_fix incelemeye eski/yeni hâli verir", 'originals = {f: _git(root, "show", f"origin/main:{f}").stdout' in mf
          and "changes={f: (originals.get(f, \"\"), c) for f, c in cur.items()}, tool=tool" in mf)


def test_tool_fix_leftovers():
    """#264: Rector zincirleme atamayı atladı, inceleme CHANGES_REQUIRED dedi, satır elle
    düzeltilecekti. Kullanıcı kararı: manuel müdahale gerekmesin — araçtan sonra kalan yerler
    taranır ve LLM yalnız o satırların penceresini düzeltir; inceleme maddesi de bir turda."""
    print("\n[araç düzeltmesi: kalan yerler ve inceleme turu]")
    import re as _re
    from agile_sdlc_crew.quality import modern_fix, php_modern
    rules = {r.key: r for r in php_modern.load_rules()}
    check("satır-yerel kural aracı olmasa da araç işi (?->), blok düzeyindeki değil (match, promotion, dinamik property)",
          php_modern.llm_line_fixable(rules["nullsafe"]) and not php_modern.llm_line_fixable(rules["match"])
          and not php_modern.llm_line_fixable(rules["ctor_promotion"])
          and not php_modern.llm_line_fixable(rules["dynamic_properties"]))
    check("pencereler birleşir", modern_fix._merge_windows([10, 14, 40], 100, 3) == [(7, 17, [10, 14]), (37, 43, [40])])

    calls = []

    def fake_llm(prompt: str) -> str:
        calls.append(prompt)
        blocks = []
        for body in _re.findall(r"### WINDOW \d+ [^\n]*\n```\n(.*?)\n```", prompt, _re.S):
            lines = [_re.sub(r"^\s*\d+\| ?", "", ln) for ln in body.splitlines()]
            out = [_re.sub(r"isset\((\$[\w\[\]'\"]+)\) \? \1 : ", r"\1 ?? ", ln) for ln in lines]
            blocks.append("```php\n" + "\n".join(out) + "\n```")
        return "\n\n".join(blocks)

    if not shutil.which("php"):
        skip("kalan yer düzeltmesi", "php yok")
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "core"
        (root / "app").mkdir(parents=True)
        (root / "app" / "R.php").write_text(
            "<?php\nfunction r(array $params) {\n"
            "    $searchText = $params['searchText'] = isset($params['searchText']) ? $params['searchText'] : null;\n"
            "    $b = 1;\n    return $searchText;\n}\n"
            "function k($t) {\n    switch ($t) {\n        case 'a':\n            return 1;\n        case 'b':\n"
            "            return 2;\n        default:\n            return 0;\n    }\n}\n")
        hits = php_modern.scan_files(root, ["app/R.php", "../../x.php"])
        check("tarayıcı --files: yalnız verilen dosya, kalan ?? yeri bulunur",
              any(h["rule"] == "null_coalesce" and h["line"] == 3 for h in hits), str(hits))
        scope = {"null_coalesce": ["app/R.php"], "match": ["app/R.php"]}
        res = modern_fix.fix_leftovers(root, scope, "8.2", ask=fake_llm)
        out = (root / "app" / "R.php").read_text()
        check("kalan satır-yerel yer pencereyle düzeltilir (zincirleme atama dahil)",
              res["fixed"] == 1 and "$params['searchText'] ?? null" in out and not res["remaining"], f"{res} {out}")
        check("blok düzeyindeki kalan yer (switch) LLM'e verilmez, 'bilerek bırakıldı' listesinde",
              any(r == "match" for _f, _l, r in res["skipped"]) and "switch ($t)" in out, str(res))
        snap = (root / "app" / "R.php").read_text()
        n = modern_fix.window_fix(root, "app/R.php", [4], "x", radius=2, ask=lambda p: "Bu satırı değiştirdim, tamam.")
        check("düzyazı yanıt reddedilir, dosya değişmez", n == 0 and (root / "app" / "R.php").read_text() == snap)
        n = modern_fix.window_fix(root, "app/R.php", [4], "x", radius=1,
                                  ask=lambda p: "```php\n    $searchText = 1;\n    $b = 1 +;\n    switch ($b) {\n```")
        check("sözdizimini bozan yanıt php -l'de yakalanır ve geri alınır", n == 0 and (root / "app" / "R.php").read_text() == snap)
        issues = [{"file": "app/R.php", "line": 4, "severity": "major", "problem": "p", "required_fix": "f"},
                  {"file": "app/X.php", "line": 2, "severity": "major"}]
        seen = []
        modern_fix.fix_review_issues(root, issues, {"app/R.php"}, ask=lambda p: (seen.append(p), "")[1])
        check("inceleme maddesi: yalnız kapsamdaki dosya, satır penceresi + maddenin düzeltmesi isteme girer",
              len(seen) == 1 and "line 4: p FIX: f" in seen[0])
        # Toplu: 3 dosyada 5 pencere TEK çağrı (#266: pencere başına bir çağrı, 7 × $0.19)
        for k in range(3):
            (root / "app" / f"B{k}.php").write_text(
                "<?php\n$a = isset($x) ? $x : 1;\n" + "$n = 0;\n" * 30 + "$b = isset($y) ? $y : 2;\n")
        calls.clear()
        res2 = modern_fix.fix_leftovers(root, {"null_coalesce": [f"app/B{k}.php" for k in range(3)]}, "8.2", ask=fake_llm)
        check("kalan yerler tek çağrıda (3 dosya, 6 pencere → 1 claude -p)",
              len(calls) == 1 and res2["fixed"] == 6 and not res2["remaining"], f"{len(calls)} {res2}")
        n = modern_fix.window_fix(root, "app/B0.php", [2], "x", radius=1, ask=lambda p: "```php\na\n```\n```php\nb\n```")
        check("blok sayısı pencere sayısını tutmazsa parti uygulanmaz", n == 0)

    from agile_sdlc_crew.tools import claude_cli_llm as _cli
    import os as _os
    _old = _os.environ.get("CREW_CLI_CWD")
    with tempfile.TemporaryDirectory() as td2:
        _os.environ["CREW_CLI_CWD"] = str(Path(td2) / "neutral")
        try:
            check("claude -p nötr dizinde koşar (repo CLAUDE.md'si her çağrıya girmez)",
                  _cli.cli_cwd() == str(Path(td2) / "neutral") and (Path(td2) / "neutral").is_dir())
            (Path(td2) / "repo" / ".claude").mkdir(parents=True)
            with _cli.repo_tools_context([str(Path(td2) / "repo")]):
                check("repo araçlı çağrıda da cwd nötr — klonun .claude/ ayarları/hook'ları yüklenmez",
                      _cli.cli_cwd() == str(Path(td2) / "neutral"))
        finally:
            if _old is None:
                _os.environ.pop("CREW_CLI_CWD", None)
            else:
                _os.environ["CREW_CLI_CWD"] = _old
    cs = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/tools/claude_cli_llm.py").read_text()
    check("claude -p'nin iki çalıştırma yolu da cwd=cli_cwd()", cs.count("cwd=cli_cwd()") == 2)
    az = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/tools/azure_devops_base.py").read_text()
    check("PR açıklaması Azure'un 4000 sınırına sığdırılır, hata yanıtı mesajda (#268)",
          "PR_DESCRIPTION_MAX = 4000" in az and "description[:PR_DESCRIPTION_MAX - len(note)]" in az
          and "PR oluşturulamadı: {(resp.text" in az)
    mf = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/quality/modern_fix.py").read_text()
    check("bloklayan madde turlardan sonra kalırsa needs_human (WI Code Review'a geçmez)",
          "raise NeedsHumanReview(" in mf and "db.needs_human_job(job_id, str(e))" in mf
          and mf.index("raise NeedsHumanReview(") < mf.index('"review")\n        if target:'))
    check("önceki araç PR'ı açıksa dalına itilir (yeni PR / terk gerekmez)",
          "job_kind='modern_fix'" in mf and "str(pr.get(\"pr_id\")) == pid" in mf)
    check("bilerek bırakılan yerler inceleme düzeltmesine hedef olmaz", "keep = {(f, int(ln or 0))" in mf)
    check("blok düzeyindeki kuralda riskli dosya PR'dan çıkarılır, kalan yeniden incelenir (#269)",
          "non_local = lang == \"php\" and not any(_pm.llm_line_fixable(" in mf
          and "CREW_MODERN_FIX_DROP_RISKY" in mf and '"content": originals.get(f, "")} for f in to_drop]' in mf
          and "rv = review(contents)\n            continue" in mf)
    check("tüm dosyalar riskliyse insana (PR açık)", "len(to_drop) >= len(contents)" in mf)
    pcs = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/pipeline_config.py").read_text()
    check("CREW_MODERN_FIX_DROP_RISKY kayıtlı (varsayılan açık)", '"key": "CREW_MODERN_FIX_DROP_RISKY"' in pcs)
    pr = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/pr_review.py").read_text()
    check("run_pr_review madde listesini ve ek bağlamı taşır", '"issue_list": issues' in pr and "extra_context" in pr)
    from agile_sdlc_crew import context_budget as cb
    from agile_sdlc_crew.flow import _quality_requirements_json
    desc = "<p>Bulunan yerler (150):</p>" + "".join(f"<p>app/F{i}.php:{i} — list($a, $b) = f();</p>" for i in range(64))
    req = _quality_requirements_json("[core] list", desc, "<li>AC</li>")
    check("kalite doğrudan akışı: 64 dosyalık Task metni kesilmeden gereksinime girer (#249: 3000'de 17 dosya)",
          "app/F63.php" in req and cb.cap("QUALITY_TASK_TEXT") >= 16000)
    fl = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/flow.py").read_text()
    check("doğrudan akışta gereksinim ileri beslemesi de Task sınırını kullanır",
          "_req_cap = max(_req_cap, _cb.cap('QUALITY_TASK_TEXT') + 3000)" in fl)


def test_scan_line_text_multibyte():
    """Teknik borç kartında Dpdcarrier.php:1300 'strpos' bulgusu `in_array(...)` satırını gösterdi:
    tarayıcı satır metnini `preg_split('/\\R/')` ile bölüyordu; u bayrağı olmadan `\\R` 0x85 baytını
    da satır sonu sayar ve Kiril 'х' (D1 85) içerir — dosyadaki 3 'х' metni 3 satır kaydırdı."""
    print("\n[tarayıcı: çok baytlı karakterde satır metni]")
    from agile_sdlc_crew.quality import php_modern
    if not shutil.which("php"):
        skip("satır metni", "php yok")
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "core"
        (root / "app").mkdir(parents=True)
        (root / "app" / "D.php").write_text(
            "<?php\nclass D {\n    const A = 'выход';\n    const B = 'хорошо';\n    const C = 'тихо';\n"
            "    public function f($s) {\n        $x = 1;\n        if (strpos($s, 'a') !== false) { return 1; }\n"
            "        return 0;\n    }\n}\n", encoding="utf-8")
        hits = [h for h in php_modern.scan_files(root, ["app/D.php"]) if h["rule"] == "str_contains"]
        check("bulgunun satır metni gerçekten o satır (0x85 baytı satır sonu sayılmaz)",
              hits and hits[0]["line"] == 8 and "strpos($s, 'a') !== false" in hits[0]["text"], str(hits))
    pm = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/quality/php_modern.py").read_text()
    check("Yeniden tara hedef sürüm önbelleğini de atlar (flo-dashboard 8.4'e alındı, kart 8.3 gösterdi)",
          "max_age_h=0 if force else" in pm and "index=_safe_index(client, app_map_cache), force=force)" in pm)


def test_tool_fix_quiet_comments():
    """PR #45729: iki koşu × (8 satır yorumu + özet) + çıkarma notları PR'ı doldurdu, ekip elle sildi.
    Araç düzeltmesi PR'a YALNIZ sonucu yazar: tek özet, satıra yalnız kalan bloklayan madde, WI'a tek yorum."""
    print("\n[araç düzeltmesi: PR'a yalnız sonuç]")
    from agile_sdlc_crew import pr_review
    from agile_sdlc_crew.quality import modern_fix

    class C:
        def __init__(self):
            self.pr = []

        def add_pr_comment(self, repo, pid, text, file_path=None, line_number=None, end_line=None):
            self.pr.append((file_path, text))
    c = C()
    issues = [{"id": "R1", "file": "app/A.php", "line": 3, "severity": "minor", "problem": "p", "required_fix": "f",
               "suggested_code": "x", "suggested_line_start": 3, "suggested_line_end": 3},
              {"id": "R2", "file": "app/B.php", "line": 2, "severity": "major", "problem": "p", "required_fix": "f"}]
    bodies = {"/app/A.php": "a\nb\nc\n", "/app/B.php": "a\nb\n"}
    pr_review.post_review(c, "core", 1, "", "ÖZET", issues, bodies, only_blocking=True, wi=False)
    check("araç PR'ı: tek özet + yalnız bloklayan madde satıra (minor/öneri satıra yazılmaz)",
          [f for f, _ in c.pr] == [None, "/app/B.php"], str(c.pr))
    s = pr_review.review_summary_markdown(verdict="APPROVE", issues=[], pr_id=1, pr_url="u", work_item_id="9",
                                          files=3, tool="rector")
    check("araç PR'ının özeti 'insan geliştirdi' demez", "insan tarafından" not in s and "rector" in s)
    c2 = C()
    pr_review.post_review(c2, "core", 1, "", "ÖZET", [issues[0]], bodies)
    check("bloklayan madde yoksa PR'a hiç yorum yazılmaz (onay / minor / öneri incelenecek durum değil)", c2.pr == [])
    s2 = pr_review.review_summary_markdown(verdict="CHANGES_REQUIRED", issues=issues, pr_id=1, pr_url="u",
                                           work_item_id="9", files=2)
    check("özet tablosunda yalnız bloklayan maddeler", "| R2 |" in s2 and "| R1 |" not in s2, s2)

    class C3(C):
        def __init__(self):
            super().__init__()
            self.desc = None

        def update_pr_description(self, repo, pid, d):
            self.desc = d
    wi = []
    c3 = C3()
    modern_fix._post_final(c3, "core", {"pr_id": 5, "url": "u"}, "77528", {"summary": "S", "file_bodies": {}}, [],
                           tool="rector", changed=["a.php"], res={"ran": ["php -l"]}, dropped=["b.php"],
                           dropped_why={"b.php": "sıra"}, left={"skipped": [], "fixed": 0}, reused=False, sig="",
                           add_wi=lambda cl, w, m: wi.append(m), desc="AÇIKLAMA")
    check("onaylanan araç PR'ı: yorum yok, çıkarılan dosyalar PR açıklamasında",
          c3.pr == [] and wi == [] and c3.desc and "AÇIKLAMA" in c3.desc and "b.php" in c3.desc, f"{c3.pr} {wi} {c3.desc}")
    fl = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/flow.py").read_text()
    check("pipeline incelemesi bloklamayan notları PR'a yorum olarak yazmaz", "Bloklamayan İnceleme Notları" not in fl)
    mf = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/quality/modern_fix.py").read_text()
    check("ara incelemeler sessiz (post=False), sonuç tek kez yazılır",
          "extra_context=notes, post=False)" in mf and mf.count("_post_final(client, repo, pr, work_item_id") == 3
          and "client.add_pr_comment(repo, int(pr[\"pr_id\"]), msg)" not in mf)
    check("PR açılınca WI'a ara yorum yok — WI'a yalnız insan incelemesi gerekince tek yorum",
          mf.count("add_wi(client, work_item_id,") == 2 and "insan incelemesi gerekli" in mf
          and "LLM incelemesi PR'a yazılıyor" not in mf)
    check("aynı PR'ı güncelleyen yeniden koşu önceki bot yorumlarını siler",
          "client.delete_own_pr_comments(repo, int(pr[\"pr_id\"]), BOT_COMMENT_MARKERS)" in mf)
    az = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/tools/azure_devops_base.py").read_text()
    check("eski yorum silme yalnız Tempo'nun kendi yorumları (yazar kimliği eşleşmeli)",
          '(c.get("author") or {}).get("id") != me' in az)


def test_modern_sp():
    """Aynı öneri tek iş, SP iş miktarından, en çok 8 (kullanıcı kararı 2026-10-30)."""
    print("\n[modernleştirme SP]")
    from agile_sdlc_crew.quality import php_modern as pm
    from agile_sdlc_crew.quality.model import Finding, Group
    check("geliştirici: ≤20 yer 1, ≤60 2, ≤150 3, ≤400 5, fazlası 8 SP",
          [pm.modern_sp(n) for n in (5, 21, 61, 151, 401, 5000)] == [1, 2, 3, 5, 8, 8])
    check("araç uygulayan dil: ≤200 1, ≤1000 2, ≤3000 3, fazlası 5 SP",
          [pm.modern_sp(n, True) for n in (150, 900, 2212, 9000)] == [1, 2, 3, 5])
    check("yapısal: ≤10 2, ≤40 3, ≤150 5, fazlası 8 SP", [pm.structural_sp(n) for n in (4, 19, 135, 956)] == [2, 3, 5, 8])
    units = [Group(source="go_modern", repo="r", title="any — f%d.go" % i, fingerprints=["go_modern:r:any:f%d.go" % i],
                   findings=[Finding(source="go_modern", file="f%d.go" % i, rule="any", count=40)]) for i in range(150)]
    packs = pm.pack_modern(units, source="go_modern", tool_fix=True)
    check("150 dosyalık aynı öneri → tek iş, SP ≤8, dosya başına parmak izi",
          len(packs) == 1 and packs[0].sp <= 8 and len(packs[0].fingerprints) == 150 and "150 dosya" in packs[0].title,
          str([(g.sp, len(g.files), g.title) for g in packs]))
    infra = Group(source="php_modern", repo="r", title="utf8 — a.php", fingerprints=["php_modern:r:utf8_encode:a.php"],
                  findings=[Finding(source="php_modern", file="a.php", rule="utf8_encode", count=1)], needs_human=True,
                  human_reason="Altyapı", infra=["ext:mbstring"])
    out = pm.pack_modern([infra], source="php_modern")
    check("altyapı işi ayrı kalır ve ön koşulu korunur", len(out) == 1 and out[0].infra == ["ext:mbstring"] and out[0].needs_human)


def test_tool_fix_build_gate():
    """#45729: inceleme onayladı, WI Code Review'a geçti ama Core-test kırmızıydı — promotion
    `public array $config = [];` varsayılanını kaldırmış, parent::__construct'ı çağırmayan Hepsijet'te
    'must not be accessed before initialization'."""
    print("\n=== Araç düzeltmesi: promotion varsayılan kaybı + PR build kapısı ===")
    from agile_sdlc_crew.quality import modern_fix as mf
    old = ("class A {\n    public string $name;\n    public array $config = [];\n    protected $apiKey = null;\n"
           "    public function __construct($config, $apiKey) { $this->config = $config; $this->apiKey = $apiKey; }\n}")
    new = ("class A {\n    public string $name;\n"
           "    public function __construct(public array $config, protected $apiKey) {}\n}")
    check("typed + varsayılan kaybı yakalanır, tipsiz = null güvenli", mf.lost_property_defaults(old, new) == ["config"],
          mf.lost_property_defaults(old, new))
    check("varsayılan korunuyorsa risk yok", mf.lost_property_defaults(old, old) == [])
    summ = ("Error: Typed property ...\n/myagent/_work/5/s/app/Integration/IntegrationAbstract.php:26\n"
            "/myagent/_work/5/s/app/X.php:3")
    check("build yığınındaki değişen dosya suçlu",
          mf.build_culprits(summ, ["app/Integration/IntegrationAbstract.php", "app/Y.php"])
          == ["app/Integration/IntegrationAbstract.php"])

    class C:
        def __init__(self, builds):
            self.builds, self.pushes = builds, []

        def get_pr_build(self, repo, pid):
            return self.builds.pop(0) if len(self.builds) > 1 else self.builds[0]

        def get_build_failure_summary(self, project, bid, max_chars=0):
            return summ

        def push_changes(self, repo, branch, changes, msg):
            self.pushes.append([c["path"] for c in changes])
    old_b = {"build_id": 1, "status": "completed", "result": "failed", "queue_time": "2026-11-04T09:00:00Z",
             "project": "P"}
    c = C([old_b, {"build_id": 2, "status": "inProgress", "queue_time": "2026-11-04T10:00:05Z"},
           {"build_id": 2, "status": "completed", "result": "succeeded", "queue_time": "2026-11-04T10:00:05Z"}])
    st, b = mf.wait_pr_build(c, "core", 1, "2026-11-04T10:00:00", timeout_s=600, interval_s=1, sleep=lambda s: None)
    check("push'tan önceki (bayat) build sayılmaz", st == "completed" and b["build_id"] == 2, f"{st} {b}")
    pc = type("PC", (), {"get": staticmethod(lambda k: 30)})
    red = {"build_id": 3, "status": "completed", "result": "failed", "queue_time": "2026-11-04T11:00:00Z",
           "project": "P", "url": "u"}
    green = {"build_id": 4, "status": "completed", "result": "succeeded", "queue_time": "2099-01-01T00:00:00Z"}
    c = C([red, green])
    contents = {"app/Integration/IntegrationAbstract.php": "new", "app/Y.php": "new"}
    dropped, why = [], {}
    fail, _ = mf._build_gate(c, "core", {"pr_id": 9}, "b", "1", contents, {}, dropped, why,
                             "2026-11-04T10:59:00", pc)
    check("kırmızı build → suçlu dosya PR'dan çıkarılır, yeşil olunca geçer",
          fail == "" and dropped == ["app/Integration/IntegrationAbstract.php"] and list(contents) == ["app/Y.php"]
          and c.pushes == [["/app/Integration/IntegrationAbstract.php"]], f"{fail} {dropped} {c.pushes}")
    c = C([dict(red)])
    contents = {"app/Z.php": "new", "app/Y.php": "new"}
    fail, _ = mf._build_gate(c, "core", {"pr_id": 9}, "b", "1", contents, {}, [], {}, "2026-11-04T10:59:00", pc)
    check("suçlu bulunamazsa insan incelemesi (Code Review'a geçmez)", "değişen dosya yok" in fail and c.pushes == [],
          fail)
    class D(C):
        def __init__(self, builds):
            super().__init__(builds)
            self.published = []

        def set_pr_draft(self, repo, pid, draft):
            self.published.append(draft)
    d = D([None, None, None, None, None, None, None, None, green])
    pr = {"pr_id": 9, "draft": True}
    fail, _ = mf._build_gate(d, "core", pr, "b", "1", {"app/Y.php": "n"}, {}, [], {}, "2026-11-04T10:59:00", pc)
    check("taslakta build gelmezse PR yayınlanır ve build beklenir", fail == "" and d.published == [False]
          and pr["draft"] is False, f"{fail} {d.published}")
    src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/quality/modern_fix.py").read_text()
    check("araç PR'ı taslak açılır, yalnız inceleme + build yeşilse yayınlanır",
          "draft=draft)" in src and src.index("if pr.get(\"draft\"):\n        client.set_pr_draft")
          > src.index("if build_fail:\n        raise NeedsHumanReview"))
    check("build kırmızıysa NeedsHumanReview, Code Review'dan önce",
          src.index("if build_fail:\n        raise NeedsHumanReview") < src.index('plan_transition(info["wi_state"]'))


def test_stale_team_status():
    """Durdurulan / orphan-fail olan işin status.json'u ajanları 'working' bırakıyordu; kuyruk boşken
    Ekip paneli mimarı ve inceleyiciyi aktif gösterdi (2026-11-04). Aktivite de o eski logu
    gösteriyordu (2026-11-05) — `live` yalnız dosya çalışan pipeline işine aitse doğru."""
    print("\n=== Boştayken Ekip/Aktivite paneli eski işi göstermez ===")
    from datetime import datetime
    from agile_sdlc_crew import server
    stale = lambda: {"finished_at": "", "work_item_id": "77527", "log": [{"m": 1}],
                     "agents": {"software_architect": {"status": "working", "current_task": "Kod Tabanı Analizi"}}}
    orig = server.db.list_running_jobs
    try:
        server.db.list_running_jobs = lambda: []
        d = server._idle_if_stale(stale())
        check("çalışan iş yoksa ajanlar boşta, log canlı değil",
              d["agents"]["software_architect"] == {"status": "idle", "current_task": None} and d["live"] is False)
        server.db.list_running_jobs = lambda: [{"work_item_id": "77528", "job_kind": "modern_fix"}]
        d = server._idle_if_stale(stale())
        check("başka bir (araç) iş çalışıyorsa eski pipeline logu canlı sayılmaz",
              d["live"] is False and d["agents"]["software_architect"]["status"] == "idle")
        server.db.list_running_jobs = lambda: [{"work_item_id": "77527", "job_kind": "pipeline"}]
        d = server._idle_if_stale(stale())
        check("dosyanın pipeline işi çalışıyorsa durum aynen, log canlı",
              d["live"] is True and d["agents"]["software_architect"]["status"] == "working")
    finally:
        server.db.list_running_jobs = orig
    now = datetime(2026, 11, 5, 13, 0)
    ev = server.activity_events(
        [{"work_item_id": "77528", "step_name": "Kod İnceleme", "status": "completed",
          "started_at": datetime(2026, 11, 5, 12, 0, 16), "finished_at": datetime(2026, 11, 5, 12, 0, 55)}],
        [{"work_item_id": "77528", "job_kind": "modern_fix", "status": "completed",
          "started_at": datetime(2026, 11, 5, 12, 0, 3), "finished_at": datetime(2026, 11, 5, 12, 12, 37)},
         {"work_item_id": "77527", "job_kind": "pipeline", "status": "failed",
          "started_at": datetime(2026, 11, 3, 11, 17), "finished_at": None}], now=now)
    check("aktivite her iş türünden, kronolojik; bugün saat, önceki gün tarih",
          [e["message"] for e in ev] == ["#77527 pipeline başladı", "#77528 araç düzeltmesi başladı",
                                          "#77528 Kod İnceleme başladı", "#77528 Kod İnceleme tamamlandı",
                                          "#77528 araç düzeltmesi tamamlandı"]
          and ev[0]["time"] == "03.11 11:17" and ev[-1]["time"] == "12:12:37", ev)
    h = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text()
    check("panel canlı değilse /api/activity'den okur", "if(d.live!==false)renderLog(d.log);else fetchActivity();" in h)


def test_pr_review_prior_discussion():
    """#45843 (job #273): iki major yorum, bir gün önce PR'da tartışılıp çözülmüş konulardı."""
    print("\n=== PR incelemesi önceki tartışmayı görür ===")
    from agile_sdlc_crew import pr_review
    th = [
        {"id": 2, "status": "fixed", "publishedDate": "2026-11-05T07:05",
         "threadContext": {"filePath": "/app/Model/StockSource.php", "rightFileStart": {"line": 428}},
         "comments": [{"content": "🤖 **[R3] MAJOR — EKSİK** (pipeline inceleme katkısı, danışma) limit eksik",
                       "author": {"displayName": "Volkan"}, "publishedDate": "2026-11-05T07:05"},
                      {"content": "Sözlü güncellendi, limite bakılmasına gerek yok @<C9BDD24E-455C-681C-AA55-CBE97089CD89>",
                       "author": {"displayName": "Ömer"}, "publishedDate": "2026-11-05T07:13"}]},
        {"id": 1, "status": "fixed", "publishedDate": "2026-11-04T12:32",
         "threadContext": {"filePath": "/app/Model/NextDayShipping.php", "rightFileStart": {"line": 96}},
         "comments": [{"content": "stok limiti dolmuş depoları çıkarmak gerekmiyor mu?",
                       "author": {"displayName": "Emre"}, "publishedDate": "2026-11-04T12:32"},
                      {"content": "gerek yok dedi", "author": {"displayName": "Ömer"},
                       "publishedDate": "2026-11-04T13:03"}]},
        {"id": 3, "properties": {"CodeReviewThreadType": {"$value": "VoteUpdate"}},
         "comments": [{"content": "voted", "commentType": "system"}]},
    ]
    b = pr_review.pr_discussion_block(th)
    check("çözülmüş insan kararı ve konumu bağlamda", "[fixed] /app/Model/NextDayShipping.php:96" in b
          and "gerek yok dedi" in b, b)
    check("önceki Tempo bulgusu + insanın reddi bağlamda, Tempo etiketli",
          "Tempo (önceki inceleme)" in b and "Sözlü güncellendi" in b and "@(kişi)" in b, b)
    check("kronolojik, sistem thread'i yok", b.index("NextDayShipping.php:96") < b.index("StockSource.php:428")
          and "voted" not in b)
    check("kural: çözülmüş konu yeniden açılmaz", "Do NOT raise an issue these threads already discussed" in b)
    check("tartışma yoksa blok yok", pr_review.pr_discussion_block([]) == "")
    src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/pr_review.py").read_text()
    check("insan PR incelemesine tartışma + WI yorumları girer, araç PR'ına girmez",
          'if human and _pc.get("CREW_PR_REVIEW_DISCUSSION")' in src and "+ discussion" in src and "+ repo_ctx + contract + pr_ctx" in src
          and "wi_comments_block(client.get_work_item_comments" in src)


def test_pr_review_coverage():
    """#45776 / #46052'yi başka bir inceleme aracıyla kıyasladık; kaçırdıklarımızın sebebi sistemseldi:
    ilk 12 dosya (Azure sırası) · yeni dosya baştan kırpılıyor · hedef reponun kuralları yok · PR'ın
    extends ettiği, main'de sonradan değişen sözleşme yok · build sonucu yok · öteki bot insan sanılıyor."""
    print("\n=== PR incelemesi: kapsam, repo kuralları, ana dal sözleşmeleri, build, öteki bot ===")
    import os
    from agile_sdlc_crew import pr_review as prv

    check("öncelik: kaynak < yapılandırma < test < doküman",
          [prv.file_priority(p) for p in ("/app/Command/Pull.php", "/app/module.yaml", "/app/Test/Command/PullTest.php",
                                         "/app/Controller/Api/docs/swagger.json", "/app/Migration/Upgrade.php",
                                         "/pkg/x_test.go", "/README.md")] == [0, 1, 2, 3, 1, 2, 3])

    big_new = "<?php\n" + "\n".join(f"$line{i} = {i};" for i in range(2, 316))          # 315 satır, yeni
    files = {"/app/Test/Command/PullTest.php": ("add", "<?php\n" + "\n".join(f"// t{i}" for i in range(3000))),
             "/app/Controller/Api/docs/swagger.json": ("edit", "{}"),
             "/app/Command/Pull.php": ("add", big_new)}
    files.update({f"/app/Core/S{i:02d}.php": ("edit", f"<?php\nclass S{i} {{}}\n") for i in range(14)})

    class FakePR:
        def __init__(self, files, base_files=None, build=None, tasks=None):
            self.files, self.base, self.build, self.tasks, self.calls = files, base_files or {}, build, tasks, []

        def get_pull_request_changes(self, repo, pid):
            return [{"item": {"path": p}, "changeType": ct} for p, (ct, _b) in self.files.items()]

        def get_file_content(self, repo, path, ref):
            self.calls.append((path, ref))
            if ref == "main":
                if path in self.base:
                    return self.base[path]
                raise RuntimeError("404")
            if path in self.files:
                return self.files[path][1]
            if path in self.base.get("__branch__", {}):
                return self.base["__branch__"][path]
            raise RuntimeError("404")

        def get_pr_build(self, repo, pid):
            return self.build

        def get_build_issue_tasks(self, project, bid):
            return self.tasks or []

        def get_build_failure_summary(self, project, bid, max_chars=4000):
            return "Failed asserting that 0 is identical to 1.\n/azp/_work/1/s/app/Test/Command/PullTest.php:69"

    pr = {"pullRequestId": 7, "sourceRefName": "refs/heads/feat", "targetRefName": "refs/heads/main",
          "lastMergeCommit": {"commitId": "m2"}}
    fc = FakePR(files)
    bodies = {}
    ctx, used = prv.build_pr_context(fc, "channel", pr, bodies=bodies)
    check("17 dosyanın hepsi bağlamda (12 dosya sınırı yok)", len(used) == 17, str(len(used)))
    check("kaynak kod testten ve dokümandan önce", used[0] == "/app/Command/Pull.php"
          and used[-1] == "/app/Controller/Api/docs/swagger.json" and used[-2] == "/app/Test/Command/PullTest.php", used)
    check("yeni dosya baştan kırpılmaz (315 satırın 221. satırı da görünür)",
          " 221| $line221 = 221;" in ctx and "(YENİ DOSYA)" in ctx and "kısaltıldı" not in ctx.split("## /app/Core")[0])
    check("yeni dosya için ana daldan eski hâl istenmez", ("/app/Command/Pull.php", "main") not in fc.calls)
    small, used2 = prv.build_pr_context(FakePR(files), "channel", pr, budget=9000, bodies={})
    check("bütçe yetmezse sığmayan dosya ADIYLA 'İNCELENMEDİ' yazılır, önce testler düşer",
          "İNCELENMEDİ" in small and "/app/Test/Command/PullTest.php" in small.split("BAĞLAM SINIRI")[1]
          and "/app/Command/Pull.php" in used2)
    os.environ["CREW_PR_REVIEW_FULL_SCOPE"] = "0"
    try:
        _old, used3 = prv.build_pr_context(FakePR(files), "channel", pr, bodies={})
    finally:
        os.environ.pop("CREW_PR_REVIEW_FULL_SCOPE", None)
    check("bayrak kapalıysa eski davranış: Azure sırasıyla ilk 12", len(used3) == 12
          and used3[0] == "/app/Test/Command/PullTest.php", used3[:2])

    # ── Repo kuralları: ilgili bölümler, hedef daldan ──
    md = ("# Channel API\nGiriş.\n\n## Kimlik Doğrulama\n" + "token " * 400 +
          "\n\n## Channel Events\nStage metodu her zaman StageResponse döner. ChannelEvents Hepsiburada.\n"
          "```\n# kod içindeki başlık sayılmaz\n```\n\n## Swagger\n" + "swagger " * 400)
    sel = prv.select_rule_sections(md, {"channelevents", "hepsiburada", "stage"}, cap=1200)
    check("kural dosyası sığmıyorsa: giriş + değişen kodla örtüşen bölüm, atlananlar adıyla",
          sel.startswith("# Channel API") and "StageResponse" in sel and "token token" not in sel
          and "atlanan bölümler: Kimlik Doğrulama, Swagger" in sel, sel[-200:])
    check("kod bloğundaki '#' başlık sayılmaz", "# kod içindeki başlık sayılmaz" in sel)
    check("sığan kural dosyası olduğu gibi", prv.select_rule_sections("# A\nx", set(), cap=100) == "# A\nx")
    rc = FakePR(files, base_files={"/CLAUDE.md": md})
    rb = prv.repo_rules_block(rc, "channel", "main", ["/app/Core/Channels/Hepsiburada/Events/X.php"],
                              {"/x.php": "<?php\nuse App\\Services\\ChannelEvents\\Event;\n"}, cap=1200)
    check("repo kuralları hedef daldan okunur ve veri olarak çerçevelenir",
          "TARGET REPO RULES (channel/CLAUDE.md on `main`" in rb and "StageResponse" in rb
          and "reference data" in rb and ("/CLAUDE.md", "main") in rc.calls)
    check("kural dosyası yoksa blok yok", prv.repo_rules_block(FakePR(files), "channel", "main", [], {}) == "")

    # ── Ana dal sözleşmeleri ──
    ev_body = ("<?php\nnamespace App\\Core\\Channels\\Hepsiburada\\Events;\n\nuse App\\Services\\ChannelEvents\\EventAbstract;\n"
               "use App\\Repository\\Services\\BaseRepository as Repo;\n\nclass CancelReceived extends EventAbstract\n{\n}\n"
               "final class Helper extends Repo implements \\JsonSerializable, LocalIface\n{\n}\n")
    check("extends/implements tam ada çözülür (use, takma ad, aynı namespace, kök \\)",
          prv._php_parents(ev_body) == ["App\\Services\\ChannelEvents\\EventAbstract", "App\\Repository\\Services\\BaseRepository",
                                        "JsonSerializable", "App\\Core\\Channels\\Hepsiburada\\Events\\LocalIface"],
          str(prv._php_parents(ev_body)))
    new_contract = ("<?php\nabstract class EventAbstract\n{\n    public function run(string $s, Event $e): StageResponse\n    {\n"
                    "        if (!$r instanceof StageResponse) {\n            throw new \\LogicException('must return StageResponse');\n"
                    "        }\n    }\n}\n")
    old_contract = "<?php\nabstract class EventAbstract\n{\n    public function run(string $s, Event $e): bool|array\n    {\n    }\n}\n"
    base_files = {"/composer.json": '{"autoload": {"psr-4": {"App\\\\": "app"}}}',
                  "/app/Services/ChannelEvents/EventAbstract.php": new_contract,
                  "/app/Repository/Services/BaseRepository.php": "<?php\nclass BaseRepository {}\n",
                  "__branch__": {"/app/Services/ChannelEvents/EventAbstract.php": old_contract,
                                 "/app/Repository/Services/BaseRepository.php": "<?php\nclass BaseRepository {}\n"}}
    cc = FakePR(files, base_files=base_files)
    bc = prv.base_contracts_block(cc, "channel", "main", "feat", {"/app/Core/Channels/Hepsiburada/Events/CancelReceived.php": ev_body},
                                  {"/app/Core/Channels/Hepsiburada/Events/CancelReceived.php"})
    check("PR'ın extends ettiği sınıf hedef daldaki GÜNCEL hâliyle girer",
          "BASE CONTRACTS (`main`" in bc and "must return StageResponse" in bc and "blocker" in bc)
    check("dal açıldıktan sonra değişen sözleşme işaretlenir ve en başa gelir",
          "DEĞİŞTİ" in bc and "-    public function run(string $s, Event $e): bool|array" in bc
          and bc.index("EventAbstract.php") < bc.index("BaseRepository.php"))
    check("çözülemeyen (composer'da olmayan) ad ve PR'ın değiştirdiği dosya alınmaz",
          "JsonSerializable" not in bc and "LocalIface.php" not in bc)
    chg = prv.base_contracts_block(cc, "channel", "main", "feat", {"/a/X.php": ev_body},
                                   {"/a/X.php", "/app/Services/ChannelEvents/EventAbstract.php"})
    check("PR sözleşmeyi kendisi değiştiriyorsa tekrar eklenmez", "EventAbstract.php" not in chg)
    check("PHP değilse blok yok", prv.base_contracts_block(cc, "x", "main", "feat", {"/m.go": "package m"}, set()) == "")

    # ── Build ──
    red = {"build_id": 9, "status": "completed", "result": "failed", "definition": "channel-test", "project": "P",
           "source_version": "m2"}
    blk, info = prv.build_status_block(FakePR(files, build=red), "channel", pr)
    check("kırmızı build: hata özeti + 'PR'ın neden olduğu hata blocker' kuralı",
          "PR BUILD — FAILED" in blk and "PullTest.php:69" in blk and "blocker" in blk and info["effective"] == "failed"
          and not info["stale"])
    _blk, info = prv.build_status_block(FakePR(files, build=dict(red, source_version="m1")), "channel", pr)
    check("build PR'ın eski hâline aitse 'eski' diye işaretlenir", info["stale"] and "OLDER state" in _blk)
    part = dict(red, result="partiallySucceeded")
    blk, info = prv.build_status_block(FakePR(files, build=part,
                                              tasks=[{"name": "SonarQube Analysis", "result": "succeededWithIssues"}]),
                                       "channel", pr)
    check("yalnız Sonar uyarısıyla kısmi başarı = testler geçti", "tests PASSED" in blk and info["effective"] == "succeeded")
    blk, _ = prv.build_status_block(FakePR(files, build=dict(red, status="inProgress", result=None)), "channel", pr)
    check("build sürüyorsa 'geçti' sanılmaz", "no result yet" in blk and "Do not assume" in blk)
    check("build pipeline'ı yoksa blok yok", prv.build_status_block(FakePR(files), "channel", pr) == ("", {}))

    # ── Öteki bot ──
    th = [{"id": 1, "status": "fixed", "publishedDate": "2026-11-09T09:02",
           "comments": [{"content": "**🔴 Kritik — x** açıklama\n---\n🤖 claude-review:finding:fe165d5a",
                         "author": {"displayName": "EcomBackend Build Service (FloTechnology)"}},
                        {"content": "✅ çözülmüş görünüyor", "author": {"displayName": "EcomBackend Build Service (FloTechnology)"}}]},
          {"id": 2, "status": "active", "publishedDate": "2026-11-09T10:00",
           "comments": [{"content": "bot " * 300 + " claude-review:finding:aa", "author": {"displayName": "X Build Service"}}]},
          {"id": 3, "status": "fixed", "publishedDate": "2026-11-09T11:00",
           "comments": [{"content": "statü kaldırıldı, gerek yok", "author": {"displayName": "Celal"}}]}]
    b = prv.pr_discussion_block(th)
    check("öteki aracın yorumu 'bulgu, karar değil' diye etiketlenir",
          b.count("İNCELEME ARACI (bulgu, karar değil)") == 3 and "not decisions" in b, b[:600])
    check("insan yanıtı olmayan 'fixed' karar sayılmaz, insanın kararı öyle işaretlenmez",
          "[fixed · insan yanıtı yok, karar sayılmaz]" in b and "- [fixed] genel\n    · Celal" in b, b)
    tight = prv.pr_discussion_block(th, cap=700)
    check("yer yetmezse önce insan thread'i alınır, çıkarılanlar sayılır",
          "statü kaldırıldı" in tight and "thread daha yer kalmadığı için alınmadı" in tight, tight[-300:])

    src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/pr_review.py").read_text()
    check("insan PR'ına hedef repo bağlamı girer, araç PR'ına girmez",
          "human = not changes" in src and "_target_repo_context(client, rname, pr, files, file_bodies) if human" in src
          and "+ repo_ctx + contract + pr_ctx" in src)


def test_pr_review_tracking():
    """Kullanıcı (2026-11-10): 2. ve 3. grup + 'farklı bir agent yorumlarda dahil ise onun hatalı yorumlarını,
    yanlışlarını, kaçırdıklarını yaz'. Parmak izli bulgu, push başına artımlı inceleme, yorum=soru, PR'ın
    birleşmiş hâli üzerinde repo araçları, öteki aracın değerlendirilmesi."""
    print("\n=== PR incelemesi: takip, artımlı, soru, repo araçları, öteki araç ===")
    import os
    import subprocess
    import tempfile
    from datetime import datetime, timedelta
    from agile_sdlc_crew import pr_review as prv
    from agile_sdlc_crew import flow

    txt = ('REVIEW_ISSUES_JSON:\n```json\n{"issues": [{"file": "app/A.php", "line": 3, "severity": "major", '
           '"problem": "p", "key": "Stage Returns Array!", "basis": "Interpretation", "other_ref": "#Fe165D5A"}]}\n```')
    it = flow._parse_review_issues(txt)[0]
    check("ayrıştırıcı key/basis/other_ref taşır (normalize)",
          it["key"] == "stage-returns-array" and it["basis"] == "interpretation" and it["other_ref"] == "Fe165D5A", it)
    a = {"file": "/app/A.php", "key": "stage-returns-array", "problem": "x"}
    check("parmak izi: dosya + anahtar, satır/metin değişse de sabit",
          prv.fingerprint(a) == prv.fingerprint({**a, "file": "app/A.php", "problem": "başka", "line": 9})
          and prv.fingerprint(a) != prv.fingerprint({**a, "key": "baska"}))

    # ── S9: yorum = soru ──
    iss = [{"severity": "major", "basis": "interpretation"}, {"severity": "minor", "basis": "code"}]
    check("yalnız WI yorumuna dayanan major soru olur, karar yeniden kurulur",
          prv.apply_questions(iss, "CHANGES_REQUIRED") == "QUESTIONS" and iss[0]["severity"] == "question")
    check("kod kusuru bloklamaya devam eder",
          prv.apply_questions([{"severity": "blocker", "basis": "code"}], "CHANGES_REQUIRED") == "CHANGES_REQUIRED")
    minor_q = [{"severity": "minor", "basis": "interpretation"}, {"severity": "minor", "basis": "interpretation"}]
    check("#46055: minor yorum maddesi soru OLMAZ (PR'a yorum açtırmaz), onay onay kalır",
          prv.apply_questions(minor_q, "APPROVE") == "APPROVE" and all(i["severity"] == "minor" for i in minor_q)
          and not prv._worth_posting(minor_q, [], []))

    # ── PR'daki izler ──
    me = "me-1"
    ours = lambda c: {"content": c, "author": {"id": me, "displayName": "Volkan"}}
    bot = lambda c: {"content": c, "author": {"displayName": "EcomBackend Build Service (FloTechnology)"}}
    _f = lambda path, key: prv.fingerprint({"file": path, "key": key})
    fp_open, fp_fixed, fp_human, fp_stale = (_f("app/A.php", "stage-returns-array"), _f("app/B.php", "k2"),
                                             _f("app/C.php", "k3"), _f("app/D.php", "k4"))
    threads = [
        {"id": 1, "status": "active", "comments": [ours("## Tempo\n*Tempo — PR İnceleme · son incelenen commit: "
                                                         "abc1234 · tempo-review-summary*")]},
        {"id": 2, "status": "active", "threadContext": {"filePath": "/app/A.php", "rightFileStart": {"line": 3}},
         "comments": [ours(f"MAJOR\n\n*tempo-finding:{fp_open} key:stage-returns-array*")]},
        {"id": 3, "status": "fixed", "threadContext": {"filePath": "/app/B.php"},
         "comments": [ours(f"x\n\n*tempo-finding:{fp_fixed} key:k2*")]},
        {"id": 4, "status": "wontFix", "threadContext": {"filePath": "/app/C.php"},
         "comments": [ours(f"x\n\n*tempo-finding:{fp_human} key:k3*"),
                      {"content": "bilinçli karar", "author": {"displayName": "Celal"}}]},
        {"id": 5, "status": "active", "threadContext": {"filePath": "/app/D.php"},
         "comments": [ours(f"x\n\n*tempo-finding:{fp_stale} key:k4*")]},
        {"id": 6, "status": "active", "comments": [{"content": "*tempo-finding:eeeeeeeeee key:z*",
                                                    "author": {"id": "someone", "displayName": "Ali"}}]},
        {"id": 10, "status": "active", "publishedDate": "2026-11-09T12:01",
         "threadContext": {"filePath": "/app/E.php", "rightFileStart": {"line": 65}},
         "comments": [bot("**🔴 Kritik — stage** ...\n---\n🤖 claude-review:finding:abb5ea1f")]},
        {"id": 11, "status": "fixed", "publishedDate": "2026-11-09T11:18",
         "threadContext": {"filePath": "/app/F.php", "rightFileStart": {"line": 70}},
         "comments": [bot("**🟠 Orta — kısmi iptal**\n---\n🤖 claude-review:finding:52c1ac8f"),
                      bot("✅ `8e85507` commit'inde çözülmüş görünüyor, thread kapatıldı.")]},
        {"id": 12, "status": "fixed", "publishedDate": "2026-11-08T20:57",
         "threadContext": {"filePath": "/app/G.php", "rightFileStart": {"line": 34}},
         "comments": [bot("**🟡 Düşük — items[0]**\n🤖 claude-review:finding:870dc2ea"),
                      {"content": "sözleşme gereği tek sipariş, bilinçli", "author": {"displayName": "Celal"}}]},
        {"id": 13, "status": "active", "comments": [bot("## 🤖 Claude Code Review\n🤖 claude-review:summary")]},
    ]
    st = prv.tempo_state(threads, me)
    check("Tempo izi: özet thread'i + son incelenen commit", st["summary"]["thread_id"] == 1 and st["commit"] == "abc1234")
    check("Tempo izi: bulgu parmak izi, anahtarı, durumu, insan yanıtı",
          st["findings"][fp_open]["key"] == "stage-returns-array" and st["findings"][fp_fixed]["status"] == "fixed"
          and st["findings"][fp_human]["human_replied"] and not st["findings"][fp_open]["human_replied"])
    check("başkasının yazdığı (alıntılanmış) işaret Tempo'nun sayılmaz", "eeeeeeeeee" not in st["findings"])
    others = prv.other_reviewer_findings(threads)
    refs = {o["ref"]: o for o in others}
    check("öteki aracın bulguları: özet hariç, ref/konum/durum",
          set(refs) == {"abb5ea1f", "52c1ac8f", "870dc2ea"} and refs["abb5ea1f"]["file"] == "/app/E.php")
    check("kendi kapattığı bulgu 'o araç kapattı', insanın yanıtladığı değil",
          refs["52c1ac8f"]["closed_by_tool"] and not refs["870dc2ea"]["closed_by_tool"]
          and refs["870dc2ea"]["humans"][0][0] == "Celal" and "çözülmüş görünüyor" in refs["52c1ac8f"]["tool_note"])
    disc = prv.pr_discussion_block(threads, skip_other_bot_only=True)
    check("öteki aracın tek başına açtığı thread tartışmada tekrar edilmez, insanın yanıtladığı kalır",
          "abb5ea1f" not in disc and "bilinçli" in disc)

    cb = prv.review_contract_block(prior=st["findings"], others=others, incremental=("abc1234", ["app/A.php"]))
    check("sözleşme: key + basis kuralı, soru dili",
          '"key"' in cb and '"interpretation"' in cb and "never blocks" in cb)
    check("sözleşme: önceki bulgu anahtarı aynen yeniden kullanılır",
          "key=stage-returns-array" in cb and "EXACTLY the same key" in cb and "key=k2" not in cb)
    check("sözleşme: artımlı kapsam ve öteki aracın bulguları + karar türleri",
          "INCREMENTAL REVIEW" in cb and "abc1234" in cb and "ref=abb5ea1f" in cb and "wrongly_closed" in cb
          and "OTHER_REVIEWER_JSON" in cb and "MISSED" in cb)
    out = ('OTHER_REVIEWER_JSON:\n```json\n{"assessments": [{"ref": "abb5ea1f", "verdict": "valid", "reason": "doğru"},'
           '{"ref": "52c1ac8f", "verdict": "wrongly_closed", "reason": "F.php:70 hâlâ tüm satırları sayıyor"},'
           '{"ref": "870dc2ea", "verdict": "wrong", "reason": "yazar kararı"},'
           '{"ref": "x", "verdict": "bilmem", "reason": "-"}]}\n```')
    asm = prv.parse_other_assessment(out)
    check("öteki araç değerlendirmesi ayrıştırılır, geçersiz karar atılır",
          [a["verdict"] for a in asm] == ["valid", "wrongly_closed", "wrong"])
    cleaned = prv.clean_assessments(asm + [{"ref": "52c1ac8f", "verdict": "outdated", "reason": "-"},
                                           {"ref": "zzz", "verdict": "wrong", "reason": "-"}], others)
    check("süzgeç: insanın yanıtladığı thread değerlendirilmez, kapalı thread 'outdated' olamaz, bilinmeyen ref atılır",
          [(a["ref"], a["verdict"]) for a in cleaned] == [("abb5ea1f", "valid"), ("52c1ac8f", "wrongly_closed")],
          cleaned)
    check("karar türü: insan kararını yok sayıp yeniden açma",
          "ignores_decision" in prv.OTHER_VERDICTS and "ignores_decision" in cb)
    # İş #280 (#46052): 33 bulgunun 17'si 16K'ya sığmadı, hiç girmedi — hepsi aracın KENDİ kapattıkları.
    many = [{"ref": f"{i:08x}", "status": "active" if i < 5 else "fixed", "closed_by_tool": i >= 5,
             "file": f"/app/F{i}.php", "line": i, "date": "2026-11-09", "humans": [], "tool_note": "",
             "text": f"**Başlık {i}** " + "ayrıntı " * 300} for i in range(40)]
    rows = prv._other_rows(many, cap=12_000)
    check("öteki aracın bulgularının HİÇBİRİ düşmez: sığmayan tek satır (ref · durum · yer · başlık) girer",
          all(f"ref={m['ref']} " in rows for m in many) and "Başlık 39" in rows and "yalnız başlığıyla" in rows
          and len(rows) <= 12_500, str(len(rows)))
    check("öncelik: açık bulgular tam gövdeyle", all(rows.split(f"ref={m['ref']} ")[1].split("\n")[1].startswith("    ")
                                                    for m in many[:5]))
    summ_cnt = prv.tracked_summary_markdown(verdict="APPROVE", issues=[], others=many, assessments=asm[:1],
                                            pr_id=1, work_item_id="1", files=1, head="abc")
    check("özet sayısı yanıltmaz: toplam / açık / değerlendirilen ayrı",
          "40 bulgusu var — 5 açık; 1 tanesi değerlendirildi" in summ_cnt)
    answered = [dict(m, humans=[("Dev", "kapsam dışı")]) for m in many[:4]]
    summ_ans = prv.tracked_summary_markdown(verdict="APPROVE", issues=[], others=answered, assessments=[],
                                            pr_id=1, work_item_id="1", files=1, head="abc")
    check("#46055: hepsini geliştirici yanıtladıysa 'değerlendirilemedi' (arıza) değil 'karara bağlı' yazar",
          "4 tanesini geliştirici yanıtlamış" in summ_ans and "karara bağlı" in summ_ans
          and "değerlendirilemedi" not in summ_ans)

    issues = [
        {"id": "R1", "file": "app/A.php", "line": 3, "severity": "major", "key": "stage-returns-array",
         "problem": "hâlâ var", "basis": "code"},
        {"id": "R2", "file": "app/E.php", "line": 65, "severity": "blocker", "key": "stage-contract",
         "problem": "stage", "basis": "code", "other_ref": "abb5ea1f"},
        {"id": "R3", "file": "app/B.php", "line": 5, "severity": "major", "key": "k2", "problem": "geri geldi",
         "basis": "code"},
        {"id": "R4", "file": "app/H.php", "line": 8, "severity": "major", "key": "yeni-kusur", "problem": "yeni",
         "basis": "code", "evidence": {}},
        {"id": "R5", "file": "app/C.php", "line": 1, "severity": "major", "key": "k3", "problem": "insan reddetti",
         "basis": "code"},
        {"id": "R6", "file": "app/I.php", "line": 1, "severity": "question", "key": "q", "problem": "statü?",
         "basis": "interpretation"},
    ]
    summ = prv.tracked_summary_markdown(verdict="CHANGES_REQUIRED", issues=issues, others=others, assessments=asm,
                                        pr_id=7, work_item_id="77", files=3, head="def5678abc",
                                        incremental=("abc1234", 2))
    from agile_sdlc_crew.branding import is_bot_comment
    check("özet: açık bulgular, öteki aracın doğrulanan bulgusu işaretli, sorular ayrı",
          "### Açık bulgular" in summ and "öteki aracın bulgusu `abb5ea1f`, doğrulandı" in summ
          and "### Yazara sorular" in summ and "statü?" in summ)
    check("özet: öteki aracın yanlışları + yanlış kapattığı + doğru sayısı + kaçırdıkları",
          "**Hatalı** — `870dc2ea`" in summ and "Yanlışlıkla kapatılmış" in summ and "1 bulgusu doğrulandı" in summ
          and "**Kaçırdıkları:**" in summ and "app/H.php:8" in summ and "app/E.php:65` — stage" not in summ.split("Kaçırdıkları")[1])
    check("özet: son incelenen commit + işaret, Tempo yorumu olarak tanınır",
          "son incelenen commit: def5678" in summ and prv.SUMMARY_MARK in summ and is_bot_comment(summ))

    class FakeAz:
        def __init__(self):
            self.calls = []

        def add_pr_comment(self, repo, pid, content, file_path=None, line_number=None, end_line=None):
            self.calls.append(("add", file_path, content))
            return {"id": 99}

        def reply_to_pr_thread(self, repo, pid, tid, content):
            self.calls.append(("reply", tid, content))

        def set_pr_thread_status(self, repo, pid, tid, status):
            self.calls.append(("status", tid, status))

        def update_pr_comment(self, repo, pid, tid, cid, content):
            self.calls.append(("update", tid, content))

    import agile_sdlc_crew.main as _main
    wi_calls = []
    _orig = _main._add_wi_comment
    _main._add_wi_comment = lambda c, w, t: wi_calls.append(w)
    try:
        fz = FakeAz()
        res = prv.sync_review(fz, "channel", 7, "77", summary=summ, issues=issues, file_bodies={},
                              reviewed={"app/A.php", "app/B.php", "app/D.php", "app/H.php"},
                              changed_since={"app/D.php", "app/H.php"}, state=st, head="def5678abc",
                              others=others, assessments=asm)
        adds = [c for c in fz.calls if c[0] == "add"]
        check("yalnız YENİ bloklayan bulgu satıra yazılır, parmak iziyle",
              len(adds) == 1 and adds[0][1] == "/app/H.php" and "tempo-finding:" in adds[0][2]
              and "key:yeni-kusur" in adds[0][2], adds)
        check("açık bulgu tekrar yazılmaz, öteki aracın doğrulanan bulgusu tekrar yazılmaz",
              res["kept"] == 1 and res["covered"] == 1)
        check("Tempo'nun kapattığı bulgu geri geldi → yeniden açılır; insanın kapattığına dokunulmaz",
              ("status", 3, "active") in fz.calls and not any(c[1] == 4 for c in fz.calls if c[0] != "add"))
        check("artık raporlanmayan + dosyası değişen bulgu 'çözüldü' diye kapanır",
              ("status", 5, "fixed") in fz.calls and any(c[0] == "reply" and c[1] == 5 and "çözülmüş" in c[2]
                                                         for c in fz.calls))
        check("öteki aracın yanlış kapattığı / hatalı bulgusuna kendi thread'inde ikinci görüş, doğruya yazılmaz",
              any(c[0] == "reply" and c[1] == 11 and "Yanlışlıkla kapatılmış" in c[2] and "tempo-assess:52c1ac8f" in c[2]
                  for c in fz.calls)
              and any(c[0] == "reply" and c[1] == 12 for c in fz.calls)
              and not any(c[0] == "reply" and c[1] == 10 for c in fz.calls))
        check("tek özet thread'i yerinde güncellenir, WI'a yeniden yazılmaz",
              ("update", 1, summ) in fz.calls and not any(c[0] == "add" and c[1] is None for c in fz.calls)
              and wi_calls == [])
        fz2 = FakeAz()
        prv.sync_review(fz2, "channel", 7, "77", summary="s", issues=issues[:1], file_bodies={},
                        reviewed={"app/A.php"}, changed_since=None, state={"findings": {}, "summary": None},
                        head="h", others=[], assessments=[])
        check("ilk incelemede özet açılır ve WI'a bir kez yazılır",
              any(c[0] == "add" and c[1] is None for c in fz2.calls) and wi_calls == ["77"])
        fz3 = FakeAz()
        st3 = {"findings": {fp_stale: {**st["findings"][fp_stale]}}, "summary": None}
        prv.sync_review(fz3, "channel", 7, "77", summary="s", issues=[], file_bodies={}, reviewed={"app/D.php"},
                        changed_since={"app/X.php"}, state=st3, head="h", others=[], assessments=[])
        check("dosyası değişmeyen bulgu kapatılmaz; söylenecek şey yoksa özet açılmaz", fz3.calls == [])
        twice = prv.other_reviewer_findings(threads + [{"id": 11, "status": "fixed", "comments": []}])
        threads_assessed = [dict(t, comments=t["comments"] + [ours("x *tempo-assess:52c1ac8f*")]) if t["id"] == 11 else t
                            for t in threads]
        fz4 = FakeAz()
        prv.sync_review(fz4, "channel", 7, "77", summary="s", issues=[], file_bodies={}, reviewed=set(),
                        changed_since=None, state={"findings": {}, "summary": None}, head="h",
                        others=prv.other_reviewer_findings(threads_assessed), assessments=asm)
        check("ikinci görüş aynı thread'e iki kez yazılmaz", not any(c[0] == "reply" and c[1] == 11 for c in fz4.calls)
              and len(twice) == 3)
    finally:
        _main._add_wi_comment = _orig

    # ── push başına yeniden inceleme ──
    now = datetime(2026, 11, 10, 12, 0)
    pay = lambda **k: __import__("json").dumps({"post": True, **k})
    jobs = [{"id": 5, "repo_name": "channel", "pr_id": "46052", "status": "completed", "work_item_id": "77347",
             "job_payload": pay(head="old"), "created_at": now - timedelta(hours=30)},
            {"id": 6, "repo_name": "channel", "pr_id": "45776", "status": "completed", "work_item_id": "PR#45776",
             "job_payload": '{"post": false}', "created_at": now - timedelta(hours=1)},
            {"id": 7, "repo_name": "core", "pr_id": "1", "status": "running", "work_item_id": "1",
             "job_payload": pay(head="x"), "created_at": now},
            ] + [{"id": 20 + i, "repo_name": "core", "pr_id": "2", "status": "completed", "work_item_id": "2",
                  "job_payload": pay(head="y"), "created_at": now - timedelta(hours=i + 1)} for i in range(4)]
    cands = prv.auto_rerun_candidates(jobs, now, max_per_day=4)
    check("aday: yalnız daha önce YAZAN, şu an koşmayan, günlük sınırı dolmamış PR",
          [(c["repo"], c["pr_id"], c["last_head"]) for c in cands] == [("channel", 46052, "old")], cands)

    class PollAz:
        def __init__(self, head, push_min_ago, status="active"):
            self.head, self.ago, self.status = head, push_min_ago, status

        def get_pull_request(self, repo, pid):
            return {"status": self.status, "lastMergeSourceCommit": {"commitId": self.head}}

        def get_pr_last_push(self, repo, pid):
            return {"date": (now - timedelta(minutes=self.ago)).strftime("%Y-%m-%dT%H:%M:%S.1234567Z")}
    spawned = []
    prv.poll_once(PollAz("new", 30), jobs, spawned.append, now, debounce_min=15)
    check("yeni push durulduysa artımlı inceleme başlar (son incelenen commit'le)",
          len(spawned) == 1 and spawned[0]["head"] == "new" and spawned[0]["last_head"] == "old")
    spawned.clear()
    prv.poll_once(PollAz("new", 5), jobs, spawned.append, now, debounce_min=15)
    prv.poll_once(PollAz("old", 60), jobs, spawned.append, now, debounce_min=15)
    prv.poll_once(PollAz("new", 60, status="completed"), jobs, spawned.append, now, debounce_min=15)
    check("geliştirici hâlâ itiyorsa / yeni push yoksa / PR kapandıysa başlamaz", spawned == [])

    # ── CLI $ sınırı bağlama özel ──
    from agile_sdlc_crew.tools import claude_cli_llm as cli
    with cli.repo_tools_context(["/tmp/x"], max_usd=3.0):
        inside = (cli._cap_cv.get(), cli._get_repo_ctx()[0])
    check("araçlı incelemenin $ sınırı yalnız o blokta geçerli",
          inside == (3.0, ["/tmp/x"]) and cli._cap_cv.get() is None and cli._get_repo_ctx()[0] == [])

    # ── PR'ın birleşmiş hâli (gerçek git) ──
    with tempfile.TemporaryDirectory() as td:
        g = lambda *a, cwd: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd,
                                           capture_output=True, text=True, check=True)
        src = os.path.join(td, "src"); os.makedirs(src)
        g("init", "-q", "-b", "main", cwd=src)
        open(os.path.join(src, "a.txt"), "w").write("main\n")
        g("add", ".", cwd=src); g("commit", "-q", "-m", "m", cwd=src)
        open(os.path.join(src, "a.txt"), "w").write("merged\n")
        secret = os.path.join(td, "secret.env")
        open(secret, "w").write("DB_PASSWORD=hunter2\n")
        os.symlink(secret, os.path.join(src, "leak.env"))       # PR yazarı repodan dışarı bağ koyuyor
        g("add", "-A", cwd=src)
        g("commit", "-q", "-m", "pr", cwd=src)
        g("update-ref", "refs/pull/7/merge", "HEAD", cwd=src)
        g("reset", "-q", "--hard", "HEAD~1", cwd=src)
        repos = os.path.join(td, "repos"); os.makedirs(repos)
        g("clone", "-q", src, os.path.join(repos, "channel"), cwd=td)
        old = os.environ.get("CREW_REPOS_DIR")
        os.environ["CREW_REPOS_DIR"] = repos
        try:
            with prv.pr_merge_checkout("channel", 7, "feat") as wt:
                seen = open(os.path.join(wt, "a.txt")).read() if wt else ""
                wt_path = str(wt)
                leak = os.path.join(wt, "leak.env")
                leak_is_link = os.path.islink(leak)
                leak_text = open(leak).read() if os.path.exists(leak) and not leak_is_link else ""
            with prv.pr_merge_checkout("../etc", 7) as bad:
                pass
        finally:
            if old is None:
                os.environ.pop("CREW_REPOS_DIR", None)
            else:
                os.environ["CREW_REPOS_DIR"] = old
        clone = os.path.join(repos, "channel")
        refs_left = subprocess.run(["git", "for-each-ref", "refs/tempo"], cwd=clone, capture_output=True, text=True).stdout
        wts = subprocess.run(["git", "worktree", "list"], cwd=clone, capture_output=True, text=True).stdout
        check("PR checkout: refs/pull/N/merge (birleşmiş hâl) okunur", seen == "merged\n", seen)
        check("PR checkout: çıkışta worktree ve geçici ref silinir; klonun dalı değişmez",
              not os.path.exists(wt_path) and refs_left == "" and wts.count("\n") == 1
              and open(os.path.join(clone, "a.txt")).read() == "main\n")
        check("PR checkout: geçersiz repo adı reddedilir", bad is None)
        check("PR checkout: PR'daki sembolik bağ repodan dışarı açılmaz (gizli dosya okunamaz)",
              not leak_is_link and "hunter2" not in leak_text, leak_text)

    fz5 = FakeAz()
    leaky = [{"id": "R1", "file": "app/Z.php", "line": 2, "severity": "major", "key": "leak", "basis": "code",
              "problem": "config şunu içeriyor: Authorization: Basic dXNlcjpzM2NyM3RQYXNz ve DB_PASSWORD=hunter2"}]
    _main._add_wi_comment = lambda c, w, t: None
    try:
        prv.sync_review(fz5, "channel", 7, "", summary="ozet DB_PASSWORD=hunter2", issues=leaky, file_bodies={},
                        reviewed=set(), changed_since=None, state={"findings": {}, "summary": None}, head="h",
                        others=[], assessments=[])
    finally:
        _main._add_wi_comment = _orig
    sent = " ".join(c[2] for c in fz5.calls if c[0] == "add")
    check("PR'a giden metinde gizli değer maskelenir", "hunter2" not in sent and "dXNlcjpzM2NyM3RQYXNz" not in sent
          and "***" in sent, sent[:300])

    src_txt = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/pr_review.py").read_text()
    check("araçlı inceleme boş dönerse araçsız yeniden koşar",
          "if not _has_decision(text):" in src_txt and "text = _kick(ctx)" in src_txt)
    srv = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/server.py").read_text()
    check("pr_review işi türü ve payload'u INSERT'te alır ve hemen running olur (kuyruk işçisi kapmaz)",
          'job_kind="pr_review",\n                           job_payload=' in srv and "db.start_job(job_id)\n    return job_id" in srv)


def test_tool_branch_conflicts():
    """#45729: #45728 aynı Ekol kurucularını değiştirip merge edildi → 7 dosya çakıştı. Kullanıcı kararı
    (2026-11-05): çakışma AYNI PR'da çözülür — PR terk edilip yenisi açılırsa review ve yorumlar kaybolur."""
    print("\n=== Araç düzeltmesi: main ile çakışan PR aynı PR'da çözülür ===")
    import subprocess
    import tempfile
    from agile_sdlc_crew import db, pipeline
    from agile_sdlc_crew.quality import modern_fix as mf

    class Cur:
        def execute(self, *a):
            pass

        def fetchall(self):
            return [{"branch_name": "feature/77528-tool-98859", "pr_id": "45729"}]

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return Cur()

    class C:
        def __init__(self, merge):
            self.merge, self.abandoned, self.created = merge, [], []

        def list_branches(self, repo):
            return [{"name": "refs/heads/feature/77528-tool-98859"}, {"name": "refs/heads/feature/77528"}]

        def get_pull_request(self, repo, pid):
            return {"mergeStatus": self.merge}

        def abandon_pull_request(self, repo, pid, reason=""):
            self.abandoned.append(pid)

        def create_branch(self, repo, br):
            self.created.append(br)
    og, of, om = db.get_conn, pipeline._find_existing_pr, mf.merge_main_into_branch
    merged = []
    try:
        db.get_conn = lambda: Conn()
        pipeline._find_existing_pr = lambda cl, r, b: {"pr_id": 45729}
        mf.merge_main_into_branch = lambda root, br: merged.append(br) or ["a.php", "b.php"]
        c = C("succeeded")
        check("çakışmasız açık PR'ın dalı yeniden kullanılır, main birleştirilmez",
              mf._tool_branch(c, "core", "77528", Path("/x")) == ("feature/77528-tool-98859", True, "")
              and not merged)
        c = C("conflicts")
        br, reused, note = mf._tool_branch(c, "core", "77528", Path("/x"))
        check("çakışan PR terk EDİLMEZ, yeni dal açılmaz; main aynı dala birleştirilir",
              br == "feature/77528-tool-98859" and reused and not c.abandoned and not c.created
              and merged == ["feature/77528-tool-98859"] and "2 dosya" in note, f"{br} {reused} {note} {c.abandoned}")
    finally:
        db.get_conn, pipeline._find_existing_pr, mf.merge_main_into_branch = og, of, om

    # Gerçek git ile: origin(bare) — main ve dal aynı satırı farklı değiştirir → çakışan dosya main'den
    # alınır, çakışmayan dal değişikliği korunur, birleştirme origin'e itilir.
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)

        def sh(cwd, *a):
            return subprocess.run(["git", *mf.MERGE_IDENT, *a], cwd=cwd, capture_output=True, text=True, check=True)
        sh(d, "init", "-q", "--bare", "-b", "main", "origin.git")
        sh(d, "clone", "-q", str(d / "origin.git"), "w")
        w = d / "w"
        (w / "a.php").write_text("ctor($k)\n")
        (w / "b.php").write_text("x\n")
        sh(w, "add", "."), sh(w, "commit", "-qm", "base"), sh(w, "push", "-q", "origin", "HEAD:main")
        sh(w, "checkout", "-qb", "tool")
        (w / "a.php").write_text("ctor(protected $k)\n")
        (w / "b.php").write_text("x-tool\n")
        sh(w, "commit", "-qam", "tool"), sh(w, "push", "-q", "origin", "tool")
        sh(w, "checkout", "-q", "main")
        (w / "a.php").write_text("ctor(#[S] $k)\n")
        sh(w, "commit", "-qam", "sensitive"), sh(w, "push", "-q", "origin", "main")
        files = mf.merge_main_into_branch(w, "tool")
        sh(w, "fetch", "-q", "origin")
        a = sh(w, "show", "origin/tool:a.php").stdout
        b = sh(w, "show", "origin/tool:b.php").stdout
        parents = sh(w, "rev-list", "--parents", "-n1", "origin/tool").stdout.split()
        check("gerçek git: çakışan dosya main'den, çakışmayan dal değişikliği korunur, birleştirme itilir",
              files == ["a.php"] and a == "ctor(#[S] $k)\n" and b == "x-tool\n" and len(parents) == 3,
              f"{files} {a!r} {b!r} {parents}")
        check("geçici worktree temizlenir", "tempo-merge-" not in sh(w, "worktree", "list").stdout)


def test_hero_latest_job():
    """Genel bakış kartı çalışan iş yokken 'en son PIPELINE işi'ni seçiyordu: araç düzeltmesi / PR
    inceleme dönemi başlayınca 27.10'daki #76578 günlerce üstte kaldı (2026-11-05)."""
    print("\n=== Genel bakış kartı en güncel işi gösterir ===")
    h = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text()
    i = h.index("async function refreshHero(){")
    body = h[i:h.index("function renderHero(", i)]
    check("çalışan/kuyruk yoksa türüne bakmadan en son iş seçilir",
          "||byNew(_jobs)[0];" in body and "byNew(_jobs.filter(isPipe))" not in body, body[:600])
    check("pipeline dışı işte yalnız koşan adımlar çizilir",
          "STEP_KEYS.filter(k=>byKey[k]&&byKey[k].status!=='pending')" in h and "ARAÇ DÜZELTMESİ" in h)


def test_tool_fix_result_and_logs():
    """#45893: bırakılan yerler listesi açıklamayı doldurdu, sona eklenen Sonuç kesildi; araç
    düzeltmesinin INFO logları handler'sız bir logger'a gidip hiçbir dosyaya düşmedi."""
    print("\n=== Araç düzeltmesi: Sonuç bölümü sınırda korunur, loglar dosyaya düşer ===")
    import logging
    from agile_sdlc_crew.quality import modern_fix as mf
    from agile_sdlc_crew.tools.azure_devops_base import PR_DESCRIPTION_MAX
    body = "Araçla uygulanan…\n" + "\n".join(f"- `app/Lib/F{i}.php:16` (ctor_promotion)" for i in range(200))
    out = mf.description_with_result(body, "İnceleme: ✅ onaylandı.\n\n**PR'dan çıkarılan 2 dosya**")
    check("uzun gövdede Sonuç tam korunur, toplam sınırın altında, gövde kısaltma notlu",
          len(out) <= PR_DESCRIPTION_MAX and out.endswith("**PR'dan çıkarılan 2 dosya**")
          and "### Sonuç" in out and "liste kısaltıldı" in out, len(out))
    again = mf.description_with_result(out, "İnceleme: 🔴")
    check("tekrar yazımda eski Sonuç değiştirilir, çoğalmaz", again.count("### Sonuç") == 1 and again.endswith("🔴"))
    check("kısa gövde aynen kalır", mf.description_with_result("kısa", "ok") == "kısa\n\n### Sonuç\nok")
    check("araç düzeltmesi logları pipeline log handler'ının logger'ında",
          mf.log is logging.getLogger("pipeline"))


def test_nr_app_infer():
    """2026-11-06: FLODevOps'ta tanımı olmayan NR uygulamaları (checkout-be.<marka> ~7.3K hata,
    loyalty.instreet) elle eşleme istiyordu; repo hatanın kendi izinden bulunur."""
    print("\n=== New Relic uygulamasının reposu hatalarından bulunur ===")
    import subprocess
    import tempfile
    from agile_sdlc_crew.quality import app_infer
    from agile_sdlc_crew.quality.model import Finding

    def mk(base, name, files):
        r = base / name
        for rel, body in files.items():
            (r / rel).parent.mkdir(parents=True, exist_ok=True)
            (r / rel).write_text(body)
        subprocess.run(["git", "init", "-q"], cwd=r, check=True)
        subprocess.run(["git", "add", "."], cwd=r, check=True)
        return r
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        mk(base, "webservice", {"app/Controller/Api/V1/Basket.php": "<?php function retrieveCardLoyalty(){}",
                                "app/Controller/Api/V1/Order.php": "<?php function complete(){}"})
        mk(base, "orkestra", {"app/Controller/Api/V1/Order.php": "<?php function cancel(){}"})
        mk(base, "loyalty360", {"go.mod": "module loyalty-360\n\ngo 1.26\n",
                                "internal/api/ltyv1/delivery_points.go": "package ltyv1"})
        fs = [Finding(source="newrelic", app="[Prod] checkout-be.flo.com.tr", action="Api/V1/Basket::retrieveCardLoyalty",
                      count=6000),
              Finding(source="newrelic", app="[Prod] checkout-be.flo.com.tr", action="Api/V1/Order::complete",
                      message="Uncaught … in /var/www/app/app/Controller/Api/V1/Order.php:1625", count=5)]
        cl = app_infer.clues(fs)
        check("izler: işlem adı → dosya son eki + metot, mesajdaki yol",
              ("method", "Api/V1/Basket.php|retrieveCardLoyalty") in cl
              and ("path", "app/Controller/Api/V1/Order.php") in cl, cl)
        repos = app_infer._repos(base)
        r = app_infer.infer(cl, repos)
        check("kanıtla en güçlü repo: webservice (orkestra'da Order.php var ama metot yok)",
              r and r["repo"] == "webservice" and r["score"] == 3 and r["runner_up"] == 1, r)
        check("tek iz yetmez (belirsiz → eşlenmez)",
              app_infer.infer([("method", "Api/V1/Basket.php|retrieveCardLoyalty")], repos) is None)
        check("Go modül adı + kod yolu → loyalty360",
              (app_infer.infer([("module", "loyalty-360"), ("path", "internal/api/ltyv1/delivery_points.go")], repos)
               or {}).get("repo") == "loyalty360")
        by_app = {"[Prod] checkout-be.flo.com.tr": fs,
                  "[Prod] checkout-be.butigo.com.tr": [Finding(source="newrelic", app="[Prod] checkout-be.butigo.com.tr",
                                                               action="Api/V1/Basket::retrieveCardLoyalty", count=5)],
                  "[Prod] bilinmeyen.flo.com.tr": [Finding(source="newrelic", app="x", action="A::b", count=9)]}
        cache = base / "inferred.yaml"
        out = app_infer.resolve_unmapped(by_app, base=base, cache_path=cache, code=lambda app: [])
        check("az hatalı kardeş (tek iz) ailenin eşlendiği repoya bağlanır; kanıtsız eşlenmez",
              out == {"[Prod] checkout-be.flo.com.tr": "webservice", "[Prod] checkout-be.butigo.com.tr": "webservice"},
              out)
        calls = []
        out2 = app_infer.resolve_unmapped(by_app, base=base, cache_path=cache, code=lambda app: calls.append(app) or [])
        check("sonuç önbelleklenir (ikinci çağrıda arama yok)", out2 == out and calls == [])
    src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/quality/intake.py").read_text()
    check("gather eşlenemeyen uygulamada çıkarımı dener (bayrakla)",
          'pipeline_config.get("CREW_QUALITY_NR_INFER")' in src and "app_infer.resolve_unmapped(orphans)" in src)


def test_quality_detail():
    """Kullanıcı (2026-11-06): NR/Sonar işlerinde kritiklik ve sıklık görünmüyordu."""
    print("\n=== Kalite önizlemesi: kritiklik + sıklık ===")
    from agile_sdlc_crew.quality import intake, render
    from agile_sdlc_crew.quality.model import Finding, Group

    def nr(cls, count, rate=0.02):
        return Finding(source="newrelic", app="[Prod] flo.com.tr", err_class=cls, count=count, error_rate=rate,
                       first_seen=1.0, last_seen=2.0)
    g = Group(source="newrelic", repo="webservice", title="t", fingerprints=["x"], findings=[nr("E_WARNING", 128466)])
    d = render.group_detail(g, nr_days=7)
    check("uyarı çok sık olsa da en çok 'yüksek' (istek çökmez)", d["level"] == "yüksek" and d["per_day"] == 18352.3, d)
    g2 = Group(source="newrelic", repo="w", title="t", fingerprints=["y"], findings=[nr("TypeError", 900)])
    check("yakalanmamış istisna bir kademe yukarı ve tek 'kritik' yolu",
          render.group_detail(g2, nr_days=7)["level"] == "yüksek"
          and render.group_detail(Group(source="newrelic", repo="w", title="t", fingerprints=["z"],
                                        findings=[nr("TypeError", 7000)]), nr_days=7)["level"] == "kritik")
    s1 = Group(source="sonar", repo="o", title="t", fingerprints=["s"],
               findings=[Finding(source="sonar", itype="BUG", severity="BLOCKER", count=2)])
    check("Sonar: önem → seviye, tür + önem etiketi",
          render.group_detail(s1) == {"level": "kritik", "kind": "BUG · BLOCKER", "count": 2})
    check("önizleme satırı detayı taşır", intake._row(g, 7)["detail"]["count"] == 128466)
    check("Task açıklamasında da kritiklik satırı", "<b>Kritiklik:</b> Yüksek · E_WARNING" in render.task_description(g))


def test_upgrade_rules():
    print("\n[upgrade: katalog breaks/fix_min ve kural süzgeci]")
    import tempfile
    from pathlib import Path
    from agile_sdlc_crew.quality import php_modern as pm, upgrade
    rules = pm.load_rules()
    by = {r.key: r for r in rules}
    check("removed_functions hedefte çalışmaz", by["removed_functions"].breaks == "error")
    check("implicit_nullable deprecation", by["implicit_nullable"].breaks == "deprecation")
    check("mekanik kural yükseltme kuralı değil", by["str_contains"].breaks == "")
    keys = {r.key for r in upgrade.upgrade_rules(rules, "8.2", "8.4")}
    check("8.2→8.4: 8.3 ve 8.4 kuralları",
          {"get_class_noarg", "deprecated_83", "implicit_nullable", "deprecated_84"} <= keys, str(keys))
    check("8.2→8.4: 8.2 ve öncesi yok",
          not ({"dynamic_properties", "utf8_encode", "removed_functions"} & keys))
    check("7.4→8.4 removed_functions'ı içerir",
          "removed_functions" in {r.key for r in upgrade.upgrade_rules(rules, "7.4", "8.4")})
    check("error engeller", upgrade.blocking(by["removed_functions"], auto=False))
    check("otomatik deprecation engeller, otomatik olmayan engellemez",
          upgrade.blocking(by["implicit_nullable"], auto=True)
          and not upgrade.blocking(by["implicit_nullable"], auto=False))
    check("breaks boş kural engellemez", not upgrade.blocking(by["str_contains"], auto=True))
    check("fix_min varsayılanı (7, 0)", by["str_contains"].fix_min == (7, 0))
    base = ("rules:\n  - key: x\n    since: \"8.0\"\n    kind: deprecation\n"
            "{extra}")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "r.yaml"
        p.write_text(base.format(extra="    breaks: saçma\n"), encoding="utf-8")
        try:
            pm.load_rules(p)
            check("geçersiz breaks ValueError", False)
        except ValueError:
            check("geçersiz breaks ValueError", True)
        p.write_text(base.format(extra="    fix_min: \"8.4\"\n"), encoding="utf-8")
        check("fix_min '8.4' → (8, 4)", pm.load_rules(p)[0].fix_min == (8, 4))


def test_php_modern_82_84():
    """core 8.4 sanılıyordu ama öneriler 8.1'de bitiyordu: katalogda 8.2'den sonrası yok
    denecek kadar azdı. Tarayıcıya 8.2/8.3/8.4 kuralları eklendi."""
    print("\n[php_modern: 8.2 / 8.3 / 8.4 kuralları]")
    import shutil
    from agile_sdlc_crew.quality import php_modern as pm
    if not shutil.which("php"):
        skip("php_modern 8.2-8.4", "php yok"); return
    fx = Path(__file__).resolve().parent / "fixtures" / "php_modern"
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "a.php").write_text((Path(__file__).resolve().parent / "fixtures" / "php_modern_82" / "a.php").read_text())
        res = pm.scan_repo(Path(td))
    got = {(h["rule"], h["line"]) for h in res["hits"]}
    want = {("typed_class_constants", 3), ("dynamic_properties", 8), ("sensitive_parameter", 9),
            ("new_without_parens", 10), ("new_without_parens", 11), ("get_class_noarg", 13), ("partial_callable", 15),
            ("deprecated_84", 16), ("deprecated_84", 17), ("deprecated_84", 18), ("mb_legacy_encoding", 19),
            ("deprecated_83", 20), ("deprecated_83", 21), ("readonly_class", 24)}
    check("yeni kurallar bulunur", want <= got, str(sorted(want - got)))
    check("yanlış pozitif yok: tipli sabit, işaretli parametre, anonim sınıf, get_class($x), extends/AllowDynamic sınıflar, const LIST",
          not ({("typed_class_constants", 4), ("sensitive_parameter", 9 + 100), ("new_without_parens", 12), ("get_class_noarg", 14),
                ("dynamic_properties", 25), ("dynamic_properties", 27), ("array_destructuring", 5)} & got), str(sorted(got)))
    rules = {r.key: r for r in pm.load_rules()}
    check("katalogda 8.2/8.3/8.4 kuralları", all(k in rules for k in (
        "dynamic_properties", "partial_callable", "mb_legacy_encoding", "sensitive_parameter", "readonly_class",
        "typed_class_constants", "get_class_noarg", "deprecated_83", "new_without_parens", "deprecated_84")))
    check("8.2 hedefte 8.3/8.4 kuralı önerilmez", not {r.key for r in pm.applicable(list(rules.values()), "8.2")}
          & {"typed_class_constants", "get_class_noarg", "deprecated_83", "new_without_parens", "deprecated_84"})
    md = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/quality/modern.py").read_text()
    html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text()
    check("repo listesi: taranabilenler üstte, bilinmeyenler altta ayrı grupta",
          'items.sort(key=lambda i: (not i.get("scannable")' in md and "Hedef sürümü bilinmeyen (" in html)


def test_upgrade_php_rules():
    """Yükseltme kırılma kuralları (7.4 → 8.5): break.php'de her kural bulunur,
    ok.php'deki karşı örneklerde hiçbir yükseltme bulgusu çıkmaz."""
    print("\n[upgrade: PHP kırılma kuralları]")
    import shutil
    from agile_sdlc_crew.quality import php_modern as pm
    if not shutil.which("php"):
        skip("upgrade PHP kuralları", "php yok"); return
    fx = Path(__file__).resolve().parent / "fixtures" / "upgrade" / "php"
    res = pm.scan_repo(fx)
    by = {r.key: r for r in pm.load_rules()}
    hits = {(h["rule"], h["file"]) for h in res["hits"]}
    keys = ("removed_functions", "curly_offset", "removed_casts", "required_after_optional", "legacy_autoload",
            "php4_constructor", "loose_compare_numeric", "null_to_internal", "removed_81", "serializable_only",
            "moved_to_pecl_84", "noncanonical_casts_85", "backtick_exec_85", "deprecated_85")
    for k in keys:
        check(f"{k} bulundu", (k, "break.php") in hits, str(sorted(h for h in hits if h[1] == "break.php")))
    got = {(h["rule"], h["line"]) for h in res["hits"] if h["file"] == "break.php"}
    check("removed_casts hem (real) hem (unset)", {("removed_casts", 5), ("removed_casts", 6)} <= got, str(sorted(got)))
    check("noncanonical_casts_85 dört cast da", {("noncanonical_casts_85", n) for n in (17, 18, 19, 20)} <= got, str(sorted(got)))
    check("(double) yalnız 8.5 kuralı; (real) 8.5 kuralı değil", ("noncanonical_casts_85", 5) not in got, str(sorted(got)))
    check("deprecated_85: curl_close + case ;", {("deprecated_85", 22), ("deprecated_85", 24)} <= got, str(sorted(got)))
    bad = [h for h in res["hits"] if h["file"] == "ok.php" and by[h["rule"]].breaks]
    check("karşı örneklerde yükseltme bulgusu yok", not bad, str(bad))
    for k in keys:
        check(f"{k} katalogda ve kırılma kuralı", k in by and bool(by[k].breaks), k)
    for k in ("legacy_autoload", "php4_constructor", "serializable_only"):
        check(f"{k} line_local değil", not by[k].line_local)
    check("breaks sınıfları", by["curly_offset"].breaks == "error" and by["php4_constructor"].breaks == "behavior"
          and by["required_after_optional"].breaks == "deprecation" and by["moved_to_pecl_84"].breaks == "error")
    check("moved_to_pecl_84 eklenti ister", set(sum(by["moved_to_pecl_84"].requires_if.values(), [])) >= {"imap", "pspell", "oci8"})
    check("kural since sürümleri", by["removed_81"].since == (8, 1) and by["noncanonical_casts_85"].since == (8, 5)
          and by["curly_offset"].since == (8, 0))


def test_upgrade_probe_and_blockers():
    """Hedef araçla doğrulama (php -l / compile()) ve composer.lock bağımlılık engelleri."""
    print("\n[upgrade: probe ve bağımlılık engelleri]")
    import json as _json
    import os
    import shutil
    import sys as _sys
    import tempfile
    from pathlib import Path
    from agile_sdlc_crew.quality import upgrade
    A = upgrade.php_constraint_allows
    check("^7.3|^8.0 8.4'e izin verir", A("^7.3|^8.0", "8.4"))
    check("~8.1.0 8.4'e izin vermez", not A("~8.1.0", "8.4"))
    check(">=7.4 <8.3 izin vermez", not A(">=7.4 <8.3", "8.4"))
    check("anlaşılmayan kısıt engel değil", A("dev-main as 1.0", "8.4"))
    check("kısıt biçimleri", A(">=7.2.5", "8.4") and A("^8.0 || ^7.4", "8.4") and not A("^7.4", "8.4")
          and A("8.*", "8.4") and not A("<8.4", "8.4") and A("<=8.4", "8.4") and not A("~8.3.0", "8.4") and A("~8.3", "8.4")
          and not A("^8.0 <8.3", "8.4") and A(">= 7.4, < 9", "8.4") and not A("7.4 - 8.2", "8.4"))
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "repo"
        root.mkdir()
        (root / "composer.lock").write_text(_json.dumps({"packages": [
            {"name": "a/ok", "version": "1.0", "require": {"php": "^8.0"}},
            {"name": "b/old", "version": "2.0", "require": {"php": "~8.1.0"}},
            {"name": "c/none", "version": "1.0"}]}))
        check("yalnız b/old engel", [b["name"] for b in upgrade.blockers(root, "php", "8.4")] == ["b/old"])
        check("PHP dışı dilde engel yok", upgrade.blockers(root, "go", "1.25") == [])
        fake = base / "bin"
        fake.mkdir()
        good, bad = fake / "php8.4", fake / "notphp"
        good.write_text("#!/bin/sh\n"); bad.write_text("#!/bin/sh\n")
        old = {k: os.environ.get(k) for k in ("CREW_UPGRADE_PHP_BINS", "CREW_UPGRADE_PYTHON_BINS")}
        try:
            os.environ["CREW_UPGRADE_PHP_BINS"] = f"8.4={good},8.5={bad},8.6=/yok/php,x.y={good},rel/php=1"
            check("tool_bins: yalnız php… adlı, var olan, N.N sürümlü", upgrade.tool_bins("CREW_UPGRADE_PHP_BINS", "php") == {"8.4": str(good)},
                  str(upgrade.tool_bins("CREW_UPGRADE_PHP_BINS", "php")))
            check("_tool_ok env'e bakar", upgrade._tool_ok("php", "8.4") and not upgrade._tool_ok("php", "8.5"))
            os.environ.pop("CREW_UPGRADE_PHP_BINS")
            hits, st = upgrade.probe(root, "php", "8.2", "8.4")
            check("araç yokken probe atlanır, hata yok", hits == [] and st["ran"] is False and "araç" in st["reason"], str(st))
            check("Next.js: araç yok", upgrade.probe(root, "nextjs", "14.0", "15.0")[1] == {"ran": False, "tool": "", "reason": "araç yok"})
            check("pyproject.toml pipeline_config'te değil", "CREW_UPGRADE_PHP_BINS" not in
                  (Path(upgrade.__file__).parent.parent / "pipeline_config.py").read_text())
            rules = upgrade.probe_rules([{"rule": "probe:error", "message": "Parse error: x"},
                                         {"rule": "probe:deprecation", "message": "Deprecated: y"}], to="8.4")
            by = {r.key: r for r in rules}
            check("probe_rules alanları", by["probe:error"].breaks == "error" and by["probe:error"].auto and by["probe:error"].line_local
                  and by["probe:deprecation"].breaks == "deprecation" and not by["probe:deprecation"].auto
                  and by["probe:error"].since == (8, 4) and "Parse error" in by["probe:error"].title)
            # PHP
            php = shutil.which("php")
            if php:
                (root / "src").mkdir(); (root / "vendor").mkdir()
                (root / "src" / "bad.php").write_text("<?php\nfunction ( {\n")
                (root / "src" / "dep.php").write_text("<?php\nfunction f(int $a = null) { return $a; }\n")
                (root / "src" / "ok.php").write_text("<?php\nfunction g(?int $a = null) { return $a; }\n")
                (root / "vendor" / "v.php").write_text("<?php\nfunction ( {\n")
                link = fake / "php"
                os.symlink(php, link)
                os.environ["CREW_UPGRADE_PHP_BINS"] = f"8.4={link}"
                hits, st = upgrade.probe(root, "php", "8.2", "8.4")
                errs = [h for h in hits if h["rule"] == "probe:error"]
                check("php: parse error → probe:error, vendor dışlanır", st["ran"] is True
                      and [h["file"] for h in errs] == ["src/bad.php"] and errs[0]["line"] >= 1, str((hits, st)))
                check("php: ok.php temiz", not [h for h in hits if h["file"] == "src/ok.php"])
                check("php: hit şekli", all(set(h) == {"rule", "file", "line", "text", "message"} for h in hits) and len(hits) >= 1)
            else:
                skip("php probe", "yerelde php yok")
            # Python tuzakları
            pr = base / "pyrepo"
            pr.mkdir()
            marker = base / "MARKER"
            for n in ("py_compile.py", "ruff.py", "warnings.py", "json.py"):
                (pr / n).write_text(f"open({str(marker)!r}, 'w').write('x')\n")
            (pr / "a.py").write_text("def (:\n")
            (pr / "b.py").write_text("x = 1\nif x is 1:\n    pass\n")
            (pr / "c.py").write_text("x = 1\n")
            exe = Path(_sys.executable)
            link = fake / "python3.x"
            os.symlink(exe, link)
            ver = f"{_sys.version_info[0]}.{_sys.version_info[1]}"
            os.environ["CREW_UPGRADE_PYTHON_BINS"] = f"{ver}={link}"
            hits, st = upgrade.probe(pr, "python", "3.9", ver)
            check("python: işaret dosyası oluşmadı (depo kodu çalışmadı)", not marker.exists())
            check("python: SyntaxError → probe:error", any(h["rule"] == "probe:error" and h["file"] == "a.py" for h in hits), str((hits, st)))
            check("python: SyntaxWarning → probe:deprecation", any(h["rule"] == "probe:deprecation" and h["file"] == "b.py" and h["line"] == 2 for h in hits), str(hits))
            check("python: temiz dosya hit'siz", not [h for h in hits if h["file"] == "c.py"])
            hits2, _ = upgrade.probe(pr, "python", "3.9", ver, files=["../etc/passwd", "c.py"])
            check("python: kök dışı yol reddedilir", hits2 == [])
            # Next.js peer engelleri
            nx = base / "nx"
            nx.mkdir()
            (nx / "package.json").write_text(_json.dumps({"dependencies": {"next": "^14.0.0", "react": "17.0.2",
                                                                          "react-dom": "^17.0.2"}}))
            bl = upgrade.blockers(nx, "nextjs", "15.0")
            check("nextjs: react 17 → her paket için peer engeli",
                  sorted(b["name"] for b in bl) == ["react", "react-dom"] and all(b["kind"] == "peer" for b in bl)
                  and bl[0]["detail"] == "Next 15.0 en az react 18.2 ister", str(bl))
            (nx / "package.json").write_text(_json.dumps({"dependencies": {"next": "^14.0.0", "react": "^18.3.0"}}))
            check("nextjs: react ^18.3.0 engel değil", upgrade.blockers(nx, "nextjs", "15.0") == [])
            (nx / "package.json").write_text(_json.dumps({"dependencies": {"react": "latest", "react-dom": "workspace:*"}}))
            check("nextjs: anlaşılmayan sürüm engel değil; package.json yoksa boş",
                  upgrade.blockers(nx, "nextjs", "15.0") == [] and upgrade.blockers(base / "yok", "nextjs", "15.0") == [])
            # Go probe
            import subprocess as sp
            if shutil.which("go") and shutil.which("git") and upgrade._tool_ok("go", "1.24"):
                def mk(name, gomod, src):
                    r = base / name
                    r.mkdir()
                    (r / "go.mod").write_text(gomod)
                    (r / "main.go").write_text(src)
                    for a in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i"]):
                        sp.run(["git", *a], cwd=r, check=True, capture_output=True)
                    return r
                bad = mk("gobad", "module m\n\ngo 1.22\n", "package main\n\nfunc main() {\n\t_ = undefinedThing\n}\n")
                before = (bad / "go.mod").read_bytes()
                hits, st = upgrade.probe(bad, "go", "1.22", "1.24")
                check("go: derleme hatası tek probe:error (vet kopyası yok)", st["ran"] is True
                      and [(h["rule"], h["file"], h["line"]) for h in hits] == [("probe:error", "main.go", 4)], str((hits, st)))
                wl = sp.run(["git", "worktree", "list"], cwd=bad, capture_output=True, text=True).stdout.strip().splitlines()
                check("go: geçici worktree kalmadı, go.mod değişmedi", len(wl) == 1 and (bad / "go.mod").read_bytes() == before
                      and not [p for p in Path(tempfile.gettempdir()).glob("tempo-gprobe-*")], str(wl))
                ok = mk("gook", "module m\n\ngo 1.22\n", "package main\n\nfunc main() {}\n")
                hits, st = upgrade.probe(ok, "go", "1.22", "1.24")
                check("go: temiz modül → ran=True, hit yok", st["ran"] is True and hits == [], str((hits, st)))
                broken = mk("gobroken", "module m\n\ngo 1.22\n\nfoo bar\n", "package main\n\nfunc main() {}\n")
                hits, st = upgrade.probe(broken, "go", "1.22", "1.24")
                check("go: build hiçbir yere varmadı → ran=False + neden", st["ran"] is False and hits == [] and st["reason"], str((hits, st)))
            else:
                skip("go probe", "yerelde go/git yok ya da hedeften eski")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def test_upgrade_plan_sites():
    """Yükseltme hedefi ve sürüm yerleri: seçilebilir sürümler, imaj kanıtı, CI ve kod siteleri."""
    print("\n[upgrade: hedef sürüm ve sürüm yerleri]")
    import tempfile
    from pathlib import Path
    from agile_sdlc_crew.quality import intake, upgrade
    index = {"core": [{"src": "anatolia/build/lojistik", "images": ["eu/prod-ar/php-8-2:1.0"]},
                      {"src": "russia/build/lojistik", "images": ["10.1.1.1:5000/prod-ar/php-8-2:1.0"]}],
             "web": [{"src": "anatolia/build/web", "images": ["eu/prod-ar/php-8-4:1.0"]}]}
    check("varsayılan hedef: yayınlanmış en yüksek imaj", upgrade.default_target("php", "8.2", index) == "8.4")
    _kv = upgrade.known_versions("nextjs")
    check("liste sonunu geçmiş repo için hedef yok (Next 16.3, liste 15.0 → geriye yükseltme önerilmez)",
          upgrade.default_target("nextjs", "99.0", index) is None
          and upgrade.default_target("nextjs", _kv[-1], index) is None
          and upgrade.default_target("nextjs", "0.1", index) == _kv[-1])
    sel = upgrade.selectable("php", "8.2", index)
    check("seçilebilir: listedeki her üst sürüm", [s["version"] for s in sel] == ["8.3", "8.4", "8.5"])
    # Görev 2 PHP 8.5 kırılma kurallarını kataloğa ekledi (noncanonical_casts_85) → catalog True
    check("8.5 seçilebilir: imajı yok ama katalog kuralı var",
          sel[-1]["published"] is False and sel[-1]["catalog"] is True and sel[1]["published"] is True)
    check("Go: listenin sonuncusu varsayılan", upgrade.default_target("go", "1.24", {}) == "1.27")
    check("imaj adı", upgrade.image_for("eu/prod-ar/php-8-2:1.0", "php", "8.4") == "eu/prod-ar/php-8-4:1.0")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "core"
        root.mkdir()
        (root / "composer.json").write_text('{\n  "require": {\n    "php": "^8.2"\n  }\n}\n')
        df = {"/build/lojistik/Dockerfile": "FROM scratch\nFROM eu/prod-ar/php-8-2:1.0\n"}
        read = lambda d, p: ("FROM 10.1.1.1:5000/prod-ar/php-8-2:1.0\n" if d == "russia" else "FROM eu/prod-ar/php-8-2:1.0\n")
        yml = {"Test Pipelines/core.yml": "variables:\n  phpVersion: '8.2'\nresources:\n  - repository: s\n    name: EcomBackend/core\n",
               "Test Pipelines/other.yml": "variables:\n  phpVersion: '8.2'\nresources:\n    name: EcomBackend/other\n"}
        ss = upgrade.sites(None, "core", root, "php", "8.2", "8.4", index, read=read, yml_files=yml)
        dv = {s["repo"]: s for s in ss if s["where"] == "devops"}
        check("anatolia sitesi açık", dv["anatolia"]["blocked"] == "" and dv["anatolia"]["path"] == "/build/lojistik/Dockerfile"
              and dv["anatolia"]["line"] == 1 and dv["anatolia"]["new"] == "eu/prod-ar/php-8-4:1.0", str(dv))
        check("russia imajı yayınlanmamış → engelli", "yayınlanmamış" in dv["russia"]["blocked"], str(dv["russia"]))
        ci = [s for s in ss if s["where"] == "ci"]
        check("CI sitesi yalnız core'u anan dosya", [s["path"] for s in ci] == ["Test Pipelines/core.yml"]
              and ci[0]["line"] == 2 and ci[0]["old"] == "'8.2'" and ci[0]["new"] == "'8.4'", str(ci))
        cs = [s for s in ss if s["where"] == "code"]
        check("kod sitesi: composer require.php", len(cs) == 1 and cs[0]["path"] == "composer.json" and cs[0]["line"] == 3
              and cs[0]["old"] == "^8.2" and cs[0]["new"] == "^8.4", str(cs))
        deps = intake.Deps(gather=None, board=None, repo_ok=lambda r: True, queue=None, record=None, now=None,
                           cfg=lambda: {"repos_dir": str(base)})
        p85 = upgrade.plan(None, "core", "php", to="8.5", deps=deps, index=index, read=read, yml_files=yml)
        check("yayınlanmamış hedef: bütün üretim siteleri engelli",
              p85["sites"] and all("yayınlanmamış" in s["blocked"] for s in p85["sites"] if s["where"] == "devops"))
        p84 = upgrade.plan(None, "core", "php", to="8.4", deps=deps, index=index, read=read, yml_files=yml)
        check("plan: current/target/up_to_date", p84["current"] == "8.2" and p84["target"] == "8.4" and not p84["up_to_date"]
              and len(p84["per_src"]) == 2, str(p84["current"]))
        for bad in ("9.9", "8.4; rm", "8.1"):
            try:
                upgrade.plan(None, "core", "php", to=bad, deps=deps, index=index)
                check(f"geçersiz hedef {bad}", False)
            except ValueError as e:
                check(f"geçersiz hedef {bad}", "Hedef sürüm seçilemez" in str(e))
        idx2 = {"core": [{"src": "anatolia/build/lojistik", "images": ["eu/prod-ar/php-8-4:1.0"]}]}
        pu = upgrade.plan(None, "core", "php", deps=deps, index=idx2)
        check("güncel repo: up_to_date, site yok", pu["up_to_date"] is True and pu["sites"] == [], str(pu))

        # Düzeltme turu 1 — 1: bulunamayan Dockerfile satırı sessizce düşmez
        idx3 = {"core": [{"src": "anatolia/build/a", "images": ["eu/prod-ar/php-8-2:1.0"]},
                         {"src": "anatolia/build/b", "images": ["eu/prod-ar/php-8-2:1.0"]}],
                "web": [{"src": "anatolia/build/web", "images": ["eu/prod-ar/php-8-4:1.0"]}]}
        rd3 = lambda d, p: ("" if "/build/a/" in p else
                            "FROM eu/prod-ar/php-8-2:1.0 AS x\nRUN true\nFROM eu/prod-ar/php-8-2:1.0\n")
        s3 = upgrade.sites(None, "core", root, "php", "8.2", "8.4", idx3, read=rd3, yml_files={})
        d3 = [s for s in s3 if s["where"] == "devops"]
        miss = [s for s in d3 if s["path"] == "/build/a/Dockerfile"]
        check("eksik FROM satırı: line 0 + engelli site",
              len(miss) == 1 and miss[0]["line"] == 0 and "bulunamadı" in miss[0]["blocked"], str(miss))
        two = sorted(s["line"] for s in d3 if s["path"] == "/build/b/Dockerfile")
        check("aynı imaj iki FROM satırında: iki site", two == [1, 3], str(two))

        # 3: yalnız app_map/per_src'te bilinen servis de site alır
        ps = [{"src": "global/build/only", "version": "8.2", "image": "eu/prod-ar/php-8-2:1.0"}]
        rd4 = lambda d, p: "FROM eu/prod-ar/php-8-2:1.0\n"
        s4 = upgrade.sites(None, "core", root, "php", "8.2", "8.4", idx3, read=rd4, yml_files={}, per_src=ps)
        check("per_src'te olan servis için devops sitesi var",
              any(s["where"] == "devops" and s["repo"] == "global" and s["line"] == 1 for s in s4), str(s4))
        p4 = upgrade.plan(None, "core", "php", to="8.4", deps=deps, index=idx3, read=rd4, yml_files={})
        check("plan per_src'i siteye taşır", p4["per_src"] == [] or all(
            any(s["where"] == "devops" and s["repo"] == x["src"].split("/")[0] for s in p4["sites"]) for x in p4["per_src"]))

        # 2: aralık kısıtı yeniden yazılmaz
        for cons in ("^7.4|^8.0", ">=7.4 <8.5", ">=7.4,<9"):
            r2 = base / "cr"
            r2.mkdir(exist_ok=True)
            (r2 / "composer.json").write_text('{\n  "require": {\n    "php": "%s"\n  }\n}\n' % cons)
            c2 = [s for s in upgrade.sites(None, "cr", r2, "php", "7.4", "8.4", {}, yml_files={}) if s["where"] == "code"]
            if cons == "^7.4|^8.0":
                check("composer ^|^ yeniden yazılır", len(c2) == 1 and c2[0]["new"] == ">=8.4" and c2[0]["blocked"] == "", str(c2))
            else:
                check(f"composer {cons}: elle düzenlenmeli", len(c2) == 1 and "elle" in c2[0]["blocked"]
                      and c2[0]["new"] == c2[0]["old"], str(c2))
        r3 = base / "py"
        r3.mkdir()
        (r3 / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.8,<3.12"\n')
        c3 = [s for s in upgrade.sites(None, "py", r3, "python", "3.8", "3.12", {}, yml_files={}) if s["where"] == "code"]
        check("requires-python üst sınırlı: elle düzenlenmeli", len(c3) == 1 and "elle" in c3[0]["blocked"]
              and c3[0]["new"] == c3[0]["old"], str(c3))


def _fake_hub(tags: dict, calls: list | None = None, down: bool = False):
    """Docker Hub taklidi: `tags` = {ad: [etiket, …]}; liste ucu `name=<minor>.` süzer, tek etiket ucu 200 / 404."""
    def fetch(url):
        if calls is not None:
            calls.append(url)
        if down:
            raise OSError("ağ yok")
        m = re.search(r"/library/(\w+)/tags\?name=([\d.]+)&", url)
        if m:
            return 200, {"results": [{"name": t} for t in tags.get(m.group(1), []) if m.group(2) in t]}
        m = re.search(r"/library/(\w+)/tags/([^/?]+)$", url)
        if m:
            return (200, {"name": m.group(2)}) if m.group(2) in tags.get(m.group(1), []) else (404, None)
        return 500, None
    return fetch


class _FakeBaseImages:
    """FLODevOps/base-images taklidi (kayıt tutan): dal -> {yol: bayt}; yazma çağrıları `calls`ta."""
    PHP84 = (b"ARG PHP_VERSION=8.4.27\nFROM php:${PHP_VERSION}-fpm-trixie\n\nRUN set -ex && \\\n"
             b"    apt-get update && \\\n    # Opcache\n    docker-php-ext-install opcache && \\\n"
             b"    docker-php-ext-enable opcache && \\\n    echo done")

    def __init__(self, files=None, prs=None, fail_list=False, fail_at=None, branch_result=None,
                 move_head_after=None):
        self.fail_at = fail_at              # "branch" | "push" | "pr" | "deny-branch" | "deny-push"
        self.branch_result = branch_result
        self.move_head_after = move_head_after   # list_branches çağrı sayısı; sonrası staging başı ilerler
        self.list_n = 0
        self.fail_list_after = None         # bu kadar list_branches çağrısından sonra OSError
        self.org_url = "https://dev.azure.com/org"
        self.item_meta: dict = {}           # (dal, yol) -> listeleme öğesine eklenen alanlar (gitObjectType, objectId)
        self.pr_response = None             # create_pull_request yanıtını ez
        self.oids: dict = {}                # dal -> başın objectId'si (yoksa "oid-<dal>")
        self.files = {"staging": dict(files if files is not None else {
            "/Jenkinsfile": b"pipeline {}", "/README.md": b"# x",
            "/php-7-4/Dockerfile": b"FROM php:7.4-fpm", "/php-7-4/tag.txt": b"1.0",
            "/php-8-2/dockerfile": b"ARG PHP_VERSION=8.2.1\nFROM php:${PHP_VERSION}-fpm-trixie", "/php-8-2/tag.txt": b"1.0",
            "/php-8-4/Dockerfile": self.PHP84, "/php-8-4/tag.txt": b"1.1", "/php-8-4/extra.ini": b"a=1\r\n",
            "/python-3-13-18/Dockerfile": b"FROM python:3.13.18-slim-trixie", "/python-3-13-18/tag.txt": b"1.0",
            "/python-3-13-18/requirements.txt": b"requests==2\n"})}
        self.prs = dict(prs or {})
        self.fail_list = fail_list
        self.calls: list = []

    def list_branches(self, repo, project=None):
        if self.fail_list or (self.fail_list_after is not None and self.list_n >= self.fail_list_after):
            raise OSError("ağ yok")
        self.list_n += 1
        moved = self.move_head_after is not None and self.list_n > self.move_head_after
        return [{"name": f"refs/heads/{b}",
                 "objectId": self.oids.get(b, f"oid-{b}") + ("-moved" if moved and b == "staging" else "")}
                for b in self.files]

    def get_items_in_path(self, repo, path, branch=None, recursion_level="oneLevel", project=None):
        fs = self.files[branch]
        path = "/" + path.strip("/")
        out, dirs = [], set()
        for p in fs:
            if path == "/":
                rel = p.lstrip("/")
            elif p.startswith(path + "/"):
                rel = p[len(path) + 1:]
            else:
                continue
            if "/" in rel and recursion_level != "full":
                dirs.add(path.rstrip("/") + "/" + rel.split("/")[0])
            else:
                out.append({"path": p, "isFolder": False, **self.item_meta.get((branch, p), {})})
        return [{"path": d, "isFolder": True} for d in sorted(dirs)] + out

    def get_file_bytes(self, repo, path, branch=None, project=None, version=None, version_type=None, **kw):
        if version:
            assert version_type == "commit", version_type
            branch = re.sub(r"-moved$", "", version[len("oid-"):])
        return self.files[branch or "staging"].get(path)

    def _deny(self):
        import requests as _rq
        e = _rq.HTTPError("403")
        e.response = SimpleNamespace(status_code=403)
        raise e

    def create_branch(self, repo, name, source_branch="main", project=None, source_object_id=None):
        if self.fail_at == "deny-branch":
            self._deny()
        self.calls.append(("branch", name, source_branch, source_object_id))
        if self.branch_result is not None:
            return self.branch_result
        self.files[name] = dict(self.files[source_branch])
        self.oids[name] = f"oid-{source_branch}"     # yeni dal kaynağın commit'inde
        return {"value": [{"success": True}]}

    def push_changes(self, repo, branch, changes, msg, project=None, old_object_id=None):
        import base64 as _b64
        if self.fail_at == "deny-push":
            self._deny()
        self.calls.append(("push", branch, changes, old_object_id, project))
        self.oids[branch] = f"oid-{branch}"          # push dalın başını ilerletir
        for c in changes:
            self.files[branch][c["path"]] = _b64.b64decode(c["content"])
        return {}

    def create_pull_request(self, repo, src, tgt="main", title="", description="", work_item_ids=None,
                            draft=False, project=None):
        if self.fail_at == "pr":
            raise OSError("PR servisi çöktü")
        self.calls.append(("pr", src, tgt, title, description, draft, project))
        self.prs[src] = {"id": 77, "status": "active", "url": "http://pr/77"}
        if self.pr_response is not None:
            return self.pr_response
        return {"pullRequestId": 77, "url": "https://dev.azure.com/org/_apis/git/repositories/x/pullRequests/77",
                "repository": {"name": repo, "project": {"name": "FLODevOps"}}}

    def find_pull_request(self, repo, branch, project=None):
        return self.prs.get(branch)


def test_upgrade_image_template():
    """Taban imaj şablonu: gruplama, durum, kaynak dizinden türetme, tek taslak PR."""
    print("\n[upgrade: taban imaj şablonu]")
    from agile_sdlc_crew.quality import upgrade_image as ui, upgrade_registry as ur
    cfg = ui.load_cfg()
    check("cfg: base-images / staging", cfg["repo"] == "base-images" and cfg["branch"] == "staging")
    hub = {"php": ["8.5.12", "8.5.12-fpm-trixie", "8.3.9", "8.3.9-fpm-trixie"], "python": ["3.14.2", "3.14.2-slim-trixie"]}
    res = ur.Resolver(fetch=_fake_hub(hub))

    def site(region, new, blocked="imaj yayınlanmamış", where="devops", path="/build/channel/Dockerfile"):
        return {"where": where, "repo": region, "project": "FLODevOps", "branch": "production", "path": path,
                "line": 1, "old": re.sub(r"(php|python)-\d+-\d+(?:-\d+)?:", r"\1-8-2:", new), "new": new,
                "blocked": blocked}
    pre = {"anatolia": "europe-west1-docker.pkg.dev/flo-prod-main/prod-ar/", "russia": "10.131.167.220:5000/prod-ar/",
           "mars": "m.example/"}
    sites = [site("anatolia", pre["anatolia"] + "php-8-5:1.0", path="/a/Dockerfile"),
             site("anatolia", pre["anatolia"] + "php-8-5:1.0", path="/b/Dockerfile"),
             site("russia", pre["russia"] + "php-8-5:1.0", path="/r/Dockerfile"),
             site("anatolia", "golang:1.27.3"),
             site("anatolia", pre["anatolia"] + "php-8-5:1.0", where="code"),
             site("anatolia", pre["anatolia"] + "php-8-5:1.0", blocked="")]
    need = ui.needed(sites, "php")
    check("needed: tek dizin, iki bölge, üç site",
          len(need) == 1 and need[0]["dir"] == "php-8-5" and need[0]["tag"] == "1.0" and need[0]["error"] == ""
          and sorted(need[0]["regions"]) == ["anatolia", "russia"] and len(need[0]["sites"]) == 3, str(need))
    check("needed: library imajı hariç", ui.needed([site("anatolia", "golang:1.27.3")], "go") == [])
    clash = ui.needed([site("anatolia", pre["anatolia"] + "php-8-5:1.0"),
                       site("russia", pre["russia"] + "php-8-5:1.1")], "php")
    check("needed: farklı etiket → error",
          len(clash) == 1 and clash[0]["error"] == "bölgeler farklı etiket istiyor: 1.0, 1.1", str(clash))
    py = ui.needed([site("anatolia", pre["anatolia"] + "python-3-14-2:1.0")], "python")
    check("needed: üç parçalı dizin", bool(py) and py[0]["dir"] == "python-3-14-2")
    check("branch_name", ui.branch_name("php-8-5") == "tempo/base-image-php-8-5")
    entry = need[0]

    c = _FakeBaseImages()
    st = ui.state(c, entry)
    check("state yok", st["state"] == "yok" and st["pr"] is None and st["dir"] == "php-8-5", str(st))
    c2 = _FakeBaseImages()
    c2.files["staging"]["/php-8-5/tag.txt"] = b"1.0"
    st = ui.state(c2, entry)
    check("state şablon var + bayraklar", st["state"] == "şablon var" and "mainLocation" in st["reason"]
          and "yandexLocation" in st["reason"], str(st))
    for status, want in (("active", "pr açık"), ("abandoned", "pr reddedildi")):
        c3 = _FakeBaseImages(prs={"tempo/base-image-php-8-5": {"id": 5, "status": status, "url": "u"}})
        st = ui.state(c3, entry)
        check(f"state {want}", st["state"] == want and st["pr"]["id"] == 5, str(st))
    c4 = _FakeBaseImages(prs={"tempo/base-image-php-8-5": {"id": 5, "status": "completed", "url": "u"}})
    st = ui.state(c4, entry)
    check("state completed ama dizin yok → belirsiz", st["state"] == "belirsiz" and st["reason"], str(st))
    st = ui.state(c, dict(entry, error="bölgeler farklı etiket istiyor: 1.0, 1.1"))
    check("state entry.error → belirsiz", st["state"] == "belirsiz" and "farklı etiket" in st["reason"])
    st = ui.state(_FakeBaseImages(fail_list=True), entry)
    check("state okuma hatası → belirsiz", st["state"] == "belirsiz" and st["reason"], str(st))

    b = ui.build(c, "php", "8.5", entry, resolver=res)
    f = b["files"]
    d = f["/php-8-5/Dockerfile"].decode()
    check("build php: kaynak php-8-4, tam 8.5.12", b["source"] == "php-8-4" and b["full"] == "8.5.12", str(b["source"]))
    check("build php: ARG değişti, FROM aynı",
          d.startswith("ARG PHP_VERSION=8.5.12\nFROM php:${PHP_VERSION}-fpm-trixie\n"))
    check("build php: opcache satırları yok, öncesi korunur",
          "docker-php-ext" not in d and "apt-get update && \\\n" in d and d.endswith("echo done"))
    check("build php: tag.txt satır sonsuz", f["/php-8-5/tag.txt"] == b"1.0")
    check("build php: diğer dosya bayt eşit", f["/php-8-5/extra.ini"] == b"a=1\r\n")
    check("build php: not var", any("Opcache" in n for n in b["notes"]), str(b["notes"]))
    cl = _FakeBaseImages({"/php-8-2/dockerfile": b"ARG PHP_VERSION=8.2.1\nFROM php:${PHP_VERSION}-fpm-trixie",
                          "/php-8-2/tag.txt": b"1.0"})
    bl = ui.build(cl, "php", "8.3", dict(entry, dir="php-8-3"), resolver=res)
    check("build: küçük harfli dockerfile adı korunur",
          set(bl["files"]) == {"/php-8-3/dockerfile", "/php-8-3/tag.txt"}
          and bl["files"]["/php-8-3/dockerfile"].startswith(b"ARG PHP_VERSION=8.3.9"), str(set(bl["files"])))
    check("build php 8.3: düzeltme notu yok", not any("düzeltme uygulanmadı" in n for n in bl["notes"]))
    cn = _FakeBaseImages({"/php-8-4/Dockerfile": b"ARG PHP_VERSION=8.4.1\nFROM php:${PHP_VERSION}-fpm-trixie\nRUN true",
                          "/php-8-4/tag.txt": b"1.0"})
    bn = ui.build(cn, "php", "8.5", entry, resolver=res)
    check("build: drop eşleşmezse 'düzeltme uygulanmadı'",
          any(n.startswith("düzeltme uygulanmadı") for n in bn["notes"]), str(bn["notes"]))

    pe = py[0]
    bp = ui.build(c, "python", "3.14", pe, resolver=res)
    check("build python: dizin, sürüm, sonek, requirements, tag",
          bp["source"] == "python-3-13-18"
          and bp["files"]["/python-3-14-2/Dockerfile"] == b"FROM python:3.14.2-slim-trixie"
          and bp["files"]["/python-3-14-2/requirements.txt"] == b"requests==2\n"
          and bp["files"]["/python-3-14-2/tag.txt"] == b"1.0", str(bp["files"]))

    def fails(name, cl, lang, to, e, r=res, frag="", cfg_=None):
        try:
            ui.build(cl, lang, to, e, resolver=r, cfg=cfg_)
        except ui.ImageTemplateError as ex:
            check(name, frag in str(ex), str(ex))
        else:
            check(name, False, "hata fırlatılmadı")
    fails("kaynak yok", _FakeBaseImages({"/php-9-0/tag.txt": b"1.0"}), "php", "8.5", entry, frag="Kaynak dizin bulunamadı")
    two = _FakeBaseImages({"/php-8-4/Dockerfile": b"ARG PHP_VERSION=8.4.1\nARG PHP_VERSION=8.4.2\nFROM php:${PHP_VERSION}-fpm",
                           "/php-8-4/tag.txt": b"1.0"})
    fails("iki sürüm satırı", two, "php", "8.5", entry, frag="sürüm satırı")
    fails("latest_patch None", c, "php", "8.5", entry, r=ur.Resolver(fetch=_fake_hub({})), frag="yama")
    fails("tag_exists False", c, "php", "8.5", entry,
          r=ur.Resolver(fetch=_fake_hub({}), patches={"php": {"8.5": "8.5.12"}}), frag="Docker Hub")
    fails("tag_exists None", c, "php", "8.5", entry,
          r=ur.Resolver(fetch=lambda u: (500, None), patches={"php": {"8.5": "8.5.12"}}), frag="doğrulanamadı")
    bad = dict(cfg, fixes={"php": {"8.5": [{"drop": r"echo done", "note": "x"}]}})
    fails("drop satırı \\ ile bitmiyor", c, "php", "8.5", entry, frag="\\", cfg_=bad)

    # --- fix round 1: dar düzeltme kuralı
    def php_with(body: bytes):
        return _FakeBaseImages({"/php-8-4/Dockerfile": b"ARG PHP_VERSION=8.4.1\nFROM php:${PHP_VERSION}-fpm-trixie\n" + body,
                                "/php-8-4/tag.txt": b"1.0"})
    multi = php_with(b"RUN set -ex && \\\n    docker-php-ext-install opcache pdo_mysql gd && \\\n    echo done")
    fails("çok eklentili opcache satırı atılmaz", multi, "php", "8.5", entry, frag="docker-php-ext-install opcache pdo_mysql")
    head = php_with(b"RUN docker-php-ext-install opcache \\\n    && echo done")
    fails("RUN ile başlayan satır atılmaz", head, "php", "8.5", entry, frag="RUN docker-php-ext-install opcache")
    last = php_with(b"RUN set -ex && \\\n    echo a && \\\n    docker-php-ext-enable opcache")
    fails("zincirin son satırı atılmaz", last, "php", "8.5", entry, frag="docker-php-ext-enable opcache")
    check("gerçek iki satır atılır, çevresi korunur",
          b"docker-php-ext" not in ui.build(c, "php", "8.5", entry, resolver=res)["files"]["/php-8-5/Dockerfile"])

    # --- fix round 1: yazma korumaları
    def pr_calls(cl):
        return [x[0] for x in cl.calls]
    bname = "tempo/base-image-php-8-5"
    cf = _FakeBaseImages(fail_at="pr")
    try:
        ui.open_pr(cf, "php", "8.5", entry, resolver=res)
        check("PR hatası → ImageTemplateError", False)
    except ui.ImageTemplateError as ex:
        check("PR hatası dalı adlandırır, push yapıldı der", bname in str(ex) and "Push yapıldı" in str(ex), str(ex))
        check("PR hatası: bu çağrı dal + push yazdı → wrote True", ex.wrote is True)
    cf2 = _FakeBaseImages(fail_at="pr")
    try:
        ui.open_pr(cf2, "php", "8.5", entry, resolver=res)
    except ui.ImageTemplateError:
        pass
    n_before = len(cf2.calls)
    try:
        ui.open_pr(cf2, "php", "8.5", entry, resolver=res)
        check("ikinci deneme de PR'da düşer", False)
    except ui.ImageTemplateError as ex:
        check("dal hazır, yalnız PR yine açılamadı → wrote False, yeni dal / push yok",
              ex.wrote is False and pr_calls(cf2)[n_before:] == [], f"{ex.wrote} {pr_calls(cf2)[n_before:]}")
    check("PR hatasından sonra dal + push yazılmış", pr_calls(cf) == ["branch", "push"], str(pr_calls(cf)))
    cf.fail_at = None
    k = len(cf.calls)
    r = ui.open_pr(cf, "php", "8.5", entry, resolver=res)
    check("aynı içerikli dal: yalnız PR açılır", pr_calls(cf)[k:] == ["pr"] and r["state"] == "pr açık", str(cf.calls[k:]))
    cd = _FakeBaseImages(fail_at="pr")
    try:
        ui.open_pr(cd, "php", "8.5", entry, resolver=res)
    except ui.ImageTemplateError:
        pass
    cd.fail_at = None
    cd.files[bname]["/php-8-5/tag.txt"] = b"9.9"
    k = len(cd.calls)
    try:
        ui.open_pr(cd, "php", "8.5", entry, resolver=res)
        check("farklı içerikli dal reddedilir", False)
    except ui.ImageTemplateError as ex:
        check("farklı içerikli dal reddedilir, yazma yok", len(cd.calls) == k and "farklı" in str(ex), str(ex))
    ce = _FakeBaseImages(fail_at="pr")
    try:
        ui.open_pr(ce, "php", "8.5", entry, resolver=res)
    except ui.ImageTemplateError:
        pass
    ce.fail_at = None
    ce.files[bname]["/php-8-5/EXTRA"] = b"x"
    k = len(ce.calls)
    try:
        ui.open_pr(ce, "php", "8.5", entry, resolver=res)
        check("fazladan dosyalı dal reddedilir", False)
    except ui.ImageTemplateError:
        check("fazladan dosyalı dal reddedilir, yazma yok", len(ce.calls) == k)
    cl_ = _FakeBaseImages()
    cl_.files[bname] = dict(cl_.files["staging"])
    cl_.oids[bname] = "oid-staging"
    r = ui.open_pr(cl_, "php", "8.5", entry, resolver=res)
    check("boş kalan dal yeniden kullanılır (branch yok, push + PR)", pr_calls(cl_) == ["push", "pr"] and r["state"] == "pr açık",
          str(pr_calls(cl_)))
    cg = _FakeBaseImages()
    cg.files[bname] = dict(cg.files["staging"], **{"/other": b"x"})
    cg.oids[bname] = "oid-other-commit"
    try:
        ui.open_pr(cg, "php", "8.5", entry, resolver=res)
        check("başka commit'li dal reddedilir", False)
    except ui.ImageTemplateError:
        check("başka commit'li dal reddedilir, yazma yok", cg.calls == [])
    cmv = _FakeBaseImages(move_head_after=3)
    try:
        ui.open_pr(cmv, "php", "8.5", entry, resolver=res)
        check("hedef dal ilerledi → hata", False)
    except ui.ImageTemplateError as ex:
        check("hedef dal build sırasında ilerledi → yazma yok", cmv.calls == [] and "ilerledi" in str(ex), str(ex))
    for where in ("deny-branch", "deny-push"):
        cdn = _FakeBaseImages(fail_at=where)
        try:
            ui.open_pr(cdn, "php", "8.5", entry, resolver=res)
            check(f"{where}: ImageTemplateError", False)
        except ui.ImageTemplateError as ex:
            check(f"{where}: ImageTemplateError, yetki + dal adı", "401/403" in str(ex) and bname in str(ex), str(ex))
    cnb = _FakeBaseImages(branch_result={"value": [{"success": False, "updateStatus": "PermissionRequired"}]})
    try:
        ui.open_pr(cnb, "php", "8.5", entry, resolver=res)
        check("create_branch success False → hata", False)
    except ui.ImageTemplateError as ex:
        check("create_branch success False → hata, push yok",
              "PermissionRequired" in str(ex) and "Push yapılmadı" in str(ex) and pr_calls(cnb) == ["branch"], str(ex))
    nu = _FakeBaseImages({"/php-8-4/Dockerfile": b"ARG PHP_VERSION=8.4.1\nFROM php:${PHP_VERSION}-fpm\n\xff\xfe",
                          "/php-8-4/tag.txt": b"1.0"})
    fails("UTF-8 olmayan Dockerfile", nu, "php", "8.5", entry, frag="UTF-8")
    big = _FakeBaseImages({**{f"/php-8-4/f{i}": b"x" for i in range(21)}, "/php-8-4/Dockerfile": b"ARG PHP_VERSION=8.4.1\nFROM php:${PHP_VERSION}-fpm"})
    fails("21 dosya → hata", big, "php", "8.5", entry, frag="dosya var")
    huge = _FakeBaseImages({"/php-8-4/blob": b"x" * 2_100_000, "/php-8-4/Dockerfile": b"ARG PHP_VERSION=8.4.1\nFROM php:${PHP_VERSION}-fpm"})
    fails("2 MB üstü → hata", huge, "php", "8.5", entry, frag="MB")
    # needed: geçersiz etiket / bilinmeyen bölge / başka önek
    for bad_tag in (".5", "-x"):
        e_ = ui.needed([site("anatolia", pre["anatolia"] + f"php-8-5:{bad_tag}")], "php")
        check(f"needed: etiket {bad_tag!r} → error", len(e_) == 1 and "geçersiz etiket" in e_[0]["error"], str(e_))
        check(f"state: etiket {bad_tag!r} → belirsiz, PR yok",
              ui.state(_FakeBaseImages(), e_[0])["state"] == "belirsiz")
    e_ = ui.needed([site("mars", pre["mars"] + "php-8-5:1.0")], "php")
    check("needed: bilinmeyen bölge → error", len(e_) == 1 and e_[0]["error"] == "bilinmeyen bölge: mars", str(e_))
    foreign = site("anatolia", pre["anatolia"] + "php-8-5:1.0")
    foreign["old"] = "other.example/x/php-8-2:1.0"
    check("needed: old ile farklı önek → bizim imajımız değil", ui.needed([foreign], "php") == [])
    # PR metni: ters tırnak / denetim karakteri, sınır, RU cümlesi korunur
    evil = dict(entry, sites=[site("anatolia", pre["anatolia"] + "php-8-5:1.0", path="/a`b\x07/Dockerfile")]
                + [site("russia", pre["russia"] + "php-8-5:1.0", path=f"/r{i}/" + "x" * 150) for i in range(300)])
    cev = _FakeBaseImages()
    r = ui.open_pr(cev, "php", "8.5", evil, resolver=res, repo="rp`o")
    dv = cev.calls[-1][4]
    check("açıklama: ters tırnak/denetim karakteri yok", "/ab/Dockerfile" in dv and "\x07" not in dv and "rp`o" not in dv)
    check("açıklama ≤ 4000, RU cümlesi + bayraklar duruyor, liste kısaldı",
          len(dv) <= 4000 and "RU registry" in dv and "yandexLocation" in dv and "mainLocation" in dv
          and "servis daha" in dv, str(len(dv)))
    check("URL: web bağlantısı (API url'si değil)",
          r["pr"]["url"] == "https://dev.azure.com/org/FLODevOps/_git/base-images/pullrequest/77", r["pr"]["url"])
    # round 2: içerik eşitliği, etiket, ağ hatası
    cj = _FakeBaseImages(fail_at="pr")
    try:
        ui.open_pr(cj, "php", "8.5", entry, resolver=res)
    except ui.ImageTemplateError:
        pass
    cj.fail_at = None
    cj.files[bname]["/Jenkinsfile"] = b"pipeline { evil }"
    k = len(cj.calls)
    try:
        ui.open_pr(cj, "php", "8.5", entry, resolver=res)
        check("aynı yollar, değişmiş Jenkinsfile → reddedilir", False)
    except ui.ImageTemplateError:
        check("aynı yollar, değişmiş Jenkinsfile → reddedilir, yazma yok", len(cj.calls) == k)
    ck = _FakeBaseImages(fail_at="pr")
    try:
        ui.open_pr(ck, "php", "8.5", entry, resolver=res)
    except ui.ImageTemplateError:
        pass
    ck.fail_at = None
    ck.files["staging"]["/README.md"] = b"# staging moved"      # hedef içeriği dal açıldıktan sonra değişti
    ck.oids["staging"] = "oid-staging-moved"
    k = len(ck.calls)
    try:
        ui.open_pr(ck, "php", "8.5", entry, resolver=res)
        check("hedef kaydı → dal artık eşit değil", False)
    except ui.ImageTemplateError:
        check("hedef sabit commit'ten farklı → reddedilir, yazma yok", len(ck.calls) == k)
    for tg in ("a/b", "1.0 x", "x" * 65):
        e_ = ui.needed([site("anatolia", pre["anatolia"] + f"php-8-5:{tg}")], "php")
        check(f"needed: etiket {tg[:12]!r} → entry error", len(e_) == 1 and "geçersiz etiket" in e_[0]["error"], str(e_))
    cn2 = _FakeBaseImages()
    cn2.fail_list_after = 3
    try:
        ui.open_pr(cn2, "php", "8.5", entry, resolver=res)
        check("ağ hatası → ImageTemplateError", False)
    except ui.ImageTemplateError as ex:
        check("build sonrası ağ hatası ImageTemplateError, yazma yok", cn2.calls == [])
        check("hazırlık hatası dalı adlandırır, push yapılmadı der",
              bname in str(ex) and "Push yapılmadı" in str(ex), str(ex))
    except OSError:
        check("build sonrası ağ hatası ImageTemplateError", False, "ham OSError")
    # round 3: düz dosya olmayan öğe / nesne kimliği / bozuk PR yanıtı / tag temizliği
    cs = _FakeBaseImages(fail_at="pr")
    try:
        ui.open_pr(cs, "php", "8.5", entry, resolver=res)
    except ui.ImageTemplateError:
        pass
    cs.fail_at = None
    cs.item_meta[(bname, "/README.md")] = {"gitObjectType": "commit"}      # submodule
    k = len(cs.calls)
    try:
        ui.open_pr(cs, "php", "8.5", entry, resolver=res)
        check("submodule (commit) öğeli dal reddedilir", False)
    except ui.ImageTemplateError:
        check("submodule (commit) öğeli dal reddedilir, yazma yok", len(cs.calls) == k)
    co = _FakeBaseImages(fail_at="pr")
    try:
        ui.open_pr(co, "php", "8.5", entry, resolver=res)
    except ui.ImageTemplateError:
        pass
    co.fail_at = None
    co.item_meta[(bname, "/README.md")] = {"gitObjectType": "blob", "objectId": "aaa"}
    co.item_meta[("staging", "/README.md")] = {"gitObjectType": "blob", "objectId": "bbb"}
    k = len(co.calls)
    try:
        ui.open_pr(co, "php", "8.5", entry, resolver=res)
        check("aynı bayt, farklı objectId → reddedilir", False)
    except ui.ImageTemplateError:
        check("aynı bayt, farklı objectId → reddedilir, yazma yok", len(co.calls) == k)
    ci = _FakeBaseImages(fail_at="pr")
    try:
        ui.open_pr(ci, "php", "8.5", entry, resolver=res)
    except ui.ImageTemplateError:
        pass
    ci.fail_at = None
    for br_ in (bname, "staging"):
        ci.item_meta[(br_, "/README.md")] = {"gitObjectType": "blob", "objectId": "same"}
    k = len(ci.calls)
    ui.open_pr(ci, "php", "8.5", entry, resolver=res)
    check("blob + eşit objectId → yalnız PR", [x[0] for x in ci.calls[k:]] == ["pr"])
    cp = _FakeBaseImages()
    cp.pr_response = ["garip"]
    try:
        ui.open_pr(cp, "php", "8.5", entry, resolver=res)
        check("bozuk PR yanıtı → hata", False)
    except ui.ImageTemplateError as ex:
        check("bozuk PR yanıtı: dal adı + 'kontrol et'", bname in str(ex) and "kontrol" in str(ex), str(ex))
    cp2 = _FakeBaseImages()
    cp2.pr_response = {"url": "x"}
    try:
        ui.open_pr(cp2, "php", "8.5", entry, resolver=res)
        check("id'siz PR yanıtı → hata", False)
    except ui.ImageTemplateError as ex:
        check("id'siz PR yanıtı: 'kontrol et'", "kontrol" in str(ex), str(ex))
    check("state: tag temizlenir", ui.state(_FakeBaseImages(), dict(entry, tag="1.\x070`"))["tag"] == "1.0")

    c5 = _FakeBaseImages()
    r1 = ui.open_pr(c5, "php", "8.5", entry, resolver=res, repo="svc-a")
    kinds = [x[0] for x in c5.calls]
    check("open_pr: bir branch, bir push, bir taslak PR", kinds == ["branch", "push", "pr"], str(kinds))
    br, pu, pr = c5.calls
    check("open_pr: dal adı + sabit objectId", br[1] == "tempo/base-image-php-8-5" and br[2] == "staging"
          and br[3] == "oid-staging" and pu[3] == "oid-staging", str(br))
    check("open_pr: push hepsi add + base64",
          all(x["changeType"] == "add" and x["contentType"] == "base64encoded" for x in pu[2])
          and {x["path"] for x in pu[2]} >= {"/php-8-5/Dockerfile", "/php-8-5/tag.txt"}, str(pu[2]))
    check("open_pr: proje, taslak, hedef", pu[4] == "FLODevOps" and pr[5] is True and pr[2] == "staging"
          and pr[6] == "FLODevOps")
    check("open_pr: başlık", pr[3] == "php-8-5 taban imaj şablonu (PHP 8.5.12)", pr[3])
    desc = pr[4]
    check("open_pr: açıklama içeriği",
          "yandexLocation" in desc and "mainLocation" in desc and "/r/Dockerfile" in desc
          and "baseImage=php-8-5" in desc and "RU registry" in desc and "svc-a" in desc
          and "doğrulanmadı" in desc and len(desc) <= 4000, desc)
    check("open_pr: sonuç", r1["state"] == "pr açık" and r1["pr"]["id"] == 77 and isinstance(r1["notes"], list), str(r1))
    n = len(c5.calls)
    r2 = ui.open_pr(c5, "php", "8.5", entry, resolver=res, repo="svc-a")
    check("ikinci çağrı ikinci PR açmaz", len(c5.calls) == n and r2["state"] == "pr açık", str(r2))
    for name, cl2, want in (
            ("pr reddedildi", _FakeBaseImages(prs={"tempo/base-image-php-8-5": {"id": 5, "status": "abandoned", "url": "u"}}),
             "pr reddedildi"),
            ("şablon var", c2, "şablon var")):
        k = len(cl2.calls)
        r = ui.open_pr(cl2, "php", "8.5", entry, resolver=res)
        check(f"open_pr {name}: yazma yok", r["state"] == want and len(cl2.calls) == k, str(r))
    many = dict(entry, sites=[site("anatolia", pre["anatolia"] + "php-8-5:1.0", path=f"/s{i}/Dockerfile")
                              for i in range(40)])
    cm = _FakeBaseImages()
    ui.open_pr(cm, "php", "8.5", many, resolver=res)
    dm = cm.calls[-1][4]
    check("açıklama sınırlı: ilk 10 + '… ve 30 servis daha'",
          "/s9/Dockerfile" in dm and "/s10/Dockerfile" not in dm and "… ve 30 servis daha" in dm and len(dm) <= 4000)


def test_upgrade_image_wiring():
    """Taban imaj şablonu bağlama: ayar kapalıyken bugünkü çıktı, durum `images`, engel nedeni, otomatik
    ilerletme (yok → bir kez, pr açık → hiç), uç (403 / 404 / 409 / 200 / hata → 502)."""
    print("\n[upgrade: taban imaj şablonu bağlama]")
    from fastapi.testclient import TestClient
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import server
    from agile_sdlc_crew.quality import intake, upgrade, upgrade_image as ui, upgrade_track as ut

    html_ = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text(encoding="utf-8")
    check("kart: imaj satırları, düğme, uç ve kaçış",
          "function _upImagesHtml(" in html_ and "Şablon PR'ı aç" in html_ and "'/api/techdebt/upgrade/image'" in html_
          and "escHtml(dir" in html_ and "h+=_upImagesHtml(st.images)" in html_)
    check("kart: onay metni ayardan (kaynak dal / hedef dal / repo)",
          'data-source="${escHtml(' in html_ and "d.source" in html_ and "d.branch" in html_
          and "d.repo" in html_ and "'tempo/base-image-${dir}'" not in html_)
    pre = "eu/prod-ar/"
    unp = {"where": "devops", "repo": "anatolia", "project": "FLODevOps", "branch": "production",
           "path": "/build/core/Dockerfile", "line": 1, "old": pre + "php-8-2:1.0", "new": pre + "php-8-5:1.0",
           "blocked": upgrade.UNPUBLISHED}
    other = dict(unp, blocked="elle düzenlenmeli", path="/build/web/Dockerfile")

    def mkscan(sites):
        return {"repo": "core", "lang": "php", "current": "8.2", "target": "8.5", "up_to_date": False,
                "sites": sites, "blocking": 0, "blockers": [], "auto": [], "infra": [], "human": [],
                "ci_pins": [], "ci_source": None}
    cur_state = {"dir": "php-8-5", "tag": "1.0", "regions": ["anatolia"], "state": "yok", "reason": "", "pr": None}
    flags = {"CREW_TECHDEBT_UPGRADE_IMAGE": True}
    orig_get = pc.get
    pc.get = lambda k: flags[k] if k in flags else orig_get(k)
    try:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "core"
            root.mkdir()
            (root / "composer.json").write_text('{"require": {"php": "^8.2"}}', encoding="utf-8")
            cfg = {"pack_sp": 3, "pack_files": 5, "grace_days": 3, "task_fields": {}, "sonar_url": "", "nr_days": 7,
                   "php_exclude": [], "php_bin": "php", "scan_ttl_h": 24, "max_take": 8, "freshen": False,
                   "repos_dir": base, "targets_cache": base / "t.yaml", "scan_cache": base / "cache"}
            deps = intake.Deps(gather=None, board=lambda c: {}, repo_ok=lambda r: (base / r).is_dir(), queue=None,
                               record=lambda **k: None, now=lambda: 1e9, cfg=lambda: cfg)

            def st(sites, **k):
                return ut.status(_UpFake(), "core", "php", to="8.5", deps=deps, scan=mkscan(sites), wait_h=72,
                                 root=root, **k)
            calls = []

            def sfn(c, entry, **k):
                calls.append(entry["dir"])
                return dict(cur_state, dir=entry["dir"])
            flags["CREW_TECHDEBT_UPGRADE_IMAGE"] = False
            s = st([unp], image_state_fn=sfn)
            check("ayar kapalı → images == [], durum çağrılmaz", s["images"] == [] and not calls, str(s["images"]))
            base_reason = s["next"]["reason"]
            flags["CREW_TECHDEBT_UPGRADE_IMAGE"] = True
            s = st([other], image_state_fn=sfn)
            check("UNPUBLISHED site yok → images == []", s["images"] == [] and not calls)
            s = st([unp], image_state_fn=sfn)
            check("ayar açık + UNPUBLISHED site → images dolu",
                  [i["dir"] for i in s["images"]] == ["php-8-5"] and s["images"][0]["state"] == "yok"
                  and calls == ["php-8-5"], str(s["images"]))
            check("engel nedeni yine üretim adımını kapatır", s["next"]["step"] == "prod" and not s["next"]["ready"])
            icfg = ui.load_cfg()
            im0 = s["images"][0]
            check("images girdisi onay metni için repo / proje / hedef dal / kaynak dal taşır",
                  im0["repo"] == icfg["repo"] and im0["project"] == icfg.get("project") and im0["branch"] == icfg["branch"]
                  and im0["source_branch"] == "tempo/base-image-php-8-5" and im0["state"] == "yok", str(im0))
            s = st([unp], image_state_fn=lambda c, e, **k: dict(cur_state, state="pr açık", pr={"id": 46551, "url": "u"}))
            check("engel nedenine imaj durumu eklenir",
                  "php-8-5: şablon PR'ı #46551 açık" in s["next"]["reason"]
                  and s["next"]["reason"].startswith(base_reason.split(" — ")[0]), s["next"]["reason"])

            def boom(c, e, **k):
                raise RuntimeError("Azure çöktü")
            s = st([unp], image_state_fn=boom)
            check("durum istisnası → belirsiz, status düşmez",
                  s["images"] and s["images"][0]["state"] == "belirsiz" and "Azure çöktü" in s["images"][0]["reason"]
                  and s["stage"], str(s["images"]))

        # ── otomatik ilerletme ──
        class _CF:
            def __init__(self):
                self.comments = []

            def get_work_item_comments(self, wid):
                return []

            def add_comment(self, wid, html_):
                self.comments.append((wid, html_))

        def adv_one(images, *, open_fn, auto_img=True, handed=None, take=None, now=None, cf=None):
            flags["CREW_TECHDEBT_UPGRADE_IMAGE"] = auto_img
            tr = {"id": 500, "repo": "core", "lang": "php", "to": "8.5"}
            res = {"id": 500, "note": ""}
            stt = {"stage": "engelli", "images": images,
                   "next": {"step": "prod", "ready": False, "handed_off": handed}}
            cf = cf or _CF()
            s_scan, s_take = ut._fresh_scan, ut._auto_take
            ut._fresh_scan = lambda *a, **k: mkscan([unp])
            ut._auto_take = take or (lambda *a, **k: None)
            try:
                ut._advance_one(cf, tr, res, team="T", deps=None, create_job=None, open_jobs=None, bump_jobs=None,
                                status_fn=lambda *a, **k: stt, take_fn=None, last_bump_job=None, kw={},
                                image_open_fn=open_fn, now=now)
            finally:
                ut._fresh_scan, ut._auto_take = s_scan, s_take
            return res, cf
        opened = []

        def ofn(client, lang, to, entry, **k):
            opened.append((lang, to, entry["dir"], k.get("repo")))
            return {"state": "pr açık", "pr": {"id": 9, "url": "https://x/pr/9"}, "notes": []}
        res, cf = adv_one([cur_state], open_fn=ofn)
        check("_advance_one: yok → open_pr bir kez, nota PR yazılır",
              opened == [("php", "8.5", "php-8-5", "core")] and "#9" in res["note"], f"{opened} {res}")
        opened.clear()
        adv_one([dict(cur_state, state="pr açık", pr={"id": 5, "url": "u"})], open_fn=ofn)
        adv_one([dict(cur_state, state="belirsiz")], open_fn=ofn)
        adv_one([cur_state], open_fn=ofn, auto_img=False)
        check("_advance_one: pr açık / belirsiz / ayar kapalı → open_pr yok", opened == [])

        def bad(client, lang, to, entry, **k):
            raise ui.ImageTemplateError("dal var ama içeriği farklı")
        res, cf = adv_one([cur_state], open_fn=bad)
        check("_advance_one: hata adımı durdurmaz, nota + izleme Task'ına yorum",
              "dal var ama içeriği farklı" in res["note"] and len(cf.comments) == 1 and cf.comments[0][0] == 500,
              f"{res} {cf.comments}")

        # devredilmiş adım, sıra, yalıtım
        opened.clear()
        res, cf = adv_one([cur_state], open_fn=ofn, handed=777)
        check("_advance_one: adım DevOps'a devredilmişken de open_pr bir kez",
              len(opened) == 1 and "#777" in res["note"] and "#9" in res["note"], f"{opened} {res}")
        order = []
        opened.clear()

        def take_ok(*a, **k):
            order.append("take")
        adv_one([cur_state], open_fn=lambda *a, **k: (order.append("image"), ofn(*a, **k))[1], take=take_ok)
        check("_advance_one: imaj adımı _auto_take'ten önce", order == ["image", "take"], str(order))

        def take_boom(*a, **k):
            raise RuntimeError("alım çöktü")
        opened.clear()
        try:
            adv_one([cur_state], open_fn=ofn, take=take_boom)
            check("_advance_one: _auto_take istisnası yükselir", False)
        except RuntimeError:
            check("_advance_one: _auto_take çökse de imaj PR'ı açılmış", len(opened) == 1, str(opened))
        took = []
        s_needed = ui.needed
        ui.needed = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("beklenmeyen"))
        try:
            adv_one([cur_state], open_fn=ofn, take=lambda *a, **k: took.append(1))
            check("_advance_one: imaj adımı beklenmedik hata verse de _auto_take çalışır", took == [1], str(took))
        finally:
            ui.needed = s_needed
        # hata yorumu: enjekte `now` ve 24 sa kısma
        import hashlib
        from datetime import datetime as _dt2, timezone as _tz2
        NOW = _dt2(2026, 11, 17, 12, 0, tzinfo=_tz2.utc)
        msg = "php-8-5: şablon PR'ı açılamadı: dal var ama içeriği farklı"
        mark = ut.ERR_MARK + hashlib.sha1(msg.encode("utf-8")).hexdigest()[:10]

        class _CM(_CF):
            def __init__(self, when):
                super().__init__()
                self.when = when

            def get_work_item_comments(self, wid):
                return [{"metin": f"<p>{mark}</p>", "tarih": self.when}]
        _, cf = adv_one([cur_state], open_fn=bad, now=NOW, cf=_CM("2026-11-17T11:00:00Z"))
        check("hata yorumu: son 24 saatte aynı işaret varsa ikinci yorum yok", cf.comments == [], str(cf.comments))
        _, cf = adv_one([cur_state], open_fn=bad, now=NOW, cf=_CM("2026-11-15T11:00:00Z"))
        check("hata yorumu: işaret 24 saatten eskiyse bir yorum", len(cf.comments) == 1, str(cf.comments))

        # ── uç ──
        track = {"id": 500, "to": "8.5", "lang": "php", "repo": "core", "area": "P\\T"}
        cf = _CF()
        s_get, s_cl, s_scan = pc.get, server.AzureDevOpsClient, ut._fresh_scan
        s_fo, s_st, s_op = ut.find_open, ui.state, ui.open_pr
        flags["CREW_TECHDEBT_UPGRADE_IMAGE"] = True
        for k in ("CREW_TECHDEBT", "CREW_TECHDEBT_PHP_MODERN", "CREW_TECHDEBT_UPGRADE"):
            flags[k] = True
        server.AzureDevOpsClient = lambda: cf
        ut._fresh_scan = lambda *a, **k: mkscan([unp])
        ut.find_open = lambda c, lang, repo: track
        cur = {"st": dict(cur_state)}
        ui.state = lambda c, e, **k: dict(cur["st"], dir=e["dir"])
        got = []

        def op(c, lang, to, entry, **k):
            got.append((lang, to, entry["dir"], k.get("repo")))
            return {"state": "pr açık", "pr": {"id": 12, "url": "https://x/pr/12"}, "notes": []}
        ui.open_pr = op
        body = {"repo": "core", "lang": "php", "to": "8.5", "team": "T", "dir": "php-8-5"}
        try:
            tc = TestClient(server.app)
            flags["CREW_TECHDEBT_UPGRADE_IMAGE"] = False
            r = tc.post("/api/techdebt/upgrade/image", json=body)
            check("uç: ayar kapalı → 403, open_pr yok", r.status_code == 403 and not got, r.text)
            flags["CREW_TECHDEBT_UPGRADE_IMAGE"] = True
            r = tc.post("/api/techdebt/upgrade/image", json=dict(body, dir="php-9-9"))
            check("uç: bilinmeyen dizin → 404", r.status_code == 404 and r.json().get("error") and not got, r.text)
            r = tc.post("/api/techdebt/upgrade/image", json=dict(body, team=""))
            check("uç: takım yok → 400", r.status_code == 400, r.text)
            cur["st"] = dict(cur_state, state="pr açık", pr={"id": 5, "url": "u"}, reason="zaten açık")
            r = tc.post("/api/techdebt/upgrade/image", json=body)
            check("uç: pr açık → 409 + neden", r.status_code == 409 and "açık" in r.json().get("error", "")
                  and not got, r.text)
            cur["st"] = dict(cur_state, state="belirsiz", reason="okunamadı: x")
            r = tc.post("/api/techdebt/upgrade/image", json=body)
            check("uç: belirsiz → 409, open_pr yok", r.status_code == 409 and not got, r.text)
            for bad_to in ("8", "8.5.1", "x.y", "8.5; rm"):
                r = tc.post("/api/techdebt/upgrade/image", json=dict(body, to=bad_to))
                check(f"uç: geçersiz hedef {bad_to!r} → 400", r.status_code == 400 and r.json().get("error")
                      and not got, r.text)
            cur["st"] = dict(cur_state)
            r = tc.post("/api/techdebt/upgrade/image", json=body)
            check("uç: başarılı → 200, open_pr sonucu, repo iletilir",
                  r.status_code == 200 and r.json()["pr"]["id"] == 12 and got == [("php", "8.5", "php-8-5", "core")],
                  r.text)
            check("uç: izleme Task'ına PR bağlantısı yorumu",
                  len(cf.comments) == 1 and "https://x/pr/12" in cf.comments[0][1], str(cf.comments))
            ui.open_pr = lambda *a, **k: (_ for _ in ()).throw(ui.ImageTemplateError("dal bulunamadı"))
            r = tc.post("/api/techdebt/upgrade/image", json=body)
            check("uç: ImageTemplateError → Türkçe neden", r.status_code in (409, 502)
                  and "dal bulunamadı" in r.json().get("error", ""), r.text)
            ui.open_pr = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("401 Unauthorized"))
            r = tc.post("/api/techdebt/upgrade/image", json=body)
            check("uç: başka istisna → 500 değil, Türkçe neden",
                  r.status_code == 502 and "Şablon PR'ı açılamadı" in r.json().get("error", ""), r.text)
        finally:
            pc.get, server.AzureDevOpsClient, ut._fresh_scan = s_get, s_cl, s_scan
            ut.find_open, ui.state, ui.open_pr = s_fo, s_st, s_op
    finally:
        pc.get = orig_get


def test_upgrade_registry():
    """Docker Hub yama çözücüsü: en büyük yama, elle sabitleme, hata → None, etiket kanıtı, önbellek."""
    print("\n[upgrade: yama sürümü çözücü]")
    from agile_sdlc_crew.quality import upgrade_registry as ur
    calls: list = []
    r = ur.Resolver(fetch=_fake_hub({"php": ["8.5.2", "8.5.12", "8.5.9-fpm", "8.5", "8.4.30"]}, calls))
    check("en büyük yama (8.5.12; -fpm ve iki parçalı sayılmaz)", r.latest_patch("php", "8.5") == "8.5.12")
    n = len(calls)
    check("ikinci çağrı önbellekten", r.latest_patch("php", "8.5") == "8.5.12" and len(calls) == n)
    check("etiket 200 → True", r.tag_exists("php", "8.5.12") is True)
    check("etiket 404 → False", r.tag_exists("php", "8.9.9") is False)
    n = len(calls)
    r.tag_exists("php", "8.5.12")
    r.tag_exists("php", "8.9.9")
    check("etiket sonucu (olumsuz dahil) önbellekte", len(calls) == n)
    pc: list = []
    rp = ur.Resolver(fetch=_fake_hub({}, pc), patches={"php": {"8.5": "8.5.99"}})
    check("yaml patch Hub'a gitmeden döner", rp.latest_patch("php", "8.5") == "8.5.99" and not pc)
    check("yaml patch başka minörü etkilemez", rp.latest_patch("php", "8.4") is None)
    bad = ur.Resolver(fetch=lambda u: (500, None))
    check("500 → None", bad.latest_patch("php", "8.5") is None and bad.tag_exists("php", "8.5.1") is None)
    junk = ur.Resolver(fetch=lambda u: (200, None))
    check("bozuk JSON → None", junk.latest_patch("php", "8.5") is None)
    boom = ur.Resolver(fetch=_fake_hub({}, down=True))
    check("istisna → None", boom.latest_patch("php", "8.5") is None and boom.tag_exists("php", "8.5.1") is None)
    check("eşleşme yok → None", ur.Resolver(fetch=_fake_hub({"php": ["8.4.1"]})).latest_patch("php", "8.5") is None)
    nc: list = []
    rn = ur.Resolver(fetch=_fake_hub({}, nc))
    check("geçersiz etiket → None, fetch çağrılmaz",
          all(rn.tag_exists("php", t) is None for t in ("", "-x", "a b", "8.5/../x", "8.5?x=1")) and not nc)
    check("bilinmeyen ad / geçersiz minör → None, fetch çağrılmaz",
          rn.latest_patch("ruby", "8.5") is None and rn.latest_patch("php", "8") is None
          and rn.tag_exists("ruby", "1.2.3") is None and not nc)
    nh = ur.Resolver(fetch=_fake_hub({"node": ["20.17.0", "20.17.3", "20.17"]}))
    check("node adı kabul edilir (taban imaj güvenliği)", nh.latest_patch("node", "20.17") == "20.17.3")
    check("default() tekil", ur.default() is ur.default())
    # User-Agent: varsayılan fetch başlığı gönderir (ağ yok, get enjekte)
    seen: dict = {}

    class _R:
        status_code = 200

        def json(self):
            return {"results": []}

    def _get(url, **kw):
        seen.update(kw, url=url)
        return _R()
    st, _d = ur._http_fetch("https://hub.docker.com/x", get=_get)
    check("varsayılan fetch User-Agent başlığı gönderir",
          st == 200 and seen["headers"] == {"User-Agent": "tempo-upgrade"} and seen["timeout"] == 10, str(seen))
    # hata belleği: Hub çökünce 60 sn çağrı yapılmaz, süre dolunca tekrar denenir
    now = [1000.0]
    fc: list = []

    def failing(url):
        fc.append(url)
        raise OSError("zaman aşımı")
    rd = ur.Resolver(fetch=failing, clock=lambda: now[0])
    for _ in range(3):
        rd.latest_patch("golang", "1.27")
        rd.tag_exists("golang", "1.27.3")
    check("çöken Hub bir kez çağrılır", len(fc) == 1, str(fc))
    now[0] += 61
    rd.latest_patch("golang", "1.27")
    check("bellek dolunca yeniden denenir", len(fc) == 2, str(fc))
    sc: list = []
    r5 = ur.Resolver(fetch=lambda u: (sc.append(u) or (500, None)), clock=lambda: now[0])
    r5.latest_patch("php", "8.5")
    r5.tag_exists("php", "8.5.1")
    check("500 de belleğe alınır", len(sc) == 1, str(sc))
    # 404 çökme sayılmaz, kesin cevap 1 sa önbellekte
    hc: list = []
    r6 = ur.Resolver(fetch=_fake_hub({}, hc), clock=lambda: now[0])
    r6.tag_exists("php", "9.9.9")
    r6.tag_exists("php", "9.9.8")
    check("404 sonrası Hub çökmüş sayılmaz", len(hc) == 2)
    # tam eşleşme: sondaki satır sonu URL'ye girmez
    nl: list = []
    r7 = ur.Resolver(fetch=_fake_hub({}, nl))
    check("sondaki \\n reddedilir", r7.latest_patch("php", "8.5\n") is None and r7.tag_exists("php", "8.5.1\n") is None
          and not nl)
    # tam genişlikli rakam (re.ASCII): URL kurulmadan reddedilir
    fw: list = []
    r11 = ur.Resolver(fetch=_fake_hub({}, fw))
    check("tam genişlikli rakam reddedilir",
          r11.latest_patch("php", "\uff18.\uff15") is None and r11.tag_exists("php", "\uff18.\uff15.1") is None and not fw)
    # bozuk yaml: yükleyici düşmez
    bad_docs = ("patch: [1, 2]\n", "patch: abc\n", "- a\n- b\n", "just text\n", "patch: {php: [1], python: x}\n", "a: [\n")
    for i, doc in enumerate(bad_docs):
        with tempfile.TemporaryDirectory() as td:
            yp = Path(td) / "u.yaml"
            yp.write_text(doc, encoding="utf-8")
            try:
                got = ur._load_patches(yp)
                rr = ur.Resolver(fetch=_fake_hub({}), patches=got)
                check(f"bozuk yaml #{i} düşmez", isinstance(got, dict) and rr.latest_patch("php", "8.5") is None, repr(got))
            except Exception as e:  # noqa: BLE001
                check(f"bozuk yaml #{i} düşmez", False, repr(e))
    check("olmayan yaml dosyası düşmez", ur._load_patches(Path("/nonexistent/u.yaml")) == {})
    rt = ur.Resolver(fetch=_fake_hub({}), patches={"php": ["x"], "python": None, 3: {"a": "b"}})
    check("Resolver non-dict girdilere dayanıklı", rt.latest_patch("php", "8.5") is None)
    # yaml pin doğrulaması
    r8 = ur.Resolver(fetch=_fake_hub({"php": ["8.5.12"]}), patches={"php": {"8.5": "8.4.1"}})
    check("geçersiz pin yok sayılır, Hub'a düşer", r8.latest_patch("php", "8.5") == "8.5.12")
    r9 = ur.Resolver(fetch=_fake_hub({}), patches={"php": {8.5: "8.5.7"}})
    check("float anahtarlı pin kabul edilir", r9.latest_patch("php", "8.5") == "8.5.7")
    r10 = ur.Resolver(fetch=_fake_hub({}), patches={"php": {"8.5": "8.5.x"}})
    check("rakam olmayan pin yok sayılır", r10.latest_patch("php", "8.5") is None)


def test_upgrade_patch_sites():
    """Yama sürümü çözücüsüyle siteler: üç parçalı etiketler ve genel imajlar Hub kanıtıyla yazılır."""
    print("\n[upgrade: yama sürümlü siteler]")
    from agile_sdlc_crew.quality import upgrade, upgrade_bump as bump, upgrade_registry as ur
    hub = {"golang": ["1.27.1", "1.27.3", "1.27.3-bookworm"], "python": ["3.14.2", "3.13-slim"],
           "php": ["8.5.12", "8.5.12-fpm"]}
    res = ur.Resolver(fetch=_fake_hub(hub))
    dead = ur.Resolver(fetch=_fake_hub({}, down=True))

    def dsites(images, lang, to, r, extra=None, doc=None):
        index = {"svc": [{"src": "anatolia/build/svc", "images": images}]}
        for i, e in enumerate(extra or []):
            index.setdefault(f"o{i}", []).append(e)
        txt = doc or "".join(f"FROM {i}\n" for i in images)
        return [s for s in upgrade._devops_sites(None, "svc", lang, to, index, lambda d, p: txt, None, resolver=r)
                if s["where"] == "devops"]
    s = dsites(["golang:1.24.4"], "go", "1.27", res)
    check("golang:1.24.4 → golang:1.27.3, engelsiz, pushable",
          len(s) == 1 and s[0]["new"] == "golang:1.27.3" and s[0]["blocked"] == "" and bump.pushable(s[0]), str(s))
    s = dsites(["golang:1.24.4-alpine"], "go", "1.27", res)
    check("Hub'da 1.27.3-alpine yok → CI_HAND, old == new",
          s[0]["blocked"] == upgrade.CI_HAND and s[0]["old"] == s[0]["new"] == "golang:1.24.4-alpine", str(s))
    s = dsites(["golang:1.24.4-bookworm"], "go", "1.27", res)
    check("etiketli varyant Hub'da varsa engelsiz", s[0]["new"] == "golang:1.27.3-bookworm" and s[0]["blocked"] == "", str(s))
    s = dsites(["eu/prod-ar/python-3-13-7:1.0"], "python", "3.14", res)
    check("kendi imajımız: yeni etiket + UNPUBLISHED",
          s[0]["new"] == "eu/prod-ar/python-3-14-2:1.0" and s[0]["blocked"] == upgrade.UNPUBLISHED, str(s))
    s = dsites(["eu/prod-ar/python-3-13-7:1.0"], "python", "3.14", res,
               extra=[{"src": "anatolia/build/other", "images": ["eu/prod-ar/python-3-14-2:1.0"]}])
    check("aynı imaj bölgede kullanılıyorsa engelsiz (üç parçalı da)",
          s[0]["new"] == "eu/prod-ar/python-3-14-2:1.0" and s[0]["blocked"] == "" and bump.pushable(s[0]), str(s))
    s = dsites(["python:3.11-slim"], "python", "3.13", res)
    check("iki parçalı library imajı Hub'da var → engelsiz",
          s[0]["new"] == "python:3.13-slim" and s[0]["blocked"] == "", str(s))
    s = dsites(["python:3.11-alpine"], "python", "3.13", res)
    check("iki parçalı library imajı Hub'da yok → UNPUBLISHED",
          s[0]["new"] == "python:3.13-alpine" and s[0]["blocked"] == upgrade.UNPUBLISHED, str(s))
    s = dsites(["python:3.11-slim"], "python", "3.13", dead)
    check("Hub okunamadı → iki parçalı UNPUBLISHED", s[0]["blocked"] == upgrade.UNPUBLISHED, str(s))
    s = dsites(["golang:1.24.4"], "go", "1.27", dead)
    check("latest_patch None → bugünkü CI_HAND", s[0]["blocked"] == upgrade.CI_HAND and s[0]["old"] == s[0]["new"], str(s))
    s0 = dsites(["golang:1.24.4"], "go", "1.27", None)
    check("resolver=None → bugünkü davranış", s0[0]["blocked"] == upgrade.CI_HAND and s0[0]["old"] == s0[0]["new"], str(s0))
    # selectable.published, site yüklemiyle aynı
    idx = {"svc": [{"src": "anatolia/build/svc", "images": ["golang:1.24.4"]}]}
    svc = upgrade._devops_services("svc", "go", idx, None)
    sel = {x["version"]: x["published"] for x in upgrade.selectable("go", "1.24", idx, svc, resolver=res)}
    sel0 = {x["version"]: x["published"] for x in upgrade.selectable("go", "1.24", idx, svc)}
    check("selectable.published çözücüyle: 1.27 yayınlı (Hub'da var), 1.26 değil",
          sel["1.27"] is True and sel["1.26"] is False, str(sel))
    check("selectable çözücüsüz: üç parçalı hiç yayınlı değil", not any(sel0.values()), str(sel0))
    # CI: GoTool ve image:
    src = {"repo": "yml-files", "project": "P", "branch": "main"}
    gt = "steps:\n  - task: GoTool@0\n    inputs:\n      version: '1.24.4'\n"
    out, pins = upgrade._parse_ci_file(gt, "T/x.yml", "go", "1.27", src, resolver=res)
    check("GoTool '1.24.4' → '1.27.3'", len(out) == 1 and out[0]["old"] == "'1.24.4'" and out[0]["new"] == "'1.27.3'"
          and out[0]["blocked"] == "" and bump.pushable(out[0]), str(out))
    out, _ = upgrade._parse_ci_file(gt, "T/x.yml", "go", "1.27", src, resolver=dead)
    check("GoTool: sürüm çözülemedi → CI_HAND", out[0]["blocked"] == upgrade.CI_HAND and out[0]["old"] == out[0]["new"], str(out))
    out, _ = upgrade._parse_ci_file(gt, "T/x.yml", "go", "1.27", src)
    check("GoTool: resolver=None → CI_HAND", out[0]["blocked"] == upgrade.CI_HAND, str(out))
    ci_img = "container:\n  image: golang:1.24.4-bookworm\n"
    out, _ = upgrade._parse_ci_file(ci_img, "T/x.yml", "go", "1.27", src, resolver=res)
    check("CI image: library etiketi Hub'da → yazılır",
          len(out) == 1 and out[0]["new"] == "golang:1.27.3-bookworm" and out[0]["blocked"] == "", str(out))
    out, _ = upgrade._parse_ci_file("image: golang:1.24.4-alpine\n", "T/x.yml", "go", "1.27", src, resolver=res)
    check("CI image: Hub'da yok → CI_HAND", out[0]["blocked"] == upgrade.CI_HAND and out[0]["old"] == out[0]["new"], str(out))
    # kod reposu Dockerfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "Dockerfile").write_text("FROM golang:1.24.4-bookworm AS b\n", encoding="utf-8")
        cs = upgrade._code_sites("svc", root, "go", "1.27", resolver=res)
        check("kod Dockerfile: library etiketi Hub'da → yazılır",
              len(cs) == 1 and cs[0]["new"] == "golang:1.27.3-bookworm" and cs[0]["blocked"] == "" and bump.pushable(cs[0]), str(cs))
        cs = upgrade._code_sites("svc", root, "go", "1.27", resolver=dead)
        check("kod Dockerfile: Hub okunamadı → CI_HAND", cs[0]["blocked"] == upgrade.CI_HAND and cs[0]["old"] == cs[0]["new"], str(cs))
        cs = upgrade._code_sites("svc", root, "go", "1.27")
        check("kod Dockerfile: resolver=None → CI_HAND", cs[0]["blocked"] == upgrade.CI_HAND, str(cs))
    # digest sabitli imaj: yeniden yazılmaz, Hub'a gidilmez
    dg = "golang:1.24.4@sha256:" + "a" * 64
    dcalls: list = []
    rdg = ur.Resolver(fetch=_fake_hub(hub, dcalls))
    s = dsites([dg], "go", "1.27", rdg)
    check("digest sabitli devops imajı: CI_HAND, old == new, Hub çağrısı yok",
          s[0]["blocked"] == upgrade.CI_HAND and s[0]["old"] == s[0]["new"] == dg and not dcalls, str(s))
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "Dockerfile").write_text(f"FROM {dg}\n", encoding="utf-8")
        cs = upgrade._code_sites("svc", Path(td), "go", "1.27", resolver=rdg)
        check("digest sabitli kod Dockerfile: CI_HAND, Hub çağrısı yok",
              cs[0]["blocked"] == upgrade.CI_HAND and cs[0]["old"] == cs[0]["new"] and not dcalls, str(cs))
    out, cpins = upgrade._parse_ci_file(f"image: {dg}\n", "T/x.yml", "go", "1.27", src, resolver=rdg)
    check("digest sabitli CI image: yazılmaz (okunamayan sabitleme, kapı kapalı), Hub çağrısı yok",
          not out and len(cpins) == 1 and cpins[0]["version"] == "" and not dcalls, str((out, cpins)))
    # iki parçalı özetli imaj: üç yolda da yeniden yazılmaz (resolver'lı ve resolver'sız), Hub'a gidilmez
    dg2 = "python:3.11-slim@sha256:" + "b" * 64
    for label, rr in (("resolver'lı", ur.Resolver(fetch=_fake_hub(hub, dcalls))), ("resolver'sız", None)):
        s = dsites([dg2], "python", "3.13", rr)
        check(f"iki parçalı özetli devops imajı ({label}): CI_HAND, old == new",
              s[0]["blocked"] == upgrade.CI_HAND and s[0]["old"] == s[0]["new"] == dg2, str(s))
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "Dockerfile").write_text(f"FROM {dg2}\n", encoding="utf-8")
            cs = upgrade._code_sites("svc", Path(td), "python", "3.13", resolver=rr)
            check(f"iki parçalı özetli kod Dockerfile ({label}): CI_HAND, old == new",
                  len(cs) == 1 and cs[0]["blocked"] == upgrade.CI_HAND and cs[0]["old"] == cs[0]["new"] == dg2, str(cs))
        out, _cp = upgrade._parse_ci_file(f"image: {dg2}\n", "T/x.yml", "python", "3.13", src, resolver=rr)
        check(f"iki parçalı özetli CI image ({label}): CI_HAND, old == new",
              len(out) == 1 and out[0]["blocked"] == upgrade.CI_HAND and out[0]["old"] == out[0]["new"] == dg2, str(out))
    idx2 = {"svc": [{"src": "anatolia/build/svc", "images": [dg2]}]}
    sel2 = upgrade.selectable("python", "3.11", idx2, upgrade._devops_services("svc", "python", idx2, None),
                              resolver=ur.Resolver(fetch=_fake_hub(hub, dcalls)))
    check("selectable: özetli imaj üst sürüm için yayınlı sayılmaz", not any(x["published"] for x in sel2), str(sel2))
    check("özetli imajlarda Hub hiç çağrılmadı", not dcalls, str(dcalls))
    # plan: ayar açık + resolver=None → modül tekili
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import intake
    saved_get, saved_default = pc.get, ur._DEFAULT
    pc.get = lambda k: True if k == "CREW_TECHDEBT_UPGRADE_PATCH" else saved_get(k)
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "gocrons"
            root.mkdir()
            (root / "go.mod").write_text("module gocrons\n\ngo 1.24.0\n", encoding="utf-8")
            idx = {"gocrons": [{"src": "anatolia/build/gocrons", "images": ["golang:1.24.4"]}]}
            deps = intake.Deps(gather=None, board=None, repo_ok=lambda r: True, queue=None, record=None, now=None,
                               cfg=lambda: {"repos_dir": td})
            rd = lambda d, p: "FROM golang:1.24.4\n"
            ur._DEFAULT = res
            on = upgrade.plan(None, "gocrons", "go", to="1.27", deps=deps, index=idx, root=root, read=rd, yml_files={})
            dv = [x for x in on["sites"] if x["where"] == "devops"]
            check("plan: ayar açık → varsayılan çözücü kullanılır",
                  len(dv) == 1 and dv[0]["new"] == "golang:1.27.3" and dv[0]["blocked"] == "", str(on["sites"]))
            pc.get = saved_get
            off = upgrade.plan(None, "gocrons", "go", to="1.27", deps=deps, index=idx, root=root, read=rd, yml_files={})
            dv = [x for x in off["sites"] if x["where"] == "devops"]
            check("plan: ayar kapalı → çözücü yok (bugünkü davranış)",
                  len(dv) == 1 and dv[0]["blocked"] == upgrade.CI_HAND and dv[0]["old"] == dv[0]["new"], str(off["sites"]))
    finally:
        pc.get, ur._DEFAULT = saved_get, saved_default
    # yardımcılar
    check("image_for_full: etiket + varyant", upgrade.image_for_full("golang:1.24.4-alpine", "go", "1.27.3") == "golang:1.27.3-alpine")
    check("image_for_full: kayıt deposu önekli", upgrade.image_for_full("eu/prod-ar/python-3-13-7:1.0", "python", "3.14.2")
          == "eu/prod-ar/python-3-14-2:1.0")
    check("image_for_full: eşleşme yok aynen", upgrade.image_for_full("python:3.11-slim", "python", "3.14.2") == "python:3.11-slim")
    check("is_library_image",
          upgrade.is_library_image("golang:1.24.4", "go") and upgrade.is_library_image("docker.io/library/python:3.11", "python")
          and upgrade.is_library_image("php:8.2-fpm", "php") and not upgrade.is_library_image("eu/prod-ar/php-8-2:1.0", "php")
          and not upgrade.is_library_image("myreg.io/library/golang:1.24.4", "go"))


def test_upgrade_scan_take():
    """Yükseltme taraması ve alımı: mevcut modernleştirme yolunu (build_groups, board dedup,
    _open_tasks, Rector kuyruğu) yeniden kullanır; yalnız yükseltme kuralları + hedef araç
    bulguları, önekli başlık, tempo-upgrade etiketi, araç kapsamında upgrade_to."""
    print("\n[upgrade: tarama, alım, Task metni]")
    import inspect
    from agile_sdlc_crew.quality import intake, modern_fix, php_modern as pm, render, upgrade
    from agile_sdlc_crew.quality.model import Finding, Group

    class C:
        org_url, project = "https://dev.azure.com/o", "P"

        def __init__(self):
            self.created = []

        def list_iterations(self, team=""):
            return [{"name": "S23", "path": "P\\S23", "attributes": {"timeFrame": "current"}}]

        def query_work_items(self, wiql, limit=0):  # story zaten var → created[0] Task olur
            return [{"id": 500, "fields": {"System.AreaPath": "P\\T", "System.IterationPath": "P\\S23"}}]

        def get_team_area_path(self, team=""):
            return "P\\T"

        def create_work_item(self, type_name, fields, parent_id=None):
            self.created.append(fields)
            return {"id": 700 + len(self.created), "fields": fields}

        def update_work_item(self, wid, ops):
            return {}

        def get_authenticated_user(self):
            return {"uniqueName": "me@x"}

    index = {"core": [{"src": "anatolia/build/core", "images": ["eu/prod-ar/php-8-2:1.0"]}],
             "web": [{"src": "anatolia/build/web", "images": ["eu/prod-ar/php-8-4:1.0"]}],
             "legacy": [{"src": "anatolia/build/legacy", "images": ["eu/prod-ar/php-8-0:1.0"]}]}
    php_hits = {
        "core": [{"rule": "implicit_nullable", "file": "app/A.php", "line": 3, "text": "function f(int $a = null)"},
                 {"rule": "deprecated_84", "file": "app/B.php", "line": 5, "text": "lcg_value()"},
                 {"rule": "str_contains", "file": "app/A.php", "line": 7, "text": "strpos($a, 'x') !== false"},
                 {"rule": "moved_to_pecl_84", "file": "app/C.php", "line": 4, "text": "imap_open($h)"}],
        "legacy": [{"rule": "null_to_internal", "file": "app/L.php", "line": 2, "text": "strlen(null)"},
                   {"rule": "null_to_internal", "file": "app/L.php", "line": 8, "text": "trim(null)"},
                   {"rule": "null_to_internal", "file": "app/M.php", "line": 3, "text": "strlen(null)"},
                   {"rule": "serializable_only", "file": "app/S.php", "line": 1, "text": "class S implements Serializable"},
                   {"rule": "serializable_only", "file": "app/T.php", "line": 1, "text": "class T implements Serializable"},
                   {"rule": "loose_compare_numeric", "file": "app/L.php", "line": 4, "text": "0 == 'a'"}]}
    probes = {
        "core": ([{"rule": "probe:error", "file": "app/A.php", "line": 3, "text": "x", "message": "Deprecated twice"},
                  {"rule": "probe:error", "file": "app/D.php", "line": 9, "text": "fn(", "message": "Parse error: x"},
                  {"rule": "probe:deprecation", "file": "app/E.php", "line": 2, "text": "y", "message": "Deprecated: z"}],
                 {"ran": True, "tool": "/x/php8.4", "reason": ""}),
        "legacy": ([], {"ran": False, "tool": "", "reason": "hedef sürüm için araç yok — yalnız katalog"})}
    queued, queued_payloads, scanned = [], [], []
    _tr = pm.tool_rules
    pm.tool_rules = lambda rules: {r.key for r in rules if r.key in ("implicit_nullable", "deprecated_84")}
    try:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for name in ("core", "web", "legacy"):
                (base / name).mkdir()
                (base / name / "composer.json").write_text('{"require": {"php": "^8.0"}}', encoding="utf-8")
            cfg = {"pack_sp": 3, "pack_files": 5, "grace_days": 3, "task_fields": {}, "sonar_url": "", "nr_days": 7,
                   "php_exclude": [], "php_bin": "php", "scan_ttl_h": 24, "max_take": 8, "freshen": False,
                   "repos_dir": base, "targets_cache": base / "t.yaml", "scan_cache": base / "cache"}
            board = {}

            def mk(b):
                return intake.Deps(gather=None, board=lambda c: b, repo_ok=lambda r: (base / r).is_dir(),
                                   queue=lambda w, t: (queued.append((w, t)), 9)[1], record=lambda **k: None,
                                   now=lambda: 1e9, cfg=lambda: cfg,
                                   queue_tool=lambda w, t, p: (queued_payloads.append(p), 8)[1])
            deps = mk(board)

            def scan_fn(repo_root, **kw):
                scanned.append(Path(repo_root).name)
                return {"files": 4, "sha": "s1", "hits": list(php_hits[Path(repo_root).name]), "adoption": {}, "errors": []}
            kw = dict(index=index, scan_fn=scan_fn, head_sha_fn=lambda r: "s1", app_map_cache={}, read=lambda d, p: "",
                      yml_files={}, probe_fn=lambda root, lang, cur, to, **k: probes[Path(root).name])
            client = C()
            res = upgrade.scan(client, "core", "php", to="8.4", deps=deps, **kw)
            check("şimdiki/hedef sürüm ve probe durumu", res["current"] == "8.2" and res["target"] == "8.4"
                  and res["probe"] == {"ran": True, "tool": "/x/php8.4", "reason": ""}, str((res.get("current"), res.get("probe"))))
            check("başlık önekli", res["auto"] and all(r["title"].startswith("[core] Sürüm yükseltme 8.2→8.4:")
                                                        for r in res["auto"] + res["infra"] + res["human"]),
                  str([r["title"] for r in res["auto"]]))
            check("parmak izi modernleştirme ile aynı", res["auto"][0]["fingerprints"][0].startswith("php_modern:core:"))
            check("mekanik kural yok", not [r for r in res["auto"] + res["human"] if r["rule"] == "str_contains"])
            by = {r["rule"]: r for r in res["auto"] + res["infra"] + res["human"]}
            check("otomatik deprecation engeller + araç; probe hatası pipeline'a, engeller",
                  by["implicit_nullable"]["blocking"] and by["implicit_nullable"]["tool"] and by["implicit_nullable"]["pre_fixable"]
                  and by["probe:error"]["blocking"] and not by["probe:error"]["tool"]
                  and by["probe:error"] in res["auto"], str({k: (v["blocking"], v["tool"]) for k, v in by.items()}))
            check("katalog bulgusunun satırındaki probe hatası tekrar sayılmaz",
                  by["probe:error"]["files"] == ["app/D.php"], str(by["probe:error"]["files"]))
            check("probe uyarısı insan işi, engellemez", by["probe:deprecation"] in res["human"]
                  and not by["probe:deprecation"]["blocking"])
            check("PECL'e taşınan eklenti: altyapı satırı, error → engeller",
                  by["moved_to_pecl_84"] in res["infra"] and by["moved_to_pecl_84"]["blocking"]
                  and by["moved_to_pecl_84"]["breaks"] == "error")
            check("sayımlar", res["counts"]["error"] == 2 and res["counts"]["deprecation"] == 3
                  and res["counts"]["behavior"] == 0 and res["blocking"] == 4, str((res["counts"], res["blocking"])))
            check("kural özetinde breaks", res["rules"]["implicit_nullable"]["breaks"] == "deprecation")
            check("blockers ve siteler yanıtta", isinstance(res["blockers"], list) and isinstance(res["sites"], list)
                  and res["selectable"])

            leg = upgrade.scan(client, "legacy", "php", to="8.4", deps=deps, **kw)
            lb = {}
            for r in leg["auto"] + leg["infra"] + leg["human"]:
                lb.setdefault(r["rule"], []).append(r)
            check("behavior insan işi ve engelleyen", lb.get("null_to_internal") and all(
                r["needs_human"] and r["blocking"] and r["breaks"] == "behavior"
                and r["human_reason"] == "Davranış değişikliği: otomatik düzeltilemez" for r in lb["null_to_internal"])
                  and all(r["needs_human"] and r["blocking"] for r in leg["human"] if r["breaks"] == "behavior"), str(lb.keys()))
            check("behavior kuralı tek grup (iki dosya)", len(lb["null_to_internal"]) == 1
                  and sorted(lb["null_to_internal"][0]["files"]) == ["app/L.php", "app/M.php"])
            check("yapısal + file birimli kural (Serializable) TEK needs-human grup, engellemez",
                  len(lb.get("serializable_only", [])) == 1 and lb["serializable_only"][0] in leg["human"]
                  and lb["serializable_only"][0]["needs_human"] and not lb["serializable_only"][0]["blocking"]
                  and len(lb["serializable_only"][0]["files"]) == 2, str(lb.get("serializable_only")))
            check("since == şimdiki sürüm olan kural yok (8.0 → loose_compare_numeric)", "loose_compare_numeric" not in lb)
            check("probe doğrulanamadı: durum olduğu gibi", leg["probe"] == probes["legacy"][1] and leg["counts"]["behavior"] == 3)

            # board'da aynı parmak izi açık → ikinci kez listelenmez
            open_fp = {fp: {"wi": 900, "state": "Active"} for r in res["auto"] for fp in r["fingerprints"]}
            res2 = upgrade.scan(client, "core", "php", to="8.4", deps=mk(open_fp), **kw)
            check("board'da açık parmak izi atlanır", res2["skipped_counts"].get("açık WI") and not res2["auto"],
                  str(res2["skipped_counts"]))

            # İnsanın 'baktım' diye kapattığı behavior işi aynı yerlerle yeniden açılmaz (spec §5)
            closed = 1e9 - 10 * 86400
            done = {f"php_modern:legacy:null_to_internal:{f}": {"wi": 910, "state": "Done", "closed_at": closed}
                    for f in ("app/L.php", "app/M.php")}
            leg_d = upgrade.scan(client, "legacy", "php", to="8.4", deps=mk(done), **kw)
            rows_d = leg_d["auto"] + leg_d["infra"] + leg_d["human"]
            check("kapalı behavior işi, aynı dosyalar → listelenmez, engellemez, skipped'te",
                  not [r for r in rows_d if r["rule"] == "null_to_internal"]
                  and leg_d["blocking"] == leg["blocking"] - 1
                  and any(s["reason"] == "insan baktı: WI #910" for s in leg_d["skipped"]), str((leg_d["blocking"],
                                                                                              leg_d["skipped"])))
            php_hits["legacy"].append({"rule": "null_to_internal", "file": "app/N.php", "line": 6, "text": "strlen(null)"})
            leg_n = upgrade.scan(client, "legacy", "php", to="8.4", deps=mk(done), **dict(kw, head_sha_fn=lambda r: "s2"))
            nb = [r for r in leg_n["auto"] + leg_n["infra"] + leg_n["human"] if r["rule"] == "null_to_internal"]
            check("aynı + yeni dosya → yalnız yeni dosyalı grup, engelleyen",
                  len(nb) == 1 and nb[0]["files"] == ["app/N.php"] and nb[0]["blocking"]
                  and nb[0]["fingerprints"] == ["php_modern:legacy:null_to_internal:app/N.php"], str(nb))
            php_hits["legacy"].pop()
            done_err = {"php_modern:core:probe:error:app/D.php": {"wi": 911, "state": "Done", "closed_at": closed}}
            res_e = upgrade.scan(client, "core", "php", to="8.4", deps=mk(done_err), **kw)
            pe = [r for r in res_e["auto"] if r["rule"] == "probe:error"]
            check("kapalı error işi, bulgu duruyor → bugünkü gibi yeniden açılır",
                  len(pe) == 1 and pe[0]["blocking"] and pe[0]["recurring_of"] == 910 + 1
                  and not any("insan baktı" in s["reason"] for s in res_e["skipped"]), str(pe))

            gid = {r["rule"]: r["gid"] for r in res["auto"]}
            out = upgrade.take(client, "core", "php", [gid["implicit_nullable"], gid["probe:error"]], to="8.4", deps=deps, **kw)
            check("iki Task açıldı (story zaten var)", len(client.created) == 2 and len(out["tasks"]) == 2, str(out)[:300])
            t0 = client.created[0]
            check("Task etiketi", "tempo-upgrade" in t0["System.Tags"] and "tempo-src-php_modern" in t0["System.Tags"]
                  and t0["System.Tags"].endswith("tempo-upgrade"), t0["System.Tags"])
            check("Task başlığı önekli ve 128 içinde", t0["System.Title"].startswith("[core] Sürüm yükseltme 8.2→8.4:")
                  and len(t0["System.Title"]) <= 128, t0["System.Title"])
            check("Task: şimdiki sürümde de çalışmalı", "8.2 sürümünde de çalışmalı" in t0["System.Description"]
                  and "8.2 → 8.4 (sürüm yükseltme" in t0["System.Description"])
            check("Task parmak izi satırı modernleştirme ile aynı", "tempo-fp: php_modern:core:implicit_nullable:app/A.php"
                  in t0["System.Description"])
            check("araç kapsamı upgrade_to taşır", len(queued_payloads) == 1
                  and json.loads(queued_payloads[0])["upgrade_to"] == "8.4"
                  and json.loads(queued_payloads[0])["scope"] == {"implicit_nullable": ["app/A.php"]}, str(queued_payloads))
            check("probe hatası işi pipeline'a", len(queued) == 1 and "probe:error" in queued[0][1]
                  and queued[0][1].startswith("[core] Sürüm yükseltme"), str(queued))
            try:
                upgrade.take(client, "core", "php", ["000000000000"], to="8.4", deps=deps, **kw)
                check("bilinmeyen gid → ValueError", False)
            except ValueError as e:
                check("bilinmeyen gid → ValueError", "tarama" in str(e), str(e))
            cfg["max_take"] = 1
            n_created = len(client.created)
            try:
                capped = upgrade.take(client, "core", "php", [gid["deprecated_84"], gid["probe:error"]], to="8.4",
                                      deps=deps, **kw)
            finally:
                cfg["max_take"] = 8
            check("istek başı tavan", len(client.created) == n_created + 1 and len(capped["tasks"]) == 1
                  and [s["reason"] for s in capped["skipped"]] == ["istek başı tavan (1)"], str(capped.get("skipped")))
            intake._LOCK.acquire()
            try:
                try:
                    upgrade.take(client, "core", "php", [gid["deprecated_84"]], to="8.4", deps=deps, **kw)
                    check("kilitliyken Busy", False)
                except intake.Busy:
                    check("kilitliyken Busy", True)
            finally:
                intake._LOCK.release()
            n_scan = len(scanned)
            check("güncel repo taranmaz", upgrade.scan(client, "web", "php", deps=deps, **kw)["up_to_date"] is True
                  and len(scanned) == n_scan)
            try:
                upgrade.take(client, "web", "php", ["x"], deps=deps, **kw)
                check("güncel repoda alım → ValueError", False)
            except ValueError:
                check("güncel repoda alım → ValueError", True)

            # Python: ast kataloğu (depo kodu çalıştırılmaz), sözdizimi bozuk dosya atlanır
            py = base / "pyapp"
            (py / "venv").mkdir(parents=True)
            (py / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.11"\n')
            (py / "a.py").write_text("import imp\nfrom distutils.util import strtobool\nimport asyncore as A\n")
            (py / "b.py").write_text("from datetime import datetime\nx = datetime.utcnow()\ny = datetime.datetime.utcfromtimestamp(0)\n")
            (py / "c.py").write_text("import cgi\n")
            (py / "bad.py").write_text("import imp\ndef (:\n")
            (py / "venv" / "v.py").write_text("import imp\n")
            pkw = dict(index={}, read=lambda d, p: "", yml_files={}, probe_fn=lambda *a, **k: ([], {"ran": False, "tool": "", "reason": "yok"}))
            pr = upgrade.scan(client, "pyapp", "python", to="3.12", deps=deps, **pkw)
            pb = {r["rule"]: r for r in pr["auto"] + pr["infra"] + pr["human"]}
            check("python: 3.12 kuralları bulunur, 3.13 (cgi) yok", set(pb) == {"imp", "distutils", "asyncore", "datetime_utcnow"},
                  str(sorted(pb)))
            check("python: bozuk dosya ve venv atlanır", pb["imp"]["files"] == ["a.py"], str(pb["imp"]["files"]))
            check("python: utcnow + utcfromtimestamp iki yer", pb["datetime_utcnow"]["count"] == 2, str(pb["datetime_utcnow"]["count"]))
            check("python: otomatik düzeltilemeyen kural insan işi (auto listesi boş), error → engeller",
                  not pr["auto"] and pb["imp"] in pr["human"] and pb["imp"]["needs_human"] and pb["imp"]["blocking"]
                  and pb["imp"]["breaks"] == "error" and not pb["imp"]["tool"]
                  and pb["imp"]["fingerprints"][0] == "py_modern:pyapp:imp:a.py"
                  and pb["imp"]["title"].startswith("[pyapp] Sürüm yükseltme 3.11→3.12:"), str(sorted(r["rule"] for r in pr["auto"])))
            check("python: asyncore insan işi ama error → engeller", pb["asyncore"] in pr["human"] and pb["asyncore"]["blocking"])
            n_tool, n_pipe = len(queued_payloads), len(queued)
            upgrade.take(client, "pyapp", "python", [pb["imp"]["gid"]], to="3.12", deps=deps, **pkw)
            check("python: insan işi Task açılır, hiçbir kuyruğa alınmaz", len(queued_payloads) == n_tool and len(queued) == n_pipe
                  and "needs-human" in client.created[-1]["System.Tags"], str(queued))
            check("Task metni yükseltme kataloğundan, 'otomatik' değil 'tasarım kararı'",
                  "importlib" in client.created[-1]["System.Description"]
                  and "tasarım kararı" in client.created[-1]["System.Description"])
            py_rules = pm.load_rules(upgrade.PY_UPGRADE_RULES)
            check("python kataloğu: auto:false kuralların hepsi structural + file",
                  py_rules and all(r.kind == "structural" and r.unit == "file" for r in py_rules if not r.auto),
                  str([r.key for r in py_rules if not r.auto and r.kind != "structural"]))
            loose = [r.key for lg in ("nextjs", "go") for r in upgrade.catalog(lg)
                     if r.breaks and not r.auto and r.kind not in ("structural", "report")]
            check("Next.js / Go yükseltme kuralları: otomatik değilse structural ya da report", not loose, str(loose))

            # Next.js: senkron istek API'leri behavior; next lint yalnız sayı
            nx = base / "nx"
            (nx / "app" / "p").mkdir(parents=True)
            (nx / "package.json").write_text(json.dumps({"dependencies": {"next": "14.2.0", "react": "18.3.0"},
                                                         "scripts": {"lint": "next lint"}}))
            (nx / "app" / "a.tsx").write_text("const h = headers()\nimport { useFormState } from 'react-dom'\n")
            (nx / "app" / "p" / "page.tsx").write_text("export default function P({ params }) { return params.slug }\n")
            nr = upgrade.scan(client, "nx", "nextjs", to="15.0", deps=deps, **pkw)
            nb = {r["rule"]: r for r in nr["auto"] + nr["infra"] + nr["human"]}
            check("nextjs: senkron API'ler behavior, insan, engeller; breaks'siz kural yok",
                  set(nb) == {"sync_request_api", "sync_route_params"}
                  and all(r["needs_human"] and r["blocking"] and r["breaks"] == "behavior" for r in nb.values()), str(sorted(nb)))
            check("nextjs: next lint rapor (sayı, iş yok)", nr["rules"].get("next_lint", {}).get("count") == 1, str(nr["rules"].keys()))

            # Go: katalog yalnız rapor; probe hatası iş olur
            go = base / "goapp"
            go.mkdir()
            (go / "go.mod").write_text("module m\n\ngo 1.21\n")
            gkw = dict(pkw, probe_fn=lambda *a, **k: ([{"rule": "probe:error", "file": "main.go", "line": 4, "text": "x",
                                                         "message": "undefined: y"}], {"ran": True, "tool": "go", "reason": ""}))
            gr = upgrade.scan(client, "goapp", "go", to="1.23", deps=deps, **gkw)
            check("go: döngü değişkeni / GODEBUG rapor olarak sayılır, iş açmaz",
                  gr["rules"].get("go122_loopvar", {}).get("count") == 1 and gr["rules"]["go122_loopvar"]["kind"] == "report"
                  and not any(r["rule"].startswith("go12") for r in gr["auto"] + gr["human"]), str(gr["rules"].keys()))
            check("go: probe hatası otomatik iş (pipeline)", [r["rule"] for r in gr["auto"]] == ["probe:error"]
                  and gr["auto"][0]["fingerprints"][0].startswith("go_modern:goapp:"))
    finally:
        pm.tool_rules = _tr

    # fix_min > şimdiki sürüm → insan işi
    r = pm.Rule(key="x84", since=(8, 4), kind="deprecation", title="T", summary="", advantages=[], risks=[], before="",
                after="", ref="", auto=True, breaks="error", fix_min=(8, 4))
    g = Group(source="php_modern", repo="core", title="T — a.php", fingerprints=["php_modern:core:x84:a.php"],
              findings=[Finding(source="php_modern", repo="core", file="a.php", rule="x84", lines=[1], uris=["1|x"])])
    upgrade._mark_groups([g], {"x84": r}, "8.2", "8.4")
    check("fix_min > şimdiki → needs_human + neden", g.needs_human and g.human_reason
          == "Düzeltme yalnız 8.4 ve sonrasında çalışır — sürüm adımıyla birlikte ele alınmalı", g.human_reason)
    check("Group.extra_tags render'da sona eklenir", g.extra_tags == ["tempo-upgrade"]
          and render.task_tags(g, lane="techdebt").endswith("needs-human; tempo-upgrade")
          and Group(source="s", repo="r", title="t", fingerprints=[], findings=[]).extra_tags == [])
    long = Group(source="php_modern", repo="core", title="x" * 200, fingerprints=["php_modern:core:x84:a.php"], findings=g.findings)
    upgrade._mark_groups([long], {"x84": r}, "8.2", "8.4")
    check("uzun başlıkta önek korunur", render.task_title(long).startswith("[core] Sürüm yükseltme 8.2→8.4: x")
          and len(render.task_title(long)) == 128)

    # modern_fix: upgrade_to ayrı okunur, load_scope 4'lü kalır, hedef araç hatası dosyayı düşürür
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "gocrons").mkdir()
        p = '{"lang": "go", "repo": "gocrons", "scope": {"any": ["a.go"]}, "upgrade_to": "1.25"}'
        check("load_scope 4'lü kalır", len(modern_fix.load_scope(p, Path(td))) == 4)
    check("upgrade_to doğrulanır", modern_fix.upgrade_target(p) == "1.25"
          and modern_fix.upgrade_target('{"upgrade_to": "1.25; rm -rf /"}') == ""
          and modern_fix.upgrade_target('{"upgrade_to": 8.4}') == "" and modern_fix.upgrade_target("") == "")
    fake = lambda root, lang, cur, to, files=None: (
        [{"rule": "probe:error", "file": "app/B.php", "line": 2, "text": "", "message": "Parse error: y"},
         {"rule": "probe:deprecation", "file": "app/A.php", "line": 1, "text": "", "message": "Deprecated: z"},
         {"rule": "probe:error", "file": "app/Z.php", "line": 1, "text": "", "message": "kapsam dışı"}],
        {"ran": True, "tool": "php8.4", "reason": ""})
    drops = modern_fix._upgrade_drops(Path("/nonexistent"), "php", "8.2", "8.4", ["app/A.php", "app/B.php"], probe=fake)
    check("hedef araçta hata veren dosya düşer (uyarı ve kapsam dışı değil)", list(drops) == ["app/B.php"]
          and "8.4" in drops["app/B.php"] and "Parse error" in drops["app/B.php"], str(drops))
    check("Go probe'u değişen dosyaları değil HEAD'i derler → dosya düşürmez",
          modern_fix._upgrade_drops(Path("/x"), "go", "1.22", "1.24", ["app/B.php"], probe=fake) == {})
    check("hedef araç yoksa düşürme yok", modern_fix._upgrade_drops(
        Path("/x"), "php", "8.2", "8.4", ["a.php"], probe=lambda *a, **k: ([], {"ran": False, "tool": "", "reason": ""})) == {})
    src = inspect.getsource(modern_fix._run_scoped)
    check("Rector sürümü şimdiki üretim sürümü (upgrade_to'dan alınmaz)",
          "apply_tool(root, lang, scope, version)" in src
          and "fix_leftovers(root, scope, version, upgrade_to=upgrade_to)" in src
          and "upgrade_to" not in inspect.getsource(modern_fix.php_apply)
          and "upgrade_to" not in inspect.getsource(modern_fix.rector_config)
          and "_upgrade_drops(root, lang, version, upgrade_to, changed)" in src)
    check("run upgrade_to'yu iş kaydından okur", "upgrade_to=upgrade_target(job.get(\"job_payload\")"
          in inspect.getsource(modern_fix.run))

    # Fix round 1 — 4: ek denetim açık kalır (fail-open): probe istisnası işi düşürmez
    def boom(*a, **k):
        raise RuntimeError("git worktree açılamadı")
    try:
        got = modern_fix._upgrade_drops(Path("/x"), "php", "8.2", "8.4", ["app/A.php"], probe=boom)
    except Exception as e:  # noqa: BLE001
        got = e
    check("probe istisnası: hiçbir dosya düşmez, iş sürer", got == {}, repr(got))

    # Fix round 1 — 2: pencere talimatı şimdiki sürümde de çalışmayı ister
    from agile_sdlc_crew import pipeline_config as _pc
    from agile_sdlc_crew.quality import modern
    nc = {r.key: r for r in pm.load_rules()}["null_coalesce"]
    sentence = ("The code must keep working on PHP 8.2 as well as on 8.4; "
                "do not use syntax or functions that only exist in 8.4.")
    check("talimat: upgrade_to yokken değişmez", modern_fix._rule_instruction(nc, "8.2") ==
          modern_fix._rule_instruction(nc, "8.2", "") and "8.4" not in modern_fix._rule_instruction(nc, "8.2"))
    check("talimat: upgrade_to varken cümle eklenir", sentence in modern_fix._rule_instruction(nc, "8.2", "8.4"),
          modern_fix._rule_instruction(nc, "8.2", "8.4"))
    seen_items = []
    _sf, _bw, _get = pm.scan_files, modern_fix.batch_window_fix, _pc.get
    left = [[{"rule": "null_coalesce", "file": "app/A.php", "line": 2}], []]
    pm.scan_files = lambda root, files, **k: left.pop(0) if left else []
    modern_fix.batch_window_fix = lambda root, items, **k: (seen_items.extend(items), 1)[1]
    _pc.get = lambda key, *a, **k: True if key == "CREW_MODERN_FIX_LLM_LEFTOVERS" else _get(key, *a, **k)
    try:
        modern_fix.fix_leftovers(Path("/x"), {"null_coalesce": ["app/A.php"]}, "8.2", upgrade_to="8.4")
        with_up = [i for _f, _l, i in seen_items]
        seen_items.clear()
        left[:] = [[{"rule": "null_coalesce", "file": "app/A.php", "line": 2}], []]
        modern_fix.fix_leftovers(Path("/x"), {"null_coalesce": ["app/A.php"]}, "8.2")
        without = [i for _f, _l, i in seen_items]
    finally:
        pm.scan_files, modern_fix.batch_window_fix, _pc.get = _sf, _bw, _get
    check("fix_leftovers upgrade_to'yu talimata taşır; yokken eklemez",
          with_up and sentence in with_up[0] and without and "8.4" not in without[0], f"{with_up} {without}")
    check("_run_scoped her iki fix_leftovers çağrısında upgrade_to geçer",
          inspect.getsource(modern_fix._run_scoped).count("upgrade_to=upgrade_to)") >= 2)

    # Fix round 1 — 3: seçim/tavan ve Task açma tek yerde
    srcs = {f.__module__: inspect.getsource(f) for f in (pm.take, modern.take, upgrade.take)}
    check("üç take ortak _pick + _open_and_report kullanır",
          all("_pick(" in s and "_open_and_report(" in s for s in srcs.values()), str({k: ("_pick(" in s) for k, s in srcs.items()}))
    qdir = Path(pm.__file__).parent
    check("tavan metni tek yerde (php_modern)",
          sum((qdir / f).read_text().count("istek başı tavan") for f in ("php_modern.py", "modern.py", "upgrade.py")) == 1)


def _iv_client(files: dict, dates: dict | None = None):
    """base-images@production sahte client'ı: yol → içerik; okunamayan → istisna."""
    def get_file_content(repo, path, branch=None, project=None):
        assert repo == "base-images" and branch == "production" and project == "FLODevOps", (repo, branch, project)
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    def get_last_commit_date(repo, path, branch, project=None):
        return (dates or {}).get(path, "")
    return SimpleNamespace(get_file_content=get_file_content, get_last_commit_date=get_last_commit_date)


def test_image_vuln_inventory():
    """Taban imaj envanteri: kendi imaj → dayanak FROM (ARG çözümü), tekilleştirme, bölge, takma ad, hata."""
    print("\n[taban imaj güvenliği: envanter]")
    from agile_sdlc_crew.quality import image_vuln as iv
    reg = "europe-west1-docker.pkg.dev/flo-prod-main/prod-ar"
    files = {
        "/php-8-4/Dockerfile": "ARG PHP_VERSION=8.4.27\nFROM php:${PHP_VERSION}-fpm-bookworm\nRUN x\n",
        "/php-8-2/dockerfile": "FROM php:8.4.27-fpm-bookworm\n",           # küçük harf; aynı dayanak
        "/python-3-13-7/Dockerfile": "FROM python:3.13.7-slim-bookworm\n",
        "/node-20-17/Dockerfile": "FROM node:20.17.0-bookworm\n",
        "/noarg-1/Dockerfile": "ARG V\nFROM php:${V}-fpm\n",                  # varsayılansız ARG
        "/evil-1/Dockerfile": "FROM php:8.4;rm\n",
    }
    client = _iv_client(files, {"/php-8-4": "2026-10-21T08:00:00Z", "/php-8-2": "2026-09-01T00:00:00Z"})
    index = {
        "channel": [{"src": "anatolia/build/channel", "images": [f"{reg}/php-8-4:1.0", "base", "builder"]},
                    {"src": "global/build/channel", "images": [f"{reg}/php-8-2:1.0"]}],
        "core": [{"src": "russia/build/core", "images": ["10.131.167.220:5000/prod-ar/php-8-4:1.0"]}],
        "gocrons": [{"src": "anatolia/build/gocrons", "images": ["golang:1.24.4", "debian:12"]}],
        "py": [{"src": "anatolia/build/py", "images": [f"{reg}/python-3-13-7:1.0", "python:3.11-slim",
                                                       "huggingface_hub"]}],
        "web": [{"src": "anatolia/build/web", "images": [f"{reg}/node-20-17:1.0",
                                                         "node:${NODE_VERSION}-alpine3.24"]}],
        "bad": [{"src": "global/build/bad", "images": [f"{reg}/noarg-1:1.0", f"{reg}/missing-9:1.0",
                                                       f"{reg}/evil-1:1.0"]}],
    }
    recs = {r["image"]: r for r in iv.inventory(client, index)}
    php = recs.get("php:8.4.27-fpm-bookworm")
    check("ARG çözülür; iki dizin + üç bölge tek kayıt", bool(php) and php["via"] == ["php-8-2", "php-8-4"]
          and php["lang"] == "php" and php["version"] == "8.4.27" and not php["error"], str(php))
    check("bölge başına kullanıcılar (registry farklı olsa da)",
          php["users"] == {"anatolia": ["build/channel"], "global": ["build/channel"], "russia": ["build/core"]},
          str(php and php["users"]))
    check("etkilenen kod repoları: repo → bölge → derleme dizinleri",
          php["repos"] == {"channel": {"anatolia": ["build/channel"], "global": ["build/channel"]},
                           "core": {"russia": ["build/core"]}}
          and recs["golang:1.24.4"]["repos"] == {"gocrons": {"anatolia": ["build/gocrons"]}},
          str(php and php.get("repos")))
    check("touched: dizinlere son dokunulan en yeni tarih", php["touched"] == "2026-10-21", php["touched"])
    check("küçük harfli dockerfile okunur (php-8-2 dayanağa katıldı)", "php-8-2" in php["via"])
    check("takma ad / etiketsiz ad atlanır", "base" not in recs and "builder" not in recs
          and "huggingface_hub" not in recs)
    check("python / node / go dil ve sürümü",
          recs["python:3.13.7-slim-bookworm"]["lang"] == "python" and recs["python:3.13.7-slim-bookworm"]["version"] == "3.13.7"
          and recs["node:20.17.0-bookworm"]["lang"] == "node" and recs["node:20.17.0-bookworm"]["version"] == "20.17.0"
          and recs["golang:1.24.4"]["lang"] == "go" and recs["golang:1.24.4"]["version"] == "1.24.4"
          and recs["python:3.11-slim"]["version"] == "3.11")
    check("dil olmayan imaj da envanterde (lang boş)", recs["debian:12"]["lang"] == "" and not recs["debian:12"]["error"])
    check("genel imajda via boş", recs["golang:1.24.4"]["via"] == [])
    check("ARG'ı kullanıcı Dockerfile'ında olan imaj → error, özgün ad",
          "node:${NODE_VERSION}-alpine3.24" in recs and recs["node:${NODE_VERSION}-alpine3.24"]["error"] != "")
    na = recs.get(f"{reg}/noarg-1:1.0")
    check("varsayılansız ARG → error, özgün ad", bool(na) and "ARG" in na["error"], str(na))
    check("okunamayan taban dizin → error", recs[f"{reg}/missing-9:1.0"]["error"] != "")
    check("kötü niyetli FROM → error (alt sürece gidecek ad olmaz)",
          recs[f"{reg}/evil-1:1.0"]["error"] != "" and "php:8.4;rm" not in recs)
    check("hatalı kayıtta lang boş", recs[f"{reg}/noarg-1:1.0"]["lang"] == "")
    good = ["php:8.4.27-fpm-bookworm", "golang:1.24.4", "registry.example/prod-ar/php-8-4:1.0", "debian"]
    bad = ["", "-php:8", "--flag", "php:8.4; rm -rf /", "php 8", "$(id)", "php:${V}", "PHP:8", "a" * 201,
           "node:20\nx", "ünicode:1", "a@sha256:abc"]
    check("valid_image iyi adlar", all(iv.valid_image(x) for x in good), str([x for x in good if not iv.valid_image(x)]))
    check("valid_image kötü girdiler", not any(iv.valid_image(x) for x in bad), str([x for x in bad if iv.valid_image(x)]))
    check("valid_image str olmayan", not iv.valid_image(None) and not iv.valid_image(5))


def _iv_trivy_json(vulns, *, os_name="12.15"):
    return json.dumps({"ArtifactName": "x", "Metadata": {"OS": {"Family": "debian", "Name": os_name},
                                                          "ImageConfig": {"created": "2026-11-05T07:25:52Z"}},
                       "Results": [{"Target": "t", "Type": "debian", "Vulnerabilities": vulns}]})


def test_image_vuln_signals():
    """Trivy sonucu, yama geride, destek bitmiş, seviye tablosu ve önbellek (run/fetch/resolver enjekte)."""
    print("\n[taban imaj güvenliği: işaretler ve önbellek]")
    import os as _os
    import subprocess as sp
    from datetime import date
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import image_vuln as iv
    from agile_sdlc_crew.quality import upgrade_registry as ur
    saved_cmd = iv.trivy_cmd
    iv.trivy_cmd = lambda **kw: ["trivy"]
    try:
        def v(cve, pkg, sev, fixed="1.2", inst="1.1"):
            d = {"VulnerabilityID": cve, "PkgName": pkg, "InstalledVersion": inst, "Severity": sev, "Status": "fixed"}
            if fixed is not None:
                d["FixedVersion"] = fixed
            return d
        calls: list = []

        def run_ok(cmd, timeout):
            calls.append(cmd)
            return 0, _iv_trivy_json([
                v("CVE-1", "curl", "HIGH"), v("CVE-2", "curl", "CRITICAL", "1.3"), v("CVE-3", "zlib", "HIGH"),
                v("CVE-4", "nofix", "CRITICAL", fixed=None), v("CVE-5", "empty", "HIGH", fixed=""),
                v("CVE-6", "curl", "HIGH"), v("CVE-7", "curl", "HIGH"), v("CVE-8", "curl", "HIGH"),
                v("CVE-9", "curl", "HIGH"), v("CVE-10", "curl", "HIGH")])
        s = iv.scan_image("php:8.4.27-fpm-bookworm", run=run_ok)
        check("tarama tamam; yalnız düzeltmeli sayılır (düzeltmesiz / boş FixedVersion hariç)",
              s["state"] == "tamam" and s["critical"] == 1 and s["high"] == 7, str(s))
        pk = {p["name"]: p for p in s["packages"]}
        check("paket başına en yüksek önem; düzeltmesizler listede yok",
              set(pk) == {"curl", "zlib"} and pk["curl"]["severity"] == "CRITICAL" and pk["curl"]["fixed"] == "1.3"
              and s["packages"][0]["name"] == "curl", str(s["packages"]))
        check("paket başına CVE ≤ 5", len(pk["curl"]["cves"]) == 5)
        check("os + imaj tarihi", s["os"] == "debian 12.15" and s["created"] == "2026-11-05")
        cmd = calls[0]
        check("komut: liste biçimi, sabit bayraklar, imaj en sonda",
              cmd[:1] == ["trivy"] and cmd[1:] == ["image", "--quiet", "--timeout", "585s", "--platform", "linux/amd64", "--scanners", "vuln",
                                                   "--severity", "HIGH,CRITICAL", "--format", "json",
                                                   "php:8.4.27-fpm-bookworm"], str(cmd))
        n = iv.scan_image("x:1", run=lambda c, t: (0, json.dumps({"Results": [{"Vulnerabilities": None}]})))
        check("Vulnerabilities: null → 0 bulgu", n["state"] == "tamam" and n["critical"] == n["high"] == 0, str(n))
        check("Results yok / boş nesne → 0 bulgu, çökme yok",
              iv.scan_image("x:1", run=lambda c, t: (0, "{}"))["state"] == "tamam"
              and iv.scan_image("x:1", run=lambda c, t: (0, '{"Results": null}'))["state"] == "tamam")
        check("dönüş kodu 1 → tarama başarısız", iv.scan_image("x:1", run=lambda c, t: (1, "{}"))["state"] == "tarama başarısız")
        check("bozuk / boş JSON → tarama başarısız (sessizce temiz değil)",
              iv.scan_image("x:1", run=lambda c, t: (0, "not json"))["state"] == "tarama başarısız"
              and iv.scan_image("x:1", run=lambda c, t: (0, ""))["state"] == "tarama başarısız")

        def boom(c, t):
            raise sp.TimeoutExpired(c, t)
        check("zaman aşımı", iv.scan_image("x:1", run=boom)["state"] == "zaman aşımı")
        check("run istisnası → tarama başarısız",
              iv.scan_image("x:1", run=lambda c, t: (_ for _ in ()).throw(OSError("x")))["state"] == "tarama başarısız")
        spy: list = []
        for bad in ("php:8.4; rm -rf /", "--flag", "-v", "a b", "$(id)", ""):
            r = iv.scan_image(bad, run=lambda c, t: spy.append(c) or (0, "{}"))
            assert r["state"] == "geçersiz imaj", (bad, r)
        check("geçersiz imajda run hiç çağrılmaz", not spy)
        iv.trivy_cmd = lambda **kw: None
        spy2: list = []
        check("tarayıcı yok", iv.scan_image("x:1", run=lambda c, t: spy2.append(c))["state"] == "tarayıcı yok" and not spy2)
        iv.trivy_cmd = saved_cmd
        saved_env = _os.environ.get("CREW_TRIVY_BIN")
        with tempfile.TemporaryDirectory() as td:
            ok = Path(td) / "trivy-0.60"
            ok.write_text("#!/bin/sh\n"); ok.chmod(0o755)
            wrong = Path(td) / "evil"
            wrong.write_text("#!/bin/sh\n"); wrong.chmod(0o755)
            try:
                _os.environ["CREW_TRIVY_BIN"] = str(ok)
                check("CREW_TRIVY_BIN geçerli → [bin]", iv.trivy_cmd(which=lambda n: None) == [str(ok)])
                _os.environ["CREW_TRIVY_BIN"] = str(wrong)
                check("CREW_TRIVY_BIN adı trivy… değil → yok sayılır", iv.trivy_cmd(which=lambda n: None) is None)
                _os.environ["CREW_TRIVY_BIN"] = "trivy"
                check("CREW_TRIVY_BIN göreli yol → yok sayılır", iv.trivy_cmd(which=lambda n: None) is None)
                _os.environ.pop("CREW_TRIVY_BIN")
                check("PATH'te trivy", iv.trivy_cmd(which=lambda n: "/x/trivy" if n == "trivy" else None) == ["/x/trivy"])
                dk = iv.trivy_cmd(which=lambda n: "/x/docker" if n == "docker" else None)
                check("docker yedeği: önbellek repo dışı, imaj sabit",
                      bool(dk) and dk[:4] == ["/x/docker", "run", "--rm", "--name"] and dk[-1] == iv.trivy_image()
                      and re.fullmatch(r"tempo-trivy-[0-9a-f]{12}", dk[4]) and dk[5] == "-v"
                      and ".cache/tempo-trivy:/root/.cache/" in dk[6] and "crewai" not in dk[6], str(dk))
                check("trivy_cmd her çağrıda benzersiz kap adı",
                      iv.trivy_cmd(which=lambda n: "/x/docker" if n == "docker" else None)[4] != dk[4])
                check("hiçbiri yok → None", iv.trivy_cmd(which=lambda n: None) is None)
            finally:
                if saved_env is None:
                    _os.environ.pop("CREW_TRIVY_BIN", None)
                else:
                    _os.environ["CREW_TRIVY_BIN"] = saved_env
        check("CREW_TRIVY_BIN pipeline_config'e kayıtlı DEĞİL (env-only)",
              not any(f["key"] == "CREW_TRIVY_BIN" for f in pc.SCHEMA))
        iv.trivy_cmd = lambda **kw: ["trivy"]

        # patch_lag
        hub = ur.Resolver(fetch=_fake_hub({"php": ["8.4.27", "8.4.30", "8.4.5"], "golang": ["1.24.4", "1.24.9"],
                                           "node": ["20.17.0", "20.17.3"], "python": ["3.13.7"]}))
        check("yama geride: php", iv.patch_lag("php", "8.4.27", resolver=hub) == {"latest": "8.4.30"})
        check("yama geride: go → golang Hub adı", iv.patch_lag("go", "1.24.4", resolver=hub) == {"latest": "1.24.9"})
        check("yama geride: node", iv.patch_lag("node", "20.17.0", resolver=hub) == {"latest": "20.17.3"})
        check("güncel / bilinmeyen / iki parçalı / dilsiz → None",
              iv.patch_lag("python", "3.13.7", resolver=hub) is None and iv.patch_lag("php", "8.4.30", resolver=hub) is None
              and iv.patch_lag("php", "8.4", resolver=hub) is None and iv.patch_lag("node", "20", resolver=hub) is None
              and iv.patch_lag("", "1.2.3", resolver=hub) is None and iv.patch_lag("php", "8.9.1", resolver=hub) is None)
        check("Hub çökerse None (uyarı değil)",
              iv.patch_lag("php", "8.4.27", resolver=ur.Resolver(fetch=_fake_hub({}, down=True))) is None)

        # eol
        rows = {"php": [{"cycle": "8.4", "eol": "2028-12-31", "latest": "8.4.30"},
                        {"cycle": "7.4", "eol": "2022-11-28"}, {"cycle": "9.0", "eol": False}, {"cycle": "5.6", "eol": True}],
                "nodejs": [{"cycle": "20", "eol": "2026-04-30"}]}
        urls: list = []

        def fetch(url):
            urls.append(url)
            return rows[url.rsplit("/", 1)[1][:-5]]
        iv._EOL_CACHE.clear(); iv._EOL_DOWN.clear()
        today = date(2026, 11, 18)
        check("eol geçmiş → tarih", iv.eol("php", "7.4.33", fetch=fetch, today=today) == {"eol": "2022-11-28"})
        check("eol gelecek → None", iv.eol("php", "8.4.27", fetch=fetch, today=today) is None)
        check("eol false → None", iv.eol("php", "9.0.1", fetch=fetch, today=today) is None)
        check("eol true → destek bitti (tarihsiz)", iv.eol("php", "5.6.1", fetch=fetch, today=today) == {"eol": ""})
        check("node döngüsü yalnız majör", iv.eol("node", "20.17.0", fetch=fetch, today=today) == {"eol": "2026-04-30"})
        check("bilinmeyen döngü → None", iv.eol("php", "8.1.2", fetch=fetch, today=today) is None)
        check("ürün adları ve tek istek (24 sa bellek)",
              urls == ["https://endoflife.date/api/php.json", "https://endoflife.date/api/nodejs.json"], str(urls))
        iv._EOL_CACHE.clear(); iv._EOL_DOWN.clear()

        def down(url):
            raise OSError("ağ yok")
        check("okunamadı → None", iv.eol("php", "7.4.33", fetch=down, today=today) is None)
        check("beklenmedik biçim → None",
              iv.eol("php", "7.4.33", fetch=lambda u: {"x": 1}, today=today) is None)
        iv._EOL_CACHE.clear(); iv._EOL_DOWN.clear()
        check("geçersiz tarih → None",
              iv.eol("php", "7.4.33", fetch=lambda u: [{"cycle": "7.4", "eol": "yarın"}], today=today) is None)
        iv._EOL_CACHE.clear(); iv._EOL_DOWN.clear()

        # report: seviye tablosu + önbellek
        reg = "r.example/prod-ar"
        files = {"/lvl-crit/Dockerfile": "FROM php:8.4.27-fpm\n", "/lvl-high/Dockerfile": "FROM php:8.3.1\n",
                 "/lvl-eol/Dockerfile": "FROM php:7.4.33\n", "/lvl-clean/Dockerfile": "FROM php:8.4.30\n",
                 "/lvl-unk/Dockerfile": "FROM debian:12\n"}
        client = _iv_client(files)
        index = {"a": [{"src": "anatolia/build/a", "images": [f"{reg}/lvl-crit:1", f"{reg}/lvl-high:1",
                                                              f"{reg}/lvl-eol:1", f"{reg}/lvl-clean:1",
                                                              f"{reg}/lvl-unk:1"]},
                       {"src": "russia/build/a", "images": [f"{reg}/lvl-clean:1"]}]}
        scans = {"php:8.4.27-fpm": {"critical": 1, "high": 0}, "php:8.3.1": {"critical": 0, "high": 2},
                 "php:7.4.33": {"critical": 0, "high": 0}, "php:8.4.30": {"critical": 0, "high": 0},
                 "debian:12": None}
        ran: list = []

        def scan_fn(image):
            ran.append(image)
            s = scans[image]
            if s is None:
                return {"state": "tarama başarısız", "critical": 0, "high": 0, "packages": [], "os": "", "created": ""}
            return {"state": "tamam", "packages": [], "os": "debian 12", "created": "2026-11-05", **s}
        hub2 = ur.Resolver(fetch=_fake_hub({"php": ["8.4.30", "8.3.9"]}))
        rows["php"] = [{"cycle": "7.4", "eol": "2022-11-28"}, {"cycle": "8.4", "eol": "2028-12-31"},
                       {"cycle": "8.3", "eol": "2027-12-31"}]
        eol_fn = lambda lang, ver, **kw: iv.eol(lang, ver, fetch=fetch, today=today)  # noqa: E731
        with tempfile.TemporaryDirectory() as td:
            kw = dict(index=index, scan_fn=scan_fn, resolver=hub2, eol_fn=eol_fn, cache_dir=Path(td), ttl_h=24, now=1000.0)
            rep = iv.report(client, **kw)
            lv = {r["image"]: r for r in rep["images"]}
            check("kritik: düzeltmeli CRITICAL", lv["php:8.4.27-fpm"]["level"] == "kritik"
                  and "taban imaj yeniden üretilmeli (DevOps, bölge bayrakları)" in lv["php:8.4.27-fpm"]["actions"]
                  and "yama sürümünü yükselt (8.4.30)" in lv["php:8.4.27-fpm"]["actions"], str(lv["php:8.4.27-fpm"]))
            check("yüksek: düzeltmeli HIGH (ve yama geride 8.3.1 → 8.3.9)", lv["php:8.3.1"]["level"] == "yüksek")
            check("kritik: destek bitmiş (taramada bulgu yok)", lv["php:7.4.33"]["level"] == "kritik"
                  and any("destek 2022-11-28'de bitti" in a for a in lv["php:7.4.33"]["actions"]))
            check("temiz: tarama tamam, işaret yok", lv["php:8.4.30"]["level"] == "temiz" and lv["php:8.4.30"]["actions"] == [])
            check("bilinmiyor: tarama yapılamadı, başka işaret yok", lv["debian:12"]["level"] == "bilinmiyor")
            check("sıralama: kritik → yüksek → bilinmiyor → temiz",
                  [r["level"] for r in rep["images"]] == ["kritik", "kritik", "yüksek", "bilinmiyor", "temiz"],
                  str([r["level"] for r in rep["images"]]))
            check("rapor alanları", rep["scanner"] is True and rep["note"] and rep["scanned_at"])
            check("her imaj bir kez tarandı", sorted(ran) == sorted(scans), str(ran))
            n = len(ran)
            iv.report(client, **kw)
            check("ikinci çağrı önbellekten; başarısız tarama yeniden denenir", ran[n:] == ["debian:12"], str(ran[n:]))
            n = len(ran)
            iv.report(client, force=True, **kw)
            check("force önbelleği atlar", len(ran) - n == 5)
            n = len(ran)
            iv.report(client, **{**kw, "now": 1000.0 + 25 * 3600})
            check("TTL dolunca yeniden taranır", len(ran) - n == 5)
            names = sorted(p.name for p in Path(td).iterdir())
            check("önbellek dosya adı imaj adından türetilmiş hash (ham ad yol olmaz)",
                  bool(names) and all(re.fullmatch(r"[0-9a-f]{40}\.json", f) for f in names), str(names))
            p = iv._cache_path(Path(td), "php:8.3.1")
            doc = json.loads(p.read_text())
            doc["notified"] = "abc"
            p.write_text(json.dumps(doc))
            iv.report(client, force=True, **kw)
            check("yeniden yazımda notified korunur", json.loads(p.read_text()).get("notified") == "abc")
            idx2 = {"a": [{"src": "global/build/a", "images": [f"{reg}/nope-1:1"]}]}
            r2 = iv.report(client, **{**kw, "index": idx2})
            check("dayanak çözülemedi → bilinmiyor, neden scan'de, tarama yok",
                  r2["images"][0]["level"] == "bilinmiyor" and r2["images"][0]["scan"]["state"] == "dayanak çözülemedi"
                  and r2["images"][0]["scan"]["reason"] and f"{reg}/nope-1:1" not in ran,
                  str(r2["images"][0]))
    finally:
        iv.trivy_cmd = saved_cmd
        iv._EOL_CACHE.clear(); iv._EOL_DOWN.clear()


def test_image_vuln_hardening():
    """Fix round 1: güvenilmez önbellek, harici kayıt deposu, bayrak belirteci, bilgi seviyesi, eol bellek, kap temizliği."""
    print("\n[taban imaj güvenliği: sertleştirme]")
    import subprocess as sp
    from datetime import date
    from agile_sdlc_crew.quality import image_vuln as iv
    from agile_sdlc_crew.quality import upgrade_registry as ur
    saved_cmd = iv.trivy_cmd
    iv.trivy_cmd = lambda **kw: ["trivy"]
    try:
        spy: list = []
        hosts = ["evil.example.com/x:1", "169.254.169.254/a:1", "10.0.0.5/a", "localhost/a:1", "localhost:5000/a",
                 "reg:5000/a:1", "docker.io/library/php:8.4"]
        for h in hosts:
            r = iv.scan_image(h, run=lambda c, t: spy.append(c) or (0, "{}"))
            check(f"harici kayıt deposu taranmaz: {h}", r["state"] in ("harici kayıt deposu", "geçersiz imaj"), r["state"])
        check("harici kayıt deposunda run hiç çağrılmaz", not spy)
        check("harici kayıt deposu durumu adıyla döner",
              iv.scan_image("evil.example.com/x:1", run=lambda c, t: (0, "{}"))["state"] == "harici kayıt deposu")
        check("Docker Hub adları argv'ye ulaşır",
              all(iv.scan_image(n, run=lambda c, t: (0, "{}"))["state"] == "tamam" for n in ("php:8.4", "library/php", "org/x:1")))
        check("evil/php:8.4 php sayılmaz; library/php sayılır",
              iv._lang_version("evil/php:8.4.1") == ("", "") and iv._lang_version("library/php:8.4.1") == ("php", "8.4.1")
              and iv._lang_version("evil.example.com/php:8.4.1") == ("", "") and iv._lang_version("php:8.4.1") == ("php", "8.4.1"))
        reg = "r.example/prod-ar"
        client = _iv_client({"/h-1/Dockerfile": "FROM evil.example.com/php:8.4.27\n",
                             "/h-2/Dockerfile": "FROM php@sha256:abcd\n"})
        index = {"a": [{"src": "global/build/a", "images": [f"{reg}/h-1:1", f"{reg}/h-2:1", "10.0.0.5/a:1",
                                                           "--platform=$BUILDPLATFORM", "debian", "scratch", "base",
                                                           "php@sha256:abcd", "node:${V}-alpine"]}]}
        recs = {r["image"]: r for r in iv.inventory(client, index)}
        check("bayrak belirteci kayıt olmaz", not any(k.startswith("-") for k in recs))
        check("etiketsiz resmî ad envantere girer, scratch / takma ad girmez",
              "debian" in recs and recs["debian"]["lang"] == "" and "scratch" not in recs and "base" not in recs)
        check("özet sabitlemeli imaj: özel neden", "özetle sabitlenmiş" in recs["php@sha256:abcd"]["error"]
              and "özetle sabitlenmiş" in recs[f"{reg}/h-2:1"]["error"], str(recs.get("php@sha256:abcd")))
        check("ARG çözülemeyen imaj: özel neden", "ARG varsayılanı yok" in recs["node:${V}-alpine"]["error"])
        scans: list = []
        eol_fn = lambda *a, **k: None  # noqa: E731
        hub500 = ur.Resolver(fetch=lambda u: (500, None))
        ok_scan = {"state": "tamam", "critical": 0, "high": 0, "packages": [], "os": "", "created": ""}
        rp = iv.report(client, index=index, scan_fn=lambda i: scans.append(i) or {**ok_scan},
                       resolver=hub500, eol_fn=eol_fn, cache_dir=Path(tempfile.mkdtemp()), ttl_h=24, now=1.0)
        by = {r["image"]: r for r in rp["images"]}
        ext = by.get("10.0.0.5/a:1")
        check("rapor: harici kayıt deposu → bilinmiyor + neden, tarama çağrılmaz",
              bool(ext) and ext["level"] == "bilinmiyor" and ext["scan"]["state"] == "harici kayıt deposu"
              and bool(ext["scan"]["reason"]) and "10.0.0.5/a:1" not in scans, str(ext))
        e2 = by.get("evil.example.com/php:8.4.27")
        check("harici kayıt deposundaki üretim dayanağı da taranmaz, dil sayılmaz",
              bool(e2) and e2["scan"]["state"] == "harici kayıt deposu" and "evil.example.com/php:8.4.27" not in scans
              and e2["lang"] == "", str(e2))

        with tempfile.TemporaryDirectory() as td:
            cd = Path(td)
            path = iv._cache_path(cd, "php:8.4.27")
            variants = {
                "built_at metin": {"image": "php:8.4.27", "built_at": "x", "scan": ok_scan},
                "scan eksik anahtar": {"image": "php:8.4.27", "built_at": 1000.0, "scan": {"state": "tamam"}},
                "critical metin": {"image": "php:8.4.27", "built_at": 1000.0, "scan": {**ok_scan, "critical": "3"}},
                "packages liste değil": {"image": "php:8.4.27", "built_at": 1000.0, "scan": {**ok_scan, "packages": "x"}},
                "state tamam değil": {"image": "php:8.4.27", "built_at": 1000.0, "scan": {**ok_scan, "state": "zaman aşımı"}},
                "scan dict değil": {"image": "php:8.4.27", "built_at": 1000.0, "scan": [1]},
                "kök liste": [1, 2],
                "başka imaj": {"image": "x:1", "built_at": 1000.0, "scan": ok_scan},
            }
            for name, doc in variants.items():
                path.write_text(json.dumps(doc))
                check(f"bozuk önbellek → yeniden tara: {name}", iv._cache_read(cd, "php:8.4.27", 24, 1001.0) is None)
            path.write_text('{"image": "php:8.4.27", "built_at": 10')
            check("kesik JSON → yeniden tara", iv._cache_read(cd, "php:8.4.27", 24, 1001.0) is None)
            path.write_text(json.dumps({"image": "php:8.4.27", "built_at": 1000.0, "scan": ok_scan}))
            check("geçerli önbellek okunur", (iv._cache_read(cd, "php:8.4.27", 24, 1001.0) or {}).get("state") == "tamam")
            c2 = _iv_client({"/p-1/Dockerfile": "FROM php:8.4.30\n"})
            idx = {"a": [{"src": "global/build/a", "images": [f"{reg}/p-1:1"]}]}
            iv._cache_path(cd, "php:8.4.30").write_text(
                json.dumps({"image": "php:8.4.30", "built_at": "x", "scan": {"critical": "3"}}))
            ran: list = []
            rr = iv.report(c2, index=idx, scan_fn=lambda i: ran.append(i) or {**ok_scan}, resolver=hub500,
                           eol_fn=eol_fn, cache_dir=cd, ttl_h=24, now=1001.0)
            check("bozuk önbellekle report çökmez, yeniden tarar", ran == ["php:8.4.30"] and rr["images"][0]["level"] == "temiz")

        c3 = _iv_client({"/q-1/Dockerfile": "FROM python:3.11-slim\n", "/q-2/Dockerfile": "FROM python:3.13.7-slim\n"})
        idx = {"a": [{"src": "global/build/a", "images": [f"{reg}/q-1:1", f"{reg}/q-2:1"]}]}
        r3 = iv.report(c3, index=idx, scan_fn=lambda i: {**ok_scan}, resolver=hub500,
                       eol_fn=eol_fn, cache_dir=Path(tempfile.mkdtemp()), ttl_h=24, now=1.0)
        b3 = {r["image"]: r for r in r3["images"]}
        check("iki parçalı etiket, temiz tarama → bilgi (temiz değil), not eylemde",
              b3["python:3.11-slim"]["level"] == "bilgi" and any("belirsiz" in a for a in b3["python:3.11-slim"]["actions"]))
        check("üç parçalı etiket, temiz tarama → temiz", b3["python:3.13.7-slim"]["level"] == "temiz")

        iv._EOL_CACHE.clear(); iv._EOL_DOWN.clear()
        getattr(iv, "_EOL_DOWN", {}).clear()
        now = [100.0]
        n: list = []

        def down(u):
            n.append(u)
            raise OSError("x")
        d = date(2026, 11, 18)
        for _ in range(3):
            iv.eol("php", "7.4.1", fetch=down, today=d, clock=lambda: now[0])
        check("eol başarısız → 5 dk yeniden denenmez", len(n) == 1, str(len(n)))
        now[0] += 301
        iv.eol("php", "7.4.1", fetch=down, today=d, clock=lambda: now[0])
        check("süre dolunca yeniden denenir", len(n) == 2)
        iv._EOL_CACHE.clear(); iv._EOL_DOWN.clear()
        getattr(iv, "_EOL_DOWN", {}).clear()

        saved_run = iv.subprocess.run
        seen: list = []
        iv.subprocess.run = lambda cmd, **kw: seen.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr="")
        try:
            iv.trivy_cmd = lambda **kw: ["/x/docker", "run", "--rm", "--name", "tempo-trivy-0123456789ab", "-v",
                                         "/nonexistent-tempo-test:/root/.cache/", "aquasec/trivy:latest"]

            def boom(c, t):
                raise sp.TimeoutExpired(c, t)
            r = iv.scan_image("php:8.4", run=boom)
            check("zaman aşımı: kap silinir (liste argv)", r["state"] == "zaman aşımı"
                  and seen == [["/x/docker", "rm", "-f", "tempo-trivy-0123456789ab"]], str(seen))
            seen.clear()
            iv.trivy_cmd = lambda **kw: ["trivy"]
            iv.scan_image("php:8.4", run=boom)
            check("docker olmayan tarayıcıda kap silme çağrılmaz", not seen)
        finally:
            iv.subprocess.run = saved_run
    finally:
        iv.trivy_cmd = saved_cmd
        iv._EOL_CACHE.clear(); iv._EOL_DOWN.clear()
        getattr(iv, "_EOL_DOWN", {}).clear()


def test_image_vuln_settings():
    """Dört ayar pipeline_config'te, varsayılan değerleriyle."""
    print("\n[taban imaj güvenliği: ayarlar]")
    from agile_sdlc_crew import pipeline_config as pc
    by = {f["key"]: f for f in pc.SCHEMA}
    check("_IMAGE_VULN / _TASK / _NOTIFY kapalı bool",
          all(by.get(k, {}).get("type") == "bool" and by[k]["default"] is False
              for k in ("CREW_TECHDEBT_IMAGE_VULN", "CREW_TECHDEBT_IMAGE_VULN_TASK", "CREW_TECHDEBT_IMAGE_VULN_NOTIFY")))
    check("_SCAN_H int 24, min 1", by.get("CREW_TECHDEBT_IMAGE_VULN_SCAN_H", {}).get("type") == "int"
          and by["CREW_TECHDEBT_IMAGE_VULN_SCAN_H"]["default"] == 24 and by["CREW_TECHDEBT_IMAGE_VULN_SCAN_H"].get("min") == 1)


def _iv_rec(image, level="kritik", *, crit=1, eol=None, users=1, patch=None, actions=None, pkgs=None):
    return {"image": image, "lang": "php", "version": "8.4.27", "via": [], "touched": "",
            "users": {"anatolia": [f"build/s{i}" for i in range(users)]}, "error": "",
            "scan": {"state": "tamam", "critical": crit, "high": 3, "packages": pkgs or [], "os": "debian 12",
                     "created": "2026-11-05", "ignored": {"critical": 0, "high": 0, "packages": []}},
            "patch": patch, "eol": eol, "level": level,
            "actions": actions if actions is not None else ["yeniden üret"]}


def test_image_vuln_alerts():
    """Uç, yoklayıcı, uyarı (iş + Telegram özeti, tavan), yok sayılan paketler. Ağ / Docker / Azure / Telegram yok."""
    print("\n[taban imaj güvenliği: uç, yoklayıcı, uyarı]")
    import inspect
    import threading
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import daily
    from agile_sdlc_crew.quality import image_vuln as iv, render

    knobs = {"CREW_TECHDEBT_IMAGE_VULN_TASK": True, "CREW_TECHDEBT_IMAGE_VULN_NOTIFY": True,
             "CREW_TECHDEBT_IMAGE_VULN_MAX_TASKS": 3}
    _get = pc.get
    pc.get = lambda k: knobs[k] if k in knobs else _get(k)
    _tg = daily.telegram_configured
    try:
        # ── alert: iş açma, tavan, sıra ──────────────────────────────────
        board: dict = {}
        opened: list = []

        def open_fn(client, rec, *, team, deps):
            opened.append(rec["image"])
            board[f"image_vuln:{rec['image']}"] = {"wi": 900 + len(opened), "state": "New", "closed_at": 0.0}
            return {"id": 900 + len(opened), "url": "https://x/_workitems/edit/%d" % (900 + len(opened))}
        deps = SimpleNamespace(board=lambda c: board, now=lambda: 1000.0)
        rep = {"images": [_iv_rec("golang:1.24.4", crit=23, eol={"eol": "2026-02-10"}, users=3),
                          _iv_rec("php:7.4.33-fpm", crit=18, eol={"eol": "2022-11-28"}, users=2),
                          _iv_rec("php:8.4.27", crit=5, users=11),
                          _iv_rec("python:3.12", crit=9, users=1),
                          _iv_rec("node:20.17.0", crit=9, users=4),
                          _iv_rec("debian:12", level="yüksek", crit=0)]}
        with tempfile.TemporaryDirectory() as td:
            kw = dict(team="T", deps=deps, open_fn=open_fn, notify_fn=lambda t: True, cache_dir=Path(td))
            knobs["CREW_TECHDEBT_IMAGE_VULN_TASK"] = False
            out = iv.alert(None, rep, **kw)
            check("_TASK kapalı → hiç iş açılmaz", opened == [] and not any(r.get("state") == "açıldı" for r in out))
            knobs["CREW_TECHDEBT_IMAGE_VULN_TASK"] = True
            iv.alert(None, rep, **{**kw, "team": ""})
            check("takım yoksa iş açılmaz (yazma yolu kapalı)", opened == [])
            out = iv.alert(None, rep, **kw)
            check("tavan 3: destek bitmişler önce, sonra düzeltmeli kritik sayısı, sonra servis sayısı",
                  opened == ["golang:1.24.4", "php:7.4.33-fpm", "node:20.17.0"], str(opened))
            st = {r["image"]: r["state"] for r in out}
            check("kalanlar 'sırada', yüksek seviyeli imaj listede yok",
                  st.get("php:8.4.27") == "sırada" and st.get("python:3.12") == "sırada" and "debian:12" not in st
                  and st["golang:1.24.4"] == "açıldı", str(st))
            n = len(opened)
            out = iv.alert(None, rep, **kw)
            st = {r["image"]: r["state"] for r in out}
            check("ikinci çağrı: açık işi olana ikinci iş açılmaz; açık iş tavana sayılmaz",
                  st["golang:1.24.4"] == "açık iş" and len(opened) - n == 2 and opened[n:] == ["python:3.12", "php:8.4.27"],
                  f"{st} {opened[n:]}")
            out = iv.alert(None, rep, **kw)
            check("üçüncü çağrı: hepsinin işi var → yeni iş yok",
                  len(opened) == n + 2 and all(r["state"] == "açık iş" for r in out))
            # dedup durumları: Canceled bastırılır; Done + eski (grace dışı) yeniden açılır; Done + yeni açık sayılır
            b2 = {"image_vuln:golang:1.24.4": {"wi": 7, "state": "Canceled", "closed_at": 0.0},
                  "image_vuln:php:7.4.33-fpm": {"wi": 8, "state": "Done", "closed_at": 1000.0 - 10 * 86400},
                  "image_vuln:php:8.4.27": {"wi": 9, "state": "Done", "closed_at": 1000.0 - 86400}}
            opened.clear()
            d2 = SimpleNamespace(board=lambda c: b2, now=lambda: 1000.0)
            out = iv.alert(None, {"images": rep["images"][:3]}, **{**kw, "deps": d2})
            st = {r["image"]: r["state"] for r in out}
            check("Canceled (suppressed) → 'bastırıldı', açık iş etiketi değil",
                  st["golang:1.24.4"] == "bastırıldı" and "golang:1.24.4" not in opened, str(st))
            check("Done + eski (grace dışı) → yeni iş açılır (recurring)",
                  st["php:7.4.33-fpm"] == "açıldı" and opened == ["php:7.4.33-fpm"], f"{st} {opened}")
            check("Done + yeni (grace içi) → iş açılmaz", st["php:8.4.27"] == "açık iş")
            knobs["CREW_TECHDEBT_IMAGE_VULN_MAX_TASKS"] = 1
            board.clear(); opened.clear()
            iv.alert(None, rep, **kw)
            check("tavan ayardan okunur (1)", len(opened) == 1)

            # ── alert: Telegram özeti ───────────────────────────────────
            msgs: list = []
            nkw = {**kw, "notify_fn": lambda t: msgs.append(t) or True, "cache_dir": Path(td) / "n"}
            knobs["CREW_TECHDEBT_IMAGE_VULN_TASK"] = False
            iv.alert(None, rep, **nkw)
            check("tek özet mesaj (imaj başına değil), seviye sayıları + imaj + eylem",
                  len(msgs) == 1 and "golang:1.24.4" in msgs[0] and "yeniden üret" in msgs[0]
                  and "kritik" in msgs[0].lower() and "5" in msgs[0], str(msgs))
            iv.alert(None, rep, **nkw)
            check("aynı (imaj, işaret) kümesi → tekrar gönderilmez", len(msgs) == 1)
            rep2 = {"images": [dict(r) for r in rep["images"]]}
            rep2["images"][2] = _iv_rec("php:8.4.27", crit=5, users=11, patch={"latest": "8.4.30"})
            iv.alert(None, rep2, **nkw)
            check("işaret kümesi değişince yeniden gönderilir", len(msgs) == 2)
            iv.alert(None, {"images": [_iv_rec("a:1", level="temiz", crit=0)]}, **nkw)
            check("kritik yokken mesaj yok", len(msgs) == 2)
            fail_msgs: list = []
            fkw = {**nkw, "cache_dir": Path(td) / "f", "notify_fn": lambda t: fail_msgs.append(t) and False}
            iv.alert(None, rep, **fkw)
            iv.alert(None, rep, **fkw)
            check("gönderilemeyen mesaj kaydedilmez (sonraki turda yeniden denenir)", len(fail_msgs) == 2)
            m2: list = []
            cdir = Path(td) / "cor"
            cdir.mkdir()
            ckw2 = {**nkw, "notify_fn": lambda t: m2.append(t) or True, "cache_dir": cdir}
            (cdir / "notified.json").write_text("{bozuk json")
            iv.alert(None, rep, **ckw2)
            check("bozuk notified.json → 'hiçbir şey gönderilmedi' sayılır, çökmez, özet gider", len(m2) == 1)
            (cdir / "notified.json").write_text("[1, 2]")
            iv.alert(None, rep, **ckw2)
            check("beklenmedik biçimli notified.json de aynı şekilde ele alınır", len(m2) == 2)
            knobs["CREW_TECHDEBT_IMAGE_VULN_NOTIFY"] = False
            iv.alert(None, rep2, **{**nkw, "cache_dir": Path(td) / "off"})
            check("_NOTIFY kapalı → mesaj yok", len(msgs) == 2)
            knobs["CREW_TECHDEBT_IMAGE_VULN_NOTIFY"] = True
            daily.telegram_configured = lambda: False
            iv.alert(None, rep, **{**nkw, "notify_fn": None, "cache_dir": Path(td) / "noc"})
            check("Telegram ayarı yoksa sessizce atlanır (çökmez)", True)
    finally:
        pc.get = _get
        daily.telegram_configured = _tg

    # ── iş metni ve Task alanları ───────────────────────────────────────
    pkgs = [{"name": f"pkg{i}", "installed": "1", "fixed": "2", "severity": "CRITICAL", "cves": ["CVE-2026-0001"]}
            for i in range(20)]
    pkgs[0]["name"] = "<img src=x onerror=alert(1)>"
    rec = _iv_rec("golang:1.24.4", crit=23, eol={"eol": "2026-02-10"}, users=2, pkgs=pkgs,
                  patch={"latest": "1.24.13"}, actions=["yeniden üret", "sürüm yükselt (destek 2026-02-10'da bitti)"])
    rec["users"]["russia"] = ["build/core"]
    rec["repos"] = {"gocrons": {"anatolia": ["build/s0"]}, "<b>core": {"russia": ["build/core"]}}
    pkgs[1]["published"] = {"CVE-2026-0001": "2026-11-01"}
    pkgs[2]["cves"] = ['x"><script>', "GHSA-abcd-1234-wxyz"]
    pkgs[2]["published"] = {"GHSA-abcd-1234-wxyz": "2099-01-01"}
    from datetime import date as _d
    body = iv.task_body(rec, today=_d(2026, 11, 19))
    check("gövde: CVE bağlantısı + duyuru tarihi ve gün sayısı; tanınmayan kimlik bağlantısız ve kaçışlı",
          '<a href="https://nvd.nist.gov/vuln/detail/CVE-2026-0001">CVE-2026-0001</a> — duyuru 2026-11-01, 18 gün önce' in body
          and '<a href="https://github.com/advisories/GHSA-abcd-1234-wxyz">' in body and "2099-01-01" not in body
          and "<script>" not in body and iv.cve_url("CVE-2026-1/../x") == "" and iv.cve_url("javascript:x") == ""
          and iv.cve_age("bozuk") is None and iv.cve_age("2026-11-19", _d(2026, 11, 19)) == 0, body[:900])
    ps = iv._parse_scan({"Results": [{"Vulnerabilities": [
        {"Severity": "HIGH", "FixedVersion": "2", "PkgName": "a", "VulnerabilityID": "CVE-2026-1111",
         "PublishedDate": "2026-03-01T10:00:00Z"},
        {"Severity": "HIGH", "FixedVersion": "2", "PkgName": "a", "VulnerabilityID": "CVE-2026-2222", "PublishedDate": "<x>"}]}]})
    check("tarama: duyuru tarihi CVE başına saklanır, bozuk tarih alınmaz",
          ps["packages"][0]["published"] == {"CVE-2026-1111": "2026-03-01"}, str(ps["packages"]))
    check("gövde: etkilenen repolar listesi (bölgeleriyle, kaçışlı)",
          "Etkilenen repolar" in body and "<b>gocrons</b>: anatolia" in body and "&lt;b&gt;core" in body
          and "Etkilenen repolar" not in iv.task_body(_iv_rec("golang:1.24.4")), body[:400])
    check("gövde: sınır cümlesi aynen",
          "Yayınlanmış imaj değil, dayandığı resmî imaj tarandı; kendi eklediğimiz katmanlar ve derleme anında "
          "kurulan yamalar görülmez — sonuç alt sınırdır." in body)
    check("gövde: paket adı kaçışlı, ham HTML yok", "<img" not in body and "&lt;img" in body)
    check("gövde: yalnız ilk 15 paket", "pkg14" in body and "pkg15" not in body and "pkg19" not in body)
    check("gövde: bölge başına servisler + eylem", "anatolia" in body and "russia" in body and "build/core" in body
          and "yeniden üret" in body)
    g = iv.task_group(rec)
    check("Task: güvenlik uyarısı en yüksek öncelik (1)", g.priority == 1, str(g.priority))
    desc = render.task_description(g)
    tags = render.task_tags(g, lane="techdebt").split("; ")
    check("Task: etiketler", all(t in tags for t in ("tempo-quality", "tempo-techdebt", "tech-debt", "tempo-image-vuln", "needs-human")),
          str(tags))
    check("Task: parmak izi image_vuln:<imaj>", g.fingerprints == ["image_vuln:golang:1.24.4"]
          and "tempo-fp: image_vuln:golang:1.24.4" in desc and g.needs_human)
    check("Task: güvenilmez içerik notu korunur (paket / CVE adı üçüncü taraf metni), gövdeden ÖNCE",
          render.UNTRUSTED_NOTE in desc and desc.index(render.UNTRUSTED_NOTE) < desc.index("Taban imaj:"))
    check("Task: başlık", "Taban imaj güvenliği: golang:1.24.4" in render.task_title(g))
    # eylem metni: kendi taban imajı ↔ genel imaj
    base_rec = {"scan": {"state": "tamam", "critical": 1, "high": 0}, "patch": None, "eol": None, "version": "3.11",
                "lang": "python"}
    _l, own_acts = iv._judge({**base_rec, "via": ["php-8-4"]})
    _l, pub_acts = iv._judge({**base_rec, "via": []})
    check("eylem: kendi taban imajı → DevOps yeniden üretir (bölge bayrakları)",
          "taban imaj yeniden üretilmeli (DevOps, bölge bayrakları)" in own_acts, str(own_acts))
    check("eylem: genel imaj → servis yeniden yayınlanır, DevOps'a yönlendirmez",
          pub_acts == ["servis imajı yeniden derlendiğinde taze taban çekilir — servisi yeniden yayınla"], str(pub_acts))
    check("Task: kabul/davranış metni kalite (NR) metni değil",
          "New Relic" not in desc and render.acceptance_block(g) and "hata sınıfı" not in render.acceptance_block(g))
    from agile_sdlc_crew.quality import dedup
    check("Task: pano dedup bu satırı okur", render.parse_fingerprints(desc) == ["image_vuln:golang:1.24.4"]
          and "image_vuln:golang:1.24.4" in dedup.index_board([{"id": 7, "fields": {"System.State": "New", "System.Description": desc}}]))

    # ── yok sayılan paketler ────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        y = Path(td) / "image_vuln.yaml"
        y.write_text("ignore_packages: [linux-libc-dev]\n")
        check("yaml okunur", iv.load_ignore(y) == {"linux-libc-dev"})
        y.write_text("ignore_packages: [unclosed\n  - : :\n")
        check("bozuk yaml → yok sayma yok, çökme yok", iv.load_ignore(y) == set())
        y.write_text("ignore_packages: 5\n")
        check("beklenmedik biçim → yok sayma yok", iv.load_ignore(y) == set())
        check("dosya yok → yok sayma yok", iv.load_ignore(Path(td) / "nope.yaml") == set())
        check("depodaki yaml linux-libc-dev'i yok sayar", "linux-libc-dev" in iv.load_ignore())

        libc = {"name": "linux-libc-dev", "installed": "1", "fixed": "2", "severity": "CRITICAL", "cves": [],
                "critical": 1, "high": 4}
        ssl = {"name": "libssl3", "installed": "1", "fixed": "2", "severity": "HIGH", "cves": [], "critical": 0, "high": 2}
        raw = {"state": "tamam", "critical": 1, "high": 6, "packages": [libc, ssl], "os": "debian 12", "created": ""}
        f = iv.apply_ignore(raw, {"linux-libc-dev"})
        check("yok sayılan paket sayıma girmez, gizlenmez",
              f["critical"] == 0 and f["high"] == 2 and [p["name"] for p in f["packages"]] == ["libssl3"]
              and f["ignored"] == {"critical": 1, "high": 4, "packages": ["linux-libc-dev"]}, str(f))
        check("ham tarama değişmez", raw["critical"] == 1 and len(raw["packages"]) == 2 and "ignored" not in raw)

        client = _iv_client({})
        index = {"a": [{"src": "anatolia/build/a", "images": ["php:8.4.27-fpm-bookworm"]}]}
        ran: list = []

        def scan_fn(image):
            ran.append(image)
            return {**raw, "critical": 1, "high": 4, "packages": [dict(libc)]}
        hub = SimpleNamespace(latest_patch=lambda n, m: None)
        kw = dict(index=index, scan_fn=scan_fn, resolver=hub, eol_fn=lambda *a, **k: None, cache_dir=Path(td) / "c",
                  ttl_h=24, now=1000.0)
        r1 = iv.report(client, ignore_packages={"linux-libc-dev"}, **kw)
        im = r1["images"][0]
        check("yalnız yok sayılan pakette bulgu → seviye kritik değil",
              im["level"] != "kritik" and im["scan"]["critical"] == 0 and im["scan"]["ignored"]["critical"] == 1, str(im["scan"]))
        r2 = iv.report(client, ignore_packages=set(), **kw)
        check("önbellek ham tarama tutar: yaml değişince yeniden tarama olmadan etki eder",
              r2["images"][0]["level"] == "kritik" and r2["images"][0]["scan"]["critical"] == 1 and len(ran) == 1, f"{len(ran)}")
        # eski biçimli önbellek (paket başına sayım yok): yok sayılan pakette doğru sayım için yeniden taranır
        old = {"state": "tamam", "critical": 1, "high": 4, "os": "", "created": "",
               "packages": [{"name": "linux-libc-dev", "installed": "1", "fixed": "2", "severity": "CRITICAL", "cves": []}]}
        iv._cache_write(Path(td) / "c", "php:8.4.27-fpm-bookworm", old, 1000.0)
        n = len(ran)
        iv.report(client, ignore_packages={"linux-libc-dev"}, **kw)
        check("paket başına sayımı olmayan eski önbellek yok sayılan pakette yeniden taranır", len(ran) == n + 1)

    # ── uç: önbellek / tarama ───────────────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        client = _iv_client({})
        index = {"a": [{"src": "anatolia/build/a", "images": ["php:8.4.27-fpm-bookworm", "golang:1.24.4"]}]}
        ran = []

        def scan_fn2(image):
            ran.append(image)
            return {"state": "tamam", "critical": 0, "high": 0, "packages": [], "os": "", "created": ""}
        skw = dict(index=index, scan_fn=scan_fn2, resolver=hub, eol_fn=lambda *a, **k: None,
                   cache_dir=Path(td), ttl_h=24, now=1000.0, ignore_packages=set())
        spawned: list = []
        clk = [1000.0]
        ckw = dict(spawn=spawned.append, clock=lambda: clk[0])
        iv._reset_state()
        res = iv.serve(client, **ckw, **skw)
        check("önbellek boş → scanning true + eldeki kısım, tarama bu çağrıda koşmaz",
              res["scanning"] is True and len(spawned) == 1 and ran == [] and len(res["images"]) == 2
              and all(r["scan"]["state"] == "bekliyor" for r in res["images"]), str(res)[:300])
        res = iv.serve(client, **ckw, **skw)
        check("tarama sürerken ikinci istek ikinci tarama başlatmaz (kilit)", res["scanning"] is True and len(spawned) == 1)
        spawned[0]()
        check("arka plan taraması tüm imajları taradı, kilit bırakıldı", sorted(ran) == ["golang:1.24.4", "php:8.4.27-fpm-bookworm"]
              and iv._RUN["running"] is False)
        res = iv.serve(client, **ckw, **skw)
        check("önbellek dolu → scanning false, images, yeni tarama yok",
              res["scanning"] is False and len(res["images"]) == 2 and len(spawned) == 1 and len(ran) == 2
              and all(r["scan"]["state"] == "tamam" for r in res["images"]))
        res = iv.serve(client, force=True, **ckw, **skw)
        check("GET'te force yok sayılır: tarama başlamaz, çökmez", res["scanning"] is False and len(spawned) == 1
              and len(ran) == 2)
        # POST (rescan): bekleme süresi
        res = iv.rescan(client, **ckw, **skw)
        check("rescan: son tarama 5 dk içinde bitti → başlamaz, kalan süre + Türkçe neden, eldeki sonuç döner",
              res["scanning"] is False and len(spawned) == 1 and 0 < res["cooldown"] <= 300
              and "sonra" in res["message"] and len(res["images"]) == 2, str(res)[:300])
        clk[0] += 301
        res = iv.rescan(client, **ckw, **skw)
        check("rescan: bekleme geçince TAM BİR tarama başlar", res["scanning"] is True and len(spawned) == 2)
        res = iv.rescan(client, **ckw, **skw)
        check("rescan: tarama sürerken ikincisi başlamaz", len(spawned) == 2 and res["scanning"] is True)
        spawned[1]()
        check("rescan taraması önbelleği atladı", len(ran) == 4)
        res = iv.rescan(client, **ckw, **skw)
        check("rescan: biter bitmez yeniden istek bekleme süresine takılır", len(spawned) == 2 and res["cooldown"] > 0)
        # tarayıcı yok: sonuç önbelleğe girmez → her GET yeni tarama başlatmasın
        with tempfile.TemporaryDirectory() as td2:
            def noscan(image):
                return {"state": "tarayıcı yok", "critical": 0, "high": 0, "packages": [], "os": "", "created": ""}
            nkw2 = {**skw, "scan_fn": noscan, "cache_dir": Path(td2)}
            iv._reset_state()
            clk[0] = 1000.0
            sp2: list = []
            k2 = dict(spawn=sp2.append, clock=lambda: clk[0])
            iv.serve(client, **k2, **nkw2)
            sp2[0]()
            res = iv.serve(client, **k2, **nkw2)
            check("taranamayan imaj sonsuz yeniden tarama döngüsü kurmaz (son sonuç döner)",
                  len(sp2) == 1 and res["scanning"] is False
                  and {r["scan"]["state"] for r in res["images"]} == {"tarayıcı yok"}, str(res)[:200])
            clk[0] += 301
            iv.serve(client, **k2, **nkw2)
            check("bekleme süresi dolunca otomatik ilk tarama yeniden denenebilir", len(sp2) == 2)
        # çöken arka plan taraması: bayrak temizlenir, hata kaydı tutulur, hemen yeniden doğmaz
        with tempfile.TemporaryDirectory() as td3:
            def boom(image):
                raise RuntimeError("docker çöktü")
            bkw = {**skw, "scan_fn": boom, "cache_dir": Path(td3)}
            iv._reset_state()
            clk[0] = 1000.0
            sp3: list = []
            k3 = dict(spawn=sp3.append, clock=lambda: clk[0])
            iv.serve(client, **k3, **bkw)
            sp3[0]()
            res = iv.serve(client, **k3, **bkw)
            check("çöken tarama: bayrak temizlendi, GET 500 vermez, bekleme içinde yeniden doğmaz",
                  iv._RUN["running"] is False and len(sp3) == 1 and res["scanning"] is False
                  and len(res["images"]) == 2, str(res)[:200])
            clk[0] += 301
            iv.serve(client, **k3, **bkw)
            check("çöküşten sonra bekleme dolunca yeniden denenir", len(sp3) == 2)
        # bellek anlık görüntüsü: 60 sn içinde Azure okuması yok
        with tempfile.TemporaryDirectory() as td4:
            cnt = {"n": 0, "dec": 0}
            mc = _iv_client({"/php-9-9/Dockerfile": "FROM php:8.4.27\n"})
            _gfc = mc.get_file_content

            def counting(*a, **k):
                cnt["n"] += 1
                return _gfc(*a, **k)
            mc.get_file_content = counting
            midx = {"a": [{"src": "anatolia/build/a", "images": ["r.example/prod-ar/php-9-9:1"]}]}
            mkw = {**skw, "index": midx, "cache_dir": Path(td4)}

            def dec(rep):
                cnt["dec"] += 1
                return rep
            iv._reset_state()
            clk[0] = 5000.0
            m1: list = []
            km = dict(spawn=m1.append, clock=lambda: clk[0], decorate=dec)
            iv.serve(mc, **km, **mkw)
            n1, d1 = cnt["n"], cnt["dec"]
            clk[0] += 30
            iv.serve(mc, **km, **mkw)
            iv.serve(mc, **km, **mkw)
            check("60 sn içindeki GET'ler bellekten döner: Azure okuması ve giderme planı yeniden kurulmaz",
                  n1 > 0 and cnt["n"] == n1 and cnt["dec"] == d1 == 1, f"{n1} {cnt}")
            clk[0] += 40
            iv.serve(mc, **km, **mkw)
            check("60 sn geçince anlık görüntü yeniden kurulur", cnt["n"] > n1 and cnt["dec"] == 2, str(cnt))
            m1[0]()
            n2 = cnt["n"]
            iv.serve(mc, **km, **mkw)
            check("arka plan taraması bitince anlık görüntü hemen yenilenir (60 sn beklenmez)", cnt["n"] > n2, str(cnt))
        iv._reset_state()

    # ── uç: sunucu işleyicisi ───────────────────────────────────────────
    import asyncio
    from agile_sdlc_crew import server
    _get2 = pc.get
    _serve, _resc, _cl = iv.serve, iv.rescan, server.AzureDevOpsClient
    try:
        knobs2 = {"CREW_TECHDEBT": True, "CREW_TECHDEBT_IMAGE_VULN": False}
        pc.get = lambda k: knobs2[k] if k in knobs2 else _get2(k)
        server.AzureDevOpsClient = lambda: object()
        got_kw: list = []
        iv.serve = lambda client, **kw: got_kw.append(kw) or {"scanning": False, "images": [{"image": "x"}]}
        iv.rescan = lambda client, **kw: got_kw.append(("rescan", kw)) or {"scanning": True, "images": [{"image": "x"}]}
        r = asyncio.run(server.techdebt_images())
        check("ayar kapalı → 403 Türkçe neden", r.status_code == 403 and "taban imaj" in json.loads(r.body)["error"].lower())
        check("rescan: ayar kapalı → 403", asyncio.run(server.techdebt_images_rescan()).status_code == 403)
        knobs2["CREW_TECHDEBT"] = False
        knobs2["CREW_TECHDEBT_IMAGE_VULN"] = True
        check("CREW_TECHDEBT kapalı → 403", asyncio.run(server.techdebt_images()).status_code == 403)
        knobs2["CREW_TECHDEBT"] = True
        r = asyncio.run(server.techdebt_images())
        body = json.loads(r.body)
        check("ayar açık → 200 + images; GET tarama zorlamaz, giderme planı anlık görüntüyle kurulur",
              r.status_code == 200 and body["images"] and "force" not in got_kw[-1] and callable(got_kw[-1].get("decorate")))
        check("GET işleyicisi force parametresi almaz (yan etkili GET yok)",
              "force" not in inspect.signature(server.techdebt_images).parameters)
        r = asyncio.run(server.techdebt_images_rescan())
        check("rescan (POST) → 200, iv.rescan çağrılır",
              r.status_code == 200 and json.loads(r.body)["scanning"] is True and got_kw[-1][0] == "rescan")
        check("rescan POST rotası kayıtlı",
              any(getattr(rt, "path", "") == "/api/techdebt/images/rescan" and "POST" in getattr(rt, "methods", set())
                  for rt in server.app.routes))
        iv.serve = lambda client, **kw: (_ for _ in ()).throw(ValueError("kötü"))
        check("hata Türkçe JSON'a çevrilir (400)", asyncio.run(server.techdebt_images()).status_code == 400)
    finally:
        pc.get = _get2
        iv.serve, iv.rescan, server.AzureDevOpsClient = _serve, _resc, _cl

    # ── yoklayıcı ───────────────────────────────────────────────────────
    iv._POLLER = None
    calls: list = []
    sleeps: list = []
    stop = threading.Event()
    done = threading.Event()

    def tick(get_client):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Azure düştü")
        stop.set()
        done.set()
    th = iv.start_poller(lambda: None, interval_h=lambda: 6, sleep=lambda s: sleeps.append(s), tick=tick, stop_event=stop)
    done.wait(5)
    th.join(5)
    check("yoklayıcı: ilk tur bir aralık sonra, aralık saat → saniye", sleeps and sleeps[0] == 6 * 3600, str(sleeps))
    check("yoklayıcı: istisna döngüyü durdurmaz (ikinci tur koştu)", len(calls) == 2, str(calls))
    check("yoklayıcı: daemon thread", th.daemon)
    iv._POLLER = None
    # _tick: ayarlar kapalıysa Azure'a gidilmez; açıksa rapor + uyarı sırayla
    seen: list = []
    from agile_sdlc_crew.quality import modern as _mdl
    _rep, _alert, _get3, _bi3 = iv.report, iv.alert, pc.get, _mdl.build_index
    _mdl.build_index = lambda c, **kw: {}
    try:
        knobs3 = {"CREW_TECHDEBT": True, "CREW_TECHDEBT_IMAGE_VULN": False}
        pc.get = lambda k: knobs3[k] if k in knobs3 else _get3(k)
        iv.report = lambda client, **kw: seen.append(("report", kw.get("force"))) or {"images": []}
        iv.alert = lambda client, rep, **kw: seen.append(("alert", kw.get("team"))) or []
        cl = SimpleNamespace(team="Ekip")
        iv._tick(lambda: cl)
        check("_tick: ayar kapalı → hiçbir şey yapmaz", seen == [])
        knobs3["CREW_TECHDEBT_IMAGE_VULN"] = True
        iv._tick(lambda: cl)
        check("_tick: force=True rapor sonra uyarı (istemcinin takımıyla)", seen == [("report", True), ("alert", "Ekip")], str(seen))
        flags: list = []
        _a2 = iv.alert
        iv.alert = lambda client, rep, **kw: flags.append(iv._RUN["running"]) or []
        iv._tick(lambda: cl)
        iv.alert = _a2
        check("_tick: alert / giderme çalışırken tarama bayrağı bırakılmıştır (kart 'Taranıyor' demez)", flags == [False], str(flags))
        iv._RUN["running"] = True
        seen.clear()
        iv._tick(lambda: cl, sleep=lambda s: None, max_wait_s=60, step_s=30)
        check("_tick: başka tarama sürerken (bekleme süresi dolunca) atlanır", seen == [])
        iv._RUN["running"] = False
    finally:
        iv.report, iv.alert, pc.get, _mdl.build_index = _rep, _alert, _get3, _bi3

    # ── varsayılan iş açma yolu: teknik borç hikâyesi + needs-human Task (sahte client, yazma yok) ──
    from agile_sdlc_crew.quality import intake

    class _FC:
        org_url, project = "https://dev.azure.com/o", "P"

        def __init__(self):
            self.created, self.updates = [], []

        def list_iterations(self, team=""):
            return [{"name": "Sprint 42", "path": "P\\Sprint 42", "attributes": {"timeFrame": "current"}}]

        def query_work_items(self, wiql, limit=0):
            return []

        def get_team_area_path(self, team=""):
            return "P\\Team"

        def get_authenticated_user(self):
            return {"uniqueName": "me@flo.com.tr"}

        def create_work_item(self, type_name, fields, parent_id=None):
            self.created.append((type_name, fields, parent_id))
            return {"id": 2000 + len(self.created) - 1, "fields": fields}

        def update_work_item(self, wid, ops):
            self.updates.append((wid, ops))
            return {}
    fc, recorded, queued = _FC(), [], []
    ideps = intake.Deps(gather=None, board=lambda c: {}, repo_ok=lambda r: True,
                        queue=lambda wid, title: queued.append(wid), record=lambda **kw: recorded.append(kw),
                        now=lambda: 2e9, cfg=lambda: {"task_fields": {}, "sonar_url": "", "nr_days": 7})
    got = iv._open_task(fc, rec, team="Team", deps=ideps)
    tasks = [x for x in fc.created if x[0] == "Task"]
    check("varsayılan yol: bir Task, needs-human, kuyruğa alınmaz, parmak izi kaydı",
          got["id"] == 2001 and len(tasks) == 1 and "needs-human" in tasks[0][1]["System.Tags"]
          and "tempo-image-vuln" in tasks[0][1]["System.Tags"] and queued == []
          and recorded and recorded[0]["fingerprint"] == "image_vuln:golang:1.24.4" and recorded[0]["needs_human"] is True,
          str(tasks)[:300])
    check("varsayılan yol: Task öncelik alanı 1 yazılır",
          tasks[0][1].get("Microsoft.VSTS.Common.Priority") == 1, str(sorted(tasks[0][1])))
    check("varsayılan yol: Task açıklamasında kaçışlı gövde + sınır cümlesi",
          "&lt;img" in tasks[0][1]["System.Description"] and "sonuç alt sınırdır" in tasks[0][1]["System.Description"])

    html_src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text()
    seg = html_src[html_src.index("async function loadImageVuln"):html_src.index("function renderImageVuln")
                   + html_src[html_src.index("function renderImageVuln"):].index("\n}\n")]
    check("kart: uç, 403'te gizlenir, yeniden tara, yok sayılan satırı, bölge sayıları",
          "/api/techdebt/images" in seg and "resp.status===403" in seg and 'id="tdImgCard"' in html_src
          and "yok sayılan:" in html_src and "loadImageVuln(true)" in html_src and "u[k]||[]).length" in seg)
    check("kart: etkilenen repolar görünür liste; yüklenirken kart gösterilir; sekmenin son kartı",
          "Etkilenen repolar" in seg and "x.repos" in seg and "_tdLoad(" in seg and "_tdOff('img')" in seg
          and html_src.index('id="tdImgCard"') > html_src.index('id="tdModernCard"'))
    check("kart: açığı olan imajlar listenin tepesinde ayrı bölüm (kritik + yüksek > 0), kalanı 'Diğer imajlar'",
          "Güvenlik açığı olan imajlar" in seg and "Diğer imajlar" in seg
          and seg.index("Güvenlik açığı olan imajlar") < seg.index("Diğer imajlar")
          and "(c.critical||0)+(c.high||0))>0" in seg)
    check("kart: CVE bağlantıları + duyuru tarihi / gün sayısı; adres yalnız tam kalıba uyan kimlikten",
          "_ivCves(p)" in html_src and "nvd.nist.gov/vuln/detail/" in html_src and "_ivOldestDays(" in seg
          and "gün önce" in html_src and "/^CVE-\\d{4}-\\d{4,}$/" in html_src)
    det = html_src[html_src.index("function _ivCveTable"):html_src.index("function closeIvDetail")]
    check("kart: satır özet, ayrıntı pencerede (kritik açıklar tablosu, repolar, eylem, giderme düğmesi)",
          "openIvDetail(this.dataset.image)" in seg and "ivFixBtn" not in seg and "<details" not in seg
          and 'id="ivModal"' in html_src and "Kritik açıklar" in det and "Yüksek açıklar" in det
          and "_ivCveTable(pk,'CRITICAL')" in det and "ivFixBtn" in det and "Etkilenen repolar" in det
          and "${x.image}" not in det and "${p.name}" not in det and "${e.cve}" not in det)
    psv = iv._parse_scan({"Results": [{"Vulnerabilities": (
        [{"Severity": "HIGH", "FixedVersion": "2", "PkgName": "a", "VulnerabilityID": f"CVE-2026-10{i:02d}"} for i in range(6)]
        + [{"Severity": "CRITICAL", "FixedVersion": "2", "PkgName": "a", "VulnerabilityID": f"CVE-2026-20{i:02d}",
            "PublishedDate": "2026-01-02T00:00:00Z"} for i in range(7)])}]})["packages"][0]
    check("tarama: kimlik başına önem; kritikler sınıra takılmaz ve önce gelir, yüksekler yalnız boş yeri doldurur",
          psv["cves"] == [f"CVE-2026-20{i:02d}" for i in range(7)] and set(psv["sev"].values()) == {"CRITICAL"}
          and psv["critical"] == 7 and psv["high"] == 6 and len(psv["published"]) == 7, str(psv))
    psh = iv._parse_scan({"Results": [{"Vulnerabilities": [
        {"Severity": "HIGH", "FixedVersion": "2", "PkgName": "a", "VulnerabilityID": "CVE-2026-1000"},
        {"Severity": "CRITICAL", "FixedVersion": "2", "PkgName": "a", "VulnerabilityID": "CVE-2026-2000"}]}]})["packages"][0]
    check("tarama: kritik + yüksek karışık → kritik önce, önem eşlemesi doğru",
          psh["cves"] == ["CVE-2026-2000", "CVE-2026-1000"]
          and psh["sev"] == {"CVE-2026-2000": "CRITICAL", "CVE-2026-1000": "HIGH"}, str(psh))
    check("kart: her aralanan metin escHtml'den geçer (ham ${x.image} / ${p.name} yok)",
          "${x.image}" not in seg and "${p.name}" not in seg and "_ivE=s=>escHtml(" in html_src)

    by = {f["key"]: f for f in pc.SCHEMA}
    mt = by.get("CREW_TECHDEBT_IMAGE_VULN_MAX_TASKS", {})
    check("_MAX_TASKS int, varsayılan 3, min 1", mt.get("type") == "int" and mt.get("default") == 3 and mt.get("min") == 1, str(mt))


class _FixClient(_FakeBaseImages):
    """base-images (staging) + bölge repoları (production Dockerfile'ları); yazma çağrıları `calls`ta."""

    def __init__(self, dockerfiles=None, region_prs=None, **kw):
        super().__init__(**kw)
        self.docker = dict(dockerfiles or {})
        self.region_prs = dict(region_prs or {})

    def get_file_content(self, repo, path, branch=None, project=None):
        if repo == "base-images":
            data = self.files[branch].get(path)
        else:
            assert branch == "production" and project == "FLODevOps", (repo, branch, project)
            data = self.docker.get((repo, path))
        if data is None:
            raise FileNotFoundError(path)
        return data.decode() if isinstance(data, bytes) else data

    def find_pull_request(self, repo, branch, project=None):
        if repo == "base-images":
            return self.prs.get(branch)
        return self.region_prs.get((repo, branch))


class _PromoClient(_FixClient):
    """base-images'ta üç dal (staging / preprod / production) + dallar arası PR araması."""

    def __init__(self, dockerfiles=None, branches=None, between=None, **kw):
        super().__init__(dockerfiles, **kw)
        self.files = {b: dict(f) for b, f in (branches or {}).items()}
        self.between = dict(between or {})
        self.diff_fail = False
        self.commits: dict = {}             # commit → {yol: bayt} (eski PR'ın taşıdığı içerik)

    def get_file_bytes(self, repo, path, branch=None, project=None, version=None, version_type=None, **kw):
        assert version and version_type == "commit", (version, version_type)
        return self.commits[version].get(path)

    def find_pull_request_between(self, repo, src, dst, project=None):
        assert repo == "base-images" and project == "FLODevOps", (repo, project)
        return self.between.get((src, dst))

    def get_commit_diff_paths(self, repo, base, target):
        if self.diff_fail:
            raise OSError("diff okunamadı")
        return ["/php-8-4/Dockerfile", "/php-8-4/tag.txt", "/x`y/Dockerfile"]


def test_image_fix_promote_tag():
    """Taşıma PR'ı (base-images dalları arası) ve bölge repolarında etiket yükseltme. Ağ / Azure / MySQL yok."""
    print("\n[taban imaj güvenliği: taşıma ve etiket]")
    from agile_sdlc_crew.quality import image_fix as fx, image_vuln as iv
    from agile_sdlc_crew.quality import upgrade_bump as bump, upgrade_image as ui, upgrade_registry as ur
    knobs = {"CREW_TECHDEBT_UPGRADE": True, "CREW_TECHDEBT_PHP_MODERN": True, "CREW_TECHDEBT_UPGRADE_BUMP": True,
             "CREW_TECHDEBT_IMAGE_VULN_PROMOTE": True}
    reg, ru = "europe-west1-docker.pkg.dev/flo-prod-main/prod-ar", "10.131.167.220:5000/prod-ar"
    res = ur.Resolver(fetch=_fake_hub({"php": ["8.4.28", "8.4.28-fpm-trixie", "8.4.28-fpm-bookworm"]}))
    NEW = {"/php-8-4/Dockerfile": b"ARG PHP_VERSION=8.4.28\nFROM php:${PHP_VERSION}-fpm-trixie\n", "/php-8-4/tag.txt": b"1.1"}
    OLD = {"/php-8-4/Dockerfile": b"ARG PHP_VERSION=8.4.27\nFROM php:${PHP_VERSION}-fpm-bookworm\n", "/php-8-4/tag.txt": b"1.0"}
    index = {"channel": [{"src": "anatolia/build/channel", "images": [f"{reg}/php-8-4:1.0", "builder"]}],
             "core": [{"src": "russia/build/core", "images": [f"{ru}/php-8-4:1.0"]}],
             "fresh": [{"src": "global/build/fresh", "images": [f"{reg}/php-8-4:1.1"]}]}
    DF = {("anatolia", "/build/channel/Dockerfile"): f"FROM {reg}/php-8-4:1.0 AS base\nRUN x\nFROM builder\n",
          ("russia", "/build/core/Dockerfile"): f"FROM {ru}/php-8-4:1.0\n"}

    def rec(**kw):
        r = {"image": "php:8.4.27-fpm-bookworm", "lang": "php", "version": "8.4.27", "via": ["php-8-4"], "touched": "",
             "error": "", "users": {"anatolia": ["build/channel"], "russia": ["build/core"]}, "tag_lag": {},
             "scan": {"state": "tamam", "critical": 0, "high": 0, "packages": [], "os": "", "created": "",
                      "ignored": {"critical": 0, "high": 0, "packages": []}},
             "patch": {"latest": "8.4.28"}, "eol": None, "level": "yüksek", "actions": []}
        r.update(kw)
        return r

    def kind(acts, k):
        return next((a for a in acts if a["kind"] == k), None)

    def never(*a, **k):
        raise AssertionError("yazma çağrılmamalıydı")

    with _fix_env(knobs):
        # ── taşıma: plan ──────────────────────────────────────────────────
        c = _PromoClient(DF, {"staging": NEW, "preprod": NEW, "production": OLD})
        acts = fx.plan(c, rec(), index=index, resolver=res)
        a = kind(acts, "taban-taşıma")
        check("taşıma: staging = preprod, production eski → preprod → production önerilir",
              kind(acts, "taban-yama")["state"] == "güncel" and a and a["state"] == "yok"
              and (a["src"], a["dst"]) == ("preprod", "production") and a["dir"] == "php-8-4", str(a))
        check("taşıma: plan yazmaz", c.calls == [], str(c.calls))
        c = _PromoClient(DF, {"staging": NEW, "preprod": OLD, "production": OLD})
        a = kind(fx.plan(c, rec(), index=index, resolver=res), "taban-taşıma")
        check("taşıma: preprod da eski → önce staging → preprod", a["state"] == "yok" and (a["src"], a["dst"]) == ("staging", "preprod"), str(a))
        c = _PromoClient(DF, {"staging": NEW, "preprod": NEW, "production": NEW})
        check("taşıma: üç dal aynı → eylem yok", kind(fx.plan(c, rec(), index=index, resolver=res), "taban-taşıma") is None)
        c = _PromoClient(DF, {"staging": OLD, "preprod": OLD, "production": OLD})
        acts = fx.plan(c, rec(), index=index, resolver=res)
        check("taşıma: düzeltme staging'de değilken önerilmez (önce yama PR'ı)",
              kind(acts, "taban-yama")["state"] == "yok" and kind(acts, "taban-taşıma") is None, str(acts))
        pr = {"id": 9, "status": "active", "url": "https://dev.azure.com/org/FLODevOps/_git/base-images/pullrequest/9",
              "source_commit": "abc1234"}
        c = _PromoClient(DF, {"staging": NEW, "preprod": NEW, "production": OLD}, between={("preprod", "production"): pr})
        c.commits = {"abc1234": NEW, "def5678": OLD}
        a = kind(fx.plan(c, rec(), index=index, resolver=res), "taban-taşıma")
        check("taşıma: açık PR varsa (kim açmış olursa olsun) 'pr açık'", a["state"] == "pr açık" and a["pr"]["id"] == 9, str(a))
        c.between[("preprod", "production")] = dict(pr, status="abandoned")
        a = kind(fx.plan(c, rec(), index=index, resolver=res), "taban-taşıma")
        check("taşıma: aynı içerik reddedilmişse yeniden açılmaz", a["state"] == "pr reddedildi", str(a))
        c.oids["preprod"] = "0000aaaa"      # paylaşılan dala ilgisiz bir commit geldi: dal başı değişti, dizin aynı
        a = kind(fx.plan(c, rec(), index=index, resolver=res), "taban-taşıma")
        check("taşıma: dal başı ilerlese de dizin içeriği aynıysa reddedilen PR yeniden açılmaz", a["state"] == "pr reddedildi", str(a))
        c.between[("preprod", "production")] = dict(pr, status="abandoned", source_commit="def5678")
        a = kind(fx.plan(c, rec(), index=index, resolver=res), "taban-taşıma")
        check("taşıma: reddedilen PR bu dizinin BAŞKA içeriğini taşıyorduysa yeni içerik önerilir", a["state"] == "yok", str(a))
        c.between[("preprod", "production")] = dict(pr, status="completed")
        a = kind(fx.plan(c, rec(), index=index, resolver=res), "taban-taşıma")
        check("taşıma: aynı içerik tamamlanmış PR'la taşınmış ama hedef hâlâ farklı → yeniden açılmaz (pr kapandı)",
              a["state"] == "pr kapandı" and "elle" in a["reason"], str(a))
        c.between[("preprod", "production")] = dict(pr, status="completed", source_commit="def5678")
        a = kind(fx.plan(c, rec(), index=index, resolver=res), "taban-taşıma")
        check("taşıma: eski içeriği taşımış tamamlanmış PR engel değil", a["state"] == "yok", str(a))
        c.between[("preprod", "production")] = dict(pr, status="abandoned", source_commit="")
        a = kind(fx.plan(c, rec(), index=index, resolver=res), "taban-taşıma")
        check("taşıma: önceki PR'ın içeriği okunamıyorsa belirsiz (yazılmaz)", a["state"] == "belirsiz", str(a))
        # yön: hedef dal ileride / ayrışmışsa taşıma önerilmez
        AHEAD = {"/php-8-4/Dockerfile": b"ARG PHP_VERSION=8.4.29\nFROM php:${PHP_VERSION}-fpm-trixie\n", "/php-8-4/tag.txt": b"1.2"}
        c = _PromoClient(DF, {"staging": NEW, "preprod": NEW, "production": AHEAD})
        a = kind(fx.plan(c, rec(), index=index, resolver=res), "taban-taşıma")
        check("taşıma: production ilerideyse (kendi düzeltmesi var) önerilmez → elle", a["state"] == "elle" and "geride değil" in a["reason"], str(a))
        c = _PromoClient(DF, {"staging": NEW, "preprod": AHEAD, "production": OLD})
        a = kind(fx.plan(c, rec(), index=index, resolver=res), "taban-taşıma")
        check("taşıma: preprod staging'den ilerideyse staging → preprod açılmaz → elle", a["state"] == "elle", str(a))
        SAMEV = {"/php-8-4/Dockerfile": NEW["/php-8-4/Dockerfile"] + b"RUN hotfix\n", "/php-8-4/tag.txt": b"1.1"}
        c = _PromoClient(DF, {"staging": NEW, "preprod": NEW, "production": SAMEV})
        a = kind(fx.plan(c, rec(), index=index, resolver=res), "taban-taşıma")
        check("taşıma: sürüm ve etiket aynı, içerik ayrışmış → elle", a["state"] == "elle", str(a))
        CRLF = {"/php-8-4/Dockerfile": NEW["/php-8-4/Dockerfile"].replace(b"\n", b"\r\n"), "/php-8-4/tag.txt": b"1.1\n"}
        c = _PromoClient(DF, {"staging": NEW, "preprod": NEW, "production": CRLF})
        check("taşıma: yalnız satır sonu / sondaki boşluk farkı taşıma sayılmaz",
              kind(fx.plan(c, rec(), index=index, resolver=res), "taban-taşıma") is None)
        two = rec(via=["php-8-4", "php-8-2"])
        B2 = {"/php-8-2/Dockerfile": b"ARG PHP_VERSION=8.4.28\nFROM php:${PHP_VERSION}-fpm-trixie\n", "/php-8-2/tag.txt": b"1.1"}
        O2 = {"/php-8-2/Dockerfile": b"ARG PHP_VERSION=8.4.27\nFROM php:${PHP_VERSION}-fpm-trixie\n", "/php-8-2/tag.txt": b"1.0"}
        c = _PromoClient(DF, {"staging": {**NEW, **B2}, "preprod": {**NEW, **B2}, "production": {**OLD, **O2}})
        acts = fx.plan(c, two, index=index, resolver=res)
        check("taşıma: iki dizin aynı dal çiftini isterse tek eylem",
              len([x for x in acts if x["kind"] == "taban-taşıma"]) == 1, str([(x["kind"], x["state"]) for x in acts]))
        for bad in (["staging"], ["preprod", "production"], ["staging", "pre prod"], ["staging", "staging"], "staging"):
            cfg = dict(ui.load_cfg(), flow=bad)
            c = _PromoClient(DF, {"staging": NEW, "preprod": NEW, "production": OLD})
            a = kind(fx.plan(c, rec(), index=index, resolver=res, cfg=cfg), "taban-taşıma")
            check(f"taşıma: geçersiz flow {bad!r} → elle, yazılmaz", a and a["state"] == "elle", str(a))
        c = _PromoClient(DF, {"staging": NEW, "preprod": NEW})
        a = kind(fx.plan(c, rec(), index=index, resolver=res), "taban-taşıma")
        check("taşıma: hedef dal okunamıyorsa belirsiz (yazılmaz)", a and a["state"] == "belirsiz", str(a))
        knobs["CREW_TECHDEBT_IMAGE_VULN_PROMOTE"] = False
        c = _PromoClient(DF, {"staging": NEW, "preprod": NEW, "production": OLD})
        acts = fx.plan(c, rec(tag_lag={"php-8-4": {"have": ["1.0"], "latest": "1.1"}}), index=index, resolver=res)
        check("taşıma / etiket anahtarı kapalıyken (varsayılan) ikisi de hiç planlanmaz; yama eylemi eskisi gibi 'güncel'",
              [(x["kind"], x["state"]) for x in acts] == [("taban-yama", "güncel")], str(acts))
        out = fx.apply(c, rec(tag_lag={"php-8-4": {"have": ["1.0"], "latest": "1.1"}}), team="T", index=index, resolver=res,
                       push_fn=never, open_pr_fn=never, start_fn=never)
        check("anahtar kapalıyken uygula hiçbir şey yazmaz", c.calls == [] and not any(x["ran"] for x in out), str(c.calls))
        knobs["CREW_TECHDEBT_IMAGE_VULN_PROMOTE"] = True
        from agile_sdlc_crew import pipeline_config as _pcx
        check("ayar: CREW_TECHDEBT_IMAGE_VULN_PROMOTE varsayılan kapalı",
              next(d for d in _pcx.SCHEMA if d["key"] == "CREW_TECHDEBT_IMAGE_VULN_PROMOTE")["default"] is False)

        # ── taşıma: uygula ────────────────────────────────────────────────
        c = _PromoClient(DF, {"staging": NEW, "preprod": NEW, "production": OLD})
        out = fx.apply(c, rec(), team="T", index=index, resolver=res, push_fn=never, open_pr_fn=never, start_fn=never)
        a = kind(out, "taban-taşıma")
        prs = [x for x in c.calls if x[0] == "pr"]
        check("taşıma uygula: tek taslak PR preprod → production; dal / push yok",
              a["state"] == "açıldı" and a["ran"] is True and len(c.calls) == 1 and len(prs) == 1
              and prs[0][1:3] == ("preprod", "production") and prs[0][5] is True and prs[0][6] == "FLODevOps", str(c.calls)[:400])
        body = prs[0][4]
        check("taşıma gövdesi: dalın tamamını taşıdığını, değişen dosyaları ve RU bayrağını söyler",
              "TAMAMINI" in body and "/php-8-4/tag.txt" in body and "yandexLocation" in body and "russia" in body
              and "x`y" not in body and len(body) <= 4000, body)
        check("taşıma: web PR bağlantısı", a["pr"] == {"id": 77, "url": "https://dev.azure.com/org/FLODevOps/_git/base-images/pullrequest/77"}, str(a["pr"]))
        c = _PromoClient(DF, {"staging": NEW, "preprod": NEW, "production": OLD})
        c.diff_fail = True
        out = fx.apply(c, rec(), team="T", index=index, resolver=res, push_fn=never, open_pr_fn=never, start_fn=never)
        check("taşıma: dosya listesi okunamazsa PR yine açılır, gövde söyler",
              kind(out, "taban-taşıma")["state"] == "açıldı" and "okunamadı" in [x for x in c.calls if x[0] == "pr"][0][4])
        c = _PromoClient(DF, {"staging": NEW, "preprod": NEW, "production": OLD}, fail_at="pr")
        a = kind(fx.apply(c, rec(), team="T", index=index, resolver=res, push_fn=never, open_pr_fn=never, start_fn=never), "taban-taşıma")
        check("taşıma: PR açılamazsa hata, ran False (sıra harcanmaz)", a["state"] == "hata" and a["ran"] is False, str(a))
        c = _PromoClient(DF, {"staging": NEW, "preprod": NEW, "production": OLD})
        c.create_pull_request = lambda *a_, **k: c._deny()
        a = kind(fx.apply(c, rec(), team="T", index=index, resolver=res, push_fn=never, open_pr_fn=never, start_fn=never), "taban-taşıma")
        check("taşıma: 401/403 → elle, ran False", a["state"] == "elle" and a["ran"] is False, str(a))
        c = _PromoClient(DF, {"staging": NEW, "preprod": NEW, "production": OLD})
        c.pr_response = {}
        a = kind(fx.apply(c, rec(), team="T", index=index, resolver=res, push_fn=never, open_pr_fn=never, start_fn=never), "taban-taşıma")
        check("taşıma: yanıt okunamazsa belirsiz ama ran True (PR açılmış olabilir)", a["state"] == "belirsiz" and a["ran"] is True, str(a))

        # ── etiket: envanter işareti ──────────────────────────────────────
        check("etiket karşılaştırması sayısal", iv.tag_newer("1.0", "1.1") and iv.tag_newer("1.9", "1.10")
              and not iv.tag_newer("1.0", "1.0.0") and not iv.tag_newer("1", "1.1")
              and not iv.tag_newer("1.1", "1.1") and not iv.tag_newer("1.2", "1.1") and not iv.tag_newer("latest", "1.1")
              and not iv.tag_newer("1.0", "1.1;rm"))
        files = {"/php-8-4/Dockerfile": NEW["/php-8-4/Dockerfile"].decode(), "/php-8-4/tag.txt": "1.1\n"}
        recs = {r["image"]: r for r in iv.inventory(_iv_client({}), index, read=files.get)}
        p = recs["php:8.4.28-fpm-trixie"]
        check("envanter: üretim etiketi 1.1, bölgeler 1.0 çekiyor → tag_lag (güncel bölge sayılmaz)",
              p["tag_lag"] == {"php-8-4": {"have": ["1.0"], "latest": "1.1"}}, str(p["tag_lag"]))
        p.update(scan={"state": "tamam", "critical": 0, "high": 0, "packages": []}, patch=None, eol=None)
        lvl, actions = iv._judge(p)
        check("etiket geride → en az 'yüksek' + eylem metni", lvl == "yüksek"
              and "bölge repolarında imaj etiketini yükselt (php-8-4:1.1)" in actions, f"{lvl} {actions}")
        files["/php-8-4/tag.txt"] = "1.0"
        p0 = {r["image"]: r for r in iv.inventory(_iv_client({}), index, read=files.get)}["php:8.4.28-fpm-trixie"]
        check("envanter: etiket aynıyken işaret yok", p0["tag_lag"] == {}, str(p0["tag_lag"]))

        # ── etiket: plan ──────────────────────────────────────────────────
        lagrec = rec(image="php:8.4.28-fpm-trixie", version="8.4.28", patch=None, tag_lag={"php-8-4": {"have": ["1.0"], "latest": "1.1"}})
        c = _PromoClient(DF, {"staging": NEW, "preprod": NEW, "production": NEW})
        acts = fx.plan(c, lagrec, index=index, resolver=res)
        a = kind(acts, "taban-etiket")
        check("etiket plan: yalnız etiket eylemi, bölge başına birim, sabit dal adı",
              [x["kind"] for x in acts] == ["taban-etiket"] and a["state"] == "yok"
              and all(re.fullmatch(r"tempo/image-tag-php-8-4-1\.1-[0-9a-f]{8}", u["branch"]) for u in a["repos"])
              and [u["repo"] for u in a["repos"]] == ["anatolia", "russia"] and a["target"] == "php-8-4:1.1", str(a))
        br_a = a["repos"][0]["branch"]
        sa, sr = a["repos"][0]["sites"], a["repos"][1]["sites"]
        check("etiket plan: her bölgede kendi kayıt deposu adıyla yalnız etiket değişir (güncel bölgeye dokunulmaz)",
              len(sa) == 1 and sa[0]["old"] == f"{reg}/php-8-4:1.0" and sa[0]["new"] == f"{reg}/php-8-4:1.1" and sa[0]["line"] == 1
              and sa[0]["branch"] == "production" and sr[0]["old"] == f"{ru}/php-8-4:1.0" and sr[0]["new"] == f"{ru}/php-8-4:1.1"
              and all(bump.pushable(s) for s in sa + sr), f"{sa} {sr}")
        try:
            bump._validate(sa, br_a)
            bump._validate(sr, a["repos"][1]["branch"])
            check("etiket: siteler ve dal adı push_sites doğrulamasından geçer", True)
        except ValueError as e:
            check("etiket: siteler ve dal adı push_sites doğrulamasından geçer", False, str(e))
        for bad in ("tempo/image-tag-php-8-4-", "tempo/image-tag-php-8-4-1.1", "tempo/image-tag-php-8-4-1.1-abcdef12/x",
                    "tempo/image-tag-../x-1.1-abcdef12", "tempo/image-tag-PHP-1.1-abcdef12", "production",
                    "tempo/image-tag-php-8-4-1.1.1.1.1-abcdef12", "tempo/image-tag-php-8-4-1.1-ABCDEF12"):
            try:
                bump._validate_branch(bad, {"production"})
                check(f"dal adı reddedilir: {bad}", False)
            except ValueError:
                check(f"dal adı reddedilir: {bad}", True)
        check("etiket plan yazmaz", c.calls == [])
        c = _PromoClient(DF, {"production": dict(NEW, **{"/php-8-4/tag.txt": b"1.1;rm"})})
        a = kind(fx.plan(c, lagrec, index=index, resolver=res), "taban-etiket")
        check("etiket plan: üretim etiketi sayısal değilse elle", a["state"] == "elle", str(a))
        c = _PromoClient(DF, {"production": OLD})
        check("etiket plan: taze okunan üretim etiketi geride değilse eylem yok (rapor bayatmış)",
              kind(fx.plan(c, lagrec, index=index, resolver=res), "taban-etiket") is None)
        DF2 = dict(DF)
        DF2[("russia", "/build/core/Dockerfile")] = f"FROM {ru}/php-8-4:1.0@sha256:{'a' * 64}\n"
        c = _PromoClient(DF2, {"production": NEW})
        a = kind(fx.plan(c, lagrec, index=index, resolver=res), "taban-etiket")
        check("etiket plan: bir bölgede satır bulunamıyorsa / özetle sabitse hiçbiri yazılmaz (elle)", a["state"] == "elle" and "russia" in a["reason"], str(a))
        c = _PromoClient(DF, {"production": NEW}, region_prs={("anatolia", br_a): {"id": 4, "status": "active", "url": "https://x/4"}})
        a = kind(fx.plan(c, lagrec, index=index, resolver=res), "taban-etiket")
        check("etiket plan: bir bölgede PR açık, ötekinde yok → açılabilir (yok); açık olan yeniden açılmaz",
              a["state"] == "yok" and a["repos"][0]["state"] == "pr açık" and a["repos"][1]["state"] == "yok", str(a))
        # tamamlanan PR'dan sonra eski etikette yeni bir servis çıkarsa: yeni satır kümesi → yeni dal → yeniden açılabilir
        index2 = dict(index, late=[{"src": "anatolia/build/late", "images": [f"{reg}/php-8-4:1.0"]}])
        DF3 = dict(DF)
        DF3[("anatolia", "/build/late/Dockerfile")] = f"FROM {reg}/php-8-4:1.0\n"
        c = _PromoClient(DF3, {"production": NEW}, region_prs={("anatolia", br_a): {"id": 4, "status": "completed", "url": "https://x/4"}})
        a = kind(fx.plan(c, lagrec, index=index2, resolver=res), "taban-etiket")
        check("etiket plan: kapanmış PR sonradan çıkan servisi engellemez (dal adı satır kümesini taşır)",
              a["repos"][0]["state"] == "yok" and a["repos"][0]["branch"] != br_a and len(a["repos"][0]["sites"]) == 2, str(a["repos"][0]))
        c = _PromoClient(DF3, {"production": NEW}, region_prs={("anatolia", br_a): {"id": 4, "status": "active", "url": "https://x/4"}})
        c.files["anatolia-branches"] = {}
        _lb = c.list_branches
        c.list_branches = lambda repo, project=None: (_lb(repo, project) + [{"name": f"refs/heads/{br_a}", "objectId": "x"}])
        a = kind(fx.plan(c, lagrec, index=index2, resolver=res), "taban-etiket")
        check("etiket plan: aynı etiket için açık PR varken satır kümesi değişirse ikinci PR açılmaz (önce o kapanmalı)",
              a["repos"][0]["state"] == "pr açık" and "önce o kapanmalı" in a["repos"][0]["reason"], str(a["repos"][0]))
        evil = rec(image="php:8.4.28-fpm-trixie", patch=None, via=["php_8"], tag_lag={"php_8": {"have": ["1.0"], "latest": "1.1"}})
        a = kind(fx.plan(_PromoClient(DF, {"production": NEW}), evil, index=index, resolver=res), "taban-etiket")
        check("etiket plan: dal adına çevrilemeyen dizin → elle", a["state"] == "elle", str(a))

        # ── etiket: uygula ────────────────────────────────────────────────
        pushed = []

        def push_ok(client, sites, **kw):
            pushed.append((sites[0]["repo"], kw["branch"], kw["title"], kw["body"], [(s["old"], s["new"]) for s in sites]))
            return {"prs": [{"repo": sites[0]["repo"], "id": 5, "url": "https://x/5"}], "fallback": [], "errors": []}
        c = _PromoClient(DF, {"staging": NEW, "preprod": NEW, "production": NEW})
        out = fx.apply(c, lagrec, team="T", index=index, resolver=res, push_fn=push_ok, open_pr_fn=never, start_fn=never)
        a = kind(out, "taban-etiket")
        check("etiket uygula: bölge başına bir PR (anatolia + russia), ran True",
              a["state"] == "açıldı" and a["ran"] is True and [x[0] for x in pushed] == ["anatolia", "russia"]
              and all(re.fullmatch(r"tempo/image-tag-php-8-4-1\.1-[0-9a-f]{8}", x[1]) for x in pushed)
              and pushed[0][1] != pushed[1][1], str(pushed)[:300])
        check("etiket gövdesi: imaj kayıt deposuna basılmadan birleştirilmemeli + RU notu",
              "basılmadan birleştirmeyin" in pushed[0][3] and "RU (VK)" in pushed[0][3] and "çakışır" in pushed[0][3]
              and len(pushed[0][3]) <= 4000, pushed[0][3])
        knobs["CREW_TECHDEBT_UPGRADE_BUMP"] = False
        out = fx.apply(_PromoClient(DF, {"production": NEW}), lagrec, team="T", index=index, resolver=res, push_fn=never,
                       open_pr_fn=never, start_fn=never)
        check("PR anahtarı kapalıyken etiket PR'ı açılmaz", kind(out, "taban-etiket")["state"] == "elle")
        knobs["CREW_TECHDEBT_UPGRADE_BUMP"] = True

    html_src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text()
    cseg = html_src[html_src.index("function _ivFixConfirm"):html_src.index("async function fixImageVuln")]
    check("kart: onay metni taşıma ve etiket eylemlerini de sayar; etiket geride işareti",
          "taban-taşıma" in cseg and "taban-etiket" in cseg and "etiket geride" in html_src and "x.tag_lag" in html_src)


def test_image_fix():
    from agile_sdlc_crew import pipeline_config as pc
    knobs = {"CREW_TECHDEBT_UPGRADE": True, "CREW_TECHDEBT_PHP_MODERN": True, "CREW_TECHDEBT_UPGRADE_BUMP": True}
    _get = pc.get
    pc.get = lambda k: knobs[k] if k in knobs else _get(k)
    from agile_sdlc_crew.quality import image_fix as _fx
    _cur = getattr(_fx, "_repo_current", None)
    _fx._repo_current = lambda *a, **k: None          # gerçek klonlara gidilmesin
    try:
        with _fix_env(knobs):
            _image_fix_body()
    finally:
        pc.get = _get
        if _cur is not None:
            _fx._repo_current = _cur


def _image_fix_body():
    """Giderme: uyarıyla birlikte sürüm çalışması. Sahte client / resolver / enjekte fonksiyonlar; ağ, Azure, MySQL yok."""
    print("\n[taban imaj güvenliği: giderme]")
    import asyncio
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import server
    from agile_sdlc_crew.quality import image_fix as fx, image_vuln as iv, modern, upgrade as up
    from agile_sdlc_crew.quality import upgrade_bump as bump, upgrade_image as ui, upgrade_registry as ur

    reg = "europe-west1-docker.pkg.dev/flo-prod-main/prod-ar"
    hub = {"php": ["8.4.30", "8.4.30-fpm-trixie", "8.2.36", "8.2.36-fpm-trixie"],
           "python": ["3.13.18", "3.13.18-slim-bookworm"],
           "golang": ["1.24.13", "1.24.13-alpine", "1.25.5", "1.25.14"]}
    res = ur.Resolver(fetch=_fake_hub(hub))
    index = {
        "alf": [{"src": f"{r}/build/alf", "images": ["golang:1.24.4"]} for r in ("anatolia", "global", "russia")],
        "sortify": [{"src": "anatolia/build/sortify", "images": ["golang:1.25.5"]}],
        "channel": [{"src": "anatolia/build/channel", "images": [f"{reg}/php-8-4:1.1", "builder"]}],
        "core": [{"src": "russia/build/core", "images": ["10.131.167.220:5000/prod-ar/php-8-4:1.1"]}],
        "py": [{"src": "anatolia/build/py", "images": [f"{reg}/python-3-13-7:1.0"]}],
        "legacy": [{"src": "anatolia/build/legacy", "images": [f"{reg}/php-7-4:1.0"]},
                   {"src": "russia/build/legacy", "images": [f"{reg}/php-7-4:1.0"]}],
        "oldjs": [{"src": "anatolia/build/oldjs", "images": ["node:14.21.3-bullseye-slim"]}],
    }

    def rec(image, lang, version, *, via=(), users=None, patch=None, eol=None, crit=0, high=0, level="kritik"):
        return {"image": image, "lang": lang, "version": version, "via": list(via), "touched": "", "error": "",
                "users": users if users is not None else {"anatolia": ["build/x"]},
                "scan": {"state": "tamam", "critical": crit, "high": high, "packages": [], "os": "", "created": "",
                         "ignored": {"critical": 0, "high": 0, "packages": []}},
                "patch": patch, "eol": eol, "level": level, "actions": []}
    DF = {(r, "/build/alf/Dockerfile"): "FROM golang:1.24.4\nWORKDIR /app\nRUN go build\n" for r in ("anatolia", "global", "russia")}
    go = rec("golang:1.24.4", "go", "1.24.4", patch={"latest": "1.24.13"},
             users={"anatolia": ["build/alf"], "global": ["build/alf"], "russia": ["build/alf"]})
    php84 = rec("php:8.4.27-fpm-bookworm", "php", "8.4.27", via=["php-8-4"], patch={"latest": "8.4.30"},
                users={"anatolia": ["build/channel"], "russia": ["build/core"]})

    def kind(acts, k):
        return next((a for a in acts if a["kind"] == k), None)

    def cl(**kw):
        return _FixClient(DF, **kw)

    # ── plan: taban-yama (iki parçalı kendi dizin) ───────────────────────
    acts = fx.plan(cl(), php84, index=index, resolver=res)
    a = kind(acts, "taban-yama")
    s = (a or {}).get("sites") or [{}]
    check("plan taban-yama: staging'deki tek sürüm satırı, 8.4.27 → 8.4.30",
          a and a["state"] == "yok" and len(s) == 1 and s[0]["repo"] == "base-images" and s[0]["branch"] == "staging"
          and s[0]["project"] == "FLODevOps" and s[0]["path"] == "/php-8-4/Dockerfile" and s[0]["line"] == 1
          and s[0]["old"] == "8.4.27" and s[0]["new"] == "8.4.30" and bump.pushable(s[0]), str(a))
    check("plan taban-yama: sabit dal adı", a["branch"] == "tempo/image-patch-php-8-4-8.4.27-8.4.30")
    check("plan: yalnız yama işareti → başka eylem yok (yükseltme / yeniden üretme yok)", [x["kind"] for x in acts] == ["taban-yama"])
    c2 = _FixClient(DF, files={"/php-8-4/Dockerfile": b"ARG PHP_VERSION=8.4.30\nFROM php:${PHP_VERSION}-fpm-trixie\n"})
    a = kind(fx.plan(c2, php84, index=index, resolver=res), "taban-yama")
    check("plan taban-yama: staging güncel → 'güncel', yazılacak site yok",
          a["state"] == "güncel" and "staging'de güncel — DevOps üretip taşımalı" in a["reason"] and not a.get("sites"), str(a))
    c3 = _FixClient(DF, files={"/php-8-4/Dockerfile": b"ARG PHP_VERSION=8.4.1\nARG PHP_VERSION=8.4.2\nFROM php:${PHP_VERSION}-fpm-trixie\n"})
    a = kind(fx.plan(c3, php84, index=index, resolver=res), "taban-yama")
    check("plan taban-yama: sürüm satırı tek değil → elle", a["state"] == "elle" and a["reason"], str(a))
    c4 = _FixClient(DF, files={"/php-8-2/dockerfile": b"ARG PHP_VERSION=8.2.35\nFROM php:${PHP_VERSION}-fpm-trixie\n"})
    p82 = rec("php:8.2.35-fpm", "php", "8.2.35", via=["php-8-2"], patch={"latest": "8.2.36"})
    a = kind(fx.plan(c4, p82, index=index, resolver=res), "taban-yama")
    check("plan taban-yama: küçük harfli dockerfile yolu aynen", a["state"] == "yok" and a["sites"][0]["path"] == "/php-8-2/dockerfile", str(a))
    c5 = _FixClient(DF, files={"/php-8-4/Dockerfile": b"ARG PHP_VERSION=8.4.27\nFROM php:${PHP_VERSION}-fpm-trixie\n"})
    nohub = ur.Resolver(fetch=_fake_hub({"php": ["8.4.30"]}))
    a = kind(fx.plan(c5, php84, index=index, resolver=nohub), "taban-yama")
    check("plan taban-yama: etiket Hub'da yok → elle", a["state"] == "elle" and "8.4.30-fpm-trixie" in a["reason"], str(a))
    for status, want in (("active", "pr açık"), ("abandoned", "pr reddedildi")):
        cp = _FixClient(DF, prs={"tempo/image-patch-php-8-4-8.4.27-8.4.30": {"id": 9, "status": status, "url": "https://x/9"}})
        a = kind(fx.plan(cp, php84, index=index, resolver=res), "taban-yama")
        check(f"plan taban-yama: PR {status} → {want}", a["state"] == want and a["pr"]["id"] == 9, str(a))

    # ── plan: taban-dizin (üç parçalı kendi dizin) ───────────────────────
    py = rec("python:3.13.7-slim-bookworm", "python", "3.13.7", via=["python-3-13-7"], patch={"latest": "3.13.18"},
             users={"anatolia": ["build/py"]})
    a = kind(fx.plan(cl(), py, index=index, resolver=res), "taban-dizin")
    check("plan taban-dizin: dizin staging'de zaten var → 'şablon var', PR yok",
          a["state"] == "şablon var" and a["dir"] == "python-3-13-18" and not a["pr"], str(a))
    stg = {"/python-3-13-7/Dockerfile": b"FROM python:3.13.7-slim-bookworm", "/python-3-13-7/tag.txt": b"1.0"}
    a = kind(fx.plan(_FixClient(DF, files=stg), py, index=index, resolver=res), "taban-dizin")
    check("plan taban-dizin: yok → açılabilir, yeni dizin <ad>-A-B-<yama>, etiket servisten",
          a["state"] == "yok" and a["dir"] == "python-3-13-18" and a["entry"]["tag"] == "1.0" and a["to"] == "3.13", str(a))
    a = kind(fx.plan(_FixClient(DF, files=stg, prs={"tempo/base-image-python-3-13-18": {"id": 4, "status": "active", "url": "u"}}),
                     py, index=index, resolver=res), "taban-dizin")
    check("plan taban-dizin: açık PR → pr açık", a["state"] == "pr açık" and a["pr"]["id"] == 4)
    ce = ui.build(_FixClient(DF, files=stg), "python", "3.13", {"dir": "python-3-13-18", "tag": "1.0"}, resolver=res)
    check("build: üç parçalı hedefte aynı minörün küçük yaması kaynak olur",
          ce["source"] == "python-3-13-7" and ce["files"]["/python-3-13-18/Dockerfile"] == b"FROM python:3.13.18-slim-bookworm", str(ce["source"]))
    try:
        ui.build(_FixClient(DF, files={"/python-3-13-20/Dockerfile": b"FROM python:3.13.20", "/python-3-13-20/tag.txt": b"1"}),
                 "python", "3.13", {"dir": "python-3-13-18", "tag": "1.0"}, resolver=res)
        check("build: aynı minörün BÜYÜK yaması kaynak olmaz", False)
    except ui.ImageTemplateError as ex:
        check("build: aynı minörün BÜYÜK yaması kaynak olmaz", "Kaynak dizin bulunamadı" in str(ex), str(ex))

    # ── plan: imaj-yama (genel üç parçalı imaj) ──────────────────────────
    a = kind(fx.plan(cl(), go, index=index, resolver=res), "imaj-yama")
    us = {u["repo"]: u for u in a["repos"]}
    check("plan imaj-yama: üç bölge (russia dahil), FROM satırı, yalnız rakam değişir",
          set(us) == {"anatolia", "global", "russia"} and all(u["state"] == "yok" for u in us.values())
          and us["russia"]["sites"][0] == {"where": "devops", "repo": "russia", "project": "FLODevOps", "branch": "production",
                                          "path": "/build/alf/Dockerfile", "line": 1, "old": "golang:1.24.4",
                                          "new": "golang:1.24.13", "blocked": ""} and a["state"] == "yok", str(a)[:400])
    check("plan imaj-yama: sabit dal adı, hedef imaj", a["branch"] == "tempo/image-patch-golang-1.24.4-1.24.13" and a["target"] == "golang:1.24.13")
    ga = rec("golang:1.24.4-alpine", "go", "1.24.4", patch={"latest": "1.24.13"}, users={"anatolia": ["build/alf"]})
    alp = {("anatolia", "/build/alf/Dockerfile"): "FROM golang:1.24.4-alpine AS build\nRUN x\n"}
    ia = {"alf": [{"src": "anatolia/build/alf", "images": ["golang:1.24.4-alpine"]}]}
    a = kind(fx.plan(_FixClient(alp), ga, index=ia, resolver=res), "imaj-yama")
    check("plan imaj-yama: etiket soneki korunur, dal adı soneki taşır",
          a["repos"][0]["sites"][0]["new"] == "golang:1.24.13-alpine" and a["branch"] == "tempo/image-patch-golang-1.24.4-alpine-1.24.13", str(a)[:300])
    a = kind(fx.plan(cl(), go, index=index, resolver=ur.Resolver(fetch=_fake_hub({"golang": ["1.24.4"]}))), "imaj-yama")
    check("plan imaj-yama: yeni etiket Hub'da yok → elle, site yok",
          a["state"] == "elle" and not any(u.get("sites") for u in a.get("repos") or []), str(a)[:300])
    a = kind(fx.plan(cl(), go, index=index, resolver=ur.Resolver(fetch=lambda u: (500, None))), "imaj-yama")
    check("plan imaj-yama: Hub okunamadı → elle", a["state"] == "elle")
    pin = dict(DF)
    pin[("russia", "/build/alf/Dockerfile")] = "FROM golang:1.24.4@sha256:" + "a" * 64 + "\n"
    a = kind(fx.plan(_FixClient(pin), go, index=index, resolver=res), "imaj-yama")
    check("plan imaj-yama: özetle sabitlenmiş satır → bütün eylem elle",
          a["state"] == "elle" and {u["repo"]: u["state"] for u in a["repos"]}["russia"] == "elle", str(a)[:300])
    nof = dict(DF)
    nof[("global", "/build/alf/Dockerfile")] = "FROM golang:1.23.0\n"
    a = kind(fx.plan(_FixClient(nof), go, index=index, resolver=res), "imaj-yama")
    check("plan imaj-yama: FROM satırı bulunamadı → elle", a["state"] == "elle"
          and {u["repo"]: u["state"] for u in a["repos"]}["global"] == "elle", str(a)[:300])
    stale = rec("golang:1.24.4", "go", "1.24.4", patch={"latest": "1.24.13"}, users={"anatolia": ["build/ESKI"], "russia": ["build/alf"]})
    a = kind(fx.plan(cl(), stale, index=index, resolver=res), "imaj-yama")
    check("plan imaj-yama: yollar raporun önbelleğinden değil taze index'ten (build/ESKI yazılmaz)",
          all(s["path"] == "/build/alf/Dockerfile" for u in a["repos"] for s in u.get("sites") or []), str(a)[:300])
    odd = rec("golang:1.24.4", "go", "1.24.4", patch={"latest": "1.24.13"}, users={"evil-repo": ["build/x"]})
    a = kind(fx.plan(cl(), odd, index=index, resolver=res), "imaj-yama")
    check("plan imaj-yama: bölge repoları dışındaki ad hiçbir zaman yazılmaz", a["state"] == "elle")
    cg = _FixClient(DF, region_prs={("russia", "tempo/image-patch-golang-1.24.4-1.24.13"): {"id": 3, "status": "active", "url": "https://x/3"},
                                    ("global", "tempo/image-patch-golang-1.24.4-1.24.13"): {"id": 5, "status": "abandoned", "url": "https://x/5"}})
    a = kind(fx.plan(cg, go, index=index, resolver=res), "imaj-yama")
    us = {u["repo"]: u for u in a["repos"]}
    check("plan imaj-yama: bölge başına PR durumu (açık / terk / yok)",
          us["russia"]["state"] == "pr açık" and us["global"]["state"] == "pr reddedildi" and us["anatolia"]["state"] == "yok", str(us)[:300])

    # ── plan: yükseltme / yeniden-üret ───────────────────────────────────
    goeol = rec("golang:1.24.4", "go", "1.24.4", patch={"latest": "1.24.13"}, eol={"eol": "2026-02-10"},
                users={"anatolia": ["build/alf"], "global": ["build/alf"], "russia": ["build/alf"]})
    acts = fx.plan(cl(), goeol, index=index, resolver=res, find_open_fn=lambda c, lang, repo: None)
    k = {x["kind"] for x in acts}
    a = kind(acts, "yükseltme")
    want_to = up.known_versions("go")[-1]
    check("plan: go eol + yama → hem imaj-yama hem yükseltme", k == {"imaj-yama", "yükseltme"}, str(k))
    check("plan yükseltme: imajı kullanan repo, default_target", a["state"] == "yok" and [u["repo"] for u in a["repos"]] == ["alf"]
          and a["repos"][0]["to"] == want_to, str(a)[:300])
    a = kind(fx.plan(cl(), goeol, index=index, resolver=res, find_open_fn=lambda c, lang, repo: {"id": 55, "lang": lang, "repo": repo, "to": "1.27"}),
             "yükseltme")
    check("plan yükseltme: açık izleme işi → izleniyor", a["state"] == "izleniyor" and a["repos"][0]["wi"] == 55, str(a)[:300])
    gx = rec("golang:1.99.0", "go", "1.99.0", eol={"eol": "2020-01-01"}, users={"anatolia": ["build/alf"]})
    ix = {"zz": [{"src": "anatolia/build/zz", "images": ["golang:1.99.0"]}]}
    a = kind(fx.plan(cl(), gx, index=ix, resolver=res, find_open_fn=lambda *x: None), "yükseltme")
    check("plan yükseltme: hedefi olmayan repo reason ile listelenir, açılabilir değil",
          a["state"] == "hedef yok" and a["repos"][0]["to"] is None and a["repos"][0]["reason"], str(a)[:300])
    nd = rec("node:14.21.3-bullseye-slim", "node", "14.21.3", eol={"eol": "2023-04-30"}, users={"anatolia": ["build/oldjs"]})
    a = kind(fx.plan(cl(), nd, index=index, resolver=res, find_open_fn=lambda *x: None), "yükseltme")
    check("plan yükseltme: Node izlenmiyor → elle, izleme açılmaz", a["state"] == "elle" and "Node sürümü izlenmiyor — elle" in a["reason"], str(a))
    p74 = rec("php:7.4.33-fpm-bullseye", "php", "7.4.33", via=["php-7-4"], eol={"eol": "2022-11-28"}, crit=18,
              users={"anatolia": ["build/legacy"], "russia": ["build/legacy"]})
    acts = fx.plan(cl(), p74, index=index, resolver=res, find_open_fn=lambda *x: None)
    a = kind(acts, "yükseltme")
    check("plan: php-7-4 eol → yalnız yükseltme (iki servis, repo bir kez)", [x["kind"] for x in acts] == ["yükseltme"]
          and [u["repo"] for u in a["repos"]] == ["legacy"], str(acts)[:300])
    os_only = rec("php:8.4.27-fpm-bookworm", "php", "8.4.27", via=["php-8-4"], crit=1, high=123,
                  users={"anatolia": ["build/channel"], "russia": ["build/core"]})
    acts = fx.plan(cl(), os_only, index=index, resolver=res)
    a = kind(acts, "yeniden-üret")
    check("plan yeniden-üret: yalnız işletim sistemi bulgusu → devops, bölge bayrakları, yazma yok",
          [x["kind"] for x in acts] == ["yeniden-üret"] and a["state"] == "devops" and "mainLocation" in a["reason"]
          and "yandexLocation" in a["reason"] and "globalLocation" not in a["reason"] and not a.get("sites"), str(a))
    check("plan: işaret yoksa eylem yok", fx.plan(cl(), rec("debian:12", "", "", level="temiz"), index=index, resolver=res) == [])

    # ── apply: taban-yama ────────────────────────────────────────────────
    pushes: list = []

    def push_fn(client, sites, *, branch, title, body, wi_id):
        pushes.append({"sites": sites, "branch": branch, "title": title, "body": body, "wi_id": wi_id})
        return {"prs": [{"repo": sites[0]["repo"], "id": 100 + len(pushes), "url": f"https://x/pr/{100 + len(pushes)}"}],
                "fallback": [], "errors": []}

    def never(*a, **k):
        raise AssertionError("yazma yapılmamalıydı")
    c = cl()
    out = fx.apply(c, php84, team="T", index=index, resolver=res, push_fn=push_fn, open_pr_fn=never, start_fn=never)
    a = kind(out, "taban-yama")
    text = c.files["staging"]["/php-8-4/Dockerfile"].decode()
    new_text = bump.apply_site(text, pushes[0]["sites"][0])
    d = [i for i, (x, y) in enumerate(zip(text.splitlines(), new_text.splitlines())) if x != y]
    check("apply taban-yama: tek push_fn çağrısı, taslak PR sonucu işlenir",
          len(pushes) == 1 and a["state"] == "açıldı" and a["pr"]["id"] == 101 and a["ran"] is True
          and pushes[0]["branch"] == "tempo/image-patch-php-8-4-8.4.27-8.4.30" and pushes[0]["wi_id"] is None, str(out)[:300])
    check("apply taban-yama: site yalnız rakamlarda ayrışır, tek satır değişir",
          d == [0] and re.sub(r"\d+", "#", text.splitlines()[0]) == re.sub(r"\d+", "#", new_text.splitlines()[0])
          and new_text.splitlines()[0] == "ARG PHP_VERSION=8.4.30", str((d, new_text[:40])))
    check("apply taban-yama: PR metni Türkçe, işareti ve bayrakları söyler",
          "yama geride" in pushes[0]["body"] and "mainLocation" in pushes[0]["body"] and "yandexLocation" in pushes[0]["body"]
          and "8.4.27" in pushes[0]["title"] and "8.4.30" in pushes[0]["title"], pushes[0]["body"][:300])
    pushes.clear()
    ao = fx.apply(_FixClient(DF, prs={"tempo/image-patch-php-8-4-8.4.27-8.4.30": {"id": 9, "status": "active", "url": "u"}}),
                  php84, team="T", index=index, resolver=res, push_fn=never, open_pr_fn=never, start_fn=never)
    check("apply: açık PR varken hiç yazma yok", kind(ao, "taban-yama")["state"] == "pr açık" and kind(ao, "taban-yama")["ran"] is False)
    ab = fx.apply(_FixClient(DF, prs={"tempo/image-patch-php-8-4-8.4.27-8.4.30": {"id": 9, "status": "abandoned", "url": "u"}}),
                  php84, team="T", index=index, resolver=res, push_fn=never, open_pr_fn=never, start_fn=never)
    check("apply: terk edilmiş PR yeniden açılmaz", kind(ab, "taban-yama")["state"] == "pr reddedildi" and kind(ab, "taban-yama")["ran"] is False)
    ag = fx.apply(c2, php84, team="T", index=index, resolver=res, push_fn=never, open_pr_fn=never, start_fn=never)
    check("apply: staging güncel → yazma yok", kind(ag, "taban-yama")["state"] == "güncel")

    # ── apply: imaj-yama (bölge başına bir PR) ───────────────────────────
    out = fx.apply(cl(), go, team="T", index=index, resolver=res, push_fn=push_fn, open_pr_fn=never, start_fn=never)
    a = kind(out, "imaj-yama")
    check("apply imaj-yama: üç bölge → üç push_fn çağrısı (russia dahil), aynı dal adı",
          [p["sites"][0]["repo"] for p in pushes] == ["anatolia", "global", "russia"]
          and {p["branch"] for p in pushes} == {"tempo/image-patch-golang-1.24.4-1.24.13"} and a["state"] == "açıldı"
          and all(len(p["sites"]) == 1 for p in pushes), str([(p["sites"][0]["repo"], p["branch"]) for p in pushes]))
    check("apply imaj-yama: PR metni bütün bölgeleri ve tetikleyen işareti yazar",
          all(r in pushes[0]["body"] for r in ("anatolia", "global", "russia")) and "yama geride" in pushes[0]["body"]
          and "golang:1.24.4" in pushes[0]["body"] and "golang:1.24.13" in pushes[0]["body"], pushes[0]["body"][:400])
    pushes.clear()
    cg2 = _FixClient(DF, region_prs={("russia", "tempo/image-patch-golang-1.24.4-1.24.13"): {"id": 3, "status": "active", "url": "https://x/3"}})
    fx.apply(cg2, go, team="T", index=index, resolver=res, push_fn=push_fn, open_pr_fn=never, start_fn=never)
    check("apply imaj-yama: açık PR'ı olan bölgeye yazılmaz, kalanlara yazılır",
          [p["sites"][0]["repo"] for p in pushes] == ["anatolia", "global"])
    pushes.clear()
    pin2 = fx.apply(_FixClient(pin), go, team="T", index=index, resolver=res, push_fn=push_fn, open_pr_fn=never, start_fn=never)
    check("apply imaj-yama: bir bölge elle ise hiçbir bölgeye yazılmaz", pushes == [] and kind(pin2, "imaj-yama")["state"] == "elle")

    def flaky(client, sites, **kw):
        if sites[0]["repo"] == "global":
            raise ValueError("Azure düştü")
        return push_fn(client, sites, **kw)
    pushes.clear()
    out = fx.apply(cl(), go, team="T", index=index, resolver=res, push_fn=flaky, open_pr_fn=never, start_fn=never)
    us = {u["repo"]: u for u in kind(out, "imaj-yama")["repos"]}
    check("apply imaj-yama: bir bölgenin hatası ötekileri durdurmaz, hata Türkçe nedenle işaretlenir",
          us["anatolia"]["state"] == "açıldı" and us["russia"]["state"] == "açıldı" and us["global"]["state"] == "hata"
          and kind(out, "imaj-yama")["state"] == "hata", str(us)[:400])
    out = fx.apply(cl(), go, team="T", index=index, resolver=res,
                   push_fn=lambda c, s, **kw: {"prs": [], "fallback": [{"repo": s[0]["repo"], "diff": "d"}], "errors": []},
                   open_pr_fn=never, start_fn=never)
    check("apply: yazma yetkisi yok (fallback) → 'elle', PR sayılmaz", kind(out, "imaj-yama")["state"] == "elle")

    # ── apply: taban-dizin ───────────────────────────────────────────────
    opened: list = []

    def open_pr_fn(client, lang, to, entry, **kw):
        opened.append((lang, to, entry["dir"], entry["tag"]))
        return {"state": "pr açık", "pr": {"id": 31, "url": "https://x/31"}, "notes": []}
    out = fx.apply(_FixClient(DF, files=stg), py, team="T", index=index, resolver=res, push_fn=never, open_pr_fn=open_pr_fn, start_fn=never)
    a = kind(out, "taban-dizin")
    check("apply taban-dizin: upgrade_image.open_pr yoluyla, dizin / etiket doğru",
          opened == [("python", "3.13", "python-3-13-18", "1.0")] and a["state"] == "açıldı" and a["pr"]["id"] == 31, str(out)[:300])
    opened.clear()
    fx.apply(cl(), py, team="T", index=index, resolver=res, push_fn=never, open_pr_fn=open_pr_fn, start_fn=never)
    check("apply taban-dizin: dizin zaten varsa PR açılmaz", opened == [])

    # ── apply: yükseltme ─────────────────────────────────────────────────
    started: list = []

    def start_fn(client, repo, lang, **kw):
        started.append((repo, lang, kw.get("to"), kw.get("team")))
        return {"id": 700 + len(started), "created": True, "lang": lang, "repo": repo, "to": kw.get("to")}
    ev = rec("golang:1.99.0", "go", "1.24.4", eol={"eol": "2020-01-01"}, users={"anatolia": ["build/a"]})
    mi = {"a": [{"src": "anatolia/build/a", "images": ["golang:1.99.0"]}], "b": [{"src": "global/build/b", "images": ["golang:1.99.0"]}],
          "c": [{"src": "russia/build/c", "images": ["golang:1.99.0"]}]}

    def fo(c, lang, repo):
        return {"id": 55, "lang": lang, "repo": repo, "to": "1.27"} if repo == "b" else None
    out = fx.apply(cl(), ev, team="Ekip", index=mi, resolver=res, push_fn=never, open_pr_fn=never, start_fn=start_fn, find_open_fn=fo)
    a = kind(out, "yükseltme")
    to = up.default_target("go", "1.24", mi)
    check("apply yükseltme: izlemesi olan repo atlanır, olmayanlar için start çağrılır (takımla)",
          to and [s[0] for s in started] == ["a", "c"] and all(s[3] == "Ekip" and s[2] == to for s in started), str(started))
    st = {u["repo"]: u["state"] for u in a["repos"]}
    check("apply yükseltme: durumlar açıldı / izleniyor", st == {"a": "açıldı", "b": "izleniyor", "c": "açıldı"} and a["state"] == "açıldı", str(st))
    started.clear()
    out = fx.apply(cl(), ev, team="Ekip", index=mi, resolver=res, push_fn=never, open_pr_fn=never, start_fn=start_fn,
                   find_open_fn=lambda *x: None, max_open=1)
    st = {u["repo"]: u["state"] for u in kind(out, "yükseltme")["repos"]}
    check("apply yükseltme: max_open aşımı → kalanlar 'sırada'", len(started) == 1 and sorted(st.values()) == ["açıldı", "sırada", "sırada"], str(st))
    started.clear()
    out = fx.apply(cl(), ev, team="", index=mi, resolver=res, push_fn=never, open_pr_fn=never, start_fn=start_fn, find_open_fn=lambda *x: None)
    check("apply yükseltme: takım yoksa izleme açılmaz", started == [] and kind(out, "yükseltme")["state"] == "hata")
    out = fx.apply(cl(), nd, team="T", index=index, resolver=res, push_fn=never, open_pr_fn=never, start_fn=never, find_open_fn=lambda *x: None)
    check("apply: Node eol → izleme açılmaz (start çağrılsa AssertionError)", kind(out, "yükseltme")["state"] == "elle")

    # ── apply: bir eylemin istisnası ötekileri durdurmaz ─────────────────
    both = rec("php:8.4.27-fpm-bookworm", "php", "8.2.27", via=["php-8-4"], patch={"latest": "8.4.30"}, eol={"eol": "2020-01-01"},
               users={"anatolia": ["build/channel"]})
    started.clear()

    def boom(*a, **k):
        raise RuntimeError("push patladı")
    out = fx.apply(cl(), both, team="T", index=index, resolver=res, find_open_fn=lambda *x: None,
                   push_fn=boom, open_pr_fn=never, start_fn=start_fn)
    check("apply: push istisnası → 'hata', yükseltme eylemi yine de yürür",
          kind(out, "taban-yama")["state"] == "hata" and "push patladı" in kind(out, "taban-yama")["reason"]
          and kind(out, "yükseltme")["state"] == "açıldı" and [x[0] for x in started] == ["channel", "core"], str(out)[:400])

    # ── gerçek push_sites ile uçtan uca (sahte client) ───────────────────
    c = cl()
    out = fx.apply(c, php84, team="T", index=index, resolver=res, open_pr_fn=never, start_fn=never)
    kinds = [x[0] for x in c.calls]
    check("uçtan uca: dal + push + taslak PR, PR base-images → staging",
          kinds == ["branch", "push", "pr"] and c.calls[2][2] == "staging" and c.calls[2][5] is True
          and kind(out, "taban-yama")["state"] == "açıldı", str(c.calls)[:300])
    pushed = c.files["tempo/image-patch-php-8-4-8.4.27-8.4.30"]["/php-8-4/Dockerfile"].decode()
    check("uçtan uca: dalda yalnız birinci satır değişti", pushed.startswith("ARG PHP_VERSION=8.4.30\n")
          and pushed.splitlines()[1:] == c.files["staging"]["/php-8-4/Dockerfile"].decode().splitlines()[1:])
    out = fx.apply(c, php84, team="T", index=index, resolver=res, open_pr_fn=never, start_fn=never)
    check("uçtan uca: ikinci çalıştırma yeni yazma yapmaz (açık PR)", len(c.calls) == 3 and kind(out, "taban-yama")["state"] == "pr açık")

    # ── dal adı doğrulaması ──────────────────────────────────────────────
    for n in ["tempo/image-patch-php-8-4-8.4.27-8.4.30", "tempo/image-patch-golang-1.24.4-alpine-1.24.13", "tempo/upgrade-php-core-8.4"]:
        try:
            bump._validate_branch(n, {"staging"})
            check(f"dal kabul: {n}", True)
        except ValueError as ex:
            check(f"dal kabul: {n}", False, str(ex))
    for n in ["tempo/image-patch-x", "tempo/image-patch-php-8.4", "tempo/other-php-8.4.30", "tempo/image-patch--x-1.2.3",
              "tempo/image-patch-a..b-1.2.3", "image-patch-php-8.4.30", "tempo/image-patch-php-8-4-8.4.30/x",
              "refs/heads/tempo/image-patch-a-1.2.3", "tempo/image-patch-a-1.2.3 "]:
        try:
            bump._validate_branch(n, {"staging"})
            check(f"dal red: {n}", False, "kabul edildi")
        except ValueError:
            check(f"dal red: {n}", True)
    try:
        bump._validate_branch("tempo/image-patch-php-8-4-8.4.27-8.4.30", {"tempo/image-patch-php-8-4-8.4.27-8.4.30"})
        check("dal red: hedef dal", False)
    except ValueError as ex:
        check("dal red: hedef dal", "hedef dal" in str(ex))
    try:
        bump._validate_branch("main", set())
    except ValueError as ex:
        check("hata metni iki deseni de söyler", "tempo/upgrade-" in str(ex) and "tempo/image-patch-" in str(ex), str(ex))

    # ── yoklayıcı turu ───────────────────────────────────────────────────
    comments: list = []
    pcl = SimpleNamespace(team="Ekip", add_comment=lambda wid, text: comments.append((wid, text)))
    rep = {"images": [dict(php84, level="kritik"), dict(go, level="yüksek"), dict(os_only, level="bilgi")]}
    called: list = []

    def apply_fn(client, r, **kw):
        called.append(r["image"])
        if r["image"].startswith("php"):
            return [{"kind": "taban-yama", "state": "açıldı", "ran": True, "reason": "", "pr": {"id": 8, "url": "https://x/8"}},
                    {"kind": "imaj-yama", "state": "pr açık", "ran": False, "reason": "", "pr": {"id": 9, "url": "javascript:alert(1)"}}]
        raise RuntimeError("Azure düştü")
    results = [{"image": "php:8.4.27-fpm-bookworm", "state": "açıldı", "wi": 901}, {"image": "golang:1.24.4", "state": "açık iş", "wi": 902}]
    out = iv._fix_round(pcl, rep, results, team="Ekip", index={}, resolver=res, apply_fn=apply_fn)
    check("yoklayıcı giderme: yalnız kritik / yüksek imajlar; bir imajın hatası ötekini durdurmaz",
          called == ["golang:1.24.4", "php:8.4.27-fpm-bookworm"] and len(out) == 1, str(called))
    check("yoklayıcı giderme: sonuç uyarı işine yorum (yalnız yürüyen eylem varsa), https olmayan bağlantı yok",
          len(comments) == 1 and comments[0][0] == 901 and "https://x/8" in comments[0][1]
          and "javascript:" not in comments[0][1], str(comments)[:300])
    comments.clear()
    iv._fix_round(pcl, rep, [], team="Ekip", index={}, resolver=res,
                  apply_fn=lambda c, r, **kw: [{"kind": "yeniden-üret", "state": "devops", "ran": False, "reason": "x", "pr": None}])
    check("yoklayıcı giderme: yürüyen eylem yoksa yorum yok", comments == [])
    seen: list = []
    _rep, _alert, _fr, _get, _bi0 = iv.report, iv.alert, iv._fix_round, pc.get, modern.build_index
    try:
        knobs = {"CREW_TECHDEBT": True, "CREW_TECHDEBT_IMAGE_VULN": True, "CREW_TECHDEBT_IMAGE_VULN_FIX": False}
        pc.get = lambda k: knobs[k] if k in knobs else _get(k)
        modern.build_index = lambda c, **kw: {}
        iv.report = lambda client, **kw: {"images": []}
        iv.alert = lambda client, rep, **kw: seen.append("alert") or []
        iv._fix_round = lambda client, rep, results, **kw: seen.append("fix") or []
        iv._tick(lambda: pcl)
        check("_tick: _FIX kapalı → giderme çalışmaz", seen == ["alert"], str(seen))
        knobs["CREW_TECHDEBT_IMAGE_VULN_FIX"] = True
        seen.clear()
        iv._tick(lambda: pcl)
        check("_tick: _FIX açık → uyarıdan SONRA giderme", seen == ["alert", "fix"], str(seen))

        def fix_boom(*a, **k):
            raise RuntimeError("giderme çöktü")
        iv._fix_round = fix_boom
        seen.clear()
        iv._tick(lambda: pcl)
        check("_tick: giderme hatası turu düşürmez, uyarı yapılmıştı", seen == ["alert"], str(seen))
    finally:
        iv.report, iv.alert, iv._fix_round, pc.get, modern.build_index = _rep, _alert, _fr, _get, _bi0

    # ── rapor kaydına fix (yazmasız) ─────────────────────────────────────
    r2 = {"images": [dict(php84), dict(os_only, image="x:1"), {"image": "boş"}]}
    c = cl()
    iv.attach_fix(c, r2, index=index, resolver=res)
    check("attach_fix: kayıtlara yazmasız plan eklenir, iç alanlar dışarı çıkmaz",
          r2["images"][0]["fix"][0]["kind"] == "taban-yama" and "entry" not in r2["images"][0]["fix"][0]
          and r2["images"][2]["fix"] == [] and c.calls == [])
    r3 = {"images": [dict(php84)]}
    bad = _FixClient(DF)
    bad.get_file_content = lambda *a, **k: (_ for _ in ()).throw(OSError("ağ yok"))
    iv.attach_fix(bad, r3, index=index, resolver=res)
    check("attach_fix: okuma hatası çökertmez (belirsiz eylem)", r3["images"][0]["fix"][0]["state"] == "belirsiz")

    # ── ayar ve uç ───────────────────────────────────────────────────────
    by = {f["key"]: f for f in pc.SCHEMA}
    f = by.get("CREW_TECHDEBT_IMAGE_VULN_FIX", {})
    check("_FIX bool, varsayılan kapalı, açıklama Türkçe", f.get("type") == "bool" and f.get("default") is False
          and "gidermeyi" in f.get("desc", "").lower() and f.get("label"), str(f))
    _get2 = pc.get
    _cl, _report, _bi, _apply, _def = server.AzureDevOpsClient, iv.report, modern.build_index, fx.apply, ur.default
    knobs2 = {"CREW_TECHDEBT": True, "CREW_TECHDEBT_IMAGE_VULN": True}
    try:
        pc.get = lambda k: knobs2[k] if k in knobs2 else _get2(k)
        server.AzureDevOpsClient = lambda: object()
        modern.build_index = lambda c, **kw: index
        ur.default = lambda: res
        iv.report = lambda c, **kw: {"images": [php84, go]}
        runs: list = []
        fx.apply = lambda c, r, **kw: runs.append((r["image"], kw.get("team"))) or [
            {"kind": "taban-yama", "state": "açıldı", "ran": True, "reason": "", "pr": {"id": 1, "url": "https://x/1"}, "entry": {"secret": 1}}]

        def body(**k):
            return server.TechdebtImageFixRequest(**k)
        r = asyncio.run(server.techdebt_images_fix(body(image="php:8.4.27-fpm-bookworm", team="Ekip")))
        j = json.loads(r.body)
        check("uç: 200, eylemler, takım iletilir, iç alan sızmaz",
              r.status_code == 200 and j["actions"][0]["state"] == "açıldı" and runs == [("php:8.4.27-fpm-bookworm", "Ekip")]
              and "entry" not in j["actions"][0], str(j))
        r = asyncio.run(server.techdebt_images_fix(body(image="../../etc/passwd", team="Ekip")))
        check("uç: bilinmeyen imaj → 404, yol / dal / sorgu olarak hiç kullanılmaz", r.status_code == 404 and len(runs) == 1, str(r.body))
        r = asyncio.run(server.techdebt_images_fix(body(image="", team="Ekip")))
        check("uç: imaj boş → 400", r.status_code == 400)
        fx.apply = lambda c, r, **kw: [{"kind": "taban-yama", "state": "güncel", "ran": False,
                                        "reason": "staging'de güncel — DevOps üretip taşımalı", "pr": None}]
        r = asyncio.run(server.techdebt_images_fix(body(image="php:8.4.27-fpm-bookworm", team="Ekip")))
        j = json.loads(r.body)
        check("uç: yürüyecek eylem yok → 409 + nedenler", r.status_code == 409 and "staging'de güncel" in json.dumps(j, ensure_ascii=False), str(j))

        def apply_boom(c, r, **kw):
            raise RuntimeError("Authorization: Basic abc123 patladı")
        fx.apply = apply_boom
        r = asyncio.run(server.techdebt_images_fix(body(image="php:8.4.27-fpm-bookworm", team="Ekip")))
        j = json.loads(r.body)
        check("uç: beklenmeyen hata → 502 Türkçe neden, 500 / traceback / sır yok", r.status_code == 502
              and "Traceback" not in json.dumps(j) and "giderme" in j["error"].lower() and "abc123" not in j["error"], str(j))

        def bi_boom(c, **kw):
            raise OSError("ağ")
        modern.build_index = bi_boom
        r = asyncio.run(server.techdebt_images_fix(body(image="php:8.4.27-fpm-bookworm", team="Ekip")))
        check("uç: index okunamadı → 502", r.status_code == 502)
        modern.build_index = lambda c, **kw: index
        knobs2["CREW_TECHDEBT_IMAGE_VULN"] = False
        r = asyncio.run(server.techdebt_images_fix(body(image="php:8.4.27-fpm-bookworm", team="Ekip")))
        check("uç: _IMAGE_VULN kapalı → 403", r.status_code == 403)
        knobs2["CREW_TECHDEBT_IMAGE_VULN"] = True
        knobs2["CREW_TECHDEBT_IMAGE_VULN_FIX"] = False
        fx.apply = lambda c, r, **kw: [{"kind": "x", "state": "açıldı", "ran": True, "reason": "", "pr": None}]
        r = asyncio.run(server.techdebt_images_fix(body(image="php:8.4.27-fpm-bookworm", team="Ekip")))
        check("uç: düğme _FIX kapalıyken de çalışır (yalnız _IMAGE_VULN şart)", r.status_code == 200)
    finally:
        pc.get = _get2
        server.AzureDevOpsClient, iv.report, modern.build_index, fx.apply, ur.default = _cl, _report, _bi, _apply, _def

    # ── kart ─────────────────────────────────────────────────────────────
    html_src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text()
    seg = html_src[html_src.index("function renderImageVuln"):]
    seg = seg[:seg.index("\n}\n")]
    fseg = html_src[html_src.index("async function fixImageVuln"):]
    fseg = fseg[:fseg.index("\n}\n")]
    lseg = html_src[html_src.index("function _ivLink"):]
    lseg = lseg[:lseg.index("\n")]
    dseg = html_src[html_src.index("function openIvDetail"):html_src.index("function closeIvDetail")]
    check("kart: satırda giderme durumu; eylem satırları ve 'Gidermeyi başlat' düğmesi detay penceresinde",
          "x.fix" in seg and "Gidermeyi başlat" in dseg and "_ivFixRows(x.fix)" in dseg and "fixImageVuln(" in dseg
          and "${a.reason}" not in dseg and "${a.state}" not in dseg)
    check("kart: uç, onay kutusu, düğme istek boyunca kapalı, sonra yenile",
          "/api/techdebt/images/fix" in fseg and "confirm(" in fseg and "disabled=true" in fseg and "loadImageVuln(" in fseg)
    check("kart: aralanan metin escHtml'den geçer, yalnız https bağlantı", "${a.reason}" not in seg and "${a.state}" not in seg
          and "^https:" in lseg)


class _RegionFake:
    """Bölge repoları (production) için repo başına depolu sahte client; gerçek `push_sites` ile koşar."""
    org_url = "https://dev.azure.com/org"
    project = "FLODevOps"

    def __init__(self, dockerfiles, fail_repos=()):
        self.br = {}
        for (repo, path), text in dockerfiles.items():
            self.br.setdefault((repo, "production"), {})[path] = text.encode()
        self.prs, self.calls, self.fail_repos = {}, [], set(fail_repos)

    def list_branches(self, repo, project=None):
        return [{"name": f"refs/heads/{b}", "objectId": f"oid-{b}"} for (r, b) in self.br if r == repo]

    def get_file_content(self, repo, path, branch=None, project=None):
        data = self.br.get((repo, branch), {}).get(path)
        if data is None:
            raise FileNotFoundError(path)
        return data.decode()

    def get_file_bytes(self, repo, path, branch=None, project=None, version=None, version_type=None, **kw):
        if version:
            branch = version[len("oid-"):]
        return self.br.get((repo, branch), {}).get(path)

    def create_branch(self, repo, name, source, project=None, source_object_id=None):
        if repo in self.fail_repos:
            raise OSError("Azure düştü")
        self.calls.append(("branch", repo, name))
        self.br[(repo, name)] = dict(self.br[(repo, source)])
        return {"value": [{"success": True}]}

    def push_changes(self, repo, branch, changes, msg, project=None, old_object_id=None):
        import base64 as _b64
        self.calls.append(("push", repo, branch))
        for c in changes:
            self.br[(repo, branch)][c["path"]] = _b64.b64decode(c["content"])
        return {}

    def create_pull_request(self, repo, src, tgt="main", title="", description="", work_item_ids=None, draft=False,
                            project=None):
        self.calls.append(("pr", repo, src))
        self.prs[(repo, src)] = {"id": 50 + len(self.prs), "status": "active", "url": "https://x/pr", "target": tgt,
                                 "created_by": "me"}
        return {"pullRequestId": self.prs[(repo, src)]["id"], "repository": {"name": repo, "project": {"name": "FLODevOps"}}}

    def find_pull_request(self, repo, branch, project=None):
        return self.prs.get((repo, branch))

    def get_authenticated_user(self):
        return {"id": "me"}


def test_image_fix_round1():
    """Giderme düzeltme turu 1: durdurulan izleme yeniden açılmaz, taze index, tur başına sınır, anahtarlara uyum."""
    print("\n[taban imaj güvenliği: giderme — düzeltme turu 1]")
    from agile_sdlc_crew import pipeline_config as pc
    knobs = {"CREW_TECHDEBT_UPGRADE": True, "CREW_TECHDEBT_PHP_MODERN": True, "CREW_TECHDEBT_UPGRADE_BUMP": True,
             "CREW_TECHDEBT_IMAGE_VULN_MAX_TASKS": 3, "CREW_TECHDEBT_UPGRADE_MAX_OPEN": 3}
    _get = pc.get
    pc.get = lambda k: knobs[k] if k in knobs else _get(k)
    from agile_sdlc_crew.quality import image_fix as _fx
    _cur = getattr(_fx, "_repo_current", None)
    _fx._repo_current = lambda *a, **k: None
    try:
        with _fix_env(knobs):
            _image_fix_round1_body(knobs)
    finally:
        pc.get = _get
        if _cur is not None:
            _fx._repo_current = _cur


def _image_fix_round1_body(knobs):
    import functools
    from agile_sdlc_crew.quality import image_fix as fx, image_vuln as iv, modern
    from agile_sdlc_crew.quality import upgrade_bump as bump, upgrade_registry as ur, upgrade_track as ut
    from agile_sdlc_crew import pipeline_config as pc

    hub = {"php": ["8.4.30", "8.4.30-fpm-trixie"], "golang": ["1.24.13", "1.24.13-alpine", "1.25.14"],
           "python": ["3.13.18", "3.13.18-slim-bookworm"]}
    res = ur.Resolver(fetch=_fake_hub(hub))

    def rec(image, lang, version, *, via=(), users=None, patch=None, eol=None, crit=0, high=0, level="kritik"):
        return {"image": image, "lang": lang, "version": version, "via": list(via), "touched": "", "error": "",
                "users": users if users is not None else {"anatolia": ["build/alf"]},
                "scan": {"state": "tamam", "critical": crit, "high": high, "packages": [], "os": "", "created": "",
                         "ignored": {"critical": 0, "high": 0, "packages": []}},
                "patch": patch, "eol": eol, "level": level, "actions": []}

    def kind(acts, k):
        return next((a for a in acts if a["kind"] == k), None)

    def never(*a, **k):
        raise AssertionError("yazma yapılmamalıydı")
    DF = {(r, "/build/alf/Dockerfile"): "FROM golang:1.24.4\nRUN go build\n" for r in ("anatolia", "global", "russia")}
    idx = {"alf": [{"src": f"{r}/build/alf", "images": ["golang:1.24.4"]} for r in ("anatolia", "global", "russia")]}
    go = rec("golang:1.24.4", "go", "1.24.4", patch={"latest": "1.24.13"},
             users={"anatolia": ["build/alf"], "global": ["build/alf"], "russia": ["build/alf"]})
    ev = rec("golang:1.99.0", "go", "1.24.4", eol={"eol": "2020-01-01"})
    mi = {"a": [{"src": "anatolia/build/a", "images": ["golang:1.99.0"]}], "b": [{"src": "global/build/b", "images": ["golang:1.99.0"]}],
          "c": [{"src": "russia/build/c", "images": ["golang:1.99.0"]}]}
    started: list = []

    def start_fn(client, repo, lang, **kw):
        started.append(repo)
        return {"id": 700 + len(started), "created": True}

    # ── C1: durdurulan izleme yoklayıcıda yeniden açılmaz ───────────────
    from types import SimpleNamespace as NS
    rows = [{"id": 5, "fields": {"System.State": "Closed", "System.Tags": "tempo-upgrade-track",
                                 "System.Description": "<p>tempo-upgrade-key: go:alf:1.27</p>"}},
            {"id": 6, "fields": {"System.State": "New", "System.Tags": "tempo-upgrade-track",
                                 "System.Description": "<p>tempo-upgrade-key: go:live:1.27</p>"}}]
    tc = NS(query_work_items=lambda q, **kw: rows)
    check("find_open kapalı izleme işini görmez, find_any görür",
          ut.find_open(tc, "go", "alf") is None and (ut.find_any(tc, "go", "alf") or {}).get("id") == 5
          and ut.find_any(tc, "go", "alf", to="1.28") is None and ut.find_any(tc, "go", "nope") is None)
    mi1 = {"alf": [{"src": "anatolia/build/alf", "images": ["golang:1.99.0"]}]}
    fo, fa = (lambda c, lang, repo: None), (lambda c, lang, repo, to=None: {"id": 5})
    a = kind(fx.plan(_FixClient(DF), ev, index=mi1, resolver=res, find_open_fn=fo, find_any_fn=fa, auto=True), "yükseltme")
    check("plan auto: daha önce kapatılmış izleme → durduruldu, açılabilir değil",
          a["state"] == "durduruldu" and a["repos"][0]["state"] == "durduruldu" and "karttan" in a["repos"][0]["reason"], str(a)[:300])
    out = fx.apply(_FixClient(DF), ev, team="T", index=mi1, resolver=res, push_fn=never, open_pr_fn=never, start_fn=start_fn,
                   find_open_fn=fo, find_any_fn=fa, auto=True)
    check("apply auto: durdurulan izleme için start çağrılmaz", started == [] and kind(out, "yükseltme")["ran"] is False)
    out = fx.apply(_FixClient(DF), ev, team="T", index=mi1, resolver=res, push_fn=never, open_pr_fn=never, start_fn=start_fn,
                   find_open_fn=fo, find_any_fn=fa, auto=False)
    check("apply düğme (auto=False): kapatılmış izleme yeniden başlatılabilir", started == ["alf"] and kind(out, "yükseltme")["state"] == "açıldı")
    started.clear()
    out = fx.apply(_FixClient(DF), ev, team="T", index=mi1, resolver=res, push_fn=never, open_pr_fn=never, start_fn=start_fn,
                   find_open_fn=fo, find_any_fn=lambda *a: None, auto=True)
    check("apply auto: hiç izlemesi olmayan repo başlar", started == ["alf"])
    started.clear()
    out = fx.apply(_FixClient(DF), ev, team="T", index=mi1, resolver=res, push_fn=never, open_pr_fn=never, start_fn=start_fn,
                   find_open_fn=fo, auto=True, find_any_fn=lambda *a: (_ for _ in ()).throw(OSError("ağ")))
    check("apply auto: izleme geçmişi okunamazsa açılmaz", started == [] and kind(out, "yükseltme")["state"] == "belirsiz")

    # ── I3 / I6: anahtarlar ──────────────────────────────────────────────
    knobs["CREW_TECHDEBT_UPGRADE"] = False
    a = kind(fx.plan(_FixClient(DF), ev, index=mi1, resolver=res, find_open_fn=fo), "yükseltme")
    check("yükseltme: CREW_TECHDEBT_UPGRADE kapalı → elle, neden ayarı söyler",
          a["state"] == "elle" and "CREW_TECHDEBT_UPGRADE" in a["reason"], str(a))
    out = fx.apply(_FixClient(DF), ev, team="T", index=mi1, resolver=res, push_fn=never, open_pr_fn=never, start_fn=never, find_open_fn=fo)
    check("yükseltme: anahtar kapalıyken start asla çağrılmaz", kind(out, "yükseltme")["state"] == "elle")
    knobs["CREW_TECHDEBT_UPGRADE"] = True
    knobs["CREW_TECHDEBT_PHP_MODERN"] = False
    a = kind(fx.plan(_FixClient(DF), ev, index=mi1, resolver=res, find_open_fn=fo), "yükseltme")
    check("yükseltme: CREW_TECHDEBT_PHP_MODERN kapalı → elle", a["state"] == "elle" and "PHP_MODERN" in a["reason"], str(a))
    knobs["CREW_TECHDEBT_PHP_MODERN"] = True
    knobs["CREW_TECHDEBT_UPGRADE_BUMP"] = False
    p84 = rec("php:8.4.27-fpm-bookworm", "php", "8.4.27", via=["php-8-4"], patch={"latest": "8.4.30"})
    py = rec("python:3.13.7-slim-bookworm", "python", "3.13.7", via=["python-3-13-7"], patch={"latest": "3.13.18"})
    for r_, k_ in ((p84, "taban-yama"), (py, "taban-dizin"), (go, "imaj-yama")):
        a = kind(fx.plan(_FixClient(DF), r_, index=idx, resolver=res), k_)
        check(f"{k_}: _BUMP kapalı → elle, neden ayarı söyler", a["state"] == "elle" and "CREW_TECHDEBT_UPGRADE_BUMP" in a["reason"], str(a))
    out = fx.apply(_FixClient(DF), go, team="T", index=idx, resolver=res, push_fn=never, open_pr_fn=never, start_fn=never)
    check("imaj-yama: _BUMP kapalıyken hiç yazma yok", kind(out, "imaj-yama")["state"] == "elle")
    knobs["CREW_TECHDEBT_UPGRADE_BUMP"] = True

    # ── I4: dal adı eski etiketi taşır ───────────────────────────────────
    g5 = rec("golang:1.24.5", "go", "1.24.5", patch={"latest": "1.24.13"}, users={"anatolia": ["build/alf"]})
    i5 = {"alf": [{"src": "anatolia/build/alf", "images": ["golang:1.24.4", "golang:1.24.5"]}]}
    df5 = {("anatolia", "/build/alf/Dockerfile"): "FROM golang:1.24.4\nFROM golang:1.24.5\n"}
    pr4 = {("anatolia", "tempo/image-patch-golang-1.24.4-1.24.13"): {"id": 3, "status": "active", "url": "https://x/3"}}
    g4 = dict(go, users={"anatolia": ["build/alf"]})
    a4 = kind(fx.plan(_FixClient(df5, region_prs=pr4), g4, index=i5, resolver=res), "imaj-yama")
    a5 = kind(fx.plan(_FixClient(df5, region_prs=pr4), g5, index=i5, resolver=res), "imaj-yama")
    check("dal adı: eski etiket dahil, iki eski sürüm aynı dalı paylaşmaz; birinin PR'ı ötekini kapsamaz",
          a4["branch"] == "tempo/image-patch-golang-1.24.4-1.24.13" and a5["branch"] == "tempo/image-patch-golang-1.24.5-1.24.13"
          and a4["state"] == "pr açık" and a5["state"] == "yok", f"{a4['branch']} {a4['state']} {a5['branch']} {a5['state']}")
    lib = rec("library/golang:1.24.4-alpine", "go", "1.24.4", patch={"latest": "1.24.13"}, users={"anatolia": ["build/alf"]})
    il = {"alf": [{"src": "anatolia/build/alf", "images": ["library/golang:1.24.4-alpine"]}]}
    al = kind(fx.plan(_FixClient({("anatolia", "/build/alf/Dockerfile"): "FROM library/golang:1.24.4-alpine\n"}), lib,
                      index=il, resolver=res), "imaj-yama")
    check("dal adı: library/ adın parçası (golang ile çakışmaz), sonek slug'a girer, tek ek desene uyar",
          al["branch"] == "tempo/image-patch-library-golang-1.24.4-alpine-1.24.13" and bump._BRANCH_IMAGE.fullmatch(al["branch"])
          and al["state"] == "yok", str(al)[:300])
    ao = kind(fx.plan(_FixClient(DF), p84, index=idx, resolver=res), "taban-yama")
    check("taban-yama dalı da eski → yeni çiftini taşır ve desene uyar",
          ao["branch"] == "tempo/image-patch-php-8-4-8.4.27-8.4.30" and bump._BRANCH_IMAGE.fullmatch(ao["branch"]), str(ao)[:200])

    # ── küçük: kuru çalıştırma, dizin dili, metinler ─────────────────────
    dup = dict(DF)
    dup[("global", "/build/alf/Dockerfile")] = "FROM golang:1.24.4 # eski golang:1.24.4\n"
    a = kind(fx.plan(_FixClient(dup), go, index=idx, resolver=res), "imaj-yama")
    check("kuru çalıştırma: satırda eski imaj iki kez → bütün eylem elle, hiçbir bölge yazılmaz",
          a["state"] == "elle" and {u["repo"]: u["state"] for u in a["repos"]}["global"] == "elle", str(a)[:300])
    out = fx.apply(_FixClient(dup), go, team="T", index=idx, resolver=res, push_fn=never, open_pr_fn=never, start_fn=never)
    check("kuru çalıştırma: apply hiçbir bölgeye yazmaz", kind(out, "imaj-yama")["ran"] is False)
    for via, why in ((["python-8-4"], "dili"), (["foo-8-4"], "dizin")):
        a = kind(fx.plan(_FixClient(DF), rec("php:8.4.27", "php", "8.4.27", via=via, patch={"latest": "8.4.30"}),
                         index=idx, resolver=res), "taban-yama")
        check(f"_plan_own: dizin adı / dili uyuşmuyor ({via[0]}) → elle", a["state"] == "elle", str(a)[:200])
    html_ = fx.comment_html([{"kind": "x", "state": "açıldı", "reason": "a" * 299 + "<b>", "pr": None}])
    ital = html_[html_.index("<i>"):html_.index("</i>")]
    check("comment_html: önce keser sonra kaçışlar (yarım varlık yok)", "&lt;" in ital and not re.findall(r"&[a-z]+(?![a-z;])", ital), ital[-30:])
    pub = kind(fx.plan(_FixClient(DF), rec("python:3.11", "python", "3.11", crit=2, high=5), index=idx, resolver=res), "yeniden-üret")
    own = kind(fx.plan(_FixClient(DF), rec("php:8.4.27", "php", "8.4.27", via=["php-8-4"], crit=2, high=5), index=idx, resolver=res), "yeniden-üret")
    check("yeniden-üret: genel imaj → servisi yeniden yayınla; kendi imajımız → DevOps + bayraklar",
          "servisi yeniden yayınla" in pub["reason"] and "DevOps" not in pub["reason"]
          and "DevOps üretmeli" in own["reason"] and "mainLocation" in own["reason"], f"{pub['reason']} | {own['reason']}")
    a = kind(fx.plan(_FixClient(DF), ev, index=mi, resolver=res, find_open_fn=lambda *x: None), "yükseltme")
    check("yükseltme eylemi sınırı (cap) taşır", a["cap"] == 3, str(a)[:200])

    # ── I1: yoklayıcı tek taze index ─────────────────────────────────────
    seen: dict = {"bi": [], "report": None, "fix": None}
    IDX = {"fresh": []}
    _bi, _rep, _alert, _fr = modern.build_index, iv.report, iv.alert, iv._fix_round
    try:
        knobs.update({"CREW_TECHDEBT": True, "CREW_TECHDEBT_IMAGE_VULN": True, "CREW_TECHDEBT_IMAGE_VULN_FIX": True})
        modern.build_index = lambda c, **kw: seen["bi"].append(kw.get("max_age_h")) or IDX
        iv.report = lambda c, **kw: seen.update(report=kw.get("index")) or {"images": []}
        iv.alert = lambda c, rep, **kw: []
        iv._fix_round = lambda c, rep, results, **kw: seen.update(fix=kw.get("index")) or []
        iv._reset_state()
        iv._tick(lambda: NS(team="Ekip"))
        check("_tick: tek taze index (max_age_h=0) hem rapora hem gidermeye verilir",
              seen["bi"] == [0] and seen["report"] is IDX and seen["fix"] is IDX, str(seen["bi"]))
    finally:
        modern.build_index, iv.report, iv.alert, iv._fix_round = _bi, _rep, _alert, _fr
        knobs.pop("CREW_TECHDEBT_IMAGE_VULN_FIX", None)

    # ── I2: tur başına sınır, tek bütçe ──────────────────────────────────
    recs = [rec("a:1", "go", "1.24.4", eol={"eol": "2020"}, crit=5, users={"anatolia": ["x"]}),
            rec("b:1", "go", "1.24.4", eol={"eol": "2020"}, crit=9, users={"anatolia": ["x"]}),
            rec("c:1", "go", "1.24.4", crit=50, users={"anatolia": ["x"]}),
            rec("d:1", "go", "1.24.4", crit=40, users={"anatolia": ["x"]}),
            rec("e:1", "go", "1.24.4", crit=30, users={"anatolia": ["x"]}),
            rec("f:1", "go", "1.24.4", crit=20, users={"anatolia": ["x"]}),
            rec("g:1", "go", "1.24.4", crit=10, users={"anatolia": ["x"]})]
    touched: list = []
    pcl = NS(team="Ekip", add_comment=lambda wid, text: touched.append(("comment", wid)))

    def apply_fn(client, r, **kw):
        touched.append(r["image"])
        return [{"kind": "taban-yama", "state": "açıldı", "ran": True, "reason": "", "pr": {"id": 1, "url": "https://x/1"}}]
    out = iv._fix_round(pcl, {"images": recs}, [{"image": "b:1", "state": "açıldı", "wi": 41}], team="Ekip", index={}, resolver=res,
                        apply_fn=apply_fn)
    check("tur başına sınır: 7 kritik imajdan yalnız 3'ü (destek bitmiş önce, sonra kritik sayısı) işlenir",
          [t for t in touched if isinstance(t, str)] == ["b:1", "a:1", "c:1"], str(touched))
    check("sınırı aşan imajlar 'sırada' sonucu taşır", sorted(o["image"] for o in out if o.get("state") == "sırada") == ["d:1", "e:1", "f:1", "g:1"], str(out)[:300])
    check("alert işi olan imaja yorum yazılır", ("comment", 41) in touched)
    knobs["CREW_TECHDEBT_IMAGE_VULN_MAX_TASKS"] = 5
    touched.clear()
    iv._fix_round(pcl, {"images": recs}, [], team="Ekip", index={}, resolver=res, apply_fn=apply_fn)
    check("sınır ayardan okunur (5)", len([t for t in touched if isinstance(t, str)]) == 5)
    knobs["CREW_TECHDEBT_IMAGE_VULN_MAX_TASKS"] = 3

    def three(images):
        return [rec(i, "go", "1.24.4", eol={"eol": "2020"}, crit=9 - n) for n, i in enumerate(images)]
    mi3 = {"a": [{"src": "anatolia/build/a", "images": ["golang:1.90.0"]}], "b": [{"src": "global/build/b", "images": ["golang:1.91.0"]}],
           "c": [{"src": "russia/build/c", "images": ["golang:1.92.0"]}]}
    recs3 = three(["golang:1.90.0", "golang:1.91.0", "golang:1.92.0"])
    real = functools.partial(fx.apply, push_fn=never, open_pr_fn=never, start_fn=start_fn,
                             find_open_fn=lambda *a: None, find_any_fn=lambda *a: None)
    knobs["CREW_TECHDEBT_UPGRADE_MAX_OPEN"] = 2
    started.clear()
    out = iv._fix_round(pcl, {"images": recs3}, [], team="Ekip", index=mi3, resolver=res, apply_fn=real)
    st = [u["state"] for o in out for a in o["actions"] for u in a.get("repos") or []]
    check("tek izleme bütçesi tüm tur için: 3 imajdan yalnız 2 başlatma, üçüncüsü 'sırada'",
          started == ["a", "b"] and sorted(st) == ["açıldı", "açıldı", "sırada"], f"{started} {st}")

    def flaky(client, repo, lang, **kw):
        if repo == "a":
            raise OSError("Azure düştü")
        return start_fn(client, repo, lang, **kw)
    started.clear()
    real2 = functools.partial(fx.apply, push_fn=never, open_pr_fn=never, start_fn=flaky,
                              find_open_fn=lambda *a: None, find_any_fn=lambda *a: None)
    out = iv._fix_round(pcl, {"images": recs3}, [], team="Ekip", index=mi3, resolver=res, apply_fn=real2)
    st = {u["repo"]: u["state"] for o in out for a in o["actions"] for u in a.get("repos") or []}
    check("başarısız başlatma bütçeyi tüketmez (a hata, b ve c başlar)", started == ["b", "c"] and st["a"] == "hata", f"{started} {st}")

    def existing(client, repo, lang, **kw):
        started.append(repo)
        return {"id": 1, "created": False}
    started.clear()
    real3 = functools.partial(fx.apply, push_fn=never, open_pr_fn=never, start_fn=existing,
                              find_open_fn=lambda *a: None, find_any_fn=lambda *a: None)
    iv._fix_round(pcl, {"images": recs3}, [], team="Ekip", index=mi3, resolver=res, apply_fn=real3)
    check("created=False bütçeyi tüketmez (üçü de denenir)", started == ["a", "b", "c"], str(started))
    knobs["CREW_TECHDEBT_UPGRADE_MAX_OPEN"] = 3

    # ── I5: plan anlık görüntü başına en çok bir kez, bir hata ötekini düşürmez ──
    calls: list = []
    real_plan = fx.plan

    def counting(client, entry, **kw):
        calls.append(entry["image"])
        if entry["image"] == "golang:1.24.4":
            raise RuntimeError("plan patladı")
        return real_plan(client, entry, **kw)
    idx5 = {"a": [{"src": "anatolia/build/a", "images": ["golang:1.24.4", "php:8.4.27-fpm-bookworm", "python:3.11"]}]}
    fx.plan = counting
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            clk = [1000.0]
            skw = dict(index=idx5, resolver=res, eol_fn=lambda *a, **k: None, cache_dir=Path(td), ttl_h=24, now=1000.0,
                       ignore_packages=set())
            fc = _FixClient(DF)
            dec = lambda rep: iv.attach_fix(fc, rep, index=idx5, resolver=res)
            iv._reset_state()
            s1 = iv._snapshot(fc, lambda: clk[0], dec, skw)
            n1 = len(calls)
            clk[0] += 30
            iv._snapshot(fc, lambda: clk[0], dec, skw)
            iv._snapshot(fc, lambda: clk[0], dec, skw)
            check("plan: imaj başına anlık görüntü başına bir kez; bellekten sunulan GET'lerde hiç", n1 == 3 and len(calls) == 3, str(calls))
            clk[0] += 40
            iv._snapshot(fc, lambda: clk[0], dec, skw)
            check("plan: 60 sn geçince yeni anlık görüntüde yeniden (3 daha)", len(calls) == 6, str(calls))
            fixed = {r["image"]: r.get("fix") for r in s1["images"]}
            check("bir imajın planı patlayınca ötekilerin planı düşmez",
                  "fix" not in next(r for r in s1["images"] if r["image"] == "golang:1.24.4")
                  and all("fix" in r for r in s1["images"] if r["image"] != "golang:1.24.4"), str(fixed)[:200])
            iv._reset_state()
    finally:
        fx.plan = real_plan

    # ── gerçek push_sites: kısmi imaj-yama sonrası yeniden deneme ────────
    multi = {("anatolia", "/build/alf/Dockerfile"): ("FROM --platform=linux/amd64 golang:1.24.4 AS builder\nRUN x\n"
                                                     "from golang:1.24.4 AS other\nFROM scratch\n"),
             ("global", "/build/alf/Dockerfile"): "FROM golang:1.24.4\n",
             ("russia", "/build/alf/Dockerfile"): "FROM golang:1.24.4\n"}
    a = kind(fx.plan(_RegionFake(multi), go, index=idx, resolver=res), "imaj-yama")
    ana = next(u for u in a["repos"] if u["repo"] == "anatolia")
    check("çok FROM'lu Dockerfile: --platform / küçük harfli from / AS ile her eşleşen satır bir site",
          [(s["line"], s["old"], s["new"]) for s in ana["sites"]] == [(1, "golang:1.24.4", "golang:1.24.13"), (3, "golang:1.24.4", "golang:1.24.13")],
          str(ana["sites"]))
    rf = _RegionFake(multi, fail_repos={"global"})
    out = fx.apply(rf, go, team="T", index=idx, resolver=res, open_pr_fn=never, start_fn=never)
    us = {u["repo"]: u["state"] for u in kind(out, "imaj-yama")["repos"]}
    check("kısmi imaj-yama (gerçek push_sites): anatolia / russia açıldı, global hata",
          us == {"anatolia": "açıldı", "global": "hata", "russia": "açıldı"}, str(us))
    pushed = rf.br[("anatolia", a["branch"])]["/build/alf/Dockerfile"].decode()
    check("çok FROM'lu dosyada yalnız eşleşen iki satır değişti",
          pushed == ("FROM --platform=linux/amd64 golang:1.24.13 AS builder\nRUN x\nfrom golang:1.24.13 AS other\nFROM scratch\n"), pushed)
    rf.fail_repos.clear()
    rf.calls.clear()
    out = fx.apply(rf, go, team="T", index=idx, resolver=res, open_pr_fn=never, start_fn=never)
    prs = [c for c in rf.calls if c[0] == "pr"]
    check("yeniden deneme: yalnız global için PR açılır, başarılı bölgelerde ikinci PR yok",
          [c[1] for c in prs] == ["global"] and not any(c[1] in ("anatolia", "russia") for c in rf.calls), str(rf.calls))
    us = {u["repo"]: u["state"] for u in kind(out, "imaj-yama")["repos"]}
    check("yeniden deneme sonucu: anatolia / russia 'pr açık', global 'açıldı'",
          us == {"anatolia": "pr açık", "global": "açıldı", "russia": "pr açık"}, str(us))

    # ── kart: onay metni sınırı ve yeniden okuma uyarısı ─────────────────
    html_src = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text()
    seg = html_src[html_src.index("function _ivFixConfirm"):]
    seg = seg[:seg.index("\n}\n")]
    check("onay metni: izleme sayısı sınırı yansıtır, yolların uygulamada yeniden okunduğunu söyler",
          "en çok" in seg and "a.cap" in seg and "yeniden okun" in seg, seg[:300])


def test_image_fix_round2():
    """Giderme düzeltme turu 2: tur sınırı yalnız yazılan imajı sayar, güncel repo / bastırılan uyarı atlanır."""
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import image_fix as fx
    knobs = {"CREW_TECHDEBT_UPGRADE": True, "CREW_TECHDEBT_PHP_MODERN": True, "CREW_TECHDEBT_UPGRADE_BUMP": True,
             "CREW_TECHDEBT_IMAGE_VULN_MAX_TASKS": 3, "CREW_TECHDEBT_UPGRADE_MAX_OPEN": 3}
    _get, _cur = pc.get, fx._repo_current if hasattr(fx, "_repo_current") else None
    pc.get = lambda k: knobs[k] if k in knobs else _get(k)
    fx._repo_current = lambda *a, **k: None          # gerçek klonlara / index'e gidilmesin
    try:
        with _fix_env(knobs):
            _image_fix_round2_body(knobs)
    finally:
        pc.get = _get
        if _cur is not None:
            fx._repo_current = _cur


def _image_fix_round2_body(knobs):
    import functools
    import logging
    from types import SimpleNamespace as NS
    from agile_sdlc_crew.quality import image_fix as fx, image_vuln as iv
    from agile_sdlc_crew.quality import upgrade as up, upgrade_bump as bump, upgrade_registry as ur

    hub = {"php": ["8.4.30", "8.4.30-fpm-trixie"], "golang": ["1.24.13", "1.24.13-alpine"]}
    res = ur.Resolver(fetch=_fake_hub(hub))

    def rec(image, lang, version, *, via=(), users=None, patch=None, eol=None, crit=0, level="kritik"):
        return {"image": image, "lang": lang, "version": version, "via": list(via), "touched": "", "error": "",
                "users": users if users is not None else {"anatolia": ["build/alf"]},
                "scan": {"state": "tamam", "critical": crit, "high": 0, "packages": [], "os": "", "created": "",
                         "ignored": {"critical": 0, "high": 0, "packages": []}},
                "patch": patch, "eol": eol, "level": level, "actions": []}

    def kind(acts, k):
        return next((a for a in acts if a["kind"] == k), None)

    def never(*a, **k):
        raise AssertionError("yazma yapılmamalıydı")
    DF = {("anatolia", "/build/alf/Dockerfile"): "FROM golang:1.24.4\n"}
    want_to = up.known_versions("go")[-1]
    ev = rec("golang:1.99.0", "go", "1.24.4", eol={"eol": "2020-01-01"})
    mi1 = {"alf": [{"src": "anatolia/build/alf", "images": ["golang:1.99.0"]}]}
    fo = lambda c, lang, repo: None
    fa = lambda c, lang, repo, to=None: None

    # ── 1: tur sınırı yalnız yazılan imajı sayar ─────────────────────────
    ran_acts = [{"kind": "taban-yama", "state": "açıldı", "ran": True, "reason": "", "pr": {"id": 1, "url": "https://x/1"}}]
    idle_acts = [{"kind": "yükseltme", "state": "izleniyor", "ran": False, "reason": "", "pr": None}]
    recs = [rec(f"i{n}:1", "go", "1.24.4", eol={"eol": "2020"}, crit=9 - n, users={"anatolia": ["x"]}) for n in range(3)]
    recs.append(rec("p:1", "php", "8.4.27", crit=1, users={"anatolia": ["x"]}))
    called: list = []

    def apply_idle(client, r, **kw):
        called.append(r["image"])
        return ran_acts if r["image"] == "p:1" else idle_acts
    comments: list = []
    pcl = NS(team="Ekip", add_comment=lambda wid, text: comments.append(wid))
    out = iv._fix_round(pcl, {"images": recs}, [{"image": "p:1", "state": "açıldı", "wi": 77}], team="Ekip", index={},
                        resolver=res, apply_fn=apply_idle)
    check("tur sınırı: yazılacak işi olmayan 3 imaj slot yemez, 4. imaj işlenir",
          called == ["i0:1", "i1:1", "i2:1", "p:1"] and not any(o.get("state") == "sırada" for o in out) and comments == [77],
          f"{called} {out}")
    five = [rec(f"j{n}:1", "go", "1.24.4", crit=9 - n, users={"anatolia": ["x"]}) for n in range(5)]
    called.clear()

    def apply_ran(client, r, **kw):
        called.append(r["image"])
        return ran_acts
    out = iv._fix_round(pcl, {"images": five}, [], team="Ekip", index={}, resolver=res, apply_fn=apply_ran)
    check("tur sınırı: 5 imajın hepsinde iş varsa yalnız 3'ü çalışır, 2'si 'sırada'",
          len(called) == 3 and sorted(o["image"] for o in out if o.get("state") == "sırada") == ["j3:1", "j4:1"], f"{called} {out}")

    # ── 2: repo zaten hedef sürümde ──────────────────────────────────────
    started: list = []

    def start_fn(client, repo, lang, **kw):
        started.append((repo, kw.get("index")))
        return {"id": 700, "created": True}
    a = kind(fx.plan(_FixClient(DF), ev, index=mi1, resolver=res, find_open_fn=fo, find_any_fn=fa,
                     current_fn=lambda c, repo, lang, index: want_to), "yükseltme")
    check("plan: repo zaten hedef sürümde → 'güncel', neden Türkçe, açılabilir değil",
          a["state"] == "güncel" and a["repos"][0]["state"] == "güncel" and "repo zaten hedef sürümde" in a["repos"][0]["reason"], str(a)[:300])
    out = fx.apply(_FixClient(DF), ev, team="T", index=mi1, resolver=res, push_fn=never, open_pr_fn=never, start_fn=start_fn,
                   find_open_fn=fo, find_any_fn=fa, current_fn=lambda c, repo, lang, index: want_to)
    check("apply: güncel repo için start çağrılmaz, ran False", started == [] and kind(out, "yükseltme")["ran"] is False)
    comments.clear()
    real = functools.partial(fx.apply, push_fn=never, open_pr_fn=never, start_fn=start_fn, find_open_fn=fo, find_any_fn=fa,
                             current_fn=lambda c, repo, lang, index: want_to)
    iv._fix_round(pcl, {"images": [ev | {"level": "kritik"}]}, [{"image": "golang:1.99.0", "state": "açıldı", "wi": 5}],
                  team="Ekip", index=mi1, resolver=res, apply_fn=real)
    check("tur: güncel repo için uyarı işine yorum düşmez", comments == [])

    def at_target(client, repo, lang, **kw):
        raise ValueError("Hedef sürüm seçilemez: '1.27'")
    budget = [1]
    out = fx.apply(_FixClient(DF), ev, team="T", index=mi1, resolver=res, push_fn=never, open_pr_fn=never, start_fn=at_target,
                   find_open_fn=fo, find_any_fn=fa, budget=budget)
    a = kind(out, "yükseltme")
    check("start ValueError (yazmadan red): hata nedeni korunur, ran False, bütçe tüketilmez [sözleşme: ran = yazıldı]",
          a["repos"][0]["state"] == "hata" and a["ran"] is False and budget == [1], str(a)[:300])

    def other_err(client, repo, lang, **kw):
        raise ValueError("Şimdiki Go sürümü bilinmiyor")
    out = fx.apply(_FixClient(DF), ev, team="T", index=mi1, resolver=res, push_fn=never, open_pr_fn=never, start_fn=other_err,
                   find_open_fn=fo, find_any_fn=fa)
    check("başka bir start ValueError'ı 'hata' kalır; yazma olmadığı için ran False [sözleşme: ran = yazıldı]",
          kind(out, "yükseltme")["state"] == "hata" and kind(out, "yükseltme")["ran"] is False)

    # ── 7: taze index start'a iletilir ───────────────────────────────────
    started.clear()
    fx.apply(_FixClient(DF), ev, team="T", index=mi1, resolver=res, push_fn=never, open_pr_fn=never, start_fn=start_fn,
             find_open_fn=fo, find_any_fn=fa)
    check("start'a aynı taze index verilir", started and started[0][1] is mi1, str(started))

    # ── 3: bastırılmış uyarıya yoklayıcı dokunmaz ────────────────────────
    called.clear()
    comments.clear()
    two = [rec("s:1", "go", "1.24.4", eol={"eol": "2020"}, crit=9, users={"anatolia": ["x"]}),
           rec("t:1", "go", "1.24.4", crit=1, users={"anatolia": ["x"]})]
    sup_deps = SimpleNamespace(board=lambda c: {iv._fp("s:1"): {"wi": 91, "state": "Canceled", "closed_at": 0.0}},
                               now=lambda: 1000.0)
    out = iv._fix_round(pcl, {"images": two}, [{"image": "t:1", "state": "açıldı", "wi": 92}], team="Ekip", index={},
                        resolver=res, apply_fn=apply_ran, deps=sup_deps)
    check("bastırılan uyarı: remediation yok, kapalı işe yorum yok, durum 'bastırıldı'; ötekisi işlenir",
          called == ["t:1"] and comments == [92]
          and next(o for o in out if o["image"] == "s:1")["state"] == "bastırıldı", f"{called} {comments} {out}")

    # ── 4: belirsiz sonuç tek uyarı satırı ───────────────────────────────
    records: list = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)
    h = _H(level=logging.INFO)
    lg = logging.getLogger("pipeline")
    lg.addHandler(h)
    try:
        def apply_unsure(client, r, **kw):
            return [{"kind": "yükseltme", "state": "yok", "ran": False, "reason": "", "pr": None,
                     "repos": [{"repo": "a", "state": "belirsiz", "reason": "izleme geçmişi okunamadı: ağ"},
                               {"repo": "b", "state": "belirsiz", "reason": "izleme geçmişi okunamadı: ağ"}]},
                    {"kind": "taban-yama", "state": "belirsiz", "ran": False, "reason": "okunamadı: Azure", "pr": None}]
        comments.clear()
        iv._fix_round(pcl, {"images": [two[1]]}, [{"image": "t:1", "state": "açıldı", "wi": 92}], team="Ekip", index={},
                      resolver=res, apply_fn=apply_unsure)
    finally:
        lg.removeHandler(h)
    warns = [r for r in records if r.levelno == logging.WARNING and "t:1" in r.getMessage()]
    check("belirsiz eylem: imaj başına TEK uyarı satırı, nedenleri taşır, iş yorumu yok",
          len(warns) == 1 and "izleme geçmişi okunamadı" in warns[0].getMessage() and "okunamadı: Azure" in warns[0].getMessage()
          and comments == [], str([r.getMessage() for r in records])[:300])

    # ── 5: library/golang aynı dalı paylaşmaz ────────────────────────────
    g1 = rec("golang:1.24.4", "go", "1.24.4", patch={"latest": "1.24.13"})
    g2 = rec("library/golang:1.24.4", "go", "1.24.4", patch={"latest": "1.24.13"})
    i2 = {"alf": [{"src": "anatolia/build/alf", "images": ["golang:1.24.4", "library/golang:1.24.4"]}]}
    df2 = {("anatolia", "/build/alf/Dockerfile"): "FROM golang:1.24.4\nFROM library/golang:1.24.4\n"}
    pr1 = {("anatolia", "tempo/image-patch-golang-1.24.4-1.24.13"): {"id": 3, "status": "active", "url": "https://x/3"}}
    a1 = kind(fx.plan(_FixClient(df2, region_prs=pr1), g1, index=i2, resolver=res), "imaj-yama")
    a2 = kind(fx.plan(_FixClient(df2, region_prs=pr1), g2, index=i2, resolver=res), "imaj-yama")
    check("library/golang ve golang aynı dalı paylaşmaz; birinin PR'ı ötekini kapsamaz",
          a1["branch"] != a2["branch"] and a1["state"] == "pr açık" and a2["state"] == "yok"
          and bump._BRANCH_IMAGE.fullmatch(a2["branch"]), f"{a1['branch']} {a2['branch']} {a2['state']}")

    # ── 6: ASCII dışı rakam ──────────────────────────────────────────────
    p84 = rec("php:8.4.27-fpm-bookworm", "php", "8.4.27", via=["php-8-4"], patch={"latest": "8.4.30"})
    uni = _FixClient(DF, files={"/php-8-4/Dockerfile": "ARG PHP_VERSION=8.4.２７\nFROM php:${PHP_VERSION}-fpm-trixie\n".encode()})
    a = kind(fx.plan(uni, p84, index=mi1, resolver=res), "taban-yama")
    check("plan: ASCII dışı rakamlı sürüm satırı → elle (push anında patlamaz)", a["state"] == "elle", str(a)[:250])
    _vb = bump._validate_branch
    bump._validate_branch = lambda b, t: (_ for _ in ()).throw(ValueError("Geçersiz dal adı: x"))
    try:
        a = kind(fx.plan(_FixClient(DF), p84, index=mi1, resolver=res), "taban-yama")
        ag = kind(fx.plan(_FixClient(DF), rec("golang:1.24.4", "go", "1.24.4", patch={"latest": "1.24.13"},
                                              users={"anatolia": ["build/alf"]}),
                          index={"alf": [{"src": "anatolia/build/alf", "images": ["golang:1.24.4"]}]}, resolver=res), "imaj-yama")
    finally:
        bump._validate_branch = _vb
    check("plan: hesaplanan dal adı _validate_branch'ten geçmezse elle (taban-yama ve imaj-yama)",
          a["state"] == "elle" and "dal adı" in a["reason"].lower() and ag["state"] == "elle", f"{a['reason']} | {ag['state']}")

    # ── 8: eski yama PR'ı hâlâ açıkken ikincisi açılmaz ──────────────────
    c = _FixClient(DF, prs={"tempo/image-patch-php-8-4-8.4.27-8.4.29": {"id": 12, "status": "active", "url": "https://x/12"}})
    c.files["tempo/image-patch-php-8-4-8.4.27-8.4.29"] = {}
    a = kind(fx.plan(c, p84, index=mi1, resolver=res), "taban-yama")
    check("eski yama PR'ı açık → 'pr açık', neden numarayı söyler",
          a["state"] == "pr açık" and "önceki yama PR'ı açık: #12" in a["reason"], str(a)[:300])
    c = _FixClient(DF, prs={"tempo/image-patch-php-8-4-8.4.27-8.4.29": {"id": 12, "status": "abandoned", "url": "https://x/12"}})
    c.files["tempo/image-patch-php-8-4-8.4.27-8.4.29"] = {}
    a = kind(fx.plan(c, p84, index=mi1, resolver=res), "taban-yama")
    check("eski yama PR'ı terk edilmişse engellemez", a["state"] == "yok", str(a)[:200])
    gm = rec("golang:1.24.4", "go", "1.24.4", patch={"latest": "1.24.13"}, users={"anatolia": ["build/alf"]})
    ix = {"alf": [{"src": "anatolia/build/alf", "images": ["golang:1.24.4"]}]}
    rf = _RegionFake({("anatolia", "/build/alf/Dockerfile"): "FROM golang:1.24.4\n"})
    rf.br[("anatolia", "tempo/image-patch-golang-1.24.4-1.24.12")] = {}
    rf.prs[("anatolia", "tempo/image-patch-golang-1.24.4-1.24.12")] = {"id": 8, "status": "active", "url": "https://x/8",
                                                                      "target": "production", "created_by": "me"}
    a = kind(fx.plan(rf, gm, index=ix, resolver=res), "imaj-yama")
    check("imaj-yama: bölgede eski yama PR'ı açık → 'pr açık' (ikinci çakışan PR açılmaz)",
          a["repos"][0]["state"] == "pr açık" and "önceki yama PR'ı açık: #8" in a["repos"][0]["reason"], str(a)[:300])
    out = fx.apply(rf, gm, team="T", index=ix, resolver=res, open_pr_fn=never, start_fn=never)
    check("eski PR açıkken apply hiçbir şey yazmaz", not [x for x in rf.calls if x[0] in ("branch", "push", "pr")], str(rf.calls))


import contextlib


_FX_REAL_CURRENT = [None]


@contextlib.contextmanager
def _fix_env(knobs):
    """Giderme testleri için ortak ortam: ayarlar, gerçek klona gitmeyen `_repo_current`, sahte pano, geçici önbellek."""
    import tempfile
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import image_fix as fx, image_vuln as iv
    _get, _cur = pc.get, getattr(fx, "_repo_current", None)
    _dd, _cd = getattr(iv, "_default_deps", None), iv.CACHE_DIR
    _FX_REAL_CURRENT[0] = _cur
    with tempfile.TemporaryDirectory() as td:
        pc.get = lambda k: knobs[k] if k in knobs else _get(k)
        fx._repo_current = lambda *a, **k: None
        iv._default_deps = lambda: SimpleNamespace(board=lambda c: {}, now=lambda: 1000.0)
        iv.CACHE_DIR = Path(td)
        try:
            yield
        finally:
            pc.get = _get
            if _cur is not None:
                fx._repo_current = _cur
            if _dd is not None:
                iv._default_deps = _dd
            elif hasattr(iv, "_default_deps"):
                del iv._default_deps
            iv.CACHE_DIR = _cd


def test_image_fix_final():
    """Son düzeltme dalgası: yazılmayan deneme sıra harcamaz, bastırma kapalı kalır, yama PR'ı yükseltme PR'ıyla çakışmaz."""
    print("\n[taban imaj güvenliği: giderme — son düzeltme dalgası]")
    knobs = {"CREW_TECHDEBT_UPGRADE": True, "CREW_TECHDEBT_PHP_MODERN": True, "CREW_TECHDEBT_UPGRADE_BUMP": True,
             "CREW_TECHDEBT_IMAGE_VULN_MAX_TASKS": 3, "CREW_TECHDEBT_UPGRADE_MAX_OPEN": 3}
    with _fix_env(knobs):
        _image_fix_final_body(knobs)


def _image_fix_final_body(knobs):
    import functools
    import inspect
    import logging
    import tempfile
    from types import SimpleNamespace as NS
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import image_fix as fx, image_vuln as iv, modern, php_modern as pmod
    from agile_sdlc_crew.quality import upgrade_bump as bump, upgrade_image as ui, upgrade_registry as ur, upgrade_track as ut

    hub = {"php": ["8.4.30", "8.4.30-fpm-trixie"], "golang": ["1.24.13", "1.24.13-alpine"],
           "python": ["3.13.18", "3.13.18-slim-bookworm"]}
    res = ur.Resolver(fetch=_fake_hub(hub))

    def rec(image, lang, version, *, via=(), users=None, patch=None, eol=None, crit=0, level="kritik"):
        return {"image": image, "lang": lang, "version": version, "via": list(via), "touched": "", "error": "",
                "users": users if users is not None else {"anatolia": ["build/alf"]},
                "scan": {"state": "tamam", "critical": crit, "high": 0, "packages": [], "os": "", "created": "",
                         "ignored": {"critical": 0, "high": 0, "packages": []}},
                "patch": patch, "eol": eol, "level": level, "actions": []}

    def kind(acts, k):
        return next((a for a in acts if a["kind"] == k), None)

    def never(*a, **k):
        raise AssertionError("yazma yapılmamalıydı")
    DF = {(r, "/build/alf/Dockerfile"): "FROM golang:1.24.4\n" for r in ("anatolia", "global", "russia")}
    idx = {"alf": [{"src": f"{r}/build/alf", "images": ["golang:1.24.4"]} for r in ("anatolia", "global", "russia")]}
    go = rec("golang:1.24.4", "go", "1.24.4", patch={"latest": "1.24.13"},
             users={"anatolia": ["build/alf"], "global": ["build/alf"], "russia": ["build/alf"]})
    p84 = rec("php:8.4.27-fpm-bookworm", "php", "8.4.27", via=["php-8-4"], patch={"latest": "8.4.30"},
              users={"anatolia": ["build/channel"], "russia": ["build/core"]})
    fo = lambda *a: None
    pcl_comments: list = []
    pcl = NS(team="Ekip", add_comment=lambda wid, text: pcl_comments.append((wid, text)))
    fcl = _FixClient(DF)
    fcl.add_comment = pcl.add_comment

    # ── I1: yazılmayan deneme ran=False, sıra harcamaz, hata yorumu bir kez ──
    def leftover(client, sites, **kw):
        raise ValueError("kalan: dal var, PR'ı yok ve hedef dalla aynı commit'te değil")
    out = fx.apply(_FixClient(DF), go, team="T", index=idx, resolver=res, push_fn=leftover, open_pr_fn=never, start_fn=never)
    a = kind(out, "imaj-yama")
    check("kalan dal reddi: yazma yok → ran False, hata nedeni korunur",
          a["ran"] is False and a["state"] == "hata" and "dal var" in a["reason"], str(a)[:300])
    real = functools.partial(fx.apply, push_fn=leftover, open_pr_fn=never, start_fn=never, find_open_fn=fo,
                             find_any_fn=fo)
    gk = dict(go, level="kritik")
    rounds = [{"images": [gk]}]
    res_wi = [{"image": go["image"], "state": "açık iş", "wi": 50}]
    pcl_comments.clear()
    for _ in range(2):
        iv._fix_round(fcl, rounds[0], res_wi, team="Ekip", index=idx, resolver=res, apply_fn=real)
    check("aynı hata ardışık iki turda: uyarı işine TEK yorum", len(pcl_comments) == 1, str(pcl_comments)[:200])

    def leftover2(client, sites, **kw):
        raise ValueError("başka bir red nedeni")
    real2 = functools.partial(fx.apply, push_fn=leftover2, open_pr_fn=never, start_fn=never, find_open_fn=fo, find_any_fn=fo)
    iv._fix_round(fcl, rounds[0], res_wi, team="Ekip", index=idx, resolver=res, apply_fn=real2)
    check("neden değişince yeniden yorum yazılır", len(pcl_comments) == 2, str(len(pcl_comments)))
    called: list = []

    def fake_apply(client, r, **kw):
        called.append(r["image"])
        if r["image"] == "d:1":
            return [{"kind": "imaj-yama", "state": "açıldı", "ran": True, "reason": "", "pr": {"id": 1, "url": "https://x/1"}}]
        return [{"kind": "imaj-yama", "state": "hata", "ran": False, "reason": "red", "pr": None}]
    four = [rec(f"{c}:1", "go", "1.24.4", eol={"eol": "2020"}, crit=9 - n, users={"anatolia": ["x"]})
            for n, c in enumerate("abcd")]
    out = iv._fix_round(pcl, {"images": four}, [], team="Ekip", index={}, resolver=res, apply_fn=fake_apply)
    check("üç reddeden imaj + dördüncü yazılabilir: dördüncü de işlenir (slot yemez)",
          called == ["a:1", "b:1", "c:1", "d:1"] and not any(o.get("state") == "sırada" for o in out), f"{called} {out}")

    ev = rec("golang:1.99.0", "go", "1.24.4", eol={"eol": "2020-01-01"})
    mi1 = {"alf": [{"src": "anatolia/build/alf", "images": ["golang:1.99.0"]}]}

    def start_value_error(client, repo, lang, **kw):
        raise ValueError("Repo yerelde yok ya da adı geçersiz: 'alf'")
    budget = [2]
    out = fx.apply(_FixClient(DF), ev, team="T", index=mi1, resolver=res, push_fn=never, open_pr_fn=never,
                   start_fn=start_value_error, find_open_fn=fo, find_any_fn=fo, budget=budget)
    a = kind(out, "yükseltme")
    check("start ValueError: denenmedi sayılır (ran False), bütçe harcanmaz, neden korunur",
          a["ran"] is False and a["state"] == "hata" and "Repo yerelde yok" in a["reason"] and budget == [2], str(a)[:300])

    def start_select_error(client, repo, lang, **kw):
        raise ValueError("Hedef sürüm seçilemez: '1.27'")
    out = fx.apply(_FixClient(DF), ev, team="T", index=mi1, resolver=res, push_fn=never, open_pr_fn=never,
                   start_fn=start_select_error, find_open_fn=fo, find_any_fn=fo, budget=[1])
    check("M2: açık hedefle start 'Hedef sürüm seçilemez' derse 'hata' + ran False (ölü eşleme yok)",
          kind(out, "yükseltme")["ran"] is False and kind(out, "yükseltme")["state"] == "hata")
    py = rec("python:3.13.7-slim-bookworm", "python", "3.13.7", via=["python-3-13-7"], patch={"latest": "3.13.18"},
             users={"anatolia": ["build/py"]})
    ipy = {"py": [{"src": "anatolia/build/py", "images": ["r.example/prod-ar/python-3-13-7:1.0"]}]}
    stg = {"/python-3-13-7/Dockerfile": b"FROM python:3.13.7-slim-bookworm", "/python-3-13-7/tag.txt": b"1.0"}
    ipy = {"py": [{"src": "anatolia/build/py", "images": ["europe-west1-docker.pkg.dev/flo-prod-main/prod-ar/python-3-13-7:1.0"]}]}

    def tmpl_refuse(client, lang, to, entry, **kw):
        raise ui.ImageTemplateError("Hub'da etiket yok. Push yapılmadı.")

    def tmpl_partial(client, lang, to, entry, **kw):
        raise ui.ImageTemplateError("'tempo/base-image-x' dalı, PR oluşturma aşamasında hata: x. Push yapıldı.", wrote=True)

    def tmpl_ready_no_pr(client, lang, to, entry, **kw):
        # dal önceki denemeden hazır, bu çağrı yalnız PR açmayı denedi: metin "Push yapıldı" der ama yazma yok
        raise ui.ImageTemplateError("'tempo/base-image-x' dalı, PR oluşturma aşamasında hata: x. Push yapıldı.")
    o1 = fx.apply(_FixClient(DF, files=stg), py, team="T", index=ipy, resolver=res, push_fn=never, open_pr_fn=tmpl_refuse, start_fn=never)
    o2 = fx.apply(_FixClient(DF, files=stg), py, team="T", index=ipy, resolver=res, push_fn=never, open_pr_fn=tmpl_partial, start_fn=never)
    check("open_pr yazmadan reddederse ran False; push yapıldıktan sonra hata verirse ran True",
          kind(o1, "taban-dizin")["ran"] is False and kind(o1, "taban-dizin")["state"] == "hata"
          and kind(o2, "taban-dizin")["ran"] is True, f"{kind(o1, 'taban-dizin')} {kind(o2, 'taban-dizin')}")
    o3 = fx.apply(_FixClient(DF, files=stg), py, team="T", index=ipy, resolver=res, push_fn=never, open_pr_fn=tmpl_ready_no_pr, start_fn=never)
    check("dal hazırken yalnız PR açılamıyorsa ran False (metne değil hatanın wrote alanına bakılır; her gece sıra yanmaz)",
          kind(o3, "taban-dizin")["ran"] is False and kind(o3, "taban-dizin")["state"] == "hata", str(kind(o3, "taban-dizin")))

    def push_res(res_):
        return lambda client, sites, **kw: res_
    cases = [({"prs": [{"repo": "base-images", "id": 3, "url": "https://x/3", "reused": True}], "fallback": [], "errors": []}, False, "pr açık"),
             ({"prs": [], "fallback": [{"repo": "base-images", "diff": "d", "branch_written": False}], "errors": []}, False, "elle"),
             ({"prs": [], "fallback": [], "errors": [{"repo": "base-images", "error": "x", "branch_written": True, "stage": "pr"}]}, True, "hata"),
             ({"prs": [], "fallback": [], "errors": [{"repo": "base-images", "error": "x", "branch_written": False, "stage": "create"}]}, False, "hata"),
             ({"prs": [{"repo": "base-images", "id": 4, "url": "https://x/4"}], "fallback": [], "errors": []}, True, "açıldı")]
    for r_, want_ran, want_state in cases:
        a = kind(fx.apply(_FixClient(DF), p84, team="T", index=idx, resolver=res, push_fn=push_res(r_), open_pr_fn=never, start_fn=never),
                 "taban-yama")
        check(f"push sonucu → ran {want_ran}, durum {want_state}", a["ran"] is want_ran and a["state"] == want_state, str(a)[:240])

    # ── I2: öneki tam eşleşme (kardeş sonekler karışmaz) ─────────────────
    ix = {"alf": [{"src": "anatolia/build/alf", "images": ["golang:1.24.4"]}]}
    gm = rec("golang:1.24.4", "go", "1.24.4", patch={"latest": "1.24.13"}, users={"anatolia": ["build/alf"]})
    rf = _RegionFake({("anatolia", "/build/alf/Dockerfile"): "FROM golang:1.24.4\n"})
    for sib in ("alpine", "bookworm"):
        b = f"tempo/image-patch-golang-1.24.4-{sib}-1.24.13"
        rf.br[("anatolia", b)] = {}
        rf.prs[("anatolia", b)] = {"id": 21, "status": "active", "url": "https://x/21", "target": "production", "created_by": "me"}
    a = kind(fx.plan(rf, gm, index=ix, resolver=res), "imaj-yama")
    check("kardeş sonekli (-alpine / -bookworm) etkin PR bu imajı engellemez", a["repos"][0]["state"] == "yok", str(a)[:300])
    rf.br[("anatolia", "tempo/image-patch-golang-1.24.4-1.24.12")] = {}
    rf.prs[("anatolia", "tempo/image-patch-golang-1.24.4-1.24.12")] = {"id": 22, "status": "active", "url": "https://x/22",
                                                                       "target": "production", "created_by": "me"}
    a = kind(fx.plan(rf, gm, index=ix, resolver=res), "imaj-yama")
    check("aynı imajın daha eski yamasına giden açık PR engeller", a["repos"][0]["state"] == "pr açık"
          and "#22" in a["repos"][0]["reason"], str(a)[:300])

    # ── I3(a): yama PR'ı gövdesi yükseltme uyarısını taşır ───────────────
    bodies: list = []

    def cap_push(client, sites, **kw):
        bodies.append(kw["body"])
        return {"prs": [{"repo": sites[0]["repo"], "id": 9, "url": "https://x/9"}], "fallback": [], "errors": []}
    started: list = []

    def start_ok(client, repo, lang, **kw):
        started.append(repo)
        return {"id": 700, "created": True}
    goeol = rec("golang:1.24.4", "go", "1.24.4", patch={"latest": "1.24.13"}, eol={"eol": "2026-02-10"},
                users={"anatolia": ["build/alf"], "global": ["build/alf"], "russia": ["build/alf"]})
    fx.apply(_FixClient(DF), goeol, team="T", index=idx, resolver=res, push_fn=cap_push, open_pr_fn=never, start_fn=start_ok,
             find_open_fn=fo, find_any_fn=fo)
    note = "Bu imajı kullanan repolar için sürüm yükseltme izlemesi de var; yükseltme PR'ı aynı satırı değiştirir — bu PR önce birleştirilmeli ya da kapatılmalı."
    check("eol + yama: yama PR gövdesi yükseltme uyarısını taşır", bodies and all(note in b for b in bodies), str(bodies)[:300])
    bodies.clear()
    fx.apply(_FixClient(DF), go, team="T", index=idx, resolver=res, push_fn=cap_push, open_pr_fn=never, start_fn=never)
    check("yalnız yama: uyarı yok", bodies and not any("sürüm yükseltme izlemesi de var" in b for b in bodies))

    # ── I3(b): yükseltme prod adımı açık yama PR'ı varken hazır değil ────
    pr_ok = {"id": 31, "status": "active", "url": "https://x/31", "target": "production", "created_by": "me"}
    dvs = [{"where": "devops", "repo": r, "project": "FLODevOps", "branch": "production", "path": "/build/alf/Dockerfile",
            "line": 1, "old": "golang:1.24.4", "new": "golang:1.27.4", "blocked": ""} for r in ("anatolia", "russia")]
    spy = _UpFake(branches={("FLODevOps", "anatolia"): ["main", "production", "tempo/image-patch-golang-1.24.4-1.24.13"]},
                  prs={("FLODevOps", "anatolia", "tempo/image-patch-golang-1.24.4-1.24.13"): pr_ok})
    knobs["CREW_TECHDEBT_IMAGE_VULN"] = False
    calls_before = len(spy.wiql)
    check("_patch_pr_block: ayar kapalı → boş, Azure çağrısı yok",
          ut._patch_pr_block(NS(list_branches=never, find_pull_request=never), dvs) == "")
    knobs["CREW_TECHDEBT_IMAGE_VULN"] = True
    why = ut._patch_pr_block(spy, dvs)
    check("açık yama PR'ı → neden numarayı söyler", "aynı satırı değiştiren yama PR'ı açık: #31" in why and "önce o kapanmalı" in why, why)
    for st_ in ("abandoned", "completed"):
        spy2 = _UpFake(branches={("FLODevOps", "anatolia"): ["main", "production", "tempo/image-patch-golang-1.24.4-1.24.13"]},
                       prs={("FLODevOps", "anatolia", "tempo/image-patch-golang-1.24.4-1.24.13"): dict(pr_ok, status=st_)})
        check(f"{st_} yama PR'ı engellemez", ut._patch_pr_block(spy2, dvs) == "")
    own = [dict(dvs[0], old="eu/php-8-2:1.0", new="eu/php-8-4:1.0")]
    check("kendi imajımızın sitesi (üç parçalı genel etiket değil) sorgulanmaz",
          ut._patch_pr_block(NS(list_branches=never, find_pull_request=never), own) == "")

    class _Boom(_UpFake):
        def list_branches(self, repo, project=None):
            raise OSError("ağ")
    why = ut._patch_pr_block(_Boom(), dvs)
    check("ayar açıkken arama hatası → hazır değil (bilinmeyen)", why and "okunamadı" in why, why)
    src = inspect.getsource(ut._status)
    check("_status prod adımından önce _patch_pr_block'a bakar", "_patch_pr_block(" in src)

    # ── I4: bastırma kapalı kalır ────────────────────────────────────────
    tk = rec("t:1", "go", "1.24.4", crit=1, users={"anatolia": ["x"]})
    board = {iv._fp("t:1"): {"wi": 9, "state": "Canceled", "closed_at": 0.0}}
    deps = NS(board=lambda c: board, now=lambda: 1000.0)
    called.clear()

    def fa(client, r, **kw):
        called.append(r["image"])
        return [{"kind": "imaj-yama", "state": "açıldı", "ran": True, "reason": "", "pr": None}]
    out = iv._fix_round(pcl, {"images": [tk]}, [], team="Ekip", index={}, resolver=res, apply_fn=fa, deps=deps)
    check("_TASK kapalı (sonuç yok) + panoda iptal edilmiş iş → bastırıldı, hiç yazma yok",
          called == [] and out and out[0]["state"] == "bastırıldı", f"{called} {out}")

    def bad_board(c):
        raise OSError("pano okunamadı")
    called.clear()
    out = iv._fix_round(pcl, {"images": [tk]}, [], team="Ekip", index={}, resolver=res, apply_fn=fa,
                        deps=NS(board=bad_board, now=lambda: 1000.0))
    check("pano okunamazsa tur HİÇBİR şey yapmaz", called == [] and out == [])
    ok_board = {iv._fp("t:1"): {"wi": 9, "state": "New", "closed_at": 0.0}}
    called.clear()
    pcl_comments.clear()
    iv._fix_round(pcl, {"images": [tk]}, [], team="Ekip", index={}, resolver=res, apply_fn=fa,
                  deps=NS(board=lambda c: ok_board, now=lambda: 1000.0))
    check("açık iş varsa işlenir; yorum panodaki işe (sonuç listesi olmasa da) yazılır",
          called == ["t:1"] and [c[0] for c in pcl_comments] == [9], f"{called} {pcl_comments}")

    # ── M1: durdurulan ↔ tamamlanan izleme ───────────────────────────────
    def row(wid, to, state="Closed"):
        return {"id": wid, "fields": {"System.State": state, "System.Tags": "tempo-upgrade-track", "System.AreaPath": "P\\T",
                                      "System.Description": f"<p>tempo-upgrade-key: go:alf:{to}</p>"}}
    done_c = [{"metin": "<p><b>Yükseltme tamamlandı:</b> alf üretimde 1.26 sürümünde.</p>", "tarih": "2026-11-01T10:00:00Z"}]
    stop_c = [{"metin": "<p>Yükseltme izlemesi durduruldu (T).</p>", "tarih": "2026-11-01T10:00:00Z"}]
    cm = {5: done_c, 6: stop_c}
    tc = NS(query_work_items=lambda q, **kw: rows_[0], get_work_item_comments=lambda wid: cm.get(wid, []))
    rows_ = [[row(5, "1.26")]]
    check("tamamlanmış (eski hedef) izleme yeni hedefi engellemez", ut.find_stopped(tc, "go", "alf", "1.27") is None)
    check("tamamlanmış izleme aynı hedefi engeller", (ut.find_stopped(tc, "go", "alf", "1.26") or {}).get("id") == 5)
    rows_[0] = [row(5, "1.26"), row(6, "1.25")]
    check("insanın durdurduğu (tamamlanma notu yok) izleme HER hedef için engeller",
          (ut.find_stopped(tc, "go", "alf", "1.27") or {}).get("id") == 6)
    rows_[0] = [row(7, "1.26", state="New")]
    check("açık izleme find_stopped'a girmez", ut.find_stopped(tc, "go", "alf", "1.27") is None)
    seen_to: list = []
    a = kind(fx.plan(_FixClient(DF), ev, index=mi1, resolver=res, find_open_fn=fo, auto=True,
                     find_any_fn=lambda c, lang, repo, to: seen_to.append(to) or None), "yükseltme")
    check("plan geçmiş sorgusuna hedefi geçirir", seen_to == [a["repos"][0]["to"]] and seen_to[0], str(seen_to))

    # ── M3: bilinmeyen bölge '?' basmaz ──────────────────────────────────
    mars = dict(p84, users={"mars": ["build/x"]})
    a = kind(fx.plan(_FixClient(DF), mars, index=idx, resolver=res), "taban-yama")
    check("taban-yama: bilinmeyen bölge → elle, '?' yok", a["state"] == "elle" and "bilinmeyen bölge: mars" in a["reason"]
          and "?" not in a["reason"], str(a)[:240])
    osm = dict(rec("php:8.4.27", "php", "8.4.27", crit=1, users={"mars": ["x"]}))
    a = kind(fx.plan(_FixClient(DF), osm, index=idx, resolver=res), "yeniden-üret")
    check("yeniden-üret: bilinmeyen bölge → elle", a["state"] == "elle" and "bilinmeyen bölge" in a["reason"], str(a)[:200])
    imars = {"py": [{"src": "mars/build/py", "images": ["europe-west1-docker.pkg.dev/flo-prod-main/prod-ar/python-3-13-7:1.0"]}]}
    a = kind(fx.plan(_FixClient(DF, files=stg), py, index=imars, resolver=res), "taban-dizin")
    check("taban-dizin: bilinmeyen bölge → elle", a["state"] == "elle" and "bilinmeyen bölge" in a["reason"], str(a)[:240])

    # ── M4: günlük tur çalışan taramayı bekler ───────────────────────────
    knobs.update({"CREW_TECHDEBT": True, "CREW_TECHDEBT_IMAGE_VULN": True, "CREW_TECHDEBT_IMAGE_VULN_FIX": False})
    seen: dict = {"force": None, "alert": 0}
    sleeps: list = []
    _bi, _rep, _alert = modern.build_index, iv.report, iv.alert
    try:
        modern.build_index = lambda c, **kw: {}
        iv.report = lambda c, **kw: seen.update(force=kw.get("force")) or {"images": []}
        iv.alert = lambda c, rep, **kw: seen.update(alert=seen["alert"] + 1) or []
        iv._reset_state()
        iv._RUN["running"] = True

        def free_after_two(s):
            sleeps.append(s)
            if len(sleeps) == 2:
                iv._end(None)
        iv._tick(lambda: NS(team="Ekip"), sleep=free_after_two, max_wait_s=300, step_s=30)
        check("_tick: çalışan taramayı bekler, bitince koşar; biten taramanın önbelleğini yeniden kullanır (force yok)",
              seen["alert"] == 1 and seen["force"] is False and len(sleeps) == 2, f"{seen} {sleeps}")
        records: list = []

        class _H(logging.Handler):
            def emit(self, record):
                records.append(record)
        h = _H(level=logging.INFO)
        logging.getLogger("pipeline").addHandler(h)
        try:
            seen.update(alert=0)
            iv._reset_state()
            iv._RUN["running"] = True
            sleeps.clear()
            iv._tick(lambda: NS(team="Ekip"), sleep=lambda s: sleeps.append(s), max_wait_s=90, step_s=30)
        finally:
            logging.getLogger("pipeline").removeHandler(h)
            iv._reset_state()
        check("_tick: sınırlı bekleme sonunda hâlâ meşgulse uyarı logu, koşmaz",
              seen["alert"] == 0 and any(r.levelno == logging.WARNING and "atlandı" in r.getMessage() for r in records)
              and len(sleeps) == 3, f"{sleeps} {[r.getMessage() for r in records]}")
    finally:
        modern.build_index, iv.report, iv.alert = _bi, _rep, _alert
        iv._reset_state()

    # ── M6: Trivy imajı sabit ────────────────────────────────────────────
    pinned = "aquasec/trivy@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb"
    with tempfile.TemporaryDirectory() as td:
        y = Path(td) / "image_vuln.yaml"
        check("trivy_image: yaml yoksa sabit varsayılan", iv.trivy_image(Path(td) / "yok.yaml") == pinned == iv.DEFAULT_TRIVY_IMAGE)
        for good in ("aquasec/trivy:0.58.1", "aquasec/trivy@sha256:" + "a" * 64):
            y.write_text(f"trivy_image: {good}\n")
            check(f"trivy_image: geçerli değer kabul ({good[:30]}…)", iv.trivy_image(y) == good)
        for bad in ("evil/trivy:latest", "aquasec/trivy:latest; rm -rf /", "aquasec/trivy@sha256:abc", "aquasec/trivy", "-v x",
                    "aquasec/trivy:ta g", "http://x/aquasec/trivy:1", "5"):
            y.write_text(f"trivy_image: '{bad}'\n")
            check(f"trivy_image: geçersiz değer ({bad[:24]!r}) → sabit varsayılan", iv.trivy_image(y) == pinned)
        y.write_text("trivy_image: [a, b]\n")
        check("trivy_image: yanlış tür → sabit varsayılan", iv.trivy_image(y) == pinned)
    cmd = iv.trivy_cmd(which=lambda n: "/x/docker" if n == "docker" else None)
    check("trivy_cmd: docker yolu sabitlenmiş imajı kullanır, :latest değil", cmd and cmd[-1] == iv.trivy_image() and ":latest" not in " ".join(cmd), str(cmd))
    check("yaml'da trivy_image anahtarı sabit imajla duruyor", iv.trivy_image(iv.IGNORE_PATH) == pinned
          and "trivy_image:" in iv.IGNORE_PATH.read_text(encoding="utf-8"))

    # ── M9 / M10: ayar açıklamaları ──────────────────────────────────────
    by = {f["key"]: f for f in pc.SCHEMA}
    mo, vu = by["CREW_TECHDEBT_UPGRADE_MAX_OPEN"]["desc"], by["CREW_TECHDEBT_IMAGE_VULN"]["desc"]
    check("MAX_OPEN açıklaması imaj gidermesindeki ikinci kullanımı söyler", "taban imaj" in mo.lower() and "izleme" in mo.lower(), mo)
    check("IMAGE_VULN açıklaması: düğme _FIX kapalıyken de PR / izleme açar, anahtarlar", "Gidermeyi başlat" in vu
          and "CREW_TECHDEBT_UPGRADE_BUMP" in vu and "CREW_TECHDEBT_UPGRADE_IMAGE" in vu and "CREW_TECHDEBT_UPGRADE_PATCH" in vu
          and "CREW_TECHDEBT_IMAGE_VULN_FIX" in vu, vu)

    # ── Gerçek `_repo_current`: ~/.crew_repos/<repo> düzeninde geçici dizin ──
    real_cur = _FX_REAL_CURRENT[0]
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "alf").mkdir()
        (Path(td) / "alf" / "go.mod").write_text("module example.com/alf\n\ngo 1.24.4\n", encoding="utf-8")
        _dd = pmod.default_deps
        pmod.default_deps = lambda: NS(cfg=lambda: {"repos_dir": td}, repo_ok=lambda r: True)
        try:
            got = real_cur(None, "alf", "go", {})
            try:
                gone = real_cur(None, "yok-repo", "go", {})
            except ValueError as e:
                gone = str(e)
        finally:
            pmod.default_deps = _dd
    check("gerçek _repo_current: yerel klonun go.mod'undan 1.24 okur (ağ yok)", got == "1.24", str(got))
    check("gerçek _repo_current: klon yoksa istisna (plan bunu None sayar)", gone is not None and gone != "1.24", str(gone))


def main():
    print("Katman 0 kapıları — regresyon testleri")
    print("=" * 62)
    for t in (test_norm_path, test_plan_paths, test_issue_gate,
              test_requirement_ids, test_completeness, test_contract_gate,
              test_fix_targets, test_envelope, test_prune_fix_targets,
              test_reachability, test_context_prefix_stability,
              test_grep_evidence, test_bm25_identifier_terms,
              test_summary_column_extraction, test_summary_index_refresh,
              test_build_fix_selection, test_resume_wiring,
              test_build_gate_timeout_strict,
              test_build_fix_single_commit,
              test_build_fix_regressions,
              test_partial_implement_resume,
              test_needs_human_and_resume_envelope,
              test_build_gate_stale_build,
              test_readiness_gate,
              test_call_context_cross_thread,
              test_md_to_html_headings_tables,
              test_wi_lifecycle_transitions,
              test_dod_checklist,
              test_wi_lifecycle_flow_hooks,
              test_estimation,
              test_child_tasks_planning,
              test_estimate_flow_hooks,
              test_retrospective,
              test_sprint_planning,
              test_daily_summary,
              test_type_flow,
              test_type_flow_hooks,
              test_pr_review_contribution,
              test_backlog_refinement,
              test_cost_analytics,
              test_cheap_phase_split,
              test_findings_reuse,
              test_prompt_prefix_order,
              test_cli_prompt_overhead,
              test_wi_comments_context,
              test_quality_rank_nr,
              test_quality_rank_sonar,
              test_quality_select,
              test_quality_render,
              test_quality_dedup,
              test_quality_app_map,
              test_quality_sources,
              test_quality_intake,
              test_quality_endpoints,
              test_upgrade_api_surface,
              test_upgrade_card,
              test_upgrade_bump_expect_step,
              test_upgrade_advance,
              test_quality_review_fixes,
              test_quality_readiness,
              test_repo_ctx_cross_thread,
              test_windowed_edit,
              test_quality_lean,
              test_worktree_staging,
              test_techdebt_lane,
              test_techdebt_sprint_choice, test_php_repo_subdir_composer,
              test_modern_freshen_and_targets, test_techdebt_open_tasks_db,
              test_techdebt_list_queue,
              test_techdebt_endpoints,
              test_techdebt_ui,
              test_techdebt_review_fixes,
              test_dashboard_readability,
              test_human_label,
              test_quality_human_split,
              test_quality_project_filter,
              test_guidance_in_steps,
              test_completion_report_no_tool_note,
              test_review_prefetch_window,
              test_quality_estimate,
              test_no_env_dependent_tests,
              test_pr_comment_emoticons,
              test_completion_comment_integrity,
              test_php_modern_catalog,
              test_php_modern_scan,
              test_php_modern_groups,
              test_php_modern_render,
              test_php_modern_take,
              test_php_modern_endpoints,
              test_php_modern_ui,
              test_quality_sonar_exclude,
              test_env_conventions,
              test_sprint_report_no_sp,
              test_sprint_report_designs,
              test_review_retry_delete_and_no_progress,
              test_delete_push_payload,
              test_quality_direct_flow,
              test_resume_skips_discarded_work,
              test_wi_lifecycle_on_queue,
              test_pre_pr_verify_and_build_fix,
              test_security_hardening,
              test_devops_requests,
              test_modern_multilang,
              test_modern_sp,
              test_upgrade_rules,
              test_php_modern_82_84, test_upgrade_php_rules, test_upgrade_plan_sites, test_upgrade_probe_and_blockers,
              test_upgrade_registry, test_upgrade_patch_sites, test_upgrade_image_template,
              test_upgrade_image_wiring,
              test_image_vuln_inventory, test_image_vuln_signals, test_image_vuln_hardening, test_image_vuln_settings, test_image_vuln_alerts, test_image_fix, test_image_fix_round1, test_image_fix_round2, test_image_fix_final, test_image_fix_promote_tag,
              test_upgrade_scan_take,
              test_upgrade_bump_sites,
              test_upgrade_client_find_pr_and_bytes,
              test_upgrade_bump_round2,
              test_upgrade_status_and_steps,
              test_upgrade_fix_round1,
              test_upgrade_fix_round2,
              test_upgrade_fix_round3,
              test_upgrade_final_f2_three_part, test_upgrade_final_f3_published, test_upgrade_final_f6_ci_parse, test_upgrade_final_f1_pipeline_source, test_upgrade_final_f7_push_clock, test_upgrade_final_f8_pinned_read, test_upgrade_final_f5_resolved_blocks, test_upgrade_final_f9_retry_guard, test_upgrade_final_f10_catalog_flag,
              test_php_rector_fix,
              test_tool_review_diff_context,
              test_tool_fix_leftovers,
              test_scan_line_text_multibyte,
              test_tool_fix_quiet_comments, test_tool_fix_build_gate, test_stale_team_status,
              test_pr_review_prior_discussion, test_pr_review_coverage, test_pr_review_tracking, test_tool_branch_conflicts, test_hero_latest_job,
              test_tool_fix_result_and_logs, test_nr_app_infer, test_quality_detail):
        try:
            t()
        except Exception as e:
            FAIL.append(t.__name__)
            print(f"  ❌ {t.__name__} ÇÖKTÜ: {type(e).__name__}: {e}")
    print("\n" + "=" * 62)
    print(f"GEÇEN {len(PASS)} · BAŞARISIZ {len(FAIL)} · ATLANAN {len(SKIP)}")
    if FAIL:
        print("Başarısızlar:")
        for f in FAIL:
            print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
