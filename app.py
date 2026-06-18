import os
import re
import time
import math
import base64
from html import escape
from io import BytesIO
from datetime import date, datetime
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from supabase import create_client


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
    existing_rows = table_all("products", select="product_no,price")
    existing_price = {str(r.get("product_no")): safe_int(r.get("price", 0), 0) for r in existing_rows}
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
            "updated_at": datetime.now().isoformat(),
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
    data_query = apply_search(data_query).order("product_no", desc=True)
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
    """YYYY-MM-DD 또는 YYYY/MM/DD를 date 객체로 변환한다."""
    text = str(value or "").strip().replace("/", "-")
    return datetime.strptime(text, "%Y-%m-%d").date()


# -----------------------------
# 필터 / 상태
# -----------------------------

def status_filter_control(label, options, key):
    if key not in st.session_state:
        st.session_state[key] = ""
    st.caption(label)
    cols = st.columns(len(options) + 1)
    all_clicked = cols[0].button("전체", key=f"{key}_all", use_container_width=True)
    if all_clicked:
        st.session_state[key] = ""
        st.rerun()
    for i, opt in enumerate(options, start=1):
        clicked = cols[i].button(opt, key=f"{key}_{opt}", use_container_width=True)
        if clicked:
            st.session_state[key] = opt
            st.rerun()
    return st.session_state.get(key)


def update_quote_header(quote_id, team_name, pickup_date, return_date):
    quote = get_quote(quote_id)
    if not quote:
        return False, ["견적서를 찾지 못했습니다."]

    if str(return_date) < str(pickup_date):
        return False, ["반납 날짜가 픽업 날짜보다 빠릅니다."]

    if quote.get("status") in ["확정", "부분반납"]:
        failures = check_quote_availability(quote_id, pickup_date=str(pickup_date), return_date=str(return_date), exclude_self=True)
        if failures:
            return False, failures

    client = supabase_client()
    client.table("quotes").update({
        "team_name": str(team_name),
        "pickup_date": str(pickup_date),
        "return_date": str(return_date),
        "updated_at": datetime.now().isoformat(),
    }).eq("quote_id", int(quote_id)).execute()

    # 활성 대여만 견적서 날짜와 같이 조정
    for status in ACTIVE_RENTAL_STATUSES:
        client.table("rentals").update({
            "team_name": str(team_name),
            "pickup_date": str(pickup_date),
            "return_date": str(return_date),
            "updated_at": datetime.now().isoformat(),
        }).eq("quote_id", int(quote_id)).eq("status", status).execute()

    return True, []


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


def update_quote_totals(quote_id):
    items = load_quote_items_df(quote_id)
    subtotal = int(items["line_total"].fillna(0).astype(int).sum()) if not items.empty else 0
    vat = int(round(subtotal * 0.1))
    total = subtotal + vat
    supabase_client().table("quotes").update({
        "subtotal": subtotal,
        "vat": vat,
        "total": total,
        "updated_at": datetime.now().isoformat(),
    }).eq("quote_id", int(quote_id)).execute()
    clear_data_cache()


def create_quote(team_name, pickup_date, return_date, selected_items, memo=""):
    quote_no = generate_quote_no()
    client = supabase_client()
    res = client.table("quotes").insert({
        "quote_no": quote_no,
        "team_name": team_name,
        "pickup_date": str(pickup_date),
        "return_date": str(return_date),
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
    for product_no, item in selected_items.items():
        p = get_product(product_no)
        if not p:
            continue
        qty = safe_int(item.get("quantity", 1), 1)
        unit_price = safe_int(item.get("unit_price", p.get("price", 0)), 0)
        line_total = qty * unit_price
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
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        })

    if rows:
        client.table("quote_items").insert(rows).execute()

    update_quote_totals(quote_id)
    clear_data_cache()
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
        return pd.DataFrame(columns=["id", "quote_id", "product_no", "product_name", "size_text", "thumbnail_path", "thumbnail_url", "quantity", "unit_price", "line_total"])

    df = fill_quote_item_image_fallbacks(df)

    if "local_thumbnail_path" in df.columns:
        df["thumbnail_path"] = df["local_thumbnail_path"].fillna("")
    elif "thumbnail_path" not in df.columns:
        df["thumbnail_path"] = ""

    if "thumbnail_url" not in df.columns:
        df["thumbnail_url"] = ""

    cols = ["id", "quote_id", "product_no", "product_name", "size_text", "thumbnail_path", "thumbnail_url", "quantity", "unit_price", "line_total"]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df[cols].reset_index(drop=True)


def add_item_to_quote(quote_id, product_no, quantity=1):
    p = get_product(product_no)
    if not p:
        return False, "상품을 찾지 못했습니다."

    client = supabase_client()
    existing = client.table("quote_items").select("id,quantity").eq("quote_id", int(quote_id)).eq("product_no", str(product_no)).limit(1).execute().data or []
    unit_price = safe_int(p.get("price", 0), 0)

    if existing:
        item_id = int(existing[0]["id"])
        new_qty = safe_int(existing[0].get("quantity", 1), 1) + safe_int(quantity, 1)
        client.table("quote_items").update({
            "quantity": new_qty,
            "unit_price": unit_price,
            "line_total": new_qty * unit_price,
            "updated_at": datetime.now().isoformat(),
        }).eq("id", item_id).execute()
    else:
        qty = safe_int(quantity, 1)
        client.table("quote_items").insert({
            "quote_id": int(quote_id),
            "product_no": str(product_no),
            "product_name": p.get("name", ""),
            "size_text": p.get("size_text", ""),
            "thumbnail_url": p.get("thumbnail_url", ""),
            "local_thumbnail_path": p.get("local_thumbnail_path", ""),
            "quantity": qty,
            "unit_price": unit_price,
            "line_total": qty * unit_price,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }).execute()

    update_quote_totals(quote_id)
    clear_data_cache()
    return True, "상품을 추가했습니다."


def delete_quote_item(item_id, quote_id):
    supabase_client().table("quote_items").delete().eq("id", int(item_id)).eq("quote_id", int(quote_id)).execute()
    update_quote_totals(quote_id)
    clear_data_cache()


def update_quote_item(item_id, quantity, unit_price):
    qty = max(safe_int(quantity, 1), 1)
    price = max(safe_int(unit_price, 0), 0)
    client = supabase_client()
    item = client.table("quote_items").select("quote_id").eq("id", int(item_id)).limit(1).execute().data or []
    client.table("quote_items").update({
        "quantity": qty,
        "unit_price": price,
        "line_total": qty * price,
        "updated_at": datetime.now().isoformat(),
    }).eq("id", int(item_id)).execute()
    if item:
        update_quote_totals(int(item[0]["quote_id"]))


def check_quote_availability(quote_id, pickup_date=None, return_date=None, exclude_self=True):
    quote = get_quote(quote_id)
    if not quote:
        return ["견적서를 찾지 못했습니다."]

    pickup = pickup_date or quote["pickup_date"]
    ret = return_date or quote["return_date"]
    items = load_quote_items_df(quote_id)
    failures = []

    for _, item in items.iterrows():
        exclude_id = quote_id if exclude_self else None
        available_qty, total_qty, reserved = get_available_qty(item["product_no"], pickup, ret, exclude_quote_id=exclude_id)
        needed = safe_int(item["quantity"], 1)
        if needed > available_qty:
            failures.append(f'{item["product_name"]} / 필요 {needed}개 / 가능 {available_qty}개')

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
            "pickup_date": quote["pickup_date"],
            "return_date": quote["return_date"],
            "quantity": safe_int(item["quantity"], 1),
            "status": "대여중",
            "memo": "",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        })
    if rental_rows:
        client.table("rentals").insert(rental_rows).execute()

    client.table("quotes").update({"status": "확정", "updated_at": datetime.now().isoformat()}).eq("quote_id", int(quote_id)).execute()
    clear_data_cache()
    return True, []


def delete_quote(quote_id):
    client = supabase_client()
    client.table("quotes").update({"status": "삭제", "updated_at": datetime.now().isoformat()}).eq("quote_id", int(quote_id)).execute()
    client.table("rentals").update({"status": "삭제", "updated_at": datetime.now().isoformat()}).eq("quote_id", int(quote_id)).execute()
    clear_data_cache()


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


def get_rental_item(rental_id):
    res = supabase_client().table("rentals").select("*").eq("id", int(rental_id)).limit(1).execute()
    data = res.data or []
    return dict(data[0]) if data else None


def update_rental_item_dates(rental_id, new_pickup, new_return):
    rental = get_rental_item(rental_id)
    if not rental:
        return False, ["대여 상품을 찾지 못했습니다."]
    if rental.get("status") == "반납완료":
        return False, ["이미 반납완료된 상품은 날짜를 수정하지 않습니다."]
    if str(new_return) < str(new_pickup):
        return False, ["반납 날짜가 픽업 날짜보다 빠릅니다."]

    product = get_product(rental["product_no"]) or {}
    total_qty = max(safe_int(product.get("qty", 1), 1), 1)
    qty = max(safe_int(rental.get("quantity", 1), 1), 1)
    conflicts = rentals_query(
        product_no=rental["product_no"],
        pickup_date=new_pickup,
        return_date=new_return,
        active_only=True,
        exclude_rental_id=int(rental_id),
    )
    reserved = sum(safe_int(r.get("quantity", 0), 0) for r in conflicts)
    if total_qty - reserved < qty:
        return False, [f"{rental['product_name']}은 해당 날짜에 다른 대여와 겹칩니다."]

    supabase_client().table("rentals").update({
        "pickup_date": str(new_pickup),
        "return_date": str(new_return),
        "updated_at": datetime.now().isoformat(),
    }).eq("id", int(rental_id)).execute()
    update_quote_date_range_from_rentals(int(rental["quote_id"]))
    refresh_quote_return_status(int(rental["quote_id"]))
    return True, []


def update_quote_dates(quote_id, new_pickup, new_return):
    quote = get_quote(quote_id)
    if not quote:
        return False, ["견적서를 찾지 못했습니다."]

    failures = check_quote_availability(quote_id, pickup_date=str(new_pickup), return_date=str(new_return), exclude_self=True)
    if failures:
        return False, failures

    client = supabase_client()
    client.table("quotes").update({
        "pickup_date": str(new_pickup),
        "return_date": str(new_return),
        "updated_at": datetime.now().isoformat(),
    }).eq("quote_id", int(quote_id)).execute()

    for status in ACTIVE_RENTAL_STATUSES:
        client.table("rentals").update({
            "pickup_date": str(new_pickup),
            "return_date": str(new_return),
            "updated_at": datetime.now().isoformat(),
        }).eq("quote_id", int(quote_id)).eq("status", status).execute()

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
    header_h = 310
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
    qno_text = f"견적번호: {quote['quote_no']}"
    created_text = f"작성일: {quote['created_at'] or ''}"
    qno_w, _ = text_size(draw, qno_text, font_m)
    created_w, _ = text_size(draw, created_text, font_s)
    draw.text((width - margin - qno_w, info_y), qno_text, font=font_m, fill=(60, 60, 60))
    draw.text((width - margin - created_w, info_y + 44), created_text, font=font_s, fill=(100, 100, 100))

    start_y = header_h
    for order, (_, item) in enumerate(items.iterrows()):
        col = order % cols
        row = order // cols
        x = margin + col * (card_w + gap_x)
        y = start_y + row * (card_h + gap_y)

        draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=18, outline=(220, 220, 220), width=2, fill=(252, 252, 252))

        unavailable_label = False
        try:
            avail, _, _ = get_available_qty(item["product_no"], quote["pickup_date"], quote["return_date"], exclude_quote_id=quote_id)
            unavailable_label = avail <= 0 and quote.get("status") == "견적중"
        except Exception:
            unavailable_label = False
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
        draw.text((text_x, text_y + 88), f"{qty}EA x {money(unit)}", font=font_s, fill=(80, 80, 80))
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
            st.caption(str(row.get("created_at", "")))
            st.markdown(status_badge(str(row['status']), status_kind(row['status'])), unsafe_allow_html=True)
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
            st.caption(str(row.get("created_at", "")))
            st.markdown(status_badge(str(row['status']), status_kind(row['status'])), unsafe_allow_html=True)
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

def render_sync_panel():
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

    if st.button("닫기", key="sync_dialog_close"):
        st.session_state["sync_dialog_open"] = False
        st.session_state["sync_fallback_open"] = False
        st.rerun()


if hasattr(st, "dialog"):
    @st.dialog("상품 동기화")
    def sync_dialog():
        render_sync_panel()
else:
    sync_dialog = None



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
        st.markdown("<div class='dialog-scroll'>", unsafe_allow_html=True)
        st.caption(f"{quote['quote_no']} / {quote['team_name']} / {quote['pickup_date']} ~ {quote['return_date']}")
        search = st.text_input("상품 검색", key=f"dialog_add_search_{quote_id}")
        page_size = 9
        page_key = f"dialog_add_page_{quote_id}"
        if page_key not in st.session_state:
            st.session_state[page_key] = 1
        reset_page_when_search_changes(
            search,
            page_key,
            f"dialog_add_search_tracker_{quote_id}",
        )
        page_df, total_count = load_products_page(search_text=search, page=st.session_state[page_key], page_size=page_size)
        total_pages = max(1, math.ceil(total_count / page_size))
        if st.session_state[page_key] > total_pages:
            st.session_state[page_key] = total_pages
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
        pagination_controls(total_count, page_size, f"dialog_add_page_{quote_id}")
        st.markdown("</div>", unsafe_allow_html=True)

    @st.dialog("견적 기본 정보 수정")
    def quote_header_dialog(quote_id):
        quote = get_quote(quote_id)
        if not quote:
            st.error("견적서를 찾지 못했습니다.")
            return
        st.caption(f"{quote['quote_no']} / 현재 팀명: {quote['team_name']}")
        st.markdown("<input class='focus-guard' autofocus />", unsafe_allow_html=True)
        edit_team = st.text_input("팀 이름", value=str(quote["team_name"] or ""), key=f"header_dialog_team_{quote_id}")
        c1, c2 = st.columns(2)
        with c1:
            edit_pickup = st.date_input("픽업 날짜", value=parse_date_text(quote["pickup_date"]), key=f"header_dialog_pickup_{quote_id}")
        with c2:
            edit_return = st.date_input("반납 날짜", value=parse_date_text(quote["return_date"]), key=f"header_dialog_return_{quote_id}")
        if st.button("수정 저장", type="primary", use_container_width=True, key=f"header_dialog_save_{quote_id}"):
            if not edit_team.strip():
                st.error("팀 이름을 입력해야 합니다.")
            else:
                ok, failures = update_quote_header(quote_id, edit_team.strip(), edit_pickup, edit_return)
                if ok:
                    st.success("견적 정보를 수정했습니다.")
                    st.rerun()
                else:
                    st.error("수정할 수 없습니다.")
                    for f in failures:
                        st.write(f"- {f}")

    @st.dialog("대여 날짜 수정")
    def rental_date_dialog(quote_id):
        quote = get_quote(quote_id)
        if not quote:
            st.error("대여 기록을 찾지 못했습니다.")
            return
        st.caption(f"{quote['quote_no']} / {quote['team_name']}")
        st.markdown("<input class='focus-guard' autofocus />", unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            new_pickup = st.date_input("새 픽업 날짜", value=parse_date_text(quote["pickup_date"]), key=f"date_dialog_pickup_{quote_id}")
        with c2:
            new_return = st.date_input("새 반납 날짜", value=parse_date_text(quote["return_date"]), key=f"date_dialog_return_{quote_id}")
        if st.button("날짜 수정 저장", type="primary", use_container_width=True, key=f"date_dialog_save_{quote_id}"):
            if new_return < new_pickup:
                st.error("반납 날짜가 픽업 날짜보다 빠릅니다.")
            else:
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
        st.markdown("<input class='focus-guard' autofocus />", unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            new_pickup = st.date_input("새 픽업 날짜", value=parse_date_text(rental["pickup_date"]), key=f"rental_item_pickup_{rental_id}")
        with c2:
            new_return = st.date_input("새 반납 날짜", value=parse_date_text(rental["return_date"]), key=f"rental_item_return_{rental_id}")
        if st.button("날짜 수정 저장", type="primary", use_container_width=True, key=f"rental_item_date_save_{rental_id}"):
            ok, failures = update_rental_item_dates(int(rental_id), new_pickup, new_return)
            if ok:
                st.success("상품 대여 날짜를 수정했습니다.")
                st.rerun()
            else:
                st.error("날짜를 수정할 수 없습니다.")
                for f in failures:
                    st.write(f"- {f}")
else:
    availability_detail_dialog = None
    quote_add_product_dialog = None
    rental_date_dialog = None
    rental_item_date_dialog = None
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
        }


def page_quote_create():
    init_current_quote_state()
    st.header("견적서 만들기")

    c1, c2, c3 = st.columns(3)
    with c1:
        team_name = st.text_input("팀 이름", key="create_team_name")
    with c2:
        pickup_date = st.date_input("픽업 날짜", value=date.today(), key="create_pickup")
    with c3:
        return_date = st.date_input("반납 날짜", value=date.today(), key="create_return")

    if return_date < pickup_date:
        st.error("반납 날짜가 픽업 날짜보다 빠릅니다.")
        return

    search = st.text_input("상품 검색", key="create_product_search")

    st.subheader("상품 선택")

    page_size = 20
    if "create_product_page" not in st.session_state:
        st.session_state["create_product_page"] = 1
    reset_page_when_search_changes(
        search,
        "create_product_page",
        "create_product_search_tracker",
    )
    page_df, total_count = load_products_page(search_text=search, page=st.session_state["create_product_page"], page_size=page_size)
    total_pages = max(1, math.ceil(total_count / page_size))
    if st.session_state["create_product_page"] > total_pages:
        st.session_state["create_product_page"] = total_pages
        page_df, total_count = load_products_page(search_text=search, page=st.session_state["create_product_page"], page_size=page_size)

    product_nos = tuple(page_df["product_no"].astype(str).tolist()) if not page_df.empty else tuple()
    status_map = bulk_product_status(product_nos, str(pickup_date), str(return_date), 0)

    cols = st.columns(4)
    for i, (_, row) in enumerate(page_df.iterrows()):
        product_no = str(row["product_no"])
        selected = product_no in st.session_state["selected_quote_items"]
        with cols[i % 4]:
            clicked, _, _, _ = render_product_tile(
                row,
                key_prefix=f"create_{st.session_state['create_product_page']}_{i}_{product_no}",
                pickup_date=pickup_date,
                return_date=return_date,
                selected=selected,
                select_label="선택 해제" if selected else "선택",
                show_select=True,
                status_info=status_map.get(product_no, {"reserved": 0, "pending": 0}),
            )
            if clicked:
                if selected:
                    st.session_state["selected_quote_items"].pop(product_no, None)
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
        remove_codes = []
        for product_no, item in list(selected_items.items()):
            with st.container(border=True):
                cc1, cc2, cc3, cc4, cc5 = st.columns([1.1, 3, 1, 1.3, 0.8])
                with cc1:
                    show_thumb_from_values(item.get("thumbnail_path", ""), item.get("thumbnail_url", ""))
                with cc2:
                    st.markdown(f"**{item.get('name', '')}**")
                    st.caption(item.get("size_text", ""))
                    st.caption(f"상품번호: {product_no}")
                with cc3:
                    qty = st.number_input("수량", min_value=1, value=safe_int(item.get("quantity", 1), 1), key=f"create_qty_{product_no}")
                    selected_items[product_no]["quantity"] = qty
                with cc4:
                    price = st.number_input("단가", min_value=0, value=safe_int(item.get("unit_price", 0), 0), step=1000, key=f"create_price_{product_no}")
                    selected_items[product_no]["unit_price"] = price
                    st.caption(f"금액: {money(qty * price)}")
                with cc5:
                    if st.button("삭제", key=f"create_remove_{product_no}"):
                        remove_codes.append(product_no)

        for code in remove_codes:
            selected_items.pop(code, None)
            st.rerun()

        subtotal = sum(safe_int(v.get("quantity", 1), 1) * safe_int(v.get("unit_price", 0), 0) for v in selected_items.values())
        vat = int(round(subtotal * 0.1))
        total = subtotal + vat
        st.markdown(f"### 합계: 공급가 {money(subtotal)} / 부가세 {money(vat)} / 총금액 {money(total)}")

        memo = st.text_input("견적 메모", key="create_memo")
        c1, c2 = st.columns([1, 1])
        with c1:
            if st.button("견적서 만들기", type="primary", use_container_width=True):
                if not team_name.strip():
                    st.error("팀 이름을 입력해야 합니다.")
                else:
                    quote_id = create_quote(team_name.strip(), pickup_date, return_date, selected_items, memo)
                    st.session_state["selected_quote_items"] = {}
                    st.session_state["last_created_quote_id"] = quote_id
                    st.success("견적서를 저장했습니다.")
                    st.rerun()
        with c2:
            if st.button("선택 초기화", use_container_width=True):
                st.session_state["selected_quote_items"] = {}
                st.rerun()

    last_id = st.session_state.get("last_created_quote_id")
    if last_id:
        st.divider()
        st.subheader("방금 만든 견적서 다운로드")
        q = get_quote(last_id)
        if q:
            png_bytes = make_quote_image_bytes(last_id)
            pdf_bytes = make_quote_pdf_bytes(last_id)
            d1, d2 = st.columns(2)
            with d1:
                st.download_button("PNG 다운로드", data=png_bytes, file_name=quote_filename(last_id, "png"), mime="image/png", use_container_width=True)
            with d2:
                st.download_button("PDF 다운로드", data=pdf_bytes, file_name=quote_filename(last_id, "pdf"), mime="application/pdf", use_container_width=True)


# -----------------------------
# 견적 상세 공통
# -----------------------------

def render_quote_detail(quote_id, allow_edit=True, key_prefix="detail", mode="quote"):
    quote = get_quote(quote_id)
    if not quote:
        st.error("견적서를 찾지 못했습니다.")
        return

    items = load_quote_items_df(quote_id)

    title_cols = st.columns([4, 3])
    with title_cols[0]:
        st.markdown(f"### {quote['quote_no']} / {quote['team_name']}")
        st.caption(f"상태: {quote['status']} / 대여일: {quote['pickup_date']} ~ {quote['return_date']} / 총액: {money(quote['total'])}")
    with title_cols[1]:
        if mode == "rental":
            b1, b2 = st.columns(2)
            with b1:
                if quote["status"] in ["확정", "부분반납"] and st.button("날짜 수정", use_container_width=True, key=f"{key_prefix}_top_date_{quote_id}"):
                    if rental_date_dialog is not None:
                        rental_date_dialog(quote_id)
            with b2:
                if quote["status"] != "삭제" and st.button("삭제", use_container_width=True, key=f"{key_prefix}_top_delete_{quote_id}"):
                    delete_quote(quote_id)
                    st.session_state["rental_detail_id"] = None
                    st.success("삭제 처리했습니다.")
                    st.rerun()
        else:
            buttons = st.columns(4)
            with buttons[0]:
                if allow_edit and quote["status"] == "견적중" and st.button("정보 수정", use_container_width=True, key=f"{key_prefix}_top_edit_{quote_id}"):
                    if quote_header_dialog is not None:
                        quote_header_dialog(quote_id)
            with buttons[1]:
                if allow_edit and quote["status"] == "견적중" and st.button("제품 추가", use_container_width=True, key=f"{key_prefix}_top_add_{quote_id}"):
                    if quote_add_product_dialog is not None:
                        quote_add_product_dialog(quote_id)
            with buttons[2]:
                if quote["status"] == "견적중" and st.button("확정", type="primary", use_container_width=True, key=f"{key_prefix}_top_confirm_{quote_id}"):
                    ok, failures = confirm_quote(quote_id)
                    if ok:
                        st.success("견적서를 확정하고 대여 기록으로 넘겼습니다.")
                        st.rerun()
                    else:
                        st.error("확정할 수 없습니다.")
                        for f in failures:
                            st.write(f"- {f}")
            with buttons[3]:
                if quote["status"] != "삭제" and st.button("삭제", use_container_width=True, key=f"{key_prefix}_top_delete_{quote_id}"):
                    delete_quote(quote_id)
                    st.session_state["quote_detail_id"] = None
                    st.success("삭제 처리했습니다. 일반 목록에서는 보이지 않습니다.")
                    st.rerun()

    c1, c2 = st.columns(2)
    with c1:
        png_bytes = make_quote_image_bytes(quote_id)
        st.download_button("견적서 PNG 다운로드", png_bytes, quote_filename(quote_id, "png"), "image/png", use_container_width=True, key=f"{key_prefix}_png_{quote_id}")
    with c2:
        pdf_bytes = make_quote_pdf_bytes(quote_id)
        st.download_button("견적서 PDF 다운로드", pdf_bytes, quote_filename(quote_id, "pdf"), "application/pdf", use_container_width=True, key=f"{key_prefix}_pdf_{quote_id}")

    st.subheader("견적 상품")
    if items.empty:
        st.info("상품이 없습니다.")
    else:
        for _, item in items.iterrows():
            with st.container(border=True):
                c1, c2, c3, c4, c5 = st.columns([1.1, 3, 1, 1.3, 0.8])
                with c1:
                    show_thumb_from_values(item.get("thumbnail_path", ""), "")
                with c2:
                    st.markdown(f"**{item['product_name']}**")
                    st.caption(item.get("size_text", ""))
                    st.caption(f"상품번호: {item['product_no']}")
                if allow_edit and quote["status"] == "견적중":
                    with c3:
                        qty = st.number_input("수량", min_value=1, value=safe_int(item["quantity"], 1), key=f"{key_prefix}_qty_{item['id']}")
                    with c4:
                        unit = st.number_input("단가", min_value=0, value=safe_int(item["unit_price"], 0), step=1000, key=f"{key_prefix}_price_{item['id']}")
                        st.caption(f"금액: {money(qty * unit)}")
                    if safe_int(qty, 1) != safe_int(item["quantity"], 1) or safe_int(unit, 0) != safe_int(item["unit_price"], 0):
                        update_quote_item(item["id"], qty, unit)
                        st.rerun()
                    with c5:
                        if st.button("삭제", key=f"{key_prefix}_delete_item_{item['id']}"):
                            delete_quote_item(item["id"], quote_id)
                            st.rerun()
                else:
                    with c3:
                        st.write(f"수량: {item['quantity']}")
                    with c4:
                        st.write(f"단가: {money(item['unit_price'])}")
                        st.write(f"금액: {money(item['line_total'])}")


# -----------------------------
# 페이지: 견적서 조회
# -----------------------------

def page_quote_list():
    st.header("견적서 조회")

    detail_id = st.session_state.get("quote_detail_id")
    if detail_id:
        if st.button("‹ 목록으로", key="quote_back"):
            st.session_state["quote_detail_id"] = None
            st.rerun()
        render_quote_detail(int(detail_id), allow_edit=True, key_prefix="quote_detail_page")
        return

    quotes = load_quotes_df(include_deleted=False)
    if quotes.empty:
        st.info("저장된 견적서가 없습니다.")
        return

    status_filter = status_filter_control("상태 필터", ["견적중", "확정", "부분반납", "반납완료"], "quote_status_filter_v4")
    filtered = quotes[quotes["status"] == status_filter].copy() if status_filter else quotes.copy()

    search = st.text_input("팀명 / 견적번호 / 상품 요약 검색", key="quote_search")
    if search:
        s = search.lower()
        filtered = filtered[
            filtered["team_name"].astype(str).str.lower().str.contains(s, na=False) |
            filtered["quote_no"].astype(str).str.lower().str.contains(s, na=False) |
            filtered["상품요약"].astype(str).str.lower().str.contains(s, na=False)
        ]

    if filtered.empty:
        st.info("조건에 맞는 견적서가 없습니다.")
        return

    filtered = filtered.reset_index(drop=True)
    page_size = 10
    if "quote_list_page" not in st.session_state:
        st.session_state["quote_list_page"] = 1
    total_pages = max(1, math.ceil(len(filtered) / page_size))
    st.session_state["quote_list_page"] = min(max(1, st.session_state["quote_list_page"]), total_pages)
    start = (st.session_state["quote_list_page"] - 1) * page_size
    end = start + page_size
    page_df = filtered.iloc[start:end]

    for _, row in page_df.iterrows():
        detail, confirm, delete = quote_list_card(row, key_prefix="quote_list")
        if detail:
            st.session_state["quote_detail_id"] = int(row["quote_id"])
            st.rerun()
        if confirm:
            ok, failures = confirm_quote(int(row["quote_id"]))
            if ok:
                st.success("견적서를 확정했습니다.")
                st.rerun()
            else:
                st.error("확정할 수 없습니다.")
                for f in failures:
                    st.write(f"- {f}")
        if delete:
            delete_quote(int(row["quote_id"]))
            st.success("삭제 처리했습니다.")
            st.rerun()

    pagination_controls(len(filtered), page_size, "quote_list_page")


# -----------------------------
# 페이지: 대여 기록 / 반납
# -----------------------------

def page_rentals():
    st.header("대여 기록 / 반납")

    detail_id = st.session_state.get("rental_detail_id")
    if detail_id:
        if st.button("‹ 목록으로", key="rental_back"):
            st.session_state["rental_detail_id"] = None
            st.rerun()
        quote = get_quote(int(detail_id))
        if not quote:
            st.error("대여 기록을 찾지 못했습니다.")
            return

        render_quote_detail(int(detail_id), allow_edit=False, key_prefix="rental_detail_page", mode="rental")

        if quote["status"] in ["확정", "부분반납", "반납완료"]:
            st.divider()
            st.subheader("상품별 반납 / 날짜 수정")
            rentals = load_rentals_for_quote_df(int(detail_id))
            items = load_quote_items_df(int(detail_id))
            if rentals.empty:
                st.info("대여 상품이 없습니다.")
            else:
                item_thumb = {str(r["product_no"]): (str(r.get("thumbnail_path", "") or ""), str(r.get("thumbnail_url", "") or "")) for _, r in items.iterrows()}
                for _, rental in rentals.iterrows():
                    with st.container(border=True):
                        c0, c1, c2, c3, c4 = st.columns([1.0, 2.8, 1.8, 1.2, 1.2])
                        with c0:
                            _tp, _tu = item_thumb.get(str(rental["product_no"]), ("", ""))
                            show_thumb_from_values(_tp, _tu)
                        with c1:
                            st.markdown(f"**{rental['product_name']}**")
                            st.caption(f"상품번호: {rental['product_no']}")
                            st.caption(f"수량: {rental['quantity']}")
                        with c2:
                            st.caption(f"픽업 {rental['pickup_date']}")
                            st.caption(f"반납 {rental['return_date']}")
                            st.markdown(status_badge(str(rental["status"]), status_kind(rental["status"])), unsafe_allow_html=True)
                        with c3:
                            if rental["status"] == "반납완료":
                                if st.button("반납 취소", key=f"undo_return_item_{rental['id']}", use_container_width=True):
                                    ok, msg = set_rental_item_returned(int(rental["id"]), returned=False)
                                    if ok:
                                        st.success("반납을 취소했습니다.")
                                        st.rerun()
                                    else:
                                        st.error(msg)
                            else:
                                if st.button("반납", key=f"return_item_{rental['id']}", use_container_width=True):
                                    ok, msg = set_rental_item_returned(int(rental["id"]), returned=True)
                                    if ok:
                                        st.success("반납완료 처리했습니다.")
                                        st.rerun()
                                    else:
                                        st.error(msg)
                        with c4:
                            if rental["status"] != "반납완료":
                                if st.button("날짜 수정", key=f"rental_item_date_{rental['id']}", use_container_width=True):
                                    if rental_item_date_dialog is not None:
                                        rental_item_date_dialog(int(rental["id"]))
                            else:
                                st.caption("날짜 수정 없음")
        return

    quotes = load_quotes_df(include_deleted=False)
    if quotes.empty:
        st.info("대여 기록이 없습니다.")
        return

    rental_quotes = quotes[quotes["status"].isin(["확정", "부분반납", "반납완료"])].copy()
    if rental_quotes.empty:
        st.info("확정된 대여 기록이 없습니다.")
        return

    status_filter = status_filter_control("상태 필터", ["확정", "부분반납", "반납완료"], "rental_status_filter_v4")
    filtered = rental_quotes[rental_quotes["status"] == status_filter].copy() if status_filter else rental_quotes.copy()

    search = st.text_input("팀명 / 견적번호 / 상품 요약 검색", key="rental_search")
    if search:
        ss = search.lower()
        filtered = filtered[
            filtered["team_name"].astype(str).str.lower().str.contains(ss, na=False) |
            filtered["quote_no"].astype(str).str.lower().str.contains(ss, na=False) |
            filtered["상품요약"].astype(str).str.lower().str.contains(ss, na=False)
        ]

    p1, p2 = st.columns(2)
    with p1:
        pickup_day = st.date_input("픽업일", value=None, key="rental_pickup_day")
    with p2:
        return_day = st.date_input("반납일", value=None, key="rental_return_day")

    if pickup_day is not None:
        filtered = filtered[filtered["pickup_date"].astype(str) == str(pickup_day)]
    if return_day is not None:
        filtered = filtered[filtered["return_date"].astype(str) == str(return_day)]

    if filtered.empty:
        st.info("조건에 맞는 대여 기록이 없습니다.")
        return

    filtered = filtered.reset_index(drop=True)
    page_size = 10
    if "rental_list_page" not in st.session_state:
        st.session_state["rental_list_page"] = 1
    total_pages = max(1, math.ceil(len(filtered) / page_size))
    st.session_state["rental_list_page"] = min(max(1, st.session_state["rental_list_page"]), total_pages)
    start = (st.session_state["rental_list_page"] - 1) * page_size
    end = start + page_size
    page_df = filtered.iloc[start:end]

    for _, row in page_df.iterrows():
        detail, return_all, date_edit = rental_list_card(row, key_prefix="rental_list")
        if detail:
            st.session_state["rental_detail_id"] = int(row["quote_id"])
            st.rerun()
        if return_all:
            return_quote(int(row["quote_id"]))
            st.success("전체 반납 처리했습니다.")
            st.rerun()
        if date_edit:
            if rental_date_dialog is not None:
                rental_date_dialog(int(row["quote_id"]))

    pagination_controls(len(filtered), page_size, "rental_list_page")


# -----------------------------
# 앱 시작
# -----------------------------

init_db()
st.set_page_config(page_title="프라비 렌탈 관리", layout="wide")
APP_BUILD = "cloud-fix-20260618"
require_app_password()

st.markdown("""
<style>
.block-container {padding-top: 3rem; max-width: 1500px;}
h1, h2, h3 {line-height:1.25 !important; overflow:visible !important; padding-top:.12em !important; padding-bottom:.08em !important;}
[data-testid="stSidebar"] {background-color: #fbfbfb;}
.sidebar-brand {font-size:48px; font-weight:950; letter-spacing:-1.5px; color:#111; margin:0 0 2px 0; line-height:1.05;}
.sidebar-subtitle {font-size:14px; color:#555; margin:0 0 30px 0;}
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
</style>
""", unsafe_allow_html=True)

st.sidebar.markdown("""
<div class='sidebar-brand'>프라비</div>
<div class='sidebar-subtitle'>렌탈 재고·견적 관리</div>
""", unsafe_allow_html=True)

menu_options = ["제품 조회", "견적서 만들기", "견적서 조회", "대여 기록/반납"]
if "menu" not in st.session_state:
    st.session_state["menu"] = menu_options[0]

for opt in menu_options:
    label = f"● {opt}" if st.session_state["menu"] == opt else opt
    if st.sidebar.button(label, key=f"nav_{opt}", use_container_width=True):
        st.session_state["menu"] = opt
        st.session_state["quote_detail_id"] = None
        st.session_state["rental_detail_id"] = None
        st.session_state["sync_dialog_open"] = False
        st.session_state["sync_fallback_open"] = False
        st.rerun()
menu = st.session_state["menu"]

st.sidebar.divider()
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

if menu == "제품 조회":
    page_products()
elif menu == "견적서 만들기":
    page_quote_create()
elif menu == "견적서 조회":
    page_quote_list()
elif menu == "대여 기록/반납":
    page_rentals()
