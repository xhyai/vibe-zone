"""Adapter for operator-owned checks, mounted outside the public source tree."""
import importlib.util
import os
from pathlib import Path


def project(value):
    """Only aggregate counts and status may leave the trusted runner."""
    clean = {}
    if not isinstance(value, dict):
        return clean
    for key in ("hidden", "chaos", "bonus", "pilot_smoke"):
        item = value.get(key)
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if status not in {"passed", "failed", "unavailable", "disabled"}:
            status = "unavailable"
        projected = {"status": status}
        if key in {"hidden", "bonus"}:
            passed, total = item.get("passed"), item.get("total")
            if type(passed) is int and type(total) is int and 0 <= passed <= total <= 1000 and total > 0:
                projected.update(passed=passed, total=total)
                if status in {"passed", "failed"}:
                    projected["status"] = "passed" if passed == total else "failed"
            else:
                projected["status"] = "unavailable"
        clean[key] = projected
    return clean



def execute(run, flags):
    enabled = set(flags) & {"hidden", "chaos", "bonus"}
    if "chaos" in enabled:
        allowed = os.environ.get("BENCH_ALLOWED_IMAGE_IDS", "").split()
        smoke_starter = run.config["name"] == "smoke" and run.result["image_id"] in allowed
        if os.environ.get("VZ_RUNNER_ROLE") != "competition" and not smoke_starter:
            enabled.remove("chaos")
    if not enabled:
        return {}
    fallback = {name: {"status": "unavailable"} for name in enabled}
    root = Path(os.environ.get("VZ_PRIVATE_DIR", "/run/vibezone-private"))
    try:
        spec = importlib.util.spec_from_file_location("vibezone_private_checks", root / "hooks.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = project(module.run_checks(run, sorted(enabled)))
        return {name: result.get(name, fallback[name]) for name in enabled}
    except Exception:
        # Exception text can contain private checks or hostile HTTP bodies.
        return fallback
