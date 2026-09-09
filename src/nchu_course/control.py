"""Telegram 遙控：把指令翻成動作，並維護一則置頂的狀態面板。

置頂橫幅在手機上只會顯示第一行，所以面板第一行固定是完整摘要
（狀態・門數・有名額數・時間），其餘細節要點開才看得到。

Telegram 沒設定時整個類別是 no-op，daemon 不必到處判斷。
"""
import datetime

from nchu_course.config import WATCH_CODES

# 註冊到輸入框旁的指令選單
COMMANDS = [
    ("now", "立即查詢一次"),
    ("add", "加選一門課，例如 /add 0117"),
    ("go", "開始監控"),
    ("stop", "暫停監控"),
    ("status", "重新顯示狀態面板"),
    ("quit", "關閉程式（限群組管理員）"),
]

_HELP = ("可用指令：/now 立即查詢、/add <課號> 加選一門課、/go 開始監控、/stop 暫停監控、"
         "/status 重新顯示面板、/quit 關閉程式（限群組管理員）")


def _button(text, data):
    return {"text": text, "callback_data": data}


def _course_lines(courses):
    if not courses:
        return "（查無監控中的課程）"
    out = []
    for c in courses:
        mark = "✅" if c.has_seat else "⬜"
        seats = f"{c.seats} 位" if c.has_seat else ("額滿" if c.seats == 0 else "無餘額資料")
        out.append(f"{mark} {c.code} {c.name} — {seats}")
    return "\n".join(out)


def _course_brief(info, code):
    """把確認頁那一列排成看得懂的樣子，讓人按下確定前先核對。"""
    name = (info.get("name") or "").split(" ")[0]
    lines = [f"{code} {name}".strip()]
    if info.get("teacher"):
        lines.append(f"授課教師：{info['teacher'].split(' ')[0]}")
    if info.get("credits"):
        lines.append(f"學分：{info['credits']}")
    if info.get("capacity"):
        lines.append(f"人數：{info.get('enrolled', '?')} / {info['capacity']}"
                     + (f"（候補 {info['waiting']}）" if info.get("waiting") else ""))
    return "\n".join(lines)


def _hhmm(when):
    return f"{when:%H:%M}" if when else "—"


class Control:
    def __init__(self, bot, run_check, request_stop, run_enroll=None, log=print):
        self.bot = bot
        self.run_check = run_check      # 由 daemon 提供：查一輪並回傳 courses
        self.request_stop = request_stop  # 由 daemon 提供：要求主迴圈收工退出
        self.run_enroll = run_enroll    # 由 daemon 提供：加選一門課並回傳 EnrollResult
        self.log = log

        self.watching = True            # /stop 只暫停輪詢，監聽迴圈照常活著
        self.wake = False               # /go 後要求主迴圈立刻跑一輪
        self.finished = False

        self.status_id = None
        self._last_text = None

        self.courses = None
        self.missing = ()
        self.error = None
        self.checked_at = None
        self.next_at = None

        self.auto_enroll = None         # daemon 開啟自動加選時填入摘要字串
        self.enroll_notes = []          # 最近幾次加選結果，顯示在面板下方

    # ------------------------------------------------------------ 生命週期

    def start(self):
        if not self.bot.enabled:
            self.log("未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，Telegram 遙控停用")
            return
        self.bot.drain()
        self.bot.set_commands(COMMANDS)
        self.sync()
        self.log("Telegram 遙控已啟用")

    def finish(self):
        """程式結束前把面板標成已停止，免得它一直顯示「監控中」誤導人。"""
        self.finished = True
        self.sync()

    # ------------------------------------------------------ daemon 回報結果

    def record_result(self, courses):
        self.courses = courses
        self.missing = tuple(sorted(set(WATCH_CODES) - {c.code for c in courses}))
        self.error = None
        self.checked_at = datetime.datetime.now()

    def record_error(self, exc):
        self.error = f"{type(exc).__name__}: {exc}"
        self.checked_at = datetime.datetime.now()

    def record_enroll(self, results):
        """把自動加選的結果掛到面板上（只留最近三筆，免得面板被洗版）。"""
        now = datetime.datetime.now()
        for result in results:
            self.enroll_notes.append(f"{_hhmm(now)} {result.code} {result.label}")
        self.enroll_notes = self.enroll_notes[-3:]

    # ---------------------------------------------------------------- 監聽

    def listen(self, seconds):
        """阻塞最多 seconds 秒等指令。回傳 False 表示沒啟用，等待交給呼叫端。"""
        if not self.bot.enabled:
            return False
        for update in self.bot.get_updates(max(1, int(seconds))):
            try:
                self._handle(update)
            except Exception as exc:      # 遙控出錯不能拖垮監控主迴圈
                self.log(f"處理 Telegram 指令失敗: {type(exc).__name__}: {exc}")
        return True

    def _handle(self, update):
        callback = update.get("callback_query")
        if callback:
            message = callback.get("message") or {}
            if not self._is_mine(message.get("chat")):
                return
            self.bot.answer_callback(callback["id"])
            command, args = self._parse_command(callback.get("data"))
            if command:
                self._dispatch(command, args,
                               (callback.get("from") or {}).get("id"), message.get("chat"))
            return

        message = update.get("message") or {}
        chat = message.get("chat")
        if not self._is_mine(chat):
            self.log(f"忽略來自 chat_id={(chat or {}).get('id')} 的訊息")
            return
        command, args = self._command_of(message)
        if command:
            self._dispatch(command, args, (message.get("from") or {}).get("id"), chat)

    def _is_mine(self, chat):
        return bool(chat) and str(chat.get("id")) == self.bot.chat_id

    @staticmethod
    def _parse_command(text):
        """把指令拆成 (名稱, 參數)。

        吃兩種寫法：空白分隔的參數（`/add 0117`）與冒號分隔的
        callback_data（`add:yes:0117`）。回傳 (None, ()) 表示不是指令。
        """
        text = (text or "").strip()
        if not text:
            return None, ()
        parts = text.lstrip("/").split()
        if not parts:
            return None, ()
        head = parts[0].split("@")[0].lower()   # 群組裡的指令會帶 @botname 後綴
        name, _, rest = head.partition(":")
        args = tuple(x for x in rest.split(":") if x) + tuple(parts[1:])
        return (name or None), args

    @classmethod
    def _command_of(cls, message):
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return None, ()
        return cls._parse_command(text)

    def _dispatch(self, command, args=(), user_id=None, chat=None):
        answer = args[0] if args else None
        if command == "now":
            self._cmd_now()
        elif command == "go":
            self._cmd_go()
        elif command == "stop":
            self._cmd_stop()
        elif command in ("status", "start"):
            self._cmd_status()
        elif command == "add":
            if answer == "yes":
                self._cmd_add_confirm(args[1] if len(args) > 1 else "", user_id, chat)
            elif answer == "no":
                self.bot.send("已取消，沒有加選。")
            else:
                self._cmd_add(args, user_id, chat)
        elif command == "quit":
            if answer == "yes":
                self._cmd_quit_confirm(user_id, chat)
            elif answer == "no":
                self.bot.send("已取消，繼續監控。")
            else:
                self._cmd_quit(user_id, chat)
        else:
            self.bot.send(f"不認得「/{command}」。\n{_HELP}")

    def _admin_denied(self, user_id, chat, action):
        """會改變狀態的指令的權限檢查。放行回傳 None，否則回傳要回覆的理由。

        私聊裡沒有「管理員」這回事，而且 _is_mine() 已經確認過就是設定的那個人，
        所以直接放行。查不到名單（斷網、bot 被踢出群）時一律拒絕：關掉之後沒有
        任何行程在聽指令，寧可讓人回電腦按 Ctrl-C，也不要在權限不明時放行。
        """
        if (chat or {}).get("type") == "private":
            return None
        if not user_id:
            return "⚠️ 認不出是誰下的指令，無法確認權限。"
        admins = self.bot.admin_ids()
        if admins is None:
            self.log(f"無法取得群組管理員名單，拒絕{action}")
            return "⚠️ 暫時查不到群組管理員名單（可能是網路不穩），請稍後再試。"
        if str(user_id) not in admins:
            return f"⚠️ 只有群組管理員可以{action}。"
        return None

    # ---------------------------------------------------------------- 指令

    def _cmd_now(self):
        self.bot.send("查詢中…")
        try:
            courses = self.run_check()
        except Exception as exc:
            self.record_error(exc)
            self.bot.send(f"⚠️ 查詢失敗\n{self.error}")
        else:
            self.record_result(courses)
            self.bot.send(f"查詢結果 {_hhmm(self.checked_at)}\n\n{_course_lines(courses)}")
        self.sync()

    def _cmd_go(self):
        if self.watching:
            self.bot.send("已經在監控中了。")
            return
        self.watching = True
        self.wake = True          # 不用等目前這輪睡完
        self.bot.send("▶️ 已恢復監控，馬上查一次。")
        self.sync()

    def _cmd_stop(self):
        if not self.watching:
            self.bot.send("目前已是暫停狀態。")
            return
        self.watching = False
        self.bot.send("⏸ 已暫停定時查詢。程式仍在執行，隨時可用 /go 恢復或 /now 手動查一次。")
        self.sync()

    def _cmd_add(self, args, user_id, chat):
        """/add 0117：先演練一次把課程資訊撈回來，讓人核對後再按按鈕送出。

        打錯課號會加到完全不相干的課（課號每學期重編），所以這裡照 /quit 的做法
        先問一次。演練只有 0.5 秒，多這一步不影響搶課。
        """
        if self.run_enroll is None:
            self.bot.send("這個版本沒有啟用加選功能。")
            return
        denied = self._admin_denied(user_id, chat, "加選課程")
        if denied:
            self.bot.send(denied)
            return

        code = (args[0] if args else "").strip()
        if not code.isdigit() or not 1 <= len(code) <= 4:
            self.bot.send("用法：/add 0117\n（選課號碼是 4 位數字）")
            return

        self.bot.send(f"查詢 {code}…")
        try:
            result = self.run_enroll(code, dry_run=True)
        except Exception as exc:
            self.log(f"/add 演練失敗: {type(exc).__name__}: {exc}")
            self.bot.send(f"⚠️ 查不到這門課的加選頁\n{type(exc).__name__}: {exc}")
            return

        if result.status != "dry-run":
            # 還沒送出就知道加不成（查無此課號、已選過、不在選課時間…）
            self.bot.send(f"{result.label}\n{result.message}")
            return

        self.bot.send(
            "要加選這門課嗎？\n\n" + _course_brief(result.course, code),
            [[_button("✅ 確定加選", f"add:yes:{code}"), _button("取消", "add:no")]],
        )

    def _cmd_add_confirm(self, code, user_id, chat):
        """真正送出。權限再驗一次：按鈕之外也可能是有人手打 /add:yes:0117。"""
        if self.run_enroll is None:
            self.bot.send("這個版本沒有啟用加選功能。")
            return
        denied = self._admin_denied(user_id, chat, "加選課程")
        if denied:
            self.bot.send(denied)
            return
        if not code.isdigit():
            self.bot.send("不知道要加哪一門，請重打一次，例如 /add 0117")
            return

        self.bot.send(f"送出加選 {code}…")
        try:
            result = self.run_enroll(code, dry_run=False)
        except Exception as exc:
            self.log(f"/add 送出失敗: {type(exc).__name__}: {exc}")
            self.bot.send(f"⚠️ 加選失敗\n{type(exc).__name__}: {exc}")
            return

        self.record_enroll([result])
        text = f"{result.label}\n{result.message}"
        if result.ok:
            text += "\n\n建議上選課系統再確認一次選課清單。"
        self.bot.send(text)
        self.sync()

    def _cmd_quit(self, user_id, chat):
        """關閉程式是單向門，所以先問一次；真正的動作在 _cmd_quit_confirm。"""
        denied = self._admin_denied(user_id, chat, "關閉程式")
        if denied:
            self.bot.send(denied)
            return
        self.bot.send(
            "確定要關閉監控程式嗎？\n"
            "關掉之後沒有任何行程在聽指令，要重新啟動只能回到電腦前。\n"
            "（只是想暫停查詢的話用 /stop，程式會留著等你 /go。）",
            [[_button("⚠️ 確定關閉", "quit:yes"), _button("取消", "quit:no")]],
        )

    def _cmd_quit_confirm(self, user_id, chat):
        """再驗一次權限：`quit:yes` 除了按鈕，也可能是有人手打 /quit:yes 送進來的。"""
        denied = self._admin_denied(user_id, chat, "關閉程式")
        if denied:
            self.bot.send(denied)
            return
        self.bot.send("⏹ 收到，本輪結束後關閉。")
        self.request_stop()

    def _cmd_status(self):
        """重發面板並重新置頂——面板被誤刪或取消置頂時用這個救回來。"""
        self.status_id = None
        self._last_text = None
        self.sync()

    # ---------------------------------------------------------------- 面板

    def sync(self):
        """更新置頂面板。內容沒變就不打 API。"""
        if not self.bot.enabled:
            return
        text = self._panel_text()
        if text == self._last_text:
            return
        self._last_text = text
        if self.status_id is None:
            message_id = self.bot.send(text, self._buttons())
            if message_id is None:
                self._last_text = None
                return
            self.status_id = message_id
            self.bot.pin(message_id)
        elif not self.bot.edit(self.status_id, text, self._buttons()):
            self._last_text = None      # 沒改成功，下一輪再試

    def _buttons(self):
        if self.finished:
            return []
        toggle = (_button("⏸ 暫停監控", "stop") if self.watching
                  else _button("▶️ 開始監控", "go"))
        return [[_button("🔄 立即查詢", "now"), toggle]]

    def _panel_text(self):
        return "\n".join([self._headline(), "", _course_lines(self.courses or [])]
                         + self._footer())

    def _headline(self):
        """置頂橫幅只看得到這一行，所以摘要要塞得下也看得懂。"""
        if self.finished:
            return f"⏹ 已停止 · 程式已結束，遙控無效 · {_hhmm(self.checked_at)}"
        state = "🟢 監控中" if self.watching else "⏸ 已暫停"
        if self.error:
            return f"{state} · ⚠️ 查詢失敗 · {_hhmm(self.checked_at)}"
        if self.courses is None:
            return f"{state} · 尚未查詢"
        available = sum(1 for c in self.courses if c.has_seat)
        return (f"{state} · {len(self.courses)} 門 · "
                f"{available} 門有名額 · {_hhmm(self.checked_at)}")

    def _footer(self):
        lines = []
        if self.auto_enroll:
            lines.append(f"🤖 自動加選：{self.auto_enroll}")
        lines += [f"🎯 {note}" for note in self.enroll_notes]
        if self.missing:
            lines.append(f"⚠️ 這學期查無課號 {', '.join(self.missing)}（課號每學期會重編）")
        if self.error:
            lines.append(f"⚠️ 上次查詢失敗：{self.error}")
        if self.finished:
            lines.append("程式已結束，需要回電腦重新啟動。")
        elif self.watching:
            lines.append(f"下次查詢 {_hhmm(self.next_at)}" if self.next_at
                         else "準備第一次查詢…")
        else:
            lines.append("已暫停，按 ▶️ 或 /go 恢復")
        return ["", *lines] if lines else []
