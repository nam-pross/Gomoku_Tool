import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

TURN_PATH = Path(__file__).with_name("turn.json")

URL = "https://papergames.io/vi/cờ-caro"
FIND_MATCH = "Chơi trực tuyến với một người chơi ngẫu nhiên"
PLAY_AGAIN = "Chơi lại!"
LEAVE_AFTER_GAME = re.compile(r"Rời khỏi phòng\s*\(\d+\)")


def print_buttons(page):
    print("\n--- CÁC NÚT HIỆN TẠI ---")

    buttons = page.locator("button")

    for i in range(buttons.count()):
        button = buttons.nth(i)

        try:
            if button.is_visible():
                print(
                    f"[{i}] "
                    f"Text={button.inner_text(timeout=1000).strip()!r} | "
                    f"Aria={button.get_attribute('aria-label')!r} | "
                    f"Title={button.get_attribute('title')!r}"
                )
        except Exception:
            pass


def inspect_page(page):
    print("\nURL:", page.url)
    print_buttons(page)

    print("\n--- NỘI DUNG TRANG ---")
    try:
        print(page.locator("body").inner_text(timeout=3000)[:5000])
    except Exception as e:
        print("Không đọc được nội dung:", e)


def first_visible(locator):
    count = locator.count()
    for index in range(count):
        item = locator.nth(index)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def find_match_button(page):
    return first_visible(
        page.get_by_role("button", name=FIND_MATCH, exact=True)
    )


def label_of(item):
    try:
        return " ".join(item.inner_text(timeout=500).split())
    except Exception:
        return ""


def postgame_leave_button(page):
    """Nút 'Rời khỏi phòng (9)' trên màn kết thúc, không phải nút dưới góc phòng."""
    candidates = page.locator("button, a")
    count = candidates.count()
    for index in range(count):
        item = candidates.nth(index)
        try:
            if not item.is_visible():
                continue
        except Exception:
            continue
        if LEAVE_AFTER_GAME.search(label_of(item)):
            return item
    return first_visible(page.get_by_text(LEAVE_AFTER_GAME))


def game_finished(page):
    if postgame_leave_button(page) is not None:
        return True
    return (
        first_visible(page.get_by_role("button", name=PLAY_AGAIN, exact=True))
        is not None
    )


def maximize_window(page):
    try:
        session = page.context.new_cdp_session(page)
        info = session.send("Browser.getWindowForTarget")
        session.send(
            "Browser.setWindowBounds",
            {
                "windowId": info["windowId"],
                "bounds": {"windowState": "maximized"},
            },
        )
        session.detach()
    except Exception as error:
        print("\nKhông phóng to được cửa sổ:", error)


auto_enabled = {"on": True}


def set_auto_enabled(enabled):
    auto_enabled["on"] = bool(enabled)
    if auto_enabled["on"]:
        message = "Đã bật thao tác tự động."
    else:
        message = (
            "Đã dừng thao tác tự động. Bạn chơi tay; "
            "bấm Chạy tự động để tiếp tục."
        )
    print(f"\n{message}\n", end="", flush=True)


def read_players(page):
    """Đồng hồ, tên và màu quân mỗi ghế. Tên trùng tài khoản là mình."""
    try:
        return page.evaluate(
            r"""() => {
                const accountNames = new Set();
                for (const node of document.querySelectorAll("button, a")) {
                    const text = (node.innerText || "").trim().toLowerCase();
                    const first = text.split(/[\n|]/)[0].trim().split(/\s+/)[0];
                    if (first && first.length <= 30) {
                        accountNames.add(first);
                    }
                }
                return [...document.querySelectorAll("app-timer")].map((timer) => {
                    let name = "";
                    let symbol = "";
                    let node = timer.parentElement;
                    for (let step = 0; step < 8 && node; step += 1) {
                        if (node.querySelectorAll("app-timer").length !== 1) {
                            break;
                        }
                        const named = node.querySelector("span.text-truncate");
                        if (named && !name) {
                            name = (named.innerText || "").trim().split("\n")[0];
                        }
                        const mark = node.querySelector("app-player-symbol");
                        if (mark && !symbol) {
                            const html = mark.innerHTML;
                            if (html.includes("dark")) {
                                symbol = "dark";
                            } else if (html.includes("light")) {
                                symbol = "light";
                            }
                        }
                        if (name && symbol) {
                            break;
                        }
                        node = node.parentElement;
                    }
                    const separator = timer.querySelector(".time-separator");
                    const separatorClass = separator && typeof separator.className === "string"
                        ? separator.className
                        : "";
                    return {
                        name,
                        symbol,
                        time: (timer.innerText || "").replace(/\s+/g, ""),
                        highlight: separatorClass.includes("highlight"),
                        mine: name !== "" && accountNames.has(name.toLowerCase()),
                    };
                });
            }"""
        )
    except Exception:
        return None


def _clock_seconds(text):
    parts = [part for part in str(text).split(":") if part != ""]
    if len(parts) < 2:
        return None
    try:
        return int(parts[-2]) * 60 + int(parts[-1])
    except ValueError:
        return None


def whose_turn(players, previous, last_index):
    """Người có đồng hồ giảm, hoặc dấu hai chấm đang nhấp nháy."""
    if not players:
        return None, None
    active = None
    if previous and len(previous) == len(players):
        for index, (now, old) in enumerate(zip(players, previous)):
            now_seconds = _clock_seconds(now.get("time"))
            old_seconds = _clock_seconds(old.get("time"))
            if (
                now_seconds is not None
                and old_seconds is not None
                and now_seconds < old_seconds
            ):
                active = index
                break
    if active is None:
        highlighted = [index for index, player in enumerate(players) if player.get("highlight")]
        if len(highlighted) == 1:
            active = highlighted[0]
        elif last_index is not None and 0 <= last_index < len(players):
            active = last_index
    if active is None:
        return None, None
    player = players[active]
    name = (player.get("name") or "").strip()
    if player.get("mine"):
        label = "Đến lượt bạn" + (f" ({name})" if name else "")
    elif any(item.get("mine") for item in players):
        label = "Đến lượt đối thủ" + (f" ({name})" if name else "")
    elif name:
        label = f"Đến lượt {name}"
    else:
        return None, active
    return label, active


def publish_turn(your_turn, symbol, room, known=True):
    """Ghi lượt cho app.py. Không hiện thêm dòng trên trang."""
    payload = {
        "your_turn": bool(your_turn),
        "symbol": symbol or "",
        "room": room or "",
        "at": time.time(),
    }
    try:
        temporary = TURN_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(TURN_PATH)
    except OSError:
        pass
    if not room or not known:
        return ""
    if your_turn and symbol == "dark":
        return "Đến lượt bạn (quân đậm)."
    if your_turn and symbol == "light":
        return "Đến lượt bạn (quân nhạt)."
    if your_turn:
        return "Đến lượt bạn."
    return "Đến lượt đối thủ."


BAR_JS = r"""
(state) => {
    const enabled = !state || state.enabled !== false;
    const parent = document.body || document.documentElement;
    let bar = document.getElementById("gomoku-control-bar");
    if (!bar) {
        bar = document.createElement("div");
        bar.id = "gomoku-control-bar";
        bar.setAttribute("popover", "manual");
        const button = document.createElement("button");
        button.id = "gomoku-auto-toggle";
        button.type = "button";
        button.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            const next = button.dataset.on !== "1";
            button.dataset.on = next ? "1" : "0";
            button.textContent = next ? "Dừng tự động" : "Chạy tự động";
            button.style.background = next ? "#b33939" : "#1e8449";
            if (window.gomokuSetAuto) {
                window.gomokuSetAuto(next);
            }
        }, true);
        bar.append(button);
        parent.appendChild(bar);
    }
    const oldBanner = bar.querySelector("#gomoku-turn-banner");
    if (oldBanner) {
        oldBanner.remove();
    }
    bar.style.cssText = [
        "position:fixed",
        "top:12px",
        "left:50%",
        "right:auto",
        "bottom:auto",
        "transform:translateX(-50%)",
        "margin:0",
        "border:none",
        "padding:0",
        "width:max-content",
        "max-width:92vw",
        "display:flex",
        "background:transparent",
        "background-color:transparent",
        "box-shadow:none",
        "z-index:2147483647",
        "overflow:visible",
    ].join(";");
    const button = bar.querySelector("#gomoku-auto-toggle");
    button.type = "button";
    button.dataset.on = enabled ? "1" : "0";
    button.textContent = enabled ? "Dừng tự động" : "Chạy tự động";
    button.style.cssText = [
        "padding:10px 18px",
        "border:none",
        "border-radius:999px",
        "color:#fff",
        "background:" + (enabled ? "#b33939" : "#1e8449"),
        "font:700 16px/1.2 Segoe UI,system-ui,sans-serif",
        "cursor:pointer",
    ].join(";");
    if (bar.showPopover && !bar.matches(":popover-open")) {
        try {
            bar.showPopover();
        } catch (error) {}
    }
}
"""


def install_bar(page):
    """Gắn lại thanh mỗi lần trang chuyển, kẻo game xóa mất."""
    page.add_init_script(
        "window.gomokuInstallBar = "
        + BAR_JS
        + ";const gomokuBoot=()=>{try{window.gomokuInstallBar({enabled:true});}catch(e){}};"
        + "if(document.body){gomokuBoot();}else{document.addEventListener('DOMContentLoaded',gomokuBoot);}"
    )


def ensure_bar(page, enabled):
    """Nút Dừng/Chạy, luôn nổi trên cùng màn hình."""
    try:
        page.evaluate(
            "state => { if (!window.gomokuInstallBar) { window.gomokuInstallBar = "
            + BAR_JS
            + "; } window.gomokuInstallBar(state); }",
            {"enabled": bool(enabled)},
        )
    except Exception:
        pass


def in_queue(url):
    return "/vi/q/" in url


def in_room(url):
    return "/vi/r/" in url


def dismiss_landing_ad(page):
    close_buttons = page.locator("button[aria-label='Close']")
    count = close_buttons.count()
    for index in range(count):
        button = close_buttons.nth(index)
        try:
            if button.is_visible():
                button.click(timeout=1000)
                page.wait_for_timeout(400)
        except Exception:
            pass


def click_find_match(page, close_ad=False):
    if close_ad:
        dismiss_landing_ad(page)
    button = find_match_button(page)
    if button is None:
        return False
    button.click()
    print("\nĐã bấm tìm trận.")
    return True


with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=["--start-maximized"],
    )
    context = browser.new_context(no_viewport=True)
    page = context.new_page()
    page.expose_function("gomokuSetAuto", set_auto_enabled)
    install_bar(page)

    try:
        print("Đang mở Papergames...")
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        maximize_window(page)
        page.wait_for_timeout(2000)

        print("URL ban đầu:", page.url)
        print_buttons(page)
        ensure_bar(page, auto_enabled["on"])

        print("\nThanh trên cùng màn hình: Dừng tự động / Chạy tự động.")
        print("File này báo lượt cho app.py. Bên app.py chọn Tự động để chỉ đánh khi đến lượt bạn.")
        print("Khi ván xong sẽ bấm Rời khỏi phòng, rồi bấm tìm trận lại.")
        print("Nhấn Ctrl+C tại Terminal nếu muốn đóng hẳn.")

        if click_find_match(page):
            last_find = time.monotonic()
            left_match = False
        else:
            print("Chưa thấy nút tìm trận. Thanh điều khiển vẫn ở trên. Sẽ thử lại.")
            last_find = 0
            left_match = True
        leave_at = None
        announced_room = None
        turn_players = None
        turn_index = None
        announced_turn = None
        publish_turn(False, "", "")

        try:
            while True:
                page.wait_for_timeout(1500)
                url = page.url
                finished = game_finished(page)
                away = in_queue(url) or in_room(url)
                running = auto_enabled["on"]

                if in_queue(url):
                    state = "Đang tìm đối thủ, giữ nguyên trang"
                elif in_room(url) and not finished:
                    state = "Đang trong phòng, ván chưa xong, không rời"
                    if announced_room != url:
                        announced_room = url
                        turn_players = None
                        turn_index = None
                        announced_turn = None
                        print(f"\nĐã vào phòng: {url}")
                        maximize_window(page)
                elif finished:
                    state = "Ván đã xong"
                else:
                    state = "Đang ở trang chủ"

                your_turn = False
                symbol = ""
                room = ""
                label = None
                if in_room(url) and not finished:
                    players = read_players(page) or []
                    label, turn_index = whose_turn(players, turn_players, turn_index)
                    turn_players = players or None
                    your_turn = bool(label and label.startswith("Đến lượt bạn"))
                    for player in players:
                        if player.get("mine") and player.get("symbol"):
                            symbol = player["symbol"]
                    room = url
                else:
                    turn_players = None
                    turn_index = None
                note = publish_turn(
                    your_turn,
                    symbol,
                    room,
                    known=bool(room) and bool(label) if room else True,
                )
                if note != announced_turn:
                    announced_turn = note
                    if note:
                        print(f"\n{note}\n", end="", flush=True)

                ensure_bar(page, running)

                prefix = "" if running else "Tạm dừng | "
                print(f"\r{prefix}{state} | {url}", end="", flush=True)

                if away:
                    left_match = True
                    last_find = 0

                if finished:
                    if not running:
                        continue
                    leave = postgame_leave_button(page)
                    if leave is not None and (
                        leave_at is None or time.monotonic() - leave_at >= 8
                    ):
                        if leave_at is None:
                            print(f"\nVán đã xong. Bấm {label_of(leave)!r}.")
                        else:
                            print("\nBấm lại Rời khỏi phòng.")
                        leave.click()
                        leave_at = time.monotonic()
                    continue

                leave_at = None

                if away or not running:
                    continue

                if left_match and time.monotonic() - last_find >= 3:
                    print("\nĐã về trang chủ. Bấm tìm trận lại.")
                    if click_find_match(page, close_ad=True):
                        last_find = time.monotonic()
                        announced_room = None
                    else:
                        last_find = time.monotonic()
                        print("Chưa thấy nút tìm trận, sẽ thử lại.")

        except KeyboardInterrupt:
            print("\nĐã yêu cầu dừng chương trình.")

    except PlaywrightTimeoutError as e:
        print("\nHết thời gian chờ:", e)

    except Exception as e:
        print("\nCó lỗi:", repr(e))

    finally:
        publish_turn(False, "", "")
        browser.close()
