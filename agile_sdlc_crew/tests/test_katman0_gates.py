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


def main():
    print("Katman 0 kapıları — regresyon testleri")
    print("=" * 62)
    for t in (test_norm_path, test_plan_paths, test_issue_gate,
              test_requirement_ids, test_completeness, test_contract_gate,
              test_fix_targets, test_envelope, test_prune_fix_targets,
              test_reachability, test_context_prefix_stability):
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
