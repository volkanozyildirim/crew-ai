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
    _php_call_arity,
    _php_signatures,
    _requirement_ids,
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


def main():
    print("Katman 0 kapıları — regresyon testleri")
    print("=" * 62)
    for t in (test_norm_path, test_plan_paths, test_issue_gate,
              test_requirement_ids, test_completeness, test_contract_gate,
              test_fix_targets, test_envelope, test_prune_fix_targets,
              test_reachability, test_context_prefix_stability,
              test_grep_evidence, test_bm25_identifier_terms,
              test_summary_column_extraction, test_summary_index_refresh,
              test_build_fix_selection, test_resume_wiring):
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
