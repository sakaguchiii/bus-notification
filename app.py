
from flask import Flask, request
import os
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import LineBotApiError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    TemplateSendMessage, ButtonsTemplate, PostbackAction,
    PostbackEvent
)
import schedule
import time
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import threading

load_dotenv()

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
BUSVISION_BASE_URL = "https://bus-vision.jp/sanco/view/"

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

STOP_CODES = {
    "乙部朝日": "4403",
    "藤枝東": "4372",
    "イオンモール津南": "4356",
    "三重会館前": "4008",
    "堀川町": "4402",
    "津駅前": "4001"
}

class BusVisionSession:
    def __init__(self):
        self.session = requests.Session()
        self.last_approach_info = None

    def get_stop_code(self, stop_name):
        return STOP_CODES.get(stop_name)

    def search_bus(self, from_stop, to_stop):
        from_code = self.get_stop_code(from_stop)
        to_code = self.get_stop_code(to_stop)
        
        if not (from_code and to_code):
            print("[DEBUG]search_bus: 停留所コードが見つかりません。")
            return None

        approach_url = f"{BUSVISION_BASE_URL}approach.html"
        params = {
            'stopCdFrom': from_code,
            'stopCdTo': to_code,
            'addSearchDetail': 'false',
            'searchHour': 'null',
            'searchMinute': 'null',
            'searchAD': '-1',
            'searchVehicleTypeCd': 'null',
            'searchCorpCd': 'null',
            'lang': '0'
        }
        
        try:
            response = self.session.get(approach_url, params=params)
            print(f"[DEBUG]search_bus: リクエスト成功 URL={response.url}")
            return response.text
        except Exception as e:
            print(f"[DEBUG]search_bus中にエラーが発生: {e}")
            return None

    def extract_bus_info(self, html_content):
        """
        Bus-Visionの結果HTMLから、
        - バス情報が無い場合は None
        - 1番目のバスデータから通過時刻や停留所名を取得し、
        前回の情報と変わっていれば整形して返す
        """
        if not html_content:
            print("[DEBUG]extract_bus_info: HTMLがありません。")
            return None

        soup = BeautifulSoup(html_content, 'html.parser')

        # 1) バス情報が無いかどうかチェック
        error_div = soup.find('div', id='errorMsg', class_='errorMsg')
        if error_div:
            print("[DEBUG]extract_bus_info: バスの接近情報はありません。")
            # "該当する接近情報はありません。" と出ている
            return None

        # 2) バス情報(approachData)を全部探す
        approach_data_list = soup.find_all('div', class_='approachData')
        if not approach_data_list:
            print("[DEBUG]extract_bus_info: approachDataがありません。")
            # バス情報が1件もない
            return None

        # 3) その中から <span id="number">1</span> を探す
        for approach_data in approach_data_list:
            number_span = approach_data.find('span', id='number')
            if not number_span:
                continue

            # "1"という文字列かどうかチェック
            number_text = number_span.get_text(strip=True)
            if number_text == "1":
                # ここが「1番目のバス」の情報

                # 4) approachInfo (例: "11:39に白塚口･栗真中山町を通過")
                approach_info_div = approach_data.find('div', id='approachInfo')
                # 5) passInfo (例: "12個前を通過")
                pass_info_span = approach_data.find('span', id='passInfo')

                if approach_info_div and pass_info_span:
                    # 例: "11:39に白塚口･栗真中山町を通過"
                    approach_text = approach_info_div.get_text(strip=True)
                    # 例: "12個前を通過"
                    pass_info_text = pass_info_span.get_text(strip=True)

                    # ★1. 時刻と停留所名を抜き出す
                    #  "11:39に" と "白塚口･栗真中山町を通過" に分割
                    splitted = approach_text.split("に", 1)
                    if len(splitted) == 2:
                        time_part = splitted[0]       # 11:39
                        remainder = splitted[1]      # 白塚口･栗真中山町を通過
                        # "を通過" を除去
                        stop_name = remainder.replace("を通過", "")

                        # ★2. 何個前かの情報を抜き出す
                        #  pass_info_text = "12個前を通過"
                        pass_count = pass_info_text.replace("を通過", "")  # "12個前"

                        # ★3. 通知したい文章を組み立てる
                        #  例:
                        #   🚎 11:39
                        #   白塚口･栗真中山町を通過
                        #   （12個前）
                        result_str = f"🚎 {time_part}\n{stop_name}を通過\n（{pass_count}）"

                        # ★4. 前回取得と同じかどうかをチェック
                        if result_str != self.last_approach_info:
                            self.last_approach_info = result_str
                            print(f"[DEBUG]extract_bus_info: 新しいバス情報を検出 -> {result_str}")
                            return result_str
                        else:
                            print("[DEBUG] extract_bus_info: 以前と同じバス情報のため更新なし。")
        print("[DEBUG] extract_bus_info: 'number=1'のデータが見つからないか、更新情報なし。")
        # ここまで来たら情報なし・または前回と同じ
        return None

bus_session = BusVisionSession()
user_settings = {}
user_status = {}

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    handler.handle(body, signature)
    return 'OK'

def show_boarding_options(reply_token):
    buttons_template = ButtonsTemplate(
        title='乗車停留所を選択してください',
        text='以下から選択してください',
        actions=[
            PostbackAction(label='乙部朝日', data='boarding_otobe'),
            PostbackAction(label='藤枝東', data='boarding_fujieda'),
            PostbackAction(label='その他', data='boarding_other')
        ]
    )
    template_message = TemplateSendMessage(
        alt_text='乗車停留所の選択',
        template=buttons_template
    )
    line_bot_api.reply_message(reply_token, template_message)

def show_alighting_options(reply_token):
    buttons_template = ButtonsTemplate(
        title='降車停留所を選択してください',
        text='以下から選択してください',
        actions=[
            PostbackAction(label='乙部朝日', data='alighting_otobe'),
            PostbackAction(label='藤枝東', data='alighting_fujieda'),
            PostbackAction(label='その他', data='alighting_other')
        ]
    )
    template_message = TemplateSendMessage(
        alt_text='降車停留所の選択',
        template=buttons_template
    )
    line_bot_api.reply_message(reply_token, template_message)

def request_time_setting(reply_token):
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text="乗車時刻を「HH:MM」の形式で入力してください（例：08:30）")
    )

def confirm_settings(reply_token, user_id):
    settings = user_settings[user_id]
    confirmation_text = f"""
設定が完了しました：
乗車停留所: {settings['boarding']}
降車停留所: {settings['alighting']}
乗車時刻: {settings['time'].strftime('%H:%M')}

乗車時刻の7分前からバスの位置情報の監視を開始します。
"""
    line_bot_api.reply_message(reply_token, TextSendMessage(text=confirmation_text))

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    message_text = event.message.text
    
    status = user_status.get(user_id, {'state': None})
    print(f"[DEBUG] handle_message: user_id={user_id}, テキスト={message_text}, ステータス={status}")
    
    # 「設定開始」と入力した場合、乗車停留所選択ボタンを表示
    if message_text == "設定開始":
        show_boarding_options(event.reply_token)
        user_status[user_id] = {'state': 'awaiting_boarding'}
        
    # 「その他（乗車）」を選んだ後、ユーザーが手入力する流れ
    elif status['state'] == 'awaiting_other_boarding':
        if message_text not in STOP_CODES:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="申し訳ありません。その停留所は見つかりませんでした。\n別の停留所名を入力してください。")
            )
            return
        # 入力が正しかったら乗車停留所を保存
        user_settings[user_id] = {'boarding': message_text}
        show_alighting_options(event.reply_token)
        user_status[user_id] = {'state': 'awaiting_alighting'}
        
    # 「その他（降車）」を選んだ後、ユーザーが手入力する流れ
    elif status['state'] == 'awaiting_other_alighting':
        if message_text not in STOP_CODES:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="申し訳ありません。その停留所は見つかりませんでした。\n別の停留所名を入力してください。")
            )
            return
        
        user_settings[user_id]['alighting'] = message_text
        request_time_setting(event.reply_token)
        user_status[user_id] = {'state': 'awaiting_time'}
        
    # 時刻入力待ち
    elif status['state'] == 'awaiting_time':
        try:
            # ユーザーが入力した時刻をパース
            input_time = datetime.strptime(message_text, '%H:%M').time()
            # 現在の日付と組み合わせて datetime オブジェクトを作成
            now = datetime.now()
            departure_datetime = datetime.combine(now.date(), input_time)
        
            # ユーザー設定に保存
            user_settings[user_id]['time'] = departure_datetime
        
            # 確認メッセージの送信
            confirm_settings(event.reply_token, user_id)
        
            # 状態をリセット
            user_status[user_id] = {'state': None}
        
            # スケジュールの設定
            schedule_bus_check(user_id, departure_datetime)
        except ValueError:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="正しい時刻形式で入力してください（例：08:30）")
            )


########################################
# 新規追加: PostbackEvent 用ハンドラー
########################################
@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = event.postback.data
    status = user_status.get(user_id, {'state': None})

    print(f"[DEBUG] handle_postback: user_id={user_id}, data={data}, ステータス={status}")
    # 乗車停留所の選択時
    if status['state'] == 'awaiting_boarding':
        if data == 'boarding_otobe':
            user_settings[user_id] = {'boarding': '乙部朝日'}
            show_alighting_options(event.reply_token)
            user_status[user_id] = {'state': 'awaiting_alighting'}
        elif data == 'boarding_fujieda':
            user_settings[user_id] = {'boarding': '藤枝東'}
            show_alighting_options(event.reply_token)
            user_status[user_id] = {'state': 'awaiting_alighting'}
        elif data == 'boarding_other':
            # 「その他」を選んだ場合、テキスト入力で乗車停留所を受け取る
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="乗車する停留所名を入力してください。")
            )
            user_status[user_id] = {'state': 'awaiting_other_boarding'}

    # 降車停留所の選択時
    elif status['state'] == 'awaiting_alighting':
        if data == 'alighting_otobe':
            user_settings[user_id]['alighting'] = '乙部朝日'
            request_time_setting(event.reply_token)
            user_status[user_id] = {'state': 'awaiting_time'}
        elif data == 'alighting_fujieda':
            user_settings[user_id]['alighting'] = '藤枝東'
            request_time_setting(event.reply_token)
            user_status[user_id] = {'state': 'awaiting_time'}
        elif data == 'alighting_other':
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="降車する停留所名を入力してください。")
            )
            user_status[user_id] = {'state': 'awaiting_other_alighting'}

def check_bus_location(user_id):
    settings = user_settings.get(user_id)
    if not settings:
        print(f"[DEBUG] check_bus_location: user_id={user_id} の設定が見つかりません。")
        return

    print(f"[DEBUG] check_bus_location: user_id={user_id}, 乗車={settings['boarding']}, 降車={settings['alighting']} ")
    html_content = bus_session.search_bus(
        settings['boarding'],
        settings['alighting']
    )
    
    bus_info = bus_session.extract_bus_info(html_content)
    if bus_info:
        print(f"[DEBUG] check_bus_location: 新たなバス情報 -> {bus_info}")
        try:
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text=f"バス位置情報更新:\n{bus_info}")
            )
        except LineBotApiError as e:
            print(f"[DEBUG]LINEメッセージ送信エラー: {e}")
    else:
        print("[DEBUG] check_bus_location: 新たなバス情報はありません。")

def schedule_bus_check(user_id, departure_time):
    """
    当日の「departure_time の7分前」に1回だけ実行するスケジュールを設定する。
    もし departure_time がすでに過ぎている場合は、ここでどうするか別途対応が必要。
    """
    # 今の日時
    now = datetime.now()
    print(f"[DEBUG] schedule_bus_check: user_id={user_id}, 指定時刻={departure_time}, 現在時刻={now}")

    # 乗車予定の時刻が今日の何時何分かを確認
    # 例: departure_time が 18:30 なら 18:30-7分 = 18:23
    check_time = departure_time - timedelta(minutes=7)

    # 当日の年月日を departure_time から取得
    target_datetime = check_time

    # もしすでに過ぎている場合はどうする？ -> ここでキャンセルする/翌日にする etc.
    if target_datetime < now:
        print(f"[DEBUG] すでに設定時刻を過ぎています。キャンセルします。 (target_datetime={target_datetime}, now={now})")
        return

    # "HH:MM" 形式の文字列を作る(例: "18:23")
    schedule_time_str = check_time.strftime("%H:%M")

    # スケジュールにジョブを追加
    job = schedule.every().day.at(schedule_time_str).do(
        lambda: check_bus_location_loop(user_id, departure_time, job)
    )

    print(f"[DEBUG] スケジュール設定: 当日 {check_time} に1回だけ監視を開始")



def check_bus_location_loop(user_id, departure_time, job):
    """
    実際にバス位置情報を15秒おきに監視するが、
    今回は乗車時刻 + 5分まで監視し終わったらジョブをキャンセル。
    """
    print(f"[DEBUG] 監視開始: user_id={user_id}, 乗車時刻={departure_time}")
    start_now = datetime.now()
    print(f"[DEBUG] check_bus_location_loop 実行時刻: {start_now}")
    end_time = departure_time + timedelta(minutes=5)
    print(f"[DEBUG] 監視終了予定時刻={end_time}")
    while datetime.now() < end_time:
        current_time = datetime.now()
        print(f"[DEBUG] 監視ループ中: 現在={current_time} < 終了予定={end_time}")
        check_bus_location(user_id)
        time.sleep(15)

    # ループを抜けたら、このジョブをキャンセルして再実行しないようにする
    schedule.cancel_job(job)
    print("[DEBUG] 監視終了＆当日ジョブキャンセル")

def run_schedule_loop():
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    # スケジュール実行のスレッドを起動
    schedule_thread = threading.Thread(target=run_schedule_loop, daemon=True)
    schedule_thread.start()

    # Flask サーバ起動
    app.run(port=5000)
