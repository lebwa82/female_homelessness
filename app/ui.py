from __future__ import annotations

from app.catalog import get_aid_item
from app.domain import Choice, ChoiceSet, ContactMethod, NeedKind

HUMAN_CHOICE = Choice(id="human", label="Поговорить с живым человеком")

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

SAFE_CONTINUE_CHOICES = (
    Choice(id="continue_bot", label="Продолжить здесь"),
    HUMAN_CHOICE,
)

PSYCHOLOGIST_INTEREST_CHOICES = (
    Choice(id="support:psychologist", label="Хочу поговорить с психологом"),
    HUMAN_CHOICE,
)

_CHOICES_BY_SET = {
    ChoiceSet.NONE: (HUMAN_CHOICE,),
    ChoiceSet.SAFE_CONTINUE: SAFE_CONTINUE_CHOICES,
    ChoiceSet.NEED_CATEGORIES: (*NEED_CHOICES, HUMAN_CHOICE),
    ChoiceSet.PSYCHOLOGIST_INTEREST: PSYCHOLOGIST_INTEREST_CHOICES,
    ChoiceSet.CONTACT_METHODS: (*CONTACT_CHOICES, HUMAN_CHOICE),
    ChoiceSet.MORE_HELP: (*MORE_HELP_CHOICES, HUMAN_CHOICE),
}


def choices_for(
    choice_set: ChoiceSet,
    catalog_item_ids: tuple[str, ...] = (),
) -> tuple[Choice, ...]:
    """Render only backend-owned callback IDs for a symbolic choice set."""
    if choice_set is ChoiceSet.AID_CATALOG:
        choices = tuple(
            Choice(id=f"aid:{item.id}", label=item.label)
            for item_id in catalog_item_ids
            if (item := get_aid_item(item_id)) is not None
        )
        return (*choices, HUMAN_CHOICE)
    return _CHOICES_BY_SET[choice_set]
