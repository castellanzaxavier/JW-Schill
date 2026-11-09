import os
import logging
import threading
import asyncio
import re
from flask import Flask
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
import google.generativeai as genai
import requests
from bs4 import BeautifulSoup

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PORT = int(os.environ.get("PORT", 10000))

app = Flask(__name__)

@app.route("/")
def index():
    return "Bot JW attivo e operativo nel cloud!"

genai.configure(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """
Sei un assistente virtuale fraterno, parli con un tono caloroso, rispettoso, edificante e fraterno (ti rivolgi all'utente chiamandolo "fratello" o con affetto cristiano).
Il tuo compito è rispondere alle domande attingendo esclusivamente dai contenuti ufficiali di JW.org e wol.jw.org.
Se non trovi informazioni certe su queste fonti, di' con dolcezza che non ci sono risposte specifiche pubblicate su jw.org.
"""

chat_model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction=SYSTEM_PROMPT
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ciao, fratello! Sono pronto ad aiutarti nelle ricerche su JW.org e WOL. "
        "Fammi una domanda o inviami il link di un video o articolo di JW.org per sapere di cosa parla."
    )

def extract_jw_content(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 1. Cerca sottotitoli .vtt direttamente nella pagina
        vtt_urls = []
        for a in soup.find_all(['a', 'source', 'track'], href=True):
            if '.vtt' in a['href']:
                vtt_urls.append(a['href'])
        for track in soup.find_all('track', src=True):
            if '.vtt' in track['src']:
                vtt_urls.append(track['src'])
                
        if vtt_urls:
            vtt_url = vtt_urls[0]
            if not vtt_url.startswith('http'):
                vtt_url = "https://www.jw.org" + vtt_url
            vtt_resp = requests.get(vtt_url, headers=headers, timeout=10)
            if vtt_resp.status_code == 200:
                # Pulisce i tag di tempo VTT per lasciare solo il testo
                clean_text = re.sub(r'\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}', '', vtt_resp.text)
                clean_text = re.sub(r'<[^>]+>', '', clean_text)
                lines = [line.strip() for line in clean_text.splitlines() if line.strip() and not line.strip().isdigit() and line.strip() != "WEBVTT"]
                return " ".join(lines)[:8000]

        # 2. Se è un articolo o pagina testo, estrai i paragrafi principali
        article = soup.find('article') or soup.find('main') or soup
        paragraphs = article.find_all('p')
        text_content = " ".join([p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 20])
        
        if text_content:
            return text_content[:8000]
            
        return None
    except Exception as e:
        logging.error(f"Errore estrazione testo: {e}")
        return None

def ask_gemini(prompt):
    chat = chat_model.start_chat()
    response = chat.send_message(prompt)
    return response.text

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    # Se l'utente invia un link di JW.org
    if "jw.org" in text.lower():
        await update.message.reply_text("Un attimo fratello, sto leggendo il contenuto dal link di JW.org...")
        
        # Estrai l'URL dal messaggio
        urls = re.findall(r'(https?://[^\s]+)', text)
        target_url = urls[0] if urls else text
        
        extracted_text = await asyncio.to_thread(extract_jw_content, target_url)
        
        if extracted_text:
            prompt = f"Analizza questo testo tratto da JW.org e fai un riassunto chiaro, incoraggiante e fraterno:\n\n{extracted_text}"
            response_text = await asyncio.to_thread(ask_gemini, prompt)
            await update.message.reply_text(response_text)
        else:
            await update.message.reply_text("Fratello, non sono riuscito ad estrarre il testo da questo specifico link. Prova con un altro articolo o video di JW.org!")
        return

    # Domanda generale
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
