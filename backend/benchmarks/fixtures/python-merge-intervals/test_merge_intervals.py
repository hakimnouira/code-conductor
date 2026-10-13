from merge_intervals import merge_intervals


def test_merges_overlapping_and_adjacent_intervals():
    assert merge_intervals([(8, 10), (1, 3), (2, 6), (6, 7)]) == [(1, 7), (8, 10)]


def test_empty_input_returns_empty_list():
    assert merge_intervals([]) == []


def test_disjoint_and_negative_intervals_are_sorted():
    assert merge_intervals([(3, 4), (-5, -2), (8, 9)]) == [(-5, -2), (3, 4), (8, 9)]


def test_single_interval_is_preserved():
    assert merge_intervals([(2, 5)]) == [(2, 5)]
