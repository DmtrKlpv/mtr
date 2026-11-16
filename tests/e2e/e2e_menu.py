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
    settings = page.locator(".rail-btn[data-view='settings']")
    account = page.locator(".rail-btn[data-view='account']")
    note("«Настройки» и «Аккаунт» — кнопки первого уровня в рейле", settings.count() == 1 and account.count() == 1,
         f"настройки={settings.count()} аккаунт={account.count()}")
    note("подменю шестерёнки убрано", page.locator("#menu, #menu-toggle, .menu-item").count() == 0)
    settings.click(); page.wait_for_timeout(900)
    note("кнопка «Настройки» открывает страницу", "Вид библиотеки" in page.inner_text("#view") or "Настройки" in page.inner_text("#view"))
    account.click(); page.wait_for_timeout(900)
    note("кнопка «Аккаунт» открывает страницу", "Аккаунт" in page.inner_text("#view"))
    b.close()
print(f"\nитог: {sum(res)}/{len(res)}")
sys.exit(0 if all(res) else 1)
