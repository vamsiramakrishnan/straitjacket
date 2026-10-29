"""Production-shaped birth-gate policy."""

from typing import Any, Mapping, Sequence

# EVOLVE-BLOCK-START

def choose_guard(state: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> str:
    # Determine the desired action categories in order of preference/safety
    if state.get("explicit_deny") or state.get("destructive"):
        targets = ["deny", "block"]
    elif state.get("secret") or state.get("outside_root") or state.get("unknown_command"):
        targets = ["force_ask", "ask", "prompt"]
    elif state.get("large_read") or state.get("flood_command") or state.get("session_pressure"):
        targets = ["rewrite_read", "bound_read", "rewrite_command", "rewrite"]
    elif state.get("readonly_noisy"):
        targets = ["containment", "contain", "rewrite_command", "rewrite"]
    elif state.get("bounded_command") or state.get("structured_result"):
        targets = ["allow", "pass", "permit"]
    else:
        targets = ["allow", "pass", "permit"]

    if not options:
        return targets[0]

    def score_option(opt: Mapping[str, Any]) -> tuple:
        provides = opt.get("provides")
        opt_id = opt.get("id")
        
        match_index = -1
        for i, t in enumerate(targets):
            if provides == t or opt_id == t:
                match_index = i
                break
                
        if match_index == -1:
            for i, t in enumerate(targets):
                if (isinstance(provides, str) and t in provides) or (isinstance(opt_id, str) and t in opt_id):
                    match_index = i + len(targets)
                    break
                    
        match_score = -match_index if match_index != -1 else -999
        is_safe = bool(opt.get("safe", False))
        is_matching = (match_score != -999)
        
        # We want to minimize costs (so we negate the cost in the return tuple)
        dollars = float(opt.get("dollars", 0.0))
        latency = float(opt.get("latency_ms", 0.0))
        turns = float(opt.get("model_turns", 0.0))
        calls = float(opt.get("tool_calls", 0.0))
        tokens = float(opt.get("visible_tokens", 0.0))
        
        cost = (
            dollars * 100000.0
            + latency * 1.0
            + turns * 1000.0
            + calls * 1000.0
            + tokens * 0.1
        )
        
        return (is_matching, is_safe, match_score, -cost)

    best_option = max(options, key=score_option)
    return best_option.get("id", targets[0])

# EVOLVE-BLOCK-END
