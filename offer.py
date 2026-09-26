"""Параметры отклика: разбор служебных строк из текста и проверка ограничений формы Kwork."""
import html
import re
from dataclasses import dataclass, field

import config

PAYMENT_FULL = 'full'      # «Целиком, когда заказ выполнен»
PAYMENT_STAGES = 'stages'  # «По мере выполнения задач»

# Ограничения формы Kwork
TEXT_MIN, TEXT_MAX = 150, 2000
TITLE_MAX = 70
STAGES_MIN = 2
STAGE_PRICE_MIN = 500

_NUM = r'(\d[\d\s]*)\s*(?:₽|р\.?|руб\.?)?'
_PRICE_RE = re.compile(rf'\s*цена\s*:\s*{_NUM}\s*', re.I)
_DAYS_RE = re.compile(r'\s*срок\s*:\s*(\d+)\s*(?:дн\.?|дней|дня|день)?\s*', re.I)
_TITLE_RE = re.compile(r'\s*название\s*:\s*(.+?)\s*', re.I)
_PAYMENT_RE = re.compile(r'\s*оплата\s*:\s*(.+?)\s*', re.I)
_STAGE_RE = re.compile(rf'\s*задача\s*:\s*(.+?)\s*[—–\-|:]\s*{_NUM}\s*', re.I)


# Варианты срока в форме Kwork: (дней, подпись)
DURATION_OPTIONS = [
    (1, '1 день'), (2, '2 дня'), (3, '3 дня'), (4, '4 дня'), (5, '5 дней'), (6, '6 дней'),
    (7, '7 дней'), (10, '10 дней'), (14, '2 недели'), (21, '3 недели'), (30, '1 месяц'), (60, '2 месяца'),
]

_PARAM_RES = {'Цена': _PRICE_RE, 'Срок': _DAYS_RE}
_ALL_PARAM_RES = (_PRICE_RE, _DAYS_RE, _TITLE_RE, _PAYMENT_RE, _STAGE_RE)


class OfferError(ValueError):
    """Ошибка в параметрах отклика, текст понятен пользователю."""


@dataclass
class Offer:
    text: str
    price: int | None
    days: int
    title: str
    payment: str = PAYMENT_FULL
    stages: list[tuple[str, int]] = field(default_factory=list)

    def summary_html(self) -> str:
        price = f'{self.price:,} ₽'.replace(',', ' ') if self.price else 'из проекта'
        lines = [f'💰 Цена: <b>{price}</b> · ⏱ Срок: <b>{duration_label(self.days)}</b>']
        if self.payment == PAYMENT_STAGES:
            lines.append('💳 Оплата: <b>по мере выполнения задач</b>')
            lines += [f'   {i}. {html.escape(name)} — {p:,} ₽'.replace(',', ' ')
                      for i, (name, p) in enumerate(self.stages, 1)]
        else:
            lines.append('💳 Оплата: <b>целиком</b>')
            lines.append(f'📝 Название заказа: {html.escape(self.title)}')
        return '\n'.join(lines)


def _int(s: str) -> int:
    return int(re.sub(r'\s', '', s))


def default_price(project: dict) -> int | None:
    m = re.match(r'[\d\s]+', project.get('price') or '')
    return _int(m.group()) if m and m.group().strip() else None


def max_price(project: dict) -> int | None:
    """Допустимый максимум из строки цены проекта: «5 000 ₽ (допустимо до 15 000 ₽)»."""
    m = re.search(r'допустимо до\s*([\d\s]+)', project.get('price') or '')
    return _int(m.group(1)) if m else default_price(project)


def duration_label(days: int) -> str:
    """Подпись варианта Kwork, который будет выбран для days (ближайший не меньший)."""
    return next((label for n, label in DURATION_OPTIONS if n >= days), DURATION_OPTIONS[-1][1])


def set_param(draft: str, name: str, value: int) -> str:
    """Заменяет (или добавляет в конец) строку «Цена: N» / «Срок: N» в черновике."""
    regex = _PARAM_RES[name]
    lines = [line for line in draft.splitlines() if not regex.fullmatch(line)]
    while lines and not lines[-1].strip():
        lines.pop()
    body = [line for line in lines if not any(r.fullmatch(line) for r in _ALL_PARAM_RES)]
    params = [line for line in lines if any(r.fullmatch(line) for r in _ALL_PARAM_RES)]
    # Параметры держим отдельным блоком после текста
    return '\n'.join(body).rstrip() + '\n\n' + '\n'.join([*params, f'{name}: {value}'])


def param_lines(draft: str) -> list[str]:
    """Служебные строки черновика — чтобы сохранить их при перегенерации текста."""
    return [line.strip() for line in draft.splitlines() if any(r.fullmatch(line) for r in _ALL_PARAM_RES)]


def strip_params(raw_text: str) -> str:
    """Текст отклика без служебных строк."""
    return parse_offer(raw_text, {'title': ''}, validate=False).text


def parse_offer(raw_text: str, project: dict, *, validate: bool = True) -> Offer:
    """Вырезает из текста строки «Цена:», «Срок:», «Название:», «Оплата:», «Задача:» и собирает Offer."""
    price, days = default_price(project), config.OFFER_DEFAULT_DAYS
    title = project.get('title') or ''
    payment = config.OFFER_PAYMENT_TYPE
    stages: list[tuple[str, int]] = []
    lines = []
    for line in raw_text.splitlines():
        if m := _PRICE_RE.fullmatch(line):
            price = _int(m.group(1))
        elif m := _DAYS_RE.fullmatch(line):
            days = int(m.group(1))
        elif m := _TITLE_RE.fullmatch(line):
            title = m.group(1)
        elif m := _STAGE_RE.fullmatch(line):
            stages.append((m.group(1)[:TITLE_MAX], _int(m.group(2))))
        elif m := _PAYMENT_RE.fullmatch(line):
            value = m.group(1).lower()
            if value.startswith(('част', 'этап', 'по мере', 'задач')):
                payment = PAYMENT_STAGES
            elif value.startswith(('цел', 'полн', 'сразу')):
                payment = PAYMENT_FULL
            else:
                raise OfferError(f'Не понял «Оплата: {m.group(1)}». Используйте «Оплата: целиком» или «Оплата: частями».')
        else:
            lines.append(line)

    if stages:
        payment = PAYMENT_STAGES
    if payment == PAYMENT_STAGES and stages:
        price = sum(p for _, p in stages)  # итоговая цена = сумма задач

    offer = Offer(
        text='\n'.join(lines).strip(),
        price=price,
        days=days,
        title=re.sub(r'\s+', ' ', title).strip()[:TITLE_MAX].rstrip(),
        payment=payment,
        stages=stages,
    )
    if validate:
        _validate(offer)
    return offer


def _validate(offer: Offer) -> None:
    if len(offer.text) < TEXT_MIN:
        raise OfferError(f'Текст отклика слишком короткий: {len(offer.text)} символов, Kwork требует минимум {TEXT_MIN}.')
    if len(offer.text) > TEXT_MAX:
        raise OfferError(f'Текст отклика слишком длинный: {len(offer.text)} символов, максимум {TEXT_MAX}.')
    if not offer.price:
        raise OfferError('Не удалось определить цену — добавьте строку «Цена: 5000».')
    if offer.payment == PAYMENT_STAGES:
        if len(offer.stages) < STAGES_MIN:
            raise OfferError(
                f'Для оплаты по мере выполнения нужно минимум {STAGES_MIN} задачи. Добавьте строки вида:\n'
                'Задача: Разработка — 3000\nЗадача: Тестирование и сдача — 2000'
            )
        if cheap := [name for name, p in offer.stages if p < STAGE_PRICE_MIN]:
            raise OfferError(f'Стоимость задачи должна быть не меньше {STAGE_PRICE_MIN} ₽: {", ".join(cheap)}.')
    elif not offer.title:
        raise OfferError('Не указано название заказа — добавьте строку «Название: …».')
