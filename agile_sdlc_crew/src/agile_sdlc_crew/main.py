#!/usr/bin/env python
"""Agile SDLC Crew - Giris noktasi."""

import sys
import warnings
import webbrowser

from agile_sdlc_crew.crew import AgileSDLCCrew
from agile_sdlc_crew.dashboard import StatusTracker, start_dashboard_server

warnings.filterwarnings("ignore", category=SyntaxWarning, module="pysbd")

DASHBOARD_PORT = 8765


def run():
    """Crew'u dashboard ile birlikte calistirir."""

    # Work Item ID al
    if len(sys.argv) > 1:
        work_item_id = sys.argv[1]
    else:
        work_item_id = input("Azure DevOps Work Item ID: ").strip()

    if not work_item_id:
        print("Hata: Work Item ID girilmedi.")
        sys.exit(1)

    # Dashboard sunucusunu baslat
    tracker = StatusTracker()
    server = start_dashboard_server(port=DASHBOARD_PORT)
    print(f"\n{'='*60}")
    print(f"  PIXEL ART DASHBOARD: http://localhost:{DASHBOARD_PORT}")
    print(f"{'='*60}\n")

    # Tarayicida ac
    try:
        webbrowser.open(f"http://localhost:{DASHBOARD_PORT}")
    except Exception:
        pass

    # Crew'u olustur ve dashboard tracker'i bagla
    agile_crew = AgileSDLCCrew()
    agile_crew.set_status_tracker(tracker)

    # Sprint'i baslat
    tracker.start(work_item_id)

    inputs = {"work_item_id": work_item_id}

    try:
        result = agile_crew.crew().kickoff(inputs=inputs)
        tracker.finish()
        print("\n" + "=" * 60)
        print("  SPRINT TAMAMLANDI!")
        print("=" * 60)
        print(f"\nSonuc:\n{result.raw[:500] if result.raw else 'Sonuc yok'}...")
        return result
    except Exception as e:
        tracker.finish()
        raise Exception(f"Crew calistirilirken hata olustu: {e}")
    finally:
        server.shutdown()


def train():
    """Crew'u egitir."""
    inputs = {"work_item_id": "0"}
    try:
        AgileSDLCCrew().crew().train(
            n_iterations=int(sys.argv[1]) if len(sys.argv) > 1 else 1,
            filename=sys.argv[2] if len(sys.argv) > 2 else "training_data.pkl",
            inputs=inputs,
        )
    except Exception as e:
        raise Exception(f"Crew egitimi sirasinda hata olustu: {e}")


def replay():
    """Belirli bir gorevden tekrar calistirir."""
    try:
        AgileSDLCCrew().crew().replay(
            task_id=sys.argv[1] if len(sys.argv) > 1 else ""
        )
    except Exception as e:
        raise Exception(f"Replay sirasinda hata olustu: {e}")


def test():
    """Crew'u test eder."""
    inputs = {"work_item_id": "0"}
    try:
        AgileSDLCCrew().crew().test(
            n_iterations=int(sys.argv[1]) if len(sys.argv) > 1 else 1,
            eval_llm=sys.argv[2] if len(sys.argv) > 2 else None,
            inputs=inputs,
        )
    except Exception as e:
        raise Exception(f"Test sirasinda hata olustu: {e}")


if __name__ == "__main__":
    run()
