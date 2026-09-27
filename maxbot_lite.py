"""maxbot-lite — однофайловый мост MAX (web.max.ru через живой Chromium) -> Telegram.
Неофициальная автоматизация MAX — возможен бан аккаунта, личное использование на свой страх.
"""
import asyncio
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from playwright.async_api import async_playwright

VERSION = "lite 1.1"
BASE = Path(__file__).resolve().parent
MAX_WEB_URL = "https://web.max.ru"
EVAL_TIMEOUT_S = 15.0


# --- .env (без внешних зависимостей: простые KEY=VALUE) ----------------------


def load_env() -> None:
    env_path = BASE / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


load_env()

TELEGRAM_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
OWNER_ID = int(os.environ.get("OWNER_TG_ID", "0") or 0)
HEADLESS = os.environ.get("HEADLESS", "false").strip().lower() == "true"
PROFILE_DIR = (BASE / os.environ.get("PROFILE_DIR", "./max_session")).resolve()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
# Мьют чатов: имена через запятую в .env (MUTED_CHATS="Спам, Рассылка").
# Совпадение по имени без регистра; уведомления по таким чатам не шлём.
MUTED_CHATS = {n.strip().lower()
               for n in os.environ.get("MUTED_CHATS", "").split(",")
               if n.strip()}

LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "bot-lite.log"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("lite")

if not TELEGRAM_TOKEN or not OWNER_ID:
    print("Заполни .env: TG_BOT_TOKEN и OWNER_TG_ID (см. .env.example).")
    sys.exit(1)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

_pw = None
browser_ctx = None
page = None
started_at = time.time()
active_chat_id: str | None = None
shutting = False            # флаг остановки: фоновые петли затихают молча

# --- page_lock (порт browser/page_lock.py 2.0, A-9): одна DOM-операция -------


class PageLock:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.holder = ""

    @asynccontextmanager
    async def guard(self, name: str, timeout: float = 30.0):
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("страница занята (%s) дольше %.0f с — %r выполняю "
                        "БЕЗ замка", self.holder, timeout, name)
            yield False
            return
        self.holder = name
        try:
            yield True
        finally:
            self.holder = ""
            self.lock.release()


PAGE_LOCK = PageLock()


async def ev(js: str, *args, timeout: float = EVAL_TIMEOUT_S):
    """evaluate с ТАЙМАУТОМ (2.0/2.4): подвисший CDP не возвращает управление
    никогда — без wait_for бот висел бы молча."""
    if page is None:
        raise RuntimeError("страницы нет (браузер перезапускается)")
    return await asyncio.wait_for(page.evaluate(js, *args), timeout=timeout)


# --- richText (порт browser/dom_text.py 2.0, бой 31.08) -----------------------
# Эмодзи в MAX это <span data-lexical-emoji>/<img alt> — innerText их не видит.
# ⚠️ Никаких реальных '\n' внутри JS-литерала.

RICH_TEXT_FN = r"""
const NL = String.fromCharCode(10);
const RT_BLOCK = {DIV: 1, P: 1, LI: 1, TR: 1, BUTTON: 1, SECTION: 1,
                  ARTICLE: 1, H1: 1, H2: 1, H3: 1, H4: 1};
const RT_BLOCK_DISPLAY = {block: 1, flex: 1, grid: 1, table: 1,
                          'list-item': 1, 'table-row': 1, 'flow-root': 1};
function rtIsBlock(node, tag) {
  if (RT_BLOCK[tag]) return true;
  try {
    const d = getComputedStyle(node).display;
    return !!RT_BLOCK_DISPLAY[d];
  } catch (e) { return false; }
}
function richText(el) {
  if (!el) return '';
  let out = '';
  for (const node of el.childNodes) {
    if (node.nodeType === 3) { out += node.nodeValue; continue; }
    if (node.nodeType !== 1) continue;
    const tag = node.tagName;
    if (tag === 'BR') { out += NL; continue; }
    const lex = node.getAttribute && node.getAttribute('data-lexical-emoji');
    if (lex) { out += lex; continue; }
    if (tag === 'IMG') {
      const alt = node.getAttribute('alt');
      if (alt) out += alt;
      continue;
    }
    if (node.hidden) continue;
    const inner = richText(node);
    if (!inner) continue;
    if (rtIsBlock(node, tag)) {
      if (out && !out.endsWith(NL)) out += NL;
      out += inner;
      if (!out.endsWith(NL)) out += NL;
    } else {
      out += inner;
    }
  }
  return out;
}
function rtNorm(s) {
  return (s || '').replace(/[ \t\u00a0\u200b]+/g, ' ').trim();
}
function richLines(el) {
  return richText(el).split(NL).map(s => rtNorm(s)).filter(Boolean);
}
"""

# --- Статусные превью — НЕ сообщения (порт media.py 2.0) ----------------------


STATUS_TAILS = (
    "печатает", "записывает аудио", "записывает видео",
    "отправляет фото", "отправляет файл",
    "присоединился к чату", "присоединилась к чату",
    "присоединился(-ась) к чату", "покинул чат", "покинула чат",
    "изменил описание", "изменила описание", "изменил название",
    "изменила название", "удалил сообщение", "удалила сообщение",
)


def is_status_preview(text: str) -> bool:
    s = " ".join((text or "").split())
    if " : " in s:
        return False
    low = s.lower()
    return any(low.endswith(t) for t in STATUS_TAILS)


# --- Браузер ------------------------------------------------------------------

_dom_changed = False


async def _on_dom_change():
    global _dom_changed
    _dom_changed = True


async def start_browser() -> None:
    global browser_ctx, page, _pw
    log.info("запускаю Chromium (профиль %s, headless=%s)...",
             PROFILE_DIR, HEADLESS)
    _pw = await async_playwright().start()
    browser_ctx = await _pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=HEADLESS,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 "
                   "Safari/537.36",
        viewport={"width": 1280, "height": 720},
        args=["--no-sandbox", "--disable-setuid-sandbox",
              "--disable-blink-features=AutomationControlled",
              "--disable-infobars"],
    )
    page = browser_ctx.pages[0] if browser_ctx.pages \
        else await browser_ctx.new_page()
    await page.goto(MAX_WEB_URL, wait_until="networkidle")
    await asyncio.sleep(4)
    await _register_observer()
    log.info("браузер запущен, страница MAX открыта")


async def _register_observer():
    try:
        await page.expose_function("on_lite_dom_change", _on_dom_change)
    except Exception:
        pass  # уже зарегистрирован на этой странице
    await page.evaluate("""() => {
        window.__liteObserver = new MutationObserver(() => {
            window.on_lite_dom_change();
        });
        window.__liteObserver.observe(document.body,
            {childList: true, subtree: true, characterData: true});
    }""")


async def restart_browser():
    """Полный перезапуск браузера (семантика engine.restart из 2.0/2.4):
    1 с на отпускание профиля, пересоздание, регистрация обсервера заново."""
    global browser_ctx, page, active_chat_id
    try:
        if browser_ctx:
            await browser_ctx.close()
    except Exception as e:
        log.warning("закрытие браузера: %r", e)
    browser_ctx = None
    page = None
    await asyncio.sleep(1.0)
    await start_browser()
    active_chat_id = None


# --- Список чатов (порт poller.py 2.0) -----------------------------------------

CHATS_JS = r"""
() => {
  __RICH_TEXT__
  const cells = [...document.querySelectorAll('button[class*="cell"]')]
    .filter(b => !b.className.includes('cell--webapp'))
    .filter(b => !b.closest('div.modal,[role="dialog"],[data-testid="popoverPortal"]'));
  const out = [];
  for (const el of cells) {
    const raw = richLines(el);
    if (raw.length < 2) continue;
    const name = raw[0];
    if (!name) continue;
    let i = 1, unread = 0;
    if (/^\d{1,3}$/.test(raw[i] || '')) { unread = parseInt(raw[i]); i++; }
    const last_message = raw.slice(i, raw.length - 1).join(' ').trim();
    const cell_time = raw[raw.length - 1] || '';
    let id = el.getAttribute('data-bot-id');
    if (!id) {
      let h = 0;
      for (let j = 0; j < name.length; j++) {
        h = ((h << 5) - h) + name.charCodeAt(j); h |= 0;
      }
      id = 'chat_' + Math.abs(h).toString(36);
    }
    el.setAttribute('data-bot-id', id);
    out.push({id, name, last_message, cell_time, unread});
  }
  return out;
}
"""
CHATS_JS = CHATS_JS.replace("__RICH_TEXT__", RICH_TEXT_FN)

# Список чатов ЛЕНИВЫЙ (замер 06.09: из 25 чатов в DOM 17): нижние появляются
# только у окна прокрутки. Контейнер — ближайший .scrollable предок ячейки.

CHAT_LIST_INFO_JS = r"""
() => {
  const cells = [...document.querySelectorAll('button[class*="cell"]')]
    .filter(b => !b.className.includes('cell--webapp'))
    .filter(b => !b.closest('div.modal,[role="dialog"],[data-testid="popoverPortal"]'));
  const sc = cells.length ? cells[0].closest('.scrollable') : null;
  if (!sc) return null;
  return {top: sc.scrollTop, h: sc.clientHeight, full: sc.scrollHeight};
}
"""
CHAT_LIST_SCROLL_JS = r"""
(target) => {
  const cells = [...document.querySelectorAll('button[class*="cell"]')]
    .filter(b => !b.className.includes('cell--webapp'))
    .filter(b => !b.closest('div.modal,[role="dialog"],[data-testid="popoverPortal"]'));
  const sc = cells.length ? cells[0].closest('.scrollable') : null;
  if (!sc) return null;
  sc.scrollTop = target;
  return {top: sc.scrollTop, h: sc.clientHeight, full: sc.scrollHeight};
}
"""


def looks_today(cell_time: str) -> bool:
    """Время ячейки — время суток («9:05») => сообщение сегодняшнее (B-61)."""
    return bool(re.fullmatch(r"\d{1,2}:\d{2}", (cell_time or "").strip()))


# --- Состояние поллера ---------------------------------------------------------

known: dict = {}
chats_order: list = []
sent_recent: dict = {}          # chat_id -> [(text, ts)] — слой 2 анти-эха
force_first_pass = asyncio.Event()
INVENTORY_EVERY_S = 600.0
OWN_MARKER = "Вы:"


def remember_sent(chat_id: str, text: str) -> None:
    sent_recent.setdefault(chat_id, []).append(
        (" ".join(text.split()), time.time()))
    sent_recent[chat_id] = sent_recent[chat_id][-8:]


def is_own_echo(chat_id: str, preview: str) -> bool:
    """Своё исходящее. Слой 1 (бой 31.08, 2.0): маркер «Вы: » гасим ВСЕГДА —
    сообщения с телефона/из MAX в реестре бота не живут. Слой 2: реестр
    отправленного через бота — для чатов без маркера (сам себе в Избранном)."""
    norm = " ".join(preview.split())
    if norm.startswith(OWN_MARKER):
        return True
    now = time.time()
    kept, matched = [], False
    for text, ts in sent_recent.get(chat_id, []):
        if now - ts > 180:
            continue
        kept.append((text, ts))
        if not matched and norm == text:
            matched = True
            continue
    sent_recent[chat_id] = kept
    return matched


# --- Поллер (порт poller.py 2.0) -----------------------------------------------


async def notify(c: dict, why: str) -> None:
    # Мьют (.env MUTED_CHATS): по такому чату молчим целиком — ни аддонам,
    # ни в личку. Хочешь считать их в сводке — убери проверку из notify.
    if (c.get("name") or "").lower() in MUTED_CHATS:
        return
    # Аддоны: даём наблюдать каждое обнаруженное новое сообщение (сводки,
    # фильтры и пр.). Ошибка аддона не должна ломать доставку уведомления.
    await _fire_msg_hooks(c, why)
    text = f"⬅️ <b>{c['name']}</b>: {c['last_message'][:600]}"
    builder = InlineKeyboardBuilder()
    builder.button(text="Открыть чат", callback_data=f"open_chat:{c['id']}")
    for attempt in (1, 2):
        try:
            await bot.send_message(
                chat_id=OWNER_ID, text=text, parse_mode=ParseMode.HTML,
                reply_markup=builder.as_markup())
            log.info("уведомление: %s — %r (%s)", c["name"],
                     c["last_message"][:50], why)
            return
        except Exception as e:
            retry = getattr(e, "retry_after", None)
            if retry and attempt == 1:
                await asyncio.sleep(float(retry) + 1)
                continue
            log.warning("уведомление не ушло: %r", e)
            return


async def inventory() -> int:
    """Прокрутить список чатов и молча доложить новые в кэш (2.0, баг №4)."""
    if shutting or PAGE_LOCK.lock.locked():
        return 0
    async with PAGE_LOCK.guard("инвентаризация", timeout=2.0) as got:
        if not got:
            return 0
        info = await ev(CHAT_LIST_INFO_JS)
        if not info or info["full"] <= info["h"] + 10:
            return 0
        added = 0

        async def on_step():
            nonlocal added
            for c in await ev(CHATS_JS) or []:
                if not c.get("name") or c["name"] == "Неизвестный чат" \
                        or c["id"] in known:
                    continue
                if not (c.get("last_message") or "").strip():
                    continue
                known[c["id"]] = c
                added += 1

        try:
            prev_top = info["top"]
            for _ in range(12):
                nxt = await ev(CHAT_LIST_SCROLL_JS, prev_top + info["h"] * 0.9)
                if not nxt or nxt["top"] <= prev_top:
                    break
                prev_top = nxt["top"]
                await asyncio.sleep(0.4)
                await on_step()
        finally:
            await ev(CHAT_LIST_SCROLL_JS, 0)
        return added


async def poller_loop():
    global _dom_changed, chats_order
    first_pass = True
    last_inventory = time.monotonic() - (INVENTORY_EVERY_S - 45.0)
    while not shutting:
        try:
            if force_first_pass.is_set():
                force_first_pass.clear()
                first_pass = True   # после рестарта весь DOM — фон
            for _ in range(10):
                await asyncio.sleep(0.1)
                if _dom_changed:
                    break
            _dom_changed = False
            if page is None:
                await asyncio.sleep(1.0)
                continue

            chats_data = await ev(CHATS_JS)
            if not chats_data:
                continue
            unique, seen = [], set()
            for c in chats_data:
                if c["name"] in seen or c["name"] == "Неизвестный чат":
                    continue
                seen.add(c["name"])
                unique.append(c)

            for c in unique:
                old = known.get(c["id"])
                if old is None:
                    # Новый чат: сегодняшнее время = свежее сообщение (B-61),
                    # иначе молча в кэш — промотка не должна уведомлять старьё.
                    if not first_pass and looks_today(c["cell_time"]) \
                            and (c["last_message"] or "").strip() \
                            and not is_own_echo(c["id"], c["last_message"]) \
                            and not is_status_preview(c["last_message"]):
                        await notify(c, "новый чат")
                    known[c["id"]] = c
                    continue
                grew = (c.get("unread") or 0) > (old.get("unread") or 0)
                changed = old["last_message"] != c["last_message"]
                if (changed or grew) and (c["last_message"] or "").strip():
                    if not is_own_echo(c["id"], c["last_message"]) \
                            and not is_status_preview(c["last_message"]):
                        await notify(c, "превью" if changed else "непрочитанные")
                known[c["id"]] = c

            chats_order = [{"id": c["id"], "name": c["name"],
                            "unread": c.get("unread", 0)} for c in unique]
            first_pass = False

            if time.monotonic() - last_inventory >= INVENTORY_EVERY_S:
                last_inventory = time.monotonic()
                added = await inventory()
                if added:
                    log.info("инвентаризация: +%s чатов из-под подгиба "
                             "(всего %s)", added, len(known))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if shutting:
                break
            log.warning("поллер: %r", e)
            await asyncio.sleep(1.0)


# --- ДЕЙСТВИЯ: порты msg_actions.py / actions.py / media.py из 2.0 -------------


async def find_real_by_index(page_, data_index: int):
    """§1.5: data-index в DOM дублируется (плейсхолдер и реальный элемент)."""
    for cand in await page_.query_selector_all(f'[data-index="{data_index}"]'):
        if await cand.query_selector('[class*="messageWrapper"]'):
            return cand
    return None


SCAN_ITEMS_JS = r"""
() => {
  __RICH_TEXT__
  const out = [];
  for (const el of document.querySelectorAll('[data-index]')) {
    if (!el.querySelector('[class*="messageWrapper"]')) continue;
    const bubble = el.querySelector('[class*="bubbleContent"]');
    let text = '';
    if (bubble) {
      for (const span of bubble.children) {
        if (span.tagName === 'SPAN' && span.classList.contains('text')
            && !span.classList.contains('meta')) {
          text = rtNorm(richText(span));
          break;
        }
      }
    }
    const timeEl = el.querySelector('div[class*="meta--text"] > span[class*="text"]')
                || el.querySelector('div[class*="meta--bubbled"] span[class*="text"]');
    out.push({
      index: parseInt(el.getAttribute('data-index')),
      text: text,
      time: timeEl ? (timeEl.innerText || '').trim() : '',
      isOut: !!el.querySelector('[class*="isOut"]')
    });
  }
  return out;
}
"""
SCAN_ITEMS_JS = SCAN_ITEMS_JS.replace("__RICH_TEXT__", RICH_TEXT_FN)

ITEM_TEXT_JS = r"""
(idx) => {
  __RICH_TEXT__
  for (const el of document.querySelectorAll('[data-index="' + idx + '"]')) {
    if (!el.querySelector('[class*="messageWrapper"]')) continue;
    const bubble = el.querySelector('[class*="bubbleContent"]');
    if (!bubble) return '';
    for (const span of bubble.children) {
      if (span.tagName === 'SPAN' && span.classList.contains('text')
          && !span.classList.contains('meta')) {
        return rtNorm(richText(span));
      }
    }
    return '';
  }
  return null;
}
"""
ITEM_TEXT_JS = ITEM_TEXT_JS.replace("__RICH_TEXT__", RICH_TEXT_FN)


def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()


async def item_text_at(page_, data_index: int) -> str | None:
    """Текст пузыря по data_index, тем же richText (иначе эмодзи выпадут)."""
    try:
        return await ev(ITEM_TEXT_JS, data_index)
    except Exception as e:
        log.debug("текст элемента #%s не прочитался: %r", data_index, e)
        return None


async def count_items(page_) -> int:
    try:
        return await ev("""() => Array.from(document.querySelectorAll('[data-index]'))
             .filter(el => el.querySelector('[class*="messageWrapper"]'))
             .length""")
    except Exception as e:
        log.debug("счёт элементов не удался: %r", e)
        return -1


async def count_text(page_, text: str) -> int:
    key = _norm(text)
    if not key:
        return 0
    try:
        items = await ev(SCAN_ITEMS_JS)
    except Exception as e:
        log.debug("скан для счёта текста не удался: %r", e)
        return -1
    return sum(1 for it in items if _norm(it.get("text") or "") == key)


async def freeze_videos(page_) -> None:
    """Каналы с зацикленными видео перерисовывают DOM и срывают наведение
    (бой 29.08). Пауза без снятия src — B-31."""
    try:
        await page_.evaluate(
            "() => document.querySelectorAll('video')"
            ".forEach(v => { try { v.pause(); } catch (e) {} })")
    except Exception:
        pass


async def hover_item(page_, data_index: int) -> bool:
    """Наведение по ПУЗЫРЮ с двойным mouse.move (MAX не считает одиночное
    перемещение наведением) + подтверждение, что messageControls появились."""
    item = await find_real_by_index(page_, data_index)
    if not item:
        return False
    try:
        await item.scroll_into_view_if_needed()
        await asyncio.sleep(0.25)
        zone = (await item.query_selector('[class*="bubbleContent"]')
                or await item.query_selector('[class*="messageWrapper"]')
                or item)
        box = await zone.bounding_box()
        if not box:
            return False
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        await page_.mouse.move(cx - 4, cy - 4)
        await asyncio.sleep(0.15)
        await page_.mouse.move(cx, cy)
        await asyncio.sleep(0.5)
        controls = await item.query_selector('[class*="messageControls"] button')
        return controls is not None
    except Exception:
        return False


LABELS_REPLY = ("Ответить", "Reply")
LABELS_ACTIONS = ("Действия с сообщением", "Message actions")
LABELS_DELETE_MENU = ("Удалить", "Delete")


async def controls_button(page_, data_index: int, labels: tuple[str, ...]):
    item = await find_real_by_index(page_, data_index)
    if not item:
        return None
    for label in labels:
        btn = await item.query_selector(
            f'[class*="messageControls"] button[aria-label="{label}"]')
        if btn:
            return btn
    for label in labels:
        btn = await page_.query_selector(f'button[aria-label="{label}"]')
        if btn:
            return btn
    return None


# Popover по id из aria-controls БЕЗ CSS-селектора (A-4: UUID бывает с цифры,
# query_selector('#id') бросал SyntaxError — действия не работали в 60%).

async def popover_by_id(page_, popover_id):
    if not popover_id:
        return None
    try:
        handle = await page_.evaluate_handle(
            "id => document.getElementById(id)", popover_id)
    except Exception:
        return None
    element = handle.as_element()
    if element is None:
        await handle.dispose()
    return element


async def open_message_actions(page_, data_index: int, retries: int = 2):
    """Навести и открыть popover действий. Ретраи с заморозкой видео (§27,
    каналы). Возвращает (status, popover_handle)."""
    status, popover = "start", None
    for attempt in range(retries + 1):
        await freeze_videos(page_)
        if attempt:
            try:
                await page_.mouse.move(10, 10)
            except Exception:
                pass
        try:
            if await hover_item(page_, data_index):
                btn = await controls_button(page_, data_index, LABELS_ACTIONS)
                if not btn:
                    status = "no_actions_button"
                else:
                    controls_id = await btn.get_attribute("aria-controls")
                    await btn.click()
                    await asyncio.sleep(0.6)
                    popover = await popover_by_id(page_, controls_id)
                    if popover is None:
                        for cand in reversed(await page_.query_selector_all(
                                '[class*="popover"]')):
                            try:
                                if await cand.is_visible():
                                    popover = cand
                                    break
                            except Exception:
                                continue
                    if popover is None:
                        status = "no_popover"
                        try:
                            await page_.keyboard.press("Escape")
                        except Exception:
                            pass
                    else:
                        return "ok", popover
            else:
                status = "hover_fail"
        except Exception as e:
            status = f"error({type(e).__name__})"
            try:
                await page_.keyboard.press("Escape")
            except Exception:
                pass
        await asyncio.sleep(0.5)
    return status, None


MENU_ITEM_CLICK_JS = r"""
(payload) => {
  const {popId, labels} = payload;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const zone = (popId && document.getElementById(popId))
            || document.querySelector('[data-testid="popoverPortal"]');
  if (!zone) return 'no_popover';
  const items = Array.from(zone.querySelectorAll('button,[role="menuitem"]'));
  for (const want of labels.map(norm)) {
    for (const b of items) {
      if (norm(b.getAttribute('aria-label')) === want
          || norm(b.innerText) === want) { b.click(); return 'ok'; }
    }
  }
  return 'no_item';
}
"""


async def menu_click(page_, popover, labels: tuple[str, ...]) -> str:
    pid = await popover.get_attribute("id") if popover else None
    return await ev(MENU_ITEM_CLICK_JS, {"popId": pid, "labels": list(labels)})


async def reply_to_item(page_, data_index: int, text: str) -> str:
    """Ответ на сообщение (порт msg_actions.reply_to_item 2.0)."""
    if not await hover_item(page_, data_index):
        return "hover_fail"
    btn = await controls_button(page_, data_index, LABELS_REPLY)
    if not btn:
        return "no_reply_button"
    await btn.click()
    await asyncio.sleep(0.4)
    editor = await page_.query_selector('[data-lexical-editor="true"]')
    if editor:
        await editor.click()
    await asyncio.sleep(0.2)
    await page_.keyboard.insert_text(text)
    await asyncio.sleep(0.2)
    send_btn = await page_.query_selector('button[aria-label="Send message"]')
    if not send_btn:
        send_btn = await page_.query_selector(
            'button[aria-label="Отправить сообщение"]')
    if send_btn:
        await send_btn.click()
    else:
        await page_.keyboard.press("Enter")
    await asyncio.sleep(0.3)
    return "ok"


# Подтверждение удаления: строго в НАСТОЯЩИХ модалках (B-20/B-68: popover
# меню тоже role="dialog" — пункт «Удалить» в нём совпадал со словом
# подтверждения, и «ok» возвращалось на неудалённом).

REAL_MODALS_FN = r"""
function realModals() {
  const out = [];
  const nodes = document.querySelectorAll(
    'div.modal,[role="dialog"],[role="alertdialog"]');
  for (const z of nodes) {
    const cls = (z.className || '') + '';
    if (/popover/i.test(cls) || z.hasAttribute('popover')) continue;
    if (z.closest && z.closest('[data-testid="popoverPortal"]')) continue;
    const box = z.getBoundingClientRect();
    if (box.width < 100 || box.height < 40) continue;
    out.push(z);
  }
  return out;
}
"""

DELETE_CONFIRM_JS = r"""
() => {
  __REAL_MODALS__
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const words = ['удалить', 'delete', 'delete for all', 'удалить у всех',
                 'удалить для всех', 'confirm', 'подтвердить', 'да'];
  for (const z of realModals()) {
    for (const b of z.querySelectorAll('button')) {
      const label = norm(b.getAttribute('aria-label')) || norm(b.innerText);
      if (!label) continue;
      if (words.some(w => label === w || label.includes(w))) {
        b.click();
        return 'ok:' + label;
      }
    }
  }
  return 'no_modal';
}
""".replace("__REAL_MODALS__", REAL_MODALS_FN)


async def confirm_delete(page_, attempts: int = 8) -> str:
    """Ждём модалку до ~4 с (без ожидания подтверждение промахивалось)."""
    for _ in range(attempts):
        res = await ev(DELETE_CONFIRM_JS)
        if res.startswith("ok"):
            return res
        await asyncio.sleep(0.5)
    return "no_modal"


async def delete_item(page_, data_index: int, only_own: bool = True) -> str:
    """Удаление (порт msg_actions.delete_item 2.0). 'ok' ТОЛЬКО когда
    сообщение реально исчезло: проверка по ПОДСЧЁТУ ТЕКСТА (бой 03.09 —
    data_index перенумеровывается), для медиа — по числу элементов,
    резерв — исчезновение индекса."""
    if only_own:
        own = await ev("""(idx) => {
            for (const el of document.querySelectorAll('[data-index="' + idx + '"]')) {
                if (!el.querySelector('[class*="messageWrapper"]')) continue;
                return el.querySelector('[class*="isOut"]') ? 'ok' : 'not_own';
            }
            return 'not_found';
        }""", data_index)
        if own != "ok":
            return own

    before_text = await item_text_at(page_, data_index)
    before_count = (await count_text(page_, before_text) if before_text else 0)
    before_items = await count_items(page_)

    status, popover = await open_message_actions(page_, data_index)
    if status != "ok":
        return status
    clicked = await menu_click(page_, popover, LABELS_DELETE_MENU)
    if clicked != "ok":
        await page_.keyboard.press("Escape")
        return "no_delete_item"

    confirmed = await confirm_delete(page_)
    if confirmed == "no_modal":
        log.warning("[delete] модалка подтверждения не появилась (#%s)",
                    data_index)
        try:
            await page_.keyboard.press("Escape")
        except Exception:
            pass
        return "no_confirm_modal"
    log.info("[delete] подтверждение нажато: %s (#%s)", confirmed, data_index)

    for _ in range(6):
        await asyncio.sleep(0.5)
        if before_text:
            if await count_text(page_, before_text) < before_count:
                return "ok"
        elif await count_items(page_) < before_items:
            return "ok"
        if await find_real_by_index(page_, data_index) is None:
            return "ok"
    log.warning("[delete] сообщение #%s всё ещё в истории (текст %r)",
                data_index, (before_text or "")[:40])
    return "still_present"


# --- Открытие чата и отправка текста (порт actions.py 2.0, B-19/B-18) ----------

TOPBAR_TITLE_JS = r"""
() => {
  const tb = document.querySelector('div.openedChat div.topbar')
          || document.querySelector('[class*="topbar"]');
  if (!tb) return null;
  const lines = (tb.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
  return lines.length ? lines[0] : null;
}
"""

FIND_CELL_BY_TITLE_JS = r"""
(payload) => {
  __RICH_TEXT__
  const {title, chatId} = payload;
  if (!title) return false;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(title);
  for (const cell of document.querySelectorAll('button[class*="cell"]')) {
    const lines = richLines(cell);
    if (!lines.length) continue;
    if (norm(lines[0]) === want) {
      cell.setAttribute('data-bot-id', chatId);
      cell.scrollIntoView({block: 'nearest'});
      cell.click();
      return true;
    }
  }
  return false;
}
"""
FIND_CELL_BY_TITLE_JS = FIND_CELL_BY_TITLE_JS.replace("__RICH_TEXT__",
                                                      RICH_TEXT_FN)


async def topbar_shows(page_, title: str) -> bool | None:
    try:
        shown = await ev(TOPBAR_TITLE_JS)
    except Exception:
        return None
    if not shown:
        return None
    norm = lambda s: " ".join((s or "").split()).strip().lower()
    return norm(title) in norm(shown)


async def open_chat(page_, chat_id: str, title: str | None) -> str | None:
    """Порт actions.open_chat 2.0 (B-19): клик -> сверка топбара -> ретраи ->
    фолбэк поиск по имени (в т.ч. прокруткой списка). None = успех."""
    for attempt in range(6):
        clicked = False
        el = await page_.query_selector(f"[data-bot-id='{chat_id}']")
        if el:
            try:
                await el.scroll_into_view_if_needed()
            except Exception:
                pass
            await el.click()
            await asyncio.sleep(0.8)
            clicked = True
        elif title:
            if await ev(FIND_CELL_BY_TITLE_JS,
                        {"title": title, "chatId": chat_id}):
                await asyncio.sleep(0.8)
                clicked = True
        if clicked:
            if not title:
                return None
            for _ in range(4):
                ok = await topbar_shows(page_, title)
                if ok:
                    return None
                if ok is None:
                    await asyncio.sleep(0.4)
                    continue
                break
            log.warning("клик по %s открыл НЕ тот чат (ждали %r), попытка %s",
                        chat_id, title, attempt + 1)
        await asyncio.sleep(0.4)

    # 06.09: чат мог быть НИЖЕ ПОДГИБА виртуального списка — ищем прокруткой.
    if title:
        clicked = False

        async def _try_click():
            nonlocal clicked
            if clicked:
                return
            if await ev(FIND_CELL_BY_TITLE_JS,
                        {"title": title, "chatId": chat_id}):
                await asyncio.sleep(0.8)
                for _ in range(4):
                    ok = await topbar_shows(page_, title)
                    if ok:
                        clicked = True
                        return
                    if ok is None:
                        await asyncio.sleep(0.4)

        info = await ev(CHAT_LIST_INFO_JS)
        if info:
            try:
                prev_top = info["top"]
                for _ in range(12):
                    nxt = await ev(CHAT_LIST_SCROLL_JS,
                                   prev_top + info["h"] * 0.9)
                    if not nxt or nxt["top"] <= prev_top:
                        break
                    prev_top = nxt["top"]
                    await asyncio.sleep(0.4)
                    await _try_click()
                    if clicked:
                        break
            finally:
                await ev(CHAT_LIST_SCROLL_JS, 0)
        if clicked:
            return None

    return "no_chat_element" if title is None else "wrong_chat_opened"


COMPOSER_TEXT_JS = r"""() => {
  const c = document.querySelector('div.composer')
         || document.querySelector('[class*="composer"]');
  if (!c) return null;
  const ed = c.querySelector('[data-lexical-editor="true"]')
          || c.querySelector('div[contenteditable="true"]');
  return ed ? (ed.innerText || '').trim() : null;
}"""


async def send_text(page_, chat_id: str, text: str,
                    title: str | None = None) -> str:
    """Порт actions.send_text 2.0 (B-18): композер пуст = ушло; None = верим
    отправке (проверить нечем); непустой после 5 попыток = НЕ отправлено."""
    reason = await open_chat(page_, chat_id, title)
    if reason:
        return reason
    editor = await page_.query_selector('[data-lexical-editor="true"]')
    if editor:
        await editor.click()
    else:
        await ev("""() => {
            const inputs = Array.from(document.querySelectorAll(
                "div[contenteditable='true'], p[contenteditable='true'], " +
                "div[role='textbox'], textarea"));
            if (inputs.length > 0) {
                const target = inputs.sort((a, b) =>
                    b.getBoundingClientRect().bottom -
                    a.getBoundingClientRect().bottom)[0];
                if (target) { target.focus(); target.click(); }
            }
        }""")
    await asyncio.sleep(0.2)
    await page_.keyboard.insert_text(text)
    await asyncio.sleep(0.2)
    send_btn = await page_.query_selector('button[aria-label="Send message"]')
    if not send_btn:
        send_btn = await page_.query_selector(
            'button[aria-label="Отправить сообщение"]')
    if send_btn:
        await send_btn.click()
    else:
        await page_.keyboard.press("Enter")
    for _ in range(5):
        await asyncio.sleep(0.4)
        try:
            left = await ev(COMPOSER_TEXT_JS)
        except Exception:
            return "ok"
        if not left:
            return "ok"
    log.error("send_text: в композере осталось %r — НЕ отправлено",
              (left or "")[:60])
    return "composer_not_empty"


# --- Загрузка файлов (порт media.py upload_files 2.0: B-26/B-27) ---------------

JS_CAMERA_ZONE = r"""() => {
    const uses = document.querySelectorAll('use[href="#icon_camera_big"]');
    for (const use of uses) {
        let el = use.parentElement;
        for (let i = 0; i < 6; i++) {
            if (!el) break;
            const r = el.getBoundingClientRect();
            if (r.width > 100 && r.height > 80)
                return {x: r.left + r.width / 2, y: r.top + r.height / 2};
            el = el.parentElement;
        }
    }
    return null;
}"""

PARSE_HISTORY_JS = r"""
(limit) => {
  __RICH_TEXT__
  const result = [];
  let histArea = document.querySelector('div.history');
  if (!histArea) histArea = document.querySelector('[class*="scrollListContent--bottom"]');
  if (!histArea) histArea = document.querySelector('[class*="scrollListContent--top"]');
  if (!histArea) return [];
  for (const item of histArea.querySelectorAll('[data-index]')) {
    if (!item.querySelector('[class*="messageWrapper"]')) continue;
    const isOut = !!(item.querySelector('[class*="messageWrapper"]') || {})
                      .className?.includes?.('isOut');
    const timeEl = item.querySelector('div[class*="meta--text"] > span[class*="text"]') ||
                   item.querySelector('div[class*="meta--bubbled"] span[class*="text"]');
    const timeStr = timeEl ? (timeEl.innerText || '').trim() : '';
    const dataIndex = parseInt(item.getAttribute('data-index') || '-1');
    const attachAudio = item.querySelector('[class*="attachAudio"]');
    if (attachAudio) { result.push({type: 'voice', time: timeStr, is_out: isOut, data_index: dataIndex}); continue; }
    if (item.querySelector('[class*="videoMessage"]')) {
        result.push({type: 'circle', time: timeStr, is_out: isOut, data_index: dataIndex}); continue; }
    let hasMedia = false;
    const grid = item.querySelector('[class*="grid"][aria-label]');
    if (grid) {
      let photos = 0, videos = 0;
      for (const tile of grid.querySelectorAll('button[class*="tile"]')) {
        if (tile.querySelector('video')) { videos++; continue; }
        const img = tile.querySelector('img[class*="image"]');
        const src = img ? (img.getAttribute('src') || '') : '';
        if (img && src && !src.includes('fn=sqr') && !src.includes('fn=w_180')) photos++;
      }
      if (photos || videos) {
        hasMedia = true;
        const parts = [];
        if (photos) parts.push(`${photos} фото`);
        if (videos) parts.push(`${videos} видео`);
        result.push({type: 'media', text: `[${parts.join(' + ')}]`, time: timeStr,
                     is_out: isOut, data_index: dataIndex});
      }
    }
    if (!hasMedia && !attachAudio) {
      const attachBtn = item.querySelector('[class*="attaches"] button[class*="container"]');
      if (attachBtn) {
        const titleEl = attachBtn.querySelector('[class*="title"]');
        result.push({type: 'file',
                     filename: titleEl ? rtNorm(richText(titleEl)) : 'файл',
                     time: timeStr, is_out: isOut, data_index: dataIndex});
      }
    }
    const bubble = item.querySelector('[class*="bubbleContent"]');
    if (bubble) {
      for (const span of bubble.children) {
        if (span.tagName === 'SPAN' && span.classList.contains('text')
            && !span.classList.contains('meta')) {
          const text = rtNorm(richText(span));
          if (text) result.push({type: 'text', text, time: timeStr,
                                 is_out: isOut, data_index: dataIndex});
          break;
        }
      }
    }
  }
  return result.slice(-limit);
}
"""
PARSE_HISTORY_JS = PARSE_HISTORY_JS.replace("__RICH_TEXT__", RICH_TEXT_FN)


async def parse_history(limit: int = 20) -> list:
    return await ev(PARSE_HISTORY_JS, limit) or []


def item_sig(item: dict) -> tuple:
    """Подпись элемента, устойчивая к перенумерации data_index (B-27)."""
    return (item.get("type"), item.get("time"),
            (item.get("filename") or "").lower(),
            (item.get("text") or "")[:40])


async def upload_file(page_, chat_id: str, title: str | None,
                      path: Path, kind: str) -> str:
    """Порт media.upload_files 2.0 (без подписей). 'ok' или причина.
    Доставка проверяется ПО ИСТОРИИ (подписи), а не по data_index (B-27).
    CDP-сессия снимается в finally (B-26)."""
    async with PAGE_LOCK.guard("отправка файла", timeout=60):
        reason = await open_chat(page_, chat_id, title)
        if reason:
            return reason

        before = await parse_history(10)
        before_sigs = {item_sig(it) for it in before}
        fname_lower = path.name.lower()

        try:
            cdp = await page_.context.new_cdp_session(page_)
            await cdp.send("Input.setInterceptDrags", {"enabled": True})
        except Exception as e:
            return f"cdp_fail({e!r})"

        try:
            rect = await ev("""() => {
                const el = document.querySelector('[class*="openedChat"]') || document.body;
                const r = el.getBoundingClientRect();
                return {x: r.left + r.width / 2, y: r.top + r.height / 2,
                        left: r.left, top: r.top, width: r.width, height: r.height};
            }""")
            x, y = rect["x"], rect["y"]
            drag_data = {"items": [], "files": [str(path)],
                         "dragOperationsMask": 1}

            if kind == "media":
                await cdp.send("Input.dispatchDragEvent",
                               {"type": "dragEnter", "x": x, "y": y,
                                "data": drag_data})
                zone = None
                for _ in range(6):          # зона дорисовывается не мгновенно
                    await asyncio.sleep(0.25)
                    zone = await ev(JS_CAMERA_ZONE)
                    if zone:
                        break
                if zone:
                    tx, ty = zone["x"], zone["y"]
                else:
                    tx = rect["left"] + rect["width"] / 2
                    ty = rect["top"] + rect["height"] * 0.75
                    log.warning("[upload] зона камеры не найдена, fallback")
                await cdp.send("Input.dispatchDragEvent",
                               {"type": "dragOver", "x": tx, "y": ty,
                                "data": drag_data})
                await asyncio.sleep(0.1)
                await cdp.send("Input.dispatchDragEvent",
                               {"type": "drop", "x": tx, "y": ty,
                                "data": drag_data})
            else:
                await cdp.send("Input.dispatchDragEvent",
                               {"type": "dragEnter", "x": x, "y": y,
                                "data": drag_data})
                await cdp.send("Input.dispatchDragEvent",
                               {"type": "dragOver", "x": x, "y": y,
                                "data": drag_data})
                await cdp.send("Input.dispatchDragEvent",
                               {"type": "drop", "x": x, "y": y,
                                "data": drag_data})

            # Медиа кладётся в композер и требует Send; не-медиа файлы
            # современный MAX шлёт СРАЗУ при дропе (урок 23.08).
            attach = None
            for _ in range(10):
                await asyncio.sleep(0.5)
                attach = await page_.query_selector(
                    'div.composer div.attaches div.attach')
                if attach:
                    break
            if attach is not None:
                await asyncio.sleep(0.5)
                send_btn = await page_.query_selector(
                    'button[aria-label="Send message"]') \
                    or await page_.query_selector(
                        'button[aria-label="Отправить сообщение"]') \
                    or await page_.query_selector('button[class*="send"]')
                if not send_btn:
                    return "no_send_button"
                await send_btn.click()
                await asyncio.sleep(0.5)
                return "ok"

            # Композера не было: проверяем доставку ПО ИСТОРИИ.
            want_types = (("photo", "video", "circle", "file")
                          if kind == "media" else ("file",))
            items = await parse_history(10)
            for it in reversed(items or []):
                if it.get("type") not in want_types:
                    continue
                fn = (it.get("filename") or "").lower()
                if fn:
                    if fname_lower not in fn:
                        continue            # файл есть, но чужой
                elif kind != "media":
                    continue
                if item_sig(it) not in before_sigs:
                    return "ok"
            return "not_delivered"
        finally:
            try:
                await cdp.send("Input.setInterceptDrags", {"enabled": False})
            except Exception:
                pass
            try:
                await cdp.detach()
            except Exception:
                pass


# --- Watchdog (порт watchdog.py 2.4: проба с таймаутом, перезапуск) ------------


async def watchdog_loop():
    global active_chat_id
    fails = 0
    await asyncio.sleep(30)
    while not shutting:
        try:
            await asyncio.sleep(30)
            if page is None:
                fails += 1
            else:
                await asyncio.wait_for(
                    page.evaluate("() => document.title"), timeout=15)
                fails = 0
                if "web.max.ru" not in (page.url or ""):
                    log.warning("watchdog: ушли с MAX, возвращаюсь")
                    await page.goto(MAX_WEB_URL, wait_until="networkidle")
                    await _register_observer()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if shutting:
                break
            fails += 1
            log.warning("watchdog: браузер не отвечает (%s/3): %r", fails, e)
            if fails >= 3:
                try:
                    await bot.send_message(
                        chat_id=OWNER_ID,
                        text="⚠️ Браузер не отвечал — перезапускаю его. "
                             "Активный чат сброшен, открой заново (/chats).")
                except Exception:
                    pass
                await restart_browser()
                known.clear()
                force_first_pass.set()
                fails = 0


# --- TG-сторона -----------------------------------------------------------------


class AppState(StatesGroup):
    in_chat = State()


def main_keyboard():
    b = ReplyKeyboardBuilder()
    b.button(text="💬 Чаты")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def chat_keyboard():
    b = ReplyKeyboardBuilder()
    b.button(text="❌ Выйти из чата")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def is_owner(message: types.Message) -> bool:
    return bool(message.from_user and message.from_user.id == OWNER_ID)


async def fetch_tg_file(message: types.Message):
    """Скачать вложение TG на диск (для CDP-дропа). None = не вложение."""
    file_id = file_name = None
    is_media = False
    if message.photo:
        file_id, file_name, is_media = (message.photo[-1].file_id,
                                        "photo.jpg", True)
    elif message.video:
        file_id = message.video.file_id
        file_name = message.video.file_name or "video.mp4"
        is_media = True
    elif message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name or "document"
    elif message.voice:
        file_id, file_name = message.voice.file_id, "voice.ogg"
    if not file_id:
        return None
    tg_file = await bot.get_file(file_id)
    downloads = BASE / "downloads"
    downloads.mkdir(exist_ok=True)
    dest = downloads / f"{int(time.time())}_{file_name}"
    await bot.download_file(tg_file.file_path, destination=str(dest))
    return file_name, dest, is_media


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    if not is_owner(message):
        return
    await state.clear()
    n = len(chats_order) or len(known)
    await message.reply(
        f"✅ maxbot-lite ({VERSION}) активен.\n"
        f"Браузер: {'живой' if page else 'перезапускается'}, "
        f"чатов в кэше: {n}.\n\n💬 Чаты — выбрать чат и писать в MAX.\n"
        f"В чате: /reply <N> <текст>, /delete <N> (своё текстовое), "
        f"/screen, /status, /logs.",
        reply_markup=main_keyboard())


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    if not is_owner(message):
        return
    await message.reply(
        "💬 Чаты — список чатов MAX\n"
        "В чате: пиши текст / кидай файл — уйдёт в MAX\n"
        "/reply <N> <текст> — ответить на сообщение №N\n"
        "/delete <N> — удалить СВОЁ текстовое №N\n"
        "/screen — скрин браузера • /status — состояние • /logs — лог")


@dp.message(F.text == "💬 Чаты")
async def btn_chats(message: types.Message, state: FSMContext):
    if not is_owner(message):
        return
    await state.set_state(None)
    await show_chats_page(message, 0)


async def show_chats_page(msg_or_cb, pg: int):
    if not chats_order:
        text = "Список чатов пуст — поллер ещё сканирует интерфейс."
        if isinstance(msg_or_cb, types.Message):
            await msg_or_cb.reply(text)
        else:
            await msg_or_cb.message.answer(text)
        return
    per_page = 6
    start = pg * per_page
    chunk = chats_order[start:start + per_page]
    b = InlineKeyboardBuilder()
    for c in chunk:
        label = c["name"]
        if c.get("unread"):
            label = f"🔴 {label} ({c['unread']})"
        b.button(text=label, callback_data=f"open_chat:{c['id']}")
    nav = []
    if pg > 0:
        nav.append(types.InlineKeyboardButton(text="◀️",
                                              callback_data=f"page:{pg-1}"))
    if start + per_page < len(chats_order):
        nav.append(types.InlineKeyboardButton(text="▶️",
                                              callback_data=f"page:{pg+1}"))
    if nav:
        b.row(*nav)
    text = f"Чаты MAX (стр. {pg + 1}):"
    if isinstance(msg_or_cb, types.Message):
        await msg_or_cb.reply(text, reply_markup=b.as_markup())
    else:
        await msg_or_cb.message.edit_text(text, reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("page:"))
async def cb_page(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        return
    await show_chats_page(callback, int(callback.data.split(":")[1]))
    await callback.answer()


@dp.callback_query(F.data.startswith("open_chat:"))
async def cb_open_chat(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        return
    chat_id = callback.data.split(":", 1)[1]
    known_c = known.get(chat_id) or next(
        (c for c in chats_order if c["id"] == chat_id), None)
    await enter_chat(callback.message, state, chat_id,
                     (known_c or {}).get("name"))
    await callback.answer()


def _reply_buttons(tap: list):
    """Инлайн-кнопки «↩️ ответить #N» на последние сообщения снимка."""
    if not tap:
        return None
    b = InlineKeyboardBuilder()
    for didx, body in tap[-6:]:
        b.button(text=f"↩️ #{didx} {(body or '')[:18]}",
                 callback_data=f"rt:{didx}")
    b.adjust(1)
    return b.as_markup()


async def enter_chat(dst: types.Message, state: FSMContext,
                     chat_id: str, title: str | None) -> None:
    """Открыть чат MAX и показать снимок последних сообщений (#N) с кнопками
    «↩️ ответить». Общее для кнопки чата и команды /open."""
    global active_chat_id
    await dst.answer("Открываю чат...", reply_markup=chat_keyboard())
    await state.set_state(AppState.in_chat)
    await state.update_data(current_chat_id=chat_id, current_title=title,
                            reply_to=None)
    try:
        async with PAGE_LOCK.guard("открытие чата"):
            reason = await open_chat(page, chat_id, title)
            if reason:
                await dst.answer(f"❌ Чат не открылся ({reason}).")
                return
            active_chat_id = chat_id
            items = await parse_history(20)
        targets, lines, tap = {}, [], []
        for it in items:
            if it.get("type") == "date":
                lines.append(f"\n📅 {it.get('text', '')}\n")
                continue
            didx = str(it.get("data_index"))
            body = it.get("text") or it.get("filename") or \
                   {"voice": "🎤 Голосовое", "circle": "⭕ Кружок",
                    "media": f"📷 {it.get('text', '')}"}.get(it.get("type"), "…")
            targets[didx] = {"type": it.get("type"), "text": body}
            prefix = "➡️" if it.get("is_out") else "⬅️"
            ts = f"[{it.get('time', '')}] " if it.get("time") else ""
            who = f"{it.get('sender', '')}: " if it.get("sender") else ""
            lines.append(f"#{didx} {prefix} {ts}{who}{body}")
            tap.append((didx, body))
        snapshot = "\n".join(lines) or "История пуста/не читается."
        if len(snapshot) > 3800:
            snapshot = "…(обрезано)…\n" + snapshot[-3800:]
        await state.update_data(targets=targets)
        await dst.answer(
            f"💬 <b>Открыт: {title or chat_id}</b>\n"
            f"Пиши — уйдёт в MAX. Кидаешь файл — уйдёт файлом.\n\n"
            f"<pre>{snapshot}</pre>\n\n"
            f"Ответить: тапни кнопку ниже или /reply &lt;N&gt; &lt;текст&gt;; "
            f"удалить своё: /delete &lt;N&gt;. Ещё: /find &lt;слово&gt;, /who.",
            parse_mode=ParseMode.HTML, reply_markup=_reply_buttons(tap))
    except Exception as e:
        await dst.answer(f"❌ Ошибка открытия чата: {e!r}")


@dp.callback_query(F.data.startswith("rt:"))
async def cb_reply_tap(callback: types.CallbackQuery, state: FSMContext):
    """Тап «↩️ ответить #N»: запоминаем цель, следующий текст станет ответом."""
    if callback.from_user.id != OWNER_ID:
        return
    didx = callback.data.split(":", 1)[1]
    await state.update_data(reply_to=didx)
    data = await state.get_data()
    tgt = (data.get("targets") or {}).get(didx, {})
    preview = (tgt.get("text") or "")[:40]
    await callback.message.answer(
        f"↩️ Отвечаю на #{didx} «{preview}». Пришли текст ответа "
        f"(или /cancel).")
    await callback.answer()


@dp.message(Command("cancel"), AppState.in_chat)
async def cmd_cancel(message: types.Message, state: FSMContext):
    if not is_owner(message):
        return
    await state.update_data(reply_to=None)
    await message.reply("Ок, ответ отменён — дальше текст уходит обычным "
                        "сообщением.")


@dp.message(Command("chats"))
async def cmd_chats(message: types.Message, state: FSMContext):
    if not is_owner(message):
        return
    await state.clear()
    await show_chats_page(message, 0)


# ⚠️ ПОРЯДОК РЕГИСТРАЦИИ: выход из чата и /reply /delete — ДО catch-all
# «пиши текст». Иначе catch-all глотает текст кнопки и уезжает им в MAX
# (бой 07.09: «Вы: ❌ Выйти из чата» уехало собеседнику).


@dp.message(Command("reply"), AppState.in_chat)
async def cmd_reply(message: types.Message, state: FSMContext):
    if not is_owner(message):
        return
    args = message.text.split(None, 2)
    data = await state.get_data()
    chat_id = data.get("current_chat_id")
    if len(args) < 3 or not args[1].isdigit() or not chat_id:
        await message.reply("Использование: /reply <N> <текст> "
                            "(номер — из снимка при открытии чата)")
        return
    status = await reply_to_item(page, int(args[1]), args[2])
    if status == "ok":
        remember_sent(chat_id, args[2])
        await message.react([types.ReactionTypeEmoji(emoji="👍")])
    else:
        await message.reply(f"❌ Ответ не отправлен ({status}).")


@dp.message(Command("delete"), AppState.in_chat)
async def cmd_delete(message: types.Message, state: FSMContext):
    if not is_owner(message):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply("Использование: /delete <N>")
        return
    status = await delete_item(page, int(args[1]), only_own=True)
    if status == "ok":
        await message.reply("✅ Удалено (подтверждено по истории).")
    elif status == "still_present":
        await message.reply("⚠️ Выполнил, но сообщение всё ещё видно — "
                            "проверь в MAX и повтори.")
    elif status == "not_own":
        await message.reply("❌ Это не твоё сообщение — удалить нельзя.")
    elif status in ("not_found", "hover_fail"):
        await message.reply("❌ Сообщение не найдено в DOM "
                            "(перерисовался список — открой чат заново).")
    else:
        await message.reply(f"❌ Не удалено ({status}).")


@dp.message(Command("find"), AppState.in_chat)
async def cmd_find(message: types.Message, state: FSMContext):
    """/find <слово> — поиск по ВИДИМОЙ истории открытого чата (без БД):
    что отрисовано в DOM сейчас, то и ищем."""
    if not is_owner(message):
        return
    args = (message.text or "").split(None, 1)
    if len(args) < 2 or not args[1].strip():
        await message.reply("Использование: /find <слово>")
        return
    q = args[1].strip().lower()
    try:
        items = await parse_history(60)
    except Exception as e:
        await message.reply(f"❌ Не прочитал историю: {e!r}")
        return
    hits = []
    for it in items:
        body = (it.get("text") or it.get("filename") or "")
        if q in body.lower():
            didx = str(it.get("data_index"))
            ts = f"[{it.get('time', '')}] " if it.get("time") else ""
            hits.append(f"#{didx} {ts}{body[:120]}")
    if not hits:
        await message.reply(f"По «{args[1].strip()}» в видимой истории ничего. "
                            "Промотай чат в MAX и повтори — ищу только то, "
                            "что сейчас в DOM.")
        return
    out = "\n".join(hits[-25:])
    await message.reply(f"🔎 Найдено {len(hits)}:\n<pre>{out}</pre>",
                        parse_mode=ParseMode.HTML)


@dp.message(Command("who"), AppState.in_chat)
async def cmd_who(message: types.Message, state: FSMContext):
    if not is_owner(message):
        return
    data = await state.get_data()
    title = data.get("current_title") or data.get("current_chat_id") or "?"
    await message.reply(f"💬 Открыт чат: <b>{title}</b>", parse_mode=ParseMode.HTML)


@dp.message(Command("open"))
async def cmd_open(message: types.Message, state: FSMContext):
    """/open <имя> — открыть чат по названию (совпадение без регистра)."""
    if not is_owner(message):
        return
    args = (message.text or "").split(None, 1)
    if len(args) < 2 or not args[1].strip():
        await message.reply("Использование: /open <часть имени чата>")
        return
    q = args[1].strip().lower()
    pool = list(known.values()) or chats_order
    exact = [c for c in pool if (c.get("name") or "").lower() == q]
    part = [c for c in pool if q in (c.get("name") or "").lower()]
    cand = exact or part
    if not cand:
        await message.reply(f"Чат «{args[1].strip()}» не найден в кэше. "
                            "Открой 💬 Чаты — список подтянется.")
        return
    if len(cand) > 1 and not exact:
        names = ", ".join(sorted({c["name"] for c in cand})[:8])
        await message.reply(f"Под «{args[1].strip()}» подходит несколько: "
                            f"{names}. Уточни.")
        return
    c = cand[0]
    await state.clear()
    await enter_chat(message, state, c["id"], c.get("name"))


@dp.message(Command("screen"))
async def cmd_screen(message: types.Message):
    if not is_owner(message):
        return
    if page is None:
        await message.reply("❌ Браузер перезапускается, попробуй позже.")
        return
    shot = BASE / "bot_screen.png"
    try:
        await page.screenshot(path=str(shot))
        await bot.send_photo(chat_id=message.chat.id,
                             photo=types.FSInputFile(shot),
                             caption="Экран браузера MAX")
    except Exception as e:
        await message.reply(f"❌ Скриншот не удался: {e!r}")


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if not is_owner(message):
        return
    browser_ok = False
    url = chat_name = ""
    if page is not None:
        try:
            await asyncio.wait_for(
                page.evaluate("() => document.title"), timeout=5)
            browser_ok = True
            url = page.url or ""
            sel = await ev("""() => {
                const s = document.querySelector('button[class*="cell--selected"]');
                if (!s) return '';
                const lines = (s.innerText || '').split('\\n');
                return lines.length ? lines[0].trim() : '';
            }""")
            chat_name = sel
        except Exception as e:
            url = f"не отвечает: {e!r}"
    up = int(time.time() - started_at)
    await message.reply(
        f"📊 <b>maxbot-lite</b> ({VERSION})\n"
        f"Браузер: {'✅ живой' if browser_ok else '❌ не отвечает'}\n"
        f"Страница: {url[:60] or '—'}\n"
        f"Открытый чат MAX: {chat_name or '—'}\n"
        f"Чатов в кэше: {len(known)}\n"
        f"Аптайм: {up // 3600}ч {(up % 3600) // 60}м",
        parse_mode=ParseMode.HTML)


@dp.message(Command("logs"))
async def cmd_logs(message: types.Message):
    if not is_owner(message):
        return
    try:
        data = LOG_FILE.read_bytes()
    except OSError as e:
        await message.reply(f"❌ Лог не читается: {e!r}")
        return
    note = ""
    if len(data) > 4 * 1024 * 1024:
        data = data[-4 * 1024 * 1024:]
        note = "\n⚠️ Файл большой — отдал последние 4 МБ"
    await bot.send_document(
        chat_id=message.chat.id,
        document=types.BufferedInputFile(data, filename=LOG_FILE.name),
        caption=f"📄 Лог, {len(data) // 1024} КБ{note}")


# ⚠️ ПОРЯДОК РЕГИСТРАЦИИ (бой 07.09): выход из чата — ДО catch-all «пиши
# текст» (ниже), иначе текст кнопки уезжает собеседнику как сообщение.
# Команды (/reply, /delete) не страдают: catch-all отсекает строки с «/».


@dp.message(F.text == "❌ Выйти из чата")
async def leave_chat(message: types.Message, state: FSMContext):
    if not is_owner(message):
        return
    try:
        if page:
            back = await page.query_selector(
                'button.backBtn[aria-label="Go back"]')
            if back:
                await back.click()
    except Exception as e:
        log.warning("выход из чата: %r", e)
    global active_chat_id
    active_chat_id = None
    await state.clear()
    await message.reply("Вышел в главное меню.", reply_markup=main_keyboard())


@dp.message(AppState.in_chat, F.photo | F.video | F.document | F.voice)
async def send_file_to_max(message: types.Message, state: FSMContext):
    if not is_owner(message):
        return
    data = await state.get_data()
    chat_id, title = data.get("current_chat_id"), data.get("current_title")
    if not chat_id:
        await message.reply("❌ Чат не выбран — 💬 Чаты.")
        return
    fetched = await fetch_tg_file(message)
    if not fetched:
        return
    file_name, dest, is_media = fetched
    kind = "media" if (message.photo or message.video) else "file"
    status = await upload_file(page, chat_id, title, dest, kind)
    try:
        dest.unlink()
    except OSError:
        pass
    if status == "ok":
        # Подпись к файлу (25.09): MAX отправляет файл сразу при дропе и текст
        # композера игнорирует, поэтому подпись досылаем отдельным сообщением.
        cap = (message.caption or "").strip()
        if cap:
            cst = await send_text(page, chat_id, cap, title)
            if cst == "ok":
                remember_sent(chat_id, cap)
            else:
                await message.reply(f"⚠️ Файл ушёл, подпись — нет ({cst}).")
        await message.react([types.ReactionTypeEmoji(emoji="👍")])
    else:
        await message.reply(f"❌ Файл не отправлен ({status}). "
                            f"Проверь /screen и попробуй ещё раз.")


@dp.message(AppState.in_chat, ~F.text.startswith("/"))
async def send_text_handler(message: types.Message, state: FSMContext):
    if not is_owner(message):
        return
    data = await state.get_data()
    chat_id, title = data.get("current_chat_id"), data.get("current_title")
    if not chat_id:
        await message.reply("❌ Чат не выбран — 💬 Чаты.")
        return
    # Реплай тапом: если раньше нажали «↩️ #N», этот текст уходит ответом
    # на то сообщение, потом цель сбрасывается.
    reply_to = data.get("reply_to")
    if reply_to:
        await state.update_data(reply_to=None)
        status = await reply_to_item(page, int(reply_to), message.text)
        if status == "ok":
            remember_sent(chat_id, message.text)
            await message.react([types.ReactionTypeEmoji(emoji="👍")])
        else:
            await message.reply(f"❌ Ответ не отправлен ({status}).")
        return
    status = await send_text(page, chat_id, message.text, title)
    if status == "ok":
        remember_sent(chat_id, message.text)
        await message.react([types.ReactionTypeEmoji(emoji="👍")])
    else:
        await message.reply(f"❌ Текст не отправлен ({status}). "
                            f"Проверь /screen и попробуй ещё раз.")


# --- Аддоны: маленький загрузчик addons/ + контракт register(ctx) -------------
# Идея (решение владельца 25.09): ядро остаётся ОДНИМ файлом, а необязательные
# фичи живут по одному .py в папке `addons/`. Есть файл — фича есть; убрал файл
# — нет. Каждый аддон экспортит `register(ctx)`; `ctx` — единственная точка,
# через которую аддон трогает бота (без импортов из ядра, чтобы связь была
# явной и узкой). Так человек получает понятную ЗАЦЕПКУ для своего кода.
#
# ⚠️ Аддоны — про ЛОГИКУ НА СТОРОНЕ БОТА (сводки, фильтры, расписания). Всё,
# что лезет в протокол MAX (сокет, перехват медиа, реакции op=155/178), в лайт
# НЕ кладём осознанно — это отдельный приватный проект.

_addon_msg_hooks: list = []      # колбэки (chat: dict, why: str) на новое сообщение
_addon_tasks: list = []          # фоновые задачи аддонов — гасятся на выходе


async def _fire_msg_hooks(c: dict, why: str) -> None:
    for cb in _addon_msg_hooks:
        try:
            res = cb(c, why)
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:
            log.warning("аддон-хук на сообщение упал: %r", e)


class AddonCtx:
    """Единственная точка, через которую аддон общается с ядром лайта.

    Всё, что нужно типичному аддону, — здесь; ядро импортировать не надо.
    Пример аддона — `addons/addon_digest.py`."""

    def __init__(self, name: str):
        self.name = name
        self.log = logging.getLogger(f"lite.addon.{name}")
        self.bot = bot
        self.dp = dp
        self.owner_id = OWNER_ID
        self.known = known           # кэш чатов {id: {name,last_message,unread,...}}

    def env(self, key: str, default=None):
        """Значение из .env / окружения (строкой)."""
        return os.environ.get(key, default)

    def page(self):
        """Текущая страница Playwright или None. Функция, а НЕ значение:
        `page` пересоздаётся при рестарте браузера."""
        return page

    async def send_owner(self, text: str, **kw):
        """Сообщение владельцу в личку (HTML по умолчанию)."""
        kw.setdefault("parse_mode", ParseMode.HTML)
        return await self.bot.send_message(chat_id=OWNER_ID, text=text, **kw)

    def on_message(self, cb):
        """Подписаться на КАЖДОЕ обнаруженное новое сообщение: cb(chat, why).
        cb может быть sync или async. chat — та же ячейка, что видит поллер."""
        _addon_msg_hooks.append(cb)

    def spawn(self, coro, name: str = "addon"):
        """Фоновая задача аддона под присмотром: гасится при остановке бота."""
        t = asyncio.create_task(coro, name=name)
        _addon_tasks.append(t)
        return t

    # Действия в MAX (для аддонов, которым это нужно; digest — не трогает).
    async def send_text(self, chat_id: str, text: str, title: str | None = None):
        return await send_text(page, chat_id, text, title)

    async def open_chat(self, chat_id: str, title: str | None = None):
        return await open_chat(page, chat_id, title)


def load_addons() -> None:
    """Найти и подключить аддоны из `addons/`. Падение одного аддона не мешает
    остальным и не роняет бота."""
    import importlib.util
    addons_dir = BASE / "addons"
    if not addons_dir.is_dir():
        return
    files = sorted(p for p in addons_dir.glob("*.py")
                   if not p.name.startswith("_"))
    for path in files:
        name = path.stem
        try:
            spec = importlib.util.spec_from_file_location(f"addon_{name}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            reg = getattr(module, "register", None)
            if not callable(reg):
                log.warning("аддон %s: нет функции register(ctx) — пропускаю",
                            name)
                continue
            reg(AddonCtx(name))
            log.info("аддон подключён: %s", name)
        except Exception as e:
            log.warning("аддон %s не подключился: %r", name, e)


# --- main -----------------------------------------------------------------------

poller_task = None
watchdog_task = None


async def main():
    global poller_task, watchdog_task, shutting
    await bot.set_my_commands([
        types.BotCommand(command="chats", description="Список чатов MAX"),
        types.BotCommand(command="open", description="Открыть чат по имени: /open <имя>"),
        types.BotCommand(command="reply", description="Ответить: /reply <N> <текст>"),
        types.BotCommand(command="delete", description="Удалить своё: /delete <N>"),
        types.BotCommand(command="find", description="Поиск в видимой истории: /find <слово>"),
        types.BotCommand(command="who", description="Какой чат открыт"),
        types.BotCommand(command="screen", description="Скрин браузера"),
        types.BotCommand(command="status", description="Состояние моста"),
        types.BotCommand(command="logs", description="Скачать лог"),
    ])
    await start_browser()
    poller_task = asyncio.create_task(poller_loop())
    watchdog_task = asyncio.create_task(watchdog_loop())
    # Аддоны подключаем ПОСЛЕ старта браузера (страница уже есть) и ДО polling.
    load_addons()

    async def _graceful():
        # Хук aiogram выполняется ДО закрытия сессии бота — гасим фоновые
        # петли ЗДЕСЬ, иначе они ещё ~9 с долбят умирающий браузер и
        # засыпают лог ошибками (бой 07.09).
        global shutting
        shutting = True
        for t in (poller_task, watchdog_task, *_addon_tasks):
            t.cancel()

    async def _say_stopped():
        try:
            await bot.send_message(chat_id=OWNER_ID, text="⛔ Бот остановлен.")
        except Exception:
            pass

    dp.shutdown.register(_graceful)
    dp.shutdown.register(_say_stopped)
    await asyncio.sleep(6)
    try:
        await bot.send_message(
            chat_id=OWNER_ID,
            text=f"✅ maxbot-lite ({VERSION}) запущен.\n"
                 f"Браузер: живой, чатов в кэше: {len(known)}.\n"
                 f"💬 Чаты — начать. /help — краткая справка.",
            reply_markup=main_keyboard())
    except Exception as e:
        log.warning("стартовое уведомление не ушло: %r", e)

    try:
        await dp.start_polling(bot)
    finally:
        shutting = True
        for t in (poller_task, watchdog_task, *_addon_tasks):
            t.cancel()
        await asyncio.gather(poller_task, watchdog_task, *_addon_tasks,
                             return_exceptions=True)
        await shutdown_browser()


async def shutdown_browser():
    global browser_ctx, page
    try:
        if browser_ctx:
            await browser_ctx.close()
    except Exception as e:
        log.warning("закрытие браузера: %r", e)
    browser_ctx = None
    page = None


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nОстановлено.")
