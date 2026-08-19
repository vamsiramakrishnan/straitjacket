# Optimize repository context selection

Evolve `choose_context(state, options) -> option_id`. Select files before
scanning: a named file beats a repository map, a named symbol needs its
definition and callers, changes need their impacted files, and language scope
should constrain a corpus. Unknown architecture may keep the full repository
map. Required target and dependency recall are hard gates. Change only the
`EVOLVE-BLOCK`.

Each option exposes `id`, `provides`, `safe`, and the five efficiency metrics.
