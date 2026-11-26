"""Public source context for each four-hour review; all times normalized to UTC."""
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx

CALENDAR = "https://www.bls.gov/schedule/news_release/bls.ics"
NEWS = "https://feeds.bbci.co.uk/news/world/rss.xml"


def calendar_window(text, now_ms):
    if "BEGIN:VCALENDAR" not in text:
        raise ValueError("invalid_calendar")
    upcoming = []
    for block in re.split(r"BEGIN:VEVENT", re.sub(r"\r?\n[ \t]", "", text))[1:]:
        title = re.search(r"(?m)^SUMMARY:(.+)", block)
        start = re.search(r"(?m)^DTSTART(?:;TZID=([^:\r\n]+))?:(\d{8}T\d{6})(Z?)", block)
        if not start or not title:
            continue
        name = title[1].strip()
        if not any(term in name.lower() for term in ("consumer price index","employment situation","producer price index")):
            continue
        zone = timezone.utc if start[3] else ZoneInfo((start[1] or "America/New_York").strip('"'))
        stamp = datetime.strptime(start[2], "%Y%m%dT%H%M%S").replace(tzinfo=zone).timestamp()*1000
        if now_ms-30*60_000 <= stamp <= now_ms+4*3_600_000:
            upcoming.append((stamp,name))
    if upcoming:
        stamp, title = min(upcoming)
        return {"status":"pause","title":title,"source":CALENDAR,"release_ms":int(stamp)}
    return {"status":"no_listed_event"}


def headlines(text, now_ms):
    result = []
    for item in ElementTree.fromstring(text).findall("./channel/item"):
        date, title = item.findtext("pubDate"), item.findtext("title") or ""
        if not date or not re.search(r"\b(invasion|war|ceasefire|airstrikes?|missile|sanctions)\b",title,re.I):
            continue
        stamp = parsedate_to_datetime(date).timestamp()*1000
        if 0 <= now_ms-stamp <= 12*3_600_000:
            result.append({"title":title[:180],"source":item.findtext("link"),"time_ms":int(stamp)})
    return result[:4]


async def enrich(event, now_ms):
    async with httpx.AsyncClient(timeout=4, follow_redirects=True) as client:
        async def get(url, parser):
            try:
                response = await client.get(url)
                response.raise_for_status()
                return parser(response.text, now_ms)
            except (httpx.HTTPError, ValueError, KeyError, ElementTree.ParseError, OverflowError):
                return None
        calendar, news = await asyncio.gather(get(CALENDAR,calendar_window),get(NEWS,headlines))
    selected = event
    if event["status"] != "pause" and calendar:
        selected = calendar
    return {**selected, "calendar_available":calendar is not None,
            "news_available":news is not None, "headlines":news or []}
