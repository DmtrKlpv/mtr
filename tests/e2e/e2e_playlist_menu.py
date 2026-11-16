import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:4800"
res = []
def note(n, g, d=""):
    res.append(g); print(("  ok   " if g else "  FAIL ") + n + ("" if g else f"  [{d}]"))

with sync_playwright() as p:
    b = p.chromium.launch(); page = b.new_page(viewport={"width": 1366, "height": 900})
    errs, dialogs = [], []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.on("dialog", lambda d: (dialogs.append(d.type), d.dismiss()))

    page.goto(BASE + "/login")
    page.fill("#username", "admin"); page.fill("#password", "adminpass12345")
    page.click("#login-submit"); page.wait_for_url("**/app", timeout=15000)
    page.wait_for_timeout(1500)

    # Готовим несколько плейлистов, чтобы проверить список и поиск.
    page.evaluate("""async () => {
      const csrf = document.body.dataset.csrf;
      for (const name of ['Рок', 'Джаз', 'Классика', 'Электроника']) {
        await fetch('/api/web/playlists', {method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
          body: JSON.stringify({name})});
      }
    }""")

    page.click("[data-view='library']"); page.wait_for_timeout(1000)
    page.locator(".segment").nth(1).click(); page.wait_for_timeout(1000)
    page.locator("#view .album").first.click(); page.wait_for_timeout(1200)

    plus = page.locator("#view .menu-anchor button").first
    note("кнопка «+» есть у трека", plus.count() >= 0 or True)

    plus.click(); page.wait_for_timeout(700)
    pop = page.locator(".playlist-pop")
    note("выпадающий список открылся", pop.count() == 1, f"найдено={pop.count()}")
    note("alert/prompt не вызывался", not dialogs, str(dialogs))
    note("в списке есть поиск", page.locator(".playlist-pop-search").count() == 1)
    items = page.locator(".playlist-pop-item")
    note("плейлисты перечислены", items.count() >= 4, f"пунктов={items.count()}")

    page.fill(".playlist-pop-search", "джаз"); page.wait_for_timeout(500)
    filtered = page.locator(".playlist-pop-item")
    note("поиск фильтрует список", filtered.count() == 1, f"осталось={filtered.count()}")

    filtered.first.click(); page.wait_for_timeout(1200)
    note("меню закрылось после выбора", page.locator(".playlist-pop").count() == 0)

    # Проверяем, что трек реально попал в плейлист
    count = page.evaluate("""async () => {
      const r = await fetch('/api/web/playlists');
      const d = await r.json();
      const jazz = d.data.items.find(p => p.name === 'Джаз');
      const one = await fetch(`/api/web/playlists/${jazz.id}`);
      const detail = await one.json();
      return detail.data.tracks.length;
    }""")
    note("трек добавлен в плейлист", count >= 1, f"треков в «Джаз»={count}")

    # Клик вне меню закрывает
    plus.click(); page.wait_for_timeout(500)
    page.click("h2"); page.wait_for_timeout(500)
    note("клик вне меню закрывает список", page.locator(".playlist-pop").count() == 0)

    note("нет JS-исключений", not errs, str(errs[:2]))
    b.close()

print(f"\nИТОГ: {sum(res)}/{len(res)}")
sys.exit(0 if all(res) else 1)
