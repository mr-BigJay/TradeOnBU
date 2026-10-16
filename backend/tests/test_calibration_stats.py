from app.services.paper_calibration_stats import walk_forward_binary_rate, wilson_interval


def test_wilson_interval_bounds():
    lo, hi = wilson_interval(14, 20)
    assert lo is not None and hi is not None
    assert 0 <= lo <= hi <= 1


def test_walk_forward_oos_split():
    wf = walk_forward_binary_rate([True] * 15 + [False] * 5)
    assert wf.train_n >= 1
    assert wf.oos_n >= 5
