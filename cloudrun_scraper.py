import datetime
import time
import logging
import os
import json
import re

from flask import Flask, request, jsonify
import psycopg2
from bs4 import BeautifulSoup
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
WHITELIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dse_stocks.json")

def load_whitelist():
    if os.path.exists(WHITELIST_PATH):
        with open(WHITELIST_PATH) as f:
            return set(json.load(f))
    return None

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
    conn.commit()
    cur.close()
    conn.close()

def fetch_live():
    url = "https://dsebd.org/latest_share_price_scroll_by_value.php"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Referer": "https://www.dsebd.org/",
    }
    response = requests.get(url, headers=headers, timeout=30, verify=False)
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
    else:
        filtered = rows

    if not filtered:
        return 0

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
    return len(filtered)

def is_market_open(now):
    if now.weekday() in (4, 5):
        return False
    market_open = now.replace(hour=10, minute=0, second=0, microsecond=0)
    market_close = now.replace(hour=14, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close

def do_scrape():
    now = datetime.datetime.now(bd_now())
    weekday_name = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][now.weekday()]

    if not is_market_open(now):
        return {"status": "skipped", "reason": f"Market closed ({weekday_name} {now.strftime('%H:%M')} BD)"}

    try:
        rows = fetch_live()
        count = save(rows)
        logger.info(f"OK {count} stocks saved @ {now.strftime('%H:%M:%S')} BD")
        return {"status": "ok", "stocks_saved": count, "time": now.strftime('%H:%M:%S BD')}
    except Exception as e:
        logger.warning(f"Attempt 1 failed: {e}, retrying in 5s...")
        time.sleep(5)
        try:
            rows = fetch_live()
            count = save(rows)
            return {"status": "ok_retry", "stocks_saved": count, "time": now.strftime('%H:%M:%S BD')}
        except Exception as e2:
            logger.error(f"RETRY FAIL: {e2}")
            return {"status": "error", "error": str(e2)}

@app.route("/", methods=["GET", "POST"])
def health():
    return jsonify({"status": "healthy", "service": "dse-scraper", "version": "5.1"})

@app.route("/scrape", methods=["GET", "POST"])
def scrape():
    result = do_scrape()
    status_code = 200 if result["status"] in ("ok", "ok_retry", "skipped") else 500
    return jsonify(result), status_code

if __name__ == "__main__":
    try:
        ensure_table()
    except Exception as e:
        logger.warning(f"Table check failed: {e}")
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Starting DSE Scraper v5.1 on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
