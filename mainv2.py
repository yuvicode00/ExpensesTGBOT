#!/usr/bin/env python
import os
import csv
import io
import logging
import datetime
import random

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
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

# Conversation states
EDIT_AMOUNT = 1
JOIN_WALLET = 2

# Global dictionaries for temporary data
user_edit_data = {}
user_wallet_context = {}  # Maps user_id to wallet_id (if set)


# ---------------------------
# Models
# ---------------------------
class Expense(Base):
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)
    category = Column(String)
    amount = Column(Float)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    wallet_id = Column(Integer, index=True, nullable=True)  # If None, then personal expense


class Wallet(Base):
    __tablename__ = "wallets"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    owner_id = Column(Integer)


class WalletMember(Base):
    __tablename__ = "wallet_members"
    id = Column(Integer, primary_key=True)
    wallet_id = Column(Integer)
    user_id = Column(Integer)


# Create tables (including new wallet tables)
Base.metadata.create_all(engine)


# ---------------------------
# Language Handling
# ---------------------------
def get_language(update: Update):
    """Detects the user's language (default: English)."""
    lang = update.effective_user.language_code
    return "he" if lang and lang.startswith("he") else "en"


def translate_text(texts, lang):
    """Returns the correct language text."""
    return texts["he"] if lang == "he" else texts["en"]


# ---------------------------
# Helper Functions
# ---------------------------
def get_current_wallet(user_id: int):
    """Returns the wallet id if the user has set a wallet context."""
    return user_wallet_context.get(user_id)


def is_user_in_wallet(session, wallet_id, user_id):
    """Checks if the user is a member of the given wallet."""
    member = session.query(WalletMember).filter(
        WalletMember.wallet_id == wallet_id, WalletMember.user_id == user_id
    ).first()
    return member is not None


# ---------------------------
# Telegram Bot Handlers
# ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_language(update)
    texts = {
        "en": (
            "ברוכים הבאים! שלחו הוצאה כמו: ספרים 50\n\n"
            "פקודות:\n"
            "כתוב 'דוח יומי' - דוח יומי \n"
            "כתוב 'דוח חודשי' - דוח חודשי\n"
            "כתבו 'שיתוף' - אפשרויות ארנק משותף\n"
            "/export - ייצוא ל-אקסל\n"
            "/archive - צפייה בכל העסקאות\n"
            "/leave - יציאה מארנק נוכחי"
        ),
        "he": (
            "ברוכים הבאים! שלחו הוצאה כמו: ספרים 50\n\n"
            "פקודות:\n"
            "כתוב 'דוח יומי' - דוח יומי \n"
            "כתוב 'דוח חודשי' - דוח חודשי\n"
            "כתבו 'שיתוף' - אפשרויות ארנק משותף\n"
            "/export - ייצוא ל-אקסל\n"
            "/archive - צפייה בכל העסקאות\n"
            "/leave - יציאה מארנק נוכחי"
        ),
    }
    await update.message.reply_text(translate_text(texts, lang))


async def expense_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles expense entry. Accepts both "Category-Amount" and "Category Amount" formats.
    Also, if the user sends "shared" (case-insensitive), shows wallet sharing options.
    """
    text = update.message.text.strip()
    lower_text = text.lower()

    # Check if the user wants wallet sharing options
    if lower_text == "שיתוף":
        keyboard = [
            [InlineKeyboardButton("Create Wallet", callback_data="shared_create")],
            [InlineKeyboardButton("Join Wallet", callback_data="shared_join")],
        ]
        await update.message.reply_text(
            "Choose an option for shared wallet:", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # Check for report commands typed as text
    if lower_text in ["דוח יומי", "daily report", "daily"]:
        await report_handler(update, context, period="daily")
        return
    if lower_text in ["דוח חודשי", "monthly report", "monthly"]:
        await report_handler(update, context, period="monthly")
        return

    try:
        # Allow both formats: with a dash or with a space separator.
        if '-' in text:
            category, amount_text = text.split('-', 1)
        else:
            tokens = text.split()
            if len(tokens) < 2:
                await update.message.reply_text(
                    "Wrong format. Use: Category-Amount or Category Amount (e.g., Books 50)"
                )
                return
            amount_text = tokens[-1]
            category = " ".join(tokens[:-1])

        # Clean the amount (remove any non-digit or non-dot characters)
        amount_clean = "".join(filter(lambda c: c.isdigit() or c == ".", amount_text))
        amount = float(amount_clean)

        session = Session()
        user_id = update.effective_user.id
        wallet_id = get_current_wallet(user_id)

        # Create a new expense. If a wallet is set, record it there; otherwise, record it as a personal expense.
        expense = Expense(
            user_id=user_id,
            category=category.strip(),
            amount=amount,
            wallet_id=wallet_id,
        )
        session.add(expense)
        session.commit()
        session.close()

        context_text = f"Recorded in wallet {wallet_id}" if wallet_id else "Recorded"
        await update.message.reply_text(f"{context_text}: {category.strip()} - {amount}₪")
    except Exception as e:
        logger.error("Error parsing expense: %s", e)
        await update.message.reply_text("Error processing expense.")


async def report_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, period: str) -> None:
    """
    Generates a daily or monthly report. Expenses are merged (summed) by category, but detailed entries
    can be seen in the breakdown.
    """
    session = Session()
    user_id = update.effective_user.id
    wallet_id = get_current_wallet(user_id)
    now = datetime.datetime.utcnow()

    if period == "daily":
        start_time = datetime.datetime(now.year, now.month, now.day)
        date_str = start_time.strftime("%Y-%m-%d")
        report_title = f"📅 Daily Report for {date_str}"
    elif period == "monthly":
        start_time = datetime.datetime(now.year, now.month, 1)
        date_str = start_time.strftime("%Y-%m")
        report_title = f"📅 Monthly Report for {date_str}"
    else:
        session.close()
        return

    if wallet_id:
        expenses = session.query(Expense).filter(
            Expense.wallet_id == wallet_id, Expense.timestamp >= start_time
        ).all()
    else:
        expenses = session.query(Expense).filter(
            Expense.user_id == user_id, Expense.wallet_id == None, Expense.timestamp >= start_time
        ).all()
    session.close()

    if not expenses:
        await update.message.reply_text("No expenses found for this period.")
        return

    # Merge expenses by category (expense merging for the summary)
    category_totals = {}
    for exp in expenses:
        category_totals[exp.category] = category_totals.get(exp.category, 0) + exp.amount
    total_spent = sum(category_totals.values())

    report_text = f"{report_title}\n\n📂 Categories:\n"
    for category, total in category_totals.items():
        report_text += f"🔹 {category}: {total}₪\n"
    report_text += f"\n💰 *Total:* {total_spent}₪"

    # Create buttons for detailed category breakdown
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
    """
    Shows all individual expenses for a selected category with options to edit or delete.
    """
    query = update.callback_query
    await query.answer()
    category = query.data[4:]

    session = Session()
    user_id = query.from_user.id
    wallet_id = get_current_wallet(user_id)
    if wallet_id:
        expenses = session.query(Expense).filter(
            Expense.wallet_id == wallet_id, Expense.category == category
        ).all()
    else:
        expenses = session.query(Expense).filter(
            Expense.user_id == user_id, Expense.wallet_id == None, Expense.category == category
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
    """
    Initiates editing an expense amount.
    """
    query = update.callback_query
    await query.answer()

    expense_id = int(query.data.split("_")[1])
    user_edit_data[query.from_user.id] = expense_id  # Save expense id for this user

    session = Session()
    expense = session.query(Expense).filter(Expense.id == expense_id).first()
    if not expense:
        await query.message.reply_text("❌ Expense not found.")
        session.close()
        return ConversationHandler.END

    category = expense.category
    amount = expense.amount
    session.close()

    await query.message.reply_text(f"✏ Edit amount for: {category} - {amount}₪")
    return EDIT_AMOUNT


async def update_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Updates the expense amount.
    """
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

        expense.amount = new_amount
        session.commit()
        category = expense.category
        session.close()

        del user_edit_data[user_id]
        await update.message.reply_text(f"✅ Updated expense: {category} - {new_amount}₪")
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Please send a number.")
    return ConversationHandler.END


async def delete_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Deletes an expense.
    """
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
    """
    Exports expenses as a CSV file.
    """
    session = Session()
    user_id = update.effective_user.id
    wallet_id = get_current_wallet(user_id)
    if wallet_id:
        expenses = session.query(Expense).filter(Expense.wallet_id == wallet_id).all()
    else:
        expenses = session.query(Expense).filter(Expense.user_id == user_id, Expense.wallet_id == None).all()
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
    await update.message.reply_document(
        document=csv_file,
        filename="expenses.csv",
        caption="Here is your CSV file."
    )


async def archive_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Shows all past transactions (archive).
    """
    session = Session()
    user_id = update.effective_user.id
    wallet_id = get_current_wallet(user_id)
    if wallet_id:
        expenses = session.query(Expense).filter(Expense.wallet_id == wallet_id).order_by(
            Expense.timestamp.desc()).all()
    else:
        expenses = session.query(Expense).filter(Expense.user_id == user_id, Expense.wallet_id == None).order_by(
            Expense.timestamp.desc()).all()
    session.close()

    if not expenses:
        await update.message.reply_text("No transactions found.")
        return

    text = "🗃 *Transaction Archive:*\n\n"
    for exp in expenses:
        text += f"{exp.timestamp.strftime('%Y-%m-%d %H:%M')} - {exp.category}: {exp.amount}₪\n"
    await update.message.reply_text(text, parse_mode="Markdown")


# ---------------------------
# Shared Wallet Inline Handlers
# ---------------------------
async def shared_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Creates a new shared wallet with a random ID and sets it as the current wallet.
    """
    query = update.callback_query
    await query.answer()

    session = Session()
    # Generate a random wallet ID between 10000 and 99999 ensuring uniqueness
    while True:
        random_id = random.randint(10000, 99999)
        exists = session.query(Wallet).filter(Wallet.id == random_id).first()
        if not exists:
            break

    wallet = Wallet(id=random_id, name=f"Wallet {random_id}", owner_id=query.from_user.id)
    session.add(wallet)
    session.commit()
    # Add creator as member
    member = WalletMember(wallet_id=wallet.id, user_id=query.from_user.id)
    session.add(member)
    session.commit()
    session.close()
    user_wallet_context[query.from_user.id] = wallet.id
    await query.message.reply_text(
        f"✅ Created wallet 'Wallet {wallet.id}' with ID {wallet.id}. It is now set as your current wallet."
    )


async def join_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Initiates the join wallet conversation by prompting the user to enter a wallet ID.
    """
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Please enter the Wallet ID to join:")
    return JOIN_WALLET


async def join_wallet_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Processes the wallet ID entered by the user and joins the wallet if found.
    """
    user_id = update.effective_user.id
    wallet_id_text = update.message.text.strip()
    try:
        wallet_id = int(wallet_id_text)
    except ValueError:
        await update.message.reply_text("❌ Invalid Wallet ID. Please enter a number.")
        return JOIN_WALLET
    session = Session()
    wallet = session.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not wallet:
        await update.message.reply_text("❌ Wallet not found. Please check the ID and try again.")
        session.close()
        return ConversationHandler.END
    if is_user_in_wallet(session, wallet_id, user_id):
        await update.message.reply_text("ℹ️ You are already a member of this wallet.")
    else:
        member = WalletMember(wallet_id=wallet_id, user_id=user_id)
        session.add(member)
        session.commit()
        await update.message.reply_text(f"✅ Joined wallet '{wallet.name}' (ID {wallet_id}).")
    session.close()
    user_wallet_context[user_id] = wallet_id
    return ConversationHandler.END


async def leave_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Leaves (clears) your current wallet context.
    """
    user_id = update.effective_user.id
    if user_id in user_wallet_context:
        del user_wallet_context[user_id]
        await update.message.reply_text("Left the current wallet context.")
    else:
        await update.message.reply_text("No wallet context to leave.")


# ---------------------------
# Main function to run the bot
# ---------------------------
def main():
    application = ApplicationBuilder().token("8160529510:AAE_6jaP1RR_77pF-imzRwSoYDUuFwksz-w").build()

    # Conversation handler for editing an expense
    edit_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_expense, pattern="^edit_")],
        states={EDIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_expense)]},
        fallbacks=[],
    )

    # Conversation handler for joining a wallet
    join_wallet_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(join_wallet_start, pattern="^shared_join$")],
        states={
            JOIN_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, join_wallet_id)]
        },
        fallbacks=[],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("export", export_handler))
    application.add_handler(CommandHandler("archive", archive_handler))
    application.add_handler(CommandHandler("leave", leave_wallet))
    application.add_handler(CallbackQueryHandler(category_breakdown, pattern="^cat_"))
    application.add_handler(CallbackQueryHandler(delete_expense, pattern="^delete_"))
    application.add_handler(CallbackQueryHandler(shared_create, pattern="^shared_create$"))
    application.add_handler(edit_conv_handler)
    application.add_handler(join_wallet_conv_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, expense_handler))

    logger.info("Bot is running...")
    application.run_polling()


if __name__ == "__main__":
    main()
