"""Agile SDLC Crew - Always-on FastAPI server with job queue."""

import json
import logging
import logging.handlers
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
    """Pipeline ile ilgili env degiskenlerini dondurur."""
    import os
    env_keys = [
        "CREW_USE_LOCAL_LLM", "CREW_LOCAL_LLM_MODEL", "CREW_LOCAL_CODER_MODEL",
        "OLLAMA_BASE_URL", "OLLAMA_CODER_BASE_URL",
        "CREW_KICKOFF_MEETING", "CREW_MAX_JOB_COST",
        "CREW_ARCHITECT_MAX_ITER", "CREW_REVIEW_MAX_RETRIES",
        "CREW_DEV_CONTEXT_BUDGET", "CREW_DEV_CONTEXT_PER_FILE",
        "CREW_MIN_WI_CONTENT_CHARS", "CREW_SM_REVIEW",
        "CREW_WORK_ITEM_PROVIDER", "CREW_SCM_PROVIDER",
    ]
    env_vals = {}
    for k in env_keys:
        v = os.environ.get(k, "")
        env_vals[k] = v
    return JSONResponse(env_vals)


@app.get("/api/config/{config_name}")
async def get_config(config_name: str):
    """YAML config dosyasini oku. config_name: 'agents' veya 'tasks'."""
    if config_name not in ("agents", "tasks"):
        return JSONResponse({"error": "Gecersiz config: agents veya tasks"}, status_code=400)
    config_file = CONFIG_DIR / f"{config_name}.yaml"
    if not config_file.exists():
        return JSONResponse({"error": f"{config_name}.yaml bulunamadi"}, status_code=404)
    content = config_file.read_text(encoding="utf-8")
    return JSONResponse({"name": config_name, "content": content})


@app.put("/api/config/{config_name}")
async def update_config(config_name: str, request: Request):
    """YAML config dosyasini guncelle. Body: {"content": "yaml string"}
    Sonraki job otomatik olarak yeni config'i kullanir."""
    if config_name not in ("agents", "tasks"):
        return JSONResponse({"error": "Gecersiz config: agents veya tasks"}, status_code=400)

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

    # Yedek al + kaydet
    config_file = CONFIG_DIR / f"{config_name}.yaml"
    backup_file = CONFIG_DIR / f"{config_name}.yaml.bak"
    if config_file.exists():
        backup_file.write_text(config_file.read_text(encoding="utf-8"), encoding="utf-8")
    config_file.write_text(content, encoding="utf-8")

    pipeline_log.info(f"Config guncellendi: {config_name}.yaml ({len(content)} char)")
    return JSONResponse({
        "status": "ok",
        "message": f"{config_name}.yaml guncellendi. Sonraki job yeni config'i kullanacak.",
        "backup": str(backup_file),
    })




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
