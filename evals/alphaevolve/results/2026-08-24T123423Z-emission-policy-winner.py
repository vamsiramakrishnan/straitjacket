"""Production-shaped output-emission policy."""

from typing import Any, Mapping, Sequence

# EVOLVE-BLOCK-START

def choose_emission(state: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> str:
    # Compute the robust fallback option ID
    fallback = "standard_digest"
    if state.get("failure"):
        fallback = "typed_failure"
    elif state.get("small") and not state.get("derived_evidence"):
        fallback = "raw_small"
    elif state.get("starved") or state.get("starvation"):
        fallback = "dense_digest"
        
    if not options:
        return fallback

    best_opt_id = None
    best_score = -float("inf")
    
    for opt in options:
        opt_id = opt.get("id") or opt.get("name") or opt.get("option_id")
        if not opt_id:
            continue
            
        opt_id_str = str(opt_id)
        caps = [str(c).lower() for c in opt.get("capabilities", [])] + [opt_id_str.lower()]
        
        score = 0.0
        
        # 1. Failure requirement: complete typed census and decisive fact
        if state.get("failure"):
            has_census = any("census" in c for c in caps)
            has_fact = any("fact" in c for c in caps)
            if has_census:
                score += 1000000.0
            if has_fact:
                score += 1000000.0
            if "typed_failure" in opt_id_str.lower():
                score += 500000.0
                
        # 2. Flood requirement: requires address
        if state.get("flood") or state.get("floods"):
            has_address = any("address" in c for c in caps)
            if has_address:
                score += 1000000.0
            if "flood" in opt_id_str.lower():
                score += 500000.0
                
        # 3. Small no evidence requirement: pass through without scaffolding
        if state.get("small") and not state.get("derived_evidence"):
            has_scaffold = any("scaffold" in c or "digest" in c for c in caps)
            if not has_scaffold:
                score += 1000000.0
            if "raw" in opt_id_str.lower():
                score += 500000.0
                
        # 4. Starvation requirement: may densify
        if state.get("starved") or state.get("starvation"):
            has_dense = any("dense" in c or "densify" in c for c in caps)
            if has_dense:
                score += 1000000.0
            if "dense" in opt_id_str.lower():
                score += 500000.0
                
        # Standard case preference
        is_special = (
            state.get("failure") or 
            state.get("flood") or 
            state.get("floods") or 
            (state.get("small") and not state.get("derived_evidence")) or 
            state.get("starved") or 
            state.get("starvation")
        )
        if not is_special:
            if "standard" in opt_id_str.lower() or "digest" in opt_id_str.lower():
                score += 500000.0
                
        # Cost minimization (penalize tokens, turns, tools, latency, and dollars)
        vt = opt.get("visible_tokens", 0)
        mt = opt.get("model_turns", 0)
        tc = opt.get("tool_calls", 0)
        lat = opt.get("latency", 0.0)
        dol = opt.get("dollars", 0.0)
        
        cost = (
            vt * 1.0 + 
            mt * 10000.0 + 
            tc * 2000.0 + 
            lat * 100.0 + 
            dol * 100000.0
        )
        
        score -= cost
        
        if score > best_score:
            best_score = score
            best_opt_id = opt_id_str
            
    return best_opt_id if best_opt_id is not None else fallback

# EVOLVE-BLOCK-END
