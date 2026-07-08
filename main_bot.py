import sqlite3
import fitz
import os
from flask import Flask, request  # Add this
import re
import io
import asyncio
from datetime import datetime
from rembg import remove, new_session
from PIL import Image, ImageDraw, ImageFont
from ethiopian_date import EthiopianDateConverter
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler

# CONFIGURATION 
ADMIN_ID = 1032772516  
TELEBIRR_NUMBER = "0923804952"
BOT_TOKEN = "8212668446:AAGAs1oZI0cqiJQ-tC6MKLbDuQaBXLL3Czc"
# ADD THIS LINE:
REMBG_SESSION = new_session()

# flask
# --- ADD THIS BLOCK ---
flask_app = Flask(__name__)

# 2. Define a placeholder for the bot app
app = None

@flask_app.route('/')
def health_check():
    return "Bot is alive!", 200

@flask_app.route('/webhook', methods=['POST'])
async def webhook():
    global app
    if app:
        # Initialize and Start if not already running
        if not app.updater: 
            await app.initialize()
            await app.start()
            
            # AUTO-SET WEBHOOK: This ensures Telegram knows where to send updates
            URL = os.environ.get("RENDER_EXTERNAL_URL")
            if URL:
                await app.bot.set_webhook(url=f"{URL}/webhook")
            
        data = request.get_json(force=True)
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
        
    return "ok", 200

# Conversation States
MENU, BUY_PACK, WAIT_RECEIPT = range(3)


# 1. DATABASE LOGIC

def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (user_id INTEGER PRIMARY KEY, credits INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

def get_credits(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE user_id=?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res[0] if res else 0

def add_credits(user_id, amount):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, credits) VALUES (?, 0)", (user_id,))
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()


# 2. PDF & ID LOGIC (Preserving your exact extraction)

def get_next_serial_number():
    filename = "serial_counter.txt"
    if not os.path.exists(filename):
        with open(filename, "w") as f:
            f.write("0000000")
            return "6000000"
    with open(filename, "r") as f:
        content = f.read().strip()
        current_sn = int(content) if content else 7000000
    next_sn = current_sn + 1
    with open(filename, "w") as f:
        f.write(str(next_sn))
    return str(next_sn)

def extract_data_from_pdf(pdf_path, user_id):
    if not os.path.exists(pdf_path): return None
    doc = fitz.open(pdf_path)
    page = doc[0]

    paths = {'photo': f"photo_{user_id}.png", 'qr': f"qr_{user_id}.png", 
             'barcode': f"barcode_{user_id}.png", 'fin': f"fin_{user_id}.png"}

    image_list = page.get_images(full=True)
    for i, img in enumerate(image_list):
        xref = img[0]
        pix = fitz.Pixmap(doc, xref)
        if pix.n - pix.alpha > 3: pix = fitz.Pixmap(fitz.csRGB, pix)
        
        if i == 0:
            img_data = pix.tobytes("png")
            output_image = remove(Image.open(io.BytesIO(img_data)), session=REMBG_SESSION)
            output_image.save(paths['photo'])
        elif i == 1: pix.save(paths['qr'])
        elif i == 2:
            img_pil = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            w, h = img_pil.size
            barcode_box = (int(w*(590/w)), int(h*(2695/h)), int(w*(1300/w)), int(h*(2900/h)))
            img_pil.crop(barcode_box).save(paths['barcode'])

    page.get_pixmap(clip=fitz.Rect(496.5, 493, 540, 501), matrix=fitz.Matrix(4, 4)).save(paths['fin'])
    
    text = page.get_text("text")
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    now = datetime.now()
    eth_now = EthiopianDateConverter.to_ethiopian(now.year, now.month, now.day)
    
    data = {
        'name_amh': lines[57] if len(lines) > 57 else "Unknown",
        'name_eng': lines[58] if len(lines) > 58 else "Unknown",
        'dob': f"{lines[43]} | {lines[44]}" if len(lines) > 44 else "Unknown",
        'sex': f"{lines[45]} | {lines[46]}" if len(lines) > 46 else "Unknown",
        'fan': "Unknown", 'sn': get_next_serial_number(),
        'phone': lines[49] if len(lines) > 49 else "",
        'address': lines[50:56],
        'expiry': f"{now.day:02d}/{now.month:02d}/{now.year+10} | {eth_now.day:02d}/{eth_now.month:02d}/{eth_now.year+10}"
    }
    for line in lines:
        clean = line.replace(" ", "")
        fan_match = re.search(r'(\d{16})', clean)
        if fan_match: data['fan'] = fan_match.group(1)
    doc.close()
    return data

def load_bold_font(size):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    font_candidates = [
        os.path.join(base_dir, "ebrima.ttf"),
        os.path.join(base_dir, "washrab.ttf"),
        os.path.join(base_dir, "arial.ttf"),
        os.path.join(base_dir, "DejaVuSans.ttf"),
    ]
    for font_path in font_candidates:
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def generate_fayda_v3(data, output_path, user_id, mode="color"):
    template_candidates = ["fayda.jpg", "Fayda.jpg", "Templet2.png", "Templet2.jpg"]
    template_path = next((name for name in template_candidates if os.path.exists(name)), None)
    if not template_path:
        return False
    canvas = Image.open(template_path).convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    f_amh = load_bold_font(39)
    f_bold = load_bold_font(32)
    f_small = load_bold_font(23)

    # Dynamic Rotated Dates
    now = datetime.now()
    eth_conv = EthiopianDateConverter.to_ethiopian(now.year, now.month, now.day)
    g_date = now.strftime("%d/%m/%Y")
    e_date = f"{eth_conv.day:02d}/{eth_conv.month:02d}/{eth_conv.year}"

    def draw_rotated_text(text, position, font):
        text_img = Image.new("RGBA", (250, 60), (255, 255, 255, 0))
        d = ImageDraw.Draw(text_img)
        d.text((0, 0), text, font=font, fill="black")
        rotated = text_img.rotate(90, expand=True)
        canvas.paste(rotated, position, rotated)

    draw_rotated_text(g_date, (22, 7), f_small)
    draw_rotated_text(e_date, (22, 270), f_small)

    # Photo Logic
    photo_path = f"photo_{user_id}.png"
    if os.path.exists(photo_path):
        raw_photo = Image.open(photo_path).convert("RGBA")
        if mode == "bw":
            r, g, b, alpha = raw_photo.split()
            gray = raw_photo.convert("L")
            raw_photo = Image.merge("RGBA", (gray, gray, gray, alpha))
        canvas.paste(raw_photo.resize((330, 370)), (62, 180), raw_photo.resize((330, 370)))
        ghost = raw_photo.resize((110, 130))
        canvas.paste(ghost, (850, 480), ghost)

    # Assets (QR, Barcode, Fingerprint)
    for asset, size, pos in [(f"qr_{user_id}.png", (260, 260), (1520, 20)), (f"barcode_{user_id}.png", (300, 65), (453, 544)), (f"fin_{user_id}.png", (240, 50), (1230, 508))]:
        if os.path.exists(asset):
            img = Image.open(asset).resize(size).convert("RGBA")
            canvas.paste(img, pos, img)

    # Main Text Overlay
    text_x = 402
    draw.text((text_x, 180), data['name_amh'], font=f_amh, fill="black")
    draw.text((text_x, 222), data['name_eng'], font=f_bold, fill="black")
    draw.text((text_x, 310), data['dob'], font=f_bold, fill="black")
    draw.text((text_x, 375), data['sex'], font=f_amh, fill="black")
    draw.text((text_x, 447), data['expiry'], font=f_bold, fill="black")
    draw.text((460, 512), data['fan'], font=f_bold, fill="black")
    draw.text((canvas.width - 180, canvas.height - 55), data['sn'], font=f_bold, fill="black")

    back_x, y_addr = (canvas.width // 2) + 46, 235
    draw.text((back_x, 70), data['phone'], font=f_bold, fill="black")
    for line in data['address']:
        draw.text((back_x, y_addr), line, font=f_amh, fill="black")
        y_addr += 40

    canvas.convert("RGB").save(output_path, "PNG")
    return True

# ==========================================
# 3. UI HELPERS
# ==========================================
def main_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🖨 Print ID", callback_data='print_id')],
                                 [InlineKeyboardButton("💳 Buy Package", callback_data='buy_package')],
                                 [InlineKeyboardButton("📞 Contact Help", callback_data='contact_help')]])

def package_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("40 birr = 1 package", callback_data='pkg_1')],
                                 [InlineKeyboardButton("500 birr = 25 packages", callback_data='pkg_20')],
                                 [InlineKeyboardButton("1500 birr = 100 packages", callback_data='pkg_100')],
                                 [InlineKeyboardButton("2000 birr = 155 packages", callback_data='pkg_150')]])


# 4. BOT HANDLERS

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    credits = get_credits(user_id)
    welcome = (
        "Welcome to the National ID Fayda Printable Converter Service! 🎉\n\n"
        "📑 **To get your printable ID card:**\n"
        "1. Download the FAYDA ID pdf from FAYDA app  OR Telebirr \n"
        "2. Send the downloaded PDF file here to this bot.\n\n"
        "እንኳን ወደ ብሔራዊ መታወቂያ ፋይዳ ካርድ ሊታተም የሚችል መቀየሪያ አገልግሎት በደህና መጡ! 🎉\n"
        "🪪 ሊታተም የሚችል መታወቂያ ካርድዎን ለማግኘት፡-\n"
       "1. የFAYDA መታወቂያ ፒዲኤፍ ከFAYDA መተግበሪያ ወይም ከTelebirr ያውርዱ \n"
       "2. የወረደውን የፒዲኤፍ ፋይል ወደዚህ ቦት ይላኩ።\n\n"
       "Baga Gara Tajaajila Jijjiirraa Maxxanfamuu Danda'u FAYDA Eenyummaa Biyyaalessaatti dhuftan! 🎉\n\n" 
"📑 NATIONAL ID  maxxanfamuu danda'u argachuuf:**\n" 
"1. ID FAYDA pdf appii FAYDA YKN Telebirr irraa buufadhaa \n" 
"2. Faayila PDF buufame as gara bot kanaatti ergi.\n\n"
        f"💰 **Your Balance:** {credits} packages"
    )
    await update.message.reply_text(welcome, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    return MENU

async def button_tap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'buy_package':
        await query.edit_message_text("Select a package below 👇", reply_markup=package_keyboard())
        return BUY_PACK
    elif query.data == 'print_id':
        await query.message.reply_text("Please send your Fayda PDF file now.")
        return MENU
    elif query.data == 'contact_help':
        await query.message.reply_text("Support: @altleg")
        return MENU

async def select_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pkg_map = {'pkg_1': '1', 'pkg_20': '20', 'pkg_100': '100', 'pkg_150': '150'}
    context.user_data['pending_pkg'] = pkg_map[query.data]
    await query.edit_message_text(f"Pay to **{TELEBIRR_NUMBER}** then send the SMS receipt here.")
    return WAIT_RECEIPT

async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    
    # Get the username or fallback to First Name if they don't have one
    user_name = f"@{user.username}" if user.username else user.first_name
    
    # Add Username to the admin message
    admin_msg = (
        f"🔔 New Payment\n"
        f"👤 User: {user_name}\n"
        f"🆔 ID: {user.id}\n"
        f"📦 Pkg: {context.user_data.get('pending_pkg')}\n\n"
        f"📝 SMS Receipt:\n{update.message.text}"
    )
    
    btns = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"appr_{user.id}_{context.user_data.get('pending_pkg')}"), 
        InlineKeyboardButton("❌ Reject", callback_data=f"rej_{user.id}")
    ]])
    
    await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg, reply_markup=btns, parse_mode="Markdown")
    await update.message.reply_text("Receipt sent for approval. / ደረሰኝዎ ለቁጥጥር ተልኳል።")
    return MENU

async def admin_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    if data[0] == "appr":
        add_credits(int(data[1]), int(data[2]))
        await context.bot.send_message(chat_id=int(data[1]), text="✅ Payment Approved!")
        await query.edit_message_text("✅ Approved")
    elif data[0] == "rej":
        await context.bot.send_message(chat_id=int(data[1]), text="❌ Payment Rejected")
        await query.edit_message_text("❌ Rejected")
    else:
        await query.edit_message_text("Done.")


# 5. INTEGRATED PDF HANDLER

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if get_credits(user_id) < 0:
        await update.message.reply_text("❌ Insufficient balance.", reply_markup=main_menu_keyboard())
        return

    msg = await update.message.reply_text("⏳ Processing...")
    pdf_path = f"input_{user_id}.pdf"
    file = await context.bot.get_file(update.message.document.file_id)
    await file.download_to_drive(pdf_path)

    try:
        # FIXED: Run extraction in a background thread
        data = await asyncio.to_thread(extract_data_from_pdf, pdf_path, user_id)
        
        if data:
            c_out, b_out = f"C_{user_id}.png", f"B_{user_id}.png"
            
            # FIXED: Run generation in background threads
            await asyncio.to_thread(generate_fayda_v3, data, c_out, user_id, "color")
            await asyncio.to_thread(generate_fayda_v3, data, b_out, user_id, "bw")
            
            with open(c_out, 'rb') as f: await update.message.reply_document(f, filename="Fayda_Color.png")
            with open(b_out, 'rb') as f: await update.message.reply_document(f, filename="Fayda_BW.png")
            
            add_credits(user_id, -1)
            await msg.edit_text(f"✅ Success! 1 package deducted. Balance: {get_credits(user_id)}")
        else:
            await msg.edit_text("❌ Extraction failed.")
    finally:
        for f in [pdf_path, f"C_{user_id}.png", f"B_{user_id}.png", f"photo_{user_id}.png", f"qr_{user_id}.png", f"barcode_{user_id}.png", f"fin_{user_id}.png"]:
            if os.path.exists(f): os.remove(f)




# ADD THIS NEW BLOCK
# 1. Initialize Database and Bot (OUTSIDE the main block)
init_db()

# Initialize the Telegram App globally so Flask can see it
app = ApplicationBuilder().token(BOT_TOKEN).build()

# Define Handlers
conv = ConversationHandler(
    entry_points=[CommandHandler('start', start)],
    states={
        MENU: [
            CallbackQueryHandler(button_tap, pattern="^(buy_package|print_id|contact_help)$"),
            MessageHandler(filters.Document.PDF, handle_pdf)
        ],
        BUY_PACK: [CallbackQueryHandler(select_package, pattern="^(pkg_1|pkg_20|pkg_100|pkg_150)$")],
        WAIT_RECEIPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_receipt)]
    },
    fallbacks=[CommandHandler('start', start)]
)

app.add_handler(conv)
app.add_handler(CallbackQueryHandler(admin_approval, pattern="^(appr|rej)_"))

# 2. Add a helper to start the bot's background processes
async def setup_webhook():
    URL = os.environ.get("RENDER_EXTERNAL_URL")
    if URL:
        await app.bot.set_webhook(url=f"{URL}/webhook")
        print(f"🚀 Webhook set to {URL}/webhook")

# This logic runs when Gunicorn starts
import threading
if os.environ.get("RENDER_EXTERNAL_URL"):
    # Run the webhook setup in the background
    import asyncio
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(app.initialize())).start()

# 3. Keep the main block ONLY for local testing (VS Code)
if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 10000))
    URL = os.environ.get("RENDER_EXTERNAL_URL") 

    if not URL:
        print("🚀 Local Mode: Polling")
        app.run_polling()
