"""Turning spoken item text into a stable identity plus a separate quantity.

Google writes whatever it heard into Keep as one line of text: "2 lemons",
"two lemons", "a dozen eggs".  AnyList models quantity as its own field, so we
split the two apart on the way in and reassemble them on the way out.

The *key* is what the merge engine uses for identity.  It is deliberately
aggressive -- lowercased, depunctuated, singularised -- because it is internal
and never shown to anyone.  That is what stops "Strawberries" from landing
next to an existing "strawberry".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import inflect

_inflect = inflect.engine()

# Spoken numbers, since Google transcribes "two lemons" as words but "2 lemons"
# as a digit depending on phrasing and locale.
_NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50,
    "couple": 2,
}

# Units we are willing to absorb into the quantity field.  Anything not listed
# stays part of the item name, which is the safe direction to be wrong in.
_UNITS: frozenset[str] = frozenset({
    "g", "gram", "grams", "kg", "kilo", "kilos", "kilogram", "kilograms",
    "mg", "oz", "ounce", "ounces", "lb", "lbs", "pound", "pounds",
    "ml", "l", "litre", "litres", "liter", "liters",
    "pack", "packs", "packet", "packets", "box", "boxes", "bag", "bags",
    "bottle", "bottles", "can", "cans", "tin", "tins", "jar", "jars",
    "punnet", "punnets", "bunch", "bunches", "head", "heads",
    "carton", "cartons", "tub", "tubs", "roll", "rolls",
    "loaf", "loaves", "slice", "slices", "clove", "cloves",
})

# Words that end in "s" but are not plurals; inflect mangles several of these
# into things like "molass" and "hummu".
_NEVER_SINGULARISE: frozenset[str] = frozenset({
    "molasses", "hummus", "asparagus", "couscous", "watercress", "cress",
    "swiss", "gas", "bass", "haggis", "chorizo", "berries",
})

_ARTICLES: frozenset[str] = frozenset({"a", "an", "the", "some"})

# "2", "2.5", "1/2" -- optionally with the unit glued on, as in "500g".
_NUMERIC = re.compile(r"^(?P<num>\d+(?:[./]\d+)?)(?P<unit>[a-z]+)?$", re.IGNORECASE)
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Parsed:
    """An item split into the parts AnyList wants to store separately."""

    name: str
    quantity: str | None


def _tidy(text: str) -> str:
    """Normalise unicode and whitespace without touching case or meaning."""
    text = unicodedata.normalize("NFKC", text)
    return _WS.sub(" ", text).strip()


def _format_number(value: float) -> str:
    """Render 2.0 as "2" but keep 2.5 as "2.5"."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _parse_number(token: str) -> float | None:
    """Read a single numeric token, including the "1/2" fraction form."""
    if "/" in token:
        numerator, _, denominator = token.partition("/")
        try:
            return float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(token)
    except ValueError:
        return None


def _leading_count(
    tokens: list[str], *, allow_bare_unit: bool
) -> tuple[float, str | None, int] | None:
    """Read a leading count off the token list.

    Returns ``(count, unit, tokens_consumed)`` or ``None`` when the text does
    not start with something we recognise as a quantity.  ``allow_bare_unit``
    is set once an article has been seen, which is what lets "a loaf of bread"
    parse while leaving "can opener" alone.
    """
    if not tokens:
        return None

    first = tokens[0].lower()

    # "half a dozen eggs" / "half a kilo of flour"
    if first == "half":
        rest = tokens[1:]
        skipped = 1
        if rest and rest[0].lower() in _ARTICLES:
            rest = rest[1:]
            skipped += 1
        inner = _leading_count(rest, allow_bare_unit=True)
        if inner is None:
            return None
        count, unit, consumed = inner
        return count / 2, unit, consumed + skipped

    # A bare "dozen" is a multiplier applied below, so it starts at one.
    if first == "dozen":
        return 1.0, None, 0

    if first in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[first]), None, 1

    match = _NUMERIC.match(first)
    if match:
        count = _parse_number(match.group("num"))
        if count is None:
            return None
        unit = match.group("unit")
        if unit and unit.lower() not in _UNITS:
            # "7up" and friends -- not a unit we know, so leave it alone.
            return None
        return count, unit.lower() if unit else None, 1

    # "a bag of rice".  The trailing "of" is what distinguishes a container
    # from an item that merely starts with a unit word, like "can opener".
    if allow_bare_unit and first in _UNITS and len(tokens) > 1 and tokens[1].lower() == "of":
        return 1.0, first, 1

    return None


def parse_quantity(text: str) -> Parsed:
    """Split leading quantity information off an item.

    Falls back to treating the whole string as the name whenever the split
    would leave nothing behind -- "12" on its own is an item called "12", not a
    quantity with no item.
    """
    tidied = _tidy(text)
    if not tidied:
        return Parsed(name="", quantity=None)

    tokens = tidied.split()

    # A leading article is never part of the name we want to store.
    had_article = tokens[0].lower() in _ARTICLES
    if had_article:
        tokens = tokens[1:]
        if not tokens:
            return Parsed(name=tidied, quantity=None)

    leading = _leading_count(tokens, allow_bare_unit=had_article)
    if leading is None:
        return Parsed(name=" ".join(tokens), quantity=None)

    count, unit, consumed = leading
    rest = tokens[consumed:]

    # "two dozen eggs" -> 24 eggs.  A dozen multiplies; it is not a unit.
    while rest and rest[0].lower() == "dozen":
        count *= 12
        rest = rest[1:]

    # A unit sitting as its own token: "2 kg potatoes".
    if unit is None and rest and rest[0].lower() in _UNITS:
        unit = rest[0].lower()
        rest = rest[1:]

    # Connectives that carry no meaning once the quantity is lifted out.
    while rest and rest[0].lower() in {"of", "x"}:
        rest = rest[1:]

    name = " ".join(rest).strip()
    if not name:
        return Parsed(name=tidied, quantity=None)

    quantity = _format_number(count)
    if unit:
        quantity = f"{quantity} {unit}"
    return Parsed(name=name, quantity=quantity)


def render(name: str, quantity: str | None) -> str:
    """Reassemble a name and quantity into the single line Keep stores."""
    name = _tidy(name)
    quantity = _tidy(quantity) if quantity else ""
    return f"{quantity} {name}".strip() if quantity else name


def _singularise(word: str) -> str:
    if word in _NEVER_SINGULARISE or len(word) <= 3:
        return word
    singular = _inflect.singular_noun(word)
    return singular if isinstance(singular, str) and singular else word


def key(name: str) -> str:
    """Collapse an item name to its identity for matching.

    Every word is singularised, not just the last one, so "chicken breasts"
    and "chicken breast" agree.  Over-normalising is safe here because the key
    is only ever compared against other keys.
    """
    text = _PUNCT.sub(" ", _tidy(name).lower())
    words = [w for w in text.split() if w and w not in _ARTICLES]
    if not words:
        return ""
    return " ".join(_singularise(word) for word in words)
