"""Pixel art dashboard icin durum takip modulu.

StatusTracker sinifi gorev durumlarini JSON olarak yazar,
DashboardServer sinifi basit bir HTTP server ile web dashboard'u sunar.
"""

import json
import os
import threading
from datetime import datetime
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path


TASK_DISPLAY_NAMES = {
    "repo_discovery_task": "Repo Kesfetme",
    "repo_dependency_analysis_task": "Bagimlilik Analizi",
    "requirement_analysis_task": "Gereksinim Analizi",
    "technical_design_task": "Teknik Tasarim",
    "implementation_task": "Kod Gelistirme",
    "code_review_task": "Kod Inceleme",
    "test_planning_task": "Test Planlama",
    "test_execution_task": "Test Yurutme",
    "uat_preparation_task": "UAT Hazirlama",
    "uat_execution_task": "UAT Yurutme",
    "completion_report_task": "Tamamlanma Raporu",
}

TASK_AGENTS = {
    "repo_discovery_task": "software_architect",
    "repo_dependency_analysis_task": "software_architect",
    "requirement_analysis_task": "business_analyst",
    "technical_design_task": "software_architect",
    "implementation_task": "senior_developer",
    "code_review_task": "software_architect",
    "test_planning_task": "qa_engineer",
    "test_execution_task": "qa_engineer",
    "uat_preparation_task": "uat_specialist",
    "uat_execution_task": "uat_specialist",
    "completion_report_task": "business_analyst",
}

AGENT_AVATARS = {
    "scrum_master": "crown",
    "business_analyst": "scroll",
    "software_architect": "blueprint",
    "senior_developer": "keyboard",
    "qa_engineer": "bug",
    "uat_specialist": "checkmark",
}

AGENT_DISPLAY_NAMES = {
    "scrum_master": "Scrum Master",
    "business_analyst": "Is Analisti",
    "software_architect": "Yazilim Mimari",
    "senior_developer": "Kidemli Gelistirici",
    "qa_engineer": "QA Muhendisi",
    "uat_specialist": "UAT Uzmani",
}


class StatusTracker:
    """Crew calisma durumunu JSON dosyasina yazan sinif."""

    def __init__(self, status_dir: str | None = None):
        if status_dir is None:
            status_dir = str(Path(__file__).parent / "web")
        self.status_file = os.path.join(status_dir, "status.json")
        self._lock = threading.Lock()
        self._status = {
            "work_item_id": "",
            "started_at": "",
            "finished_at": "",
            "agents": {},
            "tasks": [],
            "progress": {"completed": 0, "total": 11},
            "log": [],
            "repo_map": None,
        }

        for agent_key, display_name in AGENT_DISPLAY_NAMES.items():
            self._status["agents"][agent_key] = {
                "display_name": display_name,
                "status": "idle",
                "current_task": None,
                "avatar": AGENT_AVATARS.get(agent_key, "person"),
            }

        task_order = [
            "repo_discovery_task",
            "repo_dependency_analysis_task",
            "requirement_analysis_task",
            "technical_design_task",
            "implementation_task",
            "code_review_task",
            "test_planning_task",
            "test_execution_task",
            "uat_preparation_task",
            "uat_execution_task",
            "completion_report_task",
        ]
        for task_key in task_order:
            self._status["tasks"].append({
                "key": task_key,
                "name": TASK_DISPLAY_NAMES[task_key],
                "status": "pending",
                "agent": TASK_AGENTS[task_key],
            })

    def _save(self):
        with open(self.status_file, "w", encoding="utf-8") as f:
            json.dump(self._status, f, ensure_ascii=False, indent=2)

    def update_repo_map(self, repo_map: dict):
        """Repo haritasini gunceller. Dashboard'da goruntulenir.

        repo_map formati:
        {
            "repos": [
                {"name": "api-service", "language": "C#", "framework": ".NET 8",
                 "purpose": "REST API servisi", "affected": True},
                ...
            ],
            "dependencies": [
                {"from": "web-app", "to": "api-service", "type": "REST API"},
                ...
            ]
        }
        """
        with self._lock:
            self._status["repo_map"] = repo_map
            self._add_log(f"Repo haritasi guncellendi ({len(repo_map.get('repos', []))} repo)")
            self._save()

    def start(self, work_item_id: str):
        with self._lock:
            self._status["work_item_id"] = str(work_item_id)
            self._status["started_at"] = datetime.now().isoformat()
            self._status["agents"]["scrum_master"]["status"] = "working"
            self._status["agents"]["scrum_master"]["current_task"] = "Ekip Koordinasyonu"
            self._add_log("Sprint baslatildi")
            self._save()

    def task_started(self, task_key: str):
        with self._lock:
            agent_key = TASK_AGENTS.get(task_key, "")
            task_name = TASK_DISPLAY_NAMES.get(task_key, task_key)

            for t in self._status["tasks"]:
                if t["key"] == task_key:
                    t["status"] = "in_progress"
                    break

            if agent_key and agent_key in self._status["agents"]:
                self._status["agents"][agent_key]["status"] = "working"
                self._status["agents"][agent_key]["current_task"] = task_name

            self._add_log(f"{task_name} baslatildi")
            self._save()

    def task_completed(self, task_key: str):
        with self._lock:
            agent_key = TASK_AGENTS.get(task_key, "")
            task_name = TASK_DISPLAY_NAMES.get(task_key, task_key)

            for t in self._status["tasks"]:
                if t["key"] == task_key:
                    t["status"] = "completed"
                    break

            if agent_key and agent_key in self._status["agents"]:
                self._status["agents"][agent_key]["status"] = "idle"
                self._status["agents"][agent_key]["current_task"] = None

            self._status["progress"]["completed"] = sum(
                1 for t in self._status["tasks"] if t["status"] == "completed"
            )

            self._add_log(f"{task_name} tamamlandi")
            self._save()

    def finish(self):
        with self._lock:
            self._status["finished_at"] = datetime.now().isoformat()
            for agent in self._status["agents"].values():
                agent["status"] = "idle"
                agent["current_task"] = None
            self._add_log("Sprint tamamlandi!")
            self._save()

    def _add_log(self, message: str):
        self._status["log"].append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": message,
        })


class _CORSHandler(SimpleHTTPRequestHandler):
    """CORS header'lari ekleyen HTTP handler."""

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def log_message(self, format, *args):
        pass  # Konsolu kirletmemek icin loglamayi kapat


def start_dashboard_server(port: int = 8765) -> HTTPServer:
    """Dashboard web sunucusunu ayri bir thread'de baslatir."""
    web_dir = str(Path(__file__).parent / "web")
    handler = partial(_CORSHandler, directory=web_dir)
    server = HTTPServer(("0.0.0.0", port), handler)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    return server
