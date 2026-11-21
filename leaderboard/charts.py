"""Small, server-rendered plots of recorded load levels."""
import math


def load_chart(levels, score, repetition=None):
    points = []
    for level in levels:
        vus, latency = level.get("vus"), level.get("p99_ms")
        if (type(vus) not in (int, float) or type(latency) not in (int, float)
                or not math.isfinite(vus) or not math.isfinite(latency) or vus <= 0 or latency < 0):
            continue
        points.append({"vus": vus, "latency": round(latency, 1), "passed": level.get("passed") is True})
    if not points:
        return None
    maximum = max(750, max(p["latency"] for p in points) * 1.15)
    for index, point in enumerate(points):
        point.update(x=60 + index * 380 / max(1, len(points) - 1), y=240 - point["latency"] * 200 / maximum)
    return {"points": points, "line": " ".join(f'{p["x"]:.1f},{p["y"]:.1f}' for p in points),
            "threshold": 240 - 500 * 200 / maximum, "score": score, "repetition": repetition}


def result_chart(result):
    repetitions = result.get("repetitions") or []
    candidates = [(i + 1, r) for i, r in enumerate(repetitions) if r.get("max_vus") == result.get("max_vus")]
    if result.get("status") != "passed" or not candidates:
        return None
    number, recorded = candidates[0]
    return load_chart(recorded.get("levels", []), recorded["max_vus"], number)


EXAMPLE = load_chart([{"vus": vus, "p99_ms": ms, "passed": ms < 500}
                      for vus, ms in [(10, 80), (50, 190), (100, 350), (200, 620)]], 100)
