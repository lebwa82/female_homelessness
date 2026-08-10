from app.service import DELIVERY_OPTIONS, HELP_OPTIONS, MAIN_OPTIONS


def test_every_backend_choice_has_a_button_label() -> None:
    assert MAIN_OPTIONS == (
        "Продукты и гигиена",
        "Безопасное место / специалистка",
        "Вопрос о документах",
        "Другое",
    )
    assert DELIVERY_OPTIONS == ("Самовывоз в ПВЗ", "Электронный сертификат", "Связаться со специалисткой")
    assert HELP_OPTIONS == ("Получить базовую помощь", "Связаться со специалисткой")
