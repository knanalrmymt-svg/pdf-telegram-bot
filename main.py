import io
import os
import logging
from typing import Dict, List

import telebot
from telebot import types
from PIL import Image
from pypdf import PdfWriter, PdfReader

from keep_alive import keep_alive

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pdf-bot")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Add it via the environment secrets manager.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ------------------------------------------------------------------
# In-memory per-chat session state.
# mode: None | "images" | "pdfs"
# items: collected image bytes (for "images") or pdf bytes (for "pdfs")
# ------------------------------------------------------------------
sessions: Dict[int, Dict] = {}


def get_session(chat_id: int) -> Dict:
    if chat_id not in sessions:
        sessions[chat_id] = {"mode": None, "items": []}
    return sessions[chat_id]


def reset_session(chat_id: int) -> None:
    sessions[chat_id] = {"mode": None, "items": []}


WELCOME_TEXT = (
    "👋 أهلاً بك في بوت تحويل ودمج ملفات PDF!\n\n"
    "الأوامر المتاحة:\n"
    "📷 /convert — تحويل مجموعة صور إلى ملف PDF واحد\n"
    "📎 /merge — دمج عدة ملفات PDF في ملف واحد\n"
    "✅ /done — إنهاء الجلسة الحالية ومعالجة الملفات\n"
    "❌ /cancel — إلغاء العملية الحالية\n\n"
    "ابدأ بإرسال أحد الأمرين أعلاه، ثم أرسل الملفات واحدًا تلو الآخر، وفي النهاية أرسل /done."
)


@bot.message_handler(commands=["start", "help"])
def send_welcome(message: types.Message):
    reset_session(message.chat.id)
    bot.reply_to(message, WELCOME_TEXT)


@bot.message_handler(commands=["cancel"])
def cancel(message: types.Message):
    reset_session(message.chat.id)
    bot.reply_to(message, "تم إلغاء العملية الحالية. يمكنك البدء من جديد بـ /convert أو /merge.")


@bot.message_handler(commands=["convert"])
def start_convert(message: types.Message):
    session = get_session(message.chat.id)
    session["mode"] = "images"
    session["items"] = []
    bot.reply_to(
        message,
        "📷 أرسل الآن الصور التي تريد تحويلها إلى PDF، واحدة تلو الأخرى.\n"
        "عند الانتهاء أرسل /done لإنشاء الملف، أو /cancel للإلغاء.",
    )


@bot.message_handler(commands=["merge"])
def start_merge(message: types.Message):
    session = get_session(message.chat.id)
    session["mode"] = "pdfs"
    session["items"] = []
    bot.reply_to(
        message,
        "📎 أرسل الآن ملفات PDF التي تريد دمجها، ملفًا تلو الآخر (بترتيب الدمج المطلوب).\n"
        "عند الانتهاء أرسل /done لإنشاء الملف المدمج، أو /cancel للإلغاء.",
    )


@bot.message_handler(commands=["done"])
def finish(message: types.Message):
    chat_id = message.chat.id
    session = get_session(chat_id)
    mode = session["mode"]
    items: List[bytes] = session["items"]

    if mode is None or not items:
        bot.reply_to(
            message,
            "لا توجد جلسة نشطة أو لم ترسل أي ملفات بعد.\nابدأ بـ /convert لتحويل صور أو /merge لدمج ملفات PDF.",
        )
        return

    try:
        if mode == "images":
            bot.reply_to(message, f"⏳ جاري تحويل {len(items)} صورة إلى PDF...")
            output = images_to_pdf(items)
            filename = "converted.pdf"
        else:
            bot.reply_to(message, f"⏳ جاري دمج {len(items)} ملف PDF...")
            output = merge_pdfs(items)
            filename = "merged.pdf"

        bot.send_document(chat_id, (filename, output), caption="✅ تم! تفضل ملفك.")
    except Exception:
        logger.exception("Failed to process files for chat %s", chat_id)
        bot.reply_to(message, "⚠️ حدث خطأ أثناء معالجة الملفات. تأكد أن الملفات صالحة وحاول مرة أخرى.")
    finally:
        reset_session(chat_id)


def images_to_pdf(image_bytes_list: List[bytes]) -> bytes:
    images = []
    for data in image_bytes_list:
        img = Image.open(io.BytesIO(data))
        if img.mode != "RGB":
            img = img.convert("RGB")
        images.append(img)

    buffer = io.BytesIO()
    first, rest = images[0], images[1:]
    first.save(buffer, format="PDF", save_all=True, append_images=rest)
    buffer.seek(0)
    return buffer.read()


def merge_pdfs(pdf_bytes_list: List[bytes]) -> bytes:
    writer = PdfWriter()
    for data in pdf_bytes_list:
        reader = PdfReader(io.BytesIO(data))
        for page in reader.pages:
            writer.add_page(page)

    buffer = io.BytesIO()
    writer.write(buffer)
    buffer.seek(0)
    return buffer.read()


@bot.message_handler(content_types=["photo"])
def handle_photo(message: types.Message):
    session = get_session(message.chat.id)
    if session["mode"] != "images":
        bot.reply_to(message, "أرسل أولًا الأمر /convert لبدء تحويل الصور إلى PDF.")
        return

    file_id = message.photo[-1].file_id
    file_info = bot.get_file(file_id)
    data = bot.download_file(file_info.file_path)
    session["items"].append(data)
    bot.reply_to(message, f"✅ تم استلام الصورة رقم {len(session['items'])}. أرسل المزيد أو /done للإنهاء.")


@bot.message_handler(content_types=["document"])
def handle_document(message: types.Message):
    session = get_session(message.chat.id)
    doc = message.document
    mime = (doc.mime_type or "").lower()
    filename = (doc.file_name or "").lower()

    file_info = bot.get_file(doc.file_id)
    data = bot.download_file(file_info.file_path)

    if session["mode"] == "pdfs":
        if mime != "application/pdf" and not filename.endswith(".pdf"):
            bot.reply_to(message, "⚠️ هذا ليس ملف PDF. أرسل ملفات PDF فقط أثناء عملية الدمج.")
            return
        session["items"].append(data)
        bot.reply_to(message, f"✅ تم استلام الملف رقم {len(session['items'])}. أرسل المزيد أو /done للإنهاء.")
    elif session["mode"] == "images":
        if mime.startswith("image/"):
            session["items"].append(data)
            bot.reply_to(message, f"✅ تم استلام الصورة رقم {len(session['items'])}. أرسل المزيد أو /done للإنهاء.")
        else:
            bot.reply_to(message, "⚠️ أرسل صورًا فقط أثناء عملية التحويل إلى PDF.")
    else:
        bot.reply_to(message, "أرسل أولًا /convert لتحويل صور أو /merge لدمج ملفات PDF.")


@bot.message_handler(func=lambda message: True, content_types=["text"])
def echo_all(message: types.Message):
    bot.reply_to(
        message,
        "لم أفهم هذا الأمر. استخدم /convert لتحويل صور إلى PDF، أو /merge لدمج ملفات PDF، أو /help للمساعدة.",
    )


if __name__ == "__main__":
    keep_alive()
    logger.info("🤖 البوت يعمل الآن بنجاح...")
    bot.infinity_polling()
