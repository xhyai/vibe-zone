"""Small explicit PL/EN catalog shared by server-rendered pages and live refresh."""
import json
from pathlib import Path

from flask import g

ENGLISH = json.loads(Path(__file__).with_name('translations.json').read_text())
POLISH = {value: key for key, value in ENGLISH.items()}


def translate(message):
    if g.lang == 'en':
        return ENGLISH.get(message, message)
    return POLISH.get(message, message)


def public_error(message):
    if message in ENGLISH or message in POLISH:
        return translate(message)
    return translate('Testy niezaliczone. Szczegóły u organizatora.')
