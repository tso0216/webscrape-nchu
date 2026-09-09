"""狀態去重通知：只有在餘額「從無到有」或「從有到無」時才推播。

每分鐘輪詢若每輪都發訊息，一小時就是 60 則。這裡把上一輪的餘額存進
data/state.json，只在狀態翻轉時送出一則合併訊息。
"""
import json
import os
import tempfile

from nchu_course.config import LINE_CHANNEL_ACCESS_TOKEN, STATE_FILE, ensure_data_dir
from nchu_course.line import send_line_message


def _has_seat(seats):
    """seats 為 None（例如「夜校」這類非數字欄位）視為沒有名額。"""
    return isinstance(seats, int) and seats > 0


class Notifier:
    def __init__(self, state_file=STATE_FILE, token=LINE_CHANNEL_ACCESS_TOKEN, log=print,
                 telegram=None):
        self.state_file = str(state_file)
        self.token = token
        self.log = log
        self.telegram = telegram    # TelegramBot；None 或未設定時只送 LINE
        self.state = self._load()

    # ------------------------------------------------------------------ 對外

    def notify_changes(self, courses):
        """比對上一輪狀態並在需要時推播。回傳實際送出的訊息（沒送則為 None）。"""
        message = (self._first_run_message(courses) if self.state is None
                   else self._diff_message(courses))

        self.state = {c.code: c.seats for c in courses}
        self._save(self.state)

        if message:
            self._send(message)
        return message

    def notify_report(self, courses):
        """不比對狀態，直接把整份監控清單推到 LINE 與 Telegram。

        刻意不寫 state：這只是「再看一次」，不該把還沒通知過的變化吃掉。
        """
        message = self._report_message(courses)
        self._send(message)
        return message

    def alert(self, text):
        """程式層級的告警（連續抓取失敗等），不經過去重。"""
        self._send(text)

    # ------------------------------------------------------------------ 內部

    def _first_run_message(self, courses):
        available = [c for c in courses if c.has_seat]
        lines = ["[開始監控] 通識課餘額",
                 f"監控 {len(courses)} 門課，目前有名額 {len(available)} 門。"]
        if available:
            lines.append("")
            lines += [f"- {c.code} {c.name}：{c.seats} 位" for c in available]
        return "\n".join(lines)

    def _report_message(self, courses):
        if not courses:
            return "[餘額回報] 通識課餘額\n（查無監控中的課程）"
        available = sum(1 for c in courses if c.has_seat)
        lines = ["[餘額回報] 通識課餘額",
                 f"監控 {len(courses)} 門課，目前有名額 {available} 門。",
                 ""]
        for c in courses:
            mark = "✅" if c.has_seat else "⬜"
            if c.has_seat:
                seats = f"{c.seats} 位"
            else:
                seats = "額滿" if c.seats == 0 else "無餘額資料"
            lines.append(f"{mark} {c.code} {c.name}：{seats}")
        return "\n".join(lines)

    def _diff_message(self, courses):
        opened, closed = [], []
        for c in courses:
            was = _has_seat(self.state.get(c.code))
            if c.has_seat and not was:
                opened.append(c)
            elif was and not c.has_seat:
                closed.append(c)

        if not opened and not closed:
            return None

        lines = []
        if opened:
            lines.append("【有名額】")
            lines += [f"- {c.code} {c.name}：{c.seats} 位" for c in opened]
        if closed:
            if lines:
                lines.append("")
            lines.append("【已額滿】")
            lines += [f"- {c.code} {c.name}：已額滿" for c in closed]
        return "\n".join(lines)

    def _send(self, text):
        """狀態變更通知兩邊都送。先寫進 log，任一邊掛掉都還查得到內容。"""
        self.log(f"送出通知:\n{text}")
        if self.token:
            if not send_line_message(self.token, text):
                self.log("警告：LINE 通知送出失敗")
        else:
            self.log("（未設定 LINE token，略過 LINE）")
        if self.telegram is not None and self.telegram.enabled:
            if self.telegram.send(text) is None:
                self.log("警告：Telegram 通知送出失敗")

    def _load(self):
        if not os.path.exists(self.state_file):
            return None
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            self.log(f"state 檔讀取失敗，視為首次執行: {exc}")
            return None
        return data if isinstance(data, dict) else None

    def _save(self, state):
        ensure_data_dir()
        directory = os.path.dirname(os.path.abspath(self.state_file)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_file)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
