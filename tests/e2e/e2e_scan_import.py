import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:4902"
res = []
def note(n, g, d=""):
    res.append(g); print(("  ok   " if g else "  FAIL ") + n + ("" if g else f"  [{d}]"))

with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1366, "height": 950})
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))

    page.goto(BASE + "/login")
    page.fill("#username", "admin"); page.fill("#password", "adminpass12345")
    page.click("#login-submit"); page.wait_for_url("**/app", timeout=15000)
    page.wait_for_timeout(1500)

    def go(view):
        page.click(f".rail-btn[data-view='{view}']"); page.wait_for_timeout(1400)

    # 1. БАГ СО СКРИНШОТА
    go("settings")
    text = page.inner_text("#view")
    note("нет текста 'null' на странице настроек", "null" not in text, text[:120].replace("\n"," "))

    # 2. Сканирование
    go("scan")
    note("раздел «Сканирование» открылся", "Сканирование" in page.inner_text("#view"))
    note("есть прогресс-бар", page.locator(".progress").count() == 1)
    note("есть история", "История" in page.inner_text("#view"))

    btn = page.locator("#view button").filter(has_text="только изменения").first
    btn.click(); page.wait_for_timeout(3500)
    after = page.inner_text("#view")
    note("скан завершился и показал итог", "Завершено" in after or "Готов" in after, after[:160].replace("\n"," "))

    # 3. Импорт
    go("import")
    note("раздел «Импорт» открылся", "Импорт плейлистов" in page.inner_text("#view"))
    page.fill("input[aria-label='Папка с плейлистами']", "/tmp/ui7_music")
    page.wait_for_timeout(1600)
    hint = page.inner_text("#dir-hint")
    note("проверка папки работает", "Найдено" in hint, hint)

    page.locator("#view button").filter(has_text="Импортировать").first.click()
    page.wait_for_timeout(3000)
    cards = page.locator(".import-card")
    note("разбор показал плейлисты", cards.count() >= 1, f"карточек={cards.count()}")
    preview = page.inner_text("#view")
    note("видно сопоставление", "треков" in preview, preview[:180].replace("\n"," "))

    if cards.count():
        page.locator("#view button").filter(has_text="Применить").first.click()
        page.wait_for_timeout(4000)
        icons = page.evaluate("() => Array.from(document.querySelectorAll('.import-status')).map(n=>n.textContent.trim())")
        note("после применения галочка/крестик", any(i in ("✓","×","—") for i in icons), str(icons))

    data = page.evaluate("""async () => {
      const r = await fetch('/api/web/playlists'); const d = await r.json();
      return d.data.items.map(p => ({name: p.name, count: p.song_count}));
    }""")
    note("плейлист с непустым именем", any(p["name"] and p["count"] > 0 for p in data), str(data))

    note("нет JS-исключений", not errs, "; ".join(errs[:2]))
    b.close()

print(f"\nИТОГ: {sum(res)}/{len(res)}")
sys.exit(0 if all(res) else 1)
