"""
Bot d'alertes bons plans : lit les règles dans Google Sheets, interroge les flux RSS,
filtre (mots-clés, marques, prix max, remise min) et envoie sur Telegram.

Variables d'environnement requises :
  TG_TOKEN       token donné par @BotFather
  TG_CHAT_ID     ton identifiant de chat (ou celui du canal)
Fichier requis : creds.json (compte de service Google)
Optionnel : SHEET_NAME (défaut "BotDeals")
"""
import os
import re
import time
import html
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser
import gspread
import requests

import sources_api

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
SHEET_NAME = os.environ.get("SHEET_NAME", "BotDeals")
CRON_STEP = 20  # minutes entre deux exécutions (doit correspondre au cron)
TZ = ZoneInfo("Europe/Paris")

RE_PRIX = re.compile(r"(\d{1,5}(?:[.,]\d{1,2})?)\s?€")
RE_REMISE = re.compile(r"-\s?(\d{1,2})\s?%")


def to_float(v):
    try:
        return float(str(v).replace(",", ".").strip())
    except ValueError:
        return None


def liste(champ):
    return [x.strip().lower() for x in str(champ).split(",") if x.strip()]


def extraire_prix(texte):
    prix = [to_float(p) for p in RE_PRIX.findall(texte)]
    prix = [p for p in prix if p]
    return min(prix) if prix else None  # le plus bas = prix soldé le plus probable


def extraire_remise(texte):
    m = RE_REMISE.search(texte)
    return int(m.group(1)) if m else None


def envoyer(msg, image=None):
    if image:  # alerte avec la photo du produit
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                          json={"chat_id": CHAT_ID, "photo": image, "caption": msg[:1000], "parse_mode": "HTML"}, timeout=20)
        if r.ok:
            time.sleep(1)
            return
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
        timeout=20,
    )
    if not r.ok:
        print("Erreur Telegram :", r.text)
    time.sleep(1)  # limite de débit Telegram


def en_silence(config, maintenant):
    debut = int(to_float(config.get("silence_debut_h", 23)) or 23)
    fin = int(to_float(config.get("silence_fin_h", 7)) or 7)
    h = maintenant.hour
    return (h >= debut or h < fin) if debut > fin else (debut <= h < fin)


def regle_due(freq):
    freq = max(int(to_float(freq) or CRON_STEP), CRON_STEP)
    minutes = int(time.time() // 60)
    return minutes % freq < CRON_STEP


def main():
    gc = gspread.service_account(filename="creds.json")
    sh = gc.open(SHEET_NAME)

    config = {r["cle"]: r["valeur"] for r in sh.worksheet("Config").get_all_records()}
    maintenant = datetime.now(TZ)

    if str(config.get("pause_globale", "NON")).upper() == "OUI":
        print("Pause globale active.")
        return
    if en_silence(config, maintenant):
        print("Heures silencieuses.")
        return

    regles = sh.worksheet("Regles").get_all_records()
    equipes = [e for e in sh.worksheet("Equipes").get_all_records() if str(e.get("actif", "")).upper() == "OUI"]
    equipes_actives = [e["equipe"] for e in equipes]
    for e in equipes:  # une règle "maillots en promo" par équipe favorite
        alias = ", ".join([e["equipe"]] + [a for a in str(e.get("alias", "")).split(",") if a.strip()])
        regles.append({"actif": "OUI", "categorie": "maillot", "marques": alias, "source": "dealabs", "frequence_min": 20,
                       "mots_cles": "maillot, jersey, domicile, extérieur, exterieur, third, training, entraînement"})
    histo = sh.worksheet("Historique")
    deja_vus = set(histo.col_values(1))
    max_alertes = int(to_float(config.get("max_alertes_par_passage", 10)) or 10)
    flux_dealabs = config.get("flux_dealabs", "https://www.dealabs.com/rss/nouveaux")

    nouveaux, envoyes = [], 0
    cache_flux = {}

    for r in regles:
        if str(r.get("actif", "")).upper() != "OUI":
            continue
        if not regle_due(r.get("frequence_min")):
            continue

        src = str(r.get("source", "dealabs")).strip()
        if src.lower().startswith("api:"):
            for it in sources_api.recuperer(src, r.get("mots_cles", ""), r.get("remise_min_%"), equipes_actives):
                if it["id"] in deja_vus or envoyes >= max_alertes:
                    continue
                envoyer(it["msg"])
                envoyes += 1
                deja_vus.add(it["id"])
                nouveaux.append([it["id"], maintenant.strftime("%Y-%m-%d %H:%M"), r.get("categorie", ""), it["titre"][:200]])
            continue
        url = flux_dealabs if src.lower() == "dealabs" else src
        if url not in cache_flux:
            cache_flux[url] = feedparser.parse(url).entries
        entrees = cache_flux[url]

        mots = liste(r.get("mots_cles"))
        marques = liste(r.get("marques"))
        prix_max = to_float(r.get("prix_max"))
        remise_min = to_float(r.get("remise_min_%"))

        for e in entrees:
            lien = e.get("link", "")
            if not lien or lien in deja_vus:
                continue
            titre = e.get("title", "")
            texte = f"{titre} {e.get('summary', '')}".lower()

            if mots and not any(m in texte for m in mots):
                continue
            if marques and not any(m in texte for m in marques):
                continue

            prix = extraire_prix(titre) or extraire_prix(e.get("summary", ""))
            remise = extraire_remise(titre) or extraire_remise(e.get("summary", ""))
            if prix_max is not None and prix is not None and prix > prix_max:
                continue
            if remise_min is not None and remise is not None and remise < remise_min:
                continue

            if envoyes >= max_alertes:
                break
            details = []
            if prix:
                details.append(f"💶 {prix:.2f} €")
            if remise:
                details.append(f"🔻 -{remise}%")
            msg = (
                f"🔥 <b>{html.escape(titre)}</b>\n"
                f"[{html.escape(str(r.get('categorie', '')))}] {' · '.join(details)}\n"
                f"{lien}"
            )
            envoyer(msg)
            envoyes += 1
            deja_vus.add(lien)
            nouveaux.append([lien, maintenant.strftime("%Y-%m-%d %H:%M"),
                             r.get("categorie", ""), titre[:200]])

    for row in sh.worksheet("Sources_api").get_all_records():  # sources ajoutées depuis le dashboard
        if str(row.get("actif", "")).upper() != "OUI" or not regle_due(row.get("frequence_min")):
            continue
        try:
            items = sources_api.source(row)
        except Exception as e:
            print("Source", row.get("nom"), ":", e)
            continue
        for it in items:
            if it["id"] in deja_vus or envoyes >= max_alertes:
                continue
            envoyer(it["msg"], it.get("image"))
            envoyes += 1
            deja_vus.add(it["id"])
            nouveaux.append([it["id"], maintenant.strftime("%Y-%m-%d %H:%M"), row.get("nom", ""), it["titre"][:200]])

    if nouveaux:
        histo.append_rows(nouveaux)
    print(f"{envoyes} alerte(s) envoyée(s).")


if __name__ == "__main__":
    main()
