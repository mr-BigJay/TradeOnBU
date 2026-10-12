from app.services.paper_execution import PaperExecutionEngine


def test_rr_long_net_formula():
    paper = PaperExecutionEngine()
    rr = paper.calc_rr("LONG", 100.0, 98.0, 106.0, min_net_rr=1.0)
    assert rr.net_loss_usdt < 0
    assert rr.net_reward_usdt > 0
    assert rr.net_rr == round(rr.net_reward_usdt / abs(rr.net_loss_usdt), 3)


def test_rr_reject_small_target():
    paper = PaperExecutionEngine()
    rr = paper.calc_rr("LONG", 100.0, 99.5, 100.2, min_net_rr=2.0)
    assert rr.acceptable is False
