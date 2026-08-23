"""The normaliser is what decides whether two spoken items are the same thing."""

import pytest

from voice_to_anylist.normalise import key, parse_quantity, render


@pytest.mark.parametrize(
    ("text", "expected_name", "expected_quantity"),
    [
        # Nothing to split.
        ("strawberries", "strawberries", None),
        ("chicken breast", "chicken breast", None),
        # Digits, spoken numbers, and the two glued together with a unit.
        ("2 lemons", "lemons", "2"),
        ("two lemons", "lemons", "2"),
        ("12 eggs", "eggs", "12"),
        ("500g flour", "flour", "500 g"),
        ("2 kg potatoes", "potatoes", "2 kg"),
        ("2.5 kg rice", "rice", "2.5 kg"),
        ("2 litres milk", "milk", "2 litres"),
        # "of" and "x" carry nothing once the quantity is lifted out.
        ("3 bottles of wine", "wine", "3 bottles"),
        ("2 x milk", "milk", "2"),
        # A dozen multiplies rather than acting as a unit.
        ("a dozen eggs", "eggs", "12"),
        ("dozen eggs", "eggs", "12"),
        ("two dozen eggs", "eggs", "24"),
        ("half a dozen eggs", "eggs", "6"),
        ("a couple of avocados", "avocados", "2"),
        # An article plus a container splits, because "of" disambiguates it.
        ("a loaf of bread", "bread", "1 loaf"),
        ("a bag of rice", "rice", "1 bag"),
        ("half a kilo of flour", "flour", "0.5 kilo"),
        # A leading article on its own is noise, not a quantity.
        ("an apple", "apple", None),
        ("the milk", "milk", None),
        # Without the article, a leading unit word stays part of the name --
        # otherwise "can opener" becomes one can of "opener".
        ("can opener", "can opener", None),
        # Splitting must never leave the item nameless.
        ("12", "12", None),
        ("2 kg", "2 kg", None),
        # Unknown trailing letters are not a unit, so nothing is split.
        ("7up", "7up", None),
        # Whitespace and empties.
        ("  milk  ", "milk", None),
        ("", "", None),
    ],
)
def test_parse_quantity(text, expected_name, expected_quantity):
    parsed = parse_quantity(text)
    assert parsed.name == expected_name
    assert parsed.quantity == expected_quantity


@pytest.mark.parametrize(
    "canonical",
    ["strawberries", "2 lemons", "12 eggs", "2 kg potatoes", "3 bottles wine"],
)
def test_render_round_trips_canonical_forms(canonical):
    parsed = parse_quantity(canonical)
    assert render(parsed.name, parsed.quantity) == canonical


def test_render_without_quantity():
    assert render("milk", None) == "milk"
    assert render("milk", "") == "milk"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Case and plurality are the whole point of the key.
        ("Strawberries", "strawberry"),
        ("Milk", "milk"),
        ("chicken breasts", "chicken breast"),
        ("Tinned Tomatoes", "tinned tomato"),
        # Punctuation and stray whitespace.
        ("half-and-half", "half and half"),
        ("olive  oil", "olive oil"),
        # Leading articles are noise.
        ("the milk", "milk"),
        ("some apples", "apple"),
    ],
)
def test_key_treats_variants_as_the_same_item(left, right):
    assert key(left) == key(right)
    assert key(left) != ""


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("milk", "almond milk"),
        ("red onion", "brown onion"),
        ("apple juice", "apple"),
    ],
)
def test_key_keeps_genuinely_different_items_apart(left, right):
    assert key(left) != key(right)


@pytest.mark.parametrize("word", ["molasses", "hummus", "asparagus", "couscous"])
def test_key_does_not_mangle_words_that_merely_end_in_s(word):
    assert key(word) == word


def test_key_of_empty_text():
    assert key("") == ""
    assert key("   ") == ""
