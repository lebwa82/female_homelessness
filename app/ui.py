from __future__ import annotations

from app.domain import Choice, ContactMethod, NeedKind

CONTINUE_CHOICES = (
    Choice(id="continue", label="Да"),
    Choice(id="pause", label="Не сейчас"),
)

NEED_CHOICES = (
    Choice(id=f"need:{NeedKind.HOUSING.value}", label="Жильё / некуда идти"),
    Choice(id=f"need:{NeedKind.FOOD_MONEY.value}", label="Еда или деньги"),
    Choice(id=f"need:{NeedKind.LEGAL.value}", label="Документы / юридический вопрос"),
    Choice(id=f"need:{NeedKind.SUPPORT.value}", label="Поговорить / нужна поддержка"),
    Choice(id=f"need:{NeedKind.CHILDREN.value}", label="Вопрос про детей"),
    Choice(id=f"need:{NeedKind.OTHER.value}", label="Что-то другое"),
)

CONTACT_CHOICES = (
    Choice(id=f"contact:{ContactMethod.CURRENT_TELEGRAM.value}", label="Передать этот Telegram"),
    Choice(id=f"contact:{ContactMethod.OTHER_TELEGRAM.value}", label="Другой Telegram"),
    Choice(id=f"contact:{ContactMethod.PHONE.value}", label="Телефон"),
    Choice(id=f"contact:{ContactMethod.EMAIL.value}", label="Email"),
    Choice(id=f"contact:{ContactMethod.LATER.value}", label="Позже"),
)

MORE_HELP_CHOICES = (
    Choice(id="more_help", label="Нужно что-то ещё"),
    Choice(id="finish", label="Пока достаточно"),
)

FOLLOWUP_CHOICES = (
    Choice(id="followup:better", label="Лучше"),
    Choice(id="followup:same", label="Примерно так же"),
    Choice(id="followup:worse", label="Сложнее"),
)

LEVEL_TWO_CHOICES = (
    Choice(id="level2:details", label="Да, расскажите"),
    Choice(id="level2:later", label="Позже"),
    Choice(id="finish", label="Нет, спасибо"),
)
