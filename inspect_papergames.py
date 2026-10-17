import re
import time

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

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

    try:
        print("Đang mở Papergames...")
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        maximize_window(page)
        page.wait_for_timeout(2000)

        print("URL ban đầu:", page.url)
        print_buttons(page)

        if find_match_button(page) is None:
            print("Không tìm thấy nút tìm trận.")
            inspect_page(page)
            input("\nNhấn Enter để đóng trình duyệt...")
            raise SystemExit

        print("\nĐang nhấn tìm đối thủ...")
        click_find_match(page)
        last_find = time.monotonic()

        print("\nChưa tới màn kết thúc thì không rời trang.")
        print("Khi ván xong sẽ bấm Rời khỏi phòng, rồi bấm tìm trận lại.")
        print("Nhấn Ctrl+C tại Terminal nếu muốn dừng.")

        left_match = False
        leave_at = None
        announced_room = None

        try:
            while True:
                page.wait_for_timeout(1500)
                url = page.url
                finished = game_finished(page)
                away = in_queue(url) or in_room(url)

                if in_queue(url):
                    state = "Đang tìm đối thủ, giữ nguyên trang"
                elif in_room(url) and not finished:
                    state = "Đang trong phòng, ván chưa xong, không rời"
                    if announced_room != url:
                        announced_room = url
                        print(f"\nĐã vào phòng: {url}")
                        maximize_window(page)
                elif finished:
                    state = "Ván đã xong"
                else:
                    state = "Đang ở trang chủ"

                print(f"\r{state} | {url}", end="", flush=True)

                if away:
                    left_match = True
                    last_find = 0

                if finished:
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

                if away:
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
        browser.close()
