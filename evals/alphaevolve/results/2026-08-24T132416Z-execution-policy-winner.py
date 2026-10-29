"""Production-shaped execution/cache policy."""

from typing import Any, Mapping, Sequence

# EVOLVE-BLOCK-START

def choose_execution(state: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> str:
    target_id = next((val for key, val in [
        ("cache_valid", "cache_reuse"),
        ("cache_present", "rebuild"),
        ("long_running", "background_job"),
        ("flood", "foreground_captured")
    ] if state.get(key)), "foreground_inline")

    req_provides = None
    for o in options:
        if o.get("id") == target_id:
            req_provides = o.get("provides")
            break

    valid = []
    for o in options:
        if not o.get("safe", True):
            continue
            
        if "cache" in str(o.get("id", "")).lower() and not state.get("cache_valid"):
            continue
            
        if req_provides is not None and o.get("provides") != req_provides:
            continue
            
        valid.append(o)

    if valid:
        best = min(valid, key=lambda x: (
            x.get("dollars", 0.0),
            x.get("latency_ms", 0.0),
            x.get("model_turns", 0.0),
            x.get("tool_calls", 0.0),
            x.get("visible_tokens", 0.0)
        ))
        return best["id"]
        
    return target_id

# EVOLVE-BLOCK-END
