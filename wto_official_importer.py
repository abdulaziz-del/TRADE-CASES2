"""Official WTO dataset loader for the Trade Cases Flask app.
Prevents silent fallback to old embedded data and can generate official data at startup.
"""
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path
from typing import Any, Dict, List

DATA_PATH = Path(os.environ.get("WTO_OFFICIAL_DATA", "data/wto_disputes_official.json"))
MAX_DS = os.environ.get("WTO_MAX_DS", "700")
AUTO_FETCH = os.environ.get("WTO_AUTO_FETCH", "1") == "1"
MIN_CASES = int(os.environ.get("WTO_MIN_CASES", "500"))

class OfficialDataMissing(RuntimeError):
    pass

def _run_importer() -> None:
    script = Path("scripts/wto_dsdb_importer.py")
    if not script.exists():
        raise OfficialDataMissing(f"Missing importer script: {script}")
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(script), "--max-ds", MAX_DS, "--out", str(DATA_PATH)]
    subprocess.check_call(cmd)

def _read_cases() -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        cases = payload.get("cases", [])
        metadata = payload.get("metadata", {})
    elif isinstance(payload, list):
        cases = payload
        metadata = {}
    else:
        cases, metadata = [], {}
    if not isinstance(cases, list):
        cases = []
    metadata = dict(metadata)
    metadata["official_data_loaded"] = True
    metadata["case_count"] = len(cases)
    metadata["path"] = str(DATA_PATH)
    return cases, metadata

def load_official_disputes(old_data: List[Dict[str, Any]] | None = None) -> List[Dict[str, Any]]:
    if not DATA_PATH.exists() and AUTO_FETCH:
        _run_importer()
    if not DATA_PATH.exists():
        raise OfficialDataMissing(f"Official WTO data file is missing: {DATA_PATH}")
    cases, _ = _read_cases()
    if len(cases) < MIN_CASES:
        raise OfficialDataMissing(
            f"Official WTO dataset has only {len(cases)} cases; expected at least {MIN_CASES}. "
            "This prevents falling back to incomplete/static data."
        )
    return cases

def official_metadata() -> Dict[str, Any]:
    if not DATA_PATH.exists():
        return {"official_data_loaded": False, "path": str(DATA_PATH), "error": "file missing"}
    try:
        _, md = _read_cases()
        return md
    except Exception as exc:
        return {"official_data_loaded": False, "path": str(DATA_PATH), "error": str(exc)}
