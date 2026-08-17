#!/usr/bin/env python3
"""
Автооновлення дашборда "Косово та Сербія" (v8).

Запускається з GitHub Actions раз на три дні. Оновлює три розділи:
  1. belgrade_visit_events — новини по осі Косово/Сербія/Україна/Албанія
  2. russia_reaction.items — реакція російських медіа та офіційних осіб
  3. serbia_crime.items    — злочинність, правоохоронні органи, резонансні події в Сербії

Кожен розділ шукається окремим запитом до Anthropic API з увімкненим веб-пошуком.
Збій одного розділу не зупиняє інші. Нічого не вигадується: запис без реального
URL-джерела відкидається, дублікати відсіюються за нормалізованим заголовком.
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

MODEL_CANDIDATES = [
    m.strip()
    for m in os.environ.get(
        "ANTHROPIC_MODELS",
        "claude-sonnet-4-5,claude-sonnet-4-20250514,claude-3-7-sonnet-latest",
    ).split(",")
    if m.strip()
]

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "5"))
MAX_NEW_ITEMS = int(os.environ.get("MAX_NEW_ITEMS", "6"))
MAX_SEARCH_USES = int(os.environ.get("MAX_SEARCH_USES", "5"))

ALLOWED_COUNTRIES = {"kosovo", "serbia", "ukraine", "albania"}
ALLOWED_TONES = {"support", "friction", "neutral"}
ALLOWED_RU_TONES = {"hostile", "negative", "neutral"}
ALLOWED_RU_TYPES = {
    "russian_state_media", "russian_official", "russian_other", "third_party_reporting",
}
ALLOWED_CRIME_CATS = {
    "organized_crime", "corruption", "economic_crime",
    "violent_crime", "public_order", "institutions",
}
ALLOWED_SEVERITY = {"high", "medium", "low"}


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #

def load_html():
    with open(HTML_PATH, encoding="utf-8") as fh:
        return fh.read()


def extract_data(html):
    m = DATA_RE.search(html)
    if not m:
        sys.exit('Не знайдено блок <script id="dashboard-data"> у index.html')
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
# Утиліти
# --------------------------------------------------------------------------- #

def norm_title(text):
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip()


def valid_date(value, today, extra_slack=10):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value or "")):
        return None
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return None
    if d > today:
        return None
    if d < today - timedelta(days=LOOKBACK_DAYS + extra_slack):
        return None
    return value


def clean_sources(raw):
    out = []
    for s in raw or []:
        if isinstance(s, (list, tuple)) and len(s) >= 2:
            label, url = str(s[0]).strip(), str(s[1]).strip()
            if url.startswith(("http://", "https://")):
                out.append([label or url, url])
    return out


def call_claude(prompt, label):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    last_error = None
    for model in MODEL_CANDIDATES:
        try:
            print(f"[api:{label}] модель {model}")
            resp = client.messages.create(
                model=model,
                max_tokens=4000,
                tools=[{"type": "web_search_20250305", "name": "web_search",
                        "max_uses": MAX_SEARCH_USES}],
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            print(f"[api:{label}] відповідь {len(text)} символів")
            return text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(f"[api:{label}] модель {model} не спрацювала: {exc}")
    raise RuntimeError(f"Жодна модель не спрацювала. Остання помилка: {last_error}")


def parse_json_block(text, key):
    m = re.search(r"<result>(.*?)</result>", text, re.S)
    blob = m.group(1) if m else text
    blob = re.sub(r"^\s*```(?:json)?|```\s*$", "", blob.strip(), flags=re.M).strip()
    start, end = blob.find("{"), blob.rfind("}")
    if start == -1 or end == -1:
        print(f"[parse:{key}] JSON не знайдено — вважаю, що нового нема")
        return []
    try:
        parsed = json.loads(blob[start:end + 1])
    except json.JSONDecodeError as exc:
        print(f"[parse:{key}] не вдалося розібрати JSON ({exc})")
        return []
    items = parsed.get(key, [])
    return items if isinstance(items, list) else []


# --------------------------------------------------------------------------- #
# Розділ 1: новини
# --------------------------------------------------------------------------- #

def prompt_news(data, today):
    events = sorted(data.get("belgrade_visit_events", []), key=lambda e: e.get("date", ""))
    known = "\n".join(f"- {e.get('date')} [{e.get('country')}] {e.get('title')}" for e in events[-14:])
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    return f"""Сьогодні {today.isoformat()}. Знайди через веб-пошук РЕАЛЬНІ ДАТОВАНІ новини за період з {since} по {today.isoformat()} за темами: українсько-косовські та українсько-сербські відносини; візити й заяви офіційних осіб Косова, Сербії, України, Албанії щодо цієї осі; торговельні та дипломатичні події; призначення послів у Косові та Сербії; ділові заходи в Приштині та Белграді.

Шукай українською, англійською, сербською, албанською.

Уже є в дашборді, не повторюй:
{known or "(порожньо)"}

ПРАВИЛА: нічого не вигадуй; кожен запис — з робочим URL реальної сторінки; якщо нічого суттєвого нема, поверни порожній масив (це нормально); date — реальна дата, YYYY-MM-DD, не в майбутньому; максимум {MAX_NEW_ITEMS} записів.

Відповідь — ЛИШЕ JSON у тегах <result></result>:
<result>
{{"news": [{{"date":"YYYY-MM-DD","country":"kosovo|serbia|ukraine|albania","tone":"support|friction|neutral","title":"заголовок українською до 200 символів","text":"1-2 речення суті українською","sources":[["Видання","https://..."]]}}]}}
</result>"""


def validate_news(items, data, today):
    seen = {norm_title(e.get("title")) for e in data.get("belgrade_visit_events", [])}
    good = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        d = valid_date(raw.get("date"), today)
        country = str(raw.get("country") or "").lower().strip()
        tone = str(raw.get("tone") or "neutral").lower().strip()
        sources = clean_sources(raw.get("sources"))
        key = norm_title(title)
        if not title or len(title) > 300 or not d or country not in ALLOWED_COUNTRIES \
                or not sources or key in seen:
            print(f"[skip:news] {title[:60]!r}")
            continue
        seen.add(key)
        good.append({"date": d, "country": country,
                     "tone": tone if tone in ALLOWED_TONES else "neutral",
                     "title": title, "text": str(raw.get("text") or "").strip(),
                     "sources": sources})
    return good[:MAX_NEW_ITEMS]


# --------------------------------------------------------------------------- #
# Розділ 2: реакція РФ
# --------------------------------------------------------------------------- #

def prompt_russia(data, today):
    block = data.get("russia_reaction") or {}
    items = sorted(block.get("items", []), key=lambda e: e.get("date", ""))
    known = "\n".join(f"- {e.get('date')} {e.get('outlet')}: {e.get('title')}" for e in items[-12:])
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    return f"""Сьогодні {today.isoformat()}. Ти ведеш медіа-моніторинг для представництва Торгово-промислової палати України. Знайди через веб-пошук РЕАЛЬНІ ДАТОВАНІ публікації російських медіа та заяви російських офіційних осіб за період з {since} по {today.isoformat()} щодо: зближення Сербії та України, візиту Зеленського до Белграда та його наслідків, постачання сербських боєприпасів Україні, торговельних переговорів Києва і Белграда.

Джерела для перевірки: РИА Новости, ТАСС, Коммерсантъ, RT, ВЗГЛЯД, Известия, Российская газета, Lenta, Газета.ру, Sputnik Serbia; заяви МЗС РФ (Захарова, Лавров), посольства РФ у Белграді, депутатів Держдуми. Також приймаються матеріали сербських і західних медіа ПРО російську реакцію (Danas, N1, Balkan Insight, RFE/RL) — познач їх типом third_party_reporting.

Уже є в дашборді, не повторюй:
{known or "(порожньо)"}

ПРАВИЛА: нічого не вигадуй; кожен запис — з робочим URL; цитата (quote) необов'язкова, МАКСИМУМ 15 слів мовою оригіналу, тільки якщо справді показова, інакше null; ніколи не вигадуй цитат; якщо нічого нового нема — порожній масив; максимум {MAX_NEW_ITEMS} записів.

tone — твоя класифікація ворожості до України: "hostile" (різко ворожа риторика, звинувачення), "negative" (критично, але стримано), "neutral" (фактичний виклад без оцінок).

Відповідь — ЛИШЕ JSON у тегах <result></result>:
<result>
{{"russia": [{{"date":"YYYY-MM-DD","outlet":"назва видання або ім'я і посада особи","outlet_type":"russian_state_media|russian_official|russian_other|third_party_reporting","title":"що саме опубліковано чи сказано, українською, до 200 символів","quote":null,"tone":"hostile|negative|neutral","url":"https://..."}}]}}
</result>"""


def validate_russia(items, data, today):
    block = data.get("russia_reaction") or {}
    seen = {norm_title(e.get("title")) for e in block.get("items", [])}
    good = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        d = valid_date(raw.get("date"), today)
        url = str(raw.get("url") or "").strip()
        otype = str(raw.get("outlet_type") or "").strip()
        tone = str(raw.get("tone") or "neutral").lower().strip()
        outlet = str(raw.get("outlet") or "").strip()
        key = norm_title(title)
        if not title or len(title) > 300 or not d or not outlet \
                or not url.startswith(("http://", "https://")) \
                or otype not in ALLOWED_RU_TYPES or key in seen:
            print(f"[skip:russia] {title[:60]!r}")
            continue
        quote = raw.get("quote")
        quote = str(quote).strip() if quote else None
        if quote and len(quote.split()) > 20:
            quote = None  # задовга цитата — краще без неї
        seen.add(key)
        good.append({"date": d, "outlet": outlet, "outlet_type": otype, "title": title,
                     "quote": quote,
                     "tone": tone if tone in ALLOWED_RU_TONES else "neutral", "url": url})
    return good[:MAX_NEW_ITEMS]


# --------------------------------------------------------------------------- #
# Розділ 3: злочинність у Сербії
# --------------------------------------------------------------------------- #

def prompt_crime(data, today):
    block = data.get("serbia_crime") or {}
    items = sorted(block.get("items", []), key=lambda e: e.get("date", ""))
    known = "\n".join(f"- {e.get('date')} [{e.get('category')}] {e.get('title')}" for e in items[-12:])
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    return f"""Сьогодні {today.isoformat()}. Ти аналітик представництва Торгово-промислової палати України. Знайди через веб-пошук РЕАЛЬНІ ДАТОВАНІ події в Сербії за період з {since} по {today.isoformat()} за темами: організована злочинність (кланові війни, затримання, наркотрафік); корупція (гучні справи, посадовці, держзакупівлі); економічні злочини (контрабанда, митниця, ухилення від податків, відмивання); насильницькі злочини та резонансні події (вбивства, вибухи, великі аварії); громадський порядок (протести, сутички з поліцією); правоохоронні інституції (МВС, прокуратура, BIA — кадрові зміни, критика, реформи).

Шукай сербською, англійською, російською. Надійні джерела: RFE/RL (Slobodna Evropa), Vreme, Danas, N1, KRIK, Nedeljnik, RTS, B92, Balkan Insight, Europol, Єврокомісія. НЕ використовуй сербські таблоїди (Kurir, Informer, Telegraf, Republika) як єдине джерело.

Уже є в дашборді, не повторюй:
{known or "(порожньо)"}

ПРАВИЛА: нічого не вигадуй; кожен запис — щонайменше з одним робочим URL; не вигадуй дат і чисел; якщо нічого суттєвого нема — порожній масив; максимум {MAX_NEW_ITEMS} записів.

Відповідь — ЛИШЕ JSON у тегах <result></result>:
<result>
{{"crime": [{{"date":"YYYY-MM-DD","category":"organized_crime|corruption|economic_crime|violent_crime|public_order|institutions","severity":"high|medium|low","title":"заголовок українською до 180 символів","text":"1-3 речення українською, до 400 символів","place":"місто або «загальнонаціонально»","business_relevance":"коротко, чим це важливо для іноземної компанії, або null","sources":[["Видання","https://..."]]}}]}}
</result>"""


def validate_crime(items, data, today):
    block = data.get("serbia_crime") or {}
    seen = {norm_title(e.get("title")) for e in block.get("items", [])}
    good = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        d = valid_date(raw.get("date"), today)
        cat = str(raw.get("category") or "").strip()
        sev = str(raw.get("severity") or "medium").strip()
        sources = clean_sources(raw.get("sources"))
        key = norm_title(title)
        if not title or len(title) > 300 or not d or cat not in ALLOWED_CRIME_CATS \
                or not sources or key in seen:
            print(f"[skip:crime] {title[:60]!r}")
            continue
        br = raw.get("business_relevance")
        br = str(br).strip() if br else None
        seen.add(key)
        good.append({"date": d, "category": cat,
                     "severity": sev if sev in ALLOWED_SEVERITY else "medium",
                     "title": title, "text": str(raw.get("text") or "").strip(),
                     "place": str(raw.get("place") or "").strip(),
                     "business_relevance": br, "sources": sources})
    return good[:MAX_NEW_ITEMS]


# --------------------------------------------------------------------------- #
# Примітки про дату
# --------------------------------------------------------------------------- #

def set_note(html, marker, text):
    pattern = re.compile(
        r'(<p class="updated-note" data-auto="' + re.escape(marker) + r'">)(.*?)(</p>)', re.S
    )
    if not pattern.search(html):
        print(f"[note] маркер {marker} не знайдено")
        return html
    return pattern.sub(lambda mm: mm.group(1) + text + mm.group(3), html, count=1)


def update_notes(html, today, counts):
    d = today.strftime("%d.%m.%Y")

    def phrase(n, what):
        return (f"Останній прогін: {d}. Додано нових записів: {n}."
                if n else
                f"Останній прогін: {d}. Нових {what} не знайдено — записів не додано, "
                f"навмисно без «порожніх» карток.")

    news = f"Оновлюється автоматично раз на три дні. {phrase(counts.get('news', 0), 'датованих подій')}"
    events = (f"Перевірка — автоматично раз на три дні. Останній прогін: {d}. "
              f"Перелік заходів оновлюється рідше за новини — перевіряйте дати на сайтах організаторів.")
    russia = f"Оновлюється автоматично раз на три дні. {phrase(counts.get('russia', 0), 'публікацій')}"
    crime = f"Оновлюється автоматично раз на три дні. {phrase(counts.get('crime', 0), 'подій')}"

    for marker, text in (("news-kosovo", news), ("news-serbia", news),
                         ("events-kosovo", events), ("events-serbia", events),
                         ("russia-note", russia), ("crime-note", crime)):
        html = set_note(html, marker, text)
    return html


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

SECTIONS = [
    # (ключ, json-ключ відповіді, prompt-функція, validate-функція, куди складати)
    ("news", "news", prompt_news, validate_news, None),
    ("russia", "russia", prompt_russia, validate_russia, "russia_reaction"),
    ("crime", "crime", prompt_crime, validate_crime, "serbia_crime"),
]


def main():
    today = datetime.now(timezone.utc).date()
    html = load_html()
    data, match = extract_data(html)

    counts = {}
    skip = os.environ.get("SKIP_SEARCH") == "1"
    if skip:
        print("[start] SKIP_SEARCH=1 — пошук пропущено")

    for key, json_key, make_prompt, validate, container in SECTIONS:
        counts[key] = 0
        if skip:
            continue
        try:
            raw_text = call_claude(make_prompt(data, today), key)
            found = validate(parse_json_block(raw_text, json_key), data, today)
        except Exception as exc:  # noqa: BLE001
            print(f"[error:{key}] {exc} — продовжую без цього розділу")
            continue

        if not found:
            print(f"[add:{key}] нових записів немає")
            continue

        if container is None:
            data.setdefault("belgrade_visit_events", []).extend(found)
            data["belgrade_visit_events"].sort(key=lambda e: e.get("date", ""), reverse=True)
        else:
            block = data.setdefault(container, {})
            block.setdefault("items", []).extend(found)
            block["items"].sort(key=lambda e: e.get("date", ""), reverse=True)

        counts[key] = len(found)
        for it in found:
            print(f"[add:{key}] {it['date']} {it['title'][:80]}")

    data["generated"] = today.strftime("%d.%m.%Y")
    data["last_auto_check"] = today.isoformat()

    html = write_html(html, data, match)
    html = update_notes(html, today, counts)
    with open(HTML_PATH, "w", encoding="utf-8") as fh:
        fh.write(html)

    check, _ = extract_data(load_html())
    print(f"[done] новини={len(check.get('belgrade_visit_events', []))} "
          f"РФ={len((check.get('russia_reaction') or {}).get('items', []))} "
          f"криміналітет={len((check.get('serbia_crime') or {}).get('items', []))}")

    total = sum(counts.values())
    parts = [f"{k}+{v}" for k, v in counts.items() if v]
    summary = (f"Оновлення {today.strftime('%d.%m')}: {', '.join(parts)}"
               if total else
               f"Оновлення {today.strftime('%d.%m')}: нового не знайдено, оновлено дати перевірки")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"summary={summary}\nadded={total}\n")
    print(f"[summary] {summary}")


if __name__ == "__main__":
    main()
