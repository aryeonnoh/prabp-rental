import os
import re
import time
import math
import base64
import hashlib
import json
import unicodedata
import calendar
from html import escape
from io import BytesIO
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from supabase import create_client

# OCR 기능은 배포 환경에 패키지가 없을 때도 앱 전체가 죽지 않도록 optional import로 처리
try:
    import pytesseract
except Exception:
    pytesseract = None

try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None
    np = None

try:
    from rapidfuzz import process as rf_process, fuzz as rf_fuzz
except Exception:
    rf_process = None
    rf_fuzz = None


DB_PATH = "rental.db"  # 로컬 백업용 이름. Supabase 버전에서는 직접 사용하지 않음
THUMB_DIR = "thumbnails"
QUOTE_DIR = "quote_files"
DEFAULT_CATEGORY_URL = "https://proprop.cafe24.com/skin-skin10/category/%EA%B0%80%EA%B5%AC/24/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

ACTIVE_RENTAL_STATUSES = ("확정", "출고완료", "대여중")


# -----------------------------
# 공통 유틸
# -----------------------------

def clean_text(text):
    return re.sub(r"\s+", " ", str(text)).strip()


def money(value):
    try:
        return f"{int(float(value)):,}원"
    except Exception:
        return "0원"


def safe_int(value, default=0):
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    try:
        return int(float(str(value).replace(",", "")))
    except Exception:
        return default


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")



def today_yyyymmdd():
    return datetime.now().strftime("%Y%m%d")


def normalize_date_text(value, allow_blank=False):
    """다양한 날짜 입력을 DB 표준 YYYY-MM-DD로 정규화한다.

    지원 예: 20260618, 2026/06/18, 2026.06.18, 2026.06.18., 2026-06-18
    """
    if value is None:
        if allow_blank:
            return ""
        raise ValueError("날짜를 입력해야 합니다.")
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    text = str(value).strip()
    if not text:
        if allow_blank:
            return ""
        raise ValueError("날짜를 입력해야 합니다.")

    # ISO datetime이 들어온 경우 날짜 부분만 사용한다.
    text = text.split("T", 1)[0].strip().rstrip(".")
    groups = re.findall(r"\d+", text)
    if len(groups) == 1:
        digits = groups[0]
        if len(digits) != 8:
            raise ValueError("날짜는 20260618 또는 2026/06/18 형식으로 입력하세요.")
        year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
    elif len(groups) >= 3:
        year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
    else:
        raise ValueError("날짜 형식을 확인하세요.")

    return date(year, month, day).isoformat()


def try_normalize_date_text(value, allow_blank=True):
    try:
        return normalize_date_text(value, allow_blank=allow_blank)
    except Exception:
        return "" if allow_blank else None


def date_input_help():
    return "예: 20260618 / 2026.06.18 / 2026/06/18"


def combine_team_person(team_name, person_name):
    team = clean_text(team_name)
    person = clean_text(person_name)
    if team and person:
        return f"{team}-{person}"
    return team or person


def split_team_person(value):
    text = clean_text(value)
    if "-" not in text:
        return text, ""
    team, person = text.rsplit("-", 1)
    return team.strip(), person.strip()


@st.cache_data(ttl=60, show_spinner=False)
def get_holiday_dates():
    raw = get_meta("holiday_dates", "[]")
    try:
        values = json.loads(raw) if raw else []
    except Exception:
        values = []
    out = set()
    for value in values:
        normalized = try_normalize_date_text(value)
        if normalized:
            out.add(normalized)
    return out


def save_holiday_dates(values):
    normalized = sorted({normalize_date_text(v) for v in values if str(v).strip()})
    set_meta("holiday_dates", json.dumps(normalized, ensure_ascii=False))
    clear_data_cache()


def rental_pricing_context(pickup_date, return_date, holidays=None):
    """엑셀의 연박 계산 기준을 앱용으로 정리한다.

    - 일요일과 사용자 지정 휴일은 영업일에서 제외
    - 2박 3일까지 기본 2배(당일/1박은 1배)
    - NETWORKDAYS.INTL(..., "0000001", 휴일)-3 만큼 연박 추가
    """
    pickup_s = normalize_date_text(pickup_date)
    return_s = normalize_date_text(return_date)
    start = datetime.strptime(pickup_s, "%Y-%m-%d").date()
    end = datetime.strptime(return_s, "%Y-%m-%d").date()
    if end < start:
        raise ValueError("반납 날짜가 픽업 날짜보다 빠릅니다.")

    holidays = set(holidays if holidays is not None else get_holiday_dates())
    cursor = start
    billable_dates = []
    while cursor <= end:
        # Python weekday: Monday=0, Sunday=6
        if cursor.weekday() != 6 and cursor.isoformat() not in holidays:
            billable_dates.append(cursor.isoformat())
        cursor += timedelta(days=1)

    stay_nights = max((end - start).days, 0)
    base_multiplier = 1 if stay_nights <= 1 else 2
    extra_days = max(0, len(billable_dates) - 3)
    multiplier = max(1, base_multiplier + extra_days)
    return {
        "pickup_date": pickup_s,
        "return_date": return_s,
        "stay_nights": stay_nights,
        "billable_days": len(billable_dates),
        "billable_dates": billable_dates,
        "base_multiplier": base_multiplier,
        "extra_days": extra_days,
        "multiplier": multiplier,
    }


def quote_price_multiplier(pickup_date, return_date):
    try:
        return rental_pricing_context(pickup_date, return_date)["multiplier"]
    except Exception:
        return 1


def calculate_line_total(quantity, unit_price, pickup_date, return_date, multiplier=None):
    factor = max(safe_int(multiplier, 0), 1) if multiplier is not None else quote_price_multiplier(pickup_date, return_date)
    return max(safe_int(quantity, 1), 1) * max(safe_int(unit_price, 0), 0) * factor


def parse_item_note(value):
    """quote_items.availability_note에 저장한 상품별 날짜 JSON을 읽는다."""
    if value is None:
        return {}
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def make_item_note(pickup_date, return_date, existing=None):
    """상품별 픽업/반납 날짜를 availability_note 텍스트에 저장한다."""
    data = parse_item_note(existing)
    data["pickup_date"] = normalize_date_text(pickup_date)
    data["return_date"] = normalize_date_text(return_date)
    return json.dumps(data, ensure_ascii=False)


def get_item_dates(item, quote=None):
    """견적 상품의 개별 날짜를 반환한다. 없으면 견적서 전체 날짜를 사용한다."""
    note = parse_item_note(item.get("availability_note", "") if hasattr(item, "get") else "")
    pickup = note.get("pickup_date") or (quote or {}).get("pickup_date")
    ret = note.get("return_date") or (quote or {}).get("return_date")
    return normalize_date_text(pickup), normalize_date_text(ret)


def update_quote_date_range_from_items(quote_id):
    """상품별 날짜의 최소/최대값으로 견적서 대표 날짜를 맞춘다."""
    quote = get_quote(quote_id)
    items = load_quote_items_df(quote_id)
    if not quote or items.empty:
        return
    pickups = []
    returns = []
    for _, item in items.iterrows():
        try:
            p, r = get_item_dates(item, quote)
            pickups.append(p)
            returns.append(r)
        except Exception:
            pass
    if not pickups or not returns:
        return
    new_pickup = min(pickups)
    new_return = max(returns)
    supabase_client().table("quotes").update({
        "pickup_date": new_pickup,
        "return_date": new_return,
        "updated_at": datetime.now().isoformat(),
    }).eq("quote_id", int(quote_id)).execute()


def set_all_quote_item_dates(quote_id, pickup_date, return_date, reprice=True):
    """전체 날짜 수정 시 모든 견적 상품 날짜를 같은 날짜로 덮어쓴다."""
    quote = get_quote(quote_id)
    if not quote:
        return
    items = load_quote_items_df(quote_id)
    client = supabase_client()
    pickup = normalize_date_text(pickup_date)
    ret = normalize_date_text(return_date)
    multiplier = quote_price_multiplier(pickup, ret)
    for _, item in items.iterrows():
        qty = max(safe_int(item.get("quantity", 1), 1), 1)
        unit_price = max(safe_int(item.get("unit_price", 0), 0), 0)
        payload = {
            "availability_note": make_item_note(pickup, ret, item.get("availability_note", "")),
            "updated_at": datetime.now().isoformat(),
        }
        if reprice:
            payload["line_total"] = calculate_line_total(qty, unit_price, pickup, ret, multiplier=multiplier)
        client.table("quote_items").update(payload).eq("id", int(item["id"])).execute()


def update_quote_item_dates(item_id, quote_id, pickup_date, return_date):
    """견적 상품 하나의 날짜를 수정하고 해당 날짜 기준으로 가격을 다시 계산한다.

    모든 상태에서 날짜 수정이 가능하다. 단, 수정이 발생하면 기존 대여 기록을 해제하고
    견적중 상태로 전환한다. 상품별 연박이 다를 수 있으므로 해당 상품 금액과 견적 총액을
    다시 계산한다.
    """
    try:
        pickup = normalize_date_text(pickup_date)
        ret = normalize_date_text(return_date)
    except Exception as e:
        return False, [str(e)]
    if ret < pickup:
        return False, ["반납 날짜가 픽업 날짜보다 빠릅니다."]

    quote = get_quote(quote_id)
    if not quote:
        return False, ["견적서를 찾지 못했습니다."]

    item_rows = supabase_client().table("quote_items").select("*").eq("id", int(item_id)).limit(1).execute().data or []
    if not item_rows:
        return False, ["견적 상품을 찾지 못했습니다."]
    item = item_rows[0]
    product_no = str(item.get("product_no", ""))
    product = get_product(product_no) or {}
    total_qty = max(safe_int(product.get("qty", 1), 1), 1)
    qty = max(safe_int(item.get("quantity", 1), 1), 1)
    if qty > total_qty:
        return False, [f"보유수량이 {total_qty}개입니다."]

    status_one = bulk_product_status((product_no,), pickup, ret, int(quote_id)).get(product_no, {})
    reserved = safe_int(status_one.get("reserved", 0), 0)
    if total_qty - reserved < qty:
        return False, [f"{item.get('product_name', product_no)}은 해당 날짜에 다른 대여와 겹칩니다. 필요 {qty}개 / 가능 {max(total_qty - reserved, 0)}개"]

    ok, message = reopen_quote_for_edit(quote_id)
    if not ok:
        return False, [message]

    line_total = calculate_line_total(qty, safe_int(item.get("unit_price", 0), 0), pickup, ret)
    supabase_client().table("quote_items").update({
        "availability_note": make_item_note(pickup, ret, item.get("availability_note", "")),
        "line_total": line_total,
        "updated_at": datetime.now().isoformat(),
    }).eq("id", int(item_id)).execute()

    update_quote_date_range_from_items(quote_id)
    update_quote_totals(quote_id, reprice=False)
    clear_quote_cache()
    return True, ([message] if message else [])


def pricing_summary_text(pickup_date, return_date):
    try:
        ctx = rental_pricing_context(pickup_date, return_date)
        billable = ctx.get("billable_days", 0)
        multiplier = ctx.get("multiplier", 1)
        extra = ctx.get("extra_days", 0)
        if extra and extra > 0:
            return f"요금 기준: 일요일/휴일 제외 {billable}일 청구 · 기본 2일 + 추가 {extra}일 · 단가 {multiplier}배 적용"
        return f"요금 기준: 일요일/휴일 제외 {billable}일 청구 · 단가 {multiplier}배 적용"
    except Exception:
        return "날짜를 입력하면 요금 기준이 계산됩니다."


def format_datetime_short(value):
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "nan", "nat"}:
        return ""
    try:
        dt = pd.to_datetime(text)
        if pd.isna(dt):
            return ""
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        text = text.replace("T", " ")
        text = re.sub(r"\.\d+", "", text)
        text = re.sub(r"\+00:00$", "", text)
        return text[:19]


def valid_status_text(value):
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "nan", "nat"}:
        return ""
    return text


def render_quote_detail_header(quote):
    status = valid_status_text(quote.get("status")) or "견적중"
    created = format_datetime_short(quote.get("created_at"))
    st.markdown(f"""
    <div class='quote-detail-head'>
        <div class='quote-status-line'>{status_badge(status, status_kind(status))}</div>
        <div class='quote-title-line'>{escape(str(quote.get('quote_no', '')))} / {escape(str(quote.get('team_name', '')))}</div>
        <div class='quote-meta-grid'>
            <div><span>대여일</span><strong>{escape(str(quote.get('pickup_date', '')))} ~ {escape(str(quote.get('return_date', '')))}</strong></div>
            <div><span>총액</span><strong>{escape(money(quote.get('total', 0)))}</strong></div>
            <div><span>작성일</span><strong>{escape(created or '-')}</strong></div>
        </div>
        <div class='quote-pricing-line'>{escape(pricing_summary_text(quote.get('pickup_date'), quote.get('return_date')))}</div>
    </div>
    """, unsafe_allow_html=True)


def scroll_to_top_once():
    if st.session_state.pop("scroll_to_top", False):
        components.html("""
        <script>
        setTimeout(function(){
            try { window.parent.scrollTo({top: 0, behavior: 'smooth'}); } catch(e) { window.parent.scrollTo(0, 0); }
        }, 80);
        </script>
        """, height=0)


# -----------------------------
# Supabase DB
# -----------------------------

@st.cache_resource
def supabase_client():
    """Streamlit secrets에 저장된 Supabase 정보로 클라이언트를 만든다."""
    url = st.secrets.get("SUPABASE_URL", "")
    key = st.secrets.get("SUPABASE_SERVICE_KEY", "") or st.secrets.get("SUPABASE_ANON_KEY", "")

    if not url or not key:
        st.error("Supabase 연결정보가 없습니다. .streamlit/secrets.toml 또는 Streamlit Cloud Secrets를 확인하세요.")
        st.stop()

    return create_client(url, key)


def chunked(items, size=500):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def clear_data_cache():
    """데이터 변경 후 Streamlit 데이터 캐시를 비운다."""
    try:
        st.cache_data.clear()
    except Exception:
        pass


def clear_quote_cache():
    """견적/대여 관련 캐시만 비운다.

    상품 페이지 캐시는 유지해서 버튼 클릭 후 반응 속도를 줄인다.
    상품 동기화나 가격 CSV 반영처럼 products가 바뀌는 작업은 기존 clear_data_cache()를 사용한다.
    """
    for name in [
        "load_quotes_df",
        "bulk_product_status",
        "cached_quote_png_bytes",
        "cached_quote_pdf_bytes",
    ]:
        fn = globals().get(name)
        try:
            if fn is not None and hasattr(fn, "clear"):
                fn.clear()
        except Exception:
            pass


def table_all(table_name, select="*", order_col=None, desc=False, page_size=1000):
    """Supabase REST 기본 제한을 피하기 위해 range로 모든 데이터를 가져온다."""
    client = supabase_client()
    rows = []
    start = 0

    while True:
        q = client.table(table_name).select(select)
        if order_col:
            q = q.order(order_col, desc=desc)
        res = q.range(start, start + page_size - 1).execute()
        data = res.data or []
        rows.extend(data)
        if len(data) < page_size:
            break
        start += page_size

    return rows


def df_from_rows(rows, columns=None):
    df = pd.DataFrame(rows or [])
    if columns:
        for c in columns:
            if c not in df.columns:
                df[c] = None
        df = df[columns]
    return df


def init_db():
    """Supabase 버전에서는 로컬 테이블을 만들지 않고, 임시 폴더만 준비한다."""
    os.makedirs(THUMB_DIR, exist_ok=True)
    os.makedirs(QUOTE_DIR, exist_ok=True)
    # 연결 테스트는 너무 자주 하지 않기 위해 생략한다. 실제 호출 때 실패하면 에러 표시.


def get_meta(key, default=""):
    try:
        res = supabase_client().table("app_settings").select("value").eq("key", str(key)).limit(1).execute()
        data = res.data or []
        return data[0].get("value", default) if data else default
    except Exception:
        return default


def set_meta(key, value):
    try:
        supabase_client().table("app_settings").upsert({
            "key": str(key),
            "value": str(value),
            "updated_at": datetime.now().isoformat(),
        }, on_conflict="key").execute()
    except Exception as e:
        st.warning(f"설정 저장 실패: {e}")


def require_app_password():
    """앱 전체를 간단한 비밀번호로 보호한다."""
    app_password = st.secrets.get("APP_PASSWORD", "")
    if not app_password:
        return

    if st.session_state.get("app_authed"):
        return

    st.markdown("# 프라비")
    st.caption("렌탈 재고·견적 관리 시스템")
    pw = st.text_input("비밀번호", type="password")

    col1, col2 = st.columns([1, 3])
    with col1:
        if st.button("입장", use_container_width=True):
            if pw == app_password:
                st.session_state["app_authed"] = True
                st.rerun()
            else:
                st.error("비밀번호가 맞지 않습니다.")

    st.stop()

# -----------------------------
# 상품 동기화 / 크롤링
# -----------------------------

def absolute_url(base_url, maybe_url):
    if not maybe_url:
        return ""
    maybe_url = maybe_url.strip()
    if maybe_url.startswith("//"):
        return "https:" + maybe_url
    return urljoin(base_url, maybe_url)


def set_page_param(url, page):
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs["page"] = [str(page)]
    new_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def extract_product_no(detail_url):
    m = re.search(r"/product/[^/]+/(\d+)(?:/|$)", detail_url)
    if m:
        return m.group(1)
    m = re.search(r"product_no=(\d+)", detail_url)
    if m:
        return m.group(1)
    return ""


def extract_between(text, start_label, end_labels):
    text = clean_text(text)
    pattern = re.escape(start_label) + r"\s*:?\s*(.*?)"
    if end_labels:
        pattern += r"(?=" + "|".join(re.escape(x) for x in end_labels) + r"|$)"
    else:
        pattern += r"$"
    m = re.search(pattern, text)
    if not m:
        return ""
    return clean_text(m.group(1))


def get_img_url(card, base_url):
    imgs = card.find_all("img")
    candidates = []
    for img in imgs:
        for attr in ["ec-data-src", "data-src", "data-original", "data-lazy", "data-lazy-src", "src"]:
            val = img.get(attr)
            if not val:
                continue
            val = val.strip()
            if not val:
                continue
            low = val.lower()
            if "blank" in low or "loading" in low or "noimage" in low:
                continue
            candidates.append(absolute_url(base_url, val))

    for url in candidates:
        low = url.lower()
        if "/web/product/" in low or "/product/" in low:
            return url
    return candidates[0] if candidates else ""


def download_thumbnail(thumbnail_url, product_no):
    if not thumbnail_url:
        return ""

    safe_name = re.sub(r"[^0-9A-Za-z가-힣_-]", "_", str(product_no))
    local_path = os.path.join(THUMB_DIR, f"{safe_name}.jpg")

    try:
        headers = dict(HEADERS)
        headers["Referer"] = "https://proprop.cafe24.com/"
        r = requests.get(thumbnail_url, headers=headers, timeout=20)
        r.raise_for_status()

        img = Image.open(BytesIO(r.content))
        img = img.convert("RGB")
        img.thumbnail((420, 420))
        img.save(local_path, "JPEG", quality=88)
        return local_path
    except Exception:
        return ""


def parse_product_card(card, link, page_url, category_title):
    detail_url = absolute_url(page_url, link.get("href", ""))
    product_no = extract_product_no(detail_url)
    if not product_no:
        return None

    text = clean_text(card.get_text(" ", strip=True))

    name = extract_between(text, "상품명", ["상품요약정보", "상품간략설명", "가용수량"])
    if not name:
        name = clean_text(link.get_text(" ", strip=True))
    name = name.replace("상품명 :", "").replace("상품명", "").strip()

    if not name:
        img = card.find("img")
        if img and img.get("alt"):
            name = clean_text(img.get("alt"))

    size_text = extract_between(text, "상품요약정보", ["상품간략설명", "가용수량"])
    short_desc = extract_between(text, "상품간략설명", ["가용수량"])

    qty = 1
    qty_match = re.search(r"가용수량\s*:?\s*(\d+)", text)
    if qty_match:
        qty = int(qty_match.group(1))

    thumbnail_url = get_img_url(card, page_url)
    local_thumb = download_thumbnail(thumbnail_url, product_no)

    return {
        "product_no": product_no,
        "name": name,
        "category": category_title,
        "size_text": size_text,
        "short_desc": short_desc,
        "qty": qty,
        "detail_url": detail_url,
        "thumbnail_url": thumbnail_url,
        "local_thumbnail_path": local_thumb,
        "source_page": page_url,
        "updated_at": now_text(),
    }


def parse_products_from_page(page_url):
    r = requests.get(page_url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    category_title = ""
    h2 = soup.find("h2")
    if h2:
        category_title = clean_text(h2.get_text(" ", strip=True))

    products = []
    seen = set()

    cards = soup.select('li[id^="anchorBoxId_"]')
    for card in cards:
        link = card.find("a", href=lambda h: h and "/product/" in h)
        if not link:
            continue
        item = parse_product_card(card, link, page_url, category_title)
        if item and item["product_no"] not in seen:
            seen.add(item["product_no"])
            products.append(item)

    if not products:
        links = soup.find_all("a", href=lambda h: h and "/product/" in h)
        for link in links:
            detail_url = absolute_url(page_url, link.get("href", ""))
            product_no = extract_product_no(detail_url)
            if not product_no or product_no in seen:
                continue
            card = link.find_parent("li") or link.find_parent("div") or link.parent
            if not card:
                continue
            item = parse_product_card(card, link, page_url, category_title)
            if item and item["product_no"] not in seen:
                seen.add(item["product_no"])
                products.append(item)

    debug = {
        "status_code": r.status_code,
        "html_length": len(r.text),
        "cards_found": len(cards),
        "product_links_found": len(soup.find_all("a", href=lambda h: h and "/product/" in h)),
        "products_parsed": len(products),
    }
    return products, debug


def upsert_products(products):
    client = supabase_client()
    existing_rows = table_all("products", select="product_no,price,updated_at")
    existing_price = {str(r.get("product_no")): safe_int(r.get("price", 0), 0) for r in existing_rows}
    existing_updated_at = {str(r.get("product_no")): str(r.get("updated_at") or "") for r in existing_rows}
    existing_set = set(existing_price.keys())

    rows = []
    added = 0
    updated = 0

    for p in products:
        product_no = str(p.get("product_no", "")).strip()
        if not product_no:
            continue

        if product_no in existing_set:
            updated += 1
        else:
            added += 1

        rows.append({
            "product_no": product_no,
            "name": p.get("name", ""),
            "category": p.get("category", ""),
            "size_text": p.get("size_text", ""),
            "short_desc": p.get("short_desc", ""),
            "qty": safe_int(p.get("qty", 1), 1),
            "price": existing_price.get(product_no, 0),
            "detail_url": p.get("detail_url", ""),
            "thumbnail_url": p.get("thumbnail_url", ""),
            "local_thumbnail_path": p.get("local_thumbnail_path", ""),
            "source_page": p.get("source_page", ""),
            "updated_at": existing_updated_at.get(product_no) or datetime.now().isoformat(),
        })

    for batch in chunked(rows, 300):
        client.table("products").upsert(batch, on_conflict="product_no").execute()

    clear_data_cache()
    return added, updated


# -----------------------------
# 상품 / 대여 가능 여부
# -----------------------------

@st.cache_data(ttl=60, show_spinner=False)
def load_products_page(search_text="", page=1, page_size=30):
    """Supabase에서 현재 페이지에 필요한 상품만 안전하게 가져온다.

    검색 결과가 줄어든 뒤 이전 페이지 번호가 남아 있어도 먼저 전체 개수를
    확인하고 유효한 페이지로 보정하므로 PGRST103 오류가 발생하지 않는다.
    """
    client = supabase_client()
    page = max(int(page or 1), 1)
    page_size = max(int(page_size or 30), 1)
    txt = str(search_text or "").strip()

    select_cols = (
        "product_no,name,category,size_text,short_desc,qty,price,detail_url,"
        "thumbnail_url,local_thumbnail_path,updated_at"
    )

    def apply_search(query):
        if not txt:
            return query
        # PostgREST의 or 필터 구문이 깨지지 않도록 쉼표를 공백으로 치환한다.
        safe = txt.replace(",", " ").strip()
        pattern = f"%{safe}%"
        return query.or_(
            f"name.ilike.{pattern},product_no.ilike.{pattern},"
            f"size_text.ilike.{pattern},category.ilike.{pattern}"
        )

    # 범위를 요청하기 전에 전체 결과 수를 먼저 확인한다. 검색 결과가 1개인데
    # 이전 페이지(예: 2페이지)의 offset 20을 요청하면 PostgREST가 416/PGRST103을
    # 반환하므로, 반드시 먼저 페이지를 보정해야 한다.
    count_query = client.table("products").select("product_no", count="exact")
    count_res = apply_search(count_query).limit(1).execute()
    total = int(getattr(count_res, "count", None) or 0)

    cols = [
        "product_no", "name", "category", "size_text", "short_desc", "qty", "price",
        "detail_url", "thumbnail_url", "local_thumbnail_path", "updated_at"
    ]
    if total <= 0:
        return df_from_rows([], cols), 0

    total_pages = max(1, math.ceil(total / page_size))
    page = min(page, total_pages)
    start = (page - 1) * page_size
    end = min(start + page_size - 1, total - 1)

    data_query = client.table("products").select(select_cols)
    data_query = apply_search(data_query).order("updated_at", desc=True).order("product_no", desc=True)
    res = data_query.range(start, end).execute()
    rows = res.data or []
    return df_from_rows(rows, cols), total


def reset_page_when_search_changes(search_text, page_key, tracker_key):
    """검색어가 달라지면 페이지를 1로 되돌린다."""
    current = str(search_text or "").strip()
    previous = st.session_state.get(tracker_key)
    if previous != current:
        st.session_state[tracker_key] = current
        st.session_state[page_key] = 1


@st.cache_data(ttl=60, show_spinner=False)
def count_products_fast(search_text=""):
    _, total = load_products_page(search_text=search_text, page=1, page_size=1)
    return total


def load_products_df():
    """호환용 전체 로드. 화면 렌더링에서는 load_products_page를 우선 사용한다."""
    rows = table_all("products")
    cols = [
        "product_no", "name", "category", "size_text", "short_desc", "qty", "price",
        "detail_url", "thumbnail_url", "local_thumbnail_path", "updated_at"
    ]
    df = df_from_rows(rows, cols)
    if df.empty:
        return df
    df["__sort_no"] = pd.to_numeric(df["product_no"], errors="coerce").fillna(0)
    df = df.sort_values("__sort_no", ascending=False).drop(columns=["__sort_no"])
    return df.reset_index(drop=True)


@st.cache_data(ttl=20, show_spinner=False)
def bulk_product_status(product_nos_tuple, pickup_date_s="", return_date_s="", exclude_quote_id=0):
    """현재 페이지 상품들의 확정/대여중 수량과 견적중 수량을 한 번에 계산한다."""
    product_nos = [str(x) for x in product_nos_tuple if str(x)]
    if not product_nos:
        return {}

    client = supabase_client()
    pickup = str(pickup_date_s or "")
    ret = str(return_date_s or "")
    exclude_quote_id = int(exclude_quote_id or 0)

    reserved_by = {p: 0 for p in product_nos}
    pending_by = {p: 0 for p in product_nos}

    for batch in chunked(product_nos, 250):
        q = client.table("rentals").select("product_no,quantity,quote_id,status,pickup_date,return_date").in_("product_no", batch).in_("status", list(ACTIVE_RENTAL_STATUSES))
        if pickup and ret:
            q = q.lte("pickup_date", ret).gte("return_date", pickup)
        if exclude_quote_id:
            q = q.neq("quote_id", exclude_quote_id)
        for r in (q.execute().data or []):
            pno = str(r.get("product_no", ""))
            reserved_by[pno] = reserved_by.get(pno, 0) + safe_int(r.get("quantity", 0), 0)

    item_rows = []
    for batch in chunked(product_nos, 250):
        item_rows.extend(client.table("quote_items").select("quote_id,product_no,quantity").in_("product_no", batch).execute().data or [])

    quote_ids = sorted({int(i.get("quote_id")) for i in item_rows if i.get("quote_id") is not None})
    valid_quote_ids = set()
    for batch in chunked(quote_ids, 250):
        q = client.table("quotes").select("quote_id,status,pickup_date,return_date").in_("quote_id", batch).eq("status", "견적중")
        if pickup and ret:
            q = q.lte("pickup_date", ret).gte("return_date", pickup)
        if exclude_quote_id:
            q = q.neq("quote_id", exclude_quote_id)
        valid_quote_ids.update(int(r["quote_id"]) for r in (q.execute().data or []) if r.get("quote_id") is not None)

    for item in item_rows:
        qid = int(item.get("quote_id") or 0)
        if qid not in valid_quote_ids:
            continue
        pno = str(item.get("product_no", ""))
        pending_by[pno] = pending_by.get(pno, 0) + safe_int(item.get("quantity", 0), 0)

    return {pno: {"reserved": int(reserved_by.get(pno, 0)), "pending": int(pending_by.get(pno, 0))} for pno in product_nos}


def get_product(product_no):
    res = supabase_client().table("products").select("*").eq("product_no", str(product_no)).limit(1).execute()
    data = res.data or []
    return dict(data[0]) if data else None


@st.cache_data(ttl=60, show_spinner=False)
def load_products_map(product_nos_tuple):
    product_nos = [str(x) for x in product_nos_tuple if str(x)]
    out = {}
    for batch in chunked(product_nos, 250):
        rows = supabase_client().table("products").select("product_no,name,size_text,qty,price,thumbnail_url,local_thumbnail_path").in_("product_no", batch).execute().data or []
        for row in rows:
            out[str(row.get("product_no"))] = row
    return out


def filter_products(df, search):
    if df.empty or not search:
        return df
    s = str(search).lower().strip()
    return df[
        df["name"].astype(str).str.lower().str.contains(s, na=False) |
        df["product_no"].astype(str).str.lower().str.contains(s, na=False) |
        df["size_text"].astype(str).str.lower().str.contains(s, na=False) |
        df["category"].astype(str).str.lower().str.contains(s, na=False)
    ]


def rentals_query(product_no=None, pickup_date=None, return_date=None, active_only=False, exclude_quote_id=None, exclude_rental_id=None):
    q = supabase_client().table("rentals").select("*")
    if product_no is not None:
        q = q.eq("product_no", str(product_no))
    if active_only:
        q = q.in_("status", list(ACTIVE_RENTAL_STATUSES))
    if pickup_date is not None and return_date is not None:
        q = q.lte("pickup_date", str(return_date)).gte("return_date", str(pickup_date))
    if exclude_quote_id is not None:
        q = q.neq("quote_id", int(exclude_quote_id))
    if exclude_rental_id is not None:
        q = q.neq("id", int(exclude_rental_id))
    return q.execute().data or []


def get_reserved_qty(product_no, pickup_date, return_date, exclude_quote_id=None):
    rows = rentals_query(product_no, pickup_date, return_date, active_only=True, exclude_quote_id=exclude_quote_id)
    return int(sum(safe_int(r.get("quantity", 0), 0) for r in rows))


def get_available_qty(product_no, pickup_date, return_date, exclude_quote_id=None):
    p = get_product(product_no)
    total_qty = safe_int(p.get("qty", 1) if p else 1, 1)
    reserved = get_reserved_qty(product_no, pickup_date, return_date, exclude_quote_id=exclude_quote_id)
    return max(total_qty - reserved, 0), total_qty, reserved


def get_pending_quote_qty(product_no, pickup_date=None, return_date=None, exclude_quote_id=None):
    items = supabase_client().table("quote_items").select("quote_id,product_no,quantity").eq("product_no", str(product_no)).execute().data or []
    if not items:
        return 0

    quote_ids = sorted({int(i["quote_id"]) for i in items if i.get("quote_id") is not None})
    quotes = []
    for batch in chunked(quote_ids, 300):
        q = supabase_client().table("quotes").select("quote_id,pickup_date,return_date,status").in_("quote_id", batch).eq("status", "견적중")
        if exclude_quote_id is not None:
            q = q.neq("quote_id", int(exclude_quote_id))
        quotes.extend(q.execute().data or [])

    quote_map = {int(q["quote_id"]): q for q in quotes}
    total = 0
    for item in items:
        qid = int(item.get("quote_id") or 0)
        q = quote_map.get(qid)
        if not q:
            continue
        if pickup_date is not None and return_date is not None:
            if not (str(q.get("pickup_date")) <= str(return_date) and str(q.get("return_date")) >= str(pickup_date)):
                continue
        total += safe_int(item.get("quantity", 0), 0)
    return int(total)


def get_active_rental_qty_total(product_no):
    rows = rentals_query(product_no=product_no, active_only=True)
    return int(sum(safe_int(r.get("quantity", 0), 0) for r in rows))


def load_conflicting_rentals_df(product_no, pickup_date, return_date, exclude_quote_id=None):
    rows = rentals_query(product_no, pickup_date, return_date, active_only=True, exclude_quote_id=exclude_quote_id)
    cols = ["id", "quote_id", "quote_no", "team_name", "product_no", "product_name", "pickup_date", "return_date", "quantity", "status", "created_at"]
    df = df_from_rows(rows, cols)
    if df.empty:
        return df
    return df.sort_values(["pickup_date", "return_date", "id"], ascending=[True, True, True]).reset_index(drop=True)


def load_pending_quotes_for_product_df(product_no, pickup_date=None, return_date=None, exclude_quote_id=None):
    item_rows = supabase_client().table("quote_items").select("quote_id,product_no,product_name,quantity").eq("product_no", str(product_no)).execute().data or []
    if not item_rows:
        return pd.DataFrame()

    quote_ids = sorted({int(i["quote_id"]) for i in item_rows if i.get("quote_id") is not None})
    quotes = []
    for batch in chunked(quote_ids, 300):
        q = supabase_client().table("quotes").select("quote_id,quote_no,team_name,pickup_date,return_date,status,total,created_at").in_("quote_id", batch).eq("status", "견적중")
        if exclude_quote_id is not None:
            q = q.neq("quote_id", int(exclude_quote_id))
        quotes.extend(q.execute().data or [])

    quote_map = {int(q["quote_id"]): q for q in quotes}
    out = []
    for item in item_rows:
        qid = int(item.get("quote_id") or 0)
        q = quote_map.get(qid)
        if not q:
            continue
        if pickup_date is not None and return_date is not None:
            if not (str(q.get("pickup_date")) <= str(return_date) and str(q.get("return_date")) >= str(pickup_date)):
                continue
        out.append({**q, "product_no": item.get("product_no"), "product_name": item.get("product_name"), "quantity": item.get("quantity")})

    df = pd.DataFrame(out)
    if df.empty:
        return df
    return df.sort_values(["pickup_date", "created_at"], ascending=[True, False]).reset_index(drop=True)


def load_product_history_df(product_no, limit=20):
    rows = supabase_client().table("rentals").select("quote_no,team_name,pickup_date,return_date,quantity,status,created_at,returned_at").eq("product_no", str(product_no)).order("pickup_date", desc=True).limit(int(limit)).execute().data or []
    cols = ["quote_no", "team_name", "pickup_date", "return_date", "quantity", "status", "created_at", "returned_at"]
    return df_from_rows(rows, cols)


def parse_date_text(value):
    """호환용: 다양한 텍스트 날짜를 date 객체로 변환한다."""
    return datetime.strptime(normalize_date_text(value), "%Y-%m-%d").date()



# -----------------------------
# 필터 / 상태
# -----------------------------

def status_filter_control(label, options, key):
    """상태 필터를 버튼 나열 대신 탭/세그먼트 형태로 표시한다."""
    if key not in st.session_state:
        st.session_state[key] = ""

    st.caption(label)
    labels = ["전체"] + list(options)
    current_value = st.session_state.get(key, "")
    current_label = current_value if current_value else "전체"

    # Streamlit 최신 버전에서는 segmented_control이 가장 명확한 탭 UI를 제공한다.
    if hasattr(st, "segmented_control"):
        selected_label = st.segmented_control(
            "",
            labels,
            default=current_label if current_label in labels else "전체",
            key=f"{key}_segmented",
            label_visibility="collapsed",
        )
        new_value = "" if selected_label == "전체" else selected_label
        if new_value != current_value:
            st.session_state[key] = new_value
            st.rerun()
        return st.session_state.get(key, "")

    # 구버전 Streamlit fallback: 버튼으로 동작하되 active 상태가 텍스트로 드러나게 한다.
    cols = st.columns(len(labels))
    for i, label_text in enumerate(labels):
        value = "" if label_text == "전체" else label_text
        active = value == current_value
        button_text = f"■ {label_text}" if active else label_text
        if cols[i].button(button_text, key=f"{key}_tab_{label_text}", use_container_width=True):
            st.session_state[key] = value
            st.rerun()
    return st.session_state.get(key, "")


def reopen_quote_for_edit(quote_id):
    """견적 내용을 수정할 때 기존 대여 상태를 해제하고 견적중으로 되돌린다.

    확정/부분반납/반납완료 상태라도 수량, 단가, 상품 구성, 기본 정보, 상품별 날짜를
    수정하면 다시 견적중 상태로 돌아간다. 기존 rentals 행은 삭제 상태로 보존해
    재고를 막지 않게 처리한다.
    """
    quote = get_quote(quote_id)
    if not quote:
        return False, "견적서를 찾지 못했습니다."
    status = str(quote.get("status", ""))
    if status == "견적중":
        return True, ""
    if status in ["확정", "부분반납", "반납완료"]:
        client = supabase_client()
        client.table("rentals").update({
            "status": "삭제",
            "updated_at": datetime.now().isoformat(),
        }).eq("quote_id", int(quote_id)).neq("status", "삭제").execute()
        client.table("quotes").update({
            "status": "견적중",
            "updated_at": datetime.now().isoformat(),
        }).eq("quote_id", int(quote_id)).execute()
        clear_quote_cache()
        return True, "수정 내용 반영을 위해 견적중 상태로 전환했습니다."
    return False, "현재 상태에서는 견적서를 수정할 수 없습니다."


def update_quote_header(quote_id, team_name, pickup_date, return_date):
    quote = get_quote(quote_id)
    if not quote:
        return False, ["견적서를 찾지 못했습니다."]

    try:
        pickup = normalize_date_text(pickup_date)
        ret = normalize_date_text(return_date)
    except Exception as e:
        return False, [str(e)]
    if ret < pickup:
        return False, ["반납 날짜가 픽업 날짜보다 빠릅니다."]

    ok, message = reopen_quote_for_edit(quote_id)
    if not ok:
        return False, [message]

    client = supabase_client()
    client.table("quotes").update({
        "team_name": str(team_name),
        "pickup_date": pickup,
        "return_date": ret,
        "updated_at": datetime.now().isoformat(),
    }).eq("quote_id", int(quote_id)).execute()
    set_all_quote_item_dates(quote_id, pickup, ret, reprice=True)
    update_quote_totals(quote_id, reprice=True)
    clear_quote_cache()
    return True, ([message] if message else [])



# -----------------------------
# 견적서 / 대여 기록
# -----------------------------

def generate_quote_no():
    prefix = f"Q-{today_yyyymmdd()}"
    rows = table_all("quotes", select="quote_no")
    nums = []
    for r in rows:
        qno = str(r.get("quote_no", ""))
        if qno.startswith(prefix + "-"):
            m = re.search(r"-(\d+)$", qno)
            if m:
                nums.append(int(m.group(1)))
    return f"{prefix}-{(max(nums) + 1 if nums else 1):03d}"


def update_quote_totals(quote_id, reprice=True, clear_cache=True):
    quote = get_quote(quote_id)
    if not quote:
        return
    client = supabase_client()
    rows = client.table("quote_items").select("id,quantity,unit_price,line_total,availability_note").eq("quote_id", int(quote_id)).execute().data or []
    subtotal = 0
    for row in rows:
        quantity = max(safe_int(row.get("quantity", 1), 1), 1)
        unit_price = max(safe_int(row.get("unit_price", 0), 0), 0)
        try:
            item_pickup, item_return = get_item_dates(row, quote)
        except Exception:
            item_pickup, item_return = quote["pickup_date"], quote["return_date"]
        line_total = calculate_line_total(quantity, unit_price, item_pickup, item_return)
        subtotal += line_total
        if reprice and line_total != safe_int(row.get("line_total", 0), 0):
            client.table("quote_items").update({
                "line_total": line_total,
                "updated_at": datetime.now().isoformat(),
            }).eq("id", int(row["id"])).execute()

    vat = int(round(subtotal * 0.1))
    total = subtotal + vat
    client.table("quotes").update({
        "subtotal": subtotal,
        "vat": vat,
        "total": total,
        "updated_at": datetime.now().isoformat(),
    }).eq("quote_id", int(quote_id)).execute()
    if clear_cache:
        clear_quote_cache()


def recalculate_open_quotes_for_holidays():
    rows = supabase_client().table("quotes").select("quote_id").in_("status", ["견적중", "확정", "부분반납"]).execute().data or []
    for row in rows:
        update_quote_totals(int(row["quote_id"]), reprice=True, clear_cache=False)
    clear_data_cache()
    return len(rows)


def create_quote(team_name, pickup_date, return_date, selected_items, memo=""):
    pickup = normalize_date_text(pickup_date)
    ret = normalize_date_text(return_date)
    quote_no = generate_quote_no()
    client = supabase_client()
    res = client.table("quotes").insert({
        "quote_no": quote_no,
        "team_name": team_name,
        "pickup_date": pickup,
        "return_date": ret,
        "status": "견적중",
        "subtotal": 0,
        "vat": 0,
        "total": 0,
        "memo": memo,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }).execute()
    quote_id = int(res.data[0]["quote_id"])

    rows = []
    multiplier = quote_price_multiplier(pickup, ret)
    for product_no, item in selected_items.items():
        p = get_product(product_no)
        if not p:
            continue
        qty = safe_int(item.get("quantity", 1), 1)
        unit_price = safe_int(item.get("unit_price", p.get("price", 0)), 0)
        line_total = calculate_line_total(qty, unit_price, pickup, ret, multiplier=multiplier)
        rows.append({
            "quote_id": quote_id,
            "product_no": str(product_no),
            "product_name": p.get("name", ""),
            "size_text": p.get("size_text", ""),
            "thumbnail_url": p.get("thumbnail_url", ""),
            "local_thumbnail_path": p.get("local_thumbnail_path", ""),
            "quantity": qty,
            "unit_price": unit_price,
            "line_total": line_total,
            "availability_note": make_item_note(pickup, ret),
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        })

    if rows:
        client.table("quote_items").insert(rows).execute()

    update_quote_totals(quote_id, reprice=False)
    clear_quote_cache()
    return quote_id


@st.cache_data(ttl=20, show_spinner=False)
def load_quotes_df(include_deleted=False):
    rows = table_all("quotes")
    cols = ["quote_id", "quote_no", "team_name", "pickup_date", "return_date", "status", "subtotal", "vat", "total", "memo", "created_at", "updated_at"]
    df = df_from_rows(rows, cols)
    if df.empty:
        return df

    if not include_deleted:
        df = df[df["status"] != "삭제"].copy()

    df = df.sort_values(["created_at", "quote_id"], ascending=[False, False]).reset_index(drop=True)

    quote_ids = [int(x) for x in df["quote_id"].dropna().tolist()]
    item_rows = []
    for batch in chunked(quote_ids, 250):
        item_rows.extend(supabase_client().table("quote_items").select("quote_id,product_no,product_name,local_thumbnail_path,thumbnail_url,id").in_("quote_id", batch).order("id").execute().data or [])

    # 오래된 quote_items에 thumbnail_url이 비어 있으면 products에서 한 번에 보강한다.
    product_nos_for_thumb = sorted({str(r.get("product_no")) for r in item_rows if r.get("product_no") and not str(r.get("thumbnail_url") or "").strip()})
    product_thumb_map = {}
    for batch in chunked(product_nos_for_thumb, 250):
        rows = supabase_client().table("products").select("product_no,thumbnail_url,local_thumbnail_path").in_("product_no", batch).execute().data or []
        for p in rows:
            product_thumb_map[str(p.get("product_no"))] = p

    for r in item_rows:
        if not str(r.get("thumbnail_url") or "").strip():
            p = product_thumb_map.get(str(r.get("product_no")))
            if p:
                r["thumbnail_url"] = p.get("thumbnail_url", "") or ""
                if not str(r.get("local_thumbnail_path") or "").strip():
                    r["local_thumbnail_path"] = p.get("local_thumbnail_path", "") or ""

    grouped = {}
    for r in item_rows:
        qid = int(r.get("quote_id") or 0)
        grouped.setdefault(qid, []).append(r)

    summaries, counts, first_paths, first_urls = [], [], [], []
    for quote_id in df["quote_id"]:
        items = grouped.get(int(quote_id), [])
        counts.append(len(items))
        if not items:
            summaries.append("상품 없음")
            first_paths.append("")
            first_urls.append("")
        else:
            first_name = str(items[0].get("product_name") or "")
            summaries.append(first_name if len(items) == 1 else f"{first_name} 외 {len(items)-1}개")
            first_paths.append(str(items[0].get("local_thumbnail_path") or ""))
            first_urls.append(str(items[0].get("thumbnail_url") or ""))
    df["상품요약"] = summaries
    df["상품수"] = counts
    df["first_thumb_path"] = first_paths
    df["first_thumb_url"] = first_urls
    return df


def get_quote(quote_id):
    res = supabase_client().table("quotes").select("*").eq("quote_id", int(quote_id)).limit(1).execute()
    data = res.data or []
    return dict(data[0]) if data else None


def fill_quote_item_image_fallbacks(df):
    """quote_items에 썸네일 URL이 비어 있으면 products 테이블에서 보강한다.

    로컬 SQLite에서 넘어온 오래된 견적 상품은 local_thumbnail_path만 있고
    Streamlit Cloud에는 해당 파일이 없을 수 있다. 배포 환경에서는 URL을
    우선 사용해야 하므로 products.thumbnail_url을 fallback으로 채운다.
    """
    if df.empty or "product_no" not in df.columns:
        return df

    if "thumbnail_url" not in df.columns:
        df["thumbnail_url"] = ""
    if "local_thumbnail_path" not in df.columns:
        df["local_thumbnail_path"] = ""
    if "size_text" not in df.columns:
        df["size_text"] = ""
    if "product_name" not in df.columns:
        df["product_name"] = ""

    need = df[
        df["product_no"].notna()
        & (
            df["thumbnail_url"].fillna("").astype(str).str.strip().eq("")
            | df["size_text"].fillna("").astype(str).str.strip().eq("")
            | df["product_name"].fillna("").astype(str).str.strip().eq("")
        )
    ]
    product_nos = [str(x) for x in need["product_no"].dropna().unique().tolist() if str(x)]
    if not product_nos:
        return df

    product_map = {}
    for batch in chunked(product_nos, 250):
        rows = supabase_client().table("products").select(
            "product_no,name,size_text,thumbnail_url,local_thumbnail_path"
        ).in_("product_no", batch).execute().data or []
        for r in rows:
            product_map[str(r.get("product_no"))] = r

    for idx, row in df.iterrows():
        p = product_map.get(str(row.get("product_no")))
        if not p:
            continue
        if not str(row.get("thumbnail_url") or "").strip():
            df.at[idx, "thumbnail_url"] = p.get("thumbnail_url", "") or ""
        if not str(row.get("local_thumbnail_path") or "").strip():
            df.at[idx, "local_thumbnail_path"] = p.get("local_thumbnail_path", "") or ""
        if not str(row.get("size_text") or "").strip():
            df.at[idx, "size_text"] = p.get("size_text", "") or ""
        if not str(row.get("product_name") or "").strip():
            df.at[idx, "product_name"] = p.get("name", "") or ""
    return df


def load_quote_items_df(quote_id):
    rows = supabase_client().table("quote_items").select("*").eq("quote_id", int(quote_id)).order("id").execute().data or []
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["id", "quote_id", "product_no", "product_name", "size_text", "thumbnail_path", "thumbnail_url", "quantity", "unit_price", "line_total", "availability_note"])

    df = fill_quote_item_image_fallbacks(df)

    if "local_thumbnail_path" in df.columns:
        df["thumbnail_path"] = df["local_thumbnail_path"].fillna("")
    elif "thumbnail_path" not in df.columns:
        df["thumbnail_path"] = ""

    if "thumbnail_url" not in df.columns:
        df["thumbnail_url"] = ""

    cols = ["id", "quote_id", "product_no", "product_name", "size_text", "thumbnail_path", "thumbnail_url", "quantity", "unit_price", "line_total", "availability_note"]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df[cols].reset_index(drop=True)


def add_item_to_quote(quote_id, product_no, quantity=1):
    p = get_product(product_no)
    if not p:
        return False, "상품을 찾지 못했습니다."

    ok, message = reopen_quote_for_edit(quote_id)
    if not ok:
        return False, message

    quote = get_quote(quote_id)
    multiplier = quote_price_multiplier(quote["pickup_date"], quote["return_date"])
    client = supabase_client()
    existing = client.table("quote_items").select("id,quantity").eq("quote_id", int(quote_id)).eq("product_no", str(product_no)).limit(1).execute().data or []
    unit_price = safe_int(p.get("price", 0), 0)
    stock_qty = max(safe_int(p.get("qty", 1), 1), 1)

    if existing:
        item_id = int(existing[0]["id"])
        new_qty = safe_int(existing[0].get("quantity", 1), 1) + safe_int(quantity, 1)
        if new_qty > stock_qty:
            return False, f"보유수량이 {stock_qty}개입니다."
        client.table("quote_items").update({
            "quantity": new_qty,
            "unit_price": unit_price,
            "line_total": calculate_line_total(new_qty, unit_price, quote["pickup_date"], quote["return_date"], multiplier=multiplier),
            "availability_note": make_item_note(quote["pickup_date"], quote["return_date"]),
            "updated_at": datetime.now().isoformat(),
        }).eq("id", item_id).execute()
    else:
        qty = min(max(safe_int(quantity, 1), 1), stock_qty)
        client.table("quote_items").insert({
            "quote_id": int(quote_id),
            "product_no": str(product_no),
            "product_name": p.get("name", ""),
            "size_text": p.get("size_text", ""),
            "thumbnail_url": p.get("thumbnail_url", ""),
            "local_thumbnail_path": p.get("local_thumbnail_path", ""),
            "quantity": qty,
            "unit_price": unit_price,
            "line_total": calculate_line_total(qty, unit_price, quote["pickup_date"], quote["return_date"], multiplier=multiplier),
            "availability_note": make_item_note(quote["pickup_date"], quote["return_date"]),
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }).execute()

    update_quote_totals(quote_id, reprice=False)
    clear_quote_cache()
    return True, message or "상품을 추가했습니다."


def delete_quote_item(item_id, quote_id):
    ok, message = reopen_quote_for_edit(quote_id)
    if not ok:
        return False, message
    supabase_client().table("quote_items").delete().eq("id", int(item_id)).eq("quote_id", int(quote_id)).execute()
    update_quote_totals(quote_id, reprice=False)
    clear_quote_cache()
    return True, message or "상품을 삭제했습니다."


def update_quote_item(item_id, quantity, unit_price):
    client = supabase_client()
    item_rows = client.table("quote_items").select("quote_id,product_no,availability_note").eq("id", int(item_id)).limit(1).execute().data or []
    if not item_rows:
        return False, "견적 상품을 찾지 못했습니다."
    quote_id = int(item_rows[0]["quote_id"])
    product_no = str(item_rows[0].get("product_no", ""))

    ok, message = reopen_quote_for_edit(quote_id)
    if not ok:
        return False, message

    product = get_product(product_no) or {}
    max_qty = max(safe_int(product.get("qty", 1), 1), 1)
    qty = max(safe_int(quantity, 1), 1)
    if qty > max_qty:
        return False, f"보유수량이 {max_qty}개입니다."
    price = max(safe_int(unit_price, 0), 0)
    quote = get_quote(quote_id)
    item_pickup, item_return = get_item_dates(item_rows[0], quote)
    client.table("quote_items").update({
        "quantity": qty,
        "unit_price": price,
        "line_total": calculate_line_total(qty, price, item_pickup, item_return),
        "updated_at": datetime.now().isoformat(),
    }).eq("id", int(item_id)).execute()
    update_quote_totals(quote_id, reprice=False)
    clear_quote_cache()
    return True, message or "상품 정보를 변경했습니다."


def check_quote_availability(quote_id, pickup_date=None, return_date=None, exclude_self=True):
    quote = get_quote(quote_id)
    if not quote:
        return ["견적서를 찾지 못했습니다."]

    pickup = normalize_date_text(pickup_date or quote["pickup_date"])
    ret = normalize_date_text(return_date or quote["return_date"])
    items = load_quote_items_df(quote_id)
    if items.empty:
        return []

    product_nos = tuple(items["product_no"].astype(str).tolist())
    product_map = load_products_map(product_nos)
    failures = []
    for _, item in items.iterrows():
        product_no = str(item["product_no"])
        item_pickup, item_return = (pickup, ret) if (pickup_date or return_date) else get_item_dates(item, quote)
        status_one = bulk_product_status((product_no,), item_pickup, item_return, int(quote_id) if exclude_self else 0).get(product_no, {})
        total_qty = max(safe_int((product_map.get(product_no) or {}).get("qty", 1), 1), 1)
        reserved = safe_int(status_one.get("reserved", 0), 0)
        available_qty = max(total_qty - reserved, 0)
        needed = safe_int(item["quantity"], 1)
        if needed > available_qty:
            failures.append(f'{item["product_name"]} / {item_pickup}~{item_return} / 필요 {needed}개 / 가능 {available_qty}개')
    return failures


def confirm_quote(quote_id):
    quote = get_quote(quote_id)
    if not quote:
        return False, ["견적서를 찾지 못했습니다."]
    if quote["status"] == "확정":
        return False, ["이미 확정된 견적서입니다."]
    if quote["status"] == "삭제":
        return False, ["삭제 상태의 견적서는 확정할 수 없습니다."]

    failures = check_quote_availability(quote_id, exclude_self=True)
    if failures:
        return False, failures

    client = supabase_client()
    items = load_quote_items_df(quote_id)

    client.table("rentals").update({"status": "삭제", "updated_at": datetime.now().isoformat()}).eq("quote_id", int(quote_id)).execute()

    rental_rows = []
    for _, item in items.iterrows():
        rental_rows.append({
            "quote_id": int(quote_id),
            "quote_no": quote["quote_no"],
            "team_name": quote["team_name"],
            "product_no": str(item["product_no"]),
            "product_name": item["product_name"],
            "pickup_date": get_item_dates(item, quote)[0],
            "return_date": get_item_dates(item, quote)[1],
            "quantity": safe_int(item["quantity"], 1),
            "status": "대여중",
            "memo": "",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        })
    if rental_rows:
        client.table("rentals").insert(rental_rows).execute()

    client.table("quotes").update({"status": "확정", "updated_at": datetime.now().isoformat()}).eq("quote_id", int(quote_id)).execute()
    clear_quote_cache()
    return True, []


def delete_quote(quote_id):
    client = supabase_client()
    client.table("quotes").update({"status": "삭제", "updated_at": datetime.now().isoformat()}).eq("quote_id", int(quote_id)).execute()
    client.table("rentals").update({"status": "삭제", "updated_at": datetime.now().isoformat()}).eq("quote_id", int(quote_id)).execute()
    clear_quote_cache()


def load_rentals_for_quote_df(quote_id):
    rows = supabase_client().table("rentals").select("*").eq("quote_id", int(quote_id)).order("id").execute().data or []
    cols = ["id", "quote_id", "quote_no", "team_name", "product_no", "product_name", "pickup_date", "return_date", "quantity", "status", "memo", "created_at", "returned_at"]
    return df_from_rows(rows, cols)


def refresh_quote_return_status(quote_id):
    rentals = load_rentals_for_quote_df(quote_id)
    if rentals.empty:
        return

    active_count = len(rentals[rentals["status"].isin(list(ACTIVE_RENTAL_STATUSES))])
    returned_count = len(rentals[rentals["status"] == "반납완료"])

    if active_count == 0 and returned_count > 0:
        new_status = "반납완료"
    elif active_count > 0 and returned_count > 0:
        new_status = "부분반납"
    else:
        new_status = "확정"

    supabase_client().table("quotes").update({"status": new_status, "updated_at": datetime.now().isoformat()}).eq("quote_id", int(quote_id)).execute()


def return_rental_items(quote_id, rental_ids):
    if not rental_ids:
        return False, "반납할 상품을 선택해야 합니다."
    ids = [int(x) for x in rental_ids]
    supabase_client().table("rentals").update({
        "status": "반납완료",
        "returned_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }).eq("quote_id", int(quote_id)).in_("id", ids).execute()
    refresh_quote_return_status(quote_id)
    clear_quote_cache()
    return True, "선택 상품을 반납 처리했습니다."


def return_quote(quote_id):
    client = supabase_client()
    for status in ACTIVE_RENTAL_STATUSES:
        client.table("rentals").update({
            "status": "반납완료",
            "returned_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }).eq("quote_id", int(quote_id)).eq("status", status).execute()
    refresh_quote_return_status(quote_id)
    clear_quote_cache()


def set_rental_item_returned(rental_id, returned=True):
    rental = get_rental_item(rental_id)
    if not rental:
        return False, "대여 상품을 찾지 못했습니다."

    data = {"updated_at": datetime.now().isoformat()}
    if returned:
        data.update({"status": "반납완료", "returned_at": datetime.now().isoformat()})
    else:
        data.update({"status": "대여중", "returned_at": None})
    supabase_client().table("rentals").update(data).eq("id", int(rental_id)).execute()
    refresh_quote_return_status(int(rental["quote_id"]))
    clear_quote_cache()
    return True, "반납 상태를 변경했습니다."


def update_quote_date_range_from_rentals(quote_id):
    rentals = load_rentals_for_quote_df(quote_id)
    if rentals.empty:
        return
    active = rentals[rentals["status"] != "삭제"].copy()
    if active.empty:
        return
    pickup = str(active["pickup_date"].min())
    ret = str(active["return_date"].max())
    supabase_client().table("quotes").update({
        "pickup_date": pickup,
        "return_date": ret,
        "updated_at": datetime.now().isoformat(),
    }).eq("quote_id", int(quote_id)).execute()
    update_quote_totals(quote_id, reprice=True)


def get_rental_item(rental_id):
    res = supabase_client().table("rentals").select("*").eq("id", int(rental_id)).limit(1).execute()
    data = res.data or []
    return dict(data[0]) if data else None


def update_rental_item_dates(rental_id, new_pickup, new_return):
    """기존 대여 상품 날짜 수정 진입점.

    이제 대여 상태에서 날짜를 수정하면 해당 견적을 견적중으로 전환하고,
    연결된 견적 상품 날짜/금액을 수정한다.
    """
    rental = get_rental_item(rental_id)
    if not rental:
        return False, ["대여 상품을 찾지 못했습니다."]
    try:
        pickup = normalize_date_text(new_pickup)
        ret = normalize_date_text(new_return)
    except Exception as e:
        return False, [str(e)]
    if ret < pickup:
        return False, ["반납 날짜가 픽업 날짜보다 빠릅니다."]

    quote_id = int(rental["quote_id"])
    product_no = str(rental["product_no"])
    qi_rows = supabase_client().table("quote_items").select("id").eq("quote_id", quote_id).eq("product_no", product_no).limit(1).execute().data or []
    if not qi_rows:
        return False, ["연결된 견적 상품을 찾지 못했습니다."]
    return update_quote_item_dates(int(qi_rows[0]["id"]), quote_id, pickup, ret)


def update_quote_dates(quote_id, new_pickup, new_return):
    quote = get_quote(quote_id)
    if not quote:
        return False, ["견적서를 찾지 못했습니다."]
    try:
        pickup = normalize_date_text(new_pickup)
        ret = normalize_date_text(new_return)
    except Exception as e:
        return False, [str(e)]
    if ret < pickup:
        return False, ["반납 날짜가 픽업 날짜보다 빠릅니다."]

    failures = check_quote_availability(quote_id, pickup_date=pickup, return_date=ret, exclude_self=True)
    if failures:
        return False, failures

    client = supabase_client()
    client.table("quotes").update({
        "pickup_date": pickup,
        "return_date": ret,
        "updated_at": datetime.now().isoformat(),
    }).eq("quote_id", int(quote_id)).execute()
    set_all_quote_item_dates(quote_id, pickup, ret, reprice=True)

    for status in ACTIVE_RENTAL_STATUSES:
        client.table("rentals").update({
            "pickup_date": pickup,
            "return_date": ret,
            "updated_at": datetime.now().isoformat(),
        }).eq("quote_id", int(quote_id)).eq("status", status).execute()

    update_quote_totals(quote_id, reprice=True)
    clear_data_cache()
    return True, []




# -----------------------------
# 견적서 이미지 / PDF
# -----------------------------
# -----------------------------
# 견적서 이미지 / PDF
# -----------------------------

def load_font(size=28, bold=False):
    """한글 견적서용 폰트 로더.

    Streamlit Community Cloud에서는 packages.txt의 fonts-noto-cjk 설치 후
    보통 /usr/share/fonts/opentype/noto 경로에 CJK 폰트가 설치된다.
    기존 trutype/noto 경로만 보면 한글이 빈칸으로 나올 수 있으므로
    여러 경로를 순서대로 확인한다.
    """
    if bold:
        candidates = [
            "/System/Library/Fonts/AppleSDGothicNeo.ttc",
            "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    else:
        candidates = [
            "/System/Library/Fonts/AppleSDGothicNeo.ttc",
            "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "/Library/Fonts/Arial Unicode.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def text_size(draw, text, font):
    box = draw.textbbox((0, 0), str(text), font=font)
    return box[2] - box[0], box[3] - box[1]


def draw_fit_text(draw, xy, text, font, fill=(30, 30, 30), max_width=None):
    text = str(text)
    if not max_width:
        draw.text(xy, text, font=font, fill=fill)
        return
    while text and text_size(draw, text + "…", font)[0] > max_width:
        text = text[:-1]
    if len(text) < len(str(text)):
        text += "…"
    draw.text(xy, text, font=font, fill=fill)


def open_thumb(path, fallback_url=""):
    # 배포 환경에서는 로컬 thumbnails 폴더가 영구 저장되지 않으므로 URL을 우선 사용한다.
    try:
        if fallback_url:
            r = requests.get(str(fallback_url), headers=HEADERS, timeout=12)
            r.raise_for_status()
            return Image.open(BytesIO(r.content)).convert("RGB")
    except Exception:
        pass
    try:
        if path and os.path.exists(str(path)):
            return Image.open(str(path)).convert("RGB")
    except Exception:
        pass
    return None


def make_quote_image_bytes(quote_id):
    quote = get_quote(quote_id)
    items = load_quote_items_df(quote_id)
    if not quote:
        raise ValueError("견적서를 찾을 수 없습니다.")

    width = 1600
    margin = 86
    cols = 5
    gap_x = 28
    gap_y = 42
    card_w = int((width - margin * 2 - gap_x * (cols - 1)) / cols)
    card_h = 510
    rows = max(1, math.ceil(len(items) / cols))
    header_h = 340
    footer_h = 390
    height = header_h + rows * card_h + max(0, rows - 1) * gap_y + footer_h + margin

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    font_logo = load_font(72, bold=True)
    font_title = load_font(46, bold=True)
    font_h = load_font(30, bold=True)
    font_m = load_font(24)
    font_mb = load_font(24, bold=True)
    font_s = load_font(20)
    font_xs = load_font(17)

    logo_text = "프라비"
    draw.text((margin, 46), logo_text, font=font_logo, fill=(0, 0, 0))

    title_text = "견적서"
    title_w, _ = text_size(draw, title_text, font_title)
    draw.text((width - margin - title_w, 78), title_text, font=font_title, fill=(20, 20, 20))
    line_y = 165
    draw.line((margin, line_y, width - margin, line_y), fill=(35, 35, 35), width=2)

    info_y = 205
    draw.text((margin, info_y), f"팀 이름: {quote['team_name']}", font=font_h, fill=(30, 30, 30))
    draw.text((margin, info_y + 50), f"대여 날짜: {quote['pickup_date']} ~ {quote['return_date']}", font=font_m, fill=(60, 60, 60))
    pricing_text = pricing_summary_text(quote['pickup_date'], quote['return_date'])
    draw.text((margin, info_y + 91), pricing_text, font=font_xs, fill=(105, 105, 105))
    qno_text = f"견적번호: {quote['quote_no']}"
    created_text = f"작성일: {format_datetime_short(quote.get('created_at'))}"
    qno_w, _ = text_size(draw, qno_text, font_m)
    created_w, _ = text_size(draw, created_text, font_s)
    draw.text((width - margin - qno_w, info_y), qno_text, font=font_m, fill=(60, 60, 60))
    draw.text((width - margin - created_w, info_y + 44), created_text, font=font_s, fill=(100, 100, 100))

    start_y = header_h
    export_multiplier = quote_price_multiplier(quote["pickup_date"], quote["return_date"])
    export_product_nos = tuple(items["product_no"].astype(str).tolist()) if not items.empty else tuple()
    export_status_map = bulk_product_status(export_product_nos, str(quote["pickup_date"]), str(quote["return_date"]), int(quote_id))
    export_product_map = load_products_map(export_product_nos)
    for order, (_, item) in enumerate(items.iterrows()):
        col = order % cols
        row = order // cols
        x = margin + col * (card_w + gap_x)
        y = start_y + row * (card_h + gap_y)

        draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=18, outline=(220, 220, 220), width=2, fill=(252, 252, 252))

        product_no = str(item["product_no"])
        total_stock = max(safe_int((export_product_map.get(product_no) or {}).get("qty", 1), 1), 1)
        reserved_qty = safe_int((export_status_map.get(product_no) or {}).get("reserved", 0), 0)
        unavailable_label = (total_stock - reserved_qty <= 0) and quote.get("status") == "견적중"
        if unavailable_label:
            draw.rounded_rectangle((x + 18, y + 16, x + 116, y + 52), radius=14, fill=(253, 236, 236), outline=(217, 45, 32), width=1)
            draw.text((x + 35, y + 24), "불가능", font=font_xs, fill=(217, 45, 32))

        thumb_top = y + (56 if unavailable_label else 18)
        thumb_bottom = thumb_top + 248
        thumb_box = (x + 20, thumb_top, x + card_w - 20, thumb_bottom)
        draw.rounded_rectangle(thumb_box, radius=16, fill=(244, 244, 244))
        thumb = open_thumb(item.get("thumbnail_path", ""), item.get("thumbnail_url", ""))
        if thumb:
            thumb.thumbnail((thumb_box[2] - thumb_box[0] - 18, thumb_box[3] - thumb_box[1] - 18))
            tx = thumb_box[0] + ((thumb_box[2] - thumb_box[0]) - thumb.width) // 2
            ty = thumb_box[1] + ((thumb_box[3] - thumb_box[1]) - thumb.height) // 2
            img.paste(thumb, (tx, ty))
        else:
            msg = "이미지 없음"
            msg_w, msg_h = text_size(draw, msg, font_s)
            draw.text((thumb_box[0] + ((thumb_box[2]-thumb_box[0])-msg_w)//2, thumb_box[1] + 80), msg, font=font_s, fill=(150, 150, 150))

        text_x = x + 22
        text_y = thumb_box[3] + 22
        draw_fit_text(draw, (text_x, text_y), item["product_name"], font_mb, max_width=card_w - 44)
        draw_fit_text(draw, (text_x, text_y + 42), item["size_text"], font_s, fill=(90, 90, 90), max_width=card_w - 44)
        qty = safe_int(item["quantity"], 1)
        unit = safe_int(item["unit_price"], 0)
        line = safe_int(item["line_total"], 0)
        draw.text((text_x, text_y + 88), f"{qty}EA x {money(unit)} x {export_multiplier}", font=font_s, fill=(80, 80, 80))
        draw.text((text_x, text_y + 128), money(line), font=font_mb, fill=(20, 20, 20))

    footer_y = header_h + rows * card_h + max(0, rows - 1) * gap_y + 92
    box_w = 455
    box_h = 235
    box_x = width - margin - box_w
    draw.rounded_rectangle((box_x, footer_y, width - margin, footer_y + box_h), radius=20, outline=(220, 220, 220), width=2, fill=(250, 250, 250))

    label_x = box_x + 42
    value_x = width - margin - 245
    draw.text((label_x, footer_y + 42), "공급가", font=font_m, fill=(70, 70, 70))
    draw.text((value_x, footer_y + 42), money(quote.get("subtotal", 0)), font=font_m, fill=(30, 30, 30))
    draw.text((label_x, footer_y + 96), "부가세", font=font_m, fill=(70, 70, 70))
    draw.text((value_x, footer_y + 96), money(quote.get("vat", 0)), font=font_m, fill=(30, 30, 30))
    draw.line((box_x + 36, footer_y + 150, width - margin - 36, footer_y + 150), fill=(220, 220, 220), width=2)
    draw.text((label_x, footer_y + 178), "총금액", font=font_h, fill=(20, 20, 20))
    draw.text((value_x, footer_y + 178), money(quote.get("total", 0)), font=font_h, fill=(20, 20, 20))

    out = BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    return out.getvalue()


def make_quote_pdf_bytes(quote_id):
    png_bytes = make_quote_image_bytes(quote_id)
    img = Image.open(BytesIO(png_bytes)).convert("RGB")
    out = BytesIO()
    img.save(out, format="PDF", resolution=150.0)
    out.seek(0)
    return out.getvalue()


def make_pdf_from_png_bytes(png_bytes):
    img = Image.open(BytesIO(png_bytes)).convert("RGB")
    out = BytesIO()
    img.save(out, format="PDF", resolution=150.0)
    out.seek(0)
    return out.getvalue()


def quote_export_signature(quote_id):
    """견적서 PNG/PDF 캐시 무효화를 위한 가벼운 서명값."""
    quote = get_quote(quote_id) or {}
    try:
        items = load_quote_items_df(quote_id)
        item_rows = items[[
            "id", "product_no", "product_name", "size_text", "thumbnail_url",
            "quantity", "unit_price", "line_total"
        ]].fillna("").to_dict(orient="records") if not items.empty else []
    except Exception:
        item_rows = []
    payload = {
        "quote_no": quote.get("quote_no"),
        "team_name": quote.get("team_name"),
        "pickup_date": str(quote.get("pickup_date", "")),
        "return_date": str(quote.get("return_date", "")),
        "subtotal": quote.get("subtotal"),
        "vat": quote.get("vat"),
        "total": quote.get("total"),
        "updated_at": str(quote.get("updated_at", "")),
        "items": item_rows,
    }
    return hashlib.md5(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@st.cache_data(ttl=900, show_spinner=False)
def cached_quote_png_bytes(quote_id, signature):
    return make_quote_image_bytes(quote_id)


@st.cache_data(ttl=900, show_spinner=False)
def cached_quote_pdf_bytes(quote_id, signature):
    png_bytes = cached_quote_png_bytes(quote_id, signature)
    return make_pdf_from_png_bytes(png_bytes)


def render_quote_export_buttons(quote_id, key_prefix="quote_export"):
    """PNG/PDF는 화면 진입 시 바로 만들지 않고 사용자가 눌렀을 때만 생성한다."""
    signature = quote_export_signature(quote_id)
    png_key = f"{key_prefix}_{quote_id}_{signature}_png"
    pdf_key = f"{key_prefix}_{quote_id}_{signature}_pdf"

    st.caption("PNG/PDF는 파일 생성 버튼을 누른 뒤 다운로드됩니다. 큰 견적서는 처음 생성할 때만 시간이 걸릴 수 있습니다.")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("PNG 파일 생성", use_container_width=True, key=f"{key_prefix}_make_png_{quote_id}_{signature}"):
            with st.spinner("PNG 생성 중..."):
                st.session_state[png_key] = cached_quote_png_bytes(quote_id, signature)
        if png_key in st.session_state:
            st.download_button(
                "PNG 다운로드",
                data=st.session_state[png_key],
                file_name=quote_filename(quote_id, "png"),
                mime="image/png",
                use_container_width=True,
                key=f"{key_prefix}_download_png_{quote_id}_{signature}",
            )
    with c2:
        if st.button("PDF 파일 생성", use_container_width=True, key=f"{key_prefix}_make_pdf_{quote_id}_{signature}"):
            with st.spinner("PDF 생성 중..."):
                st.session_state[pdf_key] = cached_quote_pdf_bytes(quote_id, signature)
        if pdf_key in st.session_state:
            st.download_button(
                "PDF 다운로드",
                data=st.session_state[pdf_key],
                file_name=quote_filename(quote_id, "pdf"),
                mime="application/pdf",
                use_container_width=True,
                key=f"{key_prefix}_download_pdf_{quote_id}_{signature}",
            )


def quote_filename(quote_id, ext="png"):
    quote = get_quote(quote_id)
    team = re.sub(r"[^0-9A-Za-z가-힣_-]", "_", quote.get("team_name", "팀") if quote else "팀")
    pickup = str(quote.get("pickup_date", "")).replace("-", "") if quote else ""
    ret = str(quote.get("return_date", "")).replace("-", "") if quote else ""
    return f"프라비_견적서_{team}_{pickup}-{ret}.{ext}"


# -----------------------------
# UI 헬퍼
# -----------------------------

def show_thumb_from_values(local_path="", url="", width=None):
    if url:
        try:
            st.image(str(url), use_container_width=True)
            return
        except Exception:
            pass
    try:
        if local_path and os.path.exists(str(local_path)):
            st.image(str(local_path), use_container_width=True)
            return
    except Exception:
        pass
    st.caption("이미지 없음")


def image_src_for_html(local_path="", url=""):
    # 배포 환경에서는 thumbnail_url을 우선 사용한다.
    if url:
        return str(url or "")
    try:
        if local_path and os.path.exists(str(local_path)):
            ext = os.path.splitext(str(local_path))[1].lower().replace(".", "") or "jpg"
            if ext == "jpg":
                ext = "jpeg"
            with open(str(local_path), "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            return f"data:image/{ext};base64,{b64}"
    except Exception:
        pass
    return ""



def render_product_card_html(row, badge_html="", subline="", selected=False, unavailable=False, extra_badge=""):
    # 선택 상태는 카드 내부 박스를 만들지 않고, 바깥 카드 wrapper에 테두리가 잡히도록 marker만 둔다.
    cls = "product-card-inner"
    img_src = image_src_for_html(row.get("local_thumbnail_path", ""), row.get("thumbnail_url", ""))
    if img_src:
        img_html = f"<img src='{escape(img_src, quote=True)}' />"
    else:
        img_html = "<div class='no-img'>이미지 없음</div>"

    name = escape(str(row.get("name", "") or ""))
    product_no = escape(str(row.get("product_no", "") or ""))
    size = escape(str(row.get("size_text", "") or "-"))
    sub = escape(str(subline or ""))
    selected_marker = "<span class='selected-card-marker'></span>" if selected else ""
    selected_html = "<div class='selected-badge-row'>" + status_badge("선택됨", "selected") + "</div>" if selected else ""

    return f"""
    <div class='{cls}'>
        {selected_marker}
        {selected_html}
        <div class='thumb-box'>{img_html}</div>
        <div class='product-name'>{name}</div>
        <div class='product-meta'>{size}</div>
        <div class='product-meta'>상품번호: {product_no}</div>
        <div class='product-meta'>{sub}</div>
    </div>
    """


def pagination_controls(total_count, page_size, state_key):
    total_pages = max(1, math.ceil(total_count / page_size))
    if state_key not in st.session_state:
        st.session_state[state_key] = 1
    st.session_state[state_key] = min(max(1, st.session_state[state_key]), total_pages)

    start = (st.session_state[state_key] - 1) * page_size
    end = start + page_size

    st.markdown("<div class='pagination-wrap'>", unsafe_allow_html=True)
    outer = st.columns([5, 2.8, 5])
    with outer[1]:
        c1, c2, c3 = st.columns([1, 1.2, 1])
        with c1:
            if st.button("이전", key=f"{state_key}_prev", disabled=st.session_state[state_key] <= 1, use_container_width=True):
                st.session_state[state_key] -= 1
                st.rerun()
        with c2:
            st.markdown(f"<div class='page-indicator'>{st.session_state[state_key]} / {total_pages}</div>", unsafe_allow_html=True)
        with c3:
            if st.button("다음", key=f"{state_key}_next", disabled=st.session_state[state_key] >= total_pages, use_container_width=True):
                st.session_state[state_key] += 1
                st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)
    return start, end, total_pages

def render_quantity_stepper(label, current_value, max_value, key_prefix):
    """보유수량을 넘지 않는 + / - 수량 조절기."""
    max_value = max(safe_int(max_value, 1), 1)
    current_value = min(max(safe_int(current_value, 1), 1), max_value)
    state_key = f"{key_prefix}_value"
    if state_key not in st.session_state:
        st.session_state[state_key] = current_value
    st.session_state[state_key] = min(max(safe_int(st.session_state[state_key], current_value), 1), max_value)

    st.caption(label)
    c1, c2, c3 = st.columns([1, 1.4, 1])
    with c1:
        if st.button("−", key=f"{key_prefix}_minus", use_container_width=True):
            if st.session_state[state_key] > 1:
                st.session_state[state_key] -= 1
                st.rerun()
    with c2:
        st.markdown(f"<div class='qty-display'>{st.session_state[state_key]}</div>", unsafe_allow_html=True)
    with c3:
        if st.button("+", key=f"{key_prefix}_plus", use_container_width=True):
            if st.session_state[state_key] >= max_value:
                try:
                    st.toast(f"보유수량이 {max_value}개입니다.", icon="⚠️")
                except Exception:
                    st.warning(f"보유수량이 {max_value}개입니다.")
            else:
                st.session_state[state_key] += 1
                st.rerun()
    return int(st.session_state[state_key])


def status_badge(text, kind="neutral"):
    colors = {
        "ok": ("#EAFBEF", "#087A2E"),
        "bad": ("#FDECEC", "#C5221F"),
        "pending": ("#FFF1CC", "#B06000"),
        "selected": ("#EAF3FF", "#1A73E8"),
        "confirmed": ("#EAFBEF", "#087A2E"),
        "partial": ("#F1EAFF", "#6941C6"),
        "returned": ("#EEF4FF", "#3538CD"),
        "deleted": ("#F2F4F7", "#667085"),
        "neutral": ("#F2F4F7", "#667085"),
    }
    bg, fg = colors.get(kind, colors["neutral"])
    return f"<span class='badge badge-{kind}' style='background:{bg};color:{fg};'>{text}</span>"


def status_kind(status):
    return {
        "견적중": "pending",
        "확정": "confirmed",
        "부분반납": "partial",
        "반납완료": "returned",
        "삭제": "deleted",
    }.get(str(status), "neutral")


def availability_badges(product_no, pickup_date, return_date, exclude_quote_id=None):
    available_qty, total_qty, reserved = get_available_qty(product_no, pickup_date, return_date, exclude_quote_id=exclude_quote_id)
    pending = get_pending_quote_qty(product_no, pickup_date, return_date, exclude_quote_id=exclude_quote_id)
    status_html = status_badge("가능", "ok") if available_qty > 0 else status_badge("불가", "bad")
    pending_html = status_badge(f"견적중 {pending}", "pending") if pending > 0 else ""
    return status_html, pending_html, available_qty, total_qty, reserved, pending


def product_card_css_class(is_available=True, selected=False):
    if selected:
        return "product-card selected-card"
    if not is_available:
        return "product-card unavailable-card"
    return "product-card"


def render_table_selection(df, display_cols, key, height=320):
    """st.dataframe row selection. Returns selected positional row index or None."""
    table_df = df.reset_index(drop=True)
    display_df = table_df[display_cols].copy()
    try:
        event = st.dataframe(
            display_df,
            use_container_width=True,
            height=height,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key=key,
        )
        selected_rows = getattr(event.selection, "rows", []) if hasattr(event, "selection") else []
        if selected_rows:
            pos = int(selected_rows[0])
            if 0 <= pos < len(table_df):
                return pos, table_df
            return None, table_df
    except (TypeError, IndexError):
        st.dataframe(display_df, use_container_width=True, height=height, hide_index=True)
    return None, table_df





def render_status_button_bar(product_no, pickup_date=None, return_date=None, key_prefix="status", exclude_quote_id=None, precomputed=None, total_qty_override=None):
    """상품 카드 안에서 쓰는 가능/불가/견적중 버튼 영역."""
    if precomputed is not None:
        total_qty = safe_int(total_qty_override, 1)
        reserved = safe_int(precomputed.get("reserved", 0), 0)
        pending = safe_int(precomputed.get("pending", 0), 0)
        available_qty = max(total_qty - reserved, 0)
    else:
        product = get_product(product_no) or {}
        if pickup_date is not None and return_date is not None:
            available_qty, total_qty, reserved = get_available_qty(product_no, pickup_date, return_date, exclude_quote_id=exclude_quote_id)
            pending = get_pending_quote_qty(product_no, pickup_date, return_date, exclude_quote_id=exclude_quote_id)
        else:
            total_qty = safe_int(product.get("qty", 1), 1)
            reserved = get_active_rental_qty_total(product_no)
            pending = get_pending_quote_qty(product_no)
            available_qty = max(total_qty - reserved, 0)

    # 상태 버튼은 카드 상단에서 한 줄로 보이게 2칸만 사용한다.
    # 3칸으로 나누면 '가능', '견적중' 글자가 세로로 깨져 보이는 문제가 생긴다.
    cols = st.columns([1, 1])
    with cols[0]:
        if reserved > 0:
            if st.button(f"🔴 불가 {reserved}", key=f"{key_prefix}_bad_{product_no}", use_container_width=True):
                if availability_detail_dialog is not None:
                    availability_detail_dialog("active", product_no, pickup_date, return_date, exclude_quote_id=exclude_quote_id)
        else:
            if st.button("🟢 가능", key=f"{key_prefix}_ok_{product_no}", use_container_width=True):
                if availability_detail_dialog is not None:
                    availability_detail_dialog("available", product_no, pickup_date, return_date, exclude_quote_id=exclude_quote_id)
    with cols[1]:
        if pending > 0:
            if st.button(f"🟠 견적중 {pending}", key=f"{key_prefix}_pending_{product_no}", use_container_width=True):
                if availability_detail_dialog is not None:
                    availability_detail_dialog("pending", product_no, pickup_date, return_date, exclude_quote_id=exclude_quote_id)
        else:
            st.markdown("<div class='status-placeholder'></div>", unsafe_allow_html=True)
    return available_qty, total_qty, reserved, pending


def render_product_tile(row, key_prefix, pickup_date=None, return_date=None, selected=False, select_label="선택", exclude_quote_id=None, show_select=True, status_info=None):
    """제품 조회/견적 만들기/제품 추가 팝업에서 공통으로 쓰는 카드 UI.
    v8 hotfix: HTML 문자열이 그대로 노출되는 문제를 막기 위해 상품 본문은 Streamlit 네이티브 요소로 렌더링한다.
    """
    product_no = str(row.get("product_no", ""))

    # 선택 상태는 바깥 컨테이너에 CSS 마커를 심어서 처리한다.
    if selected:
        st.markdown("<span class='selected-card-marker'></span>", unsafe_allow_html=True)

    with st.container(border=True):
        available_qty, total_qty, reserved, pending = render_status_button_bar(
            product_no, pickup_date, return_date, key_prefix=key_prefix, exclude_quote_id=exclude_quote_id,
            precomputed=status_info, total_qty_override=row.get("qty", 1)
        )

        if selected:
            st.markdown("<div style='margin:4px 0 8px 0;'>" + status_badge("선택됨", "selected") + "</div>", unsafe_allow_html=True)

        # 이미지 영역
        st.markdown("<div class='native-card-gap'></div>", unsafe_allow_html=True)
        thumb_path = str(row.get("local_thumbnail_path", "") or "")
        thumb_url = str(row.get("thumbnail_url", "") or "")
        show_thumb_from_values(thumb_path, thumb_url)

        # 상품 정보 영역
        name = escape(str(row.get("name", "") or ""))
        size = escape(str(row.get("size_text", "") or "-"))
        st.markdown(f"<div class='product-name native-product-name'>{name}</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='product-meta'>{size}</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='product-meta'>상품번호: {escape(product_no)}</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='product-meta native-bottom-meta'>보유 {total_qty}</div>", unsafe_allow_html=True)

        clicked = False
        if show_select:
            clicked = st.button(select_label, key=f"{key_prefix}_select_{product_no}", use_container_width=True)
        return clicked, available_qty, reserved, pending

def first_quote_item_thumb(quote_id):
    items = load_quote_items_df(int(quote_id))
    if items.empty:
        return "", ""
    return str(items.iloc[0].get("thumbnail_path", "") or ""), str(items.iloc[0].get("thumbnail_url", "") or "")



def quote_list_card(row, key_prefix="quote"):
    thumb_path = str(row.get("first_thumb_path", "") or "") if hasattr(row, "get") else ""
    thumb_url = str(row.get("first_thumb_url", "") or "") if hasattr(row, "get") else ""
    if not thumb_path and not thumb_url:
        thumb_path, thumb_url = first_quote_item_thumb(row["quote_id"])
    with st.container(border=True):
        c0, c1, c2, c3, c4 = st.columns([0.9, 2.0, 2.2, 2.6, 1.6])
        with c0:
            if thumb_path or thumb_url:
                show_thumb_from_values(thumb_path, thumb_url)
            else:
                st.caption("이미지 없음")
        with c1:
            st.markdown(f"### {row['team_name']}")
            st.caption(format_datetime_short(row.get("created_at", "")))
            _status_label = valid_status_text(row.get("status"))
            if _status_label:
                st.markdown(status_badge(_status_label, status_kind(_status_label)), unsafe_allow_html=True)
        with c2:
            st.markdown(f"**{row['quote_no']}**")
            st.caption(f"픽업 {row['pickup_date']}")
            st.caption(f"반납 {row['return_date']}")
        with c3:
            st.markdown(f"**{row.get('상품요약','')}**")
            st.caption(f"총액 {money(row.get('total', 0))}")
        with c4:
            detail = st.button("상세 보기", key=f"{key_prefix}_detail_{row['quote_id']}", use_container_width=True)
            confirm = False
            if row["status"] == "견적중":
                confirm = st.button("확정", key=f"{key_prefix}_confirm_{row['quote_id']}", use_container_width=True)
            delete = st.button("삭제", key=f"{key_prefix}_delete_{row['quote_id']}", use_container_width=True)
    return detail, confirm, delete


def rental_list_card(row, key_prefix="rental"):
    thumb_path = str(row.get("first_thumb_path", "") or "") if hasattr(row, "get") else ""
    thumb_url = str(row.get("first_thumb_url", "") or "") if hasattr(row, "get") else ""
    if not thumb_path and not thumb_url:
        thumb_path, thumb_url = first_quote_item_thumb(row["quote_id"])
    with st.container(border=True):
        c0, c1, c2, c3, c4 = st.columns([0.9, 2.0, 2.2, 2.6, 1.6])
        with c0:
            if thumb_path or thumb_url:
                show_thumb_from_values(thumb_path, thumb_url)
            else:
                st.caption("이미지 없음")
        with c1:
            st.markdown(f"### {row['team_name']}")
            st.caption(format_datetime_short(row.get("created_at", "")))
            _status_label = valid_status_text(row.get("status"))
            if _status_label:
                st.markdown(status_badge(_status_label, status_kind(_status_label)), unsafe_allow_html=True)
        with c2:
            st.markdown(f"**{row['quote_no']}**")
            st.caption(f"픽업 {row['pickup_date']}")
            st.caption(f"반납 {row['return_date']}")
        with c3:
            st.markdown(f"**{row.get('상품요약','')}**")
            st.caption(f"총액 {money(row.get('total', 0))}")
        with c4:
            detail = st.button("상세 보기", key=f"{key_prefix}_detail_{row['quote_id']}", use_container_width=True)
            return_all = False
            if row["status"] in ["확정", "부분반납"]:
                return_all = st.button("전체 반납", key=f"{key_prefix}_return_{row['quote_id']}", use_container_width=True)
            date_edit = False
            if row["status"] in ["확정", "부분반납"]:
                date_edit = st.button("날짜 수정", key=f"{key_prefix}_date_{row['quote_id']}", use_container_width=True)
    return detail, return_all, date_edit

def cafe24_product_code_to_no(value):
    """Cafe24 상품코드(P0000LPP 등)를 products.product_no 값으로 변환한다."""
    code = clean_text(value).upper()
    if not code:
        return ""

    # 이미 숫자 상품번호가 들어온 CSV도 허용한다.
    if code.isdigit():
        try:
            return str(int(code))
        except Exception:
            return ""

    if code.startswith("P"):
        code = code[1:]

    # Cafe24 상품코드의 앞쪽 0 패딩 제거 후 A=0 기준 26진수로 해석한다.
    code = code.lstrip("0")
    if not code or not re.fullmatch(r"[A-Z]+", code):
        return ""

    product_no = 0
    for ch in code:
        product_no = product_no * 26 + (ord(ch) - ord("A"))
    return str(product_no)


def parse_supply_price_value(value):
    """120000.00, 120,000원 같은 값을 원 단위 정수로 정리한다."""
    text = str(value or "").strip()
    if not text:
        return 0
    text = re.sub(r"[^0-9.\-]", "", text)
    if not text or text in {"-", ".", "-."}:
        return 0
    try:
        return max(int(float(text)), 0)
    except Exception:
        return 0


def read_cafe24_csv(uploaded_file):
    """Cafe24 CSV를 UTF-8/CP949/EUC-KR 순서로 읽는다."""
    raw = uploaded_file.getvalue()
    errors = []
    for encoding in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            df = pd.read_csv(
                BytesIO(raw),
                encoding=encoding,
                dtype=str,
                keep_default_na=False,
            )
            return df, encoding
        except Exception as e:
            errors.append(f"{encoding}: {e}")
    raise ValueError("CSV 파일을 읽지 못했습니다. " + " / ".join(errors[:2]))


def prepare_supply_price_import(uploaded_file):
    """원본 Cafe24 CSV와 products 테이블을 비교해 공급가 변경 미리보기를 만든다."""
    source_df, encoding = read_cafe24_csv(uploaded_file)

    required = ["상품코드", "공급가"]
    missing = [c for c in required if c not in source_df.columns]
    if missing:
        raise ValueError("필수 열이 없습니다: " + ", ".join(missing))

    work = pd.DataFrame({
        "source_row": range(2, len(source_df) + 2),
        "cafe24_product_code": source_df["상품코드"].astype(str).str.strip(),
        "csv_product_name": (
            source_df["상품명"].astype(str).str.strip()
            if "상품명" in source_df.columns
            else ""
        ),
        "raw_supply_price": source_df["공급가"].astype(str).str.strip(),
    })
    work["product_no"] = work["cafe24_product_code"].map(cafe24_product_code_to_no)
    work["new_price"] = work["raw_supply_price"].map(parse_supply_price_value)

    invalid_code_count = int((work["product_no"] == "").sum())
    zero_skipped_count = int((work["new_price"] <= 0).sum())

    positive = work[(work["product_no"] != "") & (work["new_price"] > 0)].copy()
    duplicate_extra_count = int(len(positive) - positive["product_no"].nunique())
    positive = positive.drop_duplicates(subset=["product_no"], keep="last")

    db_rows = table_all("products", select="product_no,name,price")
    db_df = df_from_rows(db_rows, ["product_no", "name", "price"])
    if db_df.empty:
        db_df = pd.DataFrame(columns=["product_no", "db_product_name", "current_price", "db_exists"])
    else:
        db_df["product_no"] = db_df["product_no"].astype(str).str.strip()
        db_df["db_product_name"] = db_df["name"].fillna("").astype(str)
        db_df["current_price"] = db_df["price"].map(lambda v: safe_int(v, 0))
        db_df["db_exists"] = True
        db_df = db_df[["product_no", "db_product_name", "current_price", "db_exists"]]

    preview = positive.merge(db_df, on="product_no", how="left")
    if "db_exists" not in preview.columns:
        preview["db_exists"] = False
    preview["db_exists"] = preview["db_exists"].fillna(False).astype(bool)
    preview["current_price"] = preview["current_price"].map(lambda v: safe_int(v, 0))
    preview["changed"] = preview["db_exists"] & (preview["current_price"] != preview["new_price"])

    def row_status(row):
        if not row["db_exists"]:
            return "DB 미매칭"
        if row["changed"]:
            return "변경 예정"
        return "동일"

    preview["status"] = preview.apply(row_status, axis=1)
    status_rank = {"변경 예정": 0, "DB 미매칭": 1, "동일": 2}
    preview["__rank"] = preview["status"].map(status_rank).fillna(9)
    preview = preview.sort_values(["__rank", "product_no"]).drop(columns=["__rank"]).reset_index(drop=True)

    return {
        "encoding": encoding,
        "source_count": int(len(source_df)),
        "positive_unique_count": int(len(positive)),
        "zero_skipped_count": zero_skipped_count,
        "invalid_code_count": invalid_code_count,
        "duplicate_extra_count": duplicate_extra_count,
        "matched_count": int(preview["db_exists"].sum()) if not preview.empty else 0,
        "unmatched_count": int((~preview["db_exists"]).sum()) if not preview.empty else 0,
        "changed_count": int(preview["changed"].sum()) if not preview.empty else 0,
        "same_count": int((preview["db_exists"] & ~preview["changed"]).sum()) if not preview.empty else 0,
        "preview": preview,
    }


def apply_supply_price_updates(preview_df):
    """미리보기 중 변경 예정 행만 products.price에 일괄 반영한다."""
    changes = preview_df[preview_df["changed"]].copy()
    if changes.empty:
        return 0

    # updated_at은 건드리지 않는다. 제품 조회 화면의 기존 정렬 순서를 유지하기 위함이다.
    rows = [
        {
            "product_no": str(row["product_no"]),
            "price": int(row["new_price"]),
        }
        for _, row in changes.iterrows()
    ]

    client = supabase_client()
    updated = 0
    for batch in chunked(rows, 300):
        client.table("products").upsert(batch, on_conflict="product_no").execute()
        updated += len(batch)

    set_meta("last_supply_price_import_at", now_text())
    clear_data_cache()
    return updated



def normalize_price_match_name(value):
    """상품명 매칭용 정규화.

    DB 상품명 예: 'P.B - 콘솔 15'
    엑셀 상품명 예: '콘솔 15'
    둘을 같은 상품으로 보기 위해 접두어/기호/공백을 제거한다.
    """
    text = clean_text(value).lower()
    if not text:
        return ""
    text = re.sub(r"p\s*\.?\s*b\s*[-–—_]*", "", text, flags=re.I)
    text = text.replace("프라비", "")
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"[^0-9a-z가-힣]+", "", text)
    return text.strip()


def is_valid_excel_price_name(value):
    text = clean_text(value)
    if not text:
        return False
    # 숫자만 있는 값, 가격/수량/사이즈처럼 보이는 값은 상품명에서 제외한다.
    compact = re.sub(r"\s+", "", text)
    if re.fullmatch(r"[0-9.,]+", compact):
        return False
    if re.fullmatch(r"[0-9.,]+[xX×][0-9.,xX×]+", compact):
        return False
    if not re.search(r"[가-힣A-Za-z]", text):
        return False
    return True


def detect_excel_price_columns(df):
    """엑셀 추출 CSV의 상품명/가격 컬럼을 찾는다."""
    cols = [str(c) for c in df.columns]
    name_candidates = [
        "product_name_excel", "상품명", "product_name", "name", "이름", "품명", "제품명"
    ]
    price_candidates = [
        "price", "가격", "렌탈가", "대여가", "단가", "공급가", "rental_price"
    ]
    name_col = next((c for c in name_candidates if c in cols), None)
    price_col = next((c for c in price_candidates if c in cols), None)
    if not name_col or not price_col:
        raise ValueError(
            "CSV에서 상품명/가격 컬럼을 찾지 못했습니다. "
            "필요 컬럼 예: product_name_excel, price"
        )
    return name_col, price_col


def prepare_excel_name_price_import(uploaded_file):
    """엑셀에서 추출한 상품명/가격 CSV를 products 테이블과 상품명 기준으로 매칭한다."""
    source_df, encoding = read_cafe24_csv(uploaded_file)
    name_col, price_col = detect_excel_price_columns(source_df)

    work = pd.DataFrame({
        "source_row": range(2, len(source_df) + 2),
        "excel_product_name": source_df[name_col].astype(str).map(clean_text),
        "raw_price": source_df[price_col].astype(str).map(clean_text),
    })
    if "sheet" in source_df.columns:
        work["sheet"] = source_df["sheet"].astype(str).map(clean_text)
    else:
        work["sheet"] = ""
    if "excel_row" in source_df.columns:
        work["excel_row"] = source_df["excel_row"].astype(str).map(clean_text)
    else:
        work["excel_row"] = work["source_row"].astype(str)

    work["new_price"] = work["raw_price"].map(parse_supply_price_value)
    work["normalized_name"] = work["excel_product_name"].map(normalize_price_match_name)
    work["valid_name"] = work["excel_product_name"].map(is_valid_excel_price_name)

    invalid_name_count = int((~work["valid_name"]).sum())
    zero_skipped_count = int((work["new_price"] <= 0).sum())

    valid = work[(work["valid_name"]) & (work["new_price"] > 0) & (work["normalized_name"] != "")].copy()

    # CSV 안에서 같은 상품명이 여러 가격을 가지면 자동 반영하지 않는다.
    csv_records = []
    duplicate_price_names = set()
    for norm, g in valid.groupby("normalized_name", dropna=False):
        prices = sorted(set(int(x) for x in g["new_price"].dropna().astype(int).tolist()))
        names = sorted(set(str(x) for x in g["excel_product_name"].dropna().tolist()))
        sheets = ", ".join(sorted(set(str(x) for x in g["sheet"].dropna().tolist()))[:5])
        rows = ", ".join(sorted(set(str(x) for x in g["excel_row"].dropna().tolist()))[:8])
        if len(prices) > 1:
            duplicate_price_names.add(norm)
            csv_records.append({
                "normalized_name": norm,
                "excel_product_name": names[0] if names else "",
                "new_price": prices[-1],
                "excel_prices": ", ".join(str(p) for p in prices),
                "sheet": sheets,
                "excel_row": rows,
                "csv_duplicate_price": True,
            })
        else:
            csv_records.append({
                "normalized_name": norm,
                "excel_product_name": names[0] if names else "",
                "new_price": prices[0] if prices else 0,
                "excel_prices": str(prices[0]) if prices else "",
                "sheet": sheets,
                "excel_row": rows,
                "csv_duplicate_price": False,
            })

    csv_df = pd.DataFrame(csv_records)
    if csv_df.empty:
        csv_df = pd.DataFrame(columns=[
            "normalized_name", "excel_product_name", "new_price", "excel_prices",
            "sheet", "excel_row", "csv_duplicate_price"
        ])

    db_rows = table_all("products", select="product_no,name,price")
    db_df = df_from_rows(db_rows, ["product_no", "name", "price"])
    if db_df.empty:
        db_df = pd.DataFrame(columns=["normalized_name", "product_no", "db_product_name", "current_price", "db_duplicate"])
    else:
        db_df["product_no"] = db_df["product_no"].astype(str).str.strip()
        db_df["db_product_name"] = db_df["name"].fillna("").astype(str)
        db_df["normalized_name"] = db_df["db_product_name"].map(normalize_price_match_name)
        db_df["current_price"] = db_df["price"].map(lambda v: safe_int(v, 0))
        db_dup_map = db_df.groupby("normalized_name")["product_no"].transform("count") > 1
        db_df["db_duplicate"] = db_dup_map
        db_df = db_df[["normalized_name", "product_no", "db_product_name", "current_price", "db_duplicate"]]

    preview = csv_df.merge(db_df, on="normalized_name", how="left")
    for c in ["product_no", "db_product_name"]:
        if c not in preview.columns:
            preview[c] = ""
    if "current_price" not in preview.columns:
        preview["current_price"] = 0
    if "db_duplicate" not in preview.columns:
        preview["db_duplicate"] = False

    preview["db_exists"] = preview["product_no"].fillna("").astype(str).str.strip() != ""
    preview["current_price"] = preview["current_price"].map(lambda v: safe_int(v, 0))
    preview["db_duplicate"] = preview["db_duplicate"].fillna(False).astype(bool)
    preview["csv_duplicate_price"] = preview["csv_duplicate_price"].fillna(False).astype(bool)
    preview["changed"] = (
        preview["db_exists"]
        & (~preview["db_duplicate"])
        & (~preview["csv_duplicate_price"])
        & (preview["current_price"] != preview["new_price"])
    )

    def row_status(row):
        if row["csv_duplicate_price"]:
            return "CSV 중복가격 확인필요"
        if not row["db_exists"]:
            return "DB 미매칭"
        if row["db_duplicate"]:
            return "DB 중복 확인필요"
        if row["changed"]:
            return "변경 예정"
        return "동일"

    preview["status"] = preview.apply(row_status, axis=1)
    status_rank = {
        "변경 예정": 0,
        "CSV 중복가격 확인필요": 1,
        "DB 중복 확인필요": 2,
        "DB 미매칭": 3,
        "동일": 4,
    }
    preview["__rank"] = preview["status"].map(status_rank).fillna(9)
    preview = preview.sort_values(["__rank", "normalized_name", "product_no"]).drop(columns=["__rank"]).reset_index(drop=True)

    return {
        "encoding": encoding,
        "source_count": int(len(source_df)),
        "valid_count": int(len(valid)),
        "unique_name_count": int(csv_df["normalized_name"].nunique()) if not csv_df.empty else 0,
        "invalid_name_count": invalid_name_count,
        "zero_skipped_count": zero_skipped_count,
        "matched_count": int(preview["db_exists"].sum()) if not preview.empty else 0,
        "unmatched_count": int((~preview["db_exists"]).sum()) if not preview.empty else 0,
        "changed_count": int(preview["changed"].sum()) if not preview.empty else 0,
        "same_count": int((preview["status"] == "동일").sum()) if not preview.empty else 0,
        "csv_duplicate_count": int((preview["status"] == "CSV 중복가격 확인필요").sum()) if not preview.empty else 0,
        "db_duplicate_count": int((preview["status"] == "DB 중복 확인필요").sum()) if not preview.empty else 0,
        "preview": preview,
    }


def apply_excel_name_price_updates(preview_df):
    """상품명 매칭 미리보기 중 변경 예정 행만 products.price에 반영한다."""
    changes = preview_df[preview_df["changed"]].copy()
    if changes.empty:
        return 0

    rows = []
    seen = set()
    for _, row in changes.iterrows():
        pno = str(row.get("product_no", "")).strip()
        if not pno or pno in seen:
            continue
        seen.add(pno)
        rows.append({"product_no": pno, "price": int(row["new_price"])})

    client = supabase_client()
    updated = 0
    for batch in chunked(rows, 300):
        client.table("products").upsert(batch, on_conflict="product_no").execute()
        updated += len(batch)

    set_meta("last_excel_name_price_import_at", now_text())
    clear_data_cache()
    return updated


def render_excel_name_price_import_panel():
    st.write("엑셀에서 추출한 **상품명/가격 CSV**를 업로드해서, DB에 이미 있는 상품명만 기본 단가로 반영합니다.")
    st.caption("상품명 기준 매칭입니다. DB에 없는 상품은 새로 만들지 않고, 중복 상품명/중복 가격은 자동 반영하지 않습니다.")

    flash = st.session_state.pop("excel_name_price_import_flash", "")
    if flash:
        st.success(flash)

    uploaded = st.file_uploader(
        "엑셀 추출 가격 CSV 업로드",
        type=["csv"],
        key="excel_name_price_csv_upload",
        help="예: pravi_excel_prices_extracted_no_numeric.csv",
    )
    if uploaded is None:
        st.info("CSV를 선택하면 DB 상품명과 비교한 뒤, 실제 반영 전에 변경 목록을 보여줍니다.")
        return

    try:
        result = prepare_excel_name_price_import(uploaded)
    except Exception as e:
        st.error(f"CSV 확인 실패: {e}")
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("CSV 전체", f"{result['source_count']:,}개")
    m2.metric("상품명 유효", f"{result['valid_count']:,}개")
    m3.metric("DB 매칭", f"{result['matched_count']:,}개")
    m4.metric("변경 예정", f"{result['changed_count']:,}개")

    st.caption(
        f"인코딩: {result['encoding']} · 0원/빈가격 제외 {result['zero_skipped_count']:,}개 · "
        f"숫자만/빈 상품명 제외 {result['invalid_name_count']:,}개 · "
        f"DB 미매칭 {result['unmatched_count']:,}개 · 동일 {result['same_count']:,}개"
    )
    if result["csv_duplicate_count"]:
        st.warning(f"CSV 안에서 같은 상품명에 가격이 여러 개인 항목 {result['csv_duplicate_count']:,}개는 자동 반영하지 않습니다.")
    if result["db_duplicate_count"]:
        st.warning(f"DB에 같은 이름으로 중복 매칭된 항목 {result['db_duplicate_count']:,}개는 자동 반영하지 않습니다.")

    preview = result["preview"]
    if preview.empty:
        st.warning("반영할 수 있는 가격 데이터가 없습니다.")
        return

    view = preview[[
        "status", "excel_product_name", "db_product_name", "product_no",
        "current_price", "new_price", "sheet", "excel_row", "excel_prices"
    ]].copy()
    view.columns = ["상태", "엑셀 상품명", "DB 상품명", "상품번호", "현재 단가", "새 단가", "시트", "엑셀 행", "엑셀 가격들"]

    st.markdown("#### 반영 미리보기")
    st.dataframe(view.head(500), use_container_width=True, hide_index=True, height=360)
    if len(view) > 500:
        st.caption("화면에는 앞 500개만 표시합니다. 아래 CSV 다운로드에는 전체 결과가 포함됩니다.")

    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "전체 비교표 다운로드",
            data=view.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"pravi_excel_name_price_preview_{today_yyyymmdd()}.csv",
            mime="text/csv",
            use_container_width=True,
            key="download_excel_name_price_preview",
        )
    with c2:
        changed_backup = view[view["상태"] == "변경 예정"].copy()
        st.download_button(
            "반영 전 가격 백업 다운로드",
            data=changed_backup.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"pravi_excel_name_price_backup_{today_yyyymmdd()}.csv",
            mime="text/csv",
            use_container_width=True,
            key="download_excel_name_price_backup",
        )

    for status in ["CSV 중복가격 확인필요", "DB 중복 확인필요", "DB 미매칭"]:
        sub = view[view["상태"] == status]
        if not sub.empty:
            with st.expander(f"{status} {len(sub):,}개 보기"):
                st.dataframe(sub, use_container_width=True, hide_index=True, height=260)

    if result["changed_count"] <= 0:
        st.info("현재 DB와 가격이 같거나, 자동 반영 가능한 항목이 없습니다.")
        return

    confirmed = st.checkbox(
        f"변경 예정 {result['changed_count']:,}개의 단가를 products.price에 반영합니다.",
        key="confirm_excel_name_price_import",
    )
    if st.button(
        "엑셀 가격 DB 반영 실행",
        type="primary",
        use_container_width=True,
        disabled=not confirmed,
        key="apply_excel_name_price_import",
    ):
        try:
            with st.spinner("엑셀 가격을 DB에 반영하는 중입니다..."):
                updated = apply_excel_name_price_updates(preview)
            st.session_state["selected_quote_items"] = {}
            st.session_state["excel_name_price_import_flash"] = (
                f"완료: {updated:,}개 상품의 기본 단가를 업데이트했습니다. 새 견적부터 이 단가가 자동 입력됩니다."
            )
            st.rerun()
        except Exception as e:
            st.error(f"DB 반영 실패: {e}")

def render_supply_price_import_panel():
    st.write("Cafe24 상품 CSV의 **공급가가 0원보다 큰 상품만** 프로그램 DB의 기본 단가로 반영합니다.")
    st.caption("0원 상품은 기존 DB 가격을 유지하며, DB에 없는 상품은 새로 만들지 않습니다. 기존 견적서 금액도 변경하지 않습니다.")

    flash = st.session_state.pop("supply_price_import_flash", "")
    if flash:
        st.success(flash)

    uploaded = st.file_uploader(
        "Cafe24 상품 CSV 업로드",
        type=["csv"],
        key="supply_price_csv_upload",
        help="Cafe24 상품관리에서 내려받은 원본 CSV를 그대로 선택하세요.",
    )
    if uploaded is None:
        st.info("CSV를 선택하면 DB와 비교한 뒤, 실제 반영 전에 변경 목록을 보여줍니다.")
        return

    try:
        result = prepare_supply_price_import(uploaded)
    except Exception as e:
        st.error(f"CSV 확인 실패: {e}")
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("CSV 전체", f"{result['source_count']:,}개")
    m2.metric("0원 제외 후", f"{result['positive_unique_count']:,}개")
    m3.metric("DB 매칭", f"{result['matched_count']:,}개")
    m4.metric("변경 예정", f"{result['changed_count']:,}개")

    st.caption(
        f"인코딩: {result['encoding']} · 0원 제외 {result['zero_skipped_count']:,}개 · "
        f"DB 미매칭 {result['unmatched_count']:,}개 · 동일 가격 {result['same_count']:,}개"
    )
    if result["invalid_code_count"]:
        st.warning(f"상품코드를 해석하지 못한 행이 {result['invalid_code_count']:,}개 있습니다.")
    if result["duplicate_extra_count"]:
        st.warning(f"중복 상품번호 {result['duplicate_extra_count']:,}개는 CSV의 마지막 값을 사용합니다.")

    preview = result["preview"]
    if preview.empty:
        st.warning("반영할 수 있는 공급가 데이터가 없습니다.")
        return

    view = preview[[
        "status", "product_no", "cafe24_product_code", "csv_product_name",
        "db_product_name", "current_price", "new_price"
    ]].copy()
    view.columns = ["상태", "상품번호", "Cafe24 상품코드", "CSV 상품명", "DB 상품명", "현재 단가", "새 단가"]

    st.markdown("#### 반영 미리보기")
    st.dataframe(view.head(500), use_container_width=True, hide_index=True, height=360)
    if len(view) > 500:
        st.caption("화면에는 앞 500개만 표시합니다. 아래 CSV 다운로드에는 전체 결과가 포함됩니다.")

    export_bytes = view.to_csv(index=False).encode("utf-8-sig")
    changed_backup = view[view["상태"] == "변경 예정"].copy()
    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "전체 비교표 다운로드",
            data=export_bytes,
            file_name=f"pravi_supply_price_preview_{today_yyyymmdd()}.csv",
            mime="text/csv",
            use_container_width=True,
            key="download_supply_preview",
        )
    with c2:
        st.download_button(
            "반영 전 가격 백업 다운로드",
            data=changed_backup.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"pravi_supply_price_backup_{today_yyyymmdd()}.csv",
            mime="text/csv",
            use_container_width=True,
            key="download_supply_backup",
        )

    if result["unmatched_count"]:
        unmatched_view = view[view["상태"] == "DB 미매칭"]
        with st.expander(f"DB 미매칭 {result['unmatched_count']:,}개 보기"):
            st.dataframe(unmatched_view, use_container_width=True, hide_index=True, height=260)

    confirmed = st.checkbox(
        f"변경 예정 {result['changed_count']:,}개의 단가를 products.price에 반영합니다.",
        key="confirm_supply_price_import",
    )

    if result["changed_count"] <= 0:
        st.info("현재 DB와 가격이 같아서 변경할 항목이 없습니다.")
        return

    if st.button(
        "공급가 DB 반영 실행",
        type="primary",
        use_container_width=True,
        disabled=not confirmed,
        key="apply_supply_price_import",
    ):
        try:
            with st.spinner("공급가를 DB에 반영하는 중입니다..."):
                updated = apply_supply_price_updates(preview)
            # 이미 화면에서 선택해 둔 견적 상품은 예전 단가를 들고 있을 수 있으므로 비운다.
            st.session_state["selected_quote_items"] = {}
            st.session_state["supply_price_import_flash"] = (
                f"완료: {updated:,}개 상품의 기본 단가를 업데이트했습니다. "
                "새 견적부터 이 단가가 자동 입력됩니다."
            )
            st.rerun()
        except Exception as e:
            st.error(f"DB 반영 실패: {e}")


def render_sync_panel():
    sync_tab, price_tab, excel_price_tab = st.tabs(["사이트 상품 동기화", "Cafe24 공급가 CSV", "엑셀 가격 CSV"])

    with sync_tab:
        st.write("사이트 카테고리 URL을 입력하고 상품을 프로그램 DB로 가져옵니다.")
        base_url = st.text_input("가져올 카테고리 URL", value=DEFAULT_CATEGORY_URL, key="sync_url")
        pages = st.number_input("가져올 페이지 수", min_value=1, max_value=200, value=1, step=1, key="sync_pages")
        delay = st.number_input("페이지 사이 쉬는 시간(초)", min_value=0.1, max_value=5.0, value=0.5, step=0.1, key="sync_delay")

        if st.button("상품 동기화 시작", type="primary", key="sync_start"):
            total = 0
            total_added = 0
            total_updated = 0
            progress = st.progress(0)
            log_box = st.empty()
            debug_rows = []

            for page in range(1, int(pages) + 1):
                page_url = set_page_param(base_url, page)
                log_box.info(f"{page}페이지 가져오는 중: {page_url}")
                try:
                    products, debug = parse_products_from_page(page_url)
                    added, updated = upsert_products(products)
                    total += len(products)
                    total_added += added
                    total_updated += updated
                    debug["page"] = page
                    debug["added"] = added
                    debug["updated"] = updated
                    debug_rows.append(debug)
                except Exception as e:
                    st.error(f"{page}페이지 오류: {e}")
                progress.progress(page / int(pages))
                time.sleep(float(delay))

            set_meta("last_sync_at", now_text())
            st.success(f"동기화 완료: 총 {total}개 처리 / 새 상품 {total_added}개 / 업데이트 {total_updated}개")
            if debug_rows:
                st.dataframe(pd.DataFrame(debug_rows), use_container_width=True)

    with price_tab:
        render_supply_price_import_panel()

    with excel_price_tab:
        render_excel_name_price_import_panel()

    if st.button("닫기", key="sync_dialog_close"):
        st.session_state["sync_dialog_open"] = False
        st.session_state["sync_fallback_open"] = False
        st.rerun()


def render_holiday_calendar_panel():
    today = date.today()
    if "holiday_calendar_cursor" not in st.session_state:
        st.session_state["holiday_calendar_cursor"] = today.replace(day=1).isoformat()
    if "holiday_draft_dates" not in st.session_state:
        st.session_state["holiday_draft_dates"] = sorted(get_holiday_dates())

    cursor = datetime.strptime(st.session_state["holiday_calendar_cursor"], "%Y-%m-%d").date().replace(day=1)
    selected = set(st.session_state.get("holiday_draft_dates", []))

    nav1, nav2, nav3 = st.columns([1, 3, 1])
    with nav1:
        if st.button("‹ 이전달", use_container_width=True, key="holiday_prev_month"):
            prev = (cursor - timedelta(days=1)).replace(day=1)
            st.session_state["holiday_calendar_cursor"] = prev.isoformat()
            st.rerun()
    with nav2:
        st.markdown(f"<div class='calendar-title'>{cursor.year}년 {cursor.month}월</div>", unsafe_allow_html=True)
    with nav3:
        if st.button("다음달 ›", use_container_width=True, key="holiday_next_month"):
            next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
            st.session_state["holiday_calendar_cursor"] = next_month.isoformat()
            st.rerun()

    weekday_cols = st.columns(7)
    for col, label in zip(weekday_cols, ["월", "화", "수", "목", "금", "토", "일"]):
        col.markdown(f"<div class='calendar-weekday'>{label}</div>", unsafe_allow_html=True)

    month_rows = calendar.Calendar(firstweekday=0).monthdatescalendar(cursor.year, cursor.month)
    for week in month_rows:
        cols = st.columns(7)
        for idx, day_value in enumerate(week):
            key = day_value.isoformat()
            in_month = day_value.month == cursor.month
            is_sunday = day_value.weekday() == 6
            is_selected = key in selected
            with cols[idx]:
                if not in_month:
                    st.button(" ", key=f"holiday_blank_{key}", disabled=True, use_container_width=True)
                elif is_sunday:
                    st.button(f"{day_value.day} · OFF", key=f"holiday_sunday_{key}", disabled=True, use_container_width=True)
                else:
                    label = f"{day_value.day} · 휴일" if is_selected else str(day_value.day)
                    if st.button(label, key=f"holiday_day_{key}", type="primary" if is_selected else "secondary", use_container_width=True):
                        if is_selected:
                            selected.discard(key)
                        else:
                            selected.add(key)
                        st.session_state["holiday_draft_dates"] = sorted(selected)
                        st.rerun()

    month_selected = sorted(x for x in selected if x.startswith(f"{cursor.year:04d}-{cursor.month:02d}-"))
    if month_selected:
        st.caption("이번 달 휴일: " + ", ".join(month_selected))
    else:
        st.caption("이번 달에 추가한 휴일이 없습니다. 일요일은 항상 OFF입니다.")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("휴일 저장", type="primary", use_container_width=True, key="holiday_save"):
            save_holiday_dates(selected)
            with st.spinner("활성 견적의 연박 가격을 다시 계산하는 중입니다..."):
                count = recalculate_open_quotes_for_holidays()
            st.session_state["holiday_dialog_open"] = False
            st.success(f"휴일을 저장하고 {count}개 견적을 재계산했습니다.")
            st.rerun()
    with c2:
        if st.button("닫기", use_container_width=True, key="holiday_close"):
            st.session_state["holiday_dialog_open"] = False
            st.session_state["scroll_to_top"] = True
            st.rerun()


if hasattr(st, "dialog"):
    @st.dialog("상품 동기화")
    def sync_dialog():
        render_sync_panel()

    @st.dialog("휴일 캘린더", width="large")
    def holiday_calendar_dialog():
        render_holiday_calendar_panel()
else:
    sync_dialog = None
    holiday_calendar_dialog = None


if hasattr(st, "dialog"):
    @st.dialog("상품 상태 상세")
    def availability_detail_dialog(kind, product_no, pickup_date=None, return_date=None, exclude_quote_id=None):
        product = get_product(product_no)
        st.markdown(f"### {product.get('name', product_no) if product else product_no}")
        if pickup_date is not None and return_date is not None:
            st.caption(f"조회 날짜: {pickup_date} ~ {return_date}")

        if kind == "active":
            st.markdown("#### 확정/대여중 기록")
            if pickup_date is not None and return_date is not None:
                df = load_conflicting_rentals_df(product_no, pickup_date, return_date, exclude_quote_id=exclude_quote_id)
            else:
                df = load_product_history_df(product_no, 50)
                df = df[df["status"].isin(list(ACTIVE_RENTAL_STATUSES))]
            if df.empty:
                st.info("겹치는 확정 대여가 없습니다.")
            else:
                view = df[["quote_no", "team_name", "pickup_date", "return_date", "quantity", "status"]].copy()
                view.columns = ["견적번호", "팀명", "픽업일", "반납일", "수량", "상태"]
                st.dataframe(view, use_container_width=True, hide_index=True)
        elif kind == "pending":
            st.markdown("#### 견적중 참고 목록")
            df = load_pending_quotes_for_product_df(product_no, pickup_date, return_date, exclude_quote_id=exclude_quote_id)
            if df.empty:
                st.info("겹치는 견적중 상품이 없습니다.")
            else:
                view = df[["quote_no", "team_name", "pickup_date", "return_date", "quantity", "created_at"]].copy()
                view.columns = ["견적번호", "팀명", "픽업일", "반납일", "수량", "생성일"]
                st.dataframe(view, use_container_width=True, hide_index=True)
        else:
            st.markdown("#### 가능 상태")
            st.success("선택한 날짜 기준으로 재고를 막는 확정 대여가 없습니다.")
            history = load_product_history_df(product_no, 20)
            if history.empty:
                st.info("최근 대여 이력이 없습니다.")
            else:
                st.caption("최근 대여/반납 이력")
                view = history[["quote_no", "team_name", "pickup_date", "return_date", "quantity", "status"]].copy()
                view.columns = ["견적번호", "팀명", "픽업일", "반납일", "수량", "상태"]
                st.dataframe(view, use_container_width=True, hide_index=True)

    @st.dialog("견적 상품 추가", width="large")
    def quote_add_product_dialog(quote_id):
        quote = get_quote(quote_id)
        if not quote:
            st.error("견적서를 찾지 못했습니다.")
            return
        if quote.get("status") not in ["견적중", "확정"]:
            st.info("반납이 시작된 견적서에는 상품을 추가할 수 없습니다.")
            return
        st.markdown("<div class='dialog-scroll'>", unsafe_allow_html=True)
        st.caption(f"{quote['quote_no']} / {quote['team_name']} / {quote['pickup_date']} ~ {quote['return_date']}")
        search = st.text_input("상품 검색", key=f"dialog_add_search_{quote_id}")
        page_size = 9
        page_key = f"dialog_add_page_{quote_id}"
        if page_key not in st.session_state:
            st.session_state[page_key] = 1
        reset_page_when_search_changes(search, page_key, f"dialog_add_search_tracker_{quote_id}")
        page_df, total_count = load_products_page(search_text=search, page=st.session_state[page_key], page_size=page_size)
        existing = set(load_quote_items_df(quote_id)["product_no"].astype(str).tolist())
        product_nos = tuple(page_df["product_no"].astype(str).tolist()) if not page_df.empty else tuple()
        status_map = bulk_product_status(product_nos, str(quote["pickup_date"]), str(quote["return_date"]), int(quote_id))
        cols = st.columns(3)
        for i, (_, row) in enumerate(page_df.iterrows()):
            product_no = str(row["product_no"])
            already = product_no in existing
            with cols[i % 3]:
                clicked, _, _, _ = render_product_tile(
                    row,
                    key_prefix=f"dialog_{quote_id}_{st.session_state[page_key]}_{i}",
                    pickup_date=quote["pickup_date"],
                    return_date=quote["return_date"],
                    selected=already,
                    select_label="이미 추가됨" if already else "추가",
                    exclude_quote_id=quote_id,
                    show_select=True,
                    status_info=status_map.get(product_no, {"reserved": 0, "pending": 0}),
                )
                if clicked and not already:
                    ok, msg = add_item_to_quote(quote_id, product_no)
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)
        pagination_controls(total_count, page_size, page_key)
        st.markdown("</div>", unsafe_allow_html=True)

    @st.dialog("견적 기본 정보 수정")
    def quote_header_dialog(quote_id):
        quote = get_quote(quote_id)
        if not quote:
            st.error("견적서를 찾지 못했습니다.")
            return
        team_value, person_value = split_team_person(quote.get("team_name", ""))
        st.caption(f"{quote['quote_no']} / 현재 저장명: {quote['team_name']}")
        c1, c2 = st.columns(2)
        with c1:
            edit_team = st.text_input("팀", value=team_value, key=f"header_dialog_team_{quote_id}")
        with c2:
            edit_person = st.text_input("사람", value=person_value, key=f"header_dialog_person_{quote_id}")
        c3, c4 = st.columns(2)
        with c3:
            edit_pickup = st.text_input("픽업 날짜", value=str(quote["pickup_date"]), help=date_input_help(), key=f"header_dialog_pickup_{quote_id}")
        with c4:
            edit_return = st.text_input("반납 날짜", value=str(quote["return_date"]), help=date_input_help(), key=f"header_dialog_return_{quote_id}")
        if st.button("수정 저장", type="primary", use_container_width=True, key=f"header_dialog_save_{quote_id}"):
            if not edit_team.strip() or not edit_person.strip():
                st.error("팀과 사람 이름을 모두 입력해야 합니다.")
            else:
                try:
                    pickup = normalize_date_text(edit_pickup)
                    ret = normalize_date_text(edit_return)
                except Exception as e:
                    st.error(str(e))
                    return
                ok, messages = update_quote_header(quote_id, combine_team_person(edit_team, edit_person), pickup, ret)
                if ok:
                    st.success("견적 정보를 수정했습니다.")
                    for message in messages:
                        if message:
                            st.info(message)
                    st.rerun()
                else:
                    st.error("수정할 수 없습니다.")
                    for f in messages:
                        st.write(f"- {f}")

    @st.dialog("대여 날짜 수정")
    def rental_date_dialog(quote_id):
        quote = get_quote(quote_id)
        if not quote:
            st.error("대여 기록을 찾지 못했습니다.")
            return
        st.caption(f"{quote['quote_no']} / {quote['team_name']}")
        c1, c2 = st.columns(2)
        with c1:
            new_pickup = st.text_input("새 픽업 날짜", value=str(quote["pickup_date"]), help=date_input_help(), key=f"date_dialog_pickup_{quote_id}")
        with c2:
            new_return = st.text_input("새 반납 날짜", value=str(quote["return_date"]), help=date_input_help(), key=f"date_dialog_return_{quote_id}")
        if st.button("날짜 수정 저장", type="primary", use_container_width=True, key=f"date_dialog_save_{quote_id}"):
            ok, failures = update_quote_dates(int(quote_id), new_pickup, new_return)
            if ok:
                st.success("대여 날짜를 수정했습니다.")
                st.rerun()
            else:
                st.error("날짜를 수정할 수 없습니다.")
                for f in failures:
                    st.write(f"- {f}")

    @st.dialog("상품 대여 날짜 수정")
    def rental_item_date_dialog(rental_id):
        rental = get_rental_item(rental_id)
        if not rental:
            st.error("대여 상품을 찾지 못했습니다.")
            return
        st.caption(f"{rental['quote_no']} / {rental['team_name']} / {rental['product_name']}")
        if rental.get("status") == "반납완료":
            st.info("이미 반납완료된 상품은 날짜 수정이 필요하지 않습니다.")
            return
        c1, c2 = st.columns(2)
        with c1:
            new_pickup = st.text_input("새 픽업 날짜", value=str(rental["pickup_date"]), help=date_input_help(), key=f"rental_item_pickup_{rental_id}")
        with c2:
            new_return = st.text_input("새 반납 날짜", value=str(rental["return_date"]), help=date_input_help(), key=f"rental_item_return_{rental_id}")
        if st.button("날짜 수정 저장", type="primary", use_container_width=True, key=f"rental_item_date_save_{rental_id}"):
            ok, failures = update_rental_item_dates(int(rental_id), new_pickup, new_return)
            if ok:
                st.success("상품 대여 날짜를 수정했습니다.")
                st.rerun()
            else:
                st.error("날짜를 수정할 수 없습니다.")
                for f in failures:
                    st.write(f"- {f}")

    @st.dialog("견적 상품 날짜 수정")
    def quote_item_date_dialog(item_id, quote_id):
        quote = get_quote(quote_id)
        items = load_quote_items_df(quote_id)
        target = items[items["id"].astype(str) == str(item_id)] if not items.empty else pd.DataFrame()
        if not quote or target.empty:
            st.error("견적 상품을 찾지 못했습니다.")
            return
        item = target.iloc[0]
        pickup, ret = get_item_dates(item, quote)
        st.caption(f"{quote['quote_no']} / {item['product_name']}")
        c1, c2 = st.columns(2)
        with c1:
            new_pickup = st.text_input("새 픽업 날짜", value=str(pickup), help=date_input_help(), key=f"quote_item_pickup_{item_id}")
        with c2:
            new_return = st.text_input("새 반납 날짜", value=str(ret), help=date_input_help(), key=f"quote_item_return_{item_id}")
        if st.button("날짜 수정 저장", type="primary", use_container_width=True, key=f"quote_item_date_save_{item_id}"):
            ok, messages = update_quote_item_dates(int(item_id), int(quote_id), new_pickup, new_return)
            if ok:
                for msg in messages:
                    if msg:
                        st.info(msg)
                st.success("견적 상품 날짜와 금액을 수정했습니다.")
                st.rerun()
            else:
                st.error("날짜를 수정할 수 없습니다.")
                for msg in messages:
                    st.write(f"- {msg}")
else:
    availability_detail_dialog = None
    quote_add_product_dialog = None
    rental_date_dialog = None
    rental_item_date_dialog = None
    quote_item_date_dialog = None
    quote_header_dialog = None


# -----------------------------
# 페이지: 제품 조회
# -----------------------------

def page_products():
    st.header("제품 조회")

    search = st.text_input("상품명 / 상품번호 / 사이즈 / 카테고리 검색", key="product_search")
    page_size = st.selectbox("한 페이지당 표시 개수", [20, 30, 40, 60], index=1, key="product_page_size")

    if "product_page" not in st.session_state:
        st.session_state["product_page"] = 1
    reset_page_when_search_changes(search, "product_page", "product_search_tracker")

    page_df, total_count = load_products_page(search_text=search, page=st.session_state["product_page"], page_size=page_size)
    total_pages = max(1, math.ceil(total_count / page_size))
    if st.session_state["product_page"] > total_pages:
        st.session_state["product_page"] = total_pages
        page_df, total_count = load_products_page(search_text=search, page=st.session_state["product_page"], page_size=page_size)

    st.metric("검색 결과" if search else "총 저장 제품 수", f"{total_count:,}개")

    product_nos = tuple(page_df["product_no"].astype(str).tolist()) if not page_df.empty else tuple()
    status_map = bulk_product_status(product_nos, "", "", 0)

    cols = st.columns(4)
    for i, (_, row) in enumerate(page_df.iterrows()):
        product_no = str(row.get("product_no", ""))
        with cols[i % 4]:
            render_product_tile(
                row,
                key_prefix=f"product_{st.session_state['product_page']}_{i}_{product_no}",
                show_select=False,
                status_info=status_map.get(product_no, {"reserved": 0, "pending": 0}),
            )

    pagination_controls(total_count, page_size, "product_page")


# -----------------------------
# 페이지: 견적서 만들기
# -----------------------------

def init_current_quote_state():
    if "selected_quote_items" not in st.session_state:
        st.session_state["selected_quote_items"] = {}


def add_to_current_selection(row):
    product_no = str(row["product_no"])
    if product_no not in st.session_state["selected_quote_items"]:
        st.session_state["selected_quote_items"][product_no] = {
            "product_no": product_no,
            "name": row.get("name", ""),
            "size_text": row.get("size_text", ""),
            "thumbnail_path": row.get("local_thumbnail_path", ""),
            "thumbnail_url": row.get("thumbnail_url", ""),
            "quantity": 1,
            "unit_price": safe_int(row.get("price", 0), 0),
            "stock_qty": max(safe_int(row.get("qty", 1), 1), 1),
        }



# -----------------------------
# 이미지 OCR 견적 불러오기
# -----------------------------

def ocr_clean_line(line):
    return clean_text(str(line or "").replace("｜", "|").replace("—", "-").replace("–", "-"))


def ocr_normalize_product_name(name):
    """OCR/DB 상품명을 비교하기 위한 정규화."""
    text = str(name or "").lower()
    text = text.replace("p.b", "").replace("p b", "").replace("pb", "")
    text = text.replace("p. b", "").replace("p . b", "")
    text = text.replace("０", "0").replace("Ｏ", "0").replace("o", "0")
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"[^0-9a-z가-힣]+", "", text)
    return text.strip()


@st.cache_data(ttl=600, show_spinner=False)
def load_ocr_product_candidates():
    rows = table_all("products", select="product_no,name,size_text,qty,price,thumbnail_url,local_thumbnail_path")
    candidates = []
    for r in rows:
        name = str(r.get("name", "") or "").strip()
        if not name:
            continue
        norm = ocr_normalize_product_name(name)
        if not norm:
            continue
        candidates.append({"product_no": str(r.get("product_no", "")), "name": name, "norm": norm, "row": r})
    return candidates


def _pil_resize_for_ocr(img, max_w=2400):
    img = img.convert("RGB")
    if img.width > max_w:
        ratio = max_w / img.width
        img = img.resize((max_w, int(img.height * ratio)), Image.Resampling.LANCZOS)
    return img


def preprocess_image_for_ocr(image_bytes):
    """전체 이미지 OCR용 전처리. 날짜 인식에 사용한다."""
    img = _pil_resize_for_ocr(Image.open(BytesIO(image_bytes)), max_w=2400)
    if cv2 is None or np is None:
        return img
    arr = np.array(img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    gray = cv2.resize(gray, None, fx=1.8, fy=1.8, interpolation=cv2.INTER_CUBIC)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    gray = cv2.convertScaleAbs(gray, alpha=1.35, beta=6)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return Image.fromarray(th)


def preprocess_crop_for_ocr(pil_img, scale=4.0):
    """상품 카드 하단 텍스트 OCR용 전처리."""
    img = pil_img.convert("RGB")
    if cv2 is None or np is None:
        return img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)
    arr = np.array(img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    # 작은 글자를 살리기 위해 배경 노이즈를 줄이고 대비를 올린다.
    gray = cv2.bilateralFilter(gray, 5, 55, 55)
    gray = cv2.convertScaleAbs(gray, alpha=1.55, beta=8)
    try:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
    except Exception:
        pass
    # 너무 강한 threshold는 한글 획을 날릴 수 있어서 adaptive + otsu 중 더 글자가 많은 쪽을 사용한다.
    th1 = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11)
    _, th2 = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # 검은 픽셀 수가 너무 적으면 adaptive, 아니면 otsu
    black1 = int((th1 < 128).sum())
    black2 = int((th2 < 128).sum())
    th = th1 if black1 > black2 * 1.25 else th2
    return Image.fromarray(th)


def run_ocr_from_image_bytes(image_bytes):
    """전체 이미지 OCR. 날짜와 보조 텍스트 인식용."""
    if pytesseract is None:
        raise RuntimeError("pytesseract 패키지가 설치되어 있지 않습니다. requirements.txt와 packages.txt를 배포에 반영해 주세요.")
    img = preprocess_image_for_ocr(image_bytes)
    texts = []
    for config in ["--oem 3 --psm 6", "--oem 3 --psm 11"]:
        try:
            texts.append(pytesseract.image_to_string(img, lang="kor+eng", config=config))
        except Exception:
            texts.append(pytesseract.image_to_string(img, lang="eng", config=config))
    return "\n".join(t for t in texts if t)


def _merge_boxes(boxes, iou_threshold=0.22):
    """비슷한 카드 박스를 병합한다."""
    if not boxes:
        return []
    boxes = sorted(boxes, key=lambda b: b[2] * b[3], reverse=True)
    kept = []
    for x, y, w, h in boxes:
        area = w * h
        duplicate = False
        for kx, ky, kw, kh in kept:
            ix1, iy1 = max(x, kx), max(y, ky)
            ix2, iy2 = min(x + w, kx + kw), min(y + h, ky + kh)
            iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
            inter = iw * ih
            union = area + kw * kh - inter
            if union and inter / union > iou_threshold:
                duplicate = True
                break
        if not duplicate:
            kept.append((x, y, w, h))
    # 위에서 아래, 왼쪽에서 오른쪽
    kept = sorted(kept, key=lambda b: (round(b[1] / 40) * 40, b[0]))
    return kept


def detect_product_card_boxes(image_bytes):
    """상품 카드 영역 감지.

    1차: 노란 테두리/밝은 카드 테두리 감지
    2차: 연한 배경 위의 큰 흰색/회색 상품 카드 영역 감지
    """
    if cv2 is None or np is None:
        return []
    pil = _pil_resize_for_ocr(Image.open(BytesIO(image_bytes)), max_w=2400)
    arr = np.array(pil)
    h_img, w_img = arr.shape[:2]
    boxes = []

    # 1) 노란 테두리 카드 감지: 카톡 이미지 예시처럼 상품 카드 테두리가 노란색일 때
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    yellow = cv2.inRange(hsv, np.array([18, 60, 120]), np.array([45, 255, 255]))
    yellow = cv2.morphologyEx(yellow, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8), iterations=2)
    contours, _ = cv2.findContours(yellow, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if w < 90 or h < 110:
            continue
        if w * h < 16000:
            continue
        if w > w_img * 0.95 or h > h_img * 0.95:
            continue
        # 테두리만 잡힐 수 있으므로 약간 확장
        pad = 8
        x0 = max(0, x - pad)
        y0 = max(0, y - pad)
        x1 = min(w_img, x + w + pad)
        y1 = min(h_img, y + h + pad)
        boxes.append((x0, y0, x1 - x0, y1 - y0))

    # 2) 밝은 사각 카드 감지: 노란 테두리가 없는 경우 보조
    if len(boxes) < 3:
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        # 배경과 카드 구분을 위해 가장자리 검출 후 닫기 연산
        edges = cv2.Canny(gray, 40, 120)
        edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=1)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            area = w * h
            if w < 90 or h < 110 or area < 18000:
                continue
            if w > w_img * 0.45 or h > h_img * 0.75:
                continue
            ratio = w / max(h, 1)
            if not (0.35 <= ratio <= 1.8):
                continue
            boxes.append((x, y, w, h))

    boxes = _merge_boxes(boxes)
    # 지나치게 큰 박스나 화면 헤더를 제거
    filtered = []
    for x, y, w, h in boxes:
        if y < h_img * 0.04 and h < h_img * 0.18:
            continue
        if w * h > w_img * h_img * 0.35:
            continue
        filtered.append((x, y, w, h))
    return filtered[:60]


def _ocr_pil_image(pil_img, config="--oem 3 --psm 6"):
    try:
        return pytesseract.image_to_string(pil_img, lang="kor+eng", config=config)
    except Exception:
        return pytesseract.image_to_string(pil_img, lang="eng", config=config)


def run_card_ocr_from_image_bytes(image_bytes):
    """상품 카드별 OCR.

    전체 이미지를 통째로 읽는 대신, 카드 영역을 먼저 찾은 뒤 상품명/사이즈가 있는 하단부를
    크게 확대해서 읽는다. OCR 결과에는 카드 번호를 붙여 디버깅이 가능하게 한다.
    """
    if pytesseract is None:
        raise RuntimeError("pytesseract 패키지가 설치되어 있지 않습니다.")
    pil = _pil_resize_for_ocr(Image.open(BytesIO(image_bytes)), max_w=2400)
    boxes = detect_product_card_boxes(image_bytes)
    texts = []
    for idx, (x, y, w, h) in enumerate(boxes, start=1):
        card = pil.crop((x, y, x + w, y + h))
        # 상품명은 보통 카드 하단 35~45%에 있음. 수량은 상단에 있을 수 있어 전체도 조금 읽는다.
        text_crop = card.crop((0, int(card.height * 0.52), card.width, card.height))
        crop_img = preprocess_crop_for_ocr(text_crop, scale=4.2)
        txt1 = _ocr_pil_image(crop_img, config="--oem 3 --psm 6")
        # 카드 전체 OCR은 보조로만. 너무 느려지지 않도록 작은 수량일 때만 실행.
        txt2 = ""
        if len(boxes) <= 25:
            whole = preprocess_crop_for_ocr(card, scale=2.3)
            txt2 = _ocr_pil_image(whole, config="--oem 3 --psm 11")
        block = f"[CARD {idx}]\n{txt1}\n{txt2}"
        texts.append(block)
    return "\n".join(texts), len(boxes)


def run_ocr_analysis_from_image_bytes(image_bytes):
    """OCR v2: 날짜는 전체 이미지에서, 상품은 카드별 OCR에서 우선 추출."""
    full_text = run_ocr_from_image_bytes(image_bytes)
    card_text, card_count = run_card_ocr_from_image_bytes(image_bytes)
    combined = f"{full_text}\n\n--- CARD OCR ({card_count}) ---\n{card_text}".strip()
    return combined, full_text, card_text, card_count



# -----------------------------
# PRAVI 요청서 PNG 구조화 데이터 읽기
# -----------------------------

def normalize_filename_text(value):
    try:
        return unicodedata.normalize("NFC", str(value or ""))
    except Exception:
        return str(value or "")


def compact_request_date(value):
    text = re.sub(r"[^0-9]", "", str(value or ""))
    if len(text) == 8:
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return ""


def split_request_team_person(text):
    """Cafe24 요청서의 '팀명/담당자' 한 줄을 앱의 팀/사람 입력으로 나눈다."""
    t = clean_text(normalize_filename_text(text)).replace("_", " ").strip()
    if not t:
        return "", ""
    # 추천 입력값: 프라비-홍길동
    if "-" in t:
        left, right = t.split("-", 1)
        return clean_text(left), clean_text(right)
    # 예전 입력값: 에이플랜 김민주
    parts = t.split()
    if len(parts) >= 2:
        return clean_text(" ".join(parts[:-1])), clean_text(parts[-1])
    return t, ""


def parse_pravi_request_filename(filename):
    """프라비_요청서_프라비-홍길동_20260622-20260627.png 같은 파일명에서 정보 추출."""
    name = normalize_filename_text(filename)
    base = os.path.basename(name)
    base = re.sub(r"\.[A-Za-z0-9]+$", "", base)
    out = {"team_text": "", "team": "", "person": "", "pickup_date": "", "return_date": ""}
    m = re.search(r"요청서[_\s-]+(.+?)[_\s]+(\d{8})\s*[-~]\s*(\d{8})", base)
    if not m:
        return out
    out["team_text"] = clean_text(m.group(1))
    out["team"], out["person"] = split_request_team_person(out["team_text"])
    out["pickup_date"] = compact_request_date(m.group(2))
    out["return_date"] = compact_request_date(m.group(3))
    return out


def preprocess_order_data_crop_for_ocr(image_bytes):
    """요청서 하단 PRAVI_ORDER_DATA 블록 OCR 전용 crop."""
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    # 요청서 포맷은 하단 35~45%에 구조화 텍스트가 들어간다.
    crop = img.crop((0, int(h * 0.56), w, h))
    if cv2 is None or np is None:
        return crop
    arr = np.array(crop)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    gray = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.convertScaleAbs(gray, alpha=1.45, beta=8)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return Image.fromarray(th)


def run_pravi_order_data_ocr_from_image_bytes(image_bytes):
    """Cafe24에서 생성한 요청서 PNG의 하단 데이터 블록만 OCR한다."""
    if pytesseract is None:
        return ""
    img = preprocess_order_data_crop_for_ocr(image_bytes)
    texts = []
    for lang in ["kor+eng", "eng"]:
        try:
            txt = pytesseract.image_to_string(
                img,
                lang=lang,
                config="--oem 3 --psm 6 -c preserve_interword_spaces=1",
            )
            if txt:
                texts.append(txt)
        except Exception:
            pass
    return "\n".join(texts)


def parse_pravi_order_data_text(text, filename=""):
    """PRAVI_ORDER_DATA_START/END 블록을 우선 파싱한다.

    상품은 상품번호(product_no)를 1순위로 사용하므로 OCR이 상품명을 조금 틀려도 안정적으로 매칭된다.
    """
    filename_info = parse_pravi_request_filename(filename)
    out = {
        "is_pravi_request": False,
        "team_text": filename_info.get("team_text", ""),
        "team": filename_info.get("team", ""),
        "person": filename_info.get("person", ""),
        "pickup_date": filename_info.get("pickup_date", ""),
        "return_date": filename_info.get("return_date", ""),
        "items": [],
        "raw_data_text": str(text or ""),
    }
    raw = normalize_filename_text(text)
    if "PRAVI_ORDER_DATA" in raw.upper():
        out["is_pravi_request"] = True

    lines = [ocr_clean_line(x) for x in raw.splitlines()]
    lines = [x for x in lines if x]

    for line in lines:
        u = line.upper().replace(" ", "")
        # TEAM OCR은 한글이 깨질 수 있으므로 파일명 정보가 있으면 파일명을 우선한다.
        m = re.search(r"TEAM\s*[=:]\s*(.+)$", line, re.IGNORECASE)
        if m and not out["team_text"]:
            team_text = clean_text(m.group(1))
            # 너무 깨진 OCR 값은 무시
            if len(re.sub(r"[^가-힣A-Za-z0-9\-\s]", "", team_text)) >= max(2, len(team_text) * 0.45):
                out["team_text"] = team_text
                out["team"], out["person"] = split_request_team_person(team_text)

        m = re.search(r"PICKUP\s*[=:]\s*(\d{4}\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{1,2}|\d{8})", line, re.IGNORECASE)
        if m:
            d = compact_request_date(m.group(1)) or clean_text(m.group(1)).replace("/", "-").replace(".", "-").replace(" ", "")
            if d:
                out["pickup_date"] = d
        m = re.search(r"RETURN\s*[=:]\s*(\d{4}\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{1,2}|\d{8})", line, re.IGNORECASE)
        if m:
            d = compact_request_date(m.group(1)) or clean_text(m.group(1)).replace("/", "-").replace(".", "-").replace(" ", "")
            if d:
                out["return_date"] = d

        # ITEM=7916 | 1 | P.B - 소파 102 | W2080 D730 H700
        if "ITEM" in u or re.match(r"^\d{3,6}\s*[|Il]", line):
            item_line = line
            item_line = re.sub(r"^.*?ITEM\s*[=:]\s*", "", item_line, flags=re.IGNORECASE)
            # OCR에서 | 를 I/l/ㅣ로 읽는 경우가 있어 강제 보정
            item_line = item_line.replace("｜", "|").replace("ㅣ", "|")
            # product_no, qty 뒤쪽은 넉넉하게 캡처
            m_item = re.match(r"\s*(\d{3,6})\s*[|Il]\s*(\d{1,3})\s*[|Il]\s*(.*)$", item_line)
            if not m_item:
                continue
            product_no = str(int(m_item.group(1))) if m_item.group(1).isdigit() else m_item.group(1).strip()
            qty = max(1, safe_int(m_item.group(2), 1))
            rest = clean_text(m_item.group(3))
            parts = [clean_text(x) for x in re.split(r"\s*[|]\s*", rest) if clean_text(x)]
            name = parts[0] if parts else ""
            size_text = parts[1] if len(parts) > 1 else ""
            out["items"].append({
                "product_no": product_no,
                "quantity": qty,
                "ocr_name": name,
                "size_text": size_text,
                "source_line": line,
            })

    # 중복 product_no는 수량 합산
    merged = {}
    for item in out["items"]:
        pno = str(item.get("product_no", ""))
        if not pno:
            continue
        if pno not in merged:
            merged[pno] = dict(item)
        else:
            merged[pno]["quantity"] = safe_int(merged[pno].get("quantity", 1), 1) + safe_int(item.get("quantity", 1), 1)
    out["items"] = list(merged.values())
    if out["items"]:
        out["is_pravi_request"] = True
    return out


def matches_from_pravi_order_data(order_data):
    """PRAVI_ORDER_DATA 상품번호를 DB 상품으로 직접 매칭한다."""
    matches = []
    for idx, item in enumerate(order_data.get("items", []), start=1):
        pno = str(item.get("product_no", "")).strip()
        product = get_product(pno) if pno else None
        qty = max(1, safe_int(item.get("quantity", 1), 1))
        if product:
            stock_qty = max(1, safe_int(product.get("qty", 1), 1))
            matches.append({
                "ocr_name": item.get("ocr_name") or product.get("name", ""),
                "quantity": min(qty, stock_qty),
                "source_line": item.get("source_line", ""),
                "card_index": f"요청{idx}",
                "status": "요청서 매칭 완료",
                "score": 100,
                "product_no": pno,
                "product_name": product.get("name", ""),
                "product_row": product,
                "auto_select": True,
                "match_note": "PRAVI_ORDER_DATA 상품번호 직접 매칭",
            })
        else:
            matches.append({
                "ocr_name": item.get("ocr_name", ""),
                "quantity": qty,
                "source_line": item.get("source_line", ""),
                "card_index": f"요청{idx}",
                "status": "상품번호 미등록",
                "score": 0,
                "product_no": pno,
                "product_name": "",
                "product_row": {},
                "auto_select": False,
                "match_note": "요청서에는 있으나 DB products에서 찾지 못함",
            })
    return matches


def parse_ocr_dates(text, base_year=None, base_month=None):
    base_year = int(base_year or datetime.now().year)
    base_month = int(base_month or datetime.now().month)
    t = clean_text(text)
    m = re.search(r"(\d{1,2})\s*[/\.\-]\s*(\d{1,2})\s*(?:픽업)?\s*[-~]\s*(\d{1,2})\s*[/\.\-]\s*(\d{1,2})\s*(?:반납)?", t)
    if m:
        pm, pd, rm, rd = map(int, m.groups())
        return f"{base_year:04d}-{pm:02d}-{pd:02d}", f"{base_year:04d}-{rm:02d}-{rd:02d}"
    m = re.search(r"픽\s*업\s*(\d{1,2})\s*일?\s*[-~]\s*반\s*납\s*(\d{1,2})\s*일?", t)
    if m:
        pd, rd = map(int, m.groups())
        return f"{base_year:04d}-{base_month:02d}-{pd:02d}", f"{base_year:04d}-{base_month:02d}-{rd:02d}"
    m = re.search(r"픽\s*업\s*(\d{1,2})\s*[/\.\-]\s*(\d{1,2}).*?반\s*납\s*(\d{1,2})\s*[/\.\-]\s*(\d{1,2})", t)
    if m:
        pm, pd, rm, rd = map(int, m.groups())
        return f"{base_year:04d}-{pm:02d}-{pd:02d}", f"{base_year:04d}-{rm:02d}-{rd:02d}"
    return "", ""


def ocr_extract_quantity_from_line(line):
    """OCR 라인에서 빨간 수량처럼 보이는 앞쪽 숫자를 가져온다. 실패하면 1."""
    text = clean_text(line)
    # 2개, 1.5, 0.8 등. 현재 견적 수량은 정수 기반이라 소수는 올림 처리한다.
    m = re.match(r"^\s*(\d+(?:[\.,]\d+)?)\s*(?:개|ea|EA)?\b", text)
    if not m:
        return 1
    try:
        v = float(m.group(1).replace(",", "."))
        return max(1, int(math.ceil(v)))
    except Exception:
        return 1



def ocr_candidate_line_is_noise(line):
    t = clean_text(line)
    if not t:
        return True
    noise_words = [
        "픽업", "반납", "견적", "프라비", "총금액", "공급", "세액", "CARD OCR", "감지", "선택",
        "사이즈", "상세페이지", "참고", "SET", "입니다", "220V", "LED", "일반", "서리악",
    ]
    if any(x.lower() in t.lower() for x in noise_words):
        return True
    # 숫자/기호만 있는 경우 제거
    if re.fullmatch(r"[0-9\s./,()\-]+", t):
        return True
    # 사이즈 라인만 있는 경우 제거
    if re.search(r"\b[Ww]\s*\d+", t) and not re.search(r"[가-힣]", t.replace("W", "").replace("w", "")):
        return True
    if re.search(r"\b[HhDd]\s*\d+", t) and len(t) < 28 and not re.search(r"[가-힣]", t):
        return True
    # 너무 짧고 숫자 없는 잡음
    if len(t) <= 2 and not re.search(r"\d", t):
        return True
    return False


def cleanup_ocr_product_tail(text):
    tail = clean_text(text)
    tail = tail.replace("—", "-").replace("–", "-").replace("－", "-")
    tail = tail.replace("ㅣ", "1").replace("｜", "|")
    # 수량/사이즈/설명 쪽 잘라내기
    tail = re.sub(r"\s{2,}.*$", "", tail).strip()
    tail = re.sub(r"(?:[Ww]\s*\d+|[Dd]\s*\d+|[Hh]\s*\d+|Ø\s*\d+|ø\s*\d+).*$", "", tail).strip()
    tail = re.sub(r"(?:사이즈|상세|참고|SET|상품|입니다|220V|LED).*$", "", tail, flags=re.IGNORECASE).strip()
    tail = re.sub(r"^[\-_:·•\s]+", "", tail).strip()
    tail = re.sub(r"[\|\\]+", "", tail).strip()
    # OCR이 상품명 뒤에 붙인 잔여 괄호나 점 제거
    tail = re.sub(r"[.,;:]+$", "", tail).strip()
    return tail


def _ocr_split_card_blocks(text):
    """run_card_ocr_from_image_bytes가 만든 [CARD n] 블록을 카드별로 나눈다."""
    blocks = []
    current_idx = None
    current_lines = []
    for raw in str(text or "").splitlines():
        line = ocr_clean_line(raw)
        m = re.match(r"^\[CARD\s+(\d+)\]", line, re.IGNORECASE)
        if m:
            if current_idx is not None:
                blocks.append({"card_index": current_idx, "text": "\n".join(current_lines)})
            current_idx = int(m.group(1))
            current_lines = []
        else:
            if current_idx is not None:
                current_lines.append(line)
    if current_idx is not None:
        blocks.append({"card_index": current_idx, "text": "\n".join(current_lines)})
    return blocks


def _make_scan_lines_for_card(block_text):
    lines = [ocr_clean_line(x) for x in str(block_text or "").splitlines()]
    lines = [x for x in lines if x]
    scan_lines = []
    for i, line in enumerate(lines):
        scan_lines.append(line)
        if i + 1 < len(lines):
            scan_lines.append(line + " " + lines[i + 1])
        if i + 2 < len(lines):
            scan_lines.append(line + " " + lines[i + 1] + " " + lines[i + 2])
    return scan_lines


def _extract_candidate_names_from_line(line):
    """한 줄에서 상품명 후보를 뽑는다. 한 줄에 여러 상품명이 섞인 경우도 분리한다."""
    out = []
    line = ocr_clean_line(line)
    if ocr_candidate_line_is_noise(line):
        return out

    # 흔한 OCR 교정
    fixed = line.replace("P8", "P.B").replace("P, B", "P.B").replace("P . B", "P.B")
    fixed = fixed.replace("P.B_", "P.B - ").replace("P.B:", "P.B - ")
    fixed = re.sub(r"\bP\s*[.,]?\s*B\b", "P.B", fixed, flags=re.IGNORECASE)

    # 1) P.B 패턴이 있으면 그 뒤를 상품명으로 추출. 여러 개가 붙으면 다음 P.B 전까지만.
    pb_matches = list(re.finditer(r"P\.B\s*[-_－–—]?\s*", fixed, re.IGNORECASE))
    if pb_matches:
        for idx, m in enumerate(pb_matches):
            start = m.end()
            end = pb_matches[idx + 1].start() if idx + 1 < len(pb_matches) else len(fixed)
            tail = cleanup_ocr_product_tail(fixed[start:end])
            # 너무 길면 상품명 끝 숫자 뒤까지만 사용
            m2 = re.search(r"(.{0,28}?[가-힣A-Za-z]+\s*\d+(?:\s*\([^)]*\))?)", tail)
            if m2:
                tail = cleanup_ocr_product_tail(m2.group(1))
            if tail and re.search(r"[가-힣A-Za-z]", tail) and re.search(r"\d", tail):
                out.append("P.B - " + tail)
        return out

    # 2) P.B가 빠진 경우: 한글/영문 + 숫자로 끝나는 짧은 조각만 후보로 사용
    if not (re.search(r"[가-힣A-Za-z]", fixed) and re.search(r"\d", fixed)):
        return out

    # 앞쪽 빨간 수량 제거
    fixed = re.sub(r"^\s*\d+(?:[\.,]\d+)?\s*(?:개|ea|EA)?\s*", "", fixed).strip()
    fixed = cleanup_ocr_product_tail(fixed)
    if ocr_candidate_line_is_noise(fixed):
        return out

    # 한 줄에 후보가 여러 개 섞인 경우 숫자 끝 기준으로 조각화
    parts = re.findall(r"[가-힣A-Za-z][가-힣A-Za-z0-9\s/().+\-]{0,24}?\d+(?:\s*\([^)]*\))?", fixed)
    if not parts:
        parts = [fixed]
    for p in parts:
        p = cleanup_ocr_product_tail(p)
        p = re.sub(r"^[^가-힣A-Za-z]+", "", p).strip()
        if not p or ocr_candidate_line_is_noise(p):
            continue
        if re.search(r"[가-힣A-Za-z]", p) and re.search(r"\d", p):
            out.append(p)
    return out


def parse_ocr_product_lines(text):
    """구버전 호환용. 전체 OCR에서 후보를 추출하되 자동선택에는 쓰지 않는다."""
    raw_lines = [ocr_clean_line(x) for x in str(text or "").splitlines()]
    lines = [x for x in raw_lines if x]
    scan_lines = []
    for i, line in enumerate(lines):
        scan_lines.append(line)
        if i + 1 < len(lines):
            scan_lines.append(line + " " + lines[i + 1])
        if i + 2 < len(lines):
            scan_lines.append(line + " " + lines[i + 1] + " " + lines[i + 2])

    found = []
    seen = set()
    for line in scan_lines:
        qty = ocr_extract_quantity_from_line(line)
        for name in _extract_candidate_names_from_line(line):
            key = ocr_normalize_product_name(name)
            if key and key not in seen:
                seen.add(key)
                found.append({"ocr_name": name, "quantity": qty, "source_line": line, "card_index": ""})
    return found


def parse_ocr_product_cards(card_text):
    """카드 1개당 최종 후보 1개만 반환한다."""
    blocks = _ocr_split_card_blocks(card_text)
    found = []
    for block in blocks:
        card_idx = block["card_index"]
        candidates = []
        for line in _make_scan_lines_for_card(block["text"]):
            qty = ocr_extract_quantity_from_line(line)
            for name in _extract_candidate_names_from_line(line):
                # 카드 안의 제목은 보통 짧고 끝 숫자가 있음. 너무 긴 잡음은 제거.
                if len(name) > 38:
                    continue
                candidates.append({
                    "ocr_name": name,
                    "quantity": qty,
                    "source_line": line,
                    "card_index": card_idx,
                })
        # 한 카드에서 같은 정규화 후보는 하나만
        unique = []
        seen = set()
        for c in candidates:
            key = ocr_normalize_product_name(c["ocr_name"])
            if key and key not in seen:
                seen.add(key)
                unique.append(c)
        if unique:
            found.extend(unique)
        else:
            # 실패 카드도 결과에 남겨서 확인 가능하게 함
            sample = " / ".join([x for x in _make_scan_lines_for_card(block["text"])[:3] if x])
            found.append({
                "ocr_name": sample[:60] if sample else "",
                "quantity": 1,
                "source_line": sample,
                "card_index": card_idx,
                "force_fail": True,
            })
    return found


def ocr_trailing_number(name):
    m = re.search(r"(\d+)\s*(?:\([^)]*\))?\s*$", str(name or ""))
    return m.group(1) if m else ""


def _category_token_from_name(name):
    t = str(name or "")
    t = re.sub(r"P\s*\.?\s*B\s*[-_－–—]?", "", t, flags=re.IGNORECASE).strip()
    # 첫 숫자 앞까지를 카테고리 토큰으로 사용
    m = re.match(r"([가-힣A-Za-z]+)", t.replace(" ", ""))
    return m.group(1) if m else ""


def _match_one_ocr_name(ocr_name, db_candidates, exact, choices, by_tail):
    norm = ocr_normalize_product_name(ocr_name)
    tail = ocr_trailing_number(ocr_name)
    cat = _category_token_from_name(ocr_name)
    matched = None
    score = 0
    status = "매칭 실패"
    reason = ""

    if not norm or not tail:
        return None, 0, "매칭 실패", "상품명 또는 끝 번호 인식 실패"

    if norm in exact:
        return exact[norm], 100, "매칭 완료", "정확히 일치"

    if rf_process is None or not choices:
        return None, 0, "매칭 실패", "유사도 매칭 불가"

    # 끝 번호가 같은 후보군 안에서만 우선 매칭. 끝 번호가 다르면 오매칭이 많아서 자동 후보에서 제외.
    tail_candidates = by_tail.get(tail, []) if tail else []
    if tail_candidates:
        # 카테고리 토큰이 포함되는 후보를 우선
        scoped = tail_candidates
        if cat:
            cat_scoped = [c for c in tail_candidates if cat in _category_token_from_name(c.get("name", "")) or _category_token_from_name(c.get("name", "")) in cat]
            if cat_scoped:
                scoped = cat_scoped
        tail_choices = {c["norm"]: c for c in scoped}
        best = rf_process.extractOne(norm, list(tail_choices.keys()), scorer=rf_fuzz.WRatio)
        if best:
            best_key, score, _ = best
            if score >= 72:
                return tail_choices[best_key], int(score), "후보 매칭", "끝 번호 일치"
            return None, int(score), "매칭 실패", "끝 번호는 같지만 유사도 낮음"

    # 끝 번호가 다르면 아주 높은 유사도만 후보로 표시하되 자동선택은 하지 않음
    best = rf_process.extractOne(norm, list(choices.keys()), scorer=rf_fuzz.WRatio)
    if best:
        best_key, score, _ = best
        if score >= 92:
            return choices[best_key], int(score), "확인 필요", "끝 번호 불일치 가능성"
    return None, int(score or 0), "매칭 실패", "DB 후보 없음"


def match_ocr_products(candidates):
    """카드별 후보를 매칭하고, 카드당 1개/상품당 1개만 자동 선택되도록 정리한다."""
    db_candidates = load_ocr_product_candidates()
    exact = {c["norm"]: c for c in db_candidates}
    choices = {c["norm"]: c for c in db_candidates}

    by_tail = {}
    for c in db_candidates:
        tail = ocr_trailing_number(c.get("name", ""))
        if tail:
            by_tail.setdefault(tail, []).append(c)

    # 먼저 모든 후보 점수 계산
    enriched = []
    for item in candidates:
        item = dict(item)
        if item.get("force_fail"):
            item.update({
                "status": "매칭 실패", "score": 0, "product_no": "", "product_name": "", "product_row": {},
                "auto_select": False, "match_note": "카드 텍스트 인식 실패",
            })
            enriched.append(item)
            continue
        matched, score, status, reason = _match_one_ocr_name(item.get("ocr_name", ""), db_candidates, exact, choices, by_tail)
        qty = max(1, safe_int(item.get("quantity", 1), 1))
        auto_select = bool(matched) and status in ["매칭 완료", "후보 매칭"] and score >= 82 and qty <= 10
        if qty > 10:
            status = "확인 필요"
            reason = "수량 확인 필요"
            auto_select = False
        item.update({
            "status": status,
            "score": int(score or 0),
            "product_no": matched["product_no"] if matched else "",
            "product_name": matched["name"] if matched else "",
            "product_row": matched["row"] if matched else {},
            "auto_select": auto_select,
            "match_note": reason,
        })
        enriched.append(item)

    # 카드별 최종 1개만 남김: 매칭 완료 > 후보 매칭 > 확인 필요 > 실패, 점수 높은 순
    by_card = {}
    no_card = []
    rank = {"매칭 완료": 0, "후보 매칭": 1, "확인 필요": 2, "매칭 실패": 3}
    for item in enriched:
        ci = item.get("card_index")
        if ci in [None, ""]:
            no_card.append(item)
            continue
        by_card.setdefault(ci, []).append(item)

    final = []
    for ci, items in sorted(by_card.items(), key=lambda x: int(x[0])):
        items = sorted(items, key=lambda x: (rank.get(x.get("status"), 9), -safe_int(x.get("score", 0), 0)))
        chosen = items[0]
        chosen["candidate_count_in_card"] = len(items)
        if len(items) > 1 and chosen.get("status") != "매칭 실패":
            chosen["match_note"] = (chosen.get("match_note") or "") + f" / 카드 내 후보 {len(items)}개"
        final.append(chosen)

    # 카드 없는 fallback 후보는 자동 선택하지 않고 참고로만 추가
    for item in no_card:
        item["auto_select"] = False
        item["status"] = "확인 필요" if item.get("product_no") else "매칭 실패"
        item["match_note"] = (item.get("match_note") or "") + " / 전체 OCR 후보"
        final.append(item)

    # 같은 상품이 여러 카드에서 중복 매칭되면 첫 번째만 자동 선택
    seen_pno = set()
    cleaned = []
    for item in final:
        pno = str(item.get("product_no") or "")
        if pno:
            if pno in seen_pno:
                item["status"] = "중복 제외"
                item["auto_select"] = False
                item["match_note"] = "같은 상품이 이미 선택됨"
            else:
                seen_pno.add(pno)
        cleaned.append(item)
    return cleaned


def reset_ocr_import_state():
    """이미지 인식 섹션만 초기화한다."""
    prefixes = [
        "ocr_detected_team", "ocr_detected_person", "ocr_detected_pickup", "ocr_detected_return",
        "ocr_import_editor", "ocr_quote_image_upload_",
    ]
    exact_keys = [
        "ocr_import_result", "ocr_import_add_flash", "pending_create_quote_fields",
        "ocr_import_issue_rows", "ocr_import_issue_context",
    ]
    for key in exact_keys:
        st.session_state.pop(key, None)
    for key in list(st.session_state.keys()):
        if any(str(key).startswith(prefix) for prefix in prefixes):
            st.session_state.pop(key, None)
    st.session_state["ocr_import_upload_seq"] = safe_int(st.session_state.get("ocr_import_upload_seq", 0), 0) + 1
    st.session_state["ocr_import_result_id"] = safe_int(st.session_state.get("ocr_import_result_id", 0), 0) + 1


def set_ocr_import_result(result):
    """새 OCR 결과를 저장하고, 이전 결과 위젯 캐시를 분리한다."""
    seq = safe_int(st.session_state.get("ocr_import_result_id", 0), 0) + 1
    st.session_state["ocr_import_result_id"] = seq
    # 이전 결과 입력/테이블 상태가 다음 결과에 섞이지 않게 제거한다.
    for key in list(st.session_state.keys()):
        if str(key).startswith("ocr_detected_") or str(key).startswith("ocr_import_editor"):
            st.session_state.pop(key, None)
    result = dict(result or {})
    result["result_id"] = seq
    st.session_state["ocr_import_result"] = result


def build_ocr_import_issue_rows(rows_df, pickup_date, return_date):
    """이미지 인식 반영 상품 중 현재 날짜 기준 불가/견적중 상품을 찾는다."""
    if rows_df is None or rows_df.empty or not pickup_date or not return_date:
        return []
    selected = rows_df[rows_df["선택"].astype(bool)].copy()
    product_nos = [str(x).strip() for x in selected.get("상품번호", []).tolist() if str(x).strip()]
    if not product_nos:
        return []
    status_map = bulk_product_status(tuple(product_nos), str(pickup_date), str(return_date), 0)
    out = []
    seen = set()
    for _, r in selected.iterrows():
        pno = str(r.get("상품번호", "")).strip()
        if not pno or pno in seen:
            continue
        seen.add(pno)
        stt = status_map.get(pno, {})
        reserved = safe_int(stt.get("reserved", 0), 0)
        pending = safe_int(stt.get("pending", 0), 0)
        if reserved <= 0 and pending <= 0:
            continue
        state = "불가" if reserved > 0 else "견적중"
        out.append({
            "상태": state,
            "상품번호": pno,
            "상품명": str(r.get("매칭 상품명") or r.get("OCR 상품명") or ""),
            "요청수량": safe_int(r.get("수량", 1), 1),
            "확정/대여중": reserved,
            "견적중": pending,
        })
    return out


if hasattr(st, "dialog"):
    @st.dialog("이미지 인식 상품 상태 확인", width="large")
    def ocr_import_issues_dialog():
        rows = st.session_state.get("ocr_import_issue_rows") or []
        context = st.session_state.get("ocr_import_issue_context") or {}
        st.warning("반영한 상품 중 현재 날짜 기준으로 불가 또는 견적중인 상품이 있습니다.")
        if context.get("pickup_date") and context.get("return_date"):
            st.caption(f"확인 날짜: {context.get('pickup_date')} ~ {context.get('return_date')}")
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption("상품은 선택 목록에는 추가되어 있습니다. 견적 총액에서는 불가/견적중 상품이 제외되며, 필요하면 선택된 상품 영역에서 삭제하거나 수량을 조정하세요.")
        if st.button("확인", type="primary", use_container_width=True, key="ocr_issue_dialog_close"):
            st.session_state.pop("ocr_import_issue_rows", None)
            st.session_state.pop("ocr_import_issue_context", None)
            st.rerun()
else:
    ocr_import_issues_dialog = None

def render_ocr_import_panel(current_pickup="", current_return=""):
    with st.expander("이미지/요청서 PNG로 상품 불러오기", expanded=False):
        st.caption("Cafe24 요청서 PNG는 하단 PRAVI_ORDER_DATA를 우선 읽고, 일반 이미지는 기존 OCR 방식으로 읽습니다.")

        action_cols = st.columns([1, 1, 3])
        with action_cols[0]:
            if st.button("정보 초기화", use_container_width=True, key="ocr_import_reset_btn"):
                reset_ocr_import_state()
                st.rerun()

        upload_seq = safe_int(st.session_state.get("ocr_import_upload_seq", 0), 0)
        uploaded = st.file_uploader(
            "상품 선택 이미지 업로드",
            type=["png", "jpg", "jpeg", "webp"],
            key=f"ocr_quote_image_upload_{upload_seq}",
        )
        base_year = datetime.now().year
        base_month = datetime.now().month
        try:
            if current_pickup:
                dt = parse_date_text(current_pickup)
                base_year, base_month = dt.year, dt.month
        except Exception:
            pass
        c1, c2 = st.columns(2)
        with c1:
            base_year = st.number_input("월 정보가 없는 이미지의 기준 연도", min_value=2020, max_value=2035, value=base_year, step=1, key="ocr_base_year")
        with c2:
            base_month = st.number_input("월 정보가 없는 이미지의 기준 월", min_value=1, max_value=12, value=base_month, key="ocr_base_month")

        if uploaded is not None and st.button("이미지 인식 실행", type="primary", key="run_ocr_import"):
            try:
                image_bytes = uploaded.getvalue()
                upload_name = getattr(uploaded, "name", "")
                with st.spinner("이미지에서 요청서 데이터를 읽는 중입니다..."):
                    # 1순위: Cafe24 요청서 PNG 하단 PRAVI_ORDER_DATA 블록
                    order_text = run_pravi_order_data_ocr_from_image_bytes(image_bytes)
                    order_data = parse_pravi_order_data_text(order_text, filename=upload_name)

                    if order_data.get("items"):
                        matches = matches_from_pravi_order_data(order_data)
                        set_ocr_import_result({
                            "mode": "PRAVI 요청서",
                            "raw_text": order_text,
                            "full_text": order_text,
                            "card_text": "",
                            "card_count": len(order_data.get("items", [])),
                            "pickup_date": order_data.get("pickup_date", ""),
                            "return_date": order_data.get("return_date", ""),
                            "team": order_data.get("team", ""),
                            "person": order_data.get("person", ""),
                            "team_text": order_data.get("team_text", ""),
                            "matches": matches,
                        })
                    else:
                        # 2순위: 일반 이미지 OCR fallback
                        raw_text, full_text, card_text, card_count = run_ocr_analysis_from_image_bytes(image_bytes)
                        pickup_s, return_s = parse_ocr_dates(full_text or raw_text, base_year=base_year, base_month=base_month)
                        products = parse_ocr_product_cards(card_text)
                        if not products:
                            products = parse_ocr_product_lines(full_text)
                        matches = match_ocr_products(products)
                        set_ocr_import_result({
                            "mode": "일반 OCR",
                            "raw_text": raw_text,
                            "full_text": full_text,
                            "card_text": card_text,
                            "card_count": card_count,
                            "pickup_date": pickup_s,
                            "return_date": return_s,
                            "team": "",
                            "person": "",
                            "team_text": "",
                            "matches": matches,
                        })
            except Exception as e:
                st.error(f"이미지 인식 실패: {e}")

        result = st.session_state.get("ocr_import_result")
        if not result:
            return

        result_id = safe_int(result.get("result_id", st.session_state.get("ocr_import_result_id", 0)), 0)
        st.markdown("#### 인식 결과")
        mode = result.get("mode", "")
        if mode == "PRAVI 요청서":
            st.success(f"Cafe24 요청서 PNG를 인식했습니다. 상품 {len(result.get('matches', []))}개를 상품번호 기준으로 직접 매칭했습니다.")
        elif result.get("card_count") is not None:
            st.caption(f"감지된 상품 카드: {result.get('card_count', 0)}개 · 카드당 최종 후보 1개만 표시하고, 중복 상품은 자동 제외합니다.")

        top_cols = st.columns(4)
        with top_cols[0]:
            detected_team = st.text_input("인식된 팀", value=result.get("team", ""), key=f"ocr_detected_team_{result_id}")
        with top_cols[1]:
            detected_person = st.text_input("인식된 사람", value=result.get("person", ""), key=f"ocr_detected_person_{result_id}")
        with top_cols[2]:
            detected_pickup = st.text_input("인식된 픽업일", value=result.get("pickup_date", ""), key=f"ocr_detected_pickup_{result_id}")
        with top_cols[3]:
            detected_return = st.text_input("인식된 반납일", value=result.get("return_date", ""), key=f"ocr_detected_return_{result_id}")

        rows = []
        for m in result.get("matches", []):
            rows.append({
                "선택": bool(m.get("auto_select", False)),
                "구분": m.get("card_index", ""),
                "상태": m.get("status", ""),
                "OCR 상품명": m.get("ocr_name", ""),
                "매칭 상품명": m.get("product_name", ""),
                "상품번호": m.get("product_no", ""),
                "수량": int(max(1, safe_int(m.get("quantity", 1), 1))),
                "유사도": m.get("score", 0),
                "확인메모": m.get("match_note", ""),
            })
        if rows:
            edited = st.data_editor(
                pd.DataFrame(rows),
                hide_index=True,
                use_container_width=True,
                num_rows="fixed",
                column_config={
                    "선택": st.column_config.CheckboxColumn("선택"),
                    "수량": st.column_config.NumberColumn("수량", min_value=1, step=1),
                },
                key=f"ocr_import_editor_{result_id}",
            )
            col_add, col_text = st.columns([1, 1])
            with col_add:
                if st.button("인식 결과 반영하기", type="primary", use_container_width=True, key=f"ocr_add_to_quote_{result_id}"):
                    added = 0
                    for _, r in edited.iterrows():
                        if not bool(r.get("선택")) or not str(r.get("상품번호", "")).strip():
                            continue
                        p = get_product(str(r.get("상품번호")))
                        if not p:
                            continue
                        add_to_current_selection(p)
                        pno = str(p.get("product_no"))
                        qty = max(1, safe_int(r.get("수량", 1), 1))
                        stock_qty = max(1, safe_int(p.get("qty", 1), 1))
                        if qty > stock_qty:
                            qty = stock_qty
                        st.session_state["selected_quote_items"][pno]["quantity"] = qty
                        # 선택된 상품 영역의 + / - 수량 표시값도 같이 맞춘다.
                        st.session_state[f"create_qty_{pno}_value"] = qty
                        added += 1

                    # 날짜/팀명은 이미 생성된 text_input 값을 같은 실행 흐름에서 직접 바꿀 수 없어
                    # 다음 rerun 시작 시 위젯 생성 전에 강제 반영한다.
                    pending_fields = {}
                    if detected_team:
                        pending_fields["create_team_name"] = detected_team
                    if detected_person:
                        pending_fields["create_person_name"] = detected_person
                    if detected_pickup:
                        normalized = try_normalize_date_text(detected_pickup) or detected_pickup
                        pending_fields["create_pickup_text"] = normalized
                    if detected_return:
                        normalized = try_normalize_date_text(detected_return) or detected_return
                        pending_fields["create_return_text"] = normalized
                    if pending_fields:
                        st.session_state["pending_create_quote_fields"] = pending_fields

                    check_pickup = try_normalize_date_text(detected_pickup) or current_pickup or pending_fields.get("create_pickup_text", "")
                    check_return = try_normalize_date_text(detected_return) or current_return or pending_fields.get("create_return_text", "")
                    issues = build_ocr_import_issue_rows(edited, check_pickup, check_return)
                    if issues:
                        st.session_state["ocr_import_issue_rows"] = issues
                        st.session_state["ocr_import_issue_context"] = {"pickup_date": check_pickup, "return_date": check_return}

                    st.session_state["ocr_import_add_flash"] = f"{added}개 상품을 선택 목록에 반영했습니다."
                    st.rerun()
            with col_text:
                with st.expander("인식 원문 보기"):
                    st.text(result.get("raw_text", ""))
        else:
            st.warning("인식된 상품이 없습니다. 일반 캡처 이미지는 기존 OCR 방식으로 다시 시도하거나 수동 입력이 필요합니다.")


def page_quote_create():
    init_current_quote_state()
    st.header("견적서 만들기")
    st.caption("* 필수 입력")

    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([1, 1, 1.15, 1.15])
        with c1:
            team_name = st.text_input("팀", key="create_team_name", placeholder="팀 이름 입력")
        with c2:
            person_name = st.text_input("사람", key="create_person_name", placeholder="사람 이름 입력")
        with c3:
            pickup_raw = st.text_input("픽업 날짜", value="", help=date_input_help(), key="create_pickup_text", placeholder="픽업 날짜 입력")
        with c4:
            return_raw = st.text_input("반납 날짜", value="", help=date_input_help(), key="create_return_text", placeholder="반납 날짜 입력")

    pickup_date = try_normalize_date_text(pickup_raw)
    return_date = try_normalize_date_text(return_raw)
    date_error = ""
    if pickup_raw.strip() and not pickup_date:
        date_error = "픽업 날짜 형식을 확인하세요."
    elif return_raw.strip() and not return_date:
        date_error = "반납 날짜 형식을 확인하세요."
    elif pickup_date and return_date and return_date < pickup_date:
        date_error = "반납 날짜가 픽업 날짜보다 빠릅니다."
    if date_error:
        st.warning(date_error)
    elif pickup_date and return_date:
        st.caption(pricing_summary_text(pickup_date, return_date))

    search = st.text_input("상품 검색", key="create_product_search", placeholder="상품명 입력")
    st.subheader("상품 선택")
    try:
        st.caption(f"총 저장 상품 {count_products_fast(''):,}개")
    except Exception:
        pass

    page_size = 20
    if "create_product_page" not in st.session_state:
        st.session_state["create_product_page"] = 1
    reset_page_when_search_changes(search, "create_product_page", "create_product_search_tracker")
    page_df, total_count = load_products_page(search_text=search, page=st.session_state["create_product_page"], page_size=page_size)

    product_nos = tuple(page_df["product_no"].astype(str).tolist()) if not page_df.empty else tuple()
    status_map = bulk_product_status(product_nos, pickup_date or "", return_date or "", 0)

    cols = st.columns(4)
    for i, (_, row) in enumerate(page_df.iterrows()):
        product_no = str(row["product_no"])
        selected = product_no in st.session_state["selected_quote_items"]
        with cols[i % 4]:
            clicked, _, _, _ = render_product_tile(
                row,
                key_prefix=f"create_{st.session_state['create_product_page']}_{i}_{product_no}",
                pickup_date=pickup_date or None,
                return_date=return_date or None,
                selected=selected,
                select_label="선택 해제" if selected else "선택",
                show_select=True,
                status_info=status_map.get(product_no, {"reserved": 0, "pending": 0}),
            )
            if clicked:
                if selected:
                    st.session_state["selected_quote_items"].pop(product_no, None)
                    st.session_state.pop(f"create_qty_{product_no}_value", None)
                else:
                    add_to_current_selection(row)
                st.rerun()

    pagination_controls(total_count, page_size, "create_product_page")

    st.divider()
    st.subheader("선택된 상품")
    selected_items = st.session_state["selected_quote_items"]
    if not selected_items:
        st.info("선택된 상품이 없습니다.")
    else:
        with st.container(border=True):
            st.markdown("**견적 정보**")
            info_cols = st.columns(4)
            info_cols[0].caption("팀")
            info_cols[0].write(team_name or "-")
            info_cols[1].caption("사람")
            info_cols[1].write(person_name or "-")
            info_cols[2].caption("픽업 날짜")
            info_cols[2].write(pickup_date or "-")
            info_cols[3].caption("반납 날짜")
            info_cols[3].write(return_date or "-")
            st.caption("상단 입력값과 자동으로 동기화됩니다. 수정은 상단 입력란에서 해주세요.")
        selected_codes = tuple(str(x) for x in selected_items.keys())
        selected_status = bulk_product_status(selected_codes, pickup_date or "", return_date or "", 0)
        selected_product_map = load_products_map(selected_codes)
        remove_codes = []
        multiplier = quote_price_multiplier(pickup_date, return_date) if pickup_date and return_date else 1

        for product_no, item in list(selected_items.items()):
            product = selected_product_map.get(str(product_no)) or {}
            stock_qty = max(safe_int(item.get("stock_qty", product.get("qty", 1)), 1), 1)
            selected_items[product_no]["stock_qty"] = stock_qty
            with st.container(border=True):
                render_status_button_bar(
                    product_no,
                    pickup_date or None,
                    return_date or None,
                    key_prefix=f"selected_status_{product_no}",
                    precomputed=selected_status.get(product_no, {"reserved": 0, "pending": 0}),
                    total_qty_override=stock_qty,
                )
                cc1, cc2, cc3, cc4, cc5 = st.columns([1.1, 2.7, 1.4, 1.4, 0.8])
                with cc1:
                    show_thumb_from_values(item.get("thumbnail_path", ""), item.get("thumbnail_url", ""))
                with cc2:
                    st.markdown(f"**{item.get('name', '')}**")
                    st.caption(item.get("size_text", ""))
                    st.caption(f"상품번호: {product_no}")
                    st.caption(f"보유수량: {stock_qty}개")
                with cc3:
                    qty = render_quantity_stepper("수량", item.get("quantity", 1), stock_qty, f"create_qty_{product_no}")
                    selected_items[product_no]["quantity"] = qty
                with cc4:
                    price = st.number_input("단가", min_value=0, value=safe_int(item.get("unit_price", 0), 0), step=1000, key=f"create_price_{product_no}")
                    selected_items[product_no]["unit_price"] = price
                    st.caption(f"적용 {multiplier}배")
                    st.caption(f"금액: {money(qty * price * multiplier)}")
                with cc5:
                    if st.button("삭제", key=f"create_remove_{product_no}"):
                        remove_codes.append(product_no)

        for code in remove_codes:
            selected_items.pop(code, None)
            st.session_state.pop(f"create_qty_{code}_value", None)
            st.rerun()

        subtotal = sum(
            safe_int(v.get("quantity", 1), 1) * safe_int(v.get("unit_price", 0), 0) * multiplier
            for v in selected_items.values()
        )
        vat = int(round(subtotal * 0.1))
        total = subtotal + vat
        st.markdown(f"### 합계: 공급가 {money(subtotal)} / 부가세 {money(vat)} / 총금액 {money(total)}")

        memo = st.text_input("견적 메모", key="create_memo")
        c1, c2 = st.columns([1, 1])
        with c1:
            if st.button("견적서 만들기", type="primary", use_container_width=True):
                if not team_name.strip() or not person_name.strip():
                    st.error("팀과 사람 이름을 모두 입력해야 합니다.")
                elif date_error or not pickup_date or not return_date:
                    st.error(date_error or "픽업/반납 날짜를 입력해야 합니다.")
                else:
                    quote_id = create_quote(
                        combine_team_person(team_name, person_name),
                        pickup_date,
                        return_date,
                        selected_items,
                        memo,
                    )
                    st.session_state["selected_quote_items"] = {}
                    st.session_state["last_created_quote_id"] = quote_id
                    st.success("견적서를 저장했습니다.")
                    st.rerun()
        with c2:
            if st.button("선택 초기화", use_container_width=True):
                for code in list(st.session_state.get("selected_quote_items", {}).keys()):
                    st.session_state.pop(f"create_qty_{code}_value", None)
                st.session_state["selected_quote_items"] = {}
                st.rerun()

    last_id = st.session_state.get("last_created_quote_id")
    if last_id:
        st.divider()
        st.subheader("방금 만든 견적서 다운로드")
        if get_quote(last_id):
            render_quote_export_buttons(last_id, key_prefix="created_quote_export")



# -----------------------------
# 견적 상세 공통
# -----------------------------

def render_quote_detail(quote_id, allow_edit=True, key_prefix="detail", mode="combined"):
    quote = get_quote(quote_id)
    if not quote:
        st.error("견적서를 찾지 못했습니다.")
        return
    items = load_quote_items_df(quote_id)
    status = str(quote.get("status", ""))
    # 삭제 상태가 아니면 모든 상태에서 수량/단가/날짜/상품 구성을 수정할 수 있다.
    # 수정이 발생하면 자동으로 견적중 상태로 되돌린다.
    composition_editable = status != "삭제"

    title_cols = st.columns([4.2, 1.8])
    with title_cols[0]:
        render_quote_detail_header(quote)
    with title_cols[1]:
        top_buttons = st.columns(2)
        with top_buttons[0]:
            if st.button("수정", use_container_width=True, key=f"{key_prefix}_top_edit_{quote_id}", disabled=status == "삭제"):
                if quote_header_dialog is not None:
                    quote_header_dialog(quote_id)
        with top_buttons[1]:
            if status != "삭제" and st.button("삭제", use_container_width=True, key=f"{key_prefix}_top_delete_{quote_id}"):
                delete_quote(quote_id)
                st.session_state["combined_detail_id"] = None
                st.success("삭제 처리했습니다.")
                st.rerun()

    st.markdown("#### 견적서 파일")
    render_quote_export_buttons(quote_id, key_prefix=f"{key_prefix}_export")

    render_quote_meta_editor(quote_id, key_prefix=key_prefix)

    product_head_cols = st.columns([4, 2])
    with product_head_cols[0]:
        st.subheader("견적 상품")
    with product_head_cols[1]:
        action_buttons = st.columns(2)
        with action_buttons[0]:
            if st.button("상품 추가", use_container_width=True, key=f"{key_prefix}_mid_add_{quote_id}", disabled=status == "삭제"):
                if quote_add_product_dialog is not None:
                    quote_add_product_dialog(quote_id)
        with action_buttons[1]:
            if status == "견적중":
                if st.button("확정", type="primary", use_container_width=True, key=f"{key_prefix}_mid_confirm_{quote_id}"):
                    ok, failures = confirm_quote(quote_id)
                    if ok:
                        st.success("견적서를 확정했습니다.")
                        st.rerun()
                    else:
                        st.error("확정할 수 없습니다.")
                        for f in failures:
                            st.write(f"- {f}")
            elif status in ["확정", "부분반납"]:
                if st.button("전체 반납", type="primary", use_container_width=True, key=f"{key_prefix}_mid_return_{quote_id}"):
                    return_quote(quote_id)
                    st.success("전체 반납 처리했습니다.")
                    st.rerun()
    if items.empty:
        st.info("상품이 없습니다.")
        return

    product_nos = tuple(items["product_no"].astype(str).tolist())
    product_map = load_products_map(product_nos)
    rentals_df = load_rentals_for_quote_df(int(quote_id)) if status in ["확정", "부분반납", "반납완료"] else pd.DataFrame()
    rental_map = {}
    if not rentals_df.empty:
        for _, r in rentals_df.iterrows():
            if str(r.get("status")) == "삭제":
                continue
            rental_map.setdefault(str(r.get("product_no")), []).append(dict(r))

    for _, item in items.iterrows():
        product_no = str(item["product_no"])
        product = product_map.get(product_no) or {}
        stock_qty = max(safe_int(product.get("qty", 1), 1), 1)
        item_pickup, item_return = get_item_dates(item, quote)
        status_one = bulk_product_status((product_no,), item_pickup, item_return, int(quote_id)).get(product_no, {"reserved": 0, "pending": 0})
        item_multiplier = quote_price_multiplier(item_pickup, item_return)
        related_rentals = rental_map.get(product_no, [])
        active_rental = next((r for r in related_rentals if str(r.get("status")) in ACTIVE_RENTAL_STATUSES), None)
        returned_rental = next((r for r in related_rentals if str(r.get("status")) == "반납완료"), None)
        current_rental = active_rental or returned_rental
        item_returned = bool(returned_rental and not active_rental)
        item_editable = composition_editable

        with st.container(border=True):
            status_cols = st.columns([2.3, 3.7])
            with status_cols[0]:
                render_status_button_bar(
                    product_no,
                    item_pickup,
                    item_return,
                    key_prefix=f"{key_prefix}_item_status_{item['id']}",
                    exclude_quote_id=quote_id,
                    precomputed=status_one,
                    total_qty_override=stock_qty,
                )
            c1, c2, c3, c4, c5, c6 = st.columns([1.1, 2.4, 1.2, 1.3, 1.05, 1.05])
            with c1:
                show_thumb_from_values(item.get("thumbnail_path", ""), item.get("thumbnail_url", ""))
            with c2:
                st.markdown(f"**{item['product_name']}**")
                st.caption(item.get("size_text", ""))
                st.caption(f"상품번호: {product_no}")
                st.caption(f"보유수량: {stock_qty}개")
                st.caption(f"상품 날짜: {item_pickup} ~ {item_return}")
                if current_rental:
                    st.markdown(status_badge(str(current_rental.get("status")), status_kind(current_rental.get("status"))), unsafe_allow_html=True)
            if item_editable:
                with c3:
                    qty = render_quantity_stepper("수량", item["quantity"], stock_qty, f"{key_prefix}_qty_{item['id']}")
                with c4:
                    unit = st.number_input("단가", min_value=0, value=safe_int(item["unit_price"], 0), step=1000, key=f"{key_prefix}_price_{item['id']}")
                    st.caption(f"적용 {item_multiplier}배")
                    st.caption(f"금액: {money(calculate_line_total(qty, unit, item_pickup, item_return, multiplier=item_multiplier))}")
                changed_item_values = (
                    safe_int(qty, 1) != safe_int(item["quantity"], 1)
                    or safe_int(unit, 0) != safe_int(item["unit_price"], 0)
                )
                if changed_item_values:
                    ok, msg = update_quote_item(item["id"], qty, unit)
                    if ok:
                        st.rerun()
                    else:
                        st.error(msg)
                with c5:
                    if st.button("날짜 수정", key=f"{key_prefix}_date_item_{item['id']}", use_container_width=True):
                        if quote_item_date_dialog is not None:
                            quote_item_date_dialog(int(item["id"]), int(quote_id))
                with c6:
                    if status in ["확정", "부분반납", "반납완료"] and current_rental:
                        if item_returned:
                            if st.button("반납 취소", key=f"{key_prefix}_undo_return_item_{current_rental['id']}", use_container_width=True):
                                ok, msg = set_rental_item_returned(int(current_rental["id"]), returned=False)
                                if ok:
                                    st.rerun()
                                else:
                                    st.error(msg)
                        else:
                            if st.button("반납", key=f"{key_prefix}_return_item_{current_rental['id']}", use_container_width=True):
                                ok, msg = set_rental_item_returned(int(current_rental["id"]), returned=True)
                                if ok:
                                    st.rerun()
                                else:
                                    st.error(msg)
                    if st.button("제품 삭제", key=f"{key_prefix}_delete_item_{item['id']}", use_container_width=True, disabled=status == "삭제"):
                        ok, msg = delete_quote_item(item["id"], quote_id)
                        if ok:
                            if msg:
                                st.info(msg)
                            st.rerun()
                        else:
                            st.error(msg)
            else:
                with c3:
                    st.write(f"수량: {item['quantity']}")
                with c4:
                    st.write(f"단가: {money(item['unit_price'])}")
                    st.write(f"적용 {item_multiplier}배")
                    st.write(f"금액: {money(item['line_total'])}")
                with c5:
                    if status in ["확정", "부분반납", "반납완료"] and current_rental:
                        if item_returned:
                            if st.button("반납 취소", key=f"{key_prefix}_undo_return_item_readonly_{current_rental['id']}", use_container_width=True):
                                ok, msg = set_rental_item_returned(int(current_rental["id"]), returned=False)
                                if ok:
                                    st.rerun()
                                else:
                                    st.error(msg)
                        else:
                            if st.button("반납", key=f"{key_prefix}_return_item_readonly_{current_rental['id']}", use_container_width=True):
                                ok, msg = set_rental_item_returned(int(current_rental["id"]), returned=True)
                                if ok:
                                    st.rerun()
                                else:
                                    st.error(msg)
                with c6:
                    if item_returned:
                        st.caption("반납완료")
                    else:
                        st.caption("수정 불가")




# -----------------------------
# 페이지: 견적서 조회
# -----------------------------

def combined_quote_list_card(row, key_prefix="combined"):
    thumb_path = str(row.get("first_thumb_path", "") or "")
    thumb_url = str(row.get("first_thumb_url", "") or "")
    with st.container(border=True):
        c0, c1, c2, c3, c4 = st.columns([0.9, 2.0, 2.2, 2.6, 1.6])
        with c0:
            show_thumb_from_values(thumb_path, thumb_url) if (thumb_path or thumb_url) else st.caption("이미지 없음")
        with c1:
            st.markdown(f"### {row['team_name']}")
            st.caption(format_datetime_short(row.get("created_at", "")))
            _status_label = valid_status_text(row.get("status"))
            if _status_label:
                st.markdown(status_badge(_status_label, status_kind(_status_label)), unsafe_allow_html=True)
        with c2:
            st.markdown(f"**{row['quote_no']}**")
            st.caption(f"픽업 {row['pickup_date']}")
            st.caption(f"반납 {row['return_date']}")
        with c3:
            st.markdown(f"**{row.get('상품요약','')}**")
            st.caption(f"총액 {money(row.get('total', 0))}")
        with c4:
            detail = st.button("상세 보기", key=f"{key_prefix}_detail_{row['quote_id']}", use_container_width=True)
            main_action = False
            if row["status"] == "견적중":
                main_action = st.button("확정", type="primary", key=f"{key_prefix}_confirm_{row['quote_id']}", use_container_width=True)
            elif row["status"] in ["확정", "부분반납"]:
                main_action = st.button("전체 반납", type="primary", key=f"{key_prefix}_return_{row['quote_id']}", use_container_width=True)
    return detail, main_action


def page_quote_history():
    detail_id = st.session_state.get("combined_detail_id")
    if detail_id:
        if st.button("‹ 목록으로", key="combined_back"):
            st.session_state["combined_detail_id"] = None
            st.rerun()
        render_quote_detail(int(detail_id), allow_edit=True, key_prefix="combined_detail")
        return

    st.header("견적서 조회/반납 기록")
    quotes = load_quotes_df(include_deleted=False)
    if quotes.empty:
        st.info("저장된 견적서가 없습니다.")
        return

    status_filter = status_filter_control("상태 필터", ["견적중", "확정", "부분반납", "반납완료"], "combined_status_filter")
    filtered = quotes[quotes["status"] == status_filter].copy() if status_filter else quotes.copy()

    search = st.text_input("팀명 / 견적번호 / 상품 요약 검색", key="combined_search")
    if search:
        ss = search.lower()
        filtered = filtered[
            filtered["team_name"].astype(str).str.lower().str.contains(ss, na=False)
            | filtered["quote_no"].astype(str).str.lower().str.contains(ss, na=False)
            | filtered["상품요약"].astype(str).str.lower().str.contains(ss, na=False)
        ]

    d1, d2 = st.columns(2)
    with d1:
        pickup_raw = st.text_input("픽업일 조회", key="combined_pickup_filter", placeholder="YYYYMMDD")
    with d2:
        return_raw = st.text_input("반납일 조회", key="combined_return_filter", placeholder="YYYYMMDD")
    pickup_filter = try_normalize_date_text(pickup_raw) if pickup_raw.strip() else ""
    return_filter = try_normalize_date_text(return_raw) if return_raw.strip() else ""
    if pickup_raw.strip() and not pickup_filter:
        st.warning("픽업일 검색 형식을 확인하세요.")
    elif pickup_filter:
        filtered = filtered[filtered["pickup_date"].astype(str) == pickup_filter]
    if return_raw.strip() and not return_filter:
        st.warning("반납일 검색 형식을 확인하세요.")
    elif return_filter:
        filtered = filtered[filtered["return_date"].astype(str) == return_filter]

    if filtered.empty:
        st.info("조건에 맞는 기록이 없습니다.")
        return

    filtered = filtered.reset_index(drop=True)
    page_size = 10
    if "combined_list_page" not in st.session_state:
        st.session_state["combined_list_page"] = 1
    total_pages = max(1, math.ceil(len(filtered) / page_size))
    st.session_state["combined_list_page"] = min(max(1, st.session_state["combined_list_page"]), total_pages)
    start = (st.session_state["combined_list_page"] - 1) * page_size
    page_df = filtered.iloc[start:start + page_size]

    for _, row in page_df.iterrows():
        detail, main_action = combined_quote_list_card(row)
        if detail:
            st.session_state["combined_detail_id"] = int(row["quote_id"])
            st.rerun()
        if main_action:
            if row["status"] == "견적중":
                ok, failures = confirm_quote(int(row["quote_id"]))
                if ok:
                    st.success("견적서를 확정했습니다.")
                    st.rerun()
                else:
                    st.error("확정할 수 없습니다.")
                    for f in failures:
                        st.write(f"- {f}")
            else:
                return_quote(int(row["quote_id"]))
                st.success("전체 반납 처리했습니다.")
                st.rerun()

    pagination_controls(len(filtered), page_size, "combined_list_page")





# =============================================================
# 견적서 가로형 출력 / 연박 10% / D.C / 확정 거래명세서 패치
# =============================================================
# 이 블록은 위에서 정의된 기존 함수들을 배포 직전에 다시 정의해서 최신 운영 기준을 적용한다.

import zipfile


def parse_quote_meta(value):
    """quotes.memo를 기존 일반 메모와 신규 JSON 메타 모두 호환해서 읽는다."""
    text = str(value or "").strip()
    base = {"note": "", "dc_label": "", "dc_amount": 0}
    if not text:
        return base
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            base.update(data)
            base["dc_amount"] = safe_int(base.get("dc_amount", 0), 0)
            return base
    except Exception:
        pass
    base["note"] = text
    return base


def build_quote_meta(note="", dc_label="", dc_amount=0):
    return json.dumps({
        "note": clean_text(note),
        "dc_label": clean_text(dc_label),
        "dc_amount": max(safe_int(dc_amount, 0), 0),
    }, ensure_ascii=False)


def quote_discount_info(quote):
    meta = parse_quote_meta((quote or {}).get("memo", ""))
    label = clean_text(meta.get("dc_label", ""))
    amount = max(safe_int(meta.get("dc_amount", 0), 0), 0)
    return label, amount


def rental_pricing_context(pickup_date, return_date, holidays=None):
    """상품 단가는 2박 3일 기준 금액이다.

    - 일요일과 지정 휴일은 청구일에서 제외한다.
    - 기본 2박 3일 = 청구 영업일 3일까지 기본가 1배
    - 청구 영업일이 3일을 초과하면 초과 1일당 공급가의 10%를 연박으로 계산한다.
    """
    pickup_s = normalize_date_text(pickup_date)
    return_s = normalize_date_text(return_date)
    start = datetime.strptime(pickup_s, "%Y-%m-%d").date()
    end = datetime.strptime(return_s, "%Y-%m-%d").date()
    if end < start:
        raise ValueError("반납 날짜가 픽업 날짜보다 빠릅니다.")

    holidays = set(holidays if holidays is not None else get_holiday_dates())
    cursor = start
    billable_dates = []
    while cursor <= end:
        if cursor.weekday() != 6 and cursor.isoformat() not in holidays:
            billable_dates.append(cursor.isoformat())
        cursor += timedelta(days=1)

    extra_days = max(0, len(billable_dates) - 3)
    return {
        "pickup_date": pickup_s,
        "return_date": return_s,
        "stay_nights": max((end - start).days, 0),
        "billable_days": len(billable_dates),
        "billable_dates": billable_dates,
        "base_days": min(len(billable_dates), 3),
        "extra_days": extra_days,
        "overnight_rate": 0.10,
        "base_multiplier": 1,
        "multiplier": 1 + (extra_days * 0.10),
    }


def quote_price_multiplier(pickup_date, return_date):
    try:
        return rental_pricing_context(pickup_date, return_date)["multiplier"]
    except Exception:
        return 1


def calculate_line_total(quantity, unit_price, pickup_date=None, return_date=None, multiplier=None):
    """상품 행 금액은 연박을 포함하지 않는 기본 공급가만 저장한다."""
    return max(safe_int(quantity, 1), 1) * max(safe_int(unit_price, 0), 0)


def calculate_overtime_amount(quantity, unit_price, pickup_date, return_date):
    try:
        ctx = rental_pricing_context(pickup_date, return_date)
        base = calculate_line_total(quantity, unit_price)
        return int(round(base * 0.10 * safe_int(ctx.get("extra_days", 0), 0)))
    except Exception:
        return 0


def pricing_summary_text(pickup_date, return_date):
    try:
        ctx = rental_pricing_context(pickup_date, return_date)
        extra = safe_int(ctx.get("extra_days", 0), 0)
        billable = safe_int(ctx.get("billable_days", 0), 0)
        if extra:
            return f"요금 기준: 일요일/휴일 제외 {billable}일 청구 · 기본 2박3일 + 연박 {extra}일 · 연박 10%/일"
        return f"요금 기준: 일요일/휴일 제외 {billable}일 청구 · 기본 2박3일 요금"
    except Exception:
        return "날짜를 입력하면 요금 기준이 계산됩니다."


def item_availability_for_quote(item, quote):
    """견적서 표시용 상태. 현재 견적 자기 자신은 재고 충돌에서 제외한다."""
    try:
        product_no = str(item.get("product_no"))
        item_pickup, item_return = get_item_dates(item, quote)
        status_one = bulk_product_status((product_no,), item_pickup, item_return, int(quote.get("quote_id", 0))).get(product_no, {})
        reserved = safe_int(status_one.get("reserved", 0), 0)
        pending = safe_int(status_one.get("pending", 0), 0)
        if reserved > 0:
            return "불가", reserved, pending
        if pending > 0:
            return "견적중", reserved, pending
        return "가능", reserved, pending
    except Exception:
        return "가능", 0, 0


def quote_financials(quote_id, include_only_priceable=True):
    quote = get_quote(quote_id)
    if not quote:
        return {
            "subtotal": 0, "overnight": 0, "vat": 0, "dc_label": "", "dc_amount": 0,
            "total": 0, "items": [], "billable_days": 0, "extra_days": 0,
        }
    items = load_quote_items_df(quote_id)
    subtotal = 0
    overnight = 0
    item_infos = []
    for _, item in items.iterrows():
        item_dict = dict(item)
        qty = max(safe_int(item_dict.get("quantity", 1), 1), 1)
        unit = max(safe_int(item_dict.get("unit_price", 0), 0), 0)
        item_pickup, item_return = get_item_dates(item_dict, quote)
        base = calculate_line_total(qty, unit)
        extra = calculate_overtime_amount(qty, unit, item_pickup, item_return)
        state, reserved, pending = item_availability_for_quote(item_dict, quote)
        # 견적중 상태에서는 불가/견적중 상품을 최종가에서 제외한다. 확정 후에는 확정 품목을 기준으로 산출한다.
        priceable = not (str(quote.get("status")) == "견적중" and state in ["불가", "견적중"])
        if priceable or not include_only_priceable:
            subtotal += base
            overnight += extra
        item_infos.append({
            "item": item_dict,
            "quantity": qty,
            "unit_price": unit,
            "base": base,
            "overnight": extra,
            "pickup_date": item_pickup,
            "return_date": item_return,
            "state": state,
            "reserved": reserved,
            "pending": pending,
            "priceable": priceable,
            "ctx": rental_pricing_context(item_pickup, item_return),
        })
    vat_base = subtotal + overnight
    vat = int(round(vat_base * 0.1))
    dc_label, dc_amount = quote_discount_info(quote)
    dc_amount = min(max(dc_amount, 0), vat_base + vat)
    total = max(vat_base + vat - dc_amount, 0)
    try:
        header_ctx = rental_pricing_context(quote.get("pickup_date"), quote.get("return_date"))
    except Exception:
        header_ctx = {"billable_days": 0, "extra_days": 0}
    return {
        "subtotal": int(subtotal),
        "overnight": int(overnight),
        "vat": int(vat),
        "dc_label": dc_label,
        "dc_amount": int(dc_amount),
        "total": int(total),
        "items": item_infos,
        "billable_days": safe_int(header_ctx.get("billable_days", 0), 0),
        "extra_days": safe_int(header_ctx.get("extra_days", 0), 0),
    }


def update_quote_totals(quote_id, reprice=True, clear_cache=True):
    quote = get_quote(quote_id)
    if not quote:
        return
    client = supabase_client()
    items = load_quote_items_df(quote_id)
    if reprice and not items.empty:
        for _, item in items.iterrows():
            qty = max(safe_int(item.get("quantity", 1), 1), 1)
            unit = max(safe_int(item.get("unit_price", 0), 0), 0)
            base = calculate_line_total(qty, unit)
            if base != safe_int(item.get("line_total", 0), 0):
                client.table("quote_items").update({
                    "line_total": base,
                    "updated_at": datetime.now().isoformat(),
                }).eq("id", int(item["id"])).execute()
    totals = quote_financials(quote_id, include_only_priceable=True)
    client.table("quotes").update({
        "subtotal": totals["subtotal"],
        "vat": totals["vat"],
        "total": totals["total"],
        "updated_at": datetime.now().isoformat(),
    }).eq("quote_id", int(quote_id)).execute()
    if clear_cache:
        clear_quote_cache()


def create_quote(team_name, pickup_date, return_date, selected_items, memo="", dc_label="", dc_amount=0):
    pickup = normalize_date_text(pickup_date)
    ret = normalize_date_text(return_date)
    quote_no = generate_quote_no()
    client = supabase_client()
    res = client.table("quotes").insert({
        "quote_no": quote_no,
        "team_name": team_name,
        "pickup_date": pickup,
        "return_date": ret,
        "status": "견적중",
        "subtotal": 0,
        "vat": 0,
        "total": 0,
        "memo": build_quote_meta(memo, dc_label, dc_amount),
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }).execute()
    quote_id = int(res.data[0]["quote_id"])

    rows = []
    for product_no, item in selected_items.items():
        p = get_product(product_no)
        if not p:
            continue
        qty = safe_int(item.get("quantity", 1), 1)
        unit_price = safe_int(item.get("unit_price", p.get("price", 0)), 0)
        rows.append({
            "quote_id": quote_id,
            "product_no": str(product_no),
            "product_name": p.get("name", ""),
            "size_text": p.get("size_text", ""),
            "thumbnail_url": p.get("thumbnail_url", ""),
            "local_thumbnail_path": p.get("local_thumbnail_path", ""),
            "quantity": qty,
            "unit_price": unit_price,
            "line_total": calculate_line_total(qty, unit_price),
            "availability_note": make_item_note(pickup, ret),
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        })
    if rows:
        client.table("quote_items").insert(rows).execute()
    update_quote_totals(quote_id, reprice=False)
    clear_quote_cache()
    return quote_id


def set_all_quote_item_dates(quote_id, pickup_date, return_date, reprice=True):
    quote = get_quote(quote_id)
    if not quote:
        return
    items = load_quote_items_df(quote_id)
    client = supabase_client()
    pickup = normalize_date_text(pickup_date)
    ret = normalize_date_text(return_date)
    for _, item in items.iterrows():
        qty = max(safe_int(item.get("quantity", 1), 1), 1)
        unit_price = max(safe_int(item.get("unit_price", 0), 0), 0)
        payload = {
            "availability_note": make_item_note(pickup, ret, item.get("availability_note", "")),
            "line_total": calculate_line_total(qty, unit_price),
            "updated_at": datetime.now().isoformat(),
        }
        client.table("quote_items").update(payload).eq("id", int(item["id"])).execute()


def update_quote_item(item_id, quantity, unit_price):
    client = supabase_client()
    item_rows = client.table("quote_items").select("quote_id,product_no,availability_note").eq("id", int(item_id)).limit(1).execute().data or []
    if not item_rows:
        return False, "견적 상품을 찾지 못했습니다."
    quote_id = int(item_rows[0]["quote_id"])
    product_no = str(item_rows[0].get("product_no", ""))
    ok, message = reopen_quote_for_edit(quote_id)
    if not ok:
        return False, message
    product = get_product(product_no) or {}
    max_qty = max(safe_int(product.get("qty", 1), 1), 1)
    qty = max(safe_int(quantity, 1), 1)
    if qty > max_qty:
        return False, f"보유수량이 {max_qty}개입니다."
    price = max(safe_int(unit_price, 0), 0)
    client.table("quote_items").update({
        "quantity": qty,
        "unit_price": price,
        "line_total": calculate_line_total(qty, price),
        "updated_at": datetime.now().isoformat(),
    }).eq("id", int(item_id)).execute()
    update_quote_totals(quote_id, reprice=False)
    clear_quote_cache()
    return True, message or "상품 정보를 변경했습니다."


def update_quote_item_dates(item_id, quote_id, pickup_date, return_date):
    try:
        pickup = normalize_date_text(pickup_date)
        ret = normalize_date_text(return_date)
    except Exception as e:
        return False, [str(e)]
    if ret < pickup:
        return False, ["반납 날짜가 픽업 날짜보다 빠릅니다."]
    quote = get_quote(quote_id)
    if not quote:
        return False, ["견적서를 찾지 못했습니다."]
    item_rows = supabase_client().table("quote_items").select("*").eq("id", int(item_id)).limit(1).execute().data or []
    if not item_rows:
        return False, ["견적 상품을 찾지 못했습니다."]
    item = item_rows[0]
    product_no = str(item.get("product_no", ""))
    product = get_product(product_no) or {}
    total_qty = max(safe_int(product.get("qty", 1), 1), 1)
    qty = max(safe_int(item.get("quantity", 1), 1), 1)
    if qty > total_qty:
        return False, [f"보유수량이 {total_qty}개입니다."]
    ok, message = reopen_quote_for_edit(quote_id)
    if not ok:
        return False, [message]
    supabase_client().table("quote_items").update({
        "availability_note": make_item_note(pickup, ret, item.get("availability_note", "")),
        "line_total": calculate_line_total(qty, safe_int(item.get("unit_price", 0), 0)),
        "updated_at": datetime.now().isoformat(),
    }).eq("id", int(item_id)).execute()
    update_quote_date_range_from_items(quote_id)
    update_quote_totals(quote_id, reprice=False)
    clear_quote_cache()
    return True, ([message] if message else [])


def render_quote_detail_header(quote):
    status = valid_status_text(quote.get("status")) or "견적중"
    created = format_datetime_short(quote.get("created_at"))
    totals = quote_financials(int(quote.get("quote_id")))
    st.markdown(f"""
    <div class='quote-detail-head'>
        <div class='quote-status-line'>{status_badge(status, status_kind(status))}</div>
        <div class='quote-title-line'>{escape(str(quote.get('quote_no', '')))} / {escape(str(quote.get('team_name', '')))}</div>
        <div class='quote-meta-grid'>
            <div><span>픽업</span><strong>{escape(str(quote.get('pickup_date', '')))}</strong></div>
            <div><span>반납</span><strong>{escape(str(quote.get('return_date', '')))}</strong></div>
            <div><span>공급가</span><strong>{escape(money(totals.get('subtotal', 0)))}</strong></div>
            <div><span>연박</span><strong>{escape(money(totals.get('overnight', 0)))}</strong></div>
            <div><span>총액</span><strong>{escape(money(totals.get('total', 0)))}</strong></div>
            <div><span>작성일</span><strong>{escape(created or '-')}</strong></div>
        </div>
        <div class='quote-pricing-line'>{escape(pricing_summary_text(quote.get('pickup_date'), quote.get('return_date')))}</div>
    </div>
    """, unsafe_allow_html=True)


def quote_status_label_for_image(item_info, quote):
    if str(quote.get("status")) != "견적중":
        return ""
    state = item_info.get("state", "가능")
    if state == "불가":
        return "불가"
    if state == "견적중":
        return "견적중"
    return ""


def draw_quote_product_card(draw, canvas, box, item_info, quote, fonts):
    x, y, w, h = box
    item = item_info["item"]
    state_label = quote_status_label_for_image(item_info, quote)
    priceable = item_info.get("priceable", True)
    draw.rounded_rectangle((x, y, x + w, y + h), radius=16, outline=(218, 218, 218), width=2, fill=(252, 252, 252))
    if state_label:
        fill = (253, 236, 236) if state_label == "불가" else (255, 241, 204)
        outline = (210, 60, 50) if state_label == "불가" else (210, 120, 0)
        draw.rounded_rectangle((x + 14, y + 12, x + 100, y + 42), radius=12, fill=fill, outline=outline, width=1)
        draw.text((x + 30, y + 18), state_label, font=fonts["xs_b"], fill=outline)
        if not priceable:
            draw.text((x + 110, y + 18), "총액 제외", font=fonts["xs"], fill=(150, 70, 70))
    thumb_y = y + 50
    thumb_box = (x + 18, thumb_y, x + w - 18, thumb_y + 128)
    draw.rounded_rectangle(thumb_box, radius=14, fill=(244, 244, 244))
    thumb = open_thumb(item.get("thumbnail_path", ""), item.get("thumbnail_url", ""))
    if thumb:
        thumb.thumbnail((thumb_box[2] - thumb_box[0] - 12, thumb_box[3] - thumb_box[1] - 12))
        tx = thumb_box[0] + ((thumb_box[2] - thumb_box[0]) - thumb.width) // 2
        ty = thumb_box[1] + ((thumb_box[3] - thumb_box[1]) - thumb.height) // 2
        canvas.paste(thumb, (tx, ty))
    text_x = x + 18
    ty = thumb_box[3] + 12
    draw_fit_text(draw, (text_x, ty), item.get("product_name", ""), fonts["s_b"], fill=(20,20,20), max_width=w - 36)
    draw_fit_text(draw, (text_x, ty + 30), item.get("size_text", ""), fonts["xs"], fill=(90,90,90), max_width=w - 36)
    draw.text((text_x, ty + 60), f"{item_info['quantity']}EA x {money(item_info['unit_price'])}", font=fonts["xs"], fill=(80,80,80))
    draw.text((text_x, ty + 88), money(item_info["base"]), font=fonts["s_b"], fill=(20,20,20))


def make_quote_page_images(quote_id):
    quote = get_quote(quote_id)
    if not quote:
        raise ValueError("견적서를 찾을 수 없습니다.")
    totals = quote_financials(quote_id, include_only_priceable=True)
    items = totals.get("items", [])
    pages = [items[i:i+10] for i in range(0, len(items), 10)] or [[]]
    out_pages = []
    width, height = 1600, 900
    margin = 70
    fonts = {
        "logo": load_font(62, bold=True),
        "title": load_font(42, bold=True),
        "h": load_font(28, bold=True),
        "m": load_font(22),
        "m_b": load_font(22, bold=True),
        "s": load_font(18),
        "s_b": load_font(18, bold=True),
        "xs": load_font(15),
        "xs_b": load_font(15, bold=True),
    }
    for page_idx, page_items in enumerate(pages):
        img = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(img)
        draw.text((margin, 38), "프라비", font=fonts["logo"], fill=(0,0,0))
        title = "견적서"
        tw, _ = text_size(draw, title, fonts["title"])
        draw.text((width - margin - tw, 54), title, font=fonts["title"], fill=(20,20,20))
        line_y = 125
        draw.line((margin, line_y, width - margin, line_y), fill=(35,35,35), width=2)
        if page_idx == 0:
            info_y = 150
            draw.text((margin, info_y), f"팀 이름: {quote.get('team_name','')}", font=fonts["h"], fill=(25,25,25))
            draw.text((margin, info_y + 42), f"픽업: {quote.get('pickup_date','')}", font=fonts["m"], fill=(60,60,60))
            draw.text((margin + 260, info_y + 42), f"반납: {quote.get('return_date','')}", font=fonts["m"], fill=(60,60,60))
            price_x = width - margin - 400
            py = info_y - 2
            draw.text((price_x, py), "공급가", font=fonts["m"], fill=(60,60,60)); draw.text((price_x+210, py), money(totals['subtotal']), font=fonts["m_b"], fill=(20,20,20))
            draw.text((price_x, py+36), f"연박 {totals['extra_days']}일", font=fonts["m"], fill=(60,60,60)); draw.text((price_x+210, py+36), money(totals['overnight']), font=fonts["m_b"], fill=(20,20,20))
            draw.text((price_x, py+72), "부가세", font=fonts["m"], fill=(60,60,60)); draw.text((price_x+210, py+72), money(totals['vat']), font=fonts["m_b"], fill=(20,20,20))
            if totals.get('dc_amount'):
                dc_label = totals.get('dc_label') or 'D.C'
                draw.text((price_x, py+108), dc_label, font=fonts["m"], fill=(220,40,40)); draw.text((price_x+210, py+108), f"-{money(totals['dc_amount'])}", font=fonts["m_b"], fill=(220,40,40))
                total_y = py + 150
            else:
                total_y = py + 120
            draw.line((price_x, total_y-8, width-margin, total_y-8), fill=(220,220,220), width=2)
            draw.text((price_x, total_y), "총금액", font=fonts["h"], fill=(0,0,0)); draw.text((price_x+210, total_y), money(totals['total']), font=fonts["h"], fill=(0,0,0))
            grid_top = 315
        else:
            small = f"{quote.get('team_name','')} · {quote.get('pickup_date','')} ~ {quote.get('return_date','')} · {page_idx+1}/{len(pages)}"
            draw.text((margin, 150), small, font=fonts["m"], fill=(90,90,90))
            grid_top = 200
        cols, rows = 5, 2
        gap_x, gap_y = 22, 26
        card_w = int((width - margin*2 - gap_x*(cols-1)) / cols)
        card_h = 250
        for idx, item_info in enumerate(page_items):
            col = idx % cols
            row = idx // cols
            x = margin + col*(card_w + gap_x)
            y = grid_top + row*(card_h + gap_y)
            draw_quote_product_card(draw, img, (x, y, card_w, card_h), item_info, quote, fonts)
        out_pages.append(img)
    return out_pages


def make_quote_image_bytes(quote_id):
    img = make_quote_page_images(quote_id)[0]
    out = BytesIO(); img.save(out, format="PNG"); out.seek(0); return out.getvalue()


def make_quote_png_bundle_bytes(quote_id):
    pages = make_quote_page_images(quote_id)
    if len(pages) == 1:
        out = BytesIO(); pages[0].save(out, format="PNG"); out.seek(0); return out.getvalue(), "png"
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for i, page in enumerate(pages, start=1):
            bio = BytesIO(); page.save(bio, format="PNG"); z.writestr(f"quote_page_{i:02d}.png", bio.getvalue())
    out.seek(0)
    return out.getvalue(), "zip"


def make_quote_pdf_bytes(quote_id):
    pages = make_quote_page_images(quote_id)
    images = [p.convert("RGB") for p in pages]
    out = BytesIO()
    images[0].save(out, format="PDF", resolution=150.0, save_all=True, append_images=images[1:])
    out.seek(0)
    return out.getvalue()


def make_invoice_image_bytes(quote_id):
    quote = get_quote(quote_id)
    if not quote:
        raise ValueError("견적서를 찾을 수 없습니다.")
    totals = quote_financials(quote_id, include_only_priceable=False)
    items = totals.get("items", [])
    width = 1200
    row_h = 42
    height = 420 + max(1, len(items)) * row_h
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    f_logo = load_font(44, True); f_h = load_font(24, True); f_m = load_font(18); f_s = load_font(15)
    x0 = 22; y = 20
    draw.text((x0, y), "프라비 견적서", font=f_logo, fill=(0,0,0))
    draw.text((440, y), "공급자", font=f_m, fill=(0,0,0))
    supplier = ["상호  프라비(PROP.B)      대표자  이승훈", "등록번호  772-01-02018", "주소  경기도 남양주시 진접읍 금곡리45", "전화  0507-1390-3668"]
    for i, line in enumerate(supplier):
        draw.text((520, y + i*34), line, font=f_s, fill=(30,30,30))
    y += 110
    draw.text((x0, y), f"픽업일     {quote.get('pickup_date','')}", font=f_s, fill=(0,0,0))
    draw.text((x0, y+30), f"반납일     {quote.get('return_date','')}", font=f_s, fill=(0,0,0))
    draw.text((x0, y+60), f"팀명       {quote.get('team_name','')}", font=f_s, fill=(0,0,0))
    draw.rectangle((0, y+100, width, y+155), fill=(244,244,244))
    draw.text((x0, y+114), "총금액 (공급가액 + 세액)", font=f_h, fill=(0,0,0))
    total_text = money(totals['total'])
    tw, _ = text_size(draw, total_text, f_h)
    draw.text((width-40-tw, y+114), total_text, font=f_h, fill=(0,0,0))
    y += 180
    headers = ["품명", "개당가격", "주문수량", "공급가액", "세액"]
    xs = [30, 420, 610, 760, 980]
    draw.rectangle((0, y, width, y+row_h), fill=(244,244,244))
    for h, xx in zip(headers, xs): draw.text((xx, y+12), h, font=f_s, fill=(0,0,0))
    y += row_h
    for info in items:
        item = info['item']; qty=info['quantity']; unit=info['unit_price']; base=info['base']; vat=int(round((base+info['overnight'])*0.1))
        draw.text((30, y+12), str(item.get('product_name',''))[:34], font=f_s, fill=(0,0,0))
        draw.text((420, y+12), f"{unit:,}", font=f_s, fill=(0,0,0))
        draw.text((610, y+12), str(qty), font=f_s, fill=(0,0,0))
        draw.text((760, y+12), f"{base:,}", font=f_s, fill=(0,0,0))
        draw.text((980, y+12), f"{vat:,}", font=f_s, fill=(0,0,0))
        draw.line((0, y+row_h, width, y+row_h), fill=(225,225,225), width=1)
        y += row_h
    y += 12
    draw.rectangle((0, y, width, y+92), fill=(244,244,244))
    draw.text((500, y+18), f"합계 {money(totals['subtotal'])}", font=f_s, fill=(0,0,0))
    draw.text((760, y+18), f"연박 {money(totals['overnight'])}", font=f_s, fill=(0,0,0))
    draw.text((980, y+18), f"세액 {money(totals['vat'])}", font=f_s, fill=(0,0,0))
    draw.text((x0, y+110), "입금계좌: 국민은행 / 618301-01-466485 / 이승훈", font=f_h, fill=(0,0,0))
    draw.text((x0, y+150), "E-mail : propb.come@gmail.com", font=f_h, fill=(0,0,0))
    out = BytesIO(); img.save(out, format="PNG"); out.seek(0); return out.getvalue()


def make_invoice_pdf_bytes(quote_id):
    png = make_invoice_image_bytes(quote_id)
    img = Image.open(BytesIO(png)).convert("RGB")
    out = BytesIO(); img.save(out, format="PDF", resolution=150.0); out.seek(0); return out.getvalue()


@st.cache_data(ttl=900, show_spinner=False)
def cached_quote_pdf_bytes_v2(quote_id, signature):
    return make_quote_pdf_bytes(quote_id)


@st.cache_data(ttl=900, show_spinner=False)
def cached_quote_png_bundle_bytes_v2(quote_id, signature):
    return make_quote_png_bundle_bytes(quote_id)


@st.cache_data(ttl=900, show_spinner=False)
def cached_invoice_pdf_bytes_v2(quote_id, signature):
    return make_invoice_pdf_bytes(quote_id)


@st.cache_data(ttl=900, show_spinner=False)
def cached_invoice_png_bytes_v2(quote_id, signature):
    return make_invoice_image_bytes(quote_id)


def render_quote_export_buttons(quote_id, key_prefix="quote_export"):
    signature = quote_export_signature(quote_id)
    quote = get_quote(quote_id) or {}
    st.caption("파일 생성 버튼을 누른 뒤 다운로드됩니다. 큰 견적서는 처음 생성할 때만 시간이 걸릴 수 있습니다.")
    c1, c2 = st.columns(2)
    pdf_key = f"{key_prefix}_{quote_id}_{signature}_pdf_v2"
    png_key = f"{key_prefix}_{quote_id}_{signature}_png_v2"
    with c1:
        if st.button("PDF 파일 생성", use_container_width=True, key=f"{key_prefix}_make_pdf_v2_{quote_id}_{signature}"):
            with st.spinner("PDF 생성 중..."):
                st.session_state[pdf_key] = cached_quote_pdf_bytes_v2(quote_id, signature)
        if pdf_key in st.session_state:
            st.download_button("PDF 다운로드", data=st.session_state[pdf_key], file_name=quote_filename(quote_id, "pdf"), mime="application/pdf", use_container_width=True, key=f"{key_prefix}_download_pdf_v2_{quote_id}_{signature}")
    with c2:
        if st.button("PNG 파일 생성", use_container_width=True, key=f"{key_prefix}_make_png_v2_{quote_id}_{signature}"):
            with st.spinner("PNG 생성 중..."):
                st.session_state[png_key] = cached_quote_png_bundle_bytes_v2(quote_id, signature)
        if png_key in st.session_state:
            data, kind = st.session_state[png_key]
            if kind == "zip":
                st.download_button("PNG ZIP 다운로드", data=data, file_name=quote_filename(quote_id, "zip"), mime="application/zip", use_container_width=True, key=f"{key_prefix}_download_png_zip_v2_{quote_id}_{signature}")
            else:
                st.download_button("PNG 다운로드", data=data, file_name=quote_filename(quote_id, "png"), mime="image/png", use_container_width=True, key=f"{key_prefix}_download_png_v2_{quote_id}_{signature}")
    if str(quote.get("status")) in ["확정", "부분반납", "반납완료"]:
        st.markdown("#### 확정용 거래명세서")
        i1, i2 = st.columns(2)
        inv_pdf_key = f"{key_prefix}_{quote_id}_{signature}_invoice_pdf"
        inv_png_key = f"{key_prefix}_{quote_id}_{signature}_invoice_png"
        with i1:
            if st.button("거래명세서 PDF 생성", use_container_width=True, key=f"{key_prefix}_make_invoice_pdf_{quote_id}_{signature}"):
                with st.spinner("거래명세서 PDF 생성 중..."):
                    st.session_state[inv_pdf_key] = cached_invoice_pdf_bytes_v2(quote_id, signature)
            if inv_pdf_key in st.session_state:
                st.download_button("거래명세서 PDF 다운로드", data=st.session_state[inv_pdf_key], file_name=f"프라비_거래명세서_{quote.get('quote_no','')}.pdf", mime="application/pdf", use_container_width=True, key=f"{key_prefix}_download_invoice_pdf_{quote_id}_{signature}")
        with i2:
            if st.button("거래명세서 PNG 생성", use_container_width=True, key=f"{key_prefix}_make_invoice_png_{quote_id}_{signature}"):
                with st.spinner("거래명세서 PNG 생성 중..."):
                    st.session_state[inv_png_key] = cached_invoice_png_bytes_v2(quote_id, signature)
            if inv_png_key in st.session_state:
                st.download_button("거래명세서 PNG 다운로드", data=st.session_state[inv_png_key], file_name=f"프라비_거래명세서_{quote.get('quote_no','')}.png", mime="image/png", use_container_width=True, key=f"{key_prefix}_download_invoice_png_{quote_id}_{signature}")





def update_quote_meta_fields(quote_id, note="", dc_label="", dc_amount=0):
    """견적 상세에서 메모/D.C를 수정한다. 삭제 상태만 막고, 상태 전환은 하지 않는다."""
    quote = get_quote(quote_id)
    if not quote:
        return False, "견적서를 찾지 못했습니다."
    if str(quote.get("status")) == "삭제":
        return False, "삭제 상태의 견적서는 수정할 수 없습니다."
    supabase_client().table("quotes").update({
        "memo": build_quote_meta(note, dc_label, dc_amount),
        "updated_at": datetime.now().isoformat(),
    }).eq("quote_id", int(quote_id)).execute()
    update_quote_totals(int(quote_id), reprice=False)
    clear_quote_cache()
    return True, "견적 메모/D.C를 저장했습니다."


def render_quote_meta_editor(quote_id, key_prefix="detail"):
    quote = get_quote(quote_id)
    if not quote:
        return
    meta = parse_quote_meta(quote.get("memo", ""))
    totals = quote_financials(int(quote_id), include_only_priceable=True)
    disabled = str(quote.get("status")) == "삭제"
    with st.container(border=True):
        st.markdown("#### 견적 메모 / D.C")
        c1, c2, c3 = st.columns([2.4, 1.4, 1.1])
        with c1:
            note = st.text_input("견적 메모", value=str(meta.get("note", "") or ""), key=f"{key_prefix}_meta_note_{quote_id}", disabled=disabled)
        with c2:
            dc_label = st.text_input("D.C 문구", value=str(meta.get("dc_label", "") or ""), placeholder="예: 기종할인50% D.C", key=f"{key_prefix}_meta_dc_label_{quote_id}", disabled=disabled)
        with c3:
            dc_amount = st.number_input("D.C 금액", min_value=0, value=max(safe_int(meta.get("dc_amount", 0), 0), 0), step=1000, key=f"{key_prefix}_meta_dc_amount_{quote_id}", disabled=disabled)
        d1, d2 = st.columns([1, 3])
        with d1:
            if st.button("메모/D.C 저장", type="primary", use_container_width=True, key=f"{key_prefix}_meta_save_{quote_id}", disabled=disabled):
                ok, msg = update_quote_meta_fields(quote_id, note, dc_label, dc_amount)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
        with d2:
            dc_text = f"현재 D.C: {(totals.get('dc_label') or 'D.C')} -{money(totals.get('dc_amount', 0))}" if safe_int(totals.get("dc_amount", 0), 0) else "현재 D.C 없음"
            st.caption(f"{dc_text} · D.C는 견적 상세에서만 입력/수정합니다.")


def apply_pending_create_quote_fields():
    """OCR/요청서 PNG에서 읽은 팀/사람/날짜 값을 위젯 생성 전에 적용한다."""
    pending = st.session_state.pop("pending_create_quote_fields", None)
    if not isinstance(pending, dict):
        return
    for key, value in pending.items():
        if key in ["create_team_name", "create_person_name", "create_pickup_text", "create_return_text"]:
            st.session_state[key] = str(value or "")


def page_quote_create():
    init_current_quote_state()
    apply_pending_create_quote_fields()
    flash = st.session_state.pop("ocr_import_add_flash", "")
    st.header("견적서 만들기")
    if flash:
        st.success(flash)
    st.caption("* 필수 입력")
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([1, 1, 1.15, 1.15])
        with c1:
            team_name = st.text_input("팀", key="create_team_name", placeholder="팀 이름 입력")
        with c2:
            person_name = st.text_input("사람", key="create_person_name", placeholder="사람 이름 입력")
        with c3:
            pickup_raw = st.text_input("픽업 날짜", value="", help=date_input_help(), key="create_pickup_text", placeholder="픽업 날짜 입력")
        with c4:
            return_raw = st.text_input("반납 날짜", value="", help=date_input_help(), key="create_return_text", placeholder="반납 날짜 입력")
    pickup_date = try_normalize_date_text(pickup_raw)
    return_date = try_normalize_date_text(return_raw)
    date_error = ""
    if pickup_raw.strip() and not pickup_date:
        date_error = "픽업 날짜 형식을 확인하세요."
    elif return_raw.strip() and not return_date:
        date_error = "반납 날짜 형식을 확인하세요."
    elif pickup_date and return_date and return_date < pickup_date:
        date_error = "반납 날짜가 픽업 날짜보다 빠릅니다."
    if date_error:
        st.warning(date_error)
    elif pickup_date and return_date:
        st.caption(pricing_summary_text(pickup_date, return_date))

    render_ocr_import_panel(pickup_date or "", return_date or "")
    if st.session_state.get("ocr_import_issue_rows"):
        if ocr_import_issues_dialog is not None:
            ocr_import_issues_dialog()
        else:
            st.warning("반영한 상품 중 현재 날짜 기준 불가 또는 견적중인 상품이 있습니다.")
            st.dataframe(pd.DataFrame(st.session_state.get("ocr_import_issue_rows") or []), use_container_width=True, hide_index=True)

    search = st.text_input("상품 검색", key="create_product_search", placeholder="상품명 입력")
    st.subheader("상품 선택")
    try:
        st.caption(f"총 저장 상품 {count_products_fast(''):,}개")
    except Exception:
        pass
    page_size = 20
    if "create_product_page" not in st.session_state:
        st.session_state["create_product_page"] = 1
    reset_page_when_search_changes(search, "create_product_page", "create_product_search_tracker")
    page_df, total_count = load_products_page(search_text=search, page=st.session_state["create_product_page"], page_size=page_size)
    product_nos = tuple(page_df["product_no"].astype(str).tolist()) if not page_df.empty else tuple()
    status_map = bulk_product_status(product_nos, pickup_date or "", return_date or "", 0)
    cols = st.columns(4)
    for i, (_, row) in enumerate(page_df.iterrows()):
        product_no = str(row["product_no"])
        selected = product_no in st.session_state["selected_quote_items"]
        with cols[i % 4]:
            clicked, _, _, _ = render_product_tile(row, key_prefix=f"create_{st.session_state['create_product_page']}_{i}_{product_no}", pickup_date=pickup_date or None, return_date=return_date or None, selected=selected, select_label="선택 해제" if selected else "선택", show_select=True, status_info=status_map.get(product_no, {"reserved": 0, "pending": 0}))
            if clicked:
                if selected:
                    st.session_state["selected_quote_items"].pop(product_no, None)
                    st.session_state.pop(f"create_qty_{product_no}_value", None)
                else:
                    add_to_current_selection(row)
                st.rerun()
    pagination_controls(total_count, page_size, "create_product_page")
    st.divider()
    st.subheader("선택된 상품")
    selected_items = st.session_state["selected_quote_items"]
    if not selected_items:
        st.info("선택된 상품이 없습니다.")
        last_id = st.session_state.get("last_created_quote_id")
        if last_id and get_quote(last_id):
            st.divider()
            st.subheader("방금 만든 견적서 다운로드")
            render_quote_export_buttons(last_id, key_prefix="created_quote_export")
        return
    with st.container(border=True):
        st.markdown("**견적 정보**")
        info_cols = st.columns(4)
        info_cols[0].caption("팀"); info_cols[0].write(team_name or "-")
        info_cols[1].caption("사람"); info_cols[1].write(person_name or "-")
        info_cols[2].caption("픽업 날짜"); info_cols[2].write(pickup_date or "-")
        info_cols[3].caption("반납 날짜"); info_cols[3].write(return_date or "-")
        st.caption("상단 입력값과 자동으로 동기화됩니다. 수정은 상단 입력란에서 해주세요.")
    selected_codes = tuple(str(x) for x in selected_items.keys())
    selected_status = bulk_product_status(selected_codes, pickup_date or "", return_date or "", 0)
    selected_product_map = load_products_map(selected_codes)
    remove_codes = []
    supply_preview = 0
    overtime_preview = 0
    for product_no, item in list(selected_items.items()):
        product = selected_product_map.get(str(product_no)) or {}
        stock_qty = max(safe_int(item.get("stock_qty", product.get("qty", 1)), 1), 1)
        selected_items[product_no]["stock_qty"] = stock_qty
        state_reserved = safe_int(selected_status.get(product_no, {}).get("reserved", 0), 0)
        state_pending = safe_int(selected_status.get(product_no, {}).get("pending", 0), 0)
        priceable = not (state_reserved > 0 or state_pending > 0)
        with st.container(border=True):
            render_status_button_bar(product_no, pickup_date or None, return_date or None, key_prefix=f"selected_status_{product_no}", precomputed=selected_status.get(product_no, {"reserved": 0, "pending": 0}), total_qty_override=stock_qty)
            cc1, cc2, cc3, cc4, cc5 = st.columns([1.1, 2.7, 1.4, 1.4, 0.8])
            with cc1:
                show_thumb_from_values(item.get("thumbnail_path", ""), item.get("thumbnail_url", ""))
            with cc2:
                st.markdown(f"**{item.get('name', '')}**")
                st.caption(item.get("size_text", ""))
                st.caption(f"상품번호: {product_no}")
                st.caption(f"보유수량: {stock_qty}개")
                if not priceable:
                    st.caption("불가/견적중 상품은 견적 총액에서 제외됩니다.")
            with cc3:
                qty = render_quantity_stepper("수량", item.get("quantity", 1), stock_qty, f"create_qty_{product_no}")
                selected_items[product_no]["quantity"] = qty
            with cc4:
                price = st.number_input("단가", min_value=0, value=safe_int(item.get("unit_price", 0), 0), step=1000, key=f"create_price_{product_no}")
                selected_items[product_no]["unit_price"] = price
                base = calculate_line_total(qty, price)
                extra = calculate_overtime_amount(qty, price, pickup_date, return_date) if pickup_date and return_date else 0
                st.caption(f"상품금액: {money(base)}")
                if pickup_date and return_date:
                    st.caption(f"연박: {money(extra)}")
                if priceable:
                    supply_preview += base
                    overtime_preview += extra
            with cc5:
                if st.button("삭제", key=f"create_remove_{product_no}"):
                    remove_codes.append(product_no)
    for code in remove_codes:
        selected_items.pop(code, None)
        st.session_state.pop(f"create_qty_{code}_value", None)
        st.rerun()
    memo = st.text_input("견적 메모", key="create_memo")
    vat_preview = int(round((supply_preview + overtime_preview) * 0.1))
    total_preview = supply_preview + overtime_preview + vat_preview
    summary = f"공급가 {money(supply_preview)} / 연박 {money(overtime_preview)} / 부가세 {money(vat_preview)} / 총금액 {money(total_preview)}"
    st.markdown(f"### 합계: {summary}")
    st.caption("D.C는 견적 생성 후 상세 화면에서 입력/수정합니다.")
    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("견적서 만들기", type="primary", use_container_width=True):
            if not team_name.strip() or not person_name.strip():
                st.error("팀과 사람 이름을 모두 입력해야 합니다.")
            elif date_error or not pickup_date or not return_date:
                st.error(date_error or "픽업/반납 날짜를 입력해야 합니다.")
            else:
                quote_id = create_quote(combine_team_person(team_name, person_name), pickup_date, return_date, selected_items, memo)
                st.session_state["selected_quote_items"] = {}
                st.session_state["last_created_quote_id"] = quote_id
                st.success("견적서를 저장했습니다.")
                st.rerun()
    with c2:
        if st.button("선택 초기화", use_container_width=True):
            for code in list(st.session_state.get("selected_quote_items", {}).keys()):
                st.session_state.pop(f"create_qty_{code}_value", None)
            st.session_state["selected_quote_items"] = {}
            st.rerun()
    last_id = st.session_state.get("last_created_quote_id")
    if last_id and get_quote(last_id):
        st.divider()
        st.subheader("방금 만든 견적서 다운로드")
        render_quote_export_buttons(last_id, key_prefix="created_quote_export")


# -----------------------------
# 앱 시작
# -----------------------------

init_db()
st.set_page_config(page_title="프라비 렌탈 관리", layout="wide")
APP_BUILD = "request-png-import-v4"
require_app_password()

st.markdown("""
<style>
.block-container {padding-top: 3rem; max-width: 1500px;}
h1, h2, h3 {line-height:1.25 !important; overflow:visible !important; padding-top:.12em !important; padding-bottom:.08em !important;}
[data-testid="stSidebar"] {background-color: #fbfbfb;}
.sidebar-brand {font-size:52px; font-weight:950; letter-spacing:-1.5px; color:#111; margin:0 0 2px 0; line-height:1.02;}
.sidebar-subtitle {font-size:14px; color:#555; margin:0 0 30px 0;}
.sidebar-nav {display:flex; flex-direction:column; gap:14px; margin:18px 0 36px 0;}
.sidebar-nav-active {display:block; padding:15px 16px; border:1.5px solid #111827; border-radius:18px; text-align:center; color:#fff !important; font-weight:850; background:#111827; box-shadow:0 8px 18px rgba(17,24,39,.10); margin:0 0 14px 0;}
.sidebar-nav-spacer {height:8px;}
[data-testid="stSegmentedControl"] {margin-bottom:18px;}
[data-testid="stSegmentedControl"] button {border-radius:14px !important; min-height:44px !important; font-weight:750 !important;}
.filter-label {font-size:15px; font-weight:700; color:#222; margin: 0 0 6px 0;}
.small-muted {color:#777; font-size:13px;}
.focus-guard {position:fixed; left:-9999px; top:-9999px; opacity:0; width:1px; height:1px; pointer-events:none;}
.badge {
    display:inline-flex;
    align-items:center;
    justify-content:center;
    padding:6px 12px;
    border-radius:999px;
    font-size:13px;
    font-weight:800;
    margin:0 4px 6px 0;
    white-space:nowrap;
    word-break:keep-all;
    line-height:1.1;
}
.product-card-inner {
    border:none;
    border-radius:24px;
    padding:0 0 24px 0;
    background:#fff;
    min-height:0;
    transition:all .15s ease;
}
.product-card-inner:hover .thumb-box {
    box-shadow:0 8px 22px rgba(16,24,40,.08);
}
.badge-selected {
    background:#EAF3FF !important;
    color:#1A73E8 !important;
}
.selected-badge-row {height:auto; min-height:0; display:flex; align-items:center; margin:0 0 10px 0;}
.thumb-box {
    width:100%;
    height:230px;
    background:#f3f3f3;
    border-radius:18px;
    display:flex;
    align-items:center;
    justify-content:center;
    overflow:hidden;
    margin:16px 0 18px 0;
}
.thumb-box img {
    max-width:100%;
    max-height:100%;
    object-fit:contain;
}
.no-img {color:#999; font-size:14px;}
.product-name {font-weight:900; font-size:20px; line-height:1.35; margin-bottom:10px; color:#242633; word-break:keep-all; overflow:visible;}
.product-meta {color:#717680; font-size:14px; line-height:1.6; word-break:keep-all;}
.status-placeholder {height:42px;}
.stButton > button {
    border-radius:16px;
    min-height:42px;
    transition:all .15s ease;
    font-weight:700;
    white-space:nowrap !important;
    word-break:keep-all !important;
    line-height:1.15 !important;
    overflow:hidden;
    text-overflow:ellipsis;
}
.stButton > button:hover {
    filter:brightness(.96);
    transform:translateY(-1px);
}
.pagination-wrap {margin: 20px 0 34px 0;}
.page-indicator {text-align:center; padding-top:10px; font-size:14px; font-weight:400; color:#2D3142; white-space:nowrap;}
.pagination-wrap .stButton > button {font-size:14px !important; font-weight:400 !important; min-height:38px !important; border-radius:14px !important;}
.dialog-scroll {max-height:800px; overflow-y:auto; padding-right:8px;}
[data-testid="column"] img {border-radius:18px; object-fit:contain; background:#f3f3f3;}
.native-card-gap {height:16px;}
.native-product-name {margin-top:14px;}
.native-bottom-meta {margin-bottom:26px;}
.qty-display {text-align:center; padding:10px 4px; font-size:18px; font-weight:800; background:#f5f6f8; border-radius:12px;}
.calendar-title {text-align:center; font-size:23px; font-weight:900; padding:8px 0;}
.calendar-weekday {text-align:center; font-weight:800; color:#555; padding:6px 0;}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.product-card-inner) {
    border-radius:24px !important;
    background:#fff !important;
    overflow:hidden !important;
    transition:all .15s ease !important;
}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.product-card-inner):hover {
    border-color:#C9CED6 !important;
    box-shadow:0 10px 28px rgba(16,24,40,.08) !important;
}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.selected-card-marker) {
    border:4px solid #1A73E8 !important;
    box-shadow:0 0 0 1px rgba(26,115,232,.10), 0 10px 28px rgba(26,115,232,.12) !important;
}
div[data-testid="stVerticalBlockBorderWrapper"] {border-radius:22px !important;}

.quote-detail-head {
    padding: 4px 0 14px 0;
}
.quote-status-line {
    margin-bottom: 10px;
}
.quote-title-line {
    font-size: 30px;
    font-weight: 900;
    line-height: 1.25;
    letter-spacing: -0.8px;
    color: #242633;
    word-break: keep-all;
}
.quote-meta-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 18px;
    margin-top: 10px;
    color: #667085;
    font-size: 14px;
}
.quote-meta-grid span {
    margin-right: 6px;
    color: #8A8F98;
}
.quote-meta-grid strong {
    color: #535862;
    font-weight: 750;
}
.quote-pricing-line {
    display: inline-flex;
    margin-top: 14px;
    padding: 8px 12px;
    border-radius: 999px;
    background: #F3F4F6;
    color: #667085;
    font-size: 14px;
    font-weight: 650;
}
</style>
""", unsafe_allow_html=True)

st.sidebar.markdown("""
<div class='sidebar-brand'>프라비</div>
<div class='sidebar-subtitle'>렌탈 재고·견적 관리</div>
""", unsafe_allow_html=True)

menu_options = ["견적서 만들기", "견적서 조회/반납 기록"]

# HTML 링크 href='?menu=...' 방식은 Streamlit Cloud에서 새 세션처럼 동작해
# 메뉴를 누를 때마다 비밀번호 화면이 다시 나올 수 있다.
# 따라서 메뉴 전환은 URL 이동 없이 session_state만 바꾸는 st.button 방식으로 처리한다.
if st.session_state.get("menu") not in menu_options:
    st.session_state["menu"] = "견적서 만들기"

for opt in menu_options:
    if st.session_state["menu"] == opt:
        st.sidebar.markdown(f"<div class='sidebar-nav-active'>{opt}</div>", unsafe_allow_html=True)
    else:
        if st.sidebar.button(opt, key=f"nav_btn_{opt}", use_container_width=True):
            st.session_state["menu"] = opt
            st.session_state["combined_detail_id"] = None
            st.session_state["sync_dialog_open"] = False
            st.session_state["sync_fallback_open"] = False
            st.session_state["holiday_dialog_open"] = False
            st.rerun()

menu = st.session_state["menu"]
scroll_to_top_once()
st.sidebar.markdown("<div class='sidebar-nav-spacer'></div>", unsafe_allow_html=True)

st.sidebar.divider()
st.sidebar.caption(f"오늘 {date.today().strftime('%Y.%m.%d')}")
if st.sidebar.button("휴일 캘린더", use_container_width=True, key="open_holiday_calendar"):
    st.session_state["holiday_draft_dates"] = sorted(get_holiday_dates())
    if holiday_calendar_dialog is not None:
        holiday_calendar_dialog()
    else:
        st.session_state["holiday_dialog_open"] = True

# st.dialog의 X 닫기는 session_state를 바꾸지 않으므로, open 상태를 계속 들고 있으면
# 다른 버튼을 눌렀을 때 휴일 캘린더가 다시 뜬다. 기본 dialog는 버튼 클릭 시에만 열고,
# dialog 미지원 환경에서만 fallback expander 상태를 사용한다.
if holiday_calendar_dialog is None and st.session_state.get("holiday_dialog_open"):
    with st.sidebar.expander("휴일 캘린더", expanded=True):
        render_holiday_calendar_panel()

if st.sidebar.button("상품 동기화", use_container_width=True, key="open_sync_dialog"):
    st.session_state["sync_dialog_open"] = True

if st.session_state.get("sync_dialog_open"):
    if sync_dialog is not None:
        sync_dialog()
    else:
        st.session_state["sync_fallback_open"] = True

if st.session_state.get("sync_fallback_open"):
    with st.sidebar.expander("상품 동기화", expanded=True):
        render_sync_panel()
        if st.button("닫기", key="sync_fallback_close"):
            st.session_state["sync_fallback_open"] = False
            st.session_state["sync_dialog_open"] = False
            st.rerun()

last_sync = get_meta("last_sync_at", "아직 없음")
st.sidebar.caption(f"최근 동기화: {last_sync}")

if menu == "견적서 만들기":
    page_quote_create()
elif menu == "견적서 조회/반납 기록":
    page_quote_history()

