import asyncio
import logging
import os
import re
import threading
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from flask import Flask
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
PORT = int(os.environ.get("PORT", "10000"))

app = Flask(__name__)
bot_ready = threading.Event()

SYSTEM_PROMPT = """
Sei un assistente virtuale fraterno. Usa un tono caloroso, rispettoso e edificante.
Aiuti l'utente a comprendere pubblicazioni di JW.org e wol.jw.org.
Usa soltanto il testo ufficiale fornito nel contesto della richiesta. Non inventare
citazioni, riferimenti o contenuti e non presentare la tua conoscenza generale come
una fonte ufficiale. Se il testo disponibile non basta per rispondere, dillo con
gentilezza e chiedi un link o un estratto pertinente.
""".strip()


@app.get("/")
def index():
    return "Bot JW online" if bot_ready.is_set() else "Servizio web online; bot in avvio", 200


@app.get("/health")
def health():
    if not bot_ready.is_set():
        return {"status": "starting"}, 503
    return {"status": "ok"}, 200


def extract_jw_url(message: str) -> str | None:
    for candidate in re.findall(r"https?://[^\s<>]+", message, flags=re.IGNORECASE):
        candidate = candidate.rstrip(".,;:!?)]}\"'")
        try:
            parsed = urlsplit(candidate)
            hostname = (parsed.hostname or "").lower()
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and (hostname == "jw.org" or hostname.endswith(".jw.org")):
            return candidate
    return None


def fetch_jw_article_text(url: str) -> str | None:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; JWSchillBot/1.0)"},
            timeout=15,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for selector in ("div.bodyTxt", "div.docBody", "div.article-body", "article"):
            container = soup.select_one(selector)
            if container:
                text = " ".join(p.get_text(" ", strip=True) for p in container.find_all(["p", "li"]))
                if text:
                    return text[:12000]
        text = " ".join(p.get_text(" ", strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 30)
        return text[:12000] if text else None
    except requests.RequestException as exc:
        logger.warning("Recupero pagina JW.org non riuscito: %s", exc)
        return None


def ask_gemini(prompt: str) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.4,
            max_output_tokens=1200,
        ),
    )
    answer = (response.text or "").strip()
    if not answer:
        raise RuntimeError("Gemini ha restituito una risposta vuota")
    return answer


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "Ciao, fratello! Inviami un link a una pubblicazione di JW.org o WOL "
            "oppure un estratto da approfondire."
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    url = extract_jw_url(text)
    if url:
        await update.message.reply_text("Leggo la pagina, fratello…")
        article_text = await asyncio.to_thread(fetch_jw_article_text, url)
        if article_text:
            prompt = (
                f"Rispondi alla richiesta dell'utente usando esclusivamente il testo "
                f"della pubblicazione qui sotto. Se il testo non contiene elementi "
                f"sufficienti, dichiaralo.\n\nRichiesta: {text}\n\n"
                f"Testo della pagina ({url}):\n{article_text}"
            )
        else:
            prompt = (
                f"Non è stato possibile leggere la pagina {url}. Spiega con gentilezza "
                "che non puoi verificare il contenuto e chiedi all'utente di incollare "
                "il passaggio o inviare un altro link JW.org/WOL. Non rispondere usando "
                f"conoscenze non verificate. Richiesta: {text}"
            )
    else:
        prompt = (
            "L'utente non ha fornito una pubblicazione o un estratto ufficiale. "
            "Chiedigli con gentilezza di inviare un link JW.org/WOL o il testo da "
            f"approfondire. Non inventare una ricerca né una risposta basata su fonti.\n\n{text}"
        )

    try:
        answer = await asyncio.to_thread(ask_gemini, prompt)
    except Exception:
        logger.exception("Errore durante la richiesta a Gemini")
        await update.message.reply_text(
            "In questo momento non riesco a completare la risposta. Riprova tra poco."
        )
        return

    # Telegram accetta al massimo 4096 caratteri per messaggio.
    for offset in range(0, len(answer), 3900):
        await update.message.reply_text(answer[offset : offset + 3900])


async def remove_old_webhook(application: Application) -> None:
    # Necessario se il bot era stato configurato in precedenza per webhook.
    await application.bot.delete_webhook(drop_pending_updates=False)
    bot_ready.set()


def run_web_server() -> None:
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Variabile TELEGRAM_TOKEN mancante")
    if not GEMINI_API_KEY:
        raise RuntimeError("Variabile GEMINI_API_KEY mancante")

    threading.Thread(target=run_web_server, name="health-server", daemon=True).start()

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(remove_old_webhook)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Avvio polling Telegram; modello Gemini configurato: %s", GEMINI_MODEL)
    application.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
