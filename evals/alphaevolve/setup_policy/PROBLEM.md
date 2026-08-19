# Optimize setup and repair DevEx

Evolve `choose_setup(state, options) -> option_id`. A current successful setup
receipt should make repeat setup a verified no-op. Fresh installs configure only
detected hosts, unless no host exists or the user explicitly asks for all.
Managed drift must repair and re-verify; unreadable or unmanaged conflicts must
refuse without overwriting user configuration. Change only the `EVOLVE-BLOCK`.

Completion, preservation, idempotency, diagnostics, and verification are hard
gates. Efficiency is scored only after those gates pass.
