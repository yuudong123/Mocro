"""Freeze additional quantities once, before the first production action."""
from production import product_name


def freeze_targets(targets, bag, include_inventory=True):
    return {name: count + (0 if include_inventory else bag.get(product_name(name), 0))
            for name, count in targets.items()}


def matches_pending(pending, targets, include_inventory):
    return (pending.get('targets') == targets and
            pending.get('include_inventory', True) == include_inventory)
