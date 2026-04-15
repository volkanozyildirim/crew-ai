"""MySQL job queue ve step logging."""

import os
import json
from datetime import datetime
from contextlib import contextmanager

import pymysql
import pymysql.cursors


DB_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", 3306)),
    "user": os.environ.get("MYSQL_USER", "root"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "database": os.environ.get("MYSQL_DATABASE", "agile_sdlc_crew"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    work_item_id VARCHAR(20) NOT NULL,
    wi_title VARCHAR(255) DEFAULT '',
    status ENUM('queued','running','completed','failed') DEFAULT 'queued',
    use_hal TINYINT(1) DEFAULT 1,
    repo_name VARCHAR(100) DEFAULT '',
    branch_name VARCHAR(100) DEFAULT '',
    pr_id VARCHAR(20) DEFAULT '',
    pr_url VARCHAR(500) DEFAULT '',
    current_step VARCHAR(100) DEFAULT '',
    error_message TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    INDEX idx_status (status),
    INDEX idx_wi (work_item_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS job_steps (
    id INT AUTO_INCREMENT PRIMARY KEY,
    job_id INT NOT NULL,
    step_key VARCHAR(50) NOT NULL,
    step_name VARCHAR(100) NOT NULL,
    status ENUM('pending','running','completed','failed','skipped') DEFAULT 'pending',
    agent VARCHAR(50) DEFAULT '',
    output LONGTEXT,
    error_message TEXT,
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE,
    INDEX idx_job (job_id)
) ENGINE=InnoDB;
"""

STEP_DEFINITIONS = [
    ("requirements_analysis_task", "İş Analizi", "business_analyst"),
    ("discover_repos_task", "Repo Keşfetme", "software_architect"),
    ("dependency_analysis_task", "Bağımlılık Analizi", "software_architect"),
    ("technical_design_task", "Teknik Tasarım", "software_architect"),
    ("create_branch_task", "Branch Oluşturma", "senior_developer"),
    ("implement_change_task", "Kod Yazma & Push", "senior_developer"),
    ("create_pr_task", "PR Oluşturma", "senior_developer"),
    ("review_pr_task", "Kod İnceleme", "code_reviewer"),
    ("test_planning_task", "Test Planlama", "qa_engineer"),
    ("uat_task", "UAT Doğrulama", "uat_specialist"),
    ("completion_report_task", "Tamamlanma Raporu", "scrum_master"),
]


@contextmanager
def get_conn():
    conn = pymysql.connect(**DB_CONFIG)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Tablolari olustur."""
    with get_conn() as conn:
        cur = conn.cursor()
        for stmt in SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)


def create_job(work_item_id: str, use_hal: bool = True, wi_title: str = "") -> int:
    """Yeni is olustur, step'leri ekle, job_id dondur."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO jobs (work_item_id, use_hal, wi_title) VALUES (%s, %s, %s)",
            (work_item_id, int(use_hal), wi_title),
        )
        job_id = cur.lastrowid
        for step_key, step_name, agent in STEP_DEFINITIONS:
            cur.execute(
                "INSERT INTO job_steps (job_id, step_key, step_name, agent) VALUES (%s, %s, %s, %s)",
                (job_id, step_key, step_name, agent),
            )
        return job_id


def get_next_queued_job() -> dict | None:
    """Kuyrukta bekleyen ilk isi getir."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1")
        return cur.fetchone()


def delete_job(job_id: int):
    """Job ve step'lerini sil."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM job_steps WHERE job_id=%s", (job_id,))
        cur.execute("DELETE FROM jobs WHERE id=%s", (job_id,))


_ALLOWED_JOB_FIELDS = frozenset({
    "status", "use_hal", "repo_name", "branch_name", "pr_id", "pr_url",
    "current_step", "error_message", "wi_title", "started_at", "finished_at",
})


def update_job(job_id: int, **fields):
    """Job alanlarini guncelle. Sadece whitelist'teki alanlar kabul edilir."""
    if not fields:
        return
    safe = {k: v for k, v in fields.items() if k in _ALLOWED_JOB_FIELDS}
    if not safe:
        return
    sets = ", ".join(f"`{k}`=%s" for k in safe)
    vals = list(safe.values()) + [job_id]
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE jobs SET {sets} WHERE id=%s", vals)


def start_job(job_id: int):
    update_job(job_id, status="running", started_at=datetime.now())


def complete_job(job_id: int, **extra):
    update_job(job_id, status="completed", finished_at=datetime.now(), **extra)


def fail_job(job_id: int, error: str):
    update_job(job_id, status="failed", finished_at=datetime.now(), error_message=error[:2000])


def start_step(job_id: int, step_key: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE job_steps SET status='running', started_at=%s WHERE job_id=%s AND step_key=%s",
            (datetime.now(), job_id, step_key),
        )


def complete_step(job_id: int, step_key: str, output: str = ""):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE job_steps SET status='completed', finished_at=%s, output=%s WHERE job_id=%s AND step_key=%s",
            (datetime.now(), output[:50000], job_id, step_key),
        )


def fail_step(job_id: int, step_key: str, error: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE job_steps SET status='failed', finished_at=%s, error_message=%s WHERE job_id=%s AND step_key=%s",
            (datetime.now(), error[:5000], job_id, step_key),
        )


def skip_steps(job_id: int, step_keys: list[str]):
    """HAL modunda atlanan adimlari isaretle."""
    with get_conn() as conn:
        cur = conn.cursor()
        for key in step_keys:
            cur.execute(
                "UPDATE job_steps SET status='completed', output='HAL modu - otomatik tamamlandi', "
                "finished_at=%s WHERE job_id=%s AND step_key=%s",
                (datetime.now(), job_id, key),
            )


def get_job(job_id: int) -> dict | None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM jobs WHERE id=%s", (job_id,))
        job = cur.fetchone()
        if not job:
            return None
        cur.execute("SELECT * FROM job_steps WHERE job_id=%s ORDER BY id", (job_id,))
        job["steps"] = cur.fetchall()
        return job


def get_all_jobs(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, work_item_id, wi_title, status, use_hal, repo_name, "
            "pr_url, current_step, error_message, created_at, started_at, finished_at "
            "FROM jobs ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def get_queue_stats() -> dict:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
        )
        stats = {row["status"]: row["cnt"] for row in cur.fetchall()}
        return {
            "queued": stats.get("queued", 0),
            "running": stats.get("running", 0),
            "completed": stats.get("completed", 0),
            "failed": stats.get("failed", 0),
            "total": sum(stats.values()),
        }
