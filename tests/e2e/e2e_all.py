import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:4700"
USER, PASS = "admin", "adminpass12345"
problems, oks = [], []

def note(name, good, detail=""):
    (oks if good else problems).append(name)
    print(("  ok   " if good else "  FAIL ") + name + (f"  [{detail}]" if detail and not good else ""))

def login(page):
    page.goto(BASE + "/login")
    page.fill("#username", USER); page.fill("#password", PASS)
    page.click("#login-submit"); page.wait_for_url("**/app", timeout=15000)
    page.wait_for_timeout(1200)

def open_rail_item(page, view):
    page.click(f".rail-btn[data-view='{view}']"); page.wait_for_timeout(1000)

with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1366, "height": 900})
    js_errors, bad = [], []
    page.on("pageerror", lambda e: js_errors.append(str(e)))
    page.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 400 else None)

    login(page)
    note("вход выполнен", page.url.endswith("/app"))

    # --- Библиотека: все три вида ---
    page.click("[data-view='library']"); page.wait_for_timeout(1200)
    note("библиотека открылась", "Библиотека" in page.inner_text("#view"))
    note("альбомы отрисованы", page.locator("#view .album").count() >= 2,
         f"найдено={page.locator('#view .album').count()}")

    for idx, (label, marker) in enumerate([("Исполнители", "Queen"), ("Альбомы", "Opera"), ("Плейлисты", "Плейлист")]):
        page.locator(".segment").nth(idx).click(); page.wait_for_timeout(1200)
        text = page.inner_text("#view")
        note(f"вид «{label}» работает", marker in text or (label == "Плейлисты" and "пока нет" in text),
             text[:120].replace("\n", " "))

    # --- Плеер: альбом с двумя треками, next/prev ---
    page.locator(".segment").nth(1).click(); page.wait_for_timeout(1000)
    page.locator("#view .album", has_text="Opera").first.click(); page.wait_for_timeout(1200)
    rows = page.locator("#view .track")
    note("альбом содержит 2 трека", rows.count() == 2, f"треков={rows.count()}")
    if rows.count() >= 2:
        rows.nth(0).click(); page.wait_for_timeout(1200)
        first = page.inner_text("#now-title")
        page.click("#next"); page.wait_for_timeout(1200)
        second = page.inner_text("#now-title")
        note("«следующий» меняет трек", first != second, f"{first!r} -> {second!r}")
        page.click("#prev"); page.wait_for_timeout(1200)
        back = page.inner_text("#now-title")
        note("«предыдущий» возвращает", back == first, f"ждали {first!r}, получили {back!r}")

    # --- Плейлисты: две колонки ---
    page.click("[data-view='playlists']"); page.wait_for_timeout(1200)
    note("плейлисты: две колонки", page.locator(".two-col, .pl-split, .playlist-layout").count() >= 1
         or "пока нет" in page.inner_text("#view"))

    # --- Меню: разделы ---
    open_rail_item(page, "settings")
    note("настройки открываются", "библиотеки" in page.inner_text("#view").lower())
    sel = page.locator("select[data-key='library_view']")
    note("в настройках есть вид по умолчанию", sel.count() == 1, f"найдено={sel.count()}")

    if sel.count():
        sel.select_option("playlists")
        page.locator("button", has_text="Сохранить").first.click(); page.wait_for_timeout(1200)
        page.reload(); page.wait_for_timeout(2000)
        page.click("[data-view='library']"); page.wait_for_timeout(1500)
        active = page.locator(".segment.active").inner_text().strip() if page.locator(".segment.active").count() else ""
        note("вид по умолчанию применился", "Плейлисты" in active, f"активен={active!r}")
        # вернуть
        open_rail_item(page, "settings")
        page.locator("select[data-key='library_view']").select_option("albums")
        page.locator("button", has_text="Сохранить").first.click(); page.wait_for_timeout(1000)

    for view, name in [("users", "Пользователи"), ("server", "Сервер"), ("account", "Аккаунт")]:
        open_rail_item(page, view)
        note(f"раздел «{name}» открывается", len(page.inner_text("#view").strip()) > 10)

    # --- Мобильный вид ---
    page.set_viewport_size({"width": 390, "height": 844}); page.wait_for_timeout(800)
    page.click("[data-view='library']"); page.wait_for_timeout(1200)
    note("мобильный вид: страница отрисована", page.locator("#view").is_visible())
    has_scroll = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2")
    note("мобильный вид: нет горизонтального скролла", has_scroll)

    real_bad = [x for x in bad if "getCoverArt" not in x and "placeholder" not in x]
    note("нет JS-исключений", not js_errors, "; ".join(js_errors[:2]))
    note("нет неожиданных 4xx/5xx", not real_bad, "; ".join(real_bad[:3]))
    b.close()

print(f"\nИТОГ: ok={len(oks)} проблем={len(problems)}")
for x in problems: print("  - " + x)
sys.exit(1 if problems else 0)
