import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5003"
res = []
def note(n, g, d=""):
    res.append(g); print(("  ok   " if g else "  FAIL ") + n + ("" if g else f"  [{d}]"))

with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1500, "height": 950})
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))

    page.goto(BASE + "/login")
    page.fill("#username", "admin"); page.fill("#password", "adminpass12345")
    page.click("#login-submit"); page.wait_for_url("**/app", timeout=15000)
    page.wait_for_timeout(1500)

    page.click(".rail-btn[data-view='import']"); page.wait_for_timeout(1400)
    page.fill("input[aria-label='Папка с плейлистами']", "/tmp/dupui/music")
    page.wait_for_timeout(1600)

    page.locator("#view button").filter(has_text="Импортировать").first.click()
    page.wait_for_timeout(3000)

    cards = page.locator(".import-card")
    names = page.evaluate("""() => Array.from(document.querySelectorAll('.import-card strong'))
        .map(n => n.textContent.trim())""")
    # 3 рабочих плейлиста + 1 битый (Empty) с ошибкой в разборе.
    # Битый виден, чтобы пользователь знал о проблеме, но не импортируется.
    note("в разборе 4 карточки (3 рабочих + 1 битый)", len(names) == 4, str(names))
    note("нет дублей имён в разборе", len(names) == len(set(names)), str(names))
    errors = page.evaluate("""() => Array.from(document.querySelectorAll('.import-card'))
        .filter(c => c.querySelector('.error')).map(c => c.querySelector('strong').textContent.trim())""")
    note("битый файл помечен ошибкой", errors == ["Empty"], str(errors))
    warn = page.inner_text("#view")
    note("предупреждение о дубликатах показано", "дубликат" in warn.lower() or "дважды" in warn.lower())

    if cards.count():
        page.locator("#view button").filter(has_text="Применить").first.click()
        page.wait_for_timeout(4000)

    data = page.evaluate("""async () => {
      const r = await fetch('/api/web/playlists'); const d = await r.json();
      return d.data.items.map(p => ({name: p.name, count: p.song_count}));
    }""")
    note("создано ровно 3 плейлиста", len(data) == 3, str(data))
    names_after = [p["name"] for p in data]
    note("имена уникальны", len(names_after) == len(set(names_after)), str(names_after))
    note("нет плейлистов с 0 треков", all(p["count"] > 0 for p in data), str(data))

    # Повторный импорт не должен добавлять плейлисты
    page.click(".rail-btn[data-view='import']"); page.wait_for_timeout(1400)
    page.fill("input[aria-label='Папка с плейлистами']", "/tmp/dupui/music")
    page.wait_for_timeout(1400)
    page.locator("#view button").filter(has_text="Импортировать").first.click()
    page.wait_for_timeout(2500)
    page.locator("#view button").filter(has_text="Применить").first.click()
    page.wait_for_timeout(3000)

    data2 = page.evaluate("""async () => {
      const r = await fetch('/api/web/playlists'); const d = await r.json();
      return d.data.items.map(p => ({name: p.name, count: p.song_count}));
    }""")
    note("повторный импорт не создал дублей", len(data2) == len(data), str(data2))

    note("нет JS-исключений", not errs, "; ".join(errs[:2]))
    b.close()

print(f"\nИТОГ: {sum(res)}/{len(res)}")
sys.exit(0 if all(res) else 1)
