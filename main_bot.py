import sqlite3
import fitz
import os
import re
import io
import asyncio
import threading
import logging
from datetime import datetime
from flask import Flask, request
from rembg import remove, new_session
from PIL import Image, ImageDraw, ImageFont
from ethiopian_date import EthiopianDateConverter
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes, ConversationHandler
)

# ════════════════════════════════════════════════════
# CONFIGURATION — load from .env or use defaults
# ════════════════════════════════════════════════════
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optional

BOT_TOKEN      = os.environ.get("BOT_TOKEN",      "8212668446:AAGqJXdlFfvG13LQvN6dwvzvpvCL7MZUVtA")
ADMIN_ID       = int(os.environ.get("ADMIN_ID",   "1032772516"))
TELEBIRR_NUMBER = os.environ.get("TELEBIRR_NUMBER", "0923804952")

# ════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════
# REMBG SESSION (loaded once at startup)
# ════════════════════════════════════════════════════
logger.info("⏳ Loading rembg AI model...")
REMBG_SESSION = new_session()
logger.info("✅ rembg model loaded.")

# ════════════════════════════════════════════════════
# FLASK (for Render webhook deployment)
# ════════════════════════════════════════════════════
flask_app = Flask(__name__)
app = None  # Telegram app (set later)

@flask_app.route("/")
def health_check():
    return "✅ FaydaIDProcessor Bot is alive!", 200

@flask_app.route("/webhook", methods=["POST"])
async def webhook():
    global app
    if app:
        try:
            data = request.get_json(force=True)
            update = Update.de_json(data, app.bot)
            await app.process_update(update)
        except Exception as e:
            logger.error(f"Webhook error: {e}")
    return "ok", 200

# ════════════════════════════════════════════════════
# CONVERSATION STATES
# ════════════════════════════════════════════════════
MENU, BUY_PACK, WAIT_RECEIPT = range(3)

# ════════════════════════════════════════════════════
# 1. DATABASE
# ════════════════════════════════════════════════════
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id  INTEGER PRIMARY KEY,
            credits  INTEGER DEFAULT 0,
            username TEXT,
            joined   TEXT
        )
    """)
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized.")

def get_credits(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE user_id=?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res[0] if res else 0

def add_credits(user_id: int, amount: int, username: str = ""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO users (user_id, credits, username, joined) VALUES (?, 0, ?, ?)",
        (user_id, username, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    c.execute(
        "UPDATE users SET credits = credits + ?, username = ? WHERE user_id = ?",
        (amount, username, user_id)
    )
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, credits, username, joined FROM users ORDER BY credits DESC")
    rows = c.fetchall()
    conn.close()
    return rows

# ════════════════════════════════════════════════════
# 2. SERIAL NUMBER
# ════════════════════════════════════════════════════
SERIAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serial_counter.txt")

def get_next_serial_number() -> str:
    if not os.path.exists(SERIAL_FILE):
        with open(SERIAL_FILE, "w") as f:
            f.write("6000000")
        return "6000000"
    with open(SERIAL_FILE, "r") as f:
        content = f.read().strip()
        current_sn = int(content) if content.isdigit() else 7000000
    next_sn = current_sn + 1
    with open(SERIAL_FILE, "w") as f:
        f.write(str(next_sn))
    return str(next_sn)

# ════════════════════════════════════════════════════
# 3. PDF EXTRACTION
# ════════════════════════════════════════════════════
def extract_data_from_pdf(pdf_path: str, user_id: int) -> dict | None:
    if not os.path.exists(pdf_path):
        return None
    try:
        doc = fitz.open(pdf_path)
        page = doc[0]

        paths = {
            "photo": f"photo_{user_id}.png",
            "qr":    f"qr_{user_id}.png",
            "fin":   f"fin_{user_id}.png",
        }

        image_list = page.get_images(full=True)
        for i, img in enumerate(image_list):
            xref = img[0]
            pix  = fitz.Pixmap(doc, xref)
            if pix.n - pix.alpha > 3:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            if i == 0:
                img_data     = pix.tobytes("png")
                output_image = remove(Image.open(io.BytesIO(img_data)), session=REMBG_SESSION)
                output_image.save(paths["photo"])
            elif i == 1:
                pix.save(paths["qr"])

        # Fingerprint region
        page.get_pixmap(
            clip=fitz.Rect(496.5, 493, 540, 501),
            matrix=fitz.Matrix(4, 4)
        ).save(paths["fin"])

        text  = page.get_text("text")
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        now       = datetime.now()
        eth_now   = EthiopianDateConverter.to_ethiopian(now.year, now.month, now.day)

        data = {
            "name_amh": lines[57] if len(lines) > 57 else "Unknown",
            "name_eng": lines[58] if len(lines) > 58 else "Unknown",
            "dob":      f"{lines[43]} | {lines[44]}" if len(lines) > 44 else "Unknown",
            "sex":      f"{lines[45]} | {lines[46]}" if len(lines) > 46 else "Unknown",
            "fan":      "Unknown",
            "sn":       get_next_serial_number(),
            "phone":    lines[49] if len(lines) > 49 else "",
            "address":  lines[50:56],
            "expiry":   (
                f"{now.day:02d}/{now.month:02d}/{now.year+10} | "
                f"{eth_now.day:02d}/{eth_now.month:02d}/{eth_now.year+10}"
            ),
        }
        for line in lines:
            fan_match = re.search(r"(\d{16})", line.replace(" ", ""))
            if fan_match:
                data["fan"] = fan_match.group(1)
                break

        doc.close()
        return data

    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
        return None

# ════════════════════════════════════════════════════
# 4. FONT LOADING
# ════════════════════════════════════════════════════
def load_font(size: int) -> ImageFont.FreeTypeFont:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        "ebrima-bold.ttf", "ebrima.ttf", "washrab.ttf",
        "arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf",
    ]
    for name in candidates:
        path = os.path.join(base_dir, name)
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()

# ════════════════════════════════════════════════════
# 5. ID CARD GENERATOR
# ════════════════════════════════════════════════════
def generate_fayda_v3(data: dict, output_path: str, user_id: int, mode: str = "color") -> bool:
    template_candidates = [
        "fayda.jpg", "Fayda.jpg",
        "faydatemplate1.jpg", "faydatemplate1.png",
        "Templet2.png", "Templet2.jpg",
    ]
    base_dir = os.path.dirname(os.path.abspath(__file__))
    template_path = next(
        (os.path.join(base_dir, n) for n in template_candidates
         if os.path.exists(os.path.join(base_dir, n))),
        None
    )
    if not template_path:
        logger.error("❌ No template image found!")
        return False

    try:
        canvas = Image.open(template_path).convert("RGBA")
        draw   = ImageDraw.Draw(canvas)
        f_amh  = load_font(26)
        f_bold = load_font(26)
        f_sm   = load_font(16)

        # Rotated issue date (side strip)
        now      = datetime.now()
        eth_conv = EthiopianDateConverter.to_ethiopian(now.year, now.month, now.day)
        g_date   = now.strftime("%d/%m/%Y")
        e_date   = f"{eth_conv.day:02d}/{eth_conv.month:02d}/{eth_conv.year}"

        def draw_rotated(text, pos, font):
            tmp = Image.new("RGBA", (250, 60), (255, 255, 255, 0))
            ImageDraw.Draw(tmp).text((0, 0), text, font=font, fill="black")
            rot = tmp.rotate(90, expand=True)
            canvas.paste(rot, pos, rot)

        draw_rotated(g_date, (22, 7),   f_sm)
        draw_rotated(e_date, (22, 270), f_sm)

        # Main photo (background removed)
        photo_path = os.path.join(base_dir, f"photo_{user_id}.png")
        if os.path.exists(photo_path):
            raw_photo = Image.open(photo_path).convert("RGBA")
            if mode == "bw":
                r, g, b, alpha = raw_photo.split()
                gray      = raw_photo.convert("L")
                raw_photo = Image.merge("RGBA", (gray, gray, gray, alpha))
            # Large photo
            big   = raw_photo.resize((330, 370))
            canvas.paste(big, (62, 180), big)
            # Small ghost photo (back side)
            ghost = raw_photo.resize((110, 130))
            canvas.paste(ghost, (850, 480), ghost)

        # QR code & fingerprint
        for asset_name, size, pos in [
            (f"qr_{user_id}.png",  (490, 490), (1520,  20)),
            (f"fin_{user_id}.png", (240,  50), (1230, 508)),
        ]:
            asset_path = os.path.join(base_dir, asset_name)
            if os.path.exists(asset_path):
                img = Image.open(asset_path).resize(size).convert("RGBA")
                canvas.paste(img, pos, img)

        # Text overlay — front side
        tx = 402
        draw.text((tx, 177), data["name_amh"],  font=f_amh,  fill="black")
        draw.text((tx, 219), data["name_eng"],  font=f_bold, fill="black")
        draw.text((tx, 304), data["dob"],        font=f_bold, fill="black")
        draw.text((tx, 370), data["sex"],        font=f_amh,  fill="black")
        draw.text((tx, 440), data["expiry"],     font=f_bold, fill="black")
        draw.text((460, 490), data["fan"],       font=f_bold, fill="black")
        draw.text(
            (canvas.width - 180, canvas.height - 56),
            data["sn"], font=f_bold, fill="black"
        )

        # Text overlay — back side
        bx, y_addr = (canvas.width // 2) + 26, 234
        draw.text((bx, 71), data["phone"], font=f_bold, fill="black")
        for line in data["address"]:
            draw.text((bx, y_addr), line, font=f_amh, fill="black")
            y_addr += 40

        # Apply B&W to entire canvas if needed
        if mode == "bw":
            gray_canvas = canvas.convert("L")
            canvas = Image.merge("RGBA", (gray_canvas, gray_canvas, gray_canvas, canvas.split()[3]))

        canvas.convert("RGB").save(output_path, "PNG")
        return True

    except Exception as e:
        logger.error(f"ID generation error: {e}")
        return False


# ════════════════════════════════════════════════════
# 6. KEYBOARD BUILDERS
# ════════════════════════════════════════════════════
def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖨 Print ID Card",    callback_data="print_id")],
        [InlineKeyboardButton("💳 Buy Package",      callback_data="buy_package")],
        [InlineKeyboardButton("💰 My Balance",       callback_data="my_balance")],
        [InlineKeyboardButton("📞 Contact Support",  callback_data="contact_help")],
    ])

def package_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("40 birr  → 1 package",    callback_data="pkg_1")],
        [InlineKeyboardButton("500 birr  → 25 packages", callback_data="pkg_25")],
        [InlineKeyboardButton("1500 birr → 100 packages",callback_data="pkg_100")],
        [InlineKeyboardButton("2000 birr → 155 packages",callback_data="pkg_155")],
        [InlineKeyboardButton("⬅️ Back",                  callback_data="back_menu")],
    ])


# ════════════════════════════════════════════════════
# 7. HANDLERS
# ════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.message.from_user
    user_id = user.id
    uname   = user.username or user.first_name or ""
    # Ensure user is in DB
    add_credits(user_id, 0, uname)
    credits = get_credits(user_id)

    welcome = (
        "🎉 *Welcome to Fayda ID Processor Bot!*\n\n"
        "📄 This bot converts your *Fayda PDF* into a "
        "professional, print-ready ID card.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🇪🇹 *ፋይዳ ID Card Printer Bot*\n\n"
        "📑 *አጠቃቀም:*\n"
        "1️⃣  Fayda App ወይም Telebirr ላይ ያለዎትን PDF ያውርዱ\n"
        "2️⃣  ያን PDF ፋይል ወደዚህ Bot ይላኩ\n"
        "3️⃣  Color + B\\&W ID card ይቀበሉ 🎨\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Your Balance:* `{credits}` package(s)\n\n"
        "👇 Choose an option:"
    )
    await update.message.reply_text(
        welcome,
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    return MENU


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "ℹ️ *FaydaIDProcessor Bot — Help*\n\n"
        "*Commands:*\n"
        "/start  — Main menu\n"
        "/balance — Check your credits\n"
        "/help   — This message\n\n"
        "*How it works:*\n"
        "Send your Fayda PDF and the bot will:\n"
        "• Remove background from your photo\n"
        "• Extract all ID data\n"
        "• Generate Color & B\\&W print-ready ID cards\n\n"
        f"*Cost:* 1 credit per ID card\n\n"
        f"*Support:* @altleg"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    credits = get_credits(user_id)
    await update.message.reply_text(
        f"💰 *Your Balance:* `{credits}` package(s)",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )


async def give_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /give <user_id> <amount>"""
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /give <user_id> <amount>")
        return
    try:
        uid    = int(args[0])
        amount = int(args[1])
        add_credits(uid, amount)
        await update.message.reply_text(f"✅ Added {amount} credits to user `{uid}`.", parse_mode="Markdown")
        await context.bot.send_message(
            chat_id=uid,
            text=f"🎁 *{amount} credits* have been added to your account!\n💰 New balance: `{get_credits(uid)}`",
            parse_mode="Markdown"
        )
    except (ValueError, Exception) as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /stats"""
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("No users yet.")
        return
    lines = [f"📊 *Total Users: {len(users)}*\n"]
    for uid, creds, uname, joined in users[:20]:
        display = f"@{uname}" if uname else f"ID:{uid}"
        lines.append(f"• {display} — {creds} credits")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def button_tap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data    = query.data
    user_id = query.from_user.id

    if data == "buy_package":
        await query.edit_message_text(
            "💳 *Choose a package:*\n\n"
            "After payment, send the TeleBirr SMS receipt here.",
            parse_mode="Markdown",
            reply_markup=package_keyboard()
        )
        return BUY_PACK

    elif data == "print_id":
        await query.edit_message_text(
            "📤 Please send your *Fayda PDF* file now.\n\n"
            "_(Download from Fayda App or Telebirr → My ID → Download PDF)_",
            parse_mode="Markdown"
        )
        return MENU

    elif data == "my_balance":
        credits = get_credits(user_id)
        await query.edit_message_text(
            f"💰 *Your Balance:* `{credits}` package(s)",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return MENU

    elif data == "contact_help":
        await query.edit_message_text(
            "📞 *Support:* @altleg\n\nSend a message and we'll help you.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="back_menu")
            ]])
        )
        return MENU

    elif data == "back_menu":
        credits = get_credits(user_id)
        await query.edit_message_text(
            f"👇 *Main Menu*\n\n💰 Balance: `{credits}` package(s)",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return MENU


async def select_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pkg_map = {
        "pkg_1":   ("1",   "40"),
        "pkg_25":  ("25",  "500"),
        "pkg_100": ("100", "1500"),
        "pkg_155": ("155", "2000"),
    }
    if query.data not in pkg_map:
        return BUY_PACK
    amount, price = pkg_map[query.data]
    context.user_data["pending_pkg"] = amount
    await query.edit_message_text(
        f"💳 *Payment Instructions:*\n\n"
        f"Send *{price} Birr* to:\n"
        f"📱 TeleBirr: `{TELEBIRR_NUMBER}`\n\n"
        f"Then *copy and send the SMS receipt* here.\n"
        f"_Your {amount} package(s) will be activated after approval._",
        parse_mode="Markdown"
    )
    return WAIT_RECEIPT


async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.message.from_user
    uname   = f"@{user.username}" if user.username else user.first_name
    pending = context.user_data.get("pending_pkg", "?")

    admin_msg = (
        f"🔔 *New Payment Request*\n\n"
        f"👤 User: {uname}\n"
        f"🆔 ID: `{user.id}`\n"
        f"📦 Package: *{pending}*\n\n"
        f"📝 *Receipt:*\n{update.message.text}"
    )
    btns = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"appr_{user.id}_{pending}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"rej_{user.id}"),
    ]])
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID, text=admin_msg,
            reply_markup=btns, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to notify admin: {e}")

    await update.message.reply_text(
        "⏳ *Receipt sent for review!*\n\n"
        "You'll be notified once approved.\n"
        "_ደረሰኝዎ ለ Admin ተልኳል — ከፍቃዱ ኋላ ይነገርዎታል።_",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    return MENU


async def admin_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")

    if parts[0] == "appr" and len(parts) >= 3:
        uid    = int(parts[1])
        amount = int(parts[2])
        add_credits(uid, amount)
        new_balance = get_credits(uid)
        await context.bot.send_message(
            chat_id=uid,
            text=(
                f"✅ *Payment Approved!*\n\n"
                f"🎁 *{amount} package(s)* added.\n"
                f"💰 New Balance: `{new_balance}`\n\n"
                f"Now send your Fayda PDF to get your ID card!"
            ),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        await query.edit_message_text(f"✅ Approved — {amount} credits added to {uid}")

    elif parts[0] == "rej" and len(parts) >= 2:
        uid = int(parts[1])
        await context.bot.send_message(
            chat_id=uid,
            text=(
                "❌ *Payment Rejected.*\n\n"
                "Please check your receipt and try again, "
                "or contact @altleg for support."
            ),
            parse_mode="Markdown"
        )
        await query.edit_message_text(f"❌ Rejected for user {uid}")


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    credits = get_credits(user_id)

    # ── Credit check (fixed: was < 0, should be < 1) ──
    if credits < 1:
        await update.message.reply_text(
            "❌ *Insufficient balance!*\n\n"
            "You need at least 1 package to process an ID card.\n"
            "_ቀሪ ፓኬጅ የለዎትም — ከዚህ በታች ይግዙ:_",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return MENU

    proc_msg = await update.message.reply_text(
        "⏳ *Processing your Fayda ID...*\n\n"
        "_This may take 20-40 seconds (AI background removal)_",
        parse_mode="Markdown"
    )

    base_dir = os.path.dirname(os.path.abspath(__file__))
    pdf_path = os.path.join(base_dir, f"input_{user_id}.pdf")
    c_out    = os.path.join(base_dir, f"C_{user_id}.png")
    b_out    = os.path.join(base_dir, f"B_{user_id}.png")

    try:
        # Download PDF
        tg_file = await context.bot.get_file(update.message.document.file_id)
        await tg_file.download_to_drive(pdf_path)

        # Extract data (blocking — run in thread)
        data = await asyncio.to_thread(extract_data_from_pdf, pdf_path, user_id)

        if not data:
            await proc_msg.edit_text(
                "❌ *Could not read the PDF.*\n\n"
                "Make sure it's a valid Fayda PDF and try again."
            )
            return MENU

        # Generate ID cards (blocking — run in threads)
        ok_color = await asyncio.to_thread(generate_fayda_v3, data, c_out, user_id, "color")
        ok_bw    = await asyncio.to_thread(generate_fayda_v3, data, b_out, user_id, "bw")

        if not ok_color or not ok_bw:
            await proc_msg.edit_text(
                "❌ *ID card generation failed.*\n\n"
                "Template image missing. Contact @altleg"
            )
            return MENU

        # Send Color version
        with open(c_out, "rb") as f:
            await update.message.reply_document(
                f,
                filename="Fayda_Color.png",
                caption="🎨 *Color ID Card* — Print-ready",
                parse_mode="Markdown"
            )
        # Send B&W version
        with open(b_out, "rb") as f:
            await update.message.reply_document(
                f,
                filename="Fayda_BW.png",
                caption="🖤 *Black & White ID Card* — Print-ready",
                parse_mode="Markdown"
            )

        # Deduct credit
        add_credits(user_id, -1)
        new_balance = get_credits(user_id)
        await proc_msg.edit_text(
            f"✅ *Done! Both ID cards sent.*\n\n"
            f"💰 1 package deducted. Remaining: `{new_balance}`",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )

    except Exception as e:
        logger.error(f"handle_pdf error for user {user_id}: {e}")
        await proc_msg.edit_text(
            "❌ *An error occurred.*\n\nPlease try again or contact @altleg",
            parse_mode="Markdown"
        )

    finally:
        # Cleanup temp files
        for f in [pdf_path, c_out, b_out,
                  os.path.join(base_dir, f"photo_{user_id}.png"),
                  os.path.join(base_dir, f"qr_{user_id}.png"),
                  os.path.join(base_dir, f"fin_{user_id}.png")]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    return MENU


# ════════════════════════════════════════════════════
# 8. APP SETUP
# ════════════════════════════════════════════════════
init_db()

app = ApplicationBuilder().token(BOT_TOKEN).read_timeout(60).write_timeout(60).connect_timeout(60).pool_timeout(60).build()

# Conversation handler
conv = ConversationHandler(
    entry_points=[
        CommandHandler("start",   start),
        MessageHandler(filters.Document.PDF, handle_pdf),
    ],
    states={
        MENU: [
            MessageHandler(filters.Document.PDF, handle_pdf),
            CallbackQueryHandler(button_tap, pattern="^(print_id|buy_package|my_balance|contact_help|back_menu)$"),
        ],
        BUY_PACK: [
            CallbackQueryHandler(select_package, pattern="^pkg_"),
            CallbackQueryHandler(button_tap,     pattern="^back_menu$"),
        ],
        WAIT_RECEIPT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_receipt),
        ],
    },
    fallbacks=[CommandHandler("start", start)],
    allow_reentry=True,
)

app.add_handler(conv)
app.add_handler(CommandHandler("help",    help_cmd))
app.add_handler(CommandHandler("balance", balance_cmd))
app.add_handler(CommandHandler("give",    give_cmd))
app.add_handler(CommandHandler("stats",   stats_cmd))
app.add_handler(CallbackQueryHandler(admin_approval, pattern=r"^(appr|rej)_"))

# ════════════════════════════════════════════════════
# 9. ENTRY POINT
# ════════════════════════════════════════════════════
async def setup_webhook():
    URL = os.environ.get("RENDER_EXTERNAL_URL")
    if URL:
        await app.bot.set_webhook(url=f"{URL}/webhook")
        logger.info(f"🌐 Webhook set → {URL}/webhook")

# Auto-setup webhook on Render
if os.environ.get("RENDER_EXTERNAL_URL"):
    import asyncio as _aio
    loop = _aio.new_event_loop()
    threading.Thread(
        target=lambda: loop.run_until_complete(app.initialize()),
        daemon=True
    ).start()

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 10000))
    URL  = os.environ.get("RENDER_EXTERNAL_URL")
    if URL:
        logger.info(f"🌐 Webhook mode → {URL}/webhook")
        flask_app.run(host="0.0.0.0", port=PORT)
    else:
        logger.info("🖥️  Local polling mode...")
        app.run_polling(drop_pending_updates=True)
