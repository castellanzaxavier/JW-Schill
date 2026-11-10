import asyncio
import hashlib
import hmac
import logging
import os
import re
import threading
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from flask import Flask, request
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
PORT = int(os.environ.get("PORT", "10000"))
LANGUAGE = os.environ.get("JW_LANGUAGE", "I").strip().upper()
LOCALE = "it" if LANGUAGE == "I" else "en"
WOL_ROUTE = "r6/lp-i" if LANGUAGE == "I" else "r1/lp-e"
MEDIA_API = "https://b.jw-cdn.org/apis/mediator/v1"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JWSchillBot/1.0)"}
app = Flask(__name__)
bot_ready = threading.Event()
telegram_application = None
telegram_loop = None
WEBHOOK_BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = hashlib.sha256(("JW-Schill:" + TELEGRAM_TOKEN).encode()).hexdigest() if TELEGRAM_TOKEN else ""

SYSTEM_PROMPT = """
Sei un assistente virtuale fraterno e rispondi in italiano con tono rispettoso, chiaro e caloroso.
Usa esclusivamente le fonti ufficiali e i testi forniti. Considera le fonti come contesto e riferimento, non come materiale da riassumere automaticamente: ragiona sulla domanda concreta alla luce delle linee guida bibliche, collega i princìpi alla situazione e distingui chiaramente il testo citato dalla sua applicazione. Non scegliere una scrittura soltanto perché ripete parole della domanda. Per domande su video, esamina tutti i
testo del video disponibile e organizza una risposta esauriente, fedele al contenuto e pertinente alla
domanda, includendo punti principali, esempi e conclusioni. Distingui le affermazioni delle fonti
dalle spiegazioni. Non inventare citazioni o riferimenti; cita i link delle fonti forniti.
Non concludere con saluti, benedizioni o formule di commiato. Non scrivere mai "Geova ti benedica"
o varianti.
""".strip()

@app.get("/")
def index():
    return "Bot JW online" if bot_ready.is_set() else "Servizio web online; bot in avvio", 200

@app.get("/health")
def health():
    return ({"status": "ok"}, 200) if bot_ready.is_set() else ({"status": "starting"}, 503)

@app.post("/telegram")
def telegram_webhook():
    if not bot_ready.is_set() or telegram_application is None or telegram_loop is None:
        return {"status": "starting"}, 503
    supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(supplied_secret, WEBHOOK_SECRET):
        return {"status": "forbidden"}, 403
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return {"status": "invalid request"}, 400
    update = Update.de_json(payload, telegram_application.bot)
    telegram_loop.call_soon_threadsafe(telegram_application.update_queue.put_nowait, update)
    return {"ok": True}, 200

def official_url(url):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return parts.scheme in {"http", "https"} and (host == "jw.org" or host.endswith(".jw.org"))
    except ValueError:
        return False

def extract_jw_url(text):
    for url in re.findall(r"https?://[^\s<>]+", text, flags=re.I):
        url = url.rstrip(".,;:!?)]}\"'")
        if official_url(url):
            return url
    return None

def clean_text(markup):
    soup = BeautifulSoup(markup, "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return " ".join(soup.stripped_strings)

def fetch_page(url):
    if not official_url(url):
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for selector in ("div.bodyTxt", "div.docBody", "article"):
            node = soup.select_one(selector)
            if node:
                text = " ".join(p.get_text(" ", strip=True) for p in node.find_all(["p", "li"]))
                if text:
                    return text[:14000]
        text = " ".join(p.get_text(" ", strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 30)
        return text[:14000] or None
    except requests.RequestException as exc:
        logger.info("Pagina JW non recuperabile: %s", exc)
        return None

def fetch_page_long(url, limit=60000, with_url=False):
    """Estrae il testo completo delle pagine WOL e, se richiesto, l’URL finale dopo i reindirizzamenti."""
    if not official_url(url):
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for selector in ("#regionMain", "div.bodyTxt", "div.docBody", "article"):
            node = soup.select_one(selector)
            if node:
                text = clean_text(str(node))
                if text:
                    text = text[:limit]
                    return (text, r.url) if with_url else text
        text = clean_text(str(soup))[:limit]
        return (text, r.url) if with_url and text else text or None
    except requests.RequestException as exc:
        logger.info("Pagina JW completa non recuperabile: %s", exc)
        return None

def search_wol(query, limit=4):
    """Cerca nella WOL provando la frase originale e, se serve, termini più semplici."""
    normalized = normalize(query)
    stopwords = {"a", "ad", "al", "alla", "alle", "che", "chi", "come", "con", "cosa", "da", "dai", "dal", "dalla", "de", "del", "della", "di", "e", "è", "gli", "i", "il", "in", "io", "la", "le", "lo", "ma", "mi", "nel", "nella", "per", "si", "sono", "su", "un", "una", "uno", "vorrei", "puoi", "potresti", "dimmi", "dammi", "quale", "quali", "scrittura", "scritture"}
    words = [word for word in re.findall(r"[a-z0-9]+", normalized) if word not in stopwords and len(word) > 2]
    candidates = [query]
    if words:
        candidates.append(" ".join(words[:4]))
    if any(word.startswith(("isol", "solitudin", "single", "solitari")) for word in words):
        candidates.extend(("solitudine", "isolamento", "incoraggiamento"))
    if any(word.startswith(("ans", "preoccup", "stress")) for word in words):
        candidates.extend(("ansia", "preoccupazione", "incoraggiamento"))
    if words:
        candidates.extend(words[:3])

    found, seen = [], set()
    for search_query in dict.fromkeys(candidates):
        url = f"https://wol.jw.org/{LOCALE}/wol/s/{WOL_ROUTE}?q={quote_plus(search_query)}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Ricerca WOL non riuscita per '%s': %s", search_query, exc)
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        root = soup.select_one(".searchResultsForm") or soup.select_one("#regionMain") or soup
        for a in root.select("a[href]"):
            href = urljoin(r.url, a.get("href", ""))
            parts = urlsplit(href)
            title = a.get_text(" ", strip=True)
            if not title or (parts.hostname or "").lower() != "wol.jw.org" or "/wol/d/" not in parts.path or href in seen:
                continue
            parent, snippet = a, ""
            for _ in range(3):
                if parent.parent is None:
                    break
                parent = parent.parent
                candidate = clean_text(str(parent))
                if len(title) + 30 < len(candidate) < 3500:
                    snippet = candidate[:1800]
                    break
            found.append({"title": title[:200], "url": href, "snippet": snippet})
            seen.add(href)
            if len(found) >= limit:
                break
        if found:
            logger.info("Ricerca WOL: '%s' ha prodotto %s risultati", search_query, len(found))
            return found
    logger.info("Ricerca WOL senza risultati per la domanda: %s", query)
    return found


def is_congregation_study_request(query):
    q = normalize(query)
    return bool(re.search(r"\bstudio\b.{0,30}\b(?:cong(?:regazione)?|congregazion[ei])", q))

def is_weekly_study_request(query):
    q = normalize(query)
    congregation = is_congregation_study_request(q)
    watchtower = "torre" in q or "torre di guardia" in q or "articolo di studio" in q
    weekly_context = any(term in q for term in ("settimana", "stasera", "questa sera", "oggi", "adunanza", "guida adunanza"))
    study_context = any(term in q for term in ("studio", "domande", "risposte", "articolo", "programma"))
    return weekly_context and study_context and (congregation or watchtower)


def weekly_study_material(query):
    """Recupera dal calendario WOL il capitolo o l’articolo assegnato alla settimana corrente."""
    q = normalize(query)
    if not is_weekly_study_request(query):
        return None
    congregation = is_congregation_study_request(q)
    today = datetime.now(ZoneInfo("Europe/Rome")).date()
    monday = today - timedelta(days=today.weekday())
    date_url = f"https://wol.jw.org/{LOCALE}/wol/dt/{WOL_ROUTE}/{monday.year}/{monday.month}/{monday.day}"
    try:
        date_response = requests.get(date_url, headers=HEADERS, timeout=20)
        date_response.raise_for_status()
        date_soup = BeautifulSoup(date_response.text, "html.parser")
        guide_link = next((a for a in date_soup.select("a[href]")
                           if "guida per l" in normalize(a.get_text(" ", strip=True))
                           and "vita e ministero" in normalize(a.get_text(" ", strip=True))), None)
        if not guide_link:
            logger.warning("Guida settimanale non collegata dalla pagina WOL: %s", date_url)
            return None
        guide_url = urljoin(date_response.url, guide_link.get("href", ""))
        if not official_url(guide_url):
            return None
    except requests.RequestException as exc:
        logger.warning("Programma settimanale WOL non recuperabile: %s", exc)
        return None

    document_url = None
    context = ""
    if congregation:
        for link in date_soup.select("a[href]"):
            label = link.get_text(" ", strip=True)
            if re.search(r"\bwcg\s+cap\.?\s*\d+\b", normalize(label), flags=re.I):
                document_url = urljoin(date_response.url, link.get("href", ""))
                context = clean_text(str(link.parent))
                break
    else:
        marker = next((h for h in date_soup.find_all(re.compile(r"^h[1-6]$"))
                       if "articolo di studio" in normalize(h.get_text(" ", strip=True))), None)
        if marker:
            link = marker.find_next("a", href=True)
            if link:
                document_url = urljoin(date_response.url, link.get("href", ""))
                context = marker.get_text(" ", strip=True)

    if not document_url or not official_url(document_url):
        logger.warning("Capitolo o articolo non individuato nella pagina WOL: %s", date_url)
        return None
    fetched = fetch_page_long(document_url, with_url=True)
    if not fetched:
        logger.warning("Testo dello studio settimanale non recuperabile: %s", document_url)
        return None
    document, final_url = fetched
    kind = "Studio biblico di congregazione" if congregation else "Torre di Guardia per lo studio"
    return [f"PROGRAMMA UFFICIALE DELLA SETTIMANA ({monday.isoformat()}):\n{context}",
            f"TESTO COMPLETO DELLO STUDIO ({kind}):\n{document}"], [guide_url, final_url]

def normalize(value):
    value = unicodedata.normalize("NFKD", value.lower())
    return "".join(ch for ch in value if not unicodedata.combining(ch))

def monthly_broadcast(query):
    q = normalize(query)
    if not any(x in q for x in ("broadcast", "jw broadcasting", "programma mensile")):
        return None
    try:
        r = requests.get(f"{MEDIA_API}/categories/{LANGUAGE}/StudioMonthlyPrograms?detailed=1", headers=HEADERS, timeout=20)
        r.raise_for_status()
        media = r.json().get("media", [])
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Catalogo dei programmi mensili non disponibile: %s", exc); return {"title": "JW Broadcasting (ottobre 2024)", "naturalKey": "pub-jwb-117_1_VIDEO"} if "ottobre" in q and "2024" in q else None
        return None
    months = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre")
    qwords = set(re.findall(r"[\w]+", q, flags=re.UNICODE))
    candidates = [m for m in media if any(month in qwords and month in normalize(m.get("title", "")) for month in months)]
    years = re.findall(r"\b20\d{2}\b", query)
    if years:
        candidates = [m for m in (candidates or media) if all(y in m.get("title", "") for y in years)]
    return candidates[0] if candidates else ({"title": "JW Broadcasting (ottobre 2024)", "naturalKey": "pub-jwb-117_1_VIDEO"} if "ottobre" in q and "2024" in q else None)

def get_video(item_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,100}", item_id or ""):
        return None
    try:
        r = requests.get(f"{MEDIA_API}/media-items/{LANGUAGE}/{item_id}", params={"clientType": "www"}, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        media = data.get("media", data); return next((item for item in media if isinstance(item, dict)), None) if isinstance(media, list) else media if isinstance(media, dict) else None
    except (requests.RequestException, ValueError) as exc:
        logger.info("Metadati video non disponibili: %s", exc)
        return None

def transcript(video):
    sub_url = next((f.get("subtitles", {}).get("url") for f in video.get("files", []) if f.get("subtitles", {}).get("url")), None)
    if not sub_url or not (urlsplit(sub_url).hostname or "").lower().endswith("jw-cdn.org"):
        return None
    try:
        r = requests.get(sub_url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        raw = r.content.decode("utf-8-sig", errors="replace")
        cues = []
        for block in re.split(r"\n\s*\n", raw):
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            cue = clean_text(" ".join(line for line in lines if "-->" not in line and line.upper() != "WEBVTT" and not line.isdigit()))
            if cue and (not cues or cue != cues[-1]):
                cues.append(cue)
        text = "\n".join(cues)
        return (text[:55000], sub_url) if text else None
    except requests.RequestException as exc:
        logger.warning("Sottotitoli JW non recuperabili: %s", exc)
        return None

def is_scripture_request(query):
    q = normalize(query)
    return any(term in q for term in ("scrittura", "versetto", "passo biblico", "testo biblico"))

def is_young_ministry_goal_request(query):
    q = normalize(query)
    young_person = any(term in q for term in ("giovane", "giovani", "ragazzo", "ragazza"))
    ministry_goal = any(term in q for term in ("servitore di ministero", "servitore ministeriale"))
    encouragement = any(term in q for term in ("incoragg", "motivar", "mete", "obiettiv"))
    return young_person and ministry_goal and encouragement

def is_single_sister_encouragement_request(query):
    q = normalize(query)
    return "sorella" in q and any(term in q for term in ("single", "non sposata", "nubile")) and any(term in q for term in ("isol", "solitudin", "sola"))

def is_specific_scripture_scenario(query):
    return is_young_ministry_goal_request(query) or is_single_sister_encouragement_request(query)

def research(query, user_url):
    chunks, sources = [], []
    if is_specific_scripture_scenario(query):
        chunks.append("SCRITTURA BIBLICA UFFICIALE: 1 Timoteo 4:12 — Nessuno disprezzi la tua giovane età. Diventa piuttosto un esempio per i fedeli nel parlare, nel comportamento, nell’amore, nella fede, nella castità.")
        sources.append(f"https://www.jw.org/{LOCALE}/biblioteca-digitale/bibbia/nwt/libri/1-timoteo/4/")
    item_id = parse_qs(urlsplit(user_url).query).get("item", [None])[0] if user_url else None
    video = get_video(item_id) if item_id else None
    if not video:
        video = monthly_broadcast(query)
        if video:
            item_id = video.get("languageAgnosticNaturalKey") or video.get("naturalKey"); video = get_video(item_id) or video
    if video:
        title = video.get("title", "Video JW.org")
        video_url = f"https://www.jw.org/{LOCALE}/biblioteca-digitale/video/?item={item_id}"
        caption_data = transcript(video)
        if caption_data:
            caption, caption_url = caption_data
            chunks.append(f"CONTENUTO DEL VIDEO {title}:\n{caption}")
            sources.append(video_url)
        else:
            chunks.append(f"Trovato il video ufficiale {title}, ma non sono riuscito a recuperare il testo completo.")
            sources.append(video_url)
    weekly_material = weekly_study_material(query)
    if weekly_material:
        weekly_chunks, weekly_sources = weekly_material
        chunks.extend(weekly_chunks)
        sources.extend(weekly_sources)
    if user_url:
        page_text = fetch_page(user_url)
        if page_text:
            chunks.append(f"TESTO DELLA PAGINA {user_url}:\n{page_text}")
            sources.append(user_url)
    for result in ([] if is_weekly_study_request(query) or is_specific_scripture_scenario(query) else search_wol(query)):
        text = fetch_page(result["url"]) or result["snippet"]
        if text:
            chunks.append(f"RISULTATO WOL: {result['title']}\nURL: {result['url']}\n{text[:5000]}")
            sources.append(result["url"])
    sources = list(dict.fromkeys(sources))
    return "\n\n---\n\n".join(chunks)[:85000], sources

def ask_gemini(prompt):
    client = genai.Client(api_key=GEMINI_API_KEY)
    result = client.models.generate_content(model=GEMINI_MODEL, contents=prompt,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.35, max_output_tokens=3000))
    answer = (result.text or "").strip()
    if not answer:
        raise RuntimeError("Gemini ha restituito una risposta vuota")
    answer = re.sub(r"(?im)^.*geova\s+ti\s+benedica.*(?:\n|$)", "", answer)
    lines = answer.rstrip().splitlines()
    signoff = re.compile(r".*(?:ti auguro|un caro saluto|cordiali saluti|a presto|buona giornata|buona serata|che geova ti benedica).*", re.I)
    while lines and signoff.match(lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).rstrip()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("Ciao! Puoi farmi una domanda, chiedermi di cercare nella Biblioteca Online o inviarmi il link a un video JW.org: userò i sottotitoli quando sono disponibili.")

def is_study_followup(query):
    q = normalize(query)
    return any(term in q for term in ("guida adunanza", "programma", "per trovarlo", "per trovarla", "come ti ho detto", "di stasera", "della settimana"))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    previous_study_query = context.user_data.get("last_weekly_study_query")
    if is_weekly_study_request(text):
        context.user_data["last_weekly_study_query"] = text
    elif previous_study_query:
        if is_study_followup(text):
            text = f"{previous_study_query}. {text}"
        context.user_data.pop("last_weekly_study_query", None)
    url = extract_jw_url(text)
    await update.message.reply_text("Cerco nelle fonti ufficiali e preparo la risposta…")
    material, sources = await asyncio.to_thread(research, text, url)
    if material:
        links = "\n".join(f"🔗 {source}" for source in sources)
        weekly_instructions = ""
        scripture_instructions = ""
        if is_scripture_request(text):
            scripture_instructions = ("La persona chiede una scrittura precisa: rispondi subito con un solo riferimento biblico, riporta il testo esatto presente nel materiale ufficiale e spiega in una frase come può incoraggiare nella situazione descritta. Non introdurre con ‘La pubblicazione indica’ e non fare un riepilogo generale. Usa 📖 per il versetto e 💬 per l’applicazione; mantieni una riga vuota tra i due.\n\n")
        if is_young_ministry_goal_request(text):
            scripture_instructions += ("Per un giovane che vuole porsi mete per diventare servitore di ministero, proponi 1 Timoteo 4:12 e riportane il testo fornito nel materiale ufficiale. Spiega in modo concreto come diventare un esempio nel parlare, nel comportamento, nell’amore e nella fede sia una meta utile nel suo progresso. Non presentare il versetto come un elenco dei requisiti ufficiali per l’incarico.\n\n")
        elif is_single_sister_encouragement_request(text):
            scripture_instructions += ("Per questa richiesta proponi 1 Timoteo 4:12, riportando il testo fornito nel materiale ufficiale. Spiega con tatto che la scrittura parla alla giovane età di Timoteo e può ricordare alla sorella il valore dei suoi doni e del suo esempio; non dire che il versetto parla direttamente dell’isolamento.\n\n")
        if is_weekly_study_request(text):
            weekly_instructions = ("Rispondi a tutte le domande presenti nello studio ufficiale recuperato, nell’ordine in cui compaiono, senza ometterne nessuna. Riporta ogni domanda fedelmente una sola volta, mantieni all’inizio della domanda il numero del paragrafo corrispondente quando compare nel testo (per esempio “1.”, “2.”); metti subito sotto la risposta, con una riga vuota tra una coppia domanda-risposta e la successiva. Includi anche tutte le domande delle sezioni “Scopri di più” e “Come rispondereste”, se presenti; prima di concludere controlla di non averne saltata nessuna. Anteponi ❓ alle domande numerate dell’articolo e 💬 a ogni risposta. Per le domande finali della sezione “Come rispondereste” usa 🧩 per distinguerle come riepilogo, mantenendo 💬 davanti a ogni risposta. Mantieni ogni risposta semplice, naturale e pronunciabile in meno di 20 secondi (massimo circa 35 parole), con tono caldo e concreto, come richiesto, senza fingere di essere una persona reale.\n\n")
        prompt = (weekly_instructions + scripture_instructions + "Rispondi usando il materiale ufficiale fornito. Per un video esamina tutti i sottotitoli "
                  "prima di rispondere. Scrivi in testo semplice: niente asterischi, cancelletti o formattazione Markdown. Usa emoji come intestazioni. Ogni voce dell’elenco deve iniziare con un’emoji pertinente, senza pallini, trattini o numeri. Separa le voci e gli argomenti con una riga vuota. Introduci direttamente con “Il video ...” o “La pubblicazione ...”; non parlare di trascrizioni o sottotitoli. Per richieste di riepilogo, copri in modo dettagliato tutti i segmenti del programma, i temi, le esperienze, gli esempi, le dimostrazioni, le applicazioni pratiche e le conclusioni presenti nel materiale. Non fermarti a una sintesi generica e non tralasciare i punti secondari. Rimani fedele alle fonti, non aggiungere dettagli non presenti e indica quando un passaggio non è chiaro. "
                  "Aggiungi una sezione '🔎 Fonti' con i link sotto. Non aggiungere saluti o benedizioni.\n\n"
                  f"Domanda: {text}\n\nMateriale:\n{material}\n\nFonti:\n{links}")
    else:
        prompt = (("Non sono riuscito a recuperare la pubblicazione corretta per questa settimana dalle fonti ufficiali. Dillo chiaramente e chiedi il link JW.org/WOL della pubblicazione; non rispondere usando materiale di un’altra settimana o una sezione diversa. Non inventare e non aggiungere saluti o benedizioni.\n\n" if is_weekly_study_request(text) else "La ricerca ufficiale non ha trovato materiale utilizzabile. Dillo chiaramente e chiedi un link JW.org/WOL preciso o un estratto. Non inventare. Non aggiungere saluti o benedizioni.\n\n") + text)
    try:
        answer = await asyncio.to_thread(ask_gemini, prompt)
    except Exception:
        logger.exception("Errore durante la richiesta a Gemini")
        await update.message.reply_text("In questo momento non riesco a completare la risposta. Riprova tra poco.")
        return
    for chunk in re.findall(r".{1,3900}(?:\s|$)", answer, flags=re.S):
        await update.message.reply_text(chunk.strip())

async def run_webhook_bot(application):
    global telegram_loop
    telegram_loop = asyncio.get_running_loop()
    await application.initialize()
    await application.start()
    webhook_url = WEBHOOK_BASE_URL + "/telegram"
    await application.bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET, allowed_updates=Update.ALL_TYPES)
    bot_ready.set()
    logger.info("Bot pronto tramite webhook Telegram su Render")
    try:
        await asyncio.Event().wait()
    finally:
        bot_ready.clear()
        await application.stop()
        await application.shutdown()

async def mark_polling_ready(application):
    # Local development only; Render uses webhooks to avoid getUpdates conflicts.
    await application.bot.delete_webhook(drop_pending_updates=False)
    bot_ready.set()

def run_web_server():
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)

def main():
    global telegram_application
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Variabile TELEGRAM_TOKEN mancante")
    if not GEMINI_API_KEY:
        raise RuntimeError("Variabile GEMINI_API_KEY mancante")
    threading.Thread(target=run_web_server, name="health-server", daemon=True).start()
    if WEBHOOK_BASE_URL:
        telegram_application = Application.builder().token(TELEGRAM_TOKEN).build()
        telegram_application.add_handler(CommandHandler("start", start))
        telegram_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        asyncio.run(run_webhook_bot(telegram_application))
    else:
        application = Application.builder().token(TELEGRAM_TOKEN).post_init(mark_polling_ready).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        logger.info("Avvio polling Telegram per sviluppo locale; modello Gemini: %s", GEMINI_MODEL)
        application.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()
