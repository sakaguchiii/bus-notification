from flask import Flask, request
import os
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import LineBotApiError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    TemplateSendMessage, ButtonsTemplate, PostbackAction,
    PostbackEvent, QuickReply, QuickReplyButton, MessageAction
)
import schedule
import time
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import threading
import re
from difflib import get_close_matches

# ================================================================
#  環境変数 & 初期設定
# ================================================================

load_dotenv()

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
BUSVISION_BASE_URL = "https://bus-vision.jp/sanco/view/"

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ================================================================
#  定数
# ================================================================

# よく使う発車時刻を固定で持つ（ユーザー要望）
PRESET_TIMES = ["08:17", "18:03"]

# 停留所データを大幅に拡充
STOP_CODES = {
    # 既存の停留所
    "乙部朝日": "4403",
    "藤枝東": "4372",
    "イオンモール津南": "4356",
    "三重会館前": "4008",
    "堀川町": "4402",
    "津駅前": "4001",
    
    # 新規追加の停留所
    "御殿場口": "4010",
    "京口立町": "4012",
    "津新町駅前": "4005",
    "津市役所前": "4009",
    "観音寺": "4015",
    "片田": "4018",
    "高茶屋": "4025",
    "久居駅前": "4030",
    "榊原温泉口": "4035",
    "一身田": "4040",
    "白塚駅前": "4045",
    "河芸駅前": "4050",
    "安濃津": "4055",
    "雲出": "4060",
    "香良洲": "4065",
    "嬉野": "4070",
    "中川": "4075",
    "一志": "4080",
    "松阪駅前": "4085",
    "伊勢中川": "4090",
    "明和町": "4095",
    "斎宮": "4100",
    "小俣": "4105",
    "伊勢市駅前": "4110",
    "外宮前": "4115",
    "内宮前": "4120"
}

# 人気の停留所（使用頻度が高いと想定）
POPULAR_STOPS = [
    "津駅前", "乙部朝日", "藤枝東", "イオンモール津南", 
    "三重会館前", "御殿場口", "京口立町", "津新町駅前"
]

# ================================================================
#  BusVision ラッパークラス
# ================================================================

class BusVisionSession:
    """Bus-Vision のスクレイピングを行うセッション管理クラス"""

    def __init__(self):
        self.session = requests.Session()
        # 利用者ごとの最新接近情報キャッシュ
        self.user_last_approach_info = {}

    # ------------------------------------------------------------
    #  停留所ヘルパ
    # ------------------------------------------------------------

    @staticmethod
    def get_stop_code(stop_name):
        return STOP_CODES.get(stop_name)

    def search_similar_stops(self, input_text: str, max_results: int = 5):
        """入力に近い停留所候補を返す（完全一致→部分一致→類似度）"""
        if not input_text:
            return []

        # 完全一致優先
        if input_text in STOP_CODES:
            return [input_text]

        # 部分一致
        partial = [s for s in STOP_CODES if input_text in s or s in input_text]
        if partial:
            return partial[:max_results]

        # 類似度
        return get_close_matches(input_text, STOP_CODES.keys(), n=max_results, cutoff=0.6)

    # ------------------------------------------------------------
    #  Bus-Vision へのリクエスト
    # ------------------------------------------------------------

    def search_bus(self, from_stop: str, to_stop: str):
        """Bus-Vision の approach.html へアクセスし HTML を返す"""
        from_code, to_code = map(self.get_stop_code, (from_stop, to_stop))
        if not (from_code and to_code):
            print("[DEBUG]search_bus: 停留所コードが見つかりません。")
            return None

        params = {
            "stopCdFrom": from_code,
            "stopCdTo": to_code,
            "addSearchDetail": "false",
            "searchHour": "null",
            "searchMinute": "null",
            "searchAD": "-1",
            "searchVehicleTypeCd": "null",
            "searchCorpCd": "null",
            "lang": "0",
        }

        try:
            r = self.session.get(f"{BUSVISION_BASE_URL}approach.html", params=params)
            r.raise_for_status()
            print(f"[DEBUG]search_bus: リクエスト成功 URL={r.url}")
            return r.text
        except Exception as e:
            print(f"[DEBUG]search_bus: エラー {e}")
            return None

    # ------------------------------------------------------------
    #  HTML 解析
    # ------------------------------------------------------------

    def extract_bus_info(self, html_content: str, user_id: str):
        """HTML から接近中のバス（number=1）の情報をパースする"""
        if not html_content:
            print("[DEBUG]extract_bus_info: HTML が空")
            return None

        soup = BeautifulSoup(html_content, "html.parser")
        if soup.find("div", id="errorMsg", class_="errorMsg"):
            print("[DEBUG]extract_bus_info: 接近情報なし")
            return None

        for a in soup.find_all("div", class_="approachData"):
            number = a.find("span", id="number")
            if number and number.get_text(strip=True) == "1":
                approach = a.find("div", id="approachInfo")
                passed = a.find("span", id="passInfo")
                if approach and passed:
                    time_part, _, remainder = approach.get_text(strip=True).partition("に")
                    stop_name = remainder.replace("を通過", "")
                    pass_cnt = passed.get_text(strip=True).replace("を通過", "")
                    result = f"🚎 {time_part}\n{stop_name}を通過\n（{pass_cnt}）"

                    if self.user_last_approach_info.get(user_id) != result:
                        self.user_last_approach_info[user_id] = result
                        print(f"[DEBUG]extract_bus_info: user={user_id} update -> {result}")
                        return result
                    else:
                        print("[DEBUG]extract_bus_info: 変化なし")
        return None

    # ------------------------------------------------------------
    #  Utils
    # ------------------------------------------------------------

    def clear_user_info(self, user_id):
        self.user_last_approach_info.pop(user_id, None)
        print(f"[DEBUG]clear_user_info: user={user_id} cache cleared")

# ================================================================
#  グローバル状態
# ================================================================

bus_session = BusVisionSession()
user_settings = {}   # user_id -> {boarding, alighting, time}
user_status   = {}   # user_id -> state machine
user_active_jobs = {}  # user_id -> {job, departure_time, monitoring_thread}

# ================================================================
#  Flask Routes
# ================================================================

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    handler.handle(body, signature)
    return "OK"

# ================================================================
#  Quick Reply Builders
# ================================================================

def create_stop_quick_reply(stop_type: str = "boarding") -> QuickReply:
    """停留所選択用（人気 + その他検索）"""
    items = [
        QuickReplyButton(
            action=MessageAction(label=s, text=f"{stop_type}:{s}")
        ) for s in POPULAR_STOPS[:8]
    ]
    items.append(
        QuickReplyButton(
            action=MessageAction(label="🔍 その他を検索", text=f"{stop_type}:search")
        )
    )
    return QuickReply(items=items)


def create_time_quick_reply() -> QuickReply:
    """ユーザーが頻繁に使う時刻 + 手動入力のみ"""
    items = [
        QuickReplyButton(
            action=MessageAction(label=t, text=f"time:{t}")
        ) for t in PRESET_TIMES
    ]
    items.append(
        QuickReplyButton(
            action=MessageAction(label="⌨️ 手動入力", text="time:manual")
        )
    )
    return QuickReply(items=items)

# ================================================================
#  Message Helpers
# ================================================================

def show_boarding_options(reply_token):
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(
            text="🚌 乗車停留所を選択してください\n\n人気の停留所から選ぶか、\"その他を検索\"で検索できます。",
            quick_reply=create_stop_quick_reply("boarding"),
        ),
    )


def show_alighting_options(reply_token):
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(
            text="🏁 降車停留所を選択してください\n\n人気の停留所から選ぶか、\"その他を検索\"で検索できます。",
            quick_reply=create_stop_quick_reply("alighting"),
        ),
    )


def show_time_options(reply_token):
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(
            text="⏰ 乗車時刻を選択してください\n\nよく使う時刻をタップするか、手動入力を選択してください。",
            quick_reply=create_time_quick_reply(),
        ),
    )

# ================================================================
#  対話用ユーティリティ
# ================================================================

def handle_stop_search(reply_token, user_input: str, stop_type: str):
    """ユーザーが入力した文字列から停留所候補を出す"""
    matches = bus_session.search_similar_stops(user_input, max_results=8)
    if not matches:
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=f"「{user_input}」に該当する停留所が見つかりませんでした。\n\n別の名前で検索してみてください。"),
        )
        return False

    if len(matches) == 1:
        return matches[0]

    qr_items = [
        QuickReplyButton(action=MessageAction(label=m, text=f"{stop_type}:{m}"))
        for m in matches
    ]
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(
            text="候補の停留所が見つかりました。選択してください：",
            quick_reply=QuickReply(items=qr_items),
        ),
    )
    return False


def confirm_settings(reply_token, user_id):
    s = user_settings[user_id]
    txt = (
        "✅ 設定が完了しました\n\n"
        f"🚌 乗車停留所: {s['boarding']}\n"
        f"🏁 降車停留所: {s['alighting']}\n"
        f"⏰ 乗車時刻: {s['time'].strftime('%H:%M')}\n\n"
        "📱 乗車時刻の7分前からバスの位置情報の監視を開始します。\n"
        "❌ 監視をキャンセルしたい場合は『キャンセル』と入力してください。\n\n"
        "🔔 バスが接近したらリアルタイムでお知らせします！"
    )
    line_bot_api.reply_message(reply_token, TextSendMessage(text=txt))

# ================================================================
#  LINE Message Handlers
# ================================================================

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    status = user_status.get(user_id, {"state": None})
    print(f"[DEBUG]handle_message: uid={user_id}, text='{text}', state={status}")

    # ------------------------------------------------------------
    #  コマンド系
    # ------------------------------------------------------------
    if text in {"設定開始", "start", "開始"}:
        cancel_user_monitoring(user_id)
        show_boarding_options(event.reply_token)
        user_status[user_id] = {"state": "awaiting_boarding"}
        return

    if text in {"キャンセル", "cancel"}:
        if cancel_user_monitoring(user_id):
            msg = "❌ 監視をキャンセルしました。"
        else:
            msg = "現在監視中の予定はありません。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    if text in {"ヘルプ", "help"}:
        help_text = (
            "🚌 バス監視アプリの使い方\n\n"
            "📝 **基本コマンド**\n"
            "• 設定開始 - 新しい監視を設定\n"
            "• キャンセル - 現在の監視を停止\n"
            "• ヘルプ - この説明を表示\n\n"
            "🔍 **停留所検索**\n"
            "• 停留所名の一部を入力すると候補を表示\n"
            "• 例：『津駅』『イオン』『乙部』\n\n"
            "⏰ **時刻設定**\n"
            "• ボタンで『08:17』『18:03』を選択\n"
            "• その他は『⌨️ 手動入力』→ HH:MM 形式で入力\n\n"
            "📱 監視は乗車時刻の7分前から開始し、5分後まで継続します。"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))
        return

    # ------------------------------------------------------------
    #  prefix 付き入力（boarding:foo など）
    # ------------------------------------------------------------
    if ":" in text:
        prefix, value = text.split(":", 1)
        if prefix == "boarding":
            if value == "search":
                line_bot_api.reply_message(
                    event.reply_token, TextSendMessage(text="🔍 乗車する停留所名を入力してください"),
                )
                user_status[user_id] = {"state": "searching_boarding"}
            elif value in STOP_CODES:
                user_settings[user_id] = {"boarding": value}
                show_alighting_options(event.reply_token)
                user_status[user_id] = {"state": "awaiting_alighting"}
            return

        if prefix == "alighting":
            if value == "search":
                line_bot_api.reply_message(
                    event.reply_token, TextSendMessage(text="🔍 降車する停留所名を入力してください"),
                )
                user_status[user_id] = {"state": "searching_alighting"}
            elif value in STOP_CODES:
                user_settings[user_id]["alighting"] = value
                show_time_options(event.reply_token)
                user_status[user_id] = {"state": "awaiting_time"}
            return

        if prefix == "time":
            if value == "manual":
                line_bot_api.reply_message(
                    event.reply_token, TextSendMessage(text="⌨️ 乗車時刻を『HH:MM』形式で入力してください（例：08:30）"),
                )
                user_status[user_id] = {"state": "manual_time_input"}
            else:
                try:
                    t = datetime.strptime(value, "%H:%M").time()
                    now = datetime.now()
                    dep_dt = datetime.combine(now.date(), t)
                    if dep_dt <= now:
                        dep_dt += timedelta(days=1)
                    user_settings[user_id]["time"] = dep_dt
                    confirm_settings(event.reply_token, user_id)
                    user_status[user_id] = {"state": None}
                    schedule_bus_check(user_id, dep_dt)
                except ValueError:
                    line_bot_api.reply_message(
                        event.reply_token, TextSendMessage(text="⚠️ 正しい時刻形式で入力してください（例：08:30）"),
                    )
            return

    # ------------------------------------------------------------
    #  state machine flow
    # ------------------------------------------------------------
    if status["state"] == "searching_boarding":
        res = handle_stop_search(event.reply_token, text, "boarding")
        if res:
            user_settings[user_id] = {"boarding": res}
            show_alighting_options(event.reply_token)
            user_status[user_id] = {"state": "awaiting_alighting"}
        return

    if status["state"] == "searching_alighting":
        res = handle_stop_search(event.reply_token, text, "alighting")
        if res:
            user_settings[user_id]["alighting"] = res
            show_time_options(event.reply_token)
            user_status[user_id] = {"state": "awaiting_time"}
        return

    if status["state"] == "manual_time_input":
        try:
            t = datetime.strptime(text, "%H:%M").time()
            now = datetime.now()
            dep_dt = datetime.combine(now.date(), t)
            if dep_dt <= now:
                dep_dt += timedelta(days=1)
            user_settings[user_id]["time"] = dep_dt
            confirm_settings(event.reply_token, user_id)
            user_status[user_id] = {"state": None}
            schedule_bus_check(user_id, dep_dt)
        except ValueError:
            line_bot_api.reply_message(
                event.reply_token, TextSendMessage(text="⚠️ 正しい時刻形式で入力してください（例：08:30）"),
            )
        return

    # デフォルト応答
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="『設定開始』と入力してバス監視を開始してください。\n『ヘルプ』で使い方を確認できます。"),
    )

# ================================================================
#  Postback Handler (reserved)
# ================================================================

@handler.add(PostbackEvent)
def handle_postback(event):
    pass  # 現状未使用

# ================================================================
#  監視ロジック
# ================================================================

def check_bus_location(user_id):
    settings = user_settings.get(user_id)
    if not settings:
        print(f"[DEBUG]check_bus_location: settings missing user={user_id}")
        return

    html = bus_session.search_bus(settings["boarding"], settings["alighting"])
    info = bus_session.extract_bus_info(html, user_id)
    if info:
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text=f"🚌 バス位置情報更新:\n{info}"))
        except LineBotApiError as e:
            print(f"[DEBUG]push_message error: {e}")


def schedule_bus_check(user_id, departure_time):
    now = datetime.now()
    check_time = departure_time - timedelta(minutes=7)
    cancel_user_monitoring(user_id)

    if check_time < now:
        line_bot_api.push_message(user_id, TextSendMessage(text="⚠️ 指定時間が過ぎているため監視を開始できませんでした。"))
        return

    tag = f"user_{user_id}"
    job = schedule.every().day.at(check_time.strftime("%H:%M")).do(
        lambda: check_bus_location_loop(user_id, departure_time)
    ).tag(tag)

    user_active_jobs[user_id] = {"job": job, "departure_time": departure_time, "monitoring_thread": None}
    print(f"[DEBUG]schedule_bus_check: user={user_id}, start={check_time}")


def cancel_user_monitoring(user_id):
    if user_id not in user_active_jobs:
        return False

    info = user_active_jobs.pop(user_id)
    schedule.cancel_job(info["job"])
    bus_session.clear_user_info(user_id)
    print(f"[DEBUG]cancel_user_monitoring: user={user_id}")
    return True


def check_bus_location_loop(user_id, departure_time):
    end_time = departure_time + timedelta(minutes=5)

    def loop():
        while datetime.now() < end_time and user_id in user_active_jobs:
            check_bus_location(user_id)
            time.sleep(15)

        user_active_jobs.pop(user_id, None)
        bus_session.clear_user_info(user_id)
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text="✅ バス監視を終了しました。お疲れさまでした！"))
        except LineBotApiError as e:
            print(f"[DEBUG]push_message error: {e}")

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    if user_id in user_active_jobs:
        user_active_jobs[user_id]["monitoring_thread"] = t

# ================================================================
#  schedule runner & app start
# ================================================================

def run_schedule_loop():
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=run_schedule_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
