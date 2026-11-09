import os
import logging
import threading
from flask import Flask
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
import google.generativeai as genai
import requests
from bs4 import BeautifulSoup
import asyncio

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PORT = int(os.environ.get("PORT", 10000))

# Server Flask per mantenere felice Render sulla porta
app = Flask(__name__)

@app.route("/")
def index():
    return "Bot JW attivo e operativo nel cloud!"

genai.configure(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """
Sei un assistente virtuale fraterno, parli con un tono caloroso, rispettoso, edificante e fraterno (ti rivolgi all'utente chiamandolo "fratello" o con affetto cristiano).
Il tuo compito è rispondere alle domande e analizzare i link/argomenti attingendo esclusivamente dai contenuti ufficiali di JW.org e wol.jw.org.
Se non trovi informazioni certe su queste fonti, di' con dolcezza che non ci sono risposte specifiche pubblicate su jw.org.
"""

chat_model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction=SYSTEM_PROMPT
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ciao, fratello! Sono pronto ad aiutarti nelle ricerche su JW.org e WOL. "
        "Fammi una domanda o inviami un argomento/link per approfondire insieme."
    )

def ask_gemini(prompt):
    chat = chat_model.start_chat()
    response = chat.send_message(prompt)
    return response.text

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if "jw.org" in text.lower():
        await update.message.reply_text("Un attimo fratello, sto esaminando il contenuto alla luce delle pubblicazioni di JW.org...")
        prompt = f"L'utente ha condiviso questo link o riferimento di JW.org: '{text}'. Analizzalo e fornisci un riassunto o una spiegazione edificante e fraterna basandoti rigorosamente sugli insegnamenti di JW.org e wol.jw.org."
    else:
        await update.message.reply_text("Ricerca in corso tra le pubblicazioni di JW.org e WOL, fratello...")
        prompt = f"Rispondi alla seguente domanda basandoti rigorosamente sulle pubblicazioni di JW.org e wol.jw.org: {text}"

    response_text = await asyncio.to_thread(ask_gemini, prompt)
    await update.message.reply_text(response_text)

def run_telegram_bot():
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook", timeout=10)
    except Exception as e:
        logging.error(f"Errore pulizia webhook: {e}")

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.run_polling()

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_telegram_bot, daemon=True)
    bot_thread.start()
    
    app.run(host="0.0.0.0", port=PORT)
