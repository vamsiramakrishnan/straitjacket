# EVOLVE-BLOCK-START
def rank_blocks(items):
    return [item["id"] for item in sorted(
        items, key=lambda item: (-item["relevance"], item["id"]))]
# EVOLVE-BLOCK-END
