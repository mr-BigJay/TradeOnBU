from app.services.paper_execution import PaperExecutionEngine


def test_rr_long_acceptable():
    paper = PaperExecutionEngine()
    rr = paper.calc_rr("LONG", 100.0, 98.0, 106.0, min_net_rr=2.0)
    assert rr.gross_rr == 3.0
    assert rr.acceptable is True


def test_rr_reject_small_target():
    paper = PaperExecutionEngine()
    rr = paper.calc_rr("LONG", 100.0, 99.0, 100.5, min_net_rr=2.0)
    assert rr.acceptable is False
