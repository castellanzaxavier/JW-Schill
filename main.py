async def run_bot():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    if RENDER_EXTERNAL_HOSTNAME:
        await application.initialize()
        await application.start()
        await application.updater.start_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TELEGRAM_TOKEN,
            webhook_url=f"https://{RENDER_EXTERNAL_HOSTNAME}/{TELEGRAM_TOKEN}"
        )
        # Mantiene il bot attivo nel cloud
        import asyncio
        while True:
            await asyncio.sleep(3600)
    else:
        await application.run_polling()

def main():
    import asyncio
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()
