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
