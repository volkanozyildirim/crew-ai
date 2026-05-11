"""Agile SDLC Crew - Always-on FastAPI server with job queue."""

import json
import logging
import logging.handlers
import os
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
except ImportError:
    pass

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from agile_sdlc_crew import db
from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient


# ── Logging Setup ──

LOG_DIR = Path("/tmp")
ACCESS_LOG = LOG_DIR / "crew_access.log"
PIPELINE_LOG = LOG_DIR / "crew_pipeline.log"

def _setup_logging():
    """Access ve pipeline loglarını ayrı dosyalara yaz."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # Access logger (uvicorn HTTP request'leri)
    access_logger = logging.getLogger("uvicorn.access")
    access_handler = logging.handlers.RotatingFileHandler(
        ACCESS_LOG, maxBytes=5_000_000, backupCount=3, encoding="utf-8",
    )
    access_handler.setFormatter(fmt)
    access_logger.addHandler(access_handler)

    # Pipeline logger (iş adımları, agent çıktıları)
    pipeline_logger = logging.getLogger("pipeline")
    pipeline_logger.setLevel(logging.INFO)
    pipeline_handler = logging.handlers.RotatingFileHandler(
        PIPELINE_LOG, maxBytes=10_000_000, backupCount=5, encoding="utf-8",
    )
    pipeline_handler.setFormatter(fmt)
    pipeline_logger.addHandler(pipeline_handler)
    # Pipeline logları console'a da yazsın
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    pipeline_logger.addHandler(console_handler)

    return pipeline_logger

pipeline_log = _setup_logging()


app = FastAPI(title="Agile SDLC Crew", version="3.0")

WEB_DIR = Path(__file__).parent / "web"
STATUS_FILE = WEB_DIR / "status.json"

# Queue worker thread kontrolu
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


import re as _re

class RunRequest(BaseModel):
    work_item_id: str
    use_hal: bool = False

    def validate_wi(self) -> str | None:
        """Work item ID dogrulama. Sadece rakam kabul et."""
        wi = self.work_item_id.strip()
        if not wi or not _re.match(r'^\d{1,10}$', wi):
            return "Gecersiz Work Item ID (sadece rakam, max 10 hane)"
        return None


class PRFixRequest(BaseModel):
    repo_name: str
    pr_id: int
    work_item_id: str = ""  # opsiyonel — PR'dan da cikarilabilir


# ── API Routes ──

@app.get("/")
async def dashboard():
    return FileResponse(str(WEB_DIR / "index.html"))


@app.get("/api/health")
async def health():
    stats = db.get_queue_stats()
    return JSONResponse({"status": "ok", **stats})


@app.get("/api/status")
async def get_status():
    """Aktif is varsa onun durumunu, yoksa bos dondur."""
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            return JSONResponse(data)
        except (json.JSONDecodeError, OSError):
            pass
    return JSONResponse({"status": "idle"})


@app.post("/api/run")
async def queue_job(req: RunRequest):
    """Is kuyruğa ekle. Birden fazla is eklenebilir."""
    err = req.validate_wi()
    if err:
        return JSONResponse({"error": err}, status_code=400)
    job_id = db.create_job(req.work_item_id.strip(), req.use_hal)
    _ensure_worker()
    return JSONResponse({
        "job_id": job_id,
        "status": "queued",
        "message": f"#{req.work_item_id} kuyruga eklendi",
    }, status_code=202)


@app.post("/api/pr-fix")
async def pr_fix(req: PRFixRequest):
    """PR yorumlarindaki geri bildirimlere gore kodu duzelt.
    Mevcut branch'teki kodu okur, sadece eksikleri tamamlar, push eder."""
    job_id = db.create_job(req.work_item_id or f"PR#{req.pr_id}", use_hal=False)
    # PR fix bilgisini job metadata olarak sakla
    db.update_job(job_id, pr_id=str(req.pr_id), repo_name=req.repo_name)

    def _run_pr_fix():
        try:
            db.start_job(job_id)
            from agile_sdlc_crew.pr_fix import run_pr_fix
            result = run_pr_fix(req.repo_name, req.pr_id, req.work_item_id)
            db.complete_job(job_id)
            pipeline_log.info(f"PR-fix #{job_id} (PR #{req.pr_id}) tamamlandi: {result}")
        except Exception as e:
            db.fail_job(job_id, str(e))
            pipeline_log.error(f"PR-fix #{job_id} (PR #{req.pr_id}) basarisiz: {e}")

    fix_thread = threading.Thread(target=_run_pr_fix, daemon=True)
    fix_thread.start()

    return JSONResponse({
        "job_id": job_id,
        "status": "started",
        "message": f"PR #{req.pr_id} fix basladi",
    }, status_code=202)


@app.get("/api/jobs")
async def list_jobs():
    """Tum isleri listele."""
    jobs = db.get_all_jobs()
    # datetime serialization
    for j in jobs:
        for k in ("created_at", "started_at", "finished_at"):
            if j.get(k) and isinstance(j[k], datetime):
                j[k] = j[k].isoformat()
    return JSONResponse(jobs)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int):
    """Tek is detayi + step'ler."""
    job = db.get_job(job_id)
    if not job:
        return JSONResponse({"error": "Job bulunamadi"}, status_code=404)
    for k in ("created_at", "started_at", "finished_at"):
        if job.get(k) and isinstance(job[k], datetime):
            job[k] = job[k].isoformat()
    for s in job.get("steps", []):
        for k in ("started_at", "finished_at"):
            if s.get(k) and isinstance(s[k], datetime):
                s[k] = s[k].isoformat()
    return JSONResponse(job)


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: int):
    """Hata alan bir isi yeniden kuyruga ekle (yeni job olarak)."""
    job = db.get_job(job_id)
    if not job:
        return JSONResponse({"error": "Job bulunamadi"}, status_code=404)
    if job["status"] not in ("failed", "completed"):
        return JSONResponse(
            {"error": f"Sadece failed/completed isler retry edilebilir (durum: {job['status']})"},
            status_code=409,
        )
    new_job_id = db.create_job(
        job["work_item_id"],
        bool(job.get("use_hal", False)),
        job.get("wi_title", ""),
    )
    _ensure_worker()
    return JSONResponse({
        "job_id": new_job_id,
        "status": "queued",
        "message": f"Job #{job_id} → #{new_job_id} olarak kuyruga eklendi",
    }, status_code=202)


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: int, force: bool = False):
    """İş sil. force=true ile takılı kalmış running işler de silinebilir."""
    job = db.get_job(job_id)
    if not job:
        return JSONResponse({"error": "Job bulunamadı"}, status_code=404)
    if job["status"] == "running" and not force:
        return JSONResponse(
            {"error": "Çalışan iş silinemez. Zorla silmek için force=true kullanın."},
            status_code=409,
        )
    if job["status"] == "running":
        db.fail_job(job_id, "Kullanıcı tarafından iptal edildi")
    db.delete_job(job_id)
    return JSONResponse({"status": "ok", "message": f"Job #{job_id} silindi"})


@app.get("/api/queue")
async def queue_stats():
    """Kuyruk istatistikleri."""
    return JSONResponse(db.get_queue_stats())


@app.post("/api/reset")
async def reset_status():
    """Dashboard sifirla."""
    empty = {
        "work_item_id": "", "started_at": "", "finished_at": "",
        "agents": {}, "tasks": [],
        "progress": {"completed": 0, "total": 11},
        "log": [], "repo_map": None,
    }
    try:
        STATUS_FILE.write_text(json.dumps(empty, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return JSONResponse({"status": "ok", "message": "Dashboard sifirlandi"})


# ── Config API (Agent / Task YAML) ──

CONFIG_DIR = Path(__file__).parent / "config"

@app.get("/api/config/env")
async def get_env_config_route():
    """Pipeline ile ilgili env degiskenleri (read-only diagnostic).

    Not: Asagidaki anahtarlarin cogu artik dashboard'dan da yonetilebilir.
    Dashboard tarafi bu env'lere göre fallback yapar."""
    env_keys = [
        # MySQL
        "MYSQL_HOST", "MYSQL_PORT", "MYSQL_USER", "MYSQL_DATABASE",
        # Repo / vector DB paths
        "CREW_REPOS_DIR", "CREW_VECTOR_DB",
        # Dashboard tarafindan yonetilenler (diagnostic icin)
        "CREW_USE_LOCAL_LLM", "CREW_LOCAL_LLM_MODEL", "CREW_LOCAL_CODER_MODEL",
        "OLLAMA_BASE_URL", "OLLAMA_CODER_BASE_URL",
        "CREW_KICKOFF_MEETING", "CREW_MAX_JOB_COST",
        "CREW_ARCHITECT_MAX_ITER", "CREW_REVIEW_MAX_RETRIES",
        "CREW_DEV_CONTEXT_BUDGET", "CREW_DEV_CONTEXT_PER_FILE",
        "CREW_MIN_WI_CONTENT_CHARS", "CREW_SM_REVIEW",
        "CREW_ANALYZE_WI_MEDIA",
        "CREW_PRICE_INPUT_USD_PER_M", "CREW_PRICE_OUTPUT_USD_PER_M",
        "CREW_WORK_ITEM_PROVIDER", "CREW_SCM_PROVIDER",
    ]
    env_vals = {k: os.environ.get(k, "") for k in env_keys}
    return JSONResponse(env_vals)


_EDITABLE_CONFIGS = {"agents", "tasks", "llm_profiles"}


@app.get("/api/config/{config_name}")
async def get_config(config_name: str):
    """YAML config dosyasini oku. config_name: 'agents' | 'tasks' | 'llm_profiles'."""
    if config_name not in _EDITABLE_CONFIGS:
        return JSONResponse(
            {"error": f"Gecersiz config: {sorted(_EDITABLE_CONFIGS)}"},
            status_code=400,
        )
    config_file = CONFIG_DIR / f"{config_name}.yaml"
    if not config_file.exists():
        return JSONResponse({"error": f"{config_name}.yaml bulunamadi"}, status_code=404)
    content = config_file.read_text(encoding="utf-8")
    return JSONResponse({"name": config_name, "content": content})


@app.put("/api/config/{config_name}")
async def update_config(config_name: str, request: Request):
    """YAML config dosyasini guncelle. Body: {"content": "yaml string"}
    Sonraki job otomatik olarak yeni config'i kullanir."""
    if config_name not in _EDITABLE_CONFIGS:
        return JSONResponse(
            {"error": f"Gecersiz config: {sorted(_EDITABLE_CONFIGS)}"},
            status_code=400,
        )

    body = await request.json()
    content = body.get("content", "")
    if not content.strip():
        return JSONResponse({"error": "Bos icerik gonderilemez"}, status_code=400)

    # YAML syntax kontrolu
    import yaml
    try:
        parsed = yaml.safe_load(content)
        if not isinstance(parsed, dict):
            return JSONResponse({"error": "YAML bir dict olmali"}, status_code=400)
    except yaml.YAMLError as e:
        return JSONResponse({"error": f"YAML syntax hatasi: {e}"}, status_code=400)

    # tasks.yaml icin zorunlu degisken kontrolu
    if config_name == "tasks":
        required_vars = ["{work_item_id}"]
        for var in required_vars:
            if var not in content:
                return JSONResponse({
                    "error": f"tasks.yaml icinde '{var}' degiskeni zorunlu"
                }, status_code=400)

    # llm_profiles icin profil yapisini dogrula
    if config_name == "llm_profiles":
        profiles = parsed.get("profiles") or {}
        if not isinstance(profiles, dict) or not profiles:
            return JSONResponse({
                "error": "llm_profiles.yaml en az bir profile iceren 'profiles:' bloku icermeli",
            }, status_code=400)
        for name, p in profiles.items():
            if not isinstance(p, dict) or not p.get("provider") or not p.get("model"):
                return JSONResponse({
                    "error": f"Profile {name!r} 'provider' ve 'model' alanlari icermeli",
                }, status_code=400)

    # Yedek al + kaydet
    config_file = CONFIG_DIR / f"{config_name}.yaml"
    backup_file = CONFIG_DIR / f"{config_name}.yaml.bak"
    if config_file.exists():
        backup_file.write_text(config_file.read_text(encoding="utf-8"), encoding="utf-8")
    config_file.write_text(content, encoding="utf-8")

    # Resolver cache'i invalidate et — yeni profil bilgisi sonraki job'da gecsin
    if config_name in ("llm_profiles", "agents"):
        try:
            from agile_sdlc_crew.llm.resolver import reset_cache
            reset_cache()
        except Exception:
            pass

    pipeline_log.info(f"Config guncellendi: {config_name}.yaml ({len(content)} char)")
    return JSONResponse({
        "status": "ok",
        "message": f"{config_name}.yaml guncellendi. Sonraki job yeni config'i kullanacak.",
        "backup": str(backup_file),
    })


# ── LLM API ──

_AGENT_KEYS = [
    "scrum_master", "business_analyst", "software_architect",
    "senior_developer", "code_reviewer", "qa_engineer", "uat_specialist",
]
_AGENT_DISPLAY = {
    "scrum_master": "Scrum Master",
    "business_analyst": "İş Analisti",
    "software_architect": "Yazılım Mimarı",
    "senior_developer": "Kıdemli Geliştirici",
    "code_reviewer": "Kod İnceleyici",
    "qa_engineer": "QA Mühendisi",
    "uat_specialist": "UAT Uzmanı",
}


@app.get("/api/llm/state")
async def llm_state():
    """Mevcut LLM provider/profile/agent eslesmelerini dondurur."""
    from agile_sdlc_crew.llm.registry import list_providers
    from agile_sdlc_crew.llm.resolver import _load_profiles_doc, resolve_spec_with_source

    profiles_doc = _load_profiles_doc()
    profiles = profiles_doc.get("profiles") or {}
    defaults = profiles_doc.get("agent_defaults") or {}

    agents_info = []
    for key in _AGENT_KEYS:
        info = {"agent_key": key, "display_name": _AGENT_DISPLAY.get(key, key)}
        try:
            spec, source = resolve_spec_with_source(key)
            info.update({
                "profile": spec.get("_profile"),  # None ise inline override
                "provider": spec.get("provider", ""),
                "model": spec.get("model", ""),
                "max_tokens": int(spec.get("max_tokens", 0)),
                "source": source,
            })
        except Exception as e:
            info["error"] = str(e)
        agents_info.append(info)

    return JSONResponse({
        "providers": list_providers(),
        "profiles": profiles,
        "agent_defaults": defaults,
        "agents": agents_info,
    })


@app.get("/api/llm/models")
async def llm_models(provider: str = ""):
    """Bir provider icin onerilen model listesi. Static ya da dinamik (canli sunucudan).

    Yanit: {provider, models: [name,...], source: 'static'|'dynamic'|'error', error?}
    """
    if not provider:
        return JSONResponse({"error": "provider parametresi gerekli"}, status_code=400)

    from agile_sdlc_crew.llm.registry import list_providers
    if provider not in list_providers():
        return JSONResponse(
            {"error": f"Bilinmeyen provider: {provider}", "valid": list_providers()},
            status_code=400,
        )

    static_lists = {
        "claude_cli": ["sonnet", "opus", "haiku"],
        "anthropic": [
            "claude-sonnet-4-20250514",
            "claude-opus-4-1-20250805",
            "claude-haiku-4-5-20251001",
            "claude-3-5-sonnet-20241022",
        ],
    }
    if provider in static_lists:
        return JSONResponse({
            "provider": provider,
            "models": static_lists[provider],
            "source": "static",
        })

    # Dinamik: ollama / lmstudio / litellm — endpoint'lere sor
    import os
    import requests

    try:
        if provider == "ollama":
            base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
            r = requests.get(f"{base.rstrip('/')}/api/tags", timeout=5, verify=False)
            r.raise_for_status()
            data = r.json()
            models = sorted({m.get("name", "") for m in data.get("models", []) if m.get("name")})
            return JSONResponse({"provider": provider, "models": models, "source": "dynamic"})

        if provider == "lmstudio":
            base = (
                os.environ.get("LMSTUDIO_BASE_URL")
                or os.environ.get("OLLAMA_BASE_URL")
                or "http://localhost:1234/v1"
            )
            url = base.rstrip("/")
            if not url.endswith("/v1"):
                url += "/v1"
            r = requests.get(f"{url}/models", timeout=5, verify=False)
            r.raise_for_status()
            data = r.json()
            models = sorted({m.get("id", "") for m in data.get("data", []) if m.get("id")})
            return JSONResponse({"provider": provider, "models": models, "source": "dynamic"})

        if provider == "litellm":
            base = os.environ.get("LITELLM_BASE_URL", "")
            if not base:
                return JSONResponse({
                    "provider": provider, "models": [], "source": "error",
                    "error": "LITELLM_BASE_URL ayarlanmadi",
                })
            api_key = os.environ.get("LITELLM_API_KEY", "")
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            url = base.rstrip("/")
            # base zaten /v1 ile bitiyorsa tekrar ekleme
            url = f"{url}/models" if url.endswith("/v1") else f"{url}/v1/models"
            r = requests.get(url, timeout=5, headers=headers, verify=False)
            r.raise_for_status()
            data = r.json()
            models = sorted({m.get("id", "") for m in data.get("data", []) if m.get("id")})
            return JSONResponse({"provider": provider, "models": models, "source": "dynamic"})
    except Exception as e:
        return JSONResponse({
            "provider": provider, "models": [], "source": "error",
            "error": str(e),
        })

    return JSONResponse({"provider": provider, "models": [], "source": "static"})


@app.put("/api/llm/agents/{agent_key}")
async def update_agent_llm(agent_key: str, request: Request):
    """Agent LLM override'i ayarla. 3 farkli body formati:

    - {"profile": "<name>"}                       profile referansi
    - {"profile": null}                           override sil
    - {"provider": "ollama", "model": "qwen3:8b", "max_tokens": 4096}
                                                  inline spec
    """
    from agile_sdlc_crew.llm.resolver import set_agent_override

    if agent_key not in _AGENT_KEYS:
        return JSONResponse(
            {"error": f"Bilinmeyen agent: {agent_key}", "valid": _AGENT_KEYS},
            status_code=400,
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    # Inline spec
    if isinstance(body.get("provider"), str) and body.get("model"):
        spec = {
            "provider": body["provider"],
            "model": body["model"],
        }
        if "max_tokens" in body:
            spec["max_tokens"] = body["max_tokens"]
        for k, v in body.items():
            if k not in spec and k not in ("profile",):
                spec[k] = v
        try:
            set_agent_override(agent_key, inline_spec=spec)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        pipeline_log.info(
            f"LLM override (inline): {agent_key} -> {spec['provider']}/{spec['model']}"
        )
        return JSONResponse({"status": "ok", "agent_key": agent_key, "spec": spec})

    # Profile reference / clear
    profile = body.get("profile")
    if profile is not None and not isinstance(profile, str):
        return JSONResponse({"error": "profile string ya da null olmali"}, status_code=400)

    try:
        set_agent_override(agent_key, profile_name=profile or None)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    pipeline_log.info(f"LLM override: {agent_key} -> {profile or '(silindi)'}")
    return JSONResponse({"status": "ok", "agent_key": agent_key, "profile": profile})


# ── Provider Credentials API ──

_VALID_NS = ("llm", "embedding", "vision", "work_item", "scm")


def _registry_for_ns(ns: str):
    if ns == "llm":
        from agile_sdlc_crew.llm import registry as r
        return r
    if ns == "embedding":
        from agile_sdlc_crew.embed import registry as r
        return r
    if ns == "vision":
        from agile_sdlc_crew.vision import registry as r
        return r
    if ns == "work_item":
        from agile_sdlc_crew.providers.registry import work_item_registry
        return work_item_registry
    if ns == "scm":
        from agile_sdlc_crew.providers.registry import scm_registry
        return scm_registry
    raise ValueError(f"Bilinmeyen namespace: {ns}")


@app.get("/api/providers/credentials")
async def providers_credentials():
    """Tum provider credential schema'larini ve mevcut degerlerini dondurur.

    Secret degerler maskelenir; UI 'edit' moduna girince ayri endpoint'ten alir."""
    from agile_sdlc_crew import credentials as creds

    out: dict = {}
    for ns in _VALID_NS:
        reg = _registry_for_ns(ns)
        schemas = reg.get_credential_schemas()
        ns_data: dict = {}
        for prov, schema in schemas.items():
            stored = creds.get_all(ns, prov)
            fields = []
            for f in schema:
                stored_val = stored.get(f["name"], "")
                env_val = os.environ.get(f.get("env_fallback", ""), "") if f.get("env_fallback") else ""
                if f.get("secret") and stored_val:
                    display_val = creds.mask(stored_val)
                else:
                    display_val = stored_val
                fields.append({
                    **f,
                    "value": display_val,
                    "has_value": bool(stored_val),
                    "env_present": bool(env_val),
                })
            ns_data[prov] = {"schema": schema, "fields": fields}
        out[ns] = ns_data
    return JSONResponse(out)


@app.get("/api/providers/credentials/{namespace}/{provider}/raw")
async def providers_credentials_raw(namespace: str, provider: str):
    """Edit modu icin ham (mask edilmemis) degerleri dondurur."""
    if namespace not in _VALID_NS:
        return JSONResponse({"error": f"Bilinmeyen namespace: {namespace}"}, status_code=400)
    from agile_sdlc_crew import credentials as creds

    reg = _registry_for_ns(namespace)
    schemas = reg.get_credential_schemas()
    if provider not in schemas:
        return JSONResponse({"error": f"Bilinmeyen provider: {provider}"}, status_code=400)

    return JSONResponse({
        "namespace": namespace,
        "provider": provider,
        "values": creds.get_all(namespace, provider),
    })


@app.put("/api/providers/credentials/{namespace}/{provider}")
async def providers_credentials_update(namespace: str, provider: str, request: Request):
    """Bir provider'in credential alanlarini gunceller.

    Body: alan_adi -> deger dict'i. Bos string veya null -> alan silinir.
    Schema'da bulunmayan alanlar reddedilir.
    """
    if namespace not in _VALID_NS:
        return JSONResponse({"error": f"Bilinmeyen namespace: {namespace}"}, status_code=400)
    from agile_sdlc_crew import credentials as creds

    reg = _registry_for_ns(namespace)
    schemas = reg.get_credential_schemas()
    if provider not in schemas:
        return JSONResponse({"error": f"Bilinmeyen provider: {provider}"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse({"error": "body bir dict olmali"}, status_code=400)

    allowed = {f["name"] for f in schemas[provider]}
    unknown = set(body.keys()) - allowed
    if unknown:
        return JSONResponse({
            "error": f"Schema'da olmayan alanlar: {sorted(unknown)}. Izinli: {sorted(allowed)}"
        }, status_code=400)

    try:
        saved = creds.save(namespace, provider, body, allowed=allowed)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    pipeline_log.info(f"Credentials guncellendi: {namespace}/{provider} ({list(saved.keys())})")
    return JSONResponse({"status": "ok", "namespace": namespace, "provider": provider, "fields": list(saved.keys())})


@app.delete("/api/providers/credentials/{namespace}/{provider}")
async def providers_credentials_delete(namespace: str, provider: str):
    """Bir provider'in tum kayitli credential'lerini siler."""
    if namespace not in _VALID_NS:
        return JSONResponse({"error": f"Bilinmeyen namespace: {namespace}"}, status_code=400)
    from agile_sdlc_crew import credentials as creds

    reg = _registry_for_ns(namespace)
    schemas = reg.get_credential_schemas()
    if provider not in schemas:
        return JSONResponse({"error": f"Bilinmeyen provider: {provider}"}, status_code=400)

    creds.save(namespace, provider, {}, allowed={f["name"] for f in schemas[provider]})
    pipeline_log.info(f"Credentials silindi: {namespace}/{provider}")
    return JSONResponse({"status": "ok"})


# ── Pipeline Behavior API ──

@app.get("/api/pipeline/state")
async def pipeline_state():
    """Pipeline davranis knob'larinin schema + degerleri + kaynaklari."""
    from agile_sdlc_crew import pipeline_config as pc
    return JSONResponse({
        "fields": pc.all_values(),
        "config": pc.load_config(),
    })


@app.put("/api/pipeline/config")
async def pipeline_config_update(request: Request):
    """Pipeline knob degerlerini yaz. Body: {KEY: value, ...}.

    Bilinmeyen key veya gecersiz tip 400 doner. Bos/null deger ilgili anahtari
    yaml'dan siler (env veya default'a duser).
    """
    from agile_sdlc_crew import pipeline_config as pc

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse({"error": "body bir dict olmali"}, status_code=400)

    try:
        doc = pc.save(body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    pipeline_log.info(f"Pipeline config guncellendi: {list(body.keys())}")
    return JSONResponse({"status": "ok", "config": doc})


# ── Work Item / SCM Provider State API ──

@app.get("/api/work-item/state")
async def work_item_state():
    """Aktif work_item provider + kayitli provider listesi + config dosyasi."""
    from agile_sdlc_crew.providers import (
        get_work_item_provider_name,
        list_work_item_providers,
        load_work_item_config,
    )
    return JSONResponse({
        "provider": get_work_item_provider_name(),
        "providers": list_work_item_providers(),
        "config": load_work_item_config(),
    })


@app.get("/api/scm/state")
async def scm_state():
    """Aktif scm provider + kayitli provider listesi + config dosyasi."""
    from agile_sdlc_crew.providers import (
        get_scm_provider_name,
        list_scm_providers,
        load_scm_config,
    )
    return JSONResponse({
        "provider": get_scm_provider_name(),
        "providers": list_scm_providers(),
        "config": load_scm_config(),
    })


# ── Vision API ──

@app.get("/api/vision/state")
async def vision_state():
    """Aktif vision provider/model + kayitli providerlar + bilinen modeller."""
    from agile_sdlc_crew.vision import (
        KNOWN_VISION_MODELS, get_base_url, get_model, get_provider,
        list_providers, load_config,
    )
    return JSONResponse({
        "provider": get_provider(),
        "providers": list_providers(),
        "model": get_model(),
        "base_url": get_base_url(),
        "config": load_config(),
        "known_models": KNOWN_VISION_MODELS,
    })


@app.put("/api/vision/config")
async def vision_config_update(request: Request):
    """Vision ayarlarini kaydet. Body: {provider, model, base_url?, api_key_env?}."""
    from agile_sdlc_crew.vision import save_config

    try:
        body = await request.json()
    except Exception:
        body = {}

    provider = body.get("provider")
    model = body.get("model")
    if not isinstance(provider, str) or not provider.strip():
        return JSONResponse({"error": "provider gerekli"}, status_code=400)
    if not isinstance(model, str) or not model.strip():
        return JSONResponse({"error": "model gerekli"}, status_code=400)

    base_url = body.get("base_url", "")
    api_key_env = body.get("api_key_env", "")

    try:
        cfg = save_config(
            provider=provider.strip(),
            model=model.strip(),
            base_url=(base_url or "").strip(),
            api_key_env=(api_key_env or "").strip(),
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    pipeline_log.info(f"Vision config guncellendi: {cfg}")
    return JSONResponse({"status": "ok", "config": cfg})


@app.post("/api/vision/test")
async def vision_test(request: Request):
    """Vision config'i 1x1 test gorseli ile dene. Yanit: {status, model, provider, ...}."""
    import base64, time
    from agile_sdlc_crew.vision import (
        analyze_image, get_api_key, get_base_url, get_model, get_provider,
    )

    # 1x1 transparent PNG (smallest valid image)
    tiny_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    b64 = base64.b64encode(tiny_png).decode("ascii")

    t0 = time.time()
    try:
        result = analyze_image(
            provider=get_provider(),
            image_b64=b64,
            mime="image/png",
            prompt="Test image. Reply with one short word.",
            model=get_model(),
            base_url=get_base_url(),
            api_key=get_api_key(),
            max_tokens=20,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        return JSONResponse({
            "status": "ok",
            "provider": get_provider(),
            "model": get_model(),
            "base_url": get_base_url(),
            "elapsed_ms": elapsed_ms,
            "response": (result or "")[:200],
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "provider": get_provider(),
            "model": get_model(),
            "base_url": get_base_url(),
            "error": str(e)[:500],
        }, status_code=500)


# ── Embedding / Vector DB API ──

@app.get("/api/embed/state")
async def embed_state():
    """Embedding provider/model/dim/url/db durumunu dondurur."""
    import os, glob
    from agile_sdlc_crew.embed import (
        KNOWN_EMBED_DIMS, get_base_url, get_dim, get_model, get_provider,
        list_providers, load_config,
    )

    db_path = os.path.expanduser(
        os.environ.get("CREW_VECTOR_DB", "~/.crew_repos/.vectordb")
    )
    db_exists = os.path.isdir(db_path)
    db_size = 0
    db_tables = 0
    if db_exists:
        for root, dirs, files in os.walk(db_path):
            db_size += sum(
                os.path.getsize(os.path.join(root, f))
                for f in files if os.path.exists(os.path.join(root, f))
            )
        db_tables = len(glob.glob(os.path.join(db_path, "*.lance")))

    return JSONResponse({
        "provider": get_provider(),
        "providers": list_providers(),
        "model": get_model(),
        "dimension": get_dim(),
        "base_url": get_base_url(),
        "config": load_config(),
        "known_dims": KNOWN_EMBED_DIMS,
        "db": {
            "path": db_path,
            "exists": db_exists,
            "size_bytes": db_size,
            "tables": db_tables,
        },
    })


@app.put("/api/embed/config")
async def embed_config_update(request: Request):
    """Embedding ayarlarini kaydet. Body: {provider, model, base_url?, api_key_env?, dimension?}"""
    from agile_sdlc_crew.embed import save_config

    try:
        body = await request.json()
    except Exception:
        body = {}

    provider = body.get("provider")
    model = body.get("model")
    if not isinstance(provider, str) or not provider.strip():
        return JSONResponse({"error": "provider gerekli"}, status_code=400)
    if not isinstance(model, str) or not model.strip():
        return JSONResponse({"error": "model gerekli"}, status_code=400)

    base_url = body.get("base_url", "")
    if base_url and not isinstance(base_url, str):
        return JSONResponse({"error": "base_url string olmali"}, status_code=400)

    api_key_env = body.get("api_key_env", "")
    if api_key_env and not isinstance(api_key_env, str):
        return JSONResponse({"error": "api_key_env string olmali"}, status_code=400)

    dimension = body.get("dimension")
    if dimension is not None:
        try:
            dimension = int(dimension)
            if dimension < 32 or dimension > 8192:
                raise ValueError("dimension 32-8192 araliginda olmali")
        except (TypeError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    try:
        cfg = save_config(
            provider=provider.strip(),
            model=model.strip(),
            base_url=(base_url or "").strip(),
            api_key_env=(api_key_env or "").strip(),
            dimension=dimension,
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    pipeline_log.info(f"Embedding config guncellendi: {cfg}")
    return JSONResponse({"status": "ok", "config": cfg})


@app.post("/api/embed/test")
async def embed_test(request: Request):
    """Mevcut config ile bir test embedding cek. Sure + dim raporlar."""
    import time
    from agile_sdlc_crew.embed import get_base_url, get_model, get_provider
    from agile_sdlc_crew.tools.vector_store import _embed_text

    try:
        body = await request.json()
    except Exception:
        body = {}
    text = (body.get("text") or "Bu bir test cumlesidir.").strip()

    t0 = time.time()
    try:
        emb = _embed_text(text, retries=0)
        elapsed_ms = int((time.time() - t0) * 1000)
        return JSONResponse({
            "status": "ok",
            "provider": get_provider(),
            "model": get_model(),
            "base_url": get_base_url(),
            "dimension": len(emb),
            "elapsed_ms": elapsed_ms,
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "provider": get_provider(),
            "model": get_model(),
            "base_url": get_base_url(),
            "error": str(e)[:500],
        }, status_code=500)


@app.post("/api/embed/clear")
async def embed_clear():
    """Vector DB'yi tamamen sil. Yeni dim ile yeniden olusur."""
    import os, shutil

    db_path = os.path.expanduser(
        os.environ.get("CREW_VECTOR_DB", "~/.crew_repos/.vectordb")
    )
    if not os.path.isdir(db_path):
        return JSONResponse({"status": "ok", "message": "DB zaten yok", "path": db_path})

    try:
        shutil.rmtree(db_path)
        pipeline_log.info(f"Vector DB temizlendi: {db_path}")
        return JSONResponse({"status": "ok", "message": f"Vector DB silindi: {db_path}"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)




# ── Sprint / Board API ──

@app.get("/api/teams")
async def list_teams():
    """Azure DevOps takim listesi."""
    try:
        client = AzureDevOpsClient()
        teams = client.list_teams()
        result = [{"id": t.get("id", ""), "name": t.get("name", "")} for t in teams]
        result.sort(key=lambda t: t["name"])
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/sprints")
async def list_sprints(team: str = ""):
    """Azure DevOps sprint/iteration listesi."""
    try:
        client = AzureDevOpsClient()
        iterations = client.list_iterations(team=team)
        sprints = []
        for it in iterations:
            attrs = it.get("attributes", {})
            sprints.append({
                "id": it.get("id", ""),
                "name": it.get("name", ""),
                "path": it.get("path", ""),
                "startDate": attrs.get("startDate", ""),
                "finishDate": attrs.get("finishDate", ""),
                "timeFrame": attrs.get("timeFrame", ""),
            })
        return JSONResponse(sprints)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/board/workitems")
async def board_work_items(iteration_path: str = ""):
    """Secilen sprintteki work item'lari dondurur."""
    if not iteration_path.strip():
        return JSONResponse({"error": "iteration_path parametresi gerekli"}, status_code=400)
    try:
        client = AzureDevOpsClient()
        items = client.get_iteration_work_items(iteration_path.strip())
        return JSONResponse(items)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Static files ──
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR), html=False), name="static")


# ── Queue Worker ──

def _ensure_worker():
    """Worker thread yoksa baslat."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return
        _worker_thread = threading.Thread(target=_queue_worker, daemon=True)
        _worker_thread.start()


def _queue_worker():
    """Kuyruktan is al ve sirayla calistir."""
    while True:
        job = db.get_next_queued_job()
        if not job:
            time.sleep(3)
            continue

        job_id = job["id"]
        work_item_id = job["work_item_id"]
        use_hal = bool(job["use_hal"])

        try:
            from agile_sdlc_crew.main import run_pipeline
            from agile_sdlc_crew.dashboard import StatusTracker

            db.start_job(job_id)
            tracker = StatusTracker()
            run_pipeline(
                work_item_id,
                use_hal=use_hal,
                tracker=tracker,
                job_id=job_id,
            )
            db.complete_job(job_id)

        except Exception as e:
            db.fail_job(job_id, str(e))
            pipeline_log.error(f"Job #{job_id} (WI #{work_item_id}) basarisiz: {e}")


# ── Startup ──

@app.on_event("startup")
async def startup():
    db.init_db()
    # Orphan running job'lari temizle (sunucu restart oncesinde takili kalmislar)
    orphan = db.fail_orphan_running_jobs()
    if orphan:
        pipeline_log.info(f"Sunucu baslatildi: {orphan} takili kalmis is failed olarak isaretlendi")
    _ensure_worker()


# ── Entry point ──

def main():
    import uvicorn
    print("=" * 60)
    print("  Agile SDLC Crew - Server v3")
    print("  Dashboard: http://localhost:8765")
    print("  API:       http://localhost:8765/api/jobs")
    print(f"  Access log : {ACCESS_LOG}")
    print(f"  Pipeline log: {PIPELINE_LOG}")
    print("=" * 60)

    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["handlers"]["access_file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(ACCESS_LOG),
        "maxBytes": 5_000_000,
        "backupCount": 3,
        "formatter": "access",
    }
    # Access loglarini SADECE dosyaya yaz — console'a dusmesin
    # (crew_server.log'u kirletiyordu)
    log_config["loggers"]["uvicorn.access"]["handlers"] = ["access_file"]

    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info", log_config=log_config)


if __name__ == "__main__":
    main()
