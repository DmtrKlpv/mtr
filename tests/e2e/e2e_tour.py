import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:4601"
problems = []
ok = []

def note(name, good, detail=""):
    (ok if good else problems).append(name + (f" :: {detail}" if detail else ""))
    print(("  ok   " if good else "  FAIL ") + name + (f"  [{detail}]" if detail and not good else ""))

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    js_errors, bad_responses = [], []
    page.on("pageerror", lambda e: js_errors.append(str(e)))
    page.on("response", lambda r: bad_responses.append(f"{r.status} {r.url}") if r.status >= 400 else None)

    page.goto(BASE + "/login")
    page.fill("#username", "admin"); page.fill("#password", "adminpass12345")
    page.click("#login-submit"); page.wait_for_url("**/app", timeout=10000)
    page.wait_for_timeout(1500)

    def open_tab(view, name):
        try:
            page.click(f"[data-view='{view}']", timeout=4000)
            page.wait_for_timeout(1200)
            text = page.inner_text("#view")
            note(f"раздел «{name}» открывается", len(text.strip()) > 0, "пустой")
            return text
        except Exception as exc:
            note(f"раздел «{name}» открывается", False, str(exc)[:120])
            return ""

    t = open_tab("library", "Библиотека")
    note("библиотека: видна статистика", "Треков" in t)
    note("библиотека: есть альбомы", page.locator("#view .album").count() >= 1)

    open_tab("playlists", "Плейлисты")
    open_tab("starred", "Избранное")
    open_tab("settings", "Настройки")
    open_tab("users", "Пользователи")
    open_tab("server", "Сервер")
    open_tab("account", "Аккаунт")

    note("нет JS-исключений", not js_errors, "; ".join(js_errors[:2]))
    real_404 = [r for r in bad_responses if "getCoverArt" not in r]
    note("нет неожиданных 4xx/5xx", not real_404, "; ".join(real_404[:3]))

    browser.close()

print(f"\nитог: ok={len(ok)} проблем={len(problems)}")
for item in problems:
    print("  - " + item)
sys.exit(1 if problems else 0)
