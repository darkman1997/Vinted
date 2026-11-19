#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vinted.py — Bot Telegram "Vinted": avvisa quando escono nuovi annunci
che corrispondono alle ricerche salvate (con prezzo massimo).

Solo libreria standard di Python 3.8+ (nessun pip install).

Variabili d'ambiente
  TELEGRAM_TOKEN     obbligatoria  token del bot (da @BotFather)
  TELEGRAM_CHAT_ID   facoltativa   default: 8798296477 (la chat di Francesco)
  STATO              facoltativa   default vinted-stato.json
  DOMINIO            facoltativa   default www.vinted.it

Uso
  python vinted.py --once       un giro: comandi Telegram + controllo ricerche
  python vinted.py --dry-run    un giro senza inviare nulla e senza salvare
  python vinted.py --test       messaggio di prova su Telegram
  python vinted.py --aggiungi "harry potter blu-ray" 15
  python vinted.py --lista
  python vinted.py --togli 2
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

try:  # opzionale: serve a capire la lingua dell'annuncio
    from langdetect import DetectorFactory, detect_langs
    DetectorFactory.seed = 0
except Exception:  # senza libreria il filtro lingua resta spento
    DetectorFactory = None
    detect_langs = None

TG_BASE = "https://api.telegram.org/bot{token}/{method}"
DOMINIO = (os.environ.get("DOMINIO") or "www.vinted.it").strip()
TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
CHAT_ID = (os.environ.get("TELEGRAM_CHAT_ID") or "8798296477").strip()
STATO = (os.environ.get("STATO") or "vinted-stato.json").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PAROLE_VIETATE = ("paypal", "bonifico", "consegna a mano", "consegna amano",
                  "pagamento anticipato", "cerco", "cercasi", "cerchiamo",
                  "compro", "compriamo")
MAX_ANNUNCI = 8          # quanti annunci nuovi mostrare per ricerca in un messaggio
TTL_GIORNI = 30          # dopo quanti giorni dimenticare gli annunci gia' visti
MAX_VISTI = 3000         # limite per ricerca, per non far crescere il file all'infinito


# --------------------------------------------------------------------------
# utilita' rete
# --------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _apri(url: str, referer: str = "") -> str:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    req.add_header("Accept-Language", "it-IT,it;q=0.9,en;q=0.6")
    req.add_header("Accept-Encoding", "gzip")
    if referer:
        req.add_header("Referer", referer)
    with urllib.request.urlopen(req, timeout=45) as r:
        dati = r.read()
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            try:
                dati = gzip.decompress(dati)
            except OSError:
                pass
        return dati.decode("utf-8", "replace")


def telegram(metodo: str, **campi) -> dict:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN mancante")
    dati = urllib.parse.urlencode({k: v for k, v in campi.items() if v is not None}).encode()
    req = urllib.request.Request(TG_BASE.format(token=TELEGRAM_TOKEN, method=metodo), data=dati)
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def invia(testo: str, markup: dict | None = None) -> None:
    campi = dict(chat_id=CHAT_ID, text=testo, parse_mode="HTML", disable_web_page_preview="true")
    if markup:
        campi["reply_markup"] = json.dumps(markup)
    try:
        telegram("sendMessage", **campi)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        campi.update(text=re.sub(r"<[^>]+>", "", testo), parse_mode=None)
        telegram("sendMessage", **campi)


def invia_foto(foto: str, didascalia: str, markup: dict | None = None) -> bool:
    campi = dict(chat_id=CHAT_ID, photo=foto, caption=didascalia, parse_mode="HTML")
    if markup:
        campi["reply_markup"] = json.dumps(markup)
    try:
        telegram("sendPhoto", **campi)
        return True
    except urllib.error.HTTPError:
        return False


def tastiera() -> dict:
    return {"keyboard": [
        [{"text": "➕ Nuova ricerca"}, {"text": "📋 Le mie ricerche"}],
        [{"text": "📊 Stato"}, {"text": "❓ Aiuto"}]],
        "resize_keyboard": True,
        "is_persistent": True}


def menu_principale() -> dict:
    return {"inline_keyboard": [
        [{"text": "➕ Nuova ricerca", "callback_data": "nuova"},
         {"text": "🔎 Le mie ricerche", "callback_data": "lista"}],
        [{"text": "❓ Come funziona", "callback_data": "aiuto"}]]}


# --------------------------------------------------------------------------
# stato
# --------------------------------------------------------------------------

def stato_vuoto() -> dict:
    return {"watches": [], "seen": {}, "last_update_id": 0, "mod": None}


def carica_stato() -> dict:
    if not os.path.exists(STATO):
        return stato_vuoto()
    try:
        with open(STATO, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, json.JSONDecodeError):
        return stato_vuoto()
    base = stato_vuoto()
    base.update({k: s.get(k, base[k]) for k in base})
    for w in base["watches"]:
        if w.get("solo_italia") and not w.get("paese"):
            w["paese"] = "it"
    return base


def salva_stato(s: dict) -> None:
    with open(STATO, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2, sort_keys=True)


def chiave(w: dict) -> str:
    return "{}|{}|{}|{}|{}|{}".format(w["q"].strip().lower(), w.get("min") or "",
                                      w.get("max") or "", ",".join(w.get("stati") or []),
                                      paese_di(w), ",".join(w.get("escludi") or []))


# --------------------------------------------------------------------------
# Vinted
# --------------------------------------------------------------------------

CARD_RE = re.compile(r'data-testid="product-item-id-(\d+)--description-title"')


def prezzo_num(txt: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d{1,2})?)", (txt or "").replace("\u00a0", " "))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


RE_TITOLO_ATTR = re.compile(r'(?:alt|title)="([^"]{3,200})"')


def titolo_pulito(blocco: str) -> str:
    """Dal testo alternativo della card: toglie brand, condizioni, taglia e prezzi."""
    for m in RE_TITOLO_ATTR.finditer(blocco):
        testo_attr = html.unescape(m.group(1))
        pezzi = [x.strip() for x in re.split(r",\s*", testo_attr) if x.strip()]
        tenuti = []
        for t in pezzi:
            if re.match(r"^(Brand|Condizioni|Taglia|Stato|Colore|Materiale)\s*:", t, re.I):
                break
            if re.fullmatch(r"[\d.,]+\s*€", t):
                break
            tenuti.append(t)
        if tenuti:
            return ", ".join(tenuti)
    return ""


def estrai_annunci(pagina: str) -> list[dict]:
    """Legge la griglia risultati e restituisce gli annunci trovati."""
    annunci, visti = [], set()
    for pezzo in pagina.split('data-testid="grid-item"')[1:]:
        pm = re.search(r'data-testid="product-item-id-(\d+)"', pezzo)
        if not pm:
            continue
        pid = pm.group(1)
        if pid in visti:
            continue
        visti.add(pid)

        def testo(marker: str) -> str:
            mm = re.search(r'data-testid="product-item-id-%s--%s"[^>]*>(.*?)</' % (pid, marker), pezzo, re.S)
            if not mm:
                return ""
            return html.unescape(re.sub(r"<[^>]+>", "", mm.group(1))).strip()

        img = ""
        im = re.search(r'<img src="([^"]+)"', pezzo)
        if im:
            img = html.unescape(im.group(1))

        link = ""
        lm = re.search(r'href="(/items/[^"]+)"', pezzo)
        if lm:
            link = "https://{}{}".format(DOMINIO, html.unescape(lm.group(1)).split("?")[0])

        annunci.append({
            "id": pid,
            "titolo": titolo_pulito(pezzo) or testo("description-title") or "(senza titolo)",
            "prezzo": testo("price-text"),
            "prezzo_num": prezzo_num(testo("price-text")),
            "dettagli": testo("description-subtitle"),
            "img": img,
            "link": link,
        })
    return annunci


def dettagli_annuncio(link: str) -> dict:
    """Apre l'annuncio e legge venditore, valutazione e descrizione."""
    dati = {"venditore": "", "stelle": "", "descrizione": ""}
    if not link:
        return dati
    try:
        pagina = _apri(link, referer="https://{}/".format(DOMINIO))
    except (urllib.error.URLError, OSError):
        return dati
    m = re.search(r'data-testid="profile-username">([^<]+)<', pagina)
    if m:
        dati["venditore"] = html.unescape(m.group(1)).strip()
    m = re.search(r"valutazione di ([\d,.]+) su 5", pagina)
    if m:
        dati["stelle"] = m.group(1).replace(".", ",")
    m = re.search(r'og:description" content="([^"]*)"', pagina)
    if m:
        testo = html.unescape(m.group(1))
        testo = re.sub(r"\s+", " ", testo).strip()
        dati["descrizione"] = testo[:220]
    return dati


def cerca(w: dict) -> list[dict]:
    params = {
        "search_text": w["q"],
        "order": "newest_first",
        "currency": "EUR",
        "page": "1",
        "per_page": "96",
    }
    if w.get("max"):
        params["price_to"] = str(w["max"])
    if w.get("min"):
        params["price_from"] = str(w["min"])
    if w.get("stati"):
        params["status_ids"] = ",".join(str(x) for x in w["stati"])
    url = "https://{}/catalog?{}".format(DOMINIO, urllib.parse.urlencode(params))
    pagina = _apri(url, referer="https://{}/".format(DOMINIO))
    return estrai_annunci(pagina)


# --------------------------------------------------------------------------
# comandi Telegram
# --------------------------------------------------------------------------

PAROLE_STATO = {
    "nuovo": ["6", "1"],
    "ottimo": ["2"],
    "buono": ["3"],
    "discreto": ["4"],
}

BENVENUTO = (
    "Ciao! 👋 Sono il tuo tracker Vinted.\n\n"
    "Sorveglio quello che cerchi e ti scrivo appena esce un annuncio, con foto, prezzo e link.\n\n"
    "Per iniziare tocca ➕ e scrivimi cosa cerchi, per esempio:\n"
    "/aggiungi harry potter blu-ray 15\n"
    "Il numero finale è il prezzo massimo: puoi anche fare 5-20 per un intervallo."
)

AIUTO = (
    "Comandi:\n"
    "/aggiungi parole… [prezzo o 5-20] [nuovo/ottimo/buono] — es. /aggiungi harry potter blu-ray 5-20 nuovo\n"
    "/lista — ricerche attive (✏️ per modificarle)\n"
    "/modifica numero prezzo — es. /modifica 1 30 oppure /modifica 1 10-30\n"
    "/modifica numero nazione — italia, francia, spagna, germania… oppure tutte\n"
    "/togli numero — toglie una ricerca\n"
    "/controlla — controllo subito\n"
    "/aiuto — questo messaggio"
)


NOMI_STATO = {"6": "nuovo con cartellino", "1": "nuovo senza cartellino", "2": "ottimo", "3": "buono", "4": "discreto"}

# paese = lingua in cui e' scritto l'annuncio (Vinted non espone il paese vero)
PAESI = {
    "italia": "it", "italiano": "it", "it": "it",
    "francia": "fr", "francese": "fr", "fr": "fr",
    "spagna": "es", "spagnolo": "es", "es": "es",
    "germania": "de", "tedesco": "de", "de": "de",
    "portogallo": "pt", "portoghese": "pt", "pt": "pt",
    "olanda": "nl", "paesi bassi": "nl", "olandese": "nl", "nl": "nl",
    "polonia": "pl", "polacco": "pl", "pl": "pl",
    "belgio": "nl", "belga": "nl",
    "regno unito": "en", "inghilterra": "en", "inglese": "en", "uk": "en", "gb": "en", "en": "en",
}
NOMI_PAESE = {"it": "Italia", "fr": "Francia", "es": "Spagna", "de": "Germania",
              "pt": "Portogallo", "nl": "Olanda", "pl": "Polonia", "en": "Regno Unito"}


def paese_da(testo: str) -> str | None:
    """'italia' -> 'it'. 'tutte'/'nessuna'/'no' -> '' (nessun filtro). None se non capito."""
    t = " ".join((testo or "").lower().split())
    if not t:
        return None
    if t in ("tutte", "tutti", "nessuna", "nessuno", "no", "any", "mondo"):
        return ""
    return PAESI.get(t, t if t in NOMI_PAESE else None)


def paese_di(w: dict) -> str:
    """Paese filtrato per una ricerca (compatibile con il vecchio solo_italia)."""
    if w.get("paese"):
        return w["paese"]
    return "it" if w.get("solo_italia") else ""


def pezzi_prezzo(w: dict) -> str:
    if w.get("min") and w.get("max"):
        return "{:.0f}-{:.0f} €".format(float(w["min"]), float(w["max"]))
    if w.get("max"):
        return "max {:.0f} €".format(float(w["max"]))
    if w.get("min"):
        return "da {:.0f} €".format(float(w["min"]))
    return ""


def descrivi(w: dict, i: int) -> str:
    extra = [pezzi_prezzo(w)]
    if w.get("stati"):
        stati = [str(x) for x in w["stati"]]
        if sorted(stati) == ["1", "6"]:
            extra.append("nuovo")
        else:
            extra.append(" · ".join(NOMI_STATO.get(x, x) for x in stati))
    testo = " ".join(e for e in extra if e)
    coda = (" · " + testo) if testo else ""
    if w.get("escludi"):
        coda += " · senza " + ", ".join(w["escludi"])
    if paese_di(w):
        coda += " · solo " + NOMI_PAESE.get(paese_di(w), paese_di(w).upper())
    return "{}. {}{}".format(i, w["q"], coda)


def aggiungi_da_testo(s: dict, testo: str) -> dict | None:
    """Crea una ricerca da testo libero: parole [prezzo|min-max] [condizione]."""
    argomenti = testo.split()
    prezzo_min = prezzo_max = None
    stati: list[str] = []
    solo_italia = False
    escludi: list[str] = []
    if argomenti and argomenti[-1].lower() in ("italia", "italiano"):
        solo_italia = True
        argomenti = argomenti[:-1]
    while argomenti:
        ultimo = argomenti[-1].lower()
        if ultimo.startswith("-") and len(ultimo) > 2:
            escludi.insert(0, ultimo[1:])
            argomenti = argomenti[:-1]
            continue
        if ultimo in PAROLE_STATO and ultimo not in [x.lower() for x in argomenti[:-1]]:
            stati.extend(PAROLE_STATO[ultimo])
            argomenti = argomenti[:-1]
            continue
        m = re.fullmatch(r"(\d+(?:[.,]\d+)?)(?:-|–)(\d+(?:[.,]\d+)?)", ultimo)
        if m:
            prezzo_min = float(m.group(1).replace(",", "."))
            prezzo_max = float(m.group(2).replace(",", "."))
            argomenti = argomenti[:-1]
            continue
        if re.fullmatch(r"\d+(?:[.,]\d{1,2})?", ultimo):
            prezzo_max = float(ultimo.replace(",", "."))
            argomenti = argomenti[:-1]
            continue
        break
    query = " ".join(argomenti).strip()
    if not query:
        return None
    nuova = {"q": query, "max": prezzo_max, "min": prezzo_min, "stati": stati or [],
             "paese": "it" if solo_italia else "", "escludi": escludi}
    if any(chiave(w) == chiave(nuova) for w in s["watches"]):
        return nuova
    s["watches"].append(nuova)
    s["seen"].pop(chiave(nuova), None)
    s["ultimo_aggiunto"] = chiave(nuova)
    return nuova


def leggi_prezzo(testo: str):
    """'25' -> (None, 25) ; '10-30' -> (10, 30) ; '10 a 30'/'10 30' idem ; None se non e' un prezzo."""
    t = (testo or "").strip().replace("€", "").replace(" ", " ").strip()
    t = re.sub(r"\s*(?:-|–|a|fino a|to)\s*", "-", t) if re.search(r"\d\s*(?:-|–|a|fino a)\s*\d", t) else t
    num = r"\d+(?:[.,]\d{1,2})?"
    m = re.fullmatch(r"(%s)-(%s)" % (num, num), t)
    if m:
        return (float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", ".")))
    m = re.fullmatch(num, t)
    if m:
        return (None, float(m.group(0).replace(",", ".")))
    return None


def modifica_ricerca(s: dict, i: int, campo: str, valore) -> str:
    """Cambia prezzo o nazione della ricerca numero i (0-based) e riparte da una base pulita."""
    w = s["watches"][i]
    vecchia = chiave(w)
    if campo == "prezzo":
        w["min"], w["max"] = valore
    else:
        w["paese"] = valore or ""
    w.pop("solo_italia", None)
    if chiave(w) != vecchia:
        s["seen"].pop(vecchia, None)
    return "Aggiornata ✅\n" + descrivi(w, i + 1) + "\nRiparto dagli annunci già online, senza raffica."


def menu_modifica(s: dict, i: int) -> None:
    w = s["watches"][i]
    invia("<b>✏️ Modifico la ricerca " + str(i + 1) + "</b>\n" + descrivi(w, i + 1) +
          "\n\nCosa cambio?",
          {"inline_keyboard": [
              [{"text": "💶 Prezzo", "callback_data": "ep:{}".format(i + 1)},
               {"text": "🌍 Nazione", "callback_data": "ecm:{}".format(i + 1)}],
              [{"text": "🗑 Togli questa ricerca", "callback_data": "del:{}".format(i + 1)},
               {"text": "🔙 Le mie ricerche", "callback_data": "lista"}]]})


def menu_paesi(i: int) -> dict:
    righe = []
    for codice, nome in (("it", "🇮🇹 Italia"), ("fr", "🇫🇷 Francia"), ("es", "🇪🇸 Spagna"),
                         ("de", "🇩🇪 Germania"), ("nl", "🇳🇱 Olanda")):
        righe.append({"text": nome, "callback_data": "ec:{}:{}".format(codice, i)})
    return {"inline_keyboard": [
        righe[:3], righe[3:],
        [{"text": "🌍 Tutte le nazioni", "callback_data": "ec:all:{}".format(i)}],
        [{"text": "🔙 Indietro", "callback_data": "edit:{}".format(i)}]]}


def invia_lista(s: dict) -> None:
    if not s["watches"]:
        invia("Nessuna ricerca per ora. Tocca ➕ e dimmi cosa cerchi.", menu_principale())
        return
    righe = "\n".join("• " + descrivi(w, i + 1).split(". ", 1)[-1] for i, w in enumerate(s["watches"]))
    bottoni = []
    for i, w in enumerate(s["watches"][:10]):
        bottoni.append([{"text": "✏️ " + str(i + 1) + ". " + w["q"][:18], "callback_data": "edit:" + str(i + 1)},
                        {"text": "🗑", "callback_data": "del:" + str(i + 1)}])
    invia("<b>🔎 Le tue ricerche</b>\n" + righe +
          "\n\nTocca ✏️ per cambiare prezzo o nazione.", {"inline_keyboard": bottoni})


def gestisci_comandi(s: dict) -> bool:
    """Legge i messaggi nuovi del bot e applica i comandi. True se qualcosa e' cambiato."""
    cambiato = False
    try:
        ris = telegram("getUpdates", offset=s.get("last_update_id") or None, timeout=0)
    except (urllib.error.URLError, RuntimeError) as e:
        print("getUpdates: {}".format(e))
        return False
    for upd in ris.get("result", []):
        s["last_update_id"] = upd["update_id"] + 1
        cq = upd.get("callback_query")
        if cq:
            dati = cq.get("data") or ""
            try:
                telegram("answerCallbackQuery", callback_query_id=cq.get("id"))
            except (urllib.error.URLError, RuntimeError):
                pass
            if dati == "lista":
                invia_lista(s)
            elif dati == "aiuto":
                invia(AIUTO, menu_principale())
            elif dati == "nuova":
                invia("Scrivimi cosa cercare, così:\n/aggiungi harry potter blu-ray 15\n"
                      "Le parole, poi il prezzo massimo (o un intervallo 5-20).")
            elif dati.startswith("edit:") and dati[5:].isdigit():
                i = int(dati[5:]) - 1
                if 0 <= i < len(s["watches"]):
                    menu_modifica(s, i)
                else:
                    invia("Quella ricerca non c'è più.", menu_principale())
            elif dati.startswith("ep:") and dati[3:].isdigit():
                i = int(dati[3:]) - 1
                if 0 <= i < len(s["watches"]):
                    s["mod"] = {"i": i + 1, "campo": "prezzo", "ts": time.time()}
                    cambiato = True
                    invia("💶 Nuovo prezzo per «{}»\nMandami un numero (es. 30) o un intervallo (10-30).\n"
                          "/annulla per lasciare com'è.".format(s["watches"][i]["q"]))
                else:
                    invia("Quella ricerca non c'è più.", menu_principale())
            elif dati.startswith("ecm:") and dati[4:].isdigit():
                i = int(dati[4:]) - 1
                if 0 <= i < len(s["watches"]):
                    s["mod"] = {"i": i + 1, "campo": "paese", "ts": time.time()}
                    cambiato = True
                    invia("🌍 Da quale nazione vuoi gli annunci per «{}»?\n"
                          "Tocca un bottone o scrivimi il nome (italia, francia, spagna, germania…).".format(
                              s["watches"][i]["q"]), menu_paesi(i + 1))
                else:
                    invia("Quella ricerca non c'è più.", menu_principale())
            elif dati.startswith("ec:"):
                resto = dati[3:].split(":")
                if len(resto) == 2 and resto[1].isdigit():
                    i = int(resto[1]) - 1
                    if 0 <= i < len(s["watches"]):
                        codice = "" if resto[0] == "all" else resto[0]
                        invia(modifica_ricerca(s, i, "paese", codice))
                        cambiato = True
                        invia_lista(s)
                    else:
                        invia("Quella ricerca non c'è più.", menu_principale())
            elif dati.startswith("del:") and dati[4:].isdigit():
                i = int(dati[4:]) - 1
                if 0 <= i < len(s["watches"]):
                    tolta = s["watches"].pop(i)
                    s["seen"].pop(chiave(tolta), None)
                    cambiato = True
                    invia("Tolta ❌ " + tolta["q"])
                    invia_lista(s)
                else:
                    invia("Quella ricerca non c'è più.", menu_principale())
            continue
        msg = upd.get("message") or upd.get("edited_message") or {}
        testo = (msg.get("text") or "").strip()
        chat = str((msg.get("chat") or {}).get("id") or "")
        if not testo or (chat and chat != CHAT_ID):
            continue
        parti = testo.split()
        cmd = parti[0].split("@")[0].lower()
        argomenti = parti[1:]

        tasto = testo.lower()
        if "nuova ricerca" in tasto:
            cmd, argomenti = "/nuova", []
        elif "le mie ricerche" in tasto:
            cmd = "/lista"
        elif "stato" in tasto and len(parti) <= 2:
            cmd = "/stato"
        elif "aiuto" in tasto and len(parti) <= 2:
            cmd = "/aiuto"

        if s.get("mod") and cmd.startswith("/") and cmd not in ("/annulla",):
            s["mod"] = None
            cambiato = True

        # scaduto? dopo 10 minuti un testo libero torna a essere una nuova ricerca
        if s.get("mod") and time.time() - float(s["mod"].get("ts") or 0) > 600:
            s["mod"] = None
            cambiato = True

        # sto aspettando il nuovo prezzo o la nuova nazione di una ricerca
        if s.get("mod") and not cmd.startswith("/"):
            mod = s["mod"]
            i = int(mod.get("i") or 0) - 1
            if not (0 <= i < len(s["watches"])):
                s["mod"] = None
                cambiato = True
                invia("Quella ricerca non c'è più, vedi /lista.", menu_principale())
            elif mod.get("campo") == "prezzo":
                p = leggi_prezzo(testo)
                if p is None:
                    invia("Non ho capito il prezzo: mandami un numero (es. 30) o un intervallo (10-30).")
                else:
                    s["mod"] = None
                    cambiato = True
                    invia(modifica_ricerca(s, i, "prezzo", p))
            else:
                codice = paese_da(testo)
                if codice is None:
                    invia("Nazione non riconosciuta: scrivi italia, francia, spagna, germania… oppure tutte.",
                          menu_paesi(i + 1))
                else:
                    s["mod"] = None
                    cambiato = True
                    invia(modifica_ricerca(s, i, "paese", codice))
            continue

        if cmd == "/start":
            invia(BENVENUTO, tastiera())
        elif cmd in ("/nuova",):
            invia("🔎 Cosa vuoi tenere d'occhio?\n"
                  "Scrivimi il nome come lo cercheresti su Vinted, con il prezzo massimo:\n\n"
                  "/aggiungi ps5 250\n"
                  "/aggiungi nintendo switch oled 200\n\n"
                  "Per un intervallo: /aggiungi funko pop naruto 10-30")
            invia("Se cambi idea: /annulla")
        elif cmd in ("/annulla",):
            if s.get("mod"):
                s["mod"] = None
                cambiato = True
                invia("Ok, lascio la ricerca com'è 🙂", tastiera())
                continue
            ultima = s.pop("ultimo_aggiunto", None)
            if ultima:
                for i, w in enumerate(s["watches"]):
                    if chiave(w) == ultima:
                        tolta = s["watches"].pop(i)
                        s["seen"].pop(chiave(tolta), None)
                        cambiato = True
                        invia("Tolta: " + tolta["q"], tastiera())
                        break
                else:
                    invia("Niente da annullare 🙂", tastiera())
            else:
                invia("Niente da annullare 🙂", tastiera())
        elif cmd in ("/stato",):
            quante = len(s["watches"])
            ultimo = s.get("ultimo_giro") or "—"
            invia("<b>📊 Stato</b>\nRicerche attive: {}\nUltimo controllo: {}\n"
                  "Controllo automatico: ogni 30 minuti.".format(quante, html.escape(ultimo)), menu_principale())
        elif cmd in ("/aiuto", "/help"):
            invia(AIUTO, menu_principale())
        elif cmd in ("/aggiungi", "/add"):
            if not argomenti:
                invia("Scrivimi cosa cercare: /aggiungi parole… [prezzo o 5-20] [nuovo/ottimo/buono]")
                continue
            gia = len(s["watches"])
            nuova = aggiungi_da_testo(s, " ".join(argomenti))
            if nuova is None:
                invia("Scrivimi cosa cercare: /aggiungi parole… [prezzo o 5-20]")
                continue
            if len(s["watches"]) == gia:
                invia("Questa ricerca c'è già: " + nuova["q"])
                continue
            cambiato = True
            invia("Aggiunta ✅\n" + descrivi(nuova, len(s["watches"])) +
                  "\nTi avviso appena esce qualcosa di nuovo. Gli annunci già online li ignoro.")
        elif cmd in ("/modifica", "/mod", "/cambia"):
            if not argomenti or not argomenti[0].isdigit():
                invia("Quale ricerca cambio? /modifica numero, poi prezzo o nazione.\n"
                      "Es.: /modifica 1 30   ·   /modifica 1 10-30   ·   /modifica 1 italia   ·   /modifica 1 tutte\n"
                      "Vedi i numeri con /lista.")
                continue
            i = int(argomenti[0]) - 1
            if not (0 <= i < len(s["watches"])):
                invia("Numero non valido, vedi /lista.")
                continue
            resto = " ".join(argomenti[1:]).strip()
            if not resto:
                menu_modifica(s, i)
                continue
            p = leggi_prezzo(resto)
            if p is not None:
                cambiato = True
                invia(modifica_ricerca(s, i, "prezzo", p))
                continue
            codice = paese_da(resto)
            if codice is None:
                invia("Non ho capito: dopo il numero scrivi il prezzo (30 o 10-30) "
                      "oppure la nazione (italia, francia, spagna… o tutte).")
                continue
            cambiato = True
            invia(modifica_ricerca(s, i, "paese", codice))
        elif cmd in ("/lista", "/list"):
            invia_lista(s)
        elif cmd in ("/togli", "/rimuovi", "/del"):
            if not argomenti or not argomenti[0].isdigit():
                invia("Quale tolgo? Mandami /togli numero (vedi /lista).")
                continue
            i = int(argomenti[0]) - 1
            if 0 <= i < len(s["watches"]):
                tolta = s["watches"].pop(i)
                s["seen"].pop(chiave(tolta), None)
                cambiato = True
                invia("Tolta: " + tolta["q"])
            else:
                invia("Numero non valido, vedi /lista.")
        elif cmd in ("/controlla", "/check"):
            cambiato = True
            s["_forza"] = True
        elif not testo.startswith("/"):
            gia = len(s["watches"])
            nuova = aggiungi_da_testo(s, testo)
            if nuova is None:
                invia("Scrivimi cosa cercare: per esempio \"xbox x 200-280\".")
                continue
            if len(s["watches"]) == gia:
                invia("Questa ricerca c'è già: " + nuova["q"])
                continue
            cambiato = True
            invia("Ho messo d'occhio 👀\n" + descrivi(nuova, len(s["watches"])) +
                  "\nSe non va bene: /annulla")
        else:
            invia("Comando non riconosciuto.\n\n" + AIUTO)
    return cambiato


# --------------------------------------------------------------------------
# giro di controllo
# --------------------------------------------------------------------------

BANDIERE = {
    "it": "🇮🇹", "fr": "🇫🇷", "es": "🇪🇸", "pt": "🇵🇹", "en": "🇬🇧", "de": "🇩🇪",
    "nl": "🇳🇱", "pl": "🇵🇱", "lt": "🇱🇹", "ro": "🇷🇴", "cs": "🇨🇿", "hu": "🇭🇺",
    "sv": "🇸🇪", "da": "🇩🇰", "fi": "🇫🇮", "el": "🇬🇷", "bg": "🇧🇬", "hr": "🇭🇷",
    "sk": "🇸🇰", "sl": "🇸🇮", "et": "🇪🇪", "lv": "🇱🇻", "uk": "🇺🇦",
}


def lingua(testo: str) -> str:
    """Lingua probabile del testo (codice a due lettere) o stringa vuota se non è chiaro."""
    if not detect_langs or not testo or len(testo) < 20:
        return ""
    try:
        migliore = detect_langs(testo)[0]
        return migliore.lang if migliore.prob >= 0.75 else ""
    except Exception:
        return ""


def normalizza(testo: str) -> str:
    testo = (testo or "").lower()
    for a, b in (("à", "a"), ("á", "a"), ("è", "e"), ("é", "e"), ("ì", "i"), ("í", "i"),
                 ("ò", "o"), ("ó", "o"), ("ù", "u"), ("ú", "u"), ("-", " "), ("_", " ")):
        testo = testo.replace(a, b)
    return re.sub(r"[^a-z0-9 ]+", " ", testo)


def titolo_vale(titolo: str, w: dict) -> bool:
    """False solo se il titolo contiene una delle parole escluse (-parola)."""
    t = " " + re.sub(r"\s+", " ", normalizza(titolo)) + " "
    for parola in w.get("escludi") or []:
        pulita = normalizza(parola).strip()
        if pulita and " {} ".format(pulita) in t:
            return False
    return True


def da_scartare(*testi: str) -> bool:
    """True se uno dei testi contiene una parola da evitare (paypal, bonifico, consegna a mano...)."""
    for t in testi:
        basso = (t or "").lower()
        if any(p in basso for p in PAROLE_VIETATE):
            return True
    return False


def invia_nuovi(w: dict, nuovi: list[dict]) -> int:
    """Manda una card con foto per ogni annuncio nuovo. Ritorna quanti messaggi ha inviato."""
    def dentro(n: dict) -> bool:
        p = n.get("prezzo_num")
        if p is None:
            return True
        if w.get("max") and p > float(w["max"]):
            return False
        if w.get("min") and p < float(w["min"]):
            return False
        return True

    nuovi = [n for n in nuovi if dentro(n)]
    fuori = [n for n in nuovi if not titolo_vale(n["titolo"], w)]
    if fuori:
        for n in fuori[:3]:
            print("scartato (titolo): {}".format(n["titolo"]))
    nuovi = [n for n in nuovi if titolo_vale(n["titolo"], w)]
    if not nuovi:
        return 0

    inviati = 0
    for n in nuovi[:MAX_ANNUNCI]:
        riga = "<b>{}</b>".format(html.escape(n["titolo"]))
        if n.get("prezzo"):
            riga += "\n💶 <b>{}</b>".format(html.escape(n["prezzo"]))
        if n.get("dettagli"):
            riga += " · {}".format(html.escape(n["dettagli"]))
        extra = dettagli_annuncio(n.get("link", ""))
        if da_scartare(extra.get("descrizione", ""), extra.get("venditore", "")):
            print("scartato (parole vietate): {}".format(n["titolo"]))
            continue
        lang = lingua("{} {}".format(n["titolo"], extra.get("descrizione", "")))
        if paese_di(w) and lang and lang != paese_di(w):
            print("scartato (lingua {}, cerco {}): {}".format(lang, paese_di(w), n["titolo"]))
            continue
        if extra.get("venditore"):
            riga += "\n👤 {}".format(html.escape(extra["venditore"]))
            if extra.get("stelle"):
                riga += " · ⭐ {}/5".format(html.escape(extra["stelle"]))
        if extra.get("descrizione"):
            riga += "\n\n<i>{}</i>".format(html.escape(extra["descrizione"]))
        if lang:
            riga += "\n🌍 {}".format(BANDIERE.get(lang, lang.upper()))
        riga += "\n\n🔎 {}".format(html.escape(w["q"]))
        bottoni = {"inline_keyboard": [[{"text": "🔗 Annuncio su Vinted", "url": n["link"]}]]} if n.get("link") else None
        ok = False
        if n.get("img"):
            ok = invia_foto(n["img"], riga, bottoni)
        if not ok:
            invia(riga + ("\n" + n["link"] if n.get("link") else ""), bottoni)
        inviati += 1
        time.sleep(1)
    if len(nuovi) > MAX_ANNUNCI:
        invia("…e altri {} nuovi per questa ricerca.".format(len(nuovi) - MAX_ANNUNCI))
        inviati += 1
    return inviati


def pota(s: dict) -> None:
    # via le chiavi di ricerche che non esistono piu' (rinominate o modificate)
    attuali = {chiave(w) for w in s["watches"]}
    for k in list(s["seen"].keys()):
        if k not in attuali:
            s["seen"].pop(k, None)
    limite = date.today().toordinal() - TTL_GIORNI
    for k in list(s["seen"].keys()):
        voci = s["seen"][k]
        if isinstance(voci, list):  # vecchio formato
            voci = {i: date.today().isoformat() for i in voci}
        nuove = {i: d for i, d in voci.items()
                 if _giorno(d) is None or _giorno(d) >= limite}
        if len(nuove) > MAX_VISTI:
            for i in sorted(nuove, key=lambda x: nuove[x])[: len(nuove) - MAX_VISTI]:
                nuove.pop(i, None)
        s["seen"][k] = nuove


def _giorno(iso: str) -> int | None:
    try:
        return date.fromisoformat(iso).toordinal()
    except (ValueError, TypeError):
        return None


def giro(dry: bool = False) -> int:
    s = carica_stato()
    if not dry:
        gestisci_comandi(s)
    inviati = 0
    for w in list(s["watches"]):
        k = chiave(w)
        try:
            annunci = cerca(w)
        except (urllib.error.URLError, OSError) as e:
            print("errore su {}: {}".format(w["q"], e))
            time.sleep(3)
            continue
        visti = s["seen"].get(k) or {}
        if isinstance(visti, list):
            visti = {i: date.today().isoformat() for i in visti}
        if not visti:
            # primo giro: prendo quello che c'e' gia', senza inondare la chat
            s["seen"][k] = {a["id"]: date.today().isoformat() for a in annunci}
            print("[{}] base: {} annunci".format(w["q"], len(annunci)))
            time.sleep(2)
            continue
        nuovi = [a for a in annunci if a["id"] not in visti]
        for a in annunci:
            visti.setdefault(a["id"], date.today().isoformat())
        s["seen"][k] = visti
        if nuovi:
            if dry:
                for n in nuovi:
                    print("---\n{} | {} | {} | {}".format(n["titolo"], n["prezzo"], n["dettagli"], n["link"]))
            else:
                inviati += invia_nuovi(w, nuovi)
        time.sleep(2)
    pota(s)
    s["ultimo_giro"] = datetime.now().isoformat(timespec="minutes")
    s.pop("_forza", None)
    if not dry:
        salva_stato(s)
    print("giro finito: {} messaggi".format(inviati))
    return 0


# --------------------------------------------------------------------------
# riga di comando
# --------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Bot Vinted per Telegram")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="un giro completo (default)")
    g.add_argument("--dry-run", action="store_true", help="prova senza inviare e senza salvare")
    g.add_argument("--test", action="store_true", help="invia un messaggio di prova")
    g.add_argument("--aggiungi", nargs="+", metavar="VOCE", help="aggiunge una ricerca")
    g.add_argument("--lista", action="store_true", help="elenca le ricerche")
    g.add_argument("--togli", metavar="N", help="toglie la ricerca numero N")
    a = p.parse_args(argv)

    if a.test:
        invia("Prova del bot Vinted ✅ Se leggi questo, funziona.")
        return 0
    if a.aggiungi:
        argomenti = list(a.aggiungi)
        prezzo = None
        if re.fullmatch(r"\d+(?:[.,]\d{1,2})?", argomenti[-1]) and len(argomenti) > 1:
            prezzo = float(argomenti.pop().replace(",", "."))
        s = carica_stato()
        s["watches"].append({"q": " ".join(argomenti), "max": prezzo})
        salva_stato(s)
        print("aggiunta: {} (max {})".format(" ".join(argomenti), prezzo))
        return 0
    if a.lista:
        s = carica_stato()
        if not s["watches"]:
            print("nessuna ricerca")
        for i, w in enumerate(s["watches"]):
            print(descrivi(w, i + 1))
        return 0
    if a.togli:
        s = carica_stato()
        i = int(a.togli) - 1
        if 0 <= i < len(s["watches"]):
            print("tolta: " + s["watches"].pop(i)["q"])
            salva_stato(s)
        else:
            print("numero non valido")
        return 0
    return giro(dry=a.dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
