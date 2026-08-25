"""Complete but context-heavy AlphaEvolve seed."""

# EVOLVE-BLOCK-START


def choose_handoff(state: dict, options: tuple = ()) -> str:
    s = str(state).lower()
    
    # 1. Determine required level `r` based on the state
    if any(x in s for x in (
        'fail', 'error', 'diagnostic', 'bug', 'exception', 'crash', 'fault', 'reject',
        'failure', 'traceback', 'warn', 'warning', 'alert', 'incident', 'issue', 'problem'
    )):
        r = 4
    elif any(x in s for x in (
        'mutat', 'verify', 'verification', 'evidence', 'decision', 'assert', 'check',
        'audit', 'write', 'update', 'change', 'modify', 'delete', 'post', 'put', 'patch',
        'creation', 'create', 'transaction', 'commit', 'approval', 'approve'
    )):
        r = 3
    elif any(x in s for x in (
        'depend', 'summary', 'bound', 'consumer', 'parent', 'child', 'relation',
        'downstream', 'upstream', 'caller', 'callee', 'subscriber', 'observer', 'client', 'service'
    )):
        r = 2
    else:
        r = 1

    def get_score(opt: dict) -> int:
        opt_all = str(opt).lower()
        
        # Match option level `l`
        if any(x in opt_all for x in (
            'diagnostic', 'detail', 'verbose', 'full', 'debug', 'failure', 'error', 'traceback', 'exception'
        )):
            l = 4
        elif any(x in opt_all for x in (
            'evidence', 'decision', 'verify', 'verification', 'mutation', 'audit', 'check',
            'transaction', 'proof', 'sign', 'record'
        )):
            l = 3
        elif any(x in opt_all for x in (
            'summary', 'bound', 'info', 'brief', 'depend', 'abstract', 'overview', 'outline', 'digest'
        )):
            l = 2
        elif any(x in opt_all for x in (
            'minimal', 'address', 'id', 'short', 'compact', 'success', 'terminal', 'tiny', 'basic', 'light'
        )):
            l = 1
        elif 'expanded' in opt_all:
            l = 5
        else:
            l = 6
            
        return l - r if l >= r else 10 - l

    # Extract valid options containing an 'id'
    valid = [o for o in options if isinstance(o, dict) and 'id' in o]
    if not valid:
        if options:
            first = options[0]
            if isinstance(first, dict):
                return str(first.get('id', 'expanded'))
            return str(first)
        return "expanded"
        
    best_option = min(valid, key=get_score)
    return str(best_option['id'])


# EVOLVE-BLOCK-END
