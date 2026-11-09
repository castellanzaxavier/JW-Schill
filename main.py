import os
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
import google.generativeai as genai
import requests
from bs4 import BeautifulSoup

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PORT = int(os.environ.get("PORT", 10000))

# Piccolo server HTTP integrato per soddisferà all'istante la porta di Render
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot JW attivo nel cloud!")
    def log_message(self, format, *args):
        pass

def run_http_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    server.serve_forever()

# Avvia subito il server web di cortesia in background
threading.Thread(target=run_http_server, daemon=True).start()

genai.configure(api_key=GEMINI_API_KEY)
generation_config = {"temperature": 0.3}

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
        "Fammi una domanda o inviami il link di un video di JW.org per sapere di cosa parla."
    )

def extract_jw_video_transcript(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        transcript_text = ""
        for a in soup.find_all('a', href=True):
            if '.vtt' in a['href']:
                vtt_url = a['href']
                if not vtt_url.startswith('http'):
                    vtt_url = "https://www.jw.org" + vtt_url
                vtt_resp = requests.get(vtt_url, headers=headers)
                if vtt_resp.status_code == 200:
                    transcript_text += vtt_resp.text + "\n"
        
        if not transcript_text:
            paragraphs = soup.find_all('p')
            transcript_text = " ".join([p.get_text() for p in paragraphs])
            
        return transcript_text[:10000]
    except Exception as e:
        logging.error(f"Errore estrazione video: {e}")
        return None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "jw.org" in text.lower():
        await update.message.reply_text("Un attimo fratello, sto esaminando il video e i sottotitoli...")
        transcript = extract_jw_video_transcript(text)
        if transcript:
            prompt = f"Analizza questo testo/trascrizione tratto da un video di JW.org e riassumi in modo fraterno e chiaro di cosa parla:\n\n{transcript}"
            response = chat_model.start_chat().send_message(prompt)
            await update.message.reply_text(response.text)
        else:
            await update.message.reply_text("Fratello, non sono riuscito a estrarre i sottotitoli o il testo da questo link specifico.")
        return

    await update.message.reply_text("Ricerca in corso tra le pubblicazioni di JW.org e WOL, fratello...")
    prompt = f"Rispondi alla seguente domanda basandoti rigorosamente sulle pubblicazioni di JW.org e wol.jw.org: {text}"
    response = chat_model.start_chat().send_message(prompt)
    await update.message.reply_text(response.text)

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    application.run_polling()

if __name__ == "__main__":
    main()
