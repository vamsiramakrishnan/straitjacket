"""Safe serial-workspace AlphaEvolve seed."""

# EVOLVE-BLOCK-START


def choose_mutation_isolation(state: dict, options: tuple = ()) -> str:
    # Safely get mutation count
    mutation_count = state.get("mutation_count", 0)
    if not isinstance(mutation_count, int):
        mutation_count = 0
    
    # Safely look for keys in state
    has_own_worktree = False
    targets_declared = False
    targets_disjoint = False
    
    for k, v in state.items():
        k_lower = k.lower()
        if "mutation" in k_lower and "count" in k_lower:
            if isinstance(v, int):
                mutation_count = v
                
        if "worktree" in k_lower:
            if v and "shared" not in k_lower and "common" not in k_lower:
                has_own_worktree = True
            if isinstance(v, str) and any(x in v.lower() for x in ["isolated", "independent", "own", "private", "unique"]):
                has_own_worktree = True
                
        if "target" in k_lower:
            if any(x in k_lower for x in ["declared", "decl"]):
                if v:
                    targets_declared = True
            if k_lower == "targets" or k_lower == "write_targets":
                if isinstance(v, (list, tuple, set)) and len(v) > 0:
                    targets_declared = True
                    
        if "disjoint" in k_lower:
            if v:
                targets_disjoint = True
        if "overlap" in k_lower or "conflict" in k_lower:
            if not v:
                targets_disjoint = True

    # Check for shared workspace explicitly disabling own worktree
    for k, v in state.items():
        k_lower = k.lower()
        if "shared" in k_lower and ("workspace" in k_lower or "worktree" in k_lower):
            if v:
                has_own_worktree = False

    # Structural check
    structural_ok = False
    mutations = state.get("mutations", [])
    if isinstance(mutations, (list, tuple)) and len(mutations) > 0:
        all_have_worktree = True
        all_targets = []
        has_targets = True
        for mut in mutations:
            if isinstance(mut, dict):
                wt = mut.get("worktree") or mut.get("isolated") or mut.get("has_worktree") or mut.get("own_worktree")
                if not wt:
                    all_have_worktree = False
                t = mut.get("targets") or mut.get("write_targets") or mut.get("declared_targets")
                if not t:
                    has_targets = False
                else:
                    all_targets.append(t)
            else:
                all_have_worktree = False
                has_targets = False
                
        if all_have_worktree and has_targets:
            flat_targets = []
            distinct_targets = set()
            for t_list in all_targets:
                if isinstance(t_list, (list, tuple, set)):
                    for item in t_list:
                        flat_targets.append(item)
                        distinct_targets.add(item)
                else:
                    flat_targets.append(t_list)
                    distinct_targets.add(t_list)
            if len(flat_targets) == len(distinct_targets):
                structural_ok = True

    parallel_admissible = (has_own_worktree and targets_declared and targets_disjoint) or structural_ok

    # Parse options
    readonly_opts = []
    serial_opts = []
    parallel_opts = []
    all_opt_ids = []
    
    for opt in options:
        opt_id = ""
        opt_dict = {}
        if isinstance(opt, dict):
            opt_id = opt.get("id", "")
            opt_dict = opt
        else:
            opt_id = str(opt)
            
        all_opt_ids.append(opt_id)
        
        # Identify characteristics
        is_readonly = "readonly" in opt_id or "read_only" in opt_id or opt_dict.get("readonly") or opt_dict.get("read_only")
        is_parallel = "parallel" in opt_id or opt_dict.get("parallel")
        is_serial = "serial" in opt_id or opt_dict.get("serial")
        
        if is_readonly:
            readonly_opts.append(opt_id)
        elif is_parallel:
            parallel_opts.append(opt_id)
        elif is_serial:
            serial_opts.append(opt_id)

    # Make selection
    if not mutation_count:
        if readonly_opts:
            return readonly_opts[0]
        if serial_opts:
            return serial_opts[0]
        if all_opt_ids:
            return all_opt_ids[0]
        return "readonly_shared"
        
    if parallel_admissible:
        if parallel_opts:
            return parallel_opts[0]
        if serial_opts:
            return serial_opts[0]
        if all_opt_ids:
            return all_opt_ids[0]
        return "serial_workspace"
        
    # Serial required
    if serial_opts:
        return serial_opts[0]
    if all_opt_ids:
        # Avoid parallel if not admissible
        non_parallel = [oid for oid in all_opt_ids if oid not in parallel_opts]
        if non_parallel:
            return non_parallel[0]
        return all_opt_ids[0]
    return "serial_workspace"


# EVOLVE-BLOCK-END
