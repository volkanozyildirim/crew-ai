"""Local git repo yonetimi — Azure DevOps REST API yerine filesystem erisimi.

Repolari kalici dizinde tutar (~/.crew_repos/). Ilk kullanmda git clone,
sonraki kullanimlarda git fetch + checkout yapar. Dosya okuma, dizin listeleme
ve kod arama islemlerini local filesystem uzerinden gerceklestirir.
"""

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger("pipeline")


class LocalRepoManager:
    """Azure DevOps repolarini locale clone edip filesystem ile erisim saglar."""

    def __init__(self, base_dir: str | None = None):
        self.base_dir = Path(
            base_dir or os.environ.get("CREW_REPOS_DIR", "~/.crew_repos")
        ).expanduser()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._cloned: dict[str, Path] = {}
        self.vector_store = None  # VectorStore, flow.py tarafindan set edilir

    # ── Git Operasyonlari ───────────────────────────

    def _git(self, args: list[str], cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
        """Git komutu calistir."""
        cmd = ["git"] + args
        env = os.environ.copy()
        # SSL dogrulama kapatilmis olabilir (kurumsal proxy)
        env["GIT_SSL_NO_VERIFY"] = "true"
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,
        )

    def _auth_url(self, clone_url: str) -> str:
        """Clone URL'ine PAT ekle. Mevcut username varsa PAT ile degistirir."""
        pat = os.environ.get("AZURE_DEVOPS_PAT", "")
        if not pat:
            return clone_url
        # URL: https://OrgName@dev.azure.com/... → https://{PAT}@dev.azure.com/...
        # veya: https://dev.azure.com/... → https://{PAT}@dev.azure.com/...
        if "://" in clone_url:
            scheme, rest = clone_url.split("://", 1)
            # Mevcut username varsa kaldir
            if "@" in rest:
                rest = rest.split("@", 1)[1]
            return f"{scheme}://{pat}@{rest}"
        return clone_url

    def ensure_repo(self, repo_name: str, clone_url: str, fetch: bool = True) -> Path:
        """Repo yoksa clone et, varsa opsiyonel fetch et. Local path dondur.
        fetch=False → sadece clone (yoksa), fetch yapmaz — hizli init icin."""
        repo_dir = self.base_dir / repo_name

        if repo_dir.exists() and (repo_dir / ".git").exists():
            # Zaten var
            if fetch:
                log.info(f"  Local repo fetch: {repo_name}")
                result = self._git(["fetch", "--all", "--prune"], cwd=repo_dir)
                if result.returncode != 0:
                    log.warning(f"  git fetch hatasi: {result.stderr[:200]}")
            self._cloned[repo_name] = repo_dir
            # Summary yoksa olustur (fetch'ten bagimsiz)
            if not (repo_dir / "REPO_SUMMARY.md").exists():
                self.generate_repo_summary(repo_name)
            return repo_dir

        # Ilk kez — clone
        log.info(f"  Local repo clone: {repo_name} -> {repo_dir}")
        auth_url = self._auth_url(clone_url)
        result = self._git(
            ["clone", "--no-checkout", auth_url, str(repo_dir)],
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git clone basarisiz: {result.stderr[:500]}")

        # main branch'i checkout et
        self._git(["checkout", "main"], cwd=repo_dir)

        self._cloned[repo_name] = repo_dir

        # Repo summary olustur
        self.generate_repo_summary(repo_name)

        # Vector index olustur (ilk clone)
        if self.vector_store:
            try:
                self.vector_store.index_repo(repo_name, repo_dir)
            except Exception as e:
                log.warning(f"  Vector index hatasi ({repo_name}): {e}")

        return repo_dir

    def get_vendor_allowlist(self, repo_name: str) -> set[str]:
        """Vendor allowlist — composer.json/package.json/go.mod'dan + env override.

        Returns: relative path prefix set (orn: 'vendor/butterfly/framework',
        'node_modules/react', 'vendor/laravel/framework').

        Vector store bu prefix'lere uyan dosyalari index'e dahil eder.
        """
        repo_dir = self._get_repo_dir(repo_name)
        allow: set[str] = set()

        # composer.json
        cj = repo_dir / "composer.json"
        if cj.exists():
            try:
                import json as _json
                data = _json.loads(cj.read_text(encoding="utf-8", errors="replace"))
                req = data.get("require", {}) or {}
                req_dev = data.get("require-dev", {}) or {}
                for pkg in {**req, **req_dev}:
                    if not pkg or pkg in ("php",) or pkg.startswith("ext-"):
                        continue
                    allow.add(f"vendor/{pkg}")
            except Exception as e:
                log.warning(f"  composer.json parse hatasi ({repo_name}): {e}")

        # package.json
        pj = repo_dir / "package.json"
        if pj.exists():
            try:
                import json as _json
                data = _json.loads(pj.read_text(encoding="utf-8", errors="replace"))
                deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
                for pkg in deps:
                    if pkg:
                        allow.add(f"node_modules/{pkg}")
            except Exception as e:
                log.warning(f"  package.json parse hatasi ({repo_name}): {e}")

        # go.mod — vendor/ olusturulmussa modules.txt'ten direkt path alinabilir
        gomod = repo_dir / "vendor" / "modules.txt"
        if gomod.exists():
            try:
                for line in gomod.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("# "):
                        # "# github.com/user/pkg v1.2.3"
                        parts = line[2:].split()
                        if parts:
                            allow.add(f"vendor/{parts[0]}")
            except Exception:
                pass

        # User env override — comma-separated allowlist patterns
        extra = os.environ.get("CREW_VENDOR_INCLUDE", "")
        if extra:
            for pat in extra.split(","):
                pat = pat.strip()
                if pat:
                    allow.add(pat)

        return allow

    @staticmethod
    def _parse_required_php(repo_dir: Path) -> str | None:
        """composer.json + composer.lock'tan gerekli PHP versiyonunu (X.Y) cikar.

        Oncelik:
          1. composer.json: config.platform.php (kesin)
          2. composer.json: require.php (project constraint)
          3. composer.lock: paketlerden en strict php constraint
        """
        import json as _json
        import re as _re_php

        cj_path = repo_dir / "composer.json"
        if cj_path.exists():
            try:
                cj = _json.loads(cj_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                cj = {}

            platform = (cj.get("config") or {}).get("platform", {})
            if isinstance(platform, dict):
                p = platform.get("php")
                if p:
                    m = _re_php.match(r"^(\d+\.\d+)", str(p))
                    if m:
                        return m.group(1)

            req = cj.get("require", {}) or {}
            p = req.get("php")
            if p:
                m = _re_php.search(r"(\d+\.\d+)", str(p))
                if m:
                    return m.group(1)

        # composer.lock — paketlerden PHP constraint topla
        lock_path = repo_dir / "composer.lock"
        if lock_path.exists():
            try:
                lock = _json.loads(lock_path.read_text(encoding="utf-8", errors="replace"))
                versions = []
                for pkg in (lock.get("packages") or []) + (lock.get("packages-dev") or []):
                    php_req = (pkg.get("require") or {}).get("php")
                    if php_req:
                        # '^8.4', '>=8.2 <9.0' vb. ilk X.Y'i al
                        m = _re_php.search(r"(\d+\.\d+)", str(php_req))
                        if m:
                            try:
                                major, minor = m.group(1).split(".")
                                versions.append((int(major), int(minor)))
                            except ValueError:
                                continue
                if versions:
                    # En yuksek minimum'u sec — strict constraint kazanir
                    versions.sort(reverse=True)
                    return f"{versions[0][0]}.{versions[0][1]}"
            except Exception:
                pass

        return None

    @staticmethod
    def _parse_php_version_from_error(stderr: str) -> str | None:
        """Composer error mesajinda 'requires php ^8.4' / 'php >=8.2' gibi
        constraint'lerden X.Y'i cikarir."""
        import re as _re_err
        # Birkac yaygin pattern
        patterns = [
            r"requires php\s+[\^~]?(\d+\.\d+)",
            r"requires\s+php\s+[\^~]?>?=?\s*(\d+\.\d+)",
            r"php\s+[\^~](\d+\.\d+)",
        ]
        for pat in patterns:
            m = _re_err.search(pat, stderr, _re_err.IGNORECASE)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _find_php_binary(version: str) -> str | None:
        """X.Y PHP versiyonu icin executable bul. Brew ya da sistem php."""
        import glob as _glob
        candidates = [
            f"/opt/homebrew/opt/php@{version}/bin/php",
            f"/usr/local/opt/php@{version}/bin/php",
            f"/opt/homebrew/Cellar/php@{version}/*/bin/php",
            f"/opt/homebrew/Cellar/php/{version}.*/bin/php",
        ]
        for cand in candidates:
            if "*" in cand:
                for m in _glob.glob(cand):
                    if os.path.isfile(m):
                        return m
            elif os.path.isfile(cand):
                return cand

        # Sistem php versiyonu uyuyor mu?
        try:
            r = subprocess.run(
                ["php", "-r", "echo PHP_MAJOR_VERSION.'.'.PHP_MINOR_VERSION;"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip() == version:
                return "php"
        except Exception:
            pass
        return None

    @staticmethod
    def _try_brew_install_php(version: str) -> bool:
        """brew ile php@X.Y yuklemeyi dene. Returns True if successful."""
        log.info(f"  PHP {version} brew ile yukleniyor (5-10dk surebilir)...")
        try:
            r = subprocess.run(
                ["brew", "install", f"php@{version}"],
                capture_output=True, text=True, timeout=900,
            )
            if r.returncode == 0:
                log.info(f"  PHP {version} kuruldu")
                return True
            log.warning(f"  brew install php@{version} hatasi: {(r.stderr or r.stdout)[-300:]}")
            return False
        except FileNotFoundError:
            log.warning("  brew PATH'te yok — PHP versiyonu otomatik yuklenmedi")
            return False
        except subprocess.TimeoutExpired:
            log.warning(f"  brew install php@{version} timeout")
            return False

    def install_dependencies(self, repo_name: str, *, force: bool = False) -> dict:
        """Repo yapisina gore deps install et — vendor/ klasorunu olusturur.

        Detect:
          composer.json -> `composer install --no-dev --no-interaction --no-progress`
                           (PHP versiyonu composer.json'dan tespit edilir;
                            yoksa brew ile yuklenmeye calisilir)
          go.mod        -> `go mod vendor` (vendor/ olusturur)
          package.json  -> `npm install --silent --no-audit --no-fund`
          requirements.txt -> opsiyonel (default skip — sistem genelinde etkili)

        force=False ise zaten vendor/node_modules varsa atlar.
        Returns: {success, manager, elapsed_s, message, php_version?}
        """
        import shutil as _shutil
        import time as _t
        repo_dir = self._get_repo_dir(repo_name)

        # composer (PHP)
        if (repo_dir / "composer.json").exists():
            if not force and (repo_dir / "vendor").exists():
                return {"success": True, "manager": "composer", "elapsed_s": 0, "message": "vendor/ zaten var"}

            # PHP versiyon tespiti
            required_php = self._parse_required_php(repo_dir)
            php_bin = None
            php_msg = ""
            if required_php:
                log.info(f"  composer.json PHP {required_php} bekliyor")
                php_bin = self._find_php_binary(required_php)
                if not php_bin:
                    log.info(f"  PHP {required_php} bulunamadi, brew install deneniyor")
                    if self._try_brew_install_php(required_php):
                        php_bin = self._find_php_binary(required_php)
                if php_bin:
                    php_msg = f" PHP={required_php} ({php_bin})"
                else:
                    log.warning(f"  PHP {required_php} yuklenemedi — sistem php ile devam (--ignore-platform-reqs)")

            # Composer cagrisi: dogru PHP versiyonu varsa onunla, yoksa default
            composer_path = _shutil.which("composer")
            if not composer_path:
                return {"success": False, "manager": "composer", "elapsed_s": 0,
                        "message": "'composer' PATH'te yok"}

            if php_bin and php_bin != "php":
                cmd = [
                    php_bin, composer_path, "install",
                    "--no-dev", "--no-interaction", "--no-progress", "--prefer-dist",
                ]
            else:
                # Sistem php — versiyon uyumsuzlugu olabilir, ignore-platform-reqs ile geç
                cmd = [
                    "composer", "install",
                    "--no-dev", "--no-interaction", "--no-progress", "--prefer-dist",
                    "--ignore-platform-reqs",
                ]

            log.info(f"  composer install: {repo_name} (vendor olusturuluyor){php_msg}")
            t0 = _t.time()
            # Buyuk projeler (private packagist + SSH git repo) 10dk'dan uzun
            # surebilir. 30dk timeout — pipeline yine de cok takilirsa knob ile kontrol edilir.
            COMPOSER_TIMEOUT = int(os.environ.get("CREW_COMPOSER_TIMEOUT", "1800"))

            def _run_composer(c):
                return subprocess.run(c, cwd=repo_dir, capture_output=True, text=True, timeout=COMPOSER_TIMEOUT)

            try:
                result = _run_composer(cmd)

                # Hata + PHP version sebebi mi?
                if result.returncode != 0:
                    full_err = (result.stderr or "") + "\n" + (result.stdout or "")
                    detected_php = self._parse_php_version_from_error(full_err)
                    if detected_php and (not required_php or detected_php != required_php):
                        log.info(f"  composer error -> PHP {detected_php} bekleniyor, retry deneniyor")
                        retry_php_bin = self._find_php_binary(detected_php)
                        if not retry_php_bin:
                            log.info(f"  PHP {detected_php} brew ile yukleniyor")
                            if self._try_brew_install_php(detected_php):
                                retry_php_bin = self._find_php_binary(detected_php)
                        if retry_php_bin:
                            cmd = [
                                retry_php_bin, composer_path, "install",
                                "--no-dev", "--no-interaction", "--no-progress", "--prefer-dist",
                            ]
                            log.info(f"  composer install retry: PHP={detected_php} ({retry_php_bin})")
                            result = _run_composer(cmd)
                            required_php = detected_php
                            php_msg = f" PHP={detected_php} ({retry_php_bin})"

                elapsed = _t.time() - t0
                if result.returncode != 0:
                    err = (result.stderr or result.stdout)[-400:]
                    log.warning(f"  composer install hatasi ({repo_name}): {err[:200]}")
                    return {"success": False, "manager": "composer", "elapsed_s": elapsed,
                            "message": err[:400], "php_version": required_php}
                log.info(f"  composer install OK ({elapsed:.0f}s){php_msg}")
                return {"success": True, "manager": "composer", "elapsed_s": elapsed,
                        "message": f"vendor/ olusturuldu{php_msg}", "php_version": required_php}
            except FileNotFoundError:
                return {"success": False, "manager": "composer", "elapsed_s": 0,
                        "message": "'composer' calistirilamadi"}
            except subprocess.TimeoutExpired:
                return {"success": False, "manager": "composer", "elapsed_s": COMPOSER_TIMEOUT,
                        "message": f"timeout ({COMPOSER_TIMEOUT}s) — CREW_COMPOSER_TIMEOUT ile artir"}

        # Go modules
        if (repo_dir / "go.mod").exists():
            if not force and (repo_dir / "vendor").exists():
                return {"success": True, "manager": "go", "elapsed_s": 0, "message": "vendor/ zaten var"}
            log.info(f"  go mod vendor: {repo_name}")
            t0 = _t.time()
            try:
                result = subprocess.run(
                    ["go", "mod", "vendor"],
                    cwd=repo_dir, capture_output=True, text=True, timeout=600,
                )
                elapsed = _t.time() - t0
                if result.returncode != 0:
                    err = (result.stderr or result.stdout)[-300:]
                    log.warning(f"  go mod vendor hatasi ({repo_name}): {err[:200]}")
                    return {"success": False, "manager": "go", "elapsed_s": elapsed, "message": err[:300]}
                log.info(f"  go mod vendor OK ({elapsed:.0f}s)")
                return {"success": True, "manager": "go", "elapsed_s": elapsed, "message": "vendor/ olusturuldu"}
            except FileNotFoundError:
                return {"success": False, "manager": "go", "elapsed_s": 0, "message": "'go' PATH'te yok"}
            except subprocess.TimeoutExpired:
                return {"success": False, "manager": "go", "elapsed_s": 600, "message": "timeout"}

        # npm/yarn
        if (repo_dir / "package.json").exists():
            if not force and (repo_dir / "node_modules").exists():
                return {"success": True, "manager": "npm", "elapsed_s": 0, "message": "node_modules/ zaten var"}
            log.info(f"  npm install: {repo_name} (node_modules olusturuluyor)")
            t0 = _t.time()
            try:
                # yarn.lock varsa yarn, yoksa npm
                use_yarn = (repo_dir / "yarn.lock").exists()
                cmd = ["yarn", "install", "--silent"] if use_yarn else \
                      ["npm", "install", "--silent", "--no-audit", "--no-fund"]
                result = subprocess.run(
                    cmd, cwd=repo_dir, capture_output=True, text=True, timeout=900,
                )
                elapsed = _t.time() - t0
                if result.returncode != 0:
                    err = (result.stderr or result.stdout)[-300:]
                    log.warning(f"  {cmd[0]} install hatasi ({repo_name}): {err[:200]}")
                    return {"success": False, "manager": cmd[0], "elapsed_s": elapsed, "message": err[:300]}
                log.info(f"  {cmd[0]} install OK ({elapsed:.0f}s)")
                return {"success": True, "manager": cmd[0], "elapsed_s": elapsed, "message": "node_modules/ olusturuldu"}
            except FileNotFoundError:
                return {"success": False, "manager": "npm", "elapsed_s": 0, "message": "'npm/yarn' PATH'te yok"}
            except subprocess.TimeoutExpired:
                return {"success": False, "manager": "npm", "elapsed_s": 900, "message": "timeout (15dk)"}

        # requirements.txt — sistem-wide etkili oldugu icin opsiyonel/skip
        # Pipeline'in calistigi venv'i kirletir. Eger gerekirse force ile manuel calistir.

        return {"success": True, "manager": "none", "elapsed_s": 0, "message": "Bilinen package manager bulunamadi"}

    def checkout(self, repo_name: str, branch: str) -> Path:
        """Branch'e switch et. Remote'da varsa tracking branch olustur."""
        repo_dir = self._get_repo_dir(repo_name)

        # Dirty state varsa temizle (pipeline her zaman clean state ister)
        self._git(["checkout", "--", "."], cwd=repo_dir)
        self._git(["clean", "-fd"], cwd=repo_dir)

        # Oncelikle local branch var mi bak
        result = self._git(["checkout", branch], cwd=repo_dir)
        if result.returncode == 0:
            # Local branch vardi, pull ile guncelle
            self._git(["pull", "--ff-only"], cwd=repo_dir)
            return repo_dir

        # Local yoksa remote'dan olustur
        result = self._git(
            ["checkout", "-b", branch, f"origin/{branch}"],
            cwd=repo_dir,
        )
        if result.returncode != 0:
            # Remote'da da yoksa (yeni branch), main'den olustur
            self._git(["checkout", "main"], cwd=repo_dir)
            log.info(f"  Branch '{branch}' remote'da yok, main uzerinde calisiliyor")

        return repo_dir

    # ── Dosya Operasyonlari ─────────────────────────

    def get_file_content(self, repo_name: str, file_path: str, branch: str | None = None) -> str:
        """Local dosya oku. Branch verilmisse o branch'e checkout eder."""
        repo_dir = self._get_repo_dir(repo_name)
        if branch:
            self.checkout(repo_name, branch)

        # file_path basta / olabilir, normalize et
        clean_path = file_path.lstrip("/")
        full_path = repo_dir / clean_path

        if not full_path.exists():
            raise FileNotFoundError(f"Dosya bulunamadi: {clean_path} ({repo_name})")
        if not full_path.is_file():
            raise IsADirectoryError(f"Dizin, dosya degil: {clean_path} ({repo_name})")

        return full_path.read_text(encoding="utf-8", errors="replace")

    def get_items_in_path(
        self,
        repo_name: str,
        path: str = "/",
        branch: str | None = None,
        recursion_level: str = "oneLevel",
    ) -> list[dict]:
        """Local dizin listele. Azure API uyumlu format dondurur."""
        repo_dir = self._get_repo_dir(repo_name)
        if branch:
            self.checkout(repo_name, branch)

        clean_path = path.lstrip("/")
        target_dir = repo_dir / clean_path if clean_path else repo_dir

        if not target_dir.exists():
            return []

        items = []
        # Kendisini de ekle (Azure API davranisi)
        items.append({
            "path": f"/{clean_path}" if clean_path else "/",
            "isFolder": True,
        })

        if recursion_level == "oneLevel":
            for entry in sorted(target_dir.iterdir()):
                if entry.name.startswith("."):
                    continue
                rel = entry.relative_to(repo_dir)
                items.append({
                    "path": f"/{rel}",
                    "isFolder": entry.is_dir(),
                })
        else:
            # Full recursion
            for entry in sorted(target_dir.rglob("*")):
                if any(p.startswith(".") for p in entry.parts):
                    continue
                rel = entry.relative_to(repo_dir)
                items.append({
                    "path": f"/{rel}",
                    "isFolder": entry.is_dir(),
                })

        return items

    def search_code(self, repo_name: str, search_text: str) -> list[dict]:
        """grep -rn ile kod ara. Azure Search API uyumlu format dondurur."""
        repo_dir = self._get_repo_dir(repo_name)

        result = self._git(
            ["grep", "-rn", "--no-color", "-I", search_text],
            cwd=repo_dir,
        )

        if result.returncode != 0:
            return []

        items = []
        for line in result.stdout.strip().split("\n")[:25]:
            if not line.strip():
                continue
            # Format: file_path:line_number:content
            parts = line.split(":", 2)
            if len(parts) >= 2:
                fpath = parts[0]
                items.append({
                    "repository": {"name": repo_name},
                    "path": f"/{fpath}",
                    "matches": {"content": [{"text": line}]},
                })

        return items

    def file_exists(self, repo_name: str, file_path: str, branch: str | None = None) -> bool:
        """Dosya var mi kontrol et."""
        repo_dir = self._get_repo_dir(repo_name)
        if branch:
            self.checkout(repo_name, branch)
        clean_path = file_path.lstrip("/")
        return (repo_dir / clean_path).is_file()

    def repo_path(self, repo_name: str) -> Path:
        """Repo'nun local path'ini dondur."""
        return self._get_repo_dir(repo_name)

    # ── Repo Summary ──────────────────────────────────

    def generate_repo_summary(self, repo_name: str) -> str:
        """Repo'nun NE yaptigini anlatan semantic-arama dostu summary olustur.
        Odak: framework + repo purpose + anlamli klasor isimleri (Controller, Widget, Service, Model)."""
        import json as _json
        import re as _re

        repo_dir = self._get_repo_dir(repo_name)
        lines = [f"# {repo_name}\n"]

        # ── Framework Tespiti ──
        lang = "Bilinmiyor"
        framework = ""
        pkg_manager = ""
        description = ""
        keywords_list = []

        # composer.json
        cj = None
        if (repo_dir / "composer.json").exists():
            lang = "PHP"
            pkg_manager = "Composer"
            try:
                cj = _json.loads((repo_dir / "composer.json").read_text(encoding="utf-8", errors="replace"))
                req = cj.get("require", {})
                if "laravel/framework" in req:
                    framework = f"Laravel {req['laravel/framework']}"
                elif "butterfly/framework" in req or any("butterfly" in k for k in req):
                    framework = "Butterfly"
                else:
                    framework = "PHP"
                # description ve keywords (generic default'lari filtrele)
                desc = (cj.get("description") or "").strip()
                if desc and "create new project" not in desc.lower() and "todo" not in desc.lower() and len(desc) > 10:
                    description = desc
                kws = cj.get("keywords", [])
                if kws:
                    keywords_list = [k for k in kws if k and len(k) < 30]
            except Exception:
                pass
        # go.mod
        elif (repo_dir / "go.mod").exists():
            lang = "Go"
            pkg_manager = "Go Modules"
            try:
                mod_text = (repo_dir / "go.mod").read_text(encoding="utf-8", errors="replace")
                if "gin-gonic" in mod_text:
                    framework = "Gin"
                elif "echo" in mod_text:
                    framework = "Echo"
                elif "fiber" in mod_text:
                    framework = "Fiber"
                else:
                    framework = "Go"
            except Exception:
                pass
        # package.json
        elif (repo_dir / "package.json").exists():
            lang = "JavaScript/TypeScript"
            pkg_manager = "npm"
            try:
                pj = _json.loads((repo_dir / "package.json").read_text(encoding="utf-8", errors="replace"))
                deps = {**pj.get("dependencies", {}), **pj.get("devDependencies", {})}
                if "next" in deps:
                    framework = "Next.js"
                elif "react" in deps:
                    framework = "React"
                elif "vue" in deps:
                    framework = "Vue"
                elif "express" in deps:
                    framework = "Express"
                else:
                    framework = "Node.js"
                desc = (pj.get("description") or "").strip()
                if desc and len(desc) > 10:
                    description = desc
                kws = pj.get("keywords", [])
                if kws:
                    keywords_list = [k for k in kws if k and len(k) < 30]
            except Exception:
                pass
        elif (repo_dir / "requirements.txt").exists() or (repo_dir / "pyproject.toml").exists():
            lang = "Python"
            pkg_manager = "pip"
            framework = "Python"

        # ── README / description arama ──
        readme_excerpt = ""
        for rname in ("README.md", "README.MD", "Readme.md", "README", "README.txt"):
            rpath = repo_dir / rname
            if rpath.exists():
                try:
                    txt = rpath.read_text(encoding="utf-8", errors="replace")
                    # Basligi ve ilk paragraflari al
                    # Kod bloklarini cikar
                    txt = _re.sub(r'```.*?```', '', txt, flags=_re.DOTALL)
                    # Baslik isaretlerini kaldir ama icerigi tut
                    txt = _re.sub(r'^#+\s*', '', txt, flags=_re.MULTILINE)
                    # Ilk 1200 karakter, bos satirlari sıkıştır
                    txt = _re.sub(r'\n\s*\n+', '\n\n', txt).strip()
                    readme_excerpt = txt[:1200]
                except Exception:
                    pass
                break

        # ── Framework ve ozet ──
        lines.append("## Ozet")
        lines.append(f"- **Dil**: {lang}" + (f" / **Framework**: {framework}" if framework else ""))
        if description:
            lines.append(f"- **Aciklama**: {description}")
        if keywords_list:
            lines.append(f"- **Keywords**: {', '.join(keywords_list[:10])}")
        lines.append("")

        # ── README özeti (varsa — en değerli semantic sinyal) ──
        if readme_excerpt:
            lines.append("## README")
            lines.append(readme_excerpt)
            lines.append("")

        # ── Domain Sinyalleri: Controller / Module / Widget / Service isimleri ──
        # Bunlar repo'nun NE yaptigini dogrudan gosterir
        signals = self._extract_domain_signals(repo_dir)
        if signals:
            lines.append("## Domain Bilesenleri")
            for category, items in signals.items():
                if items:
                    lines.append(f"- **{category}**: {', '.join(items[:15])}")
            lines.append("")

        # ── Top-level dizinler (sadece 1 seviye) ──
        top_dirs = []
        for entry in sorted(repo_dir.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                if entry.name not in ("vendor", "node_modules", "storage", "cache", "logs", "public", "bin"):
                    top_dirs.append(entry.name)
        if top_dirs:
            lines.append(f"## Ust Seviye Dizinler\n{', '.join(top_dirs[:20])}\n")

        # ── Onemli Dependencies (sadece anlam ifade edenler) ──
        meaningful_deps = self._extract_meaningful_deps(repo_dir)
        if meaningful_deps:
            lines.append("## Onemli Bagimliliklar")
            lines.append(", ".join(meaningful_deps[:15]))
            lines.append("")

        content = "\n".join(lines)

        # REPO_SUMMARY.md olarak yaz (.gitignore'da olmadigi icin git'e girmez — .git disi)
        summary_path = repo_dir / "REPO_SUMMARY.md"
        summary_path.write_text(content, encoding="utf-8")
        log.info(f"  Repo summary olusturuldu: {summary_path} ({len(content)} karakter)")

        return content

    def _extract_domain_signals(self, repo_dir: Path) -> dict[str, list[str]]:
        """Controller/Widget/Module/Service isimleri repo'nun NE yaptigini dogrudan gosterir.
        Bu isimleri extract ederek semantic arama icin ayirt edici sinyal olusturur."""
        import re as _re
        signals: dict[str, list[str]] = {}

        # Aranacak domain dizinleri ve kategorileri
        domain_dirs = {
            "Controller": ["app/Controller", "app/Controllers", "src/Controller", "Controllers"],
            "Widget": ["app/Widget", "app/Widgets", "src/Widget"],
            "Module": ["app/Module", "Modules", "src/modules"],
            "Service": ["app/Service", "app/Services", "src/services", "internal/service"],
            "Model": ["app/Model", "app/Models", "src/models"],
            "Command": ["app/Command", "app/Commands", "cmd"],
            "Handler": ["internal/handler", "src/handlers", "pkg/handler"],
            "Route": ["routes"],
        }

        for category, candidates in domain_dirs.items():
            items: list[str] = []
            for cand in candidates:
                target = repo_dir / cand
                if not target.exists() or not target.is_dir():
                    continue
                try:
                    # Bir seviye altindaki klasor + dosya isimleri
                    for sub in sorted(target.iterdir()):
                        if sub.name.startswith(".") or sub.name.startswith("_"):
                            continue
                        # Dosya ise extension'i kaldir
                        name = sub.stem if sub.is_file() else sub.name
                        # "Api" gibi 2 karakterli generic isimleri atla
                        if len(name) < 3:
                            continue
                        # CamelCase veya snake_case isimleri dusun
                        if _re.match(r'^[A-Za-z][A-Za-z0-9_]+$', name):
                            items.append(name)
                except PermissionError:
                    continue
                if items:
                    break  # ilk bulunan dizinden al, birden fazla aranmasin

            if items:
                # Tekrarsız
                seen = []
                for it in items:
                    if it not in seen:
                        seen.append(it)
                signals[category] = seen[:25]

        return signals

    def _extract_meaningful_deps(self, repo_dir: Path) -> list[str]:
        """Sadece is mantigiyla ilgili bagimliliklari dondur — genel framework/util paketlerini atla."""
        import json as _json

        # Atlanacak generic paketler
        skip_patterns = [
            "php", "ext-", "symfony/polyfill", "psr/", "phpunit/",
            "typescript", "eslint", "prettier", "webpack", "babel",
            "chai", "mocha", "jest", "@types/",
            "testify", "mock", "fmt", "strings", "bytes",
        ]

        deps = []

        # composer.json
        if (repo_dir / "composer.json").exists():
            try:
                cj = _json.loads((repo_dir / "composer.json").read_text(encoding="utf-8", errors="replace"))
                for pkg in cj.get("require", {}).keys():
                    if not any(p in pkg.lower() for p in skip_patterns):
                        deps.append(pkg)
            except Exception:
                pass

        # package.json
        if (repo_dir / "package.json").exists():
            try:
                pj = _json.loads((repo_dir / "package.json").read_text(encoding="utf-8", errors="replace"))
                for pkg in pj.get("dependencies", {}).keys():
                    if not any(p in pkg.lower() for p in skip_patterns):
                        deps.append(pkg)
            except Exception:
                pass

        # go.mod
        if (repo_dir / "go.mod").exists():
            try:
                import re as _re
                mod_text = (repo_dir / "go.mod").read_text(encoding="utf-8", errors="replace")
                for m in _re.finditer(r'^\s*([a-z0-9\-\.\/]+)\s+v', mod_text, _re.MULTILINE):
                    pkg = m.group(1)
                    if not any(p in pkg.lower() for p in skip_patterns):
                        deps.append(pkg)
            except Exception:
                pass

        return deps

    def get_repo_summary(self, repo_name: str) -> str:
        """REPO_SUMMARY.md varsa icerigini dondurur, yoksa bos string."""
        try:
            repo_dir = self._get_repo_dir(repo_name)
            summary_path = repo_dir / "REPO_SUMMARY.md"
            if summary_path.exists():
                return summary_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
        return ""

    # ── Internal ────────────────────────────────────

    def _get_repo_dir(self, repo_name: str) -> Path:
        """Repo dizinini dondur. Clone edilmemisse hata verir."""
        if repo_name in self._cloned:
            return self._cloned[repo_name]
        # Belki onceki session'dan kalmis
        repo_dir = self.base_dir / repo_name
        if repo_dir.exists() and (repo_dir / ".git").exists():
            self._cloned[repo_name] = repo_dir
            return repo_dir
        raise RuntimeError(
            f"Repo '{repo_name}' henuz clone edilmedi. "
            f"ensure_repo() cagrilmali."
        )
