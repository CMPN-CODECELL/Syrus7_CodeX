#!/usr/bin/env bash
cd "$(dirname "$0")/backend"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn tradeshield.app:app --port 8000
