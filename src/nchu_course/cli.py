"""統一 CLI 入口。用 `./nchu <指令>` 或 `python -m nchu_course <指令>` 執行。"""
import argparse
import sys

from nchu_course import config, ui


# --------------------------------------------------------------------- 共用

def _quiet_log(_msg):
    pass


def _fetch(quiet=False):
    """取得查詢結果 HTML（必要時自動續命）。回傳 (html, session)。"""
    from nchu_course.session import NchuSession
    session = NchuSession(log=_quiet_log if quiet else print)
    return session.fetch_courses(), session


def _print_courses(courses, empty_hint):
    if not courses:
        print(empty_hint)
        return
    print(ui.table(ui.COURSE_HEADERS, ui.course_rows(courses), ui.COURSE_ALIGNS))
    available = sum(1 for c in courses if c.has_seat)
    print(f"\n共 {len(courses)} 門，其中 {ui.GREEN}{available} 門有名額{ui.RESET}")


# --------------------------------------------------------------------- 指令

def cmd_check(args):
    from nchu_course.scraper import parse_courses

    html, _ = _fetch(quiet=not args.verbose)
    courses = parse_courses(html)

    missing = set(config.WATCH_CODES) - {c.code for c in courses}
    # -a 只影響終端機列表；推播一律用完整清單。
    shown = [c for c in courses if c.has_seat] if args.available else courses

    _print_courses(shown, "目前沒有任何監控中的課程有名額。")
    if missing:
        print(f"\n{ui.DIM}注意：這學期查無以下課號：{', '.join(sorted(missing))}"
              f"（選課號碼每學期會重編，請上選課系統確認新的課號）{ui.RESET}")

    if args.notify or args.push:
        from nchu_course.notifier import Notifier
        notifier = Notifier(telegram=_bot())
        if args.push:
            notifier.notify_report(courses)
        if args.notify:
            notifier.notify_changes(courses)
    return 0


def _bot():
    from nchu_course.telegram import TelegramBot
    return TelegramBot(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)


def cmd_telegram(args):
    """設定用的小工具：找出自己的 chat_id，並確認 bot 真的送得出訊息。"""
    if not config.TELEGRAM_BOT_TOKEN:
        print("尚未設定 TELEGRAM_BOT_TOKEN，請先在 .env 填入 BotFather 給的 token。")
        return 1

    bot = _bot()
    if args.test:
        if not bot.enabled:
            print("尚未設定 TELEGRAM_CHAT_ID，先跑一次 `nchu telegram` 把它找出來。")
            return 1
        if bot.send("測試訊息：nchu 課程監控連線正常。") is None:
            return 1
        print(f"{ui.GREEN}[OK]{ui.RESET} 已送出測試訊息，去 Telegram 看一下。")
        return 0

    chats = bot.recent_chats()
    if not chats:
        print("沒有收到任何訊息。請先在 Telegram 對你的 bot 說一句話（例如 /start），再跑一次。")
        print(f"{ui.DIM}注意：watch 正在執行時會把訊息收走，這個指令要在它停著的時候用。{ui.RESET}")
        return 1
    print("最近傳訊息給 bot 的對話：\n")
    for chat_id, name in chats:
        print(f"  TELEGRAM_CHAT_ID={chat_id}    {name}")
    print("\n把上面那行（你自己的）貼進 .env，再用 `nchu telegram --test` 驗證。")
    return 0


def cmd_watch(args):
    from nchu_course.daemon import run

    # --dry-run 本身就代表「要演練自動加選」，不必再多打一個 -e
    auto = args.autoadd or args.dry_run or config.ENROLL_AUTO
    if auto and not config.ENROLL_CODES:
        print("要自動加選，但沒有任何課號可加（settings.toml 的 [courses] watch 是空的）。")
        return 1
    if auto:
        mode = "演練（不會真的送出）" if args.dry_run else "會實際送出加選"
        print(f"{ui.BOLD}自動加選已開啟{ui.RESET}：{mode}\n"
              f"目標課號：{', '.join(config.ENROLL_CODES)}\n"
              f"{ui.DIM}只會加選，不會退掉任何已選的課。{ui.RESET}\n")
    return run(auto_enroll=auto, dry_run=args.dry_run, debug=args.debug)


def cmd_enroll(args):
    """手動加選一門課。主要用來在開自動加選之前，先確認流程走得通。"""
    from nchu_course.enroll import enroll_course

    code = args.code.strip()
    if not args.dry_run and not args.yes:
        print(f"即將到選課系統加選課號 {ui.BOLD}{code}{ui.RESET}。")
        print(f"{ui.DIM}這支程式只會加選，不會退掉任何已選的課。{ui.RESET}")
        if input("確定嗎？(y/N) ").strip().lower() not in ("y", "yes"):
            print("已取消。")
            return 1

    result = enroll_course(code, dry_run=args.dry_run, debug=args.debug)
    # 手動加選也記一筆，watch 才不會對同一門課再送一次
    from nchu_course.enroll import record_result
    record_result(result)
    print(f"\n{result.plain_label}")
    if result.message:
        print(result.message)
    if result.status in ("aborted", "error", "unknown"):
        print(f"{ui.DIM}頁面快照存在 data/enroll-debug/，可以打開來看實際回了什麼。{ui.RESET}")
    return 0 if result.status in ("success", "taken", "dry-run") else 1


def cmd_login(args):
    from nchu_course.session import NchuSession

    session = NchuSession()
    if args.fresh:
        session.clear()
        print("已清除既有 cookie。")
    session.relogin()
    print(f"{ui.GREEN}[OK]{ui.RESET} 登入成功，cookie 已存到 {config.COOKIE_FILE}")
    return 0


def cmd_logout(args):
    from nchu_course.session import NchuSession

    NchuSession(log=_quiet_log).clear()
    print(f"已刪除 {config.COOKIE_FILE}，下次查詢會重新登入。")
    return 0


# --------------------------------------------------------------------- 進入點

def build_parser():
    p = argparse.ArgumentParser(
        prog="nchu",
        description="中興大學通識課餘額監控",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用流程：
  nchu login                 登入並存下 cookie
  nchu check                 看一次監控清單的餘額
  nchu watch                 常駐監控，有名額時推播 LINE / Telegram
  nchu watch --autoadd       常駐監控，一有名額就自動幫你加選
  nchu enroll 0532 --dry-run 演練一次加選流程（走到最後一步就停）
  nchu telegram              找出自己的 chat_id（設定 Telegram 遙控用）

自動加選只會「加」，不會退掉任何已選的課；看不懂頁面時一律中止。
""")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="查一次監控清單的餘額")
    c.add_argument("-a", "--available", action="store_true", help="只列出還有名額的")
    c.add_argument("-n", "--notify", action="store_true",
                   help="狀態有變就推播 LINE / Telegram（給排程用）")
    c.add_argument("-p", "--push", action="store_true",
                   help="把整份監控清單推播到 LINE / Telegram（不管狀態有沒有變）")
    c.add_argument("-v", "--verbose", action="store_true", help="顯示 session 續命過程")
    c.set_defaults(func=cmd_check)

    w = sub.add_parser("watch", help="常駐監控，狀態改變時推播，可用 Telegram 遙控")
    w.add_argument("--autoadd", "-autoadd", dest="autoadd", action="store_true",
                   help="有名額時自動幫你加選（只加不退；預設關閉）")
    w.add_argument("--dry-run", action="store_true",
                   help="自動加選演練：走到按「是」之前就停，不會真的送出")
    w.add_argument("--debug", action="store_true",
                   help="加選時把每一步的頁面存到 data/enroll-debug/")
    w.set_defaults(func=cmd_watch)

    e = sub.add_parser("enroll", help="手動加選一門課（用來測試加選流程）")
    e.add_argument("code", help="選課號碼，例如 0532")
    e.add_argument("--dry-run", action="store_true",
                   help="演練：走到按「是」之前就停，不會真的送出")
    e.add_argument("-y", "--yes", action="store_true", help="不要問我，直接加選")
    e.add_argument("--debug", action="store_true",
                   help="把每一步的頁面存到 data/enroll-debug/")
    e.set_defaults(func=cmd_enroll)

    t = sub.add_parser("telegram", help="找出自己的 chat_id 或測試 bot 連線")
    t.add_argument("-t", "--test", action="store_true", help="改為送出一則測試訊息")
    t.set_defaults(func=cmd_telegram)

    lg = sub.add_parser("login", help="強制重新登入並更新 cookie")
    lg.add_argument("--fresh", action="store_true", help="先清掉既有 cookie 再登入")
    lg.set_defaults(func=cmd_login)

    lo = sub.add_parser("logout", help="刪除已存的 cookie")
    lo.set_defaults(func=cmd_logout)

    return p


def _force_utf8():
    """Windows 預設輸出編碼是 cp950，課名有罕用字或輸出導向檔案時會 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def main(argv=None):
    _force_utf8()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        return 130
    except Exception as exc:
        print(f"{ui.RED}錯誤{ui.RESET}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
