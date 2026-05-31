from app.services.account_rollup import allocate_contracts_by_weight


def test_allocate_contracts_by_weight_uses_largest_remainder():
    allocations = allocate_contracts_by_weight(14, {"241405056": 34, "244172640": 15, "239474677": 6})

    assert allocations == {"241405056": 9, "244172640": 4, "239474677": 1}


def test_allocate_contracts_by_weight_preserves_total_for_small_accounts():
    allocations = allocate_contracts_by_weight(10, {"241405056": 19, "244172640": 9, "239474677": 2})

    assert allocations == {"241405056": 6, "244172640": 3, "239474677": 1}
    assert sum(allocations.values()) == 10
