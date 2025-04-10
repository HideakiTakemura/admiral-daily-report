import os
import requests
import smtplib
import datetime
from email.mime.text import MIMEText
from email.utils import formatdate
from dotenv import load_dotenv

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Metric, RunReportRequest
from google.oauth2 import service_account



load_dotenv()

# Shopify API設定
SHOP_NAME = os.getenv("SHOPIFY_SHOP_NAME")
ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")

# メール設定
MAIL_FROM = os.getenv("MAIL_FROM")
MAIL_TO = os.getenv("MAIL_TO")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")

# GA4設定
GA_KEY_PATH = "ga4-key.json"
GA_PROPERTY_ID = "316809848"

# === GA4 セッション数取得 ===
def get_ga_sessions(start_date, end_date):
    credentials = service_account.Credentials.from_service_account_file(GA_KEY_PATH)
    client = BetaAnalyticsDataClient(credentials=credentials)
    request = RunReportRequest(
        property=f"properties/{GA_PROPERTY_ID}",
        metrics=[Metric(name="sessions")],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)]
    )
    response = client.run_report(request)
    return int(response.rows[0].metric_values[0].value)

# === Shopify売上と注文数取得（ページネーション対応）===
def get_shopify_sales(date_from: str, date_to: str):
    base_url = f"https://{SHOP_NAME}.myshopify.com/admin/api/2023-10/orders.json"
    headers = {
        "X-Shopify-Access-Token": ACCESS_TOKEN,
        "Content-Type": "application/json"
    }
    # 一度に取得する最大件数を指定（250が上限の場合が多い）
    params = {
        "status": "any",
        "created_at_min": f"{date_from}T00:00:00+09:00",
        "created_at_max": f"{date_to}T23:59:59+09:00",
        "limit": 250
    }
    orders = []
    url = base_url

    while url:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        orders.extend(data.get("orders", []))
        # 初回リクエスト以降は、パラメータはURLに含まれているためクリア
        params = None
        # Linkヘッダーに次ページが存在するか確認
        link_header = response.headers.get('Link')
        if link_header and 'rel="next"' in link_header:
            # Linkヘッダーの書式例:
            # <https://xxx.myshopify.com/admin/api/2023-10/orders.json?limit=250&page_info=xxx>; rel="next", <...>; rel="previous"
            next_url = None
            parts = link_header.split(',')
            for part in parts:
                if 'rel="next"' in part:
                    start = part.find('<') + 1
                    end = part.find('>')
                    next_url = part[start:end].strip()
                    break
            url = next_url
        else:
            url = None

    total_sales = sum(float(o["total_price"]) for o in orders)
    return round(total_sales), len(orders), orders

# === 商品別売上ランキング（数量ベース） ===
def get_product_ranking(orders):
    product_quantities = {}
    for order in orders:
        for item in order.get("line_items", []):
            title = item["title"]
            quantity = int(item["quantity"])
            product_quantities[title] = product_quantities.get(title, 0) + quantity

    ranked = sorted(product_quantities.items(), key=lambda x: x[1], reverse=True)
    return ranked[:5]

# === ランキング表示用フォーマット ===
def format_product_ranking(ranking):
    if not ranking:
        return "\n🏆 昨日の売上ランキング（Top 5）\n（データなし）"

    medals = ["1位", "2位", "3位"]
    lines = ["\n\n🏆 昨日の売上ランキング（Top 5）"]
    for i, (title, qty) in enumerate(ranking):
        prefix = medals[i] if i < 3 else f"{i+1}位"
        lines.append(f"{prefix} {title}（{qty}個）")
    return "\n".join(lines)

# === メール送信 ===
def send_mail_report(month_data, day_data, product_ranking):
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    month_start = today.replace(day=1)

    subject = f"Admiral Shopify 売上レポート（{today.strftime('%Y/%m/%d')}）"

    body = f"""Admiral Shopify 売上レポート（{today.strftime('%Y/%m/%d')}）

🗓 当月総計（{month_start.strftime('%Y/%m/%d')}～{yesterday.strftime('%Y/%m/%d')}）
🏭 売上金額：¥{month_data['sales']:,}
📦 注文数：{month_data['orders']}件
👥 セッション数：{month_data['sessions']:,}
✅ CVR：{month_data['cvr']}%
💰 注文単価：¥{month_data['aov']:,}

🗖 昨日（{yesterday.strftime('%Y/%m/%d')}）
🏭 売上金額：¥{day_data['sales']:,}
📦 注文数：{day_data['orders']}件
👥 セッション数：{day_data['sessions']:,}
✅ CVR：{day_data['cvr']}%
💰 注文単価：¥{day_data['aov']:,}
"""

    body += format_product_ranking(product_ranking)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg["Date"] = formatdate()

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.starttls()
        smtp.login(MAIL_FROM, MAIL_PASSWORD)
        smtp.send_message(msg)

    print("✅ メール送信完了！")

# === 本処理 ===
if __name__ == "__main__":
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    month_start = today.replace(day=1)

    y_str = yesterday.strftime("%Y-%m-%d")
    m_str = month_start.strftime("%Y-%m-%d")

    # データ取得
    month_sales, month_orders, _ = get_shopify_sales(m_str, y_str)
    day_sales, day_orders_count, day_orders = get_shopify_sales(y_str, y_str)

    month_sessions = get_ga_sessions(m_str, y_str)
    day_sessions = get_ga_sessions(y_str, y_str)

    # データ計算
    def compose_data(sales, orders, sessions):
        cvr = round((orders / sessions) * 100, 2) if sessions else 0
        aov = round(sales / orders) if orders else 0
        return {
            "sales": sales,
            "orders": orders,
            "sessions": sessions,
            "cvr": cvr,
            "aov": aov
        }

    month_data = compose_data(month_sales, month_orders, month_sessions)
    day_data = compose_data(day_sales, day_orders_count, day_sessions)

    product_ranking = get_product_ranking(day_orders)

    send_mail_report(month_data, day_data, product_ranking)
