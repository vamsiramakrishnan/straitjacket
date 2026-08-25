"""Production-shaped output-emission policy."""

from typing import Any, Mapping, Sequence

# EVOLVE-BLOCK-START

def choose_emission(state: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> str:
    # Identify requirements
    required = set()
    if state.get("failure"):
        required.update(["typed_census", "decisive_fact"])
    if any(state.get(k) for k in ["flood", "flooded", "flooding", "is_flood", "has_flood"]):
        required.add("address")
        
    req_from_state = state.get("required_capabilities") or state.get("required_facts") or state.get("required")
    if req_from_state:
        if isinstance(req_from_state, str):
            required.add(req_from_state)
        elif isinstance(req_from_state, (list, set, tuple)):
            required.update(req_from_state)

    prefer_raw = bool(state.get("small") and not state.get("derived_evidence"))
    prefer_dense = bool(state.get("starved"))

    best_opt = None
    best_score = -float('inf')

    for opt in options:
        opt_id = opt.get("id", "")
        caps = set(opt.get("capabilities", []))
        
        # Missing required capabilities are heavily penalized
        missing_count = sum(1 for req in required if req not in caps)
        
        visible_tokens = opt.get("visible_tokens") or 0
        model_turns = opt.get("model_turns") or 0
        tool_calls = opt.get("tool_calls") or 0
        latency = opt.get("latency") or 0
        dollars = opt.get("dollars") or 0
        
        # Cost minimization (prioritizing turns and tokens)
        cost = (
            visible_tokens * 1.0 +
            model_turns * 10000.0 +
            tool_calls * 1000.0 +
            latency * 100.0 +
            dollars * 100000.0
        )
        
        score = -cost - (missing_count * 10000000.0)
        
        if prefer_raw:
            if "raw" in opt_id or "raw" in caps:
                score += 50000.0
            elif "scaffolding" in caps:
                score -= 50000.0
        else:
            if "raw" in opt_id or "raw" in caps:
                score -= 20000.0
                
        if prefer_dense:
            if "dense" in opt_id or "dense" in caps:
                score += 50000.0
        else:
            if "dense" in opt_id or "dense" in caps:
                score -= 5000.0
                
        if state.get("failure") and ("failure" in opt_id or "failure" in caps):
            score += 100000.0

        if score > best_score:
            best_score = score
            best_opt = opt

    if best_opt is not None and "id" in best_opt:
        return best_opt["id"]

    # Fallback
    if state.get("failure"):
        return "typed_failure"
    if state.get("small") and not state.get("derived_evidence"):
        return "raw_small"
    if state.get("starved"):
        return "dense_digest"
    return "standard_digest"

# EVOLVE-BLOCK-END
