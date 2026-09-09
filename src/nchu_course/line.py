"""LINE Messaging API 廣播。"""
import requests

_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"


def send_line_message(channel_access_token, message):
    """廣播一則 LINE 訊息。成功回傳 True，失敗回傳 False（不丟例外）。"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {channel_access_token}",
    }
    data = {"messages": [{"type": "text", "text": message}]}
    try:
        res = requests.post(_BROADCAST_URL, headers=headers, json=data, timeout=15)
    except requests.RequestException as exc:
        print(f"LINE 送出失敗: {exc}")
        return False
    if res.status_code != 200:
        print(f"LINE 送出失敗: {res.status_code} {res.text[:200]}")
        return False
    return True
