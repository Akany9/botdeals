"""Connecteurs API. Source d'une règle : api:football:PL,FL1 | api:adzuna:gb,ca | api:bourse:AAPL,MC.PA
Clés (variables d'environnement) : FOOTBALL_DATA_TOKEN, ADZUNA_APP_ID, ADZUNA_APP_KEY. Bourse : aucune clé."""
import html
import os
import re
import time
from datetime import date, timedelta

import requests


def _txt(s):
    return html.escape(re.sub(r"<[^>]+>", "", str(s)))


def football(codes, equipes):
    """Résultats des dernières 24 h (football-data.org, offre gratuite : 10 requêtes/min, scores différés)."""
    token = os.environ.get("FOOTBALL_DATA_TOKEN")
    if not token:
        print("FOOTBALL_DATA_TOKEN manquant")
        return []
    fav = [e.lower() for e in equipes]
    dates = {"dateFrom": (date.today() - timedelta(days=1)).isoformat(), "dateTo": date.today().isoformat(), "status": "FINISHED"}
    items = []
    for code in codes:
        r = requests.get(f"https://api.football-data.org/v4/competitions/{code}/matches",
                         headers={"X-Auth-Token": token}, params=dates, timeout=20)
        if r.status_code == 429:
            print("Limite football-data atteinte")
            break
        if not r.ok:
            print("football-data", code, r.status_code)
            continue
        for m in r.json().get("matches", []):
            dom, ext = m["homeTeam"]["name"], m["awayTeam"]["name"]
            if fav and not any(f in (dom + ext).lower() for f in fav):
                continue
            sc = m["score"]["fullTime"]
            titre = f"⚽ [{code}] {dom} {sc['home']}-{sc['away']} {ext}"
            items.append({"id": f"fd:{m['id']}", "titre": titre, "msg": f"<b>{_txt(titre)}</b>"})
        time.sleep(7)  # respecte 10 requêtes/minute
    return items


def adzuna(pays, mots):
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        print("Clés Adzuna manquantes")
        return []
    items = []
    for p in pays:
        r = requests.get(f"https://api.adzuna.com/v1/api/jobs/{p}/search/1", timeout=20, params={
            "app_id": app_id, "app_key": app_key, "results_per_page": 10, "sort_by": "date",
            "max_days_old": 2, "what_or": " ".join(mots)})
        if not r.ok:
            print("Adzuna", p, r.status_code)
            continue
        for j in r.json().get("results", []):
            titre = f"💼 [{p.upper()}] {re.sub(r'<[^>]+>', '', j['title'])} - {j.get('company', {}).get('display_name', '')}"
            msg = f"<b>{_txt(titre)}</b>\n{_txt(j.get('location', {}).get('display_name', ''))}\n{j['redirect_url']}"
            items.append({"id": f"adz:{j['id']}", "titre": titre, "msg": msg})
    return items


def bourse(tickers, seuil):
    """Alerte si la variation du dernier jour dépasse le seuil (%). Information, pas un conseil financier."""
    import yfinance as yf
    items = []
    for t in tickers:
        h = yf.Ticker(t).history(period="5d")["Close"].dropna()
        if len(h) < 2:
            continue
        var = (h.iloc[-1] / h.iloc[-2] - 1) * 100
        if abs(var) >= seuil:
            titre = f"📈 {t} {var:+.1f}% (cours {h.iloc[-1]:.2f})"
            items.append({"id": f"bourse:{t}:{h.index[-1].date()}", "titre": titre, "msg": f"<b>{_txt(titre)}</b>\nInformation, pas un conseil financier."})
    return items


def recuperer(src, mots, seuil, equipes):
    parts = src.split(":", 2)
    kind = parts[1].lower() if len(parts) > 1 else ""
    params = [x.strip() for x in parts[2].split(",") if x.strip()] if len(parts) > 2 else []
    mots_l = [m.strip() for m in str(mots).split(",") if m.strip()]
    if kind == "football":
        return football(params, equipes)
    if kind == "adzuna":
        return adzuna(params, mots_l)
    if kind == "bourse":
        try:
            s = float(str(seuil).replace(",", ".")) if str(seuil).strip() else 3.0
        except ValueError:
            s = 3.0
        return bourse(params, s)
    print("Source API inconnue :", src)
    return []


# ---------- sources configurables depuis la feuille Sources_api ----------
import csv
import gzip
import io

import feedparser


def _chemin(obj, path):
    for k in [p for p in str(path).split(".") if p]:
        if isinstance(obj, list):
            try:
                obj = obj[int(k)]
            except (ValueError, IndexError):
                return ""
        elif isinstance(obj, dict):
            obj = obj.get(k, "")
        else:
            return ""
    return obj


def _url(u):  # {VARIABLE} -> variable d'environnement (les clés ne sont jamais dans le Sheet)
    return re.sub(r"\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), str(u))


def _nombre(v):
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return 0.0


def _item(id_, titre, lien, image="", desc=""):
    titre, desc = re.sub(r"<[^>]+>", "", str(titre)), re.sub(r"<[^>]+>", "", str(desc))
    msg = f"<b>{html.escape(titre)}</b>"
    if desc:
        msg += f"\n{html.escape(desc[:220])}"
    if lien:
        msg += f"\n{lien}"
    return {"id": str(id_), "titre": titre, "msg": msg, "image": image if str(image).startswith("http") else None, "desc": desc}


def source(row):
    typ, url = str(row.get("type", "")).lower(), _url(row.get("url", ""))
    if not url or "{" in url:
        raise ValueError("URL vide ou variable d'environnement manquante")
    items = []
    if typ == "json":
        data = requests.get(url, timeout=20).json()
        liste = _chemin(data, row.get("liste", ""))
        for e in (liste if isinstance(liste, list) else [])[:50]:
            lien = _chemin(e, row.get("lien", ""))
            items.append(_item(lien or _chemin(e, row.get("titre", "")), _chemin(e, row.get("titre", "")), lien,
                               _chemin(e, row.get("image", "")), _chemin(e, row.get("description", ""))))
    elif typ == "rss":
        for e in feedparser.parse(url).entries[:50]:
            img = (e.get("media_thumbnail") or e.get("media_content") or [{}])[0].get("url", "")
            items.append(_item(e.get("link", ""), e.get("title", ""), e.get("link", ""), img, e.get("summary", "")))
    elif typ == "cheapshark":  # jeux PC (Steam, GOG, Humble...), prix en USD
        for d in requests.get(url, timeout=20).json()[:50]:
            desc = f"{d['salePrice']} $ au lieu de {d['normalPrice']} $ (-{float(d['savings']):.0f}%)"
            items.append(_item(f"cs:{d['dealID']}", d["title"], "https://www.cheapshark.com/redirect?dealID=" + d["dealID"], d.get("thumb", ""), desc))
    elif typ == "awin":  # flux produits Awin (Create-a-Feed, CSV) : seuls les articles réellement remisés
        raw = requests.get(url, timeout=120).content
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
        for p in csv.DictReader(io.StringIO(raw.decode("utf-8-sig", "replace"))):
            prix = _nombre(p.get("search_price") or p.get("store_price"))
            ancien = _nombre(p.get("rrp_price") or p.get("product_price_old"))
            if not prix or ancien <= prix:
                continue
            titre = p.get("product_name", "")
            texte = f"{titre} {p.get('brand_name', '')} {p.get('description', '')}".lower()
            filtre = [f.strip().lower() for f in str(row.get("filtre", "")).split(",") if f.strip()]
            if filtre and not any(f in texte for f in filtre):
                continue
            desc = f"{prix:.2f} {p.get('currency', '')} au lieu de {ancien:.2f} (-{(1 - prix / ancien) * 100:.0f}%)"
            items.append(_item(f"aw:{p.get('aw_product_id')}:{prix}", titre, p.get("aw_deep_link", ""),
                               p.get("aw_image_url") or p.get("merchant_image_url", ""), desc))
            if len(items) >= 20:
                break
    else:
        raise ValueError(f"type inconnu : {typ}")
    filtre = [f.strip().lower() for f in str(row.get("filtre", "")).split(",") if f.strip()]
    if filtre and typ != "awin":
        items = [i for i in items if any(f in f"{i['titre']} {i['desc']}".lower() for f in filtre)]
    return items
