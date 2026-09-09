"""Telegram Bot API 封裝。

收指令走 getUpdates 長輪詢——由我們主動去 Telegram 拉，所以不需要 webhook、
對外網址，也不必開第二條執行緒；等待下一輪查詢的空檔正好拿來監聽。

所有方法失敗時回傳 None/False 並記錄，不往外丟例外：遙控是附加功能，
不該讓監控主迴圈跟著中斷。
"""
import requests

from nchu_course.config import TELEGRAM_API_BASE, TELEGRAM_LONG_POLL

# 官方上限 4096 字元，留餘裕給截斷提示
_MAX_TEXT = 4000
_NOT_MODIFIED = "message is not modified"


def _clip(text):
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "\n…（訊息過長已截斷）"


class TelegramBot:
    def __init__(self, token, chat_id, log=print):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else None
        self.log = log
        self._offset = None

    @property
    def enabled(self):
        return bool(self.token and self.chat_id)

    # -------------------------------------------------------------- 低階呼叫

    def _call(self, method, params=None, timeout=15, ignore=()):
        """呼叫一個 Bot API method。失敗回傳 None；ignore 內的錯誤不記錄。"""
        url = f"{TELEGRAM_API_BASE}/bot{self.token}/{method}"
        try:
            res = requests.post(url, json=params or {}, timeout=timeout)
        except requests.RequestException as exc:
            self.log(f"Telegram {method} 連線失敗: {exc}")
            return None
        try:
            body = res.json()
        except ValueError:
            self.log(f"Telegram {method} 回應不是 JSON: {res.text[:200]}")
            return None
        if not body.get("ok"):
            description = str(body.get("description", ""))
            if not any(k in description for k in ignore):
                self.log(f"Telegram {method} 失敗: {description[:200]}")
            return None
        return body.get("result")

    # ---------------------------------------------------------------- 送訊息

    def send(self, text, buttons=None):
        """送一則訊息，回傳 message_id（失敗為 None）。"""
        params = {"chat_id": self.chat_id, "text": _clip(text)}
        if buttons is not None:
            params["reply_markup"] = {"inline_keyboard": buttons}
        message = self._call("sendMessage", params)
        return message.get("message_id") if message else None

    def edit(self, message_id, text, buttons=None):
        """就地改寫既有訊息。內容沒變時 API 會回 400，視為成功。"""
        params = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": _clip(text),
        }
        if buttons is not None:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return self._call("editMessageText", params, ignore=(_NOT_MODIFIED,)) is not None

    def pin(self, message_id):
        """置頂一則訊息。先取消既有置頂，免得每次重啟都疊一則新的上去。

        私聊中 bot 可以置頂自己的訊息，不需要額外權限；真的被拒絕也只是
        少了頂端橫幅，訊息本身照常更新。
        """
        self._call("unpinAllChatMessages", {"chat_id": self.chat_id})
        return self._call(
            "pinChatMessage",
            {"chat_id": self.chat_id, "message_id": message_id,
             "disable_notification": True},
        ) is not None

    def answer_callback(self, callback_id, text=None):
        """回應按鈕點擊。不做的話按鈕會一直轉圈。"""
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
        self._call("answerCallbackQuery", params)

    def admin_ids(self):
        """群組管理員的 user id 集合。查不到回傳 None（與「一個管理員都沒有」區分）。"""
        result = self._call("getChatAdministrators", {"chat_id": self.chat_id})
        if result is None:
            return None
        return {str((m.get("user") or {}).get("id")) for m in result}

    def set_commands(self, commands):
        """註冊輸入框旁的指令選單，省得記指令。commands 為 (指令, 說明) 序列。"""
        self._call("setMyCommands", {
            "commands": [{"command": c, "description": d} for c, d in commands]
        })

    # ---------------------------------------------------------------- 收訊息

    def get_updates(self, long_poll=TELEGRAM_LONG_POLL):
        """長輪詢取得新指令：沒訊息時掛在那裡最多 long_poll 秒。

        HTTP timeout 必須大於 long_poll，否則會在 Telegram 還沒回應前就自己斷線。
        """
        params = {
            "timeout": int(long_poll),
            "allowed_updates": ["message", "callback_query"],
        }
        if self._offset is not None:
            params["offset"] = self._offset
        updates = self._call("getUpdates", params, timeout=int(long_poll) + 10)
        if not updates:
            return []
        self._offset = updates[-1]["update_id"] + 1
        return updates

    def drain(self):
        """丟掉停機期間累積的指令。

        Telegram 會保留未確認的 update 24 小時，不清掉的話開機瞬間會把昨晚
        傳的 /stop 補跑一遍。順便關掉 webhook——它與 getUpdates 互斥。
        """
        self._call("deleteWebhook", {"drop_pending_updates": False})
        updates = self._call("getUpdates", {"offset": -1, "timeout": 0})
        if updates:
            self._offset = updates[-1]["update_id"] + 1

    def recent_chats(self):
        """列出最近傳訊息給 bot 的 (chat_id, 名稱)，用來找出自己的 chat_id。

        刻意不帶 offset：只看不確認，才不會把訊息吃掉。
        """
        self._call("deleteWebhook", {"drop_pending_updates": False})
        found = {}
        for update in self._call("getUpdates", {"timeout": 0}) or []:
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            if chat.get("id") is None:
                continue
            name = (chat.get("username")
                    or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                    or chat.get("title") or "")
            found[str(chat["id"])] = name
        return list(found.items())
