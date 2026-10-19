"""Local, bounded patterns for Russian identifiers and explicitly marked secrets."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class SensitiveMatch:
    start: int
    end: int
    entity_type: str


_SEPARATOR = r"[\s:№=#-]*"


def _digits(count: int) -> str:
    return rf"[0-9](?:[ \u00a0\u202f-]?[0-9]){{{count - 1}}}"


def _labeled(label: str, value: str) -> re.Pattern[str]:
    # Explicit labels also protect mistyped identifiers with invalid checksums.
    # A trailing digit must not allow a shorter pattern to mask only a prefix.
    return re.compile(
        rf"(?<!\w)(?:{label}){_SEPARATOR}(?P<value>{value})(?![ \u00a0\u202f-]?[0-9])",
        re.IGNORECASE,
    )


_LABELED_IDENTIFIERS = (
    ("RU_SNILS", _labeled(r"снилс|snils", _digits(11))),
    ("RU_INN", _labeled(r"инн|inn", rf"(?:{_digits(12)}|{_digits(10)})")),
    (
        "RU_PASSPORT",
        _labeled(
            r"паспорт(?:а|у|ом|е)?(?:\s+(?:рф|россии|гражданина\s+рф))?",
            rf"(?:серия[\s:№-]*)?{_digits(4)}[\s,;-]*(?:номер|№)?[\s:№-]*{_digits(6)}",
        ),
    ),
    (
        "RU_FOREIGN_PASSPORT",
        _labeled(r"загран(?:ичный\s+)?паспорт(?:а|у|ом|е)?", _digits(9)),
    ),
    (
        "RU_DRIVER_LICENSE",
        _labeled(
            r"водительск(?:ое|ого)\s+удостоверени[ея]|водительск(?:ие|их)\s+права|в/у",
            _digits(10),
        ),
    ),
    (
        "RU_MILITARY_ID",
        _labeled(r"военн(?:ый|ого)\s+билет(?:а)?", r"[А-ЯЁA-Z]{2}\s*[0-9]{7}"),
    ),
    (
        "RU_BIRTH_CERTIFICATE",
        _labeled(
            r"свидетельств[оа]\s+о\s+рождении",
            r"[IVXLСІ]{1,6}[- \u2010-\u2014][А-ЯЁ]{2}[\s№-]*[0-9]{6}",
        ),
    ),
    (
        "RU_BANK_ACCOUNT",
        _labeled(
            r"(?:(?:банковск(?:ий|ого)|расч[её]тн(?:ый|ого)|лицев(?:ой|ого)"
            r"|корреспондентск(?:ий|ого))\s+)?сч[её]т(?:а)?|р\s*/\s*с|к\s*/\s*с",
            _digits(20),
        ),
    ),
)

_SNILS = re.compile(r"(?<!\w)(?:[0-9]{3}[- ][0-9]{3}[- ][0-9]{3}[ -]?[0-9]{2}|[0-9]{11})(?!\w)")
_INN = re.compile(r"(?<!\w)(?:[0-9]{12}|[0-9]{10})(?!\w)")

# Values are captured separately so field labels stay useful to the model.
# Quoted values may contain spaces, escapes and delimiter characters.
_SECRET_VALUE = r"""(?:"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|[^\s,;<>"']+)"""
_SECRET_ASSIGNMENT = re.compile(
    r"""(?<![\w-])["']?(?:"""
    r"(?:[a-z][a-z0-9_.-]*[_-])?"
    r"(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key"
    r"|access[_-]?key(?:[_-]?id)?|private[_-]?key|encryption[_-]?key)"
    r"|парол[ья]|токен|секрет|(?:api[- ]?ключ)|ключ\s+(?:шифрования|доступа)"
    r""")["']?\s*[:=]\s*(?P<value>""" + _SECRET_VALUE + ")",
    re.IGNORECASE,
)
_POWERSHELL_SECRET = re.compile(
    r"(?<!\w)(?:-(?:password|passwd|pwd|token|apikey)\b"
    r"|\bConvertTo-SecureString\b(?:\s+-(?:AsPlainText|Force)\b){0,2}(?:\s+-String\b)?)"
    r"\s+(?P<value>" + _SECRET_VALUE + ")",
    re.IGNORECASE,
)
_AUTHORIZATION = re.compile(
    r"(?<!\w)(?:Bearer|Basic)\s+(?P<value>[A-Za-z0-9._~+/\x3d-]{4,})",
    re.IGNORECASE,
)
_PRIVATE_KEY = re.compile(
    r"(?P<value>-----BEGIN (?P<kind>(?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY)"
    r"-----[\s\S]*?(?:-----END (?P=kind)-----|\Z))"
)
_TOKEN = re.compile(
    r"(?<![\w-])(?P<value>"
    r"gh[pousr]_[A-Za-z0-9]{20,255}"
    r"|github_pat_[A-Za-z0-9_]{20,255}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,255}"
    r"|sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,255}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"
    r"|[0-9]{8,12}:[A-Za-z0-9_-]{30,64}"
    r"|eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")(?![\w-])"
)
_SECRETS = (_SECRET_ASSIGNMENT, _POWERSHELL_SECRET, _AUTHORIZATION, _PRIVATE_KEY, _TOKEN)


def _valid_snils(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if len(set(digits)) == 1 or int(digits[:9]) <= 1_001_998:
        return False
    checksum = sum(int(digit) * weight for digit, weight in zip(digits[:9], range(9, 0, -1)))
    checksum %= 101
    return (0 if checksum == 100 else checksum) == int(digits[-2:])


def _valid_inn(value: str) -> bool:
    if len(set(value)) == 1:
        return False

    def check(weights: tuple[int, ...]) -> int:
        return sum(int(digit) * weight for digit, weight in zip(value, weights)) % 11 % 10

    if len(value) == 10:
        return check((2, 4, 10, 3, 5, 9, 4, 6, 8)) == int(value[9])
    return check((7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == int(value[10]) and check(
        (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    ) == int(value[11])


def sensitive_matches(text: str) -> Iterator[SensitiveMatch]:
    """Yield only offsets and categories; never retain detected values in audit."""
    for entity_type, pattern in _LABELED_IDENTIFIERS:
        for match in pattern.finditer(text):
            yield SensitiveMatch(*match.span("value"), entity_type)
    for entity_type, pattern, validate in (
        ("RU_SNILS", _SNILS, _valid_snils),
        ("RU_INN", _INN, _valid_inn),
    ):
        for match in pattern.finditer(text):
            if validate(match.group()):
                yield SensitiveMatch(*match.span(), entity_type)
    for pattern in _SECRETS:
        for match in pattern.finditer(text):
            yield SensitiveMatch(*match.span("value"), "SECRET")
