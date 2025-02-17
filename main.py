#!/usr/bin/env python
import os
import csv
import io
import logging
import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------
# SQLAlchemy setup
# ---------------------------
engine = create_engine("sqlite:///expenses.db", echo=False)
# Set expire_on_commit=False so that after session.commit() the attributes remain accessible
Session = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()
EDIT_AMOUNT = 1

# Dictionary to store temporary data for editing
user_edit_data = {}

class Expense(Base):
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)
    category = Column(String)
    amount = Column(Float)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

Base.metadata.create_all(engine)

# ---------------------------
# Language Handling
# ---------------------------
def get_language(update: Update):
    """Detects the user's language (default: English)."""
    lang = update.effective_user.language_code
    return "he" if lang.startswith("he") else "en"

def translate_text(texts, lang):
    """Returns the correct language text."""
    return texts["he"] if lang == "he" else texts["en"]

# ---------------------------
# Telegram Bot Handlers
# ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_language(update)
    texts = {
        "en": "Welcome! Just send an expense like: Books-50₪\n\n"
              "Commands:\n"
              "Type 'Daily Report' - Daily summary\n"
              "Type 'Monthly Report' - Monthly summary\n"
              "/export - Export to CSV",
        "he": "שלום! פשוט שלח הוצאה כמו: ספרים-50₪\n\n"
              "פקודות:\n"
              "כתוב 'דוח יומי' - דוח יומי\n"
              "כתוב 'דוח חודשי' - דוח חודשי\n"
              "/export - ייצוא ל-CSV"
    }
    await update.message.reply_text(translate_text(texts, lang))


async def expense_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles text messages, parses expenses, and saves them to the database."""
    text = update.message.text.lower().strip()

    if text in ["דוח יומי", "דוח חודשי", "daily report", "monthly report"]:
        await report_handler(update, context, period=text)
        return

    if '-' not in text:
        await update.message.reply_text("Wrong format: Category-Amount (e.g., Books-50₪)")
        return

    try:
        category, amount_text = text.split('-', 1)
        amount_clean = "".join(filter(lambda c: c.isdigit() or c == ".", amount_text))
        amount = float(amount_clean)

        session = Session()
        expense = Expense(
            user_id=update.effective_user.id,
            category=category.strip(),
            amount=amount,
        )
        session.add(expense)
        session.commit()
        session.close()

        await update.message.reply_text(f"Recorded: {category.strip()} - {amount}₪")
    except Exception as e:
        logger.error("Error parsing expense: %s", e)
        await update.message.reply_text("Error processing expense.")


async def report_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, period: str) -> None:
    """Generates a structured daily or monthly report with an overview first."""
    session = Session()
    user_id = update.effective_user.id
    now = datetime.datetime.utcnow()

    if period in ["דוח יומי", "daily report"]:
        start_time = datetime.datetime(now.year, now.month, now.day)
        report_title = "📅 דוח יומי" if period == "דוח יומי" else "📅 Daily Report"
    elif period in ["דוח חודשי", "monthly report"]:
        start_time = datetime.datetime(now.year, now.month, 1)
        report_title = "📅 דוח חודשי" if period == "דוח חודשי" else "📅 Monthly Report"
    else:
        session.close()
        return

    expenses = session.query(Expense).filter(
        Expense.user_id == user_id, Expense.timestamp >= start_time
    ).all()
    session.close()

    if not expenses:
        await update.message.reply_text("No expenses found for this period.")
        return

    # Summarize expenses by category
    category_totals = {}
    for exp in expenses:
        category_totals[exp.category] = category_totals.get(exp.category, 0) + exp.amount

    total_spent = sum(category_totals.values())

    # Format the report
    report_text = f"{report_title}\n\n📂 Categories:\n"
    for category, total in category_totals.items():
        report_text += f"🔹 {category}: {total}₪\n"
    report_text += f"\n💰 *Total:* {total_spent}₪"

    # Create buttons for category breakdown
    keyboard = [
        [InlineKeyboardButton(text=cat, callback_data=f"cat_{cat}")]
        for cat in category_totals
    ]

    await update.message.reply_text(
        text=report_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def category_breakdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows all expenses for a selected category with edit and delete options."""
    query = update.callback_query
    await query.answer()
    category = query.data[4:]

    session = Session()
    expenses = session.query(Expense).filter(
        Expense.user_id == query.from_user.id, Expense.category == category
    ).all()
    session.close()

    if not expenses:
        await query.message.reply_text(f"No expenses found for {category}.")
        return

    text = f"📂 *{category} Breakdown:*\n----------------\n"
    keyboard = []

    for exp in expenses:
        text += f"🕒 {exp.timestamp.strftime('%Y-%m-%d %H:%M')} - {exp.amount}₪\n"
        keyboard.append([
            InlineKeyboardButton(f"✏ Edit {exp.amount}₪", callback_data=f"edit_{exp.id}"),
            InlineKeyboardButton("❌ Delete", callback_data=f"delete_{exp.id}")
        ])

    await query.message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def edit_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles editing an expense amount."""
    query = update.callback_query
    await query.answer()

    expense_id = int(query.data.split("_")[1])
    user_edit_data[query.from_user.id] = expense_id  # Store expense ID for user

    session = Session()
    expense = session.query(Expense).filter(Expense.id == expense_id).first()

    if not expense:
        await query.message.reply_text("❌ Expense not found.")
        session.close()
        return ConversationHandler.END

    # Retrieve needed attributes before closing session.
    category = expense.category
    amount = expense.amount
    session.close()

    await query.message.reply_text(f"✏ Edit amount for: {category} - {amount}₪")
    return EDIT_AMOUNT


async def update_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Updates the expense amount in the database."""
    user_id = update.effective_user.id

    if user_id not in user_edit_data:
        await update.message.reply_text("❌ No expense selected for editing.")
        return ConversationHandler.END

    new_amount_text = update.message.text.strip()

    try:
        new_amount = float(new_amount_text)

        session = Session()
        expense_id = user_edit_data[user_id]
        expense = session.query(Expense).filter(Expense.id == expense_id).first()

        if not expense:
            await update.message.reply_text("❌ Expense not found.")
            session.close()
            return ConversationHandler.END

        # Update the existing expense
        expense.amount = new_amount
        session.commit()
        # Retrieve the category before closing, if needed.
        category = expense.category
        session.close()

        # Clear user state
        del user_edit_data[user_id]

        await update.message.reply_text(f"✅ Updated expense: {category} - {new_amount}₪")
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Please send a number.")

    return ConversationHandler.END


async def delete_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deletes an expense after confirmation."""
    query = update.callback_query
    await query.answer()

    expense_id = int(query.data.split("_")[1])

    session = Session()
    expense = session.query(Expense).filter(Expense.id == expense_id).first()

    if not expense:
        await query.message.reply_text("❌ Expense not found.")
        session.close()
        return

    session.delete(expense)
    session.commit()
    session.close()

    await query.message.reply_text(f"🗑 Deleted expense: {expense.category} - {expense.amount}₪")


async def export_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exports expenses as a CSV file."""
    session = Session()
    expenses = session.query(Expense).filter(Expense.user_id == update.effective_user.id).all()
    session.close()

    if not expenses:
        await update.message.reply_text("No expenses to export.")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Category", "Amount", "Timestamp"])
    for exp in expenses:
        writer.writerow([exp.id, exp.category, exp.amount, exp.timestamp.strftime("%Y-%m-%d %H:%M")])
    output.seek(0)

    csv_file = io.BytesIO(output.getvalue().encode("utf-8"))
    csv_file.name = "expenses.csv"
    await update.message.reply_document(document=csv_file, filename="expenses.csv", caption="Here is your CSV file.")


def main():
    """Runs the bot."""
    application = ApplicationBuilder().token("8160529510:AAE_6jaP1RR_77pF-imzRwSoYDUuFwksz-w").build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_expense, pattern="^edit_")],
        states={EDIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_expense)]},
        fallbacks=[],
    )

    # Add conv_handler before the generic MessageHandler
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("export", export_handler))
    application.add_handler(CallbackQueryHandler(category_breakdown, pattern="^cat_"))
    application.add_handler(CallbackQueryHandler(delete_expense, pattern="^delete_"))
    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, expense_handler))

    logger.info("Bot is running...")
    application.run_polling()


if __name__ == "__main__":
    main()
