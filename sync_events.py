#!/usr/bin/env python3
"""Sync public Bandzoogle list-view events into a JSON feed.

Examples:
  python sync_events.py --html 'Shows and Events _ OKC Jazz Life.html' --output events.json --first-page-only
  python sync_events.py --output events.json

This uses public HTML, not an official Bandzoogle API. Pagination and markup
must be monitored. Fail closed: output is untouched if fetching/parsing fails.
"""
import argparse
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

SOURCE = 'https://okcjazz.life/shows-and-events'
TZ = ZoneInfo('America/Chicago')
LOG = logging.getLogger('okcjazz-sync')


def text_of(element):
    return element.get_text(' ', strip=True) if element else ''


def resolve_day(raw, reference):
    """Bandzoogle list dates omit the year: infer nearest sensible year."""
    candidates = []
    for year in (reference.year - 1, reference.year, reference.year + 1, reference.year + 2):
        try:
            date = dateparser.parse(f'{raw}, {year}', fuzzy=True).date()
        except (ValueError, TypeError, OverflowError):
            continue
        delta = (date - reference.date()).days
        # Allow yesterday's show, and prioritize future shows; do not silently
        # map very old items to a previous year.
        penalty = 0 if -14 <= delta <= 370 else 10000
        candidates.append((penalty + abs(delta), date))
    if not candidates:
        raise ValueError(f'Cannot interpret date: {raw!r}')
    return min(candidates)[1]


def to_iso(date, raw_time):
    if not raw_time:
        return None
    t = dateparser.parse(raw_time).time().replace(second=0, microsecond=0)
    return datetime.combine(date, t, TZ).isoformat()


def parse_html(html, reference):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for node in soup.select('.calendar_feature .event-detail, .event-detail[data-occurrence-id]'):
        name_link = node.select_one('.event-title a')
        title = text_of(name_link)
        raw_day = text_of(node.select_one('.date-long time.from .date')) or text_of(node.select_one('.date-short time.from .date'))
        raw_start = text_of(node.select_one('.date-long time.from .time')) or text_of(node.select_one('.date-short time.from .time'))
        raw_end = text_of(node.select_one('.date-long time.to .time')) or text_of(node.select_one('.date-short time.to .time'))
        venue_link = node.select_one('.event-location a')
        venue_text = text_of(venue_link) or text_of(node.select_one('.event-location'))
        notes = node.select_one('.event-notes')
        description = notes.get_text('\n', strip=True) if notes else ''
        occurrence_id = node.get('data-occurrence-id')
        if not (title and raw_day and occurrence_id and name_link and name_link.get('href')):
            raise ValueError(f'Incomplete event occurrence {occurrence_id!r}: title={title!r}, day={raw_day!r}')
        day = resolve_day(raw_day, reference)
        event_url = urljoin(SOURCE, name_link['href'])
        records.append({
            'event_id': node.get('data-event-id'),
            'occurrence_id': occurrence_id,
            'title': title,
            'date': day.isoformat(),
            'start': to_iso(day, raw_start),
            'end_time_label': raw_end or None,
            'venue': venue_text.split(',')[0].strip() if venue_text else '',
            'location': venue_text,
            'venue_url': urljoin(SOURCE, venue_link['href']) if venue_link and venue_link.get('href') else None,
            'description': description,
            'event_url': event_url,
        })
    return soup, records


def page_urls(soup):
    """Find Bandzoogle's calendar_page pagination URLs, including Last."""
    links = {}
    for a in soup.select('a[href*="calendar_page="]'):
        href = a.get('href', '')
        match = re.search(r'[?&]calendar_page=(\d+)', href)
        if match:
            links[int(match.group(1))] = urljoin(SOURCE, href)
    return links


def download(session, url):
    r = session.get(url, headers={
        'User-Agent': 'OKCJazzLife-CalendarSync/1.0 (public listings)',
        'Accept': 'text/vnd.turbo-stream.html, text/html, */*',
    }, timeout=30)
    r.raise_for_status()
    return r.text


def run():
    ap = argparse.ArgumentParser()
    ap.add_argument('--html', help='Use saved HTML for offline inspection')
    ap.add_argument('--first-page-only', action='store_true', help='Only for offline tests; do not publish')
    ap.add_argument('--output', default='events.json')
    ap.add_argument('--max-pages', type=int, default=60)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    now = datetime.now(TZ)
    session = requests.Session()
    first_html = Path(args.html).read_text(encoding='utf-8') if args.html else download(session, SOURCE)
    soup, first = parse_html(first_html, now)
    if not first:
        raise RuntimeError('Zero events found on first page; refusing to overwrite JSON')
    found = {e['occurrence_id']: e for e in first}
    LOG.info('Page 1: %d events', len(first))

    pagination = page_urls(soup)
    last_page = max(pagination.keys(), default=1)
    if last_page > args.max_pages:
        raise RuntimeError(f'Unexpected pagination depth {last_page} > {args.max_pages}')
    if not args.first_page_only and last_page > 1:
        # Use page 2 URL as a template for subsequent pages. Bandzoogle
        # exposes these links in the saved HTML, but live endpoint needs testing.
        template = pagination.get(2)
        if not template:
            raise RuntimeError('Pagination exists, but no page-2 URL was found')
        for page in range(2, last_page + 1):
            url = re.sub(r'([?&]calendar_page=)\d+', rf'\g<1>{page}', template)
            html = download(session, url)
            _, events = parse_html(html, now)
            if not events:
                raise RuntimeError(f'Page {page} returned no event records; refusing partial update')
            new_count = sum(1 for e in events if e['occurrence_id'] not in found)
            if new_count == 0:
                raise RuntimeError(f'Page {page} contained no new events; pagination may be repeating or cached')
            LOG.info('Page %d: %d events (%d new)', page, len(events), new_count)
            for e in events:
                found[e['occurrence_id']] = e
    elif args.first_page_only:
        LOG.warning('TEST MODE: Only first page parsed; not suitable as production feed')

    results = sorted(found.values(), key=lambda e: (e['date'], e['start'] or '', e['title']))
    doc = {
        'source': SOURCE,
        'generated_at': now.isoformat(),
        'timezone': 'America/Chicago',
        'event_count': len(results),
        'complete_pagination': not args.first_page_only,
        'events': results,
    }
    dest = Path(args.output)
    if dest.exists():
        old = json.loads(dest.read_text(encoding='utf-8'))
        if old.get('events') == results and old.get('complete_pagination') is True:
            LOG.info('No event changes; keeping existing feed and timestamp')
            return
    tmp = dest.with_suffix(dest.suffix + '.tmp')
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(dest)
    LOG.info('Wrote %s with %d events', dest, len(results))


if __name__ == '__main__':
    run()
