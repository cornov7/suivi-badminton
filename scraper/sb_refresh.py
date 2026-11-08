#!/usr/bin/env python3
"""Refresh the data of the "Suivi Swiss Badminton" page from swiss-badminton.ch.

Reads the public Match Center (interclub: every league and group, through the same
form requests its league/group menus send), the national tournament list and the
national-team events, then writes the documents the page stores:
  data/packs/pack-NN.json   interclub groups, packed under 200 KB per document
  data/sb_meta.json         season, freshness, sources
  data/sb_tournaments.json  tournaments of the season
  data/summary.json         counts and checks of each part (interclub, tournaments)
A part whose checks fail keeps its previous files. Exit code 0 = both parts fresh and
checked, 2 = at least one part failed (its previous data is kept).
Usage: python sb_refresh.py [output_dir]   (SB_DEBUG=1 also saves raw pages to debug/)
"""
import json, os, re, sys, time, datetime as dt
from collections import Counter, defaultdict
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:  # fresh container without the libraries
    os.system(f"{sys.executable} -m pip install -q --break-system-packages requests beautifulsoup4")
    import requests
    from bs4 import BeautifulSoup

BASE = "https://www.swiss-badminton.ch"
MC = BASE + "/fr/match-center/"
TOURS = BASE + "/fr/match-center/tournois/"
INTL = BASE + "/match-center/internationale-turniere/"
OUT = sys.argv[1] if len(sys.argv) > 1 else "data"
DELAY = 0.35
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Zurich")
except Exception:  # noqa: BLE001
    TZ = dt.timezone(dt.timedelta(hours=1))
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (compatible; SuiviSwissBadminton/1.0)", "Accept-Language": "fr-CH,fr;q=0.9"})
MONTHS = {"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
          "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12}


def get(url, method="GET", **kw):
    for attempt in range(3):
        try:
            r = S.request(method, url, timeout=60, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == 2 or (getattr(e, "response", None) is not None and e.response.status_code in (401, 403, 404)):
                raise
            time.sleep(3 * (attempt + 1))


def soup_of(html):
    return BeautifulSoup(html, "html.parser")


def txt(el):
    if el is None:
        return ""
    for br in el.find_all("br"):
        br.replace_with(" ")
    return re.sub(r"\s+", " ", el.get_text(" ")).strip()


def fr_date(s):
    m = re.search(r"(\d{1,2})\.?\s+([A-Za-zéèêûôîçà]+)\s+(\d{4})", s)
    t = re.search(r"(\d{1,2}):(\d{2})", s)
    d = f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}" if m and m.group(2).lower() in MONTHS else ""
    return d, (f"{int(t.group(1)):02d}:{t.group(2)}" if t else "")


# ------------------------------------------------------------------ interclub
def form_fields(form):
    fields = []
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name:
            continue
        if el.name == "select":
            opt = el.find("option", selected=True) or el.find("option")
            fields.append([name, opt.get("value", "") if opt else ""])
        elif el.name == "textarea":
            fields.append([name, el.text])
        else:
            typ = (el.get("type") or "text").lower()
            if typ in ("submit", "button", "image", "reset", "file"):
                continue
            if typ in ("checkbox", "radio") and not el.has_attr("checked"):
                continue
            fields.append([name, el.get("value", "")])
    return fields


def parse_matches(wrap):
    out = []
    for rd in wrap.select(".c-matchcenter-games__round"):
        r = txt(rd.select_one(".c-matchcenter-games__title"))
        for it in rd.select(".c-matchcenter-games__item"):
            d, t = fr_date(txt(it.select_one(".c-matchcenter-games__date")))
            lab = txt(it.select_one(".c-matchcenter-games__label"))
            sc = re.fullmatch(r"(\d+)\s*-\s*(\d+)", lab)
            a = it.select_one('a[href*="/team-match/"]')
            href = a.get("href", "") if a else ""
            out.append({"r": r, "d": d, "t": t, "h": txt(it.select_one(".c-matchcenter-games__team-home")),
                        "a": txt(it.select_one(".c-matchcenter-games__team-away")),
                        "hs": int(sc.group(1)) if sc else None, "as": int(sc.group(2)) if sc else None,
                        "id": (re.search(r"team-match/(\d+)", href) or [None, ""])[1],
                        "lg": (re.search(r"league/([0-9A-Fa-f-]{36})", href) or [None, ""])[1]})
    return out


def parse_standings(wrap):
    rows = []
    for tr in wrap.select("tbody tr"):
        cells = [txt(td) for td in tr.find_all(["td", "th"])]
        a = tr.select_one('a[href*="/team/"]')
        href = a.get("href", "") if a else ""
        rows.append({"cells": cells, "team": txt(a) if a else "", "tid": (re.search(r"/team/(\d+)", href) or [None, ""])[1],
                     "lg": (re.search(r"league/([0-9A-Fa-f-]{36})", href) or [None, ""])[1]})
    return rows


def scrape_interclub():
    page = soup_of(get(MC).text)
    forms = [f for f in page.find_all("form") if f.select_one('select[name$="[event]"]') and f.get("data-response-class")]
    if len(forms) < 3:
        raise SystemExit("Match Center: league forms not found (page layout changed?)")
    roles = {}
    for f in forms:
        w = page.select_one("." + f["data-response-class"])
        if w is None:
            continue
        if w.select("tbody tr"):
            roles.setdefault("st", f)
        elif any(re.fullmatch(r"\d+\s*-\s*\d+", txt(x)) for x in w.select(".c-matchcenter-games__label")):
            roles.setdefault("re", f)
        else:
            roles.setdefault("up", f)
    for key, f in zip(("st", "up", "re"), forms):  # fallback: page order is table, next games, results
        roles.setdefault(key, f)
    meta = {k: {"action": urljoin(MC, f.get("action", "")), "fields": form_fields(f), "wrap": f["data-response-class"]} for k, f in roles.items()}
    ev_sel = roles["st"].select_one('select[name$="[event]"]')
    events = [(o.get("value"), txt(o)) for o in ev_sel.find_all("option") if o.get("value")]

    def post(role, event, draw):
        m = meta[role]
        data = []
        for name, value in m["fields"]:
            if name.endswith("[event]"):
                value = str(event)
            elif name.endswith("[draw]"):
                value = "" if draw is None else str(draw)
            data.append((name, value))
        r = get(m["action"], method="POST", data=data, headers={"X-Requested-With": "XMLHttpRequest", "Referer": MC})
        time.sleep(DELAY)
        w = soup_of(r.text).select_one("." + m["wrap"])
        if w is None:
            raise RuntimeError(f"no response block for {role} {event}/{draw}")
        dsel = w.select_one('select[name$="[draw]"]')
        chosen = (dsel.find("option", selected=True) or {}).get("value") if dsel else None
        return w, chosen

    draws_by_event = []
    for ev, ename in events:
        w, _ = post("st", ev, None)
        dsel = w.select_one('select[name$="[draw]"]')
        draws_by_event.append([ev, ename, [(o.get("value"), txt(o)) for o in dsel.find_all("option") if o.get("value")] if dsel else []])
    draws, problems = [], []
    for ev, ename, dl in draws_by_event:
        for dr, dname in dl:
            ws = {}
            for role in ("st", "up", "re"):
                w, chosen = post(role, ev, dr)
                if chosen and chosen != dr:
                    problems.append(f"{dname}: {role} answered draw {chosen}")
                ws[role] = w
            draws.append({"ev": ev, "ename": ename, "dr": dr, "dname": dname, "st": parse_standings(ws["st"]),
                          "up": parse_matches(ws["up"]), "re": parse_matches(ws["re"])})
    return draws_by_event, draws, problems


LEAGUES = [("NLA", "NLA"), ("NLB", "NLB"), ("1. Liga", "1re ligue"), ("2. Liga", "2e ligue"), ("3. Liga", "3e ligue"),
           ("3. Liga - 2HD", "3e ligue 2HD"), ("4. Liga", "4e ligue"), ("4. Liga - 2HD", "4e ligue 2HD"), ("NL - 5. Liga", "5e ligue NL"),
           ("AVB-S", "AVB-S"), ("BVRZ-S", "BVRZ-S"), ("AVcB-J", "AVcB-J"), ("BVRZ-J", "BVRZ-J")]
LNAME = dict(LEAGUES)


def tie_pts(f, a, n):
    if n == 7:
        return [0, 0, 1, 1, 2, 2, 3, 3][f] if 0 <= f <= 7 else None
    if n == 4:
        return f
    if n == 8:
        if f == a:
            return 2
        if f > a:
            return 4 if f >= 7 else 3
        return 0 if f <= 1 else 1
    return None


def group_name(ename, dname):
    n = dname[len(ename) + 3:] if dname.startswith(ename + " - ") else ("" if dname == ename else dname)
    n = {"Group matches": "Phase de groupes", "West/Ouest": "Ouest", "Ost/Est": "Est"}.get(n, n)
    if re.fullmatch(r"\d+", n):
        n = "Groupe " + n
    return n or "Groupe unique"


def build_groups(draws_by_event, draws, now):
    lorder = {ename: i + 1 for i, (ev, ename, dl) in enumerate(draws_by_event)}
    dorder = {dr: i + 1 for ev, ename, dl in draws_by_event for i, (dr, dn) in enumerate(dl)}
    groups, issues = [], []
    for x in draws:
        ename, dname, dr = x["ename"], x["dname"], x["dr"]
        lg = next((m["lg"] for m in x["up"] + x["re"] if m["lg"]), "") or next((r["lg"] for r in x["st"] if r["lg"]), "")
        seen, matches = set(), []
        for m in x["re"] + x["up"]:
            if not m["h"] or not m["a"] or not m["id"] or m["id"] in seen:
                continue
            seen.add(m["id"])
            hs, as_ = m["hs"], m["as"]
            if hs is not None and hs + as_ == 0:  # 0-0 = result not entered yet, not counted in the tables
                hs = as_ = None
            rm = re.fullmatch(r"R(\d+)", m["r"] or "")
            mm = {"id": m["id"], "round": int(rm.group(1)) if rm else 0, "date": m["d"], "time": m["t"], "home": m["h"], "away": m["a"],
                  "hs": hs, "as": as_, "url": f"https://sb.tournamentsoftware.com/league/{m['lg'] or lg}/team-match/{m['id']}"}
            if not rm:
                mm["rl"] = m["r"] or ""
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", m["d"]):
                issues.append(f"date illisible: {dname} {m['d']}")
            matches.append(mm)
        if not x["st"] and not matches:
            continue  # play-off draws without teams yet
        matches.sort(key=lambda m: (m["date"], m["time"] or "99", m["round"]))
        standings = []
        for r in x["st"]:
            c = r["cells"]
            try:
                pair = lambda s: [int(v) for v in s.split("-")]
                standings.append({"rank": int(re.match(r"(\d+)", c[0]).group(1)), "team": r["team"], "played": int(c[1]), "won": int(c[2]),
                                  "drawn": int(c[3]), "lost": int(c[4]), "points": int(c[5]), "matches": pair(c[6]), "sets": pair(c[7]),
                                  "games": pair(c[8]), "url": f"https://sb.tournamentsoftware.com/league/{r['lg'] or lg}/team/{r['tid']}" if r["tid"] else ""})
            except (ValueError, IndexError, AttributeError):
                issues.append(f"ligne de classement illisible: {dname} {c}")
        played = [m for m in matches if m["hs"] is not None]
        sizes = Counter(m["hs"] + m["as"] for m in played)
        tie = sizes.most_common(1)[0][0] if sizes else (8 if ename in ("NLA", "NLB") or "2HD" in ename else 7)
        acc = defaultdict(lambda: [0] * 7)
        for m in played:
            for t, f, a in ((m["home"], m["hs"], m["as"]), (m["away"], m["as"], m["hs"])):
                v = acc[t]; v[0] += 1; v[1] += f > a; v[2] += f == a; v[3] += f < a; v[4] += f; v[5] += a
                p = tie_pts(f, a, tie); v[6] += p if p is not None else -999
        wdl_ok = all(acc[s["team"]][:6] == [s["played"], s["won"], s["drawn"], s["lost"], *s["matches"]] for s in standings)
        pts_ok = all(acc[s["team"]][6] == s["points"] for s in standings)
        teams = [{"name": s["team"], "club": re.sub(r"\s+\d+$", "", s["team"]), "url": s["url"]} for s in sorted(standings, key=lambda s: s["rank"])]
        if not teams:
            teams = [{"name": n, "club": re.sub(r"\s+\d+$", "", n), "url": ""} for n in sorted({m["home"] for m in matches} | {m["away"] for m in matches})]
        groups.append({"id": "d" + dr, "drawId": dr, "league": LNAME.get(ename, ename), "leagueOrder": lorder.get(ename, 99),
                       "name": group_name(ename, dname), "order": dorder.get(dr, 99), "kind": "complet", "tieSize": tie,
                       "ruleOk": bool(played) and wdl_ok and pts_ok and tie in (4, 7, 8), "tableMatchesResults": wdl_ok,
                       "teams": teams, "standings": standings, "matches": matches,
                       "source": {"label": "Match Center swiss-badminton.ch", "url": MC}, "updatedAt": now})
    per_league = Counter(g["league"] for g in groups)
    for g in groups:
        g["label"] = g["league"] + (" " + re.sub(r"^Groupe ", "", g["name"]) if per_league[g["league"]] > 1 else "")
    groups.sort(key=lambda g: (g["leagueOrder"], g["order"]))
    return groups, issues


def pack(groups, limit=200_000):
    packs, cur, size = [], [], 0
    for g in groups:
        n = len(json.dumps(g, ensure_ascii=False).encode())
        if cur and size + n > limit:
            packs.append(cur); cur, size = [], 0
        cur.append(g); size += n
    if cur:
        packs.append(cur)
    return packs


# ---------------------------------------------------------------- tournaments
CANTON = {
    "Aarau": "AG", "Affoltern am Albis": "ZH", "Allenlüften": "BE", "Allschwil": "BL", "Altdorf": "UR", "Arbon": "TG", "Arlesheim": "BL",
    "Ayent": "VS", "Baldegg": "LU", "Basel": "BS", "Bercher": "VD", "Bern": "BE", "Biel/Bienne": "BE", "Brig-Glis": "VS", "Bulle": "FR",
    "Burgdorf": "BE", "Bülach": "ZH", "Collombey-Muraz": "VS", "Courrendlin": "JU", "Delémont": "JU", "Ettingen": "BL", "Frauenfeld": "TG",
    "Freienstein": "ZH", "Fribourg": "FR", "Gebenstorf": "AG", "Genève": "GE", "Gevève": "GE", "Gossau SG": "SG", "Gränichen": "AG",
    "Gurmels": "FR", "Heerbrugg": "SG", "Herzogenbuchsee": "BE", "Hinwil": "ZH", "Jegenstorf": "BE", "Kaiseraugst": "AG", "Kerzers": "FR",
    "La Tour-de-Peilz": "VD", "Lausanne": "VD", "Le Landeron": "NE", "Le Sentier": "VD", "Lenzburg": "AG", "Les Acacias": "GE",
    "Leytron": "VS", "Liestal": "BL", "Littau": "LU", "Luzern": "LU", "Lyss": "BE", "Marly": "FR", "Martigny": "VS", "Morat": "FR",
    "Morges": "VD", "Neuchâtel": "NE", "Näfels": "GL", "Nürensdorf": "ZH", "Oberkirch/Sursee": "LU", "Oberuzwil": "SG", "Olten": "SO",
    "Orbe": "VD", "Orbe VD": "VD", "Payerne": "VD", "Rafz": "ZH", "Riaz": "FR", "Romont": "FR", "Schüpfen": "BE", "Sion": "VS",
    "Solothurn": "SO", "Spiez": "BE", "St-Maurice": "VS", "St. Gallen": "SG", "Sursee": "LU", "Therwil": "BL", "Thierrens": "VD",
    "Thun": "BE", "Uster": "ZH", "Vaduz": "FL", "Versoix": "GE", "Villars-sur-Glâne": "FR", "Weinfelden": "TG", "Wettingen": "AG",
    "Widen": "AG", "Widen AG": "AG", "Winterthur": "ZH", "Yverdon / Winterthur": "VD/ZH", "Yverdon-les-Bains": "VD", "Zollikofen": "BE",
    "Zug": "ZG", "Zuzwil": "SG", "Zürich": "ZH", "Langnau am Albis": "ZH", "Wittenbach": "SG",
}
AGE_RE = re.compile(r"U0?(\d{1,2})")


def parse_ages(s):
    ages = set()
    for tok in re.split(r"[/,;\s]+", s or ""):
        for m in AGE_RE.finditer(tok):
            ages.add(int(m.group(1)))
        m = re.fullmatch(r"U0?(\d{1,2})-(\d{1,2})", tok)
        if m:
            ages.add(int(m.group(2)))
    return [f"U{a}" for a in sorted(ages)]


def types_for(cat):
    cat = (cat or "").strip()
    if cat == "RT & NL":
        return ["RT", "NL"]
    if cat in ("RT", "RTJ", "SBJS", "NL"):
        return [cat]
    if cat in ("Other", "AirBadminton", "Autre"):
        return ["AUTRE"]
    return ["NC"]


def tags_for(name, cat):
    n = name.lower(); tags = []
    if "sbes" in n: tags.append("Elite Series")
    if re.search(r"schweizer ?meisterschaft|championnats? suisses?", n): tags.append("Championnat suisse")
    elif re.search(r"meisterschaft|championnat", n): tags.append("Championnat régional")
    if "sbjs" in n and "gold" in n: tags.append("SBJS Gold")
    if "sbjs" in n and "silver" in n: tags.append("SBJS Silver")
    if "para-badminton" in n: tags.append("Para")
    if "annul" in n or "abgesagt" in n: tags.append("Annulé")
    if cat == "AirBadminton": tags.append("AirBadminton")
    return tags


def parse_dates(s):
    ds = re.findall(r"(\d{2})\.(\d{2})\.(\d{4})", s)
    if not ds:
        return None, None
    iso = [f"{y}-{m}-{d}" for d, m, y in ds]
    return iso[0], iso[-1]


GUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
TS_ID = re.compile(r"tournamentsoftware\.com/.*?(" + GUID + ")", re.I)
DATE_RE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{4}(?:\s*[-–]\s*\d{1,2}\.\d{1,2}\.\d{4})?")
CATS = {"rt & nl": "RT & NL", "rt&nl": "RT & NL", "airbadminton": "AirBadminton", "other": "Other", "autre": "Other",
        "andere": "Other", "sbjs": "SBJS", "rtj": "RTJ", "rt": "RT", "nl": "NL"}
LABEL_RE = re.compile(r"^(date|datum|nom|name|tournoi|turnier|catégorie|categorie|kategorie|category|lieu|ort|location|"
                      r"catégories d'âge|categories d'age|altersklassen?|age ?groups?|âge)\s*:?\s*", re.I)


def strings_of(el):
    for br in el.find_all("br"):
        br.replace_with("\n")
    out = []
    for s in el.stripped_strings:
        for part in s.split("\n"):
            part = re.sub(r"\s+", " ", part).strip()
            if part:
                out.append(part)
    return out


def ids_in(el, rx):
    return {m.group(1).upper() for a in el.find_all("a", href=True) for m in [rx.search(a["href"])] if m}


def row_of(a, rx):
    """Largest ancestor of the link that still holds only this one event: the row of the list."""
    row, el = None, a
    for _ in range(10):
        el = el.parent
        if el is None or el.name in ("body", "html", "main"):
            break
        if len(ids_in(el, rx)) > 1:
            break
        row = el
    return row


AGE_TOK = re.compile(r"(?i)U\s?\d{1,2}(?:\s?-\s?U?\d{1,2})?|O\d{2}|Elite|Seniors?|Senioren|Juniors?|Junioren")


def is_ages(v):
    toks = [t for t in re.split(r"\s*[,/;|]\s*|\s+(?=U\d)", v) if t]
    return bool(toks) and all(AGE_TOK.fullmatch(t) for t in toks) and re.search(r"(?i)U\s?\d", v) is not None


def norm_t(s):
    return s.replace(" ", " ").strip()


def parse_row(row, link, rx, today):
    ss = [norm_t(s) for s in strings_of(row)]
    di = next((i for i, s in enumerate(ss) if DATE_RE.search(s)), None)
    if di is None:
        return None
    dtxt = DATE_RE.search(ss[di]).group(0)
    nxt = ss[di + 1].lstrip("-– ").strip() if di + 1 < len(ss) else ""
    if "-" not in dtxt and "–" not in dtxt and re.fullmatch(r"[-–]?\s*\d{1,2}\.\d{1,2}\.\d{4}", nxt):
        dtxt += " - " + nxt  # range split over two lines
        ss[di + 1] = ""
    start, end = parse_dates(dtxt.replace("–", "-"))
    if not start:
        return None
    links = [a for a in row.find_all("a", href=True) if rx.search(a["href"])]
    name = max((txt(a) for a in links), key=len, default="")
    cat, ages, loc, rest = "", [], "", []
    for s in ss:
        v = LABEL_RE.sub("", s).strip()
        if not v or DATE_RE.fullmatch(v) or v == name:
            continue
        if not cat and v.lower() in CATS:
            cat = CATS[v.lower()]
        elif is_ages(v):
            ages = parse_ages(v.replace(" ", ""))
        elif not loc and v in CANTON:
            loc = v
        else:
            rest.append(v)
    if not name:
        name = max(rest, key=len, default="")
        rest = [r for r in rest if r != name]
    if not loc:
        cand = [r for r in rest if len(r) <= 40 and not re.search(r"\d{2}:\d{2}|inscription|anmeldung|résultats|resultate|détails|details|plus|mehr", r, re.I)]
        loc = cand[-1] if cand else ""
    return {"name": re.sub(r"\s+", " ", name).strip().strip('"'), "start": start, "end": end, "category": cat,
            "ages": ages, "location": loc, "up": end >= today}


def tournament_rows(html, today):
    """Rows of one page of the national tournament list: (rows, upcoming_total_on_site, next_page_url, page_no)."""
    soup = soup_of(html)
    rows, done = [], set()
    for a in soup.find_all("a", href=True):
        m = TS_ID.search(a["href"])
        if not m or m.group(1).upper() in done:
            continue
        tid = m.group(1).upper()
        row = row_of(a, TS_ID)
        if row is None:
            continue
        p = parse_row(row, a, TS_ID, today)
        if not p:
            continue
        done.add(tid)
        url = a["href"] if "/sport/tournament" in a["href"] else f"https://sb.tournamentsoftware.com/sport/tournament?id={tid}"
        cat = p["category"]
        tags = tags_for(p["name"], cat)
        rows.append({"id": tid, "name": p["name"], "start": p["start"], "end": p["end"], "category": cat, "types": types_for(cat),
                     "ages": p["ages"], "location": p["location"], "canton": CANTON.get(p["location"], ""), "url": url,
                     "tags": tags, "_up": p["up"]})
    text = re.sub(r"\s+", " ", soup.get_text(" "))
    tot = re.search(r"(\d+)\s+tournois?\s+trouvés?", text, re.I)
    return rows, (int(tot.group(1)) if tot else None), soup


def next_page(soup, base, cur):
    for a in soup.find_all("a", href=True):
        h = a["href"].replace("%5B", "[").replace("%5D", "]")
        m = re.search(r"\[currentPage\]=(\d+)", h)
        if m and int(m.group(1)) == cur + 1:
            return urljoin(base, a["href"])
    return None


def scrape_tournaments(season_start, today, debug_dir=None):
    url, pages, rows, total = TOURS, 0, {}, None
    while url and pages < 15:
        pages += 1
        html = get(url).text
        if debug_dir and pages <= 2:
            open(os.path.join(debug_dir, f"tournois-p{pages}.html"), "w", encoding="utf-8").write(html)
        got, tot, soup = tournament_rows(html, today)
        total = total if total is not None else tot
        new = [r for r in got if r["id"] not in rows]
        for r in got:
            rows.setdefault(r["id"], r)
        past = [r for r in got if not r["_up"]]
        older = past and min(r["end"] for r in past) < season_start
        nxt = next_page(soup, url, pages)
        url = nxt if (nxt and new and (any(r["_up"] for r in got) or not older)) else None
        time.sleep(DELAY)
    items = [dict((k, v) for k, v in r.items() if k != "_up") for r in rows.values() if r["end"] >= season_start]
    n_up = sum(1 for r in rows.values() if r["_up"])
    return sorted(items, key=lambda t: (t["start"], t["end"], t["name"])), total, n_up, pages


HITO = re.compile(r"(hitobito\.[^/]+/.*?/events/(\d+))", re.I)


def scrape_international(today, debug_dir=None):
    html = get(INTL).text
    if debug_dir:
        open(os.path.join(debug_dir, "internationaux.html"), "w", encoding="utf-8").write(html)
    soup = soup_of(html)
    out, done = [], set()
    for a in soup.find_all("a", href=True):
        m = HITO.search(a["href"])
        if not m or m.group(2) in done:
            continue
        row = row_of(a, HITO)
        if row is None:
            continue
        ss = [norm_t(s) for s in strings_of(row)]
        joined = " | ".join(ss)
        dm = DATE_RE.search(joined)
        if not dm:
            continue
        start, end = parse_dates(dm.group(0).replace("–", "-"))
        name = txt(a)
        if len(name) < 4 or re.fullmatch(r"(?i)(plus|mehr|details?|détails|info(s)?|anmeldung|inscription|>+)", name):
            name = max((s for s in ss if not DATE_RE.search(s)), key=len, default=name)
        done.add(m.group(2))
        ages = parse_ages(" ".join(s for s in ss if s != name))
        if not ages and re.search(r"\b(elite|senior)", joined, re.I) is None:
            ages = parse_ages(name)
        out.append({"id": "INT-" + m.group(2), "name": name.strip(), "start": start, "end": end, "category": "International",
                    "types": ["INT"], "ages": ages, "location": "", "canton": "",
                    "url": a["href"] if a["href"].startswith("http") else urljoin(INTL, a["href"]), "tags": ["Équipe nationale"]})
    return out


def dump(obj, *path, indent=None):
    with open(os.path.join(OUT, *path), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)


def load(*path):
    try:
        with open(os.path.join(OUT, *path), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def main():
    """Each part (interclub, tournaments) is checked on its own. A part that fails keeps the
    previous files, so the data in OUT is always the last good collection of each part."""
    import traceback
    now = dt.datetime.now(TZ).replace(microsecond=0)
    stamp = now.isoformat()
    today = now.date().isoformat()
    season_y = now.year if now.month >= 7 else now.year - 1
    season_start = f"{season_y}-08-01"
    debug_dir = "debug" if os.environ.get("SB_DEBUG") else None
    if debug_dir:
        os.makedirs(debug_dir, exist_ok=True)
    os.makedirs(os.path.join(OUT, "packs"), exist_ok=True)
    prev = load("summary.json") or {}
    summary = {"startedAt": stamp}

    # interclub
    try:
        draws_by_event, draws, problems = scrape_interclub()
        groups, issues = build_groups(draws_by_event, draws, stamp)
        played = sum(1 for g in groups for m in g["matches"] if m["hs"] is not None)
        ic = {"leagues": len(draws_by_event), "draws": len(draws), "groups": len(groups),
              "teams": sum(len(g["teams"]) for g in groups), "matches": sum(len(g["matches"]) for g in groups), "played": played,
              "rule_validated": sum(g["ruleOk"] for g in groups),
              "table_mismatch": [g["label"] for g in groups if g["standings"] and not g["tableMatchesResults"]],
              "problems": problems[:20], "issues": issues[:20]}
        ic["ok"] = len(groups) >= 40 and ic["matches"] >= 2000 and not problems and len(draws_by_event) >= 10
        if ic["ok"]:
            packs = pack(groups)
            names = [f"pack-{i:02d}" for i in range(1, len(packs) + 1)]
            for n, p in zip(names, packs):
                dump({"groups": p, "updatedAt": stamp}, "packs", n + ".json")
            for fn in os.listdir(os.path.join(OUT, "packs")):
                if fn.endswith(".json") and fn[:-5] not in names:
                    os.remove(os.path.join(OUT, "packs", fn))
            ic.update({"packs": names, "updatedAt": stamp})
        else:
            old = prev.get("interclub") or {}
            ic.update({"packs": old.get("packs", []), "updatedAt": old.get("updatedAt")})
    except Exception as e:  # noqa: BLE001 - report and keep the previous data
        old = prev.get("interclub") or {}
        ic = {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:],
              "packs": old.get("packs", []), "updatedAt": old.get("updatedAt")}
    summary["interclub"] = ic

    # tournaments (national list + national-team events)
    try:
        tours, site_total, n_up, pages = scrape_tournaments(season_start, today, debug_dir)
        intl, intl_err = [], None
        try:
            intl = scrape_international(today, debug_dir)
        except Exception as e:  # noqa: BLE001
            intl_err = f"{type(e).__name__}: {e}"
        missing_loc = sum(1 for t in tours if not t["location"])
        missing_cat = sum(1 for t in tours if not t["category"])
        tr = {"items": len(tours), "upcoming_scraped": n_up, "upcoming_site": site_total, "pages": pages,
              "international": len(intl), "international_error": intl_err, "no_location": missing_loc, "no_category": missing_cat,
              "sample": tours[:3]}
        tr["ok"] = (len(tours) >= 50 and (site_total is None or n_up == site_total) and missing_cat <= len(tours) // 10
                    and missing_loc <= len(tours) // 5)
        if tr["ok"]:
            old_intl = [t for t in (load("sb_tournaments.json") or {}).get("items", []) if t.get("types") == ["INT"]]
            if intl_err or not intl:  # keep the last known national-team events
                intl = [t for t in old_intl if t["end"] >= season_start]
            items = sorted(tours + intl, key=lambda t: (t["start"], t["end"], t["name"]))
            dump({"items": items, "updatedAt": stamp, "source": TOURS}, "sb_tournaments.json")
            tr["updatedAt"] = stamp
        else:
            tr["updatedAt"] = (prev.get("tournaments") or {}).get("updatedAt")
    except Exception as e:  # noqa: BLE001
        tr = {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:],
              "updatedAt": (prev.get("tournaments") or {}).get("updatedAt")}
    summary["tournaments"] = tr

    if ic["ok"] or tr["ok"]:
        dump({"season": f"{season_y}/{(season_y + 1) % 100:02d}", "updatedAt": stamp, "missingLeagues": [],
              "interclubUpdatedAt": ic.get("updatedAt"), "tournamentsUpdatedAt": tr.get("updatedAt"),
              "sources": [{"label": "Tournois nationaux", "url": TOURS}, {"label": "Match Center interclub", "url": MC},
                          {"label": "Sélections nationales", "url": INTL}],
              "matchCenter": MC}, "sb_meta.json")
    summary["finishedAt"] = dt.datetime.now(TZ).replace(microsecond=0).isoformat()
    summary["ok"] = bool(ic["ok"] and tr["ok"])
    dump(summary, "summary.json", indent=1)
    print(json.dumps({k: ({kk: vv for kk, vv in v.items() if kk not in ("sample", "trace")} if isinstance(v, dict) else v)
                      for k, v in summary.items()}, ensure_ascii=False, indent=1))
    for part in ("interclub", "tournaments"):
        if summary[part].get("trace"):
            print(f"\n--- {part} ---\n" + summary[part]["trace"])
    sys.exit(0 if summary["ok"] else 2)


if __name__ == "__main__":
    main()
