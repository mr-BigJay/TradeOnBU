from app.domain.candles import Candle
from app.services.pattern_memory import update_drawings


def test_ai_channel_keeps_anchors_then_retires_on_closed_hour():
    h=3600000
    bars=[Candle(100+i,105+i,95+i,101+i,i*h) for i in range(30)]
    request=dict(timeframe="1h",kind="ascending_channel",upper=[h,6*h],lower=[2*h,8*h])
    def review(memory, proposals, data):
        return update_drawings({"analyst_summary":{"drawing_memory":memory}},proposals,{"1h":data},len(data)*h)
    original=review({},[request],bars[:20])
    assert len(original["active"])==1
    follow=review(original,[dict(request,upper=[3*h,9*h])],bars)
    assert len(follow["active"])==1
    before,after=original["active"][0],follow["active"][0]
    assert before["upper"]["anchors"]==after["upper"]["anchors"]
    assert after["upper"]["price"]==before["upper"]["price"]+10
    broken=review(follow,[request],bars+[Candle(130,155,129,152,30*h)])
    assert not broken["active"] and broken["ended"][0]["position"]=="above"
    assert not review({},[dict(request,timeframe="5m"),dict(request,upper=[h,99*h])],bars)["active"]


def test_descending_channel_requires_broad_four_hour_edges_even_when_carried():
    for tf,step,span,expected in (("1h",3600000,12,False),("4h",14400000,5,False),("4h",14400000,6,True)):
        bars=[Candle(150-i,155-i,145-i,150-i,i*step) for i in range(20)]
        row=dict(kind="descending_channel",timeframe=tf,upper=[step,(1+span)*step],
                 lower=[2*step,(2+span)*step])
        result=update_drawings({},[row],{tf:bars},20*step)
        assert bool(result["active"]) == expected
        if expected:
            old=dict(result["active"][0],timeframe="1h")
            previous={"analyst_summary":{"drawing_memory":{"active":[old]}}}
            assert not update_drawings(previous,[],{},20*step)["active"]
