import sys
from playwright.sync_api import sync_playwright
BASE = "http://127.0.0.1:4601"
res = []
def note(n, g, d=""):
    res.append(g); print(("  ok   " if g else "  FAIL ") + n + ("" if g else f"  [{d}]"))
with sync_playwright() as p:
    b = p.chromium.launch(); page = b.new_page(viewport={"width": 1280, "height": 800})
    page.goto(BASE + "/login")
    page.fill("#username", "admin"); page.fill("#password", "adminpass12345")
    page.click("#login-submit"); page.wait_for_url("**/app", timeout=10000)
    page.wait_for_timeout(1000)
    page.click(".rail-btn[data-view='settings']"); page.wait_for_timeout(900)
    select = page.locator("select[data-key='library_view']")
    note("в настройках есть выбор вида по умолчанию", select.count() == 1, f"найдено={select.count()}")
    if select.count():
        select.select_option("playlists")
        page.locator("button", has_text="Сохранить").first.click(); page.wait_for_timeout(900)
        page.reload(); page.wait_for_timeout(1200)
        page.click("[data-view='library']"); page.wait_for_timeout(1000)
        active = page.locator(".segment.active").inner_text().strip() if page.locator(".segment.active").count() else ""
        note("после сохранения библиотека открывается на «Плейлисты»", "Плейлисты" in active, f"активен={active!r}")
        page.click(".rail-btn[data-view='settings']"); page.wait_for_timeout(800)
        page.locator("select[data-key='library_view']").select_option("albums")
        page.locator("button", has_text="Сохранить").first.click(); page.wait_for_timeout(700)
    b.close()
print(f"\nитог: {sum(res)}/{len(res)}")
sys.exit(0 if all(res) else 1)
