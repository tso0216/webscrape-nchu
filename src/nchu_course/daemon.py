"""常駐輪詢迴圈：定時查餘額、狀態改變時推播，等待的空檔用來聽 Telegram 指令。

指令走 getUpdates 長輪詢，所以「等下一輪」與「監聽遙控」是同一條執行緒的
同一件事——沒有 webhook、沒有對外連接埠、沒有第二條執行緒。
"""
import datetime
import random
import signal
import time

from nchu_course.config import (
    BACKOFF_SCHEDULE,
    FAIL_ALERT_THRESHOLD,
    POLL_INTERVAL,
    POLL_JITTER,
    QUIET_HOURS,
    QUIET_INTERVAL,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_LONG_POLL,
)
from nchu_course.control import Control
from nchu_course.notifier import Notifier
from nchu_course.scraper import parse_courses
from nchu_course.session import NchuSession, RateLimited
from nchu_course.telegram import TelegramBot


def log(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def base_interval(now=None):
    """離峰時段拉長輪詢間隔，減少對校務系統的無謂請求。"""
    if not QUIET_HOURS or not QUIET_INTERVAL:
        return POLL_INTERVAL
    now = now or datetime.datetime.now()
    start, end = QUIET_HOURS
    in_quiet = start <= now.hour < end if start < end else (now.hour >= start or now.hour < end)
    return QUIET_INTERVAL if in_quiet else POLL_INTERVAL


class Daemon:
    def __init__(self, auto_enroll=False, dry_run=False, debug=False):
        self.stop = False
        self.session = NchuSession(log=log)
        self.bot = TelegramBot(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, log=log)
        self.notifier = Notifier(log=log, telegram=self.bot)
        self.control = Control(self.bot, run_check=self._check,
                               request_stop=self.request_stop,
                               run_enroll=self._enroll_one, log=log)
        self.failures = 0
        self.alerted = False
        # 沒開自動加選就完全不建立這個物件，也不會 import 到瀏覽器相關模組
        self.enroller = None
        if auto_enroll:
            from nchu_course.enroll import AutoEnroller
            self.enroller = AutoEnroller(notifier=self.notifier, session=self.session,
                                         log=log, dry_run=dry_run, debug=debug)

    def request_stop(self, signum=None, frame=None):
        self.stop = True
        print("\n收到停止訊號，本輪結束後退出…", flush=True)

    def run(self):
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        quiet = (f"，{QUIET_HOURS[0]:02d}:00–{QUIET_HOURS[1]:02d}:00 為 {QUIET_INTERVAL}s"
                 if QUIET_HOURS and QUIET_INTERVAL else "")
        log(f"開始輪詢，一般間隔 {POLL_INTERVAL}s{quiet}")
        if self.enroller:
            log(f"自動加選：{self.enroller.summary}")
            self.control.auto_enroll = self.enroller.summary
        self.control.start()

        while not self.stop:
            started = time.monotonic()
            wait = self._one_round(started) if self.control.watching else base_interval()
            self.control.next_at = (datetime.datetime.now()
                                    + datetime.timedelta(seconds=wait))
            self.control.sync()
            self._wait_and_listen(started, wait)

        self.control.finish()
        log("已停止")
        return 0

    def _check(self):
        """查一輪並比對狀態。

        /now 也走這裡：手動查詢一樣要更新 state.json，否則下次恢復自動輪詢時
        會拿舊狀態比對，漏報或誤報。
        """
        courses = parse_courses(self.session.fetch_courses())
        self.notifier.notify_changes(courses)
        self._auto_enroll(courses)
        return courses

    def _auto_enroll(self, courses):
        """有名額就去加選。加選失敗絕不能拖垮監控，所以整段包起來。

        加選要開瀏覽器（十幾秒），這段時間輪詢會停著等——沒名額時完全不會進來，
        有名額時本來就該優先把課加到手。
        """
        if not self.enroller:
            return
        try:
            results = self.enroller.handle(courses)
        except Exception as exc:
            log(f"自動加選發生未預期錯誤: {type(exc).__name__}: {exc}")
            return
        if results:
            self.control.record_enroll(results)

    def _enroll_one(self, code, dry_run=False):
        """Telegram /add 用：加選單一課程。

        不管有沒有開 --autoadd 都能用——/add 是人自己打的、而且按了確認才送出。
        結果一樣寫進 data/enroll.json，所以手動加到的課，自動加選不會再送一次。
        """
        from nchu_course.enroll import enroll_course, record_result

        result = enroll_course(code, dry_run=dry_run, log=log, session=self.session)
        records = self.enroller.records if self.enroller else None
        records = record_result(result, records, log=log)
        if self.enroller is not None:
            self.enroller.records = records
        return result

    def _one_round(self, started):
        try:
            courses = self._check()
            elapsed = time.monotonic() - started
            summary = ", ".join(f"{c.code}:{c.seats_text}" for c in courses)
            log(f"取得 {len(courses)} 門課（{elapsed:.1f}s）  {summary}")
            self.control.record_result(courses)
            if self.alerted:
                self.notifier.alert("[恢復] 課程監控已恢復正常")
                self.alerted = False
            self.failures = 0
            return base_interval()
        except RateLimited as exc:
            self.failures += 1
            self.control.record_error(exc)
            wait = BACKOFF_SCHEDULE[-1]
            log(f"被限流：{exc}，退避 {wait}s")
            return wait
        except Exception as exc:
            self.failures += 1
            self.control.record_error(exc)
            wait = BACKOFF_SCHEDULE[min(self.failures - 1, len(BACKOFF_SCHEDULE) - 1)]
            log(f"第 {self.failures} 次失敗：{type(exc).__name__}: {exc}，退避 {wait}s")
            if self.failures >= FAIL_ALERT_THRESHOLD and not self.alerted:
                try:
                    self.notifier.alert(
                        f"[警告] 課程監控連續失敗 {self.failures} 次\n{type(exc).__name__}: {exc}")
                    self.alerted = True
                except Exception as alert_exc:
                    log(f"告警送出失敗: {alert_exc}")
            return wait

    def _wait_and_listen(self, started, wait):
        """等到下一輪為止，期間掛在 getUpdates 上聽指令。

        /go 會把 control.wake 設成 True，好讓恢復監控時不必等剩下的睡眠跑完。
        沒設定 Telegram 時 listen() 回 False，退回原本的秒級睡眠。
        """
        remaining = wait - (time.monotonic() - started) + random.uniform(-POLL_JITTER, POLL_JITTER)
        deadline = time.monotonic() + max(5.0, remaining)
        while not self.stop and not self.control.wake:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if not self.control.listen(min(TELEGRAM_LONG_POLL, left)):
                time.sleep(min(1.0, left))
        self.control.wake = False


def run(auto_enroll=False, dry_run=False, debug=False):
    return Daemon(auto_enroll=auto_enroll, dry_run=dry_run, debug=debug).run()
