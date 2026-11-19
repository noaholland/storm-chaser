"""Write real response samples for the frontend team into assets/samples/ (frozen at the SGX storm's first tornado)."""
import json
from pathlib import Path

from _harness import ROOT, Api

OUT = ROOT / "assets" / "samples"


def save(name, obj):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(obj, indent=1) + "\n")
    print("wrote", (OUT / name).relative_to(ROOT))


def trim(d, n=5):
    """Keep the first n items of long lists so samples stay readable."""
    if isinstance(d, dict):
        return {k: trim(v, n) for k, v in d.items()}
    if isinstance(d, list) and len(d) > n:
        return [trim(v, n) for v in d[:n]] + [f"... {len(d) - n} more"]
    return d


def main():
    api = Api()
    try:
        api.wait()
        save("datasets.json", api.get("/datasets"))
        save("replay_started.json", api.post("/replay", dataset="sgx_usdjpy_storm", speed="max", pause_on="tornado"))
        for event, snap in api.stream(symbol="USDJPY", interval=0.1):
            if snap.get("state") == "paused":
                save("stream_event.json", trim(snap))
                break
        save("score.json", api.get("/score"))
        save("exposure.json", api.post("/exposure", {"positions": [
            {"asset": "USDJPY", "size": 1_000_000, "entry_price": 163.9}, {"asset": "DOGE", "size": 10}],
            "horizon_bars": 60, "confidence": 0.99}))
        save("simulate.json", trim(api.post("/simulate", {"symbol": "USDJPY", "size": 1_000_000, "steps": 60, "seed": 7})))
        save("history.json", trim(api.get("/history", n=200)))
        save("alerts.json", trim(api.get("/alerts")))
        save("health.json", api.get("/health"))
    finally:
        api.close()


if __name__ == "__main__":
    main()
