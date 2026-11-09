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

# Server Flask per mantenere Render attivo sulla porta
app = Flask(__name__)

@app.route("/")
def index():
    return "Bot JW attivo e operativo nel cloud!"

genai.configure(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """
Sei un assistente virtuale fraterno, parli con un tono caloroso, rispettoso, edificante e fraterno (ti rivolgi all'utente chiamandolo "fratello" o con affetto cristiano).
Il tuo compito è rispondere alle domande e analizzare i testi tratti da JW.org e wol.jw.org attingendo esclusivamente dai contenuti ufficiali.
Se non trovi informazioni certe su queste fonti, di' con dolcezza che non ci sono risposte specifiche pubblicate su jw.org.
"""

chat_model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction=SYSTEM_PROMPT
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ciao, fratello! Sono pronto ad aiutarti nelle ricerche su JW.org e WOL. "
        "Inviami un link di un articolo di JW.org o fammi una domanda per approfondire insieme."
    )

def fetch_jw_article_text(url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # JW.org utilizza classi specifiche come 'bodyTxt' o 'docBody' per i testi degli articoli
        body_container = soup.find('div', class_=['bodyTxt', 'docBody', 'article-body'])
        
        if body_container:
            paragraphs = body_container.find_all(['p', 'li'])
            text = " ".join([p.get_text().strip() for p in paragraphs if p.get_text().strip()])
            if text:
                return text[:8000] # Limite per sicurezza
                
        # Fallback generale se la classe specifica non viene trovata
        paragraphs = soup.find_all('p')
        text = " ".join([p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 30])
        return text[:8000] if text else None
        
    except Exception as e:
        logging.error(f"Errore durante il recupero dell'articolo: {e}")
        return None

def ask_gemini(prompt):
    chat = chat_model.start_chat()
    response = chat.send_message(prompt)
    return response.text

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if "jw.org" in text.lower():
        await update.message.reply_text("Un attimo fratello, sto leggendo l'articolo direttamente da JW.org...")
        
        # Estrae l'URL dal messaggio
        import re
        urls = re.findall(r'(https?://[^\s]+)', text)
        target_url = urls[0] if urls else text
        
        article_text = await asyncio.to_thread(fetch_jw_article_text, target_url)
        
        if article_text:
            prompt = f"Analizza questo testo estratto da un articolo ufficiale di JW.org e fornisci un riassunto chiaro, approfondito e con un tono caloroso e fraterno:\n\n{article_text}"
        else:
            prompt = f"L'utente ha condiviso questo link di JW.org ({target_url}), ma non sono riuscito a estrarre il testo HTML. Fornisci comunque un approfondimento edificante basandoti sulla tua conoscenza di questo argomento in base alle pubblicazioni di JW.org."
            
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
