import datetime
import time
import logging
import socket
import os
import json
import re

import psycopg2
from bs4 import BeautifulSoup
from curl_cffi import requests as c_requests
from curl_cffi.curl import CurlOpt

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable not set")

WHITELIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dse_stocks.json")

def load_whitelist():
    if os.path.exists(WHITELIST_PATH):
        with open(WHITELIST_PATH) as f:
            return set(json.load(f))
    return None

def is_bond(symbol):
    s = symbol.upper()
    if re.match(r"^TB\d+Y\d+$", s): return True
    if "BOND" in s: return True
    if s.startswith("IBBL"): return True
    return False

_orig_getaddrinfo = socket.getaddrinfo
def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _getaddrinfo_ipv4

def bd_now():
    return datetime.timezone(datetime.timedelta(hours=6))

def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def ensure_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            symbol VARCHAR(50) NOT NULL,
            date DATE NOT NULL,
            open DOUBLE PRECISION,
            high DOUBLE PRECISION,
            low DOUBLE PRECISION,
            close DOUBLE PRECISION,
            volume DOUBLE PRECISION,
            ycp DOUBLE PRECISION,
            "change" DOUBLE PRECISION,
            PRIMARY KEY (symbol, date)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_prices_symbol ON prices(symbol)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_prices_symbol_date ON prices(symbol, date DESC)")
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Table prices ensured")

def fetch_live():
    url = "https://dsebd.org/latest_share_price_scroll_by_value.php"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.dsebd.org/"
    }
    response = c_requests.get(url, impersonate="chrome110", headers=headers, curl_options={CurlOpt.IPRESOLVE: 1}, timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")
    table = soup.find("table", class_="shares-table")
    if not table:
        raise ValueError("Table not found on DSE page")

    rows = []
    for tr in table.find_all("tr")[1:]:
        tds = [td.text.strip() for td in tr.find_all("td")]
        if len(tds) < 11:
            continue
        def cv(val):
            try: return float(val.replace(",", ""))
            except: return None

        ltp = cv(tds[2])
        high = cv(tds[3])

        if ltp is not None and high is not None and high > 0 and ltp < high * 0.1:
            logger.warning(f"SKIP {tds[1].strip().upper()}: LTP={ltp} vs HIGH={high} (corrupted)")
            continue

        close_val = cv(tds[2])
        ycp_val = cv(tds[6])
        calculated_change = None
        if close_val is not None and ycp_val is not None and ycp_val > 0:
            calculated_change = round(close_val - ycp_val, 2)

        rows.append((
            tds[1].strip().upper(),
            datetime.datetime.now(bd_now()).strftime("%Y-%m-%d"),
            cv(tds[5]), cv(tds[3]), cv(tds[4]), close_val,
            ycp_val, calculated_change, cv(tds[10])
        ))
    return rows

def save(rows):
    whitelist = load_whitelist()
    if whitelist:
        filtered = [r for r in rows if r[0] in whitelist]
        skipped = len(rows) - len(filtered)
        if skipped > 0:
            logger.info(f"Filtered {skipped} non-stock symbols")
    else:
        filtered = [r for r in rows if not is_bond(r[0])]

    if not filtered:
        return

    conn = get_conn()
    cur = conn.cursor()
    for row in filtered:
        cur.execute("""
            INSERT INTO prices (symbol, date, open, high, low, close, ycp, "change", volume)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, date) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, ycp = EXCLUDED.ycp, "change" = EXCLUDED."change",
                volume = EXCLUDED.volume
        """, row)
    conn.commit()
    cur.close()
    conn.close()

def is_market_open(now):
    if now.weekday() in (4, 5):
        return False
    market_open = now.replace(hour=10, minute=0, second=0, microsecond=0)
    market_close = now.replace(hour=14, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close

def next_market_open(now):
    target = now.replace(hour=10, minute=0, second=0, microsecond=0)
    if now.hour >= 10 and now.weekday() not in (4, 5):
        target += datetime.timedelta(days=1)
    if target.weekday() == 5:
        target += datetime.timedelta(days=2)
    elif target.weekday() == 4:
        target += datetime.timedelta(days=3)
    if now.hour < 10 and now.weekday() not in (4, 5):
        target = now.replace(hour=10, minute=0, second=0, microsecond=0)
    delta = (target - now).total_seconds()
    return max(delta, 60)

def main():
    logger.info("LIVE SCRAPER v4 (Neon) - Sun-Thu 10:00-13:55 BD, every 2 min")
    ensure_table()
    final_scraped = False

    while True:
        now = datetime.datetime.now(bd_now())

        if is_market_open(now):
            final_scraped = False
            try:
                rows = fetch_live()
                save(rows)
                logger.info(f"OK {len(rows)} tickers @ {now.strftime('%H:%M:%S')} BD")
            except Exception as e:
                logger.warning(f"Retry in 30s: {e}")
                time.sleep(30)
                try:
                    rows = fetch_live()
                    save(rows)
                    logger.info(f"RETRY OK {len(rows)} tickers @ {datetime.datetime.now(bd_now()).strftime('%H:%M:%S')} BD")
                except Exception as e2:
                    logger.error(f"RETRY FAIL: {e2}")
            time.sleep(120)
        else:
            weekday_name = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][now.weekday()]
            if now.hour >= 10 and now.weekday() not in (4, 5) and not final_scraped:
                try:
                    rows = fetch_live()
                    save(rows)
                    logger.info(f"FINAL scrape {len(rows)} tickers @ {now.strftime('%H:%M:%S')} BD")
                    final_scraped = True
                except:
                    pass
            sleep_secs = next_market_open(now)
            wake_at = now + datetime.timedelta(seconds=sleep_secs)
            logger.info(f"Market closed ({weekday_name} {now.strftime('%H:%M')} BD). Sleeping {sleep_secs/3600:.1f}h until {wake_at.strftime('%a %H:%M')} BD...")
            time.sleep(sleep_secs)

if __name__ == "__main__":
    main()
