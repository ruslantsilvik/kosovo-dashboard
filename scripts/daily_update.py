#!/usr/bin/env python3
"""
Щоденне автооновлення дашборда "Косово та Сербія".

Запускається з GitHub Actions. Логіка:
  1. Читає index.html, витягає JSON-блок <script id="dashboard-data">.
  2. Питає Claude (Anthropic API, з увімкненим веб-пошуком) про нові
     датовані події за останні дні по осі Косово / Сербія / Україна / Албанія.
  3. Жорстко валідує відповідь (дата, тональність, країна, джерела),
     відсіює дублікати проти вже наявних записів.
  4. Додає нові записи у belgrade_visit_events, оновлює дати "Останнє оновлення".
  5. Записує файл назад. Коміт/пуш робить сам workflow.

Нічого не вигадує: запис без реального URL-джерела відкидається.
"""

import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone

try:
    import anthropic
except ImportError:
    sys.exit("Не встановлено пакет anthropic. У workflow має бути: pip install anthropic")

HTML_PATH = os.environ.get("DASHBOARD_HTML", "index.html")
DATA_RE = re.compile(
    r'(<script id="dashboard-data" type="application/json">)(.*?)(</script>)', re.S
)

# Моделі пробуються по черзі — якщо ID застаріє, скрипт не впаде.
MODEL_CANDIDATES = [
    m.strip()
    for m in os.environ.get(
        "ANTHROPIC_MODELS",
        "claude-sonnet-4-5,claude-sonnet-4-20250514,claude-3-7-sonnet-latest",
    ).split(",")
    if m.strip()
]

ALLOWED_COUNTRIES = {"kosovo", "serbia", "ukraine", "albania"}
ALLOWED_TONES = {"support", "friction", "neutral"}
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "4"))
MAX_NEW_ITEMS = int(os.environ.get("MAX_NEW_ITEMS", "6"))

UA_MONTHS = {
    1: "січня", 2: "лютого", 3: "березня", 4: "квітня", 5: "травня", 6: "червня",
    7: "липня", 8: "серпня", 9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня",
}


# --------------------------------------------------------------------------- #
# HTML / JSON I/O
# --------------------------------------------------------------------------- #

def load_html():
    with open(HTML_PATH, encoding="utf-8") as fh:
        return fh.read()


def extract_data(html):
    m = DATA_RE.search(html)
    if not m:
        sys.exit("Не знайдено блок <script id=\"dashboard-data\"> у index.html")
    return json.loads(m.group(2)), m


def write_html(html, data, match):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if "</script>" in payload:
        sys.exit("JSON містить </script> — небезпечно вставляти, зупиняюсь")
    new_html = html[: match.start()] + match.group(1) + payload + match.group(3) + html[match.end():]
    with open(HTML_PATH, "w", encoding="utf-8") as fh:
        fh.write(new_html)
    return new_html


# --------------------------------------------------------------------------- #
# Нормалізація / дедуплікація
# --------------------------------------------------------------------------- #

def norm_title(text):
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip()


def existing_keys(events):
    keys = set()
    for e in events:
        keys.add(norm_title(e.get("title")))
    return keys


# --------------------------------------------------------------------------- #
# Запит до Claude
# --------------------------------------------------------------------------- #

def build_prompt(data, today):
    events = sorted(data.get("belgrade_visit_events", []), key=lambda e: e.get("date", ""))
    recent = events[-14:]
    known = "\n".join(f"- {e.get('date')} [{e.get('country')}] {e.get('title')}" for e in recent)
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()

    return f"""Сьогодні {today.isoformat()}. Ти — дослідник-аналітик для представництва Торгово-промислової палати України в Косові.

Знайди через веб-пошук РЕАЛЬНІ, ДАТОВАНІ новини та події за період з {since} по {today.isoformat()} за темами:
- українсько-косовські та українсько-сербські відносини;
- візити, заяви, рішення офіційних осіб Косова, Сербії, України, Албанії щодо цієї осі;
- торговельні та дипломатичні події між цими країнами;
- призначення / зміни послів у Косові та Сербії;
- ділові заходи (бізнес-форуми, конференції, торговельні місії) у Приштині та Белграді.

Шукай українською, англійською, сербською та албанською.

Ці записи ВЖЕ Є в дашборді — не повторюй їх і не переказуй іншими словами:
{known or "(порожньо)"}

КРИТИЧНІ ПРАВИЛА:
1. Нічого не вигадуй. Кожен запис мусить спиратись на конкретну веб-сторінку, яку ти реально знайшов, з робочим URL.
2. Якщо за цей період не знайшлося нічого суттєво нового — поверни порожній масив. Порожній результат це нормально і краще, ніж притягнутий за вуха.
3. Не додавай загальних оглядів війни в Україні без прямого стосунку до Косова/Сербії/Балкан.
4. date — реальна дата події або публікації, формат YYYY-MM-DD, не в майбутньому.
5. Максимум {MAX_NEW_ITEMS} записів.

Формат відповіді — ЛИШЕ JSON усередині тегів <result></result>, без пояснень поза тегами:

<result>
{{"news": [
  {{
    "date": "YYYY-MM-DD",
    "country": "kosovo|serbia|ukraine|albania",
    "tone": "support|friction|neutral",
    "title": "Стислий заголовок українською (до 200 символів)",
    "text": "1-2 речення суті українською",
    "sources": [["Назва видання", "https://..."]]
  }}
]}}
</result>"""


def call_claude(prompt):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    last_error = None

    for model in MODEL_CANDIDATES:
        try:
            print(f"[api] пробую модель: {model}")
            resp = client.messages.create(
                model=model,
                max_tokens=4000,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            print(f"[api] модель {model} відповіла, {len(text)} символів тексту")
            return text
        except Exception as exc:  # noqa: BLE001 — свідомо ловимо будь-що і пробуємо далі
            last_error = exc
            print(f"[api] модель {model} не спрацювала: {exc}")

    raise RuntimeError(f"Жодна модель не спрацювала. Остання помилка: {last_error}")


def parse_response(text):
    m = re.search(r"<result>(.*?)</result>", text, re.S)
    blob = m.group(1) if m else text
    blob = re.sub(r"^\s*```(?:json)?|```\s*$", "", blob.strip(), flags=re.M).strip()
    start, end = blob.find("{"), blob.rfind("}")
    if start == -1 or end == -1:
        print("[parse] JSON у відповіді не знайдено — вважаю, що нових подій нема")
        return []
    try:
        parsed = json.loads(blob[start : end + 1])
    except json.JSONDecodeError as exc:
        print(f"[parse] не вдалося розібрати JSON ({exc}) — пропускаю додавання")
        return []
    items = parsed.get("news", [])
    return items if isinstance(items, list) else []


# --------------------------------------------------------------------------- #
# Валідація
# --------------------------------------------------------------------------- #

def validate(items, data, today):
    seen = existing_keys(data.get("belgrade_visit_events", []))
    oldest_ok = today - timedelta(days=LOOKBACK_DAYS + 10)
    good = []

    for raw in items:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        d = str(raw.get("date") or "").strip()
        country = str(raw.get("country") or "").strip().lower()
        tone = str(raw.get("tone") or "neutral").strip().lower()

        if not title or len(title) > 300:
            print(f"[skip] порожній або задовгий заголовок: {title[:60]!r}")
            continue
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            print(f"[skip] некоректна дата {d!r} у {title[:50]!r}")
            continue
        try:
            parsed_date = date.fromisoformat(d)
        except ValueError:
            print(f"[skip] нерозбірлива дата {d!r}")
            continue
        if parsed_date > today:
            print(f"[skip] дата в майбутньому {d} у {title[:50]!r}")
            continue
        if parsed_date < oldest_ok:
            print(f"[skip] застара подія {d} у {title[:50]!r}")
            continue
        if country not in ALLOWED_COUNTRIES:
            print(f"[skip] невідома країна {country!r} у {title[:50]!r}")
            continue
        if tone not in ALLOWED_TONES:
            tone = "neutral"

        sources = []
        for s in raw.get("sources") or []:
            if isinstance(s, (list, tuple)) and len(s) >= 2:
                label, url = str(s[0]).strip(), str(s[1]).strip()
                if url.startswith(("http://", "https://")):
                    sources.append([label or url, url])
        if not sources:
            print(f"[skip] немає робочого джерела у {title[:50]!r}")
            continue

        key = norm_title(title)
        if key in seen:
            print(f"[skip] дублікат: {title[:60]!r}")
            continue
        seen.add(key)

        good.append({
            "date": d,
            "country": country,
            "tone": tone,
            "title": title,
            "text": str(raw.get("text") or "").strip(),
            "sources": sources,
        })

    return good[:MAX_NEW_ITEMS]


# --------------------------------------------------------------------------- #
# Оновлення приміток про дату
# --------------------------------------------------------------------------- #

def set_note(html, marker, text):
    pattern = re.compile(
        r'(<p class="updated-note" data-auto="' + re.escape(marker) + r'">)(.*?)(</p>)', re.S
    )
    if not pattern.search(html):
        print(f"[note] маркер {marker} не знайдено — пропускаю")
        return html
    return pattern.sub(lambda m: m.group(1) + text + m.group(3), html, count=1)


def update_notes(html, today, added):
    d = today.strftime("%d.%m.%Y")
    if added:
        news = (f"Останнє оновлення: {d} (автоматична щоденна перевірка). "
                f"Цього разу додано нових записів: {added}. Кожен запис має джерело — перевіряйте за посиланням.")
    else:
        news = (f"Останнє оновлення: {d} (автоматична щоденна перевірка). "
                f"Нових суттєвих датованих подій за останні дні не знайдено — записів не додано, "
                f"навмисно без «порожніх» карток.")
    events = (f"Перевірка — щоденно в автоматичному режимі. Останній прогін: {d}. "
              f"Перелік заходів оновлюється рідше за новини — перевіряйте дати на сайтах організаторів.")

    html = set_note(html, "news-kosovo", news)
    html = set_note(html, "news-serbia", news)
    html = set_note(html, "events-kosovo", events)
    html = set_note(html, "events-serbia", events)
    return html


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    today = datetime.now(timezone.utc).date()
    html = load_html()
    data, match = extract_data(html)

    before = len(data.get("belgrade_visit_events", []))
    print(f"[start] {today} — у дашборді зараз {before} новинних записів")

    added_items = []
    if os.environ.get("SKIP_SEARCH") == "1":
        print("[start] SKIP_SEARCH=1 — пошук пропущено, лише оновлюю дати")
    else:
        try:
            raw_text = call_claude(build_prompt(data, today))
            added_items = validate(parse_response(raw_text), data, today)
        except Exception as exc:  # noqa: BLE001
            print(f"[error] пошук не вдався: {exc}")
            print("[error] продовжую без нових записів — дати все одно оновлю")

    if added_items:
        data.setdefault("belgrade_visit_events", []).extend(added_items)
        data["belgrade_visit_events"].sort(key=lambda e: e.get("date", ""), reverse=True)
        for it in added_items:
            print(f"[add] {it['date']} [{it['country']}/{it['tone']}] {it['title'][:90]}")
    else:
        print("[add] нових записів немає")

    data["generated"] = today.strftime("%d.%m.%Y")
    data["last_auto_check"] = today.isoformat()

    html = write_html(html, data, match)
    html = update_notes(html, today, len(added_items))
    with open(HTML_PATH, "w", encoding="utf-8") as fh:
        fh.write(html)

    # Перевірка, що файл лишився валідним
    check_data, _ = extract_data(load_html())
    after = len(check_data.get("belgrade_visit_events", []))
    print(f"[done] новинних записів: було {before}, стало {after}")

    summary = (f"Оновлення {today.strftime('%d.%m')}: додано {len(added_items)} новин"
               if added_items else
               f"Оновлення {today.strftime('%d.%m')}: нових подій не знайдено, оновлено дати перевірки")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"summary={summary}\n")
            fh.write(f"added={len(added_items)}\n")
    print(f"[summary] {summary}")


if __name__ == "__main__":
    main()
