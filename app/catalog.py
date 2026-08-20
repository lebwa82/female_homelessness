from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.domain import NeedKind


class AidItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    label: str
    description: str
    needs_location: bool = False


AID_CATALOG = (
    AidItem(
        id="hostel_3_nights",
        label="Три ночи в хостеле",
        description="Бронирование трёх ночей через фонд.",
        needs_location=True,
    ),
    AidItem(
        id="legal_consultation",
        label="Юрист или адвокат",
        description="Онлайн-консультация по документам и правам.",
    ),
    AidItem(
        id="psychologist_3_sessions",
        label="Три встречи с психологом",
        description="Три онлайн-встречи через партнёрскую платформу.",
    ),
    AidItem(
        id="food_card",
        label="Карточка на продукты",
        description="Электронный сертификат на продукты.",
    ),
    AidItem(
        id="children_card",
        label="Карточка на товары для детей",
        description="Электронный сертификат на детские товары.",
    ),
    AidItem(
        id="transport_payment",
        label="Оплата проезда",
        description="Городской транспорт или билет в другой город.",
        needs_location=True,
    ),
    AidItem(
        id="peer_consultation",
        label="Разговор с равной консультанткой",
        description="Разговор с женщиной с похожим опытом.",
    ),
    AidItem(
        id="addiction_specialist",
        label="Специалист по зависимости",
        description="Онлайн-консультация специалиста по зависимости.",
    ),
)

_BY_ID = {item.id: item for item in AID_CATALOG}
_BY_NEED = {
    NeedKind.HOUSING: ("hostel_3_nights", "peer_consultation", "legal_consultation"),
    NeedKind.FOOD_MONEY: ("food_card", "children_card", "transport_payment"),
    NeedKind.LEGAL: ("legal_consultation", "peer_consultation", "psychologist_3_sessions"),
    NeedKind.SUPPORT: (
        "peer_consultation",
        "psychologist_3_sessions",
        "addiction_specialist",
    ),
    NeedKind.CHILDREN: ("children_card", "legal_consultation", "peer_consultation"),
    NeedKind.OTHER: (),
}


def get_aid_item(aid_id: str) -> AidItem | None:
    return _BY_ID.get(aid_id)


def available_aid_for_need(need: NeedKind) -> tuple[AidItem, ...]:
    return tuple(_BY_ID[aid_id] for aid_id in _BY_NEED[need])

