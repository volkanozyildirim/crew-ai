#!/bin/bash
cd "$(dirname "$0")"
if [[ "${RUN_IN_FOREGROUND:-0}" == "1" ]]; then
  exec env PYTHONUNBUFFERED=1 .venv/bin/python -m agile_sdlc_crew.server
fi

kill $(pgrep -f "agile_sdlc_crew.server") 2>/dev/null
sleep 1
PYTHONUNBUFFERED=1 .venv/bin/python -m agile_sdlc_crew.server > /tmp/crew_server.log 2>&1 &
echo "PID: $!"
echo "Dashboard: http://localhost:8765"
echo ""
echo "Loglar:"
echo "  Access : tail -f /tmp/crew_access.log"
echo "  Pipeline: tail -f /tmp/crew_pipeline.log"
echo "  Server : tail -f /tmp/crew_server.log"
