from __future__ import annotations

import copy
from collections.abc import Callable
from test.fake_configs import default_config_dict
from test.fake_environment_module import (  # pylint:disable=unused-import
    FakeEnvironment,
    FakeEnvironmentParams,
    fake_environment_fixture,
)
from test.recalc_helpers import (
    dump_collection,
    recalc,
    recalc_until_the_collection_stops_changing,
)
from typing import Any
from unittest import mock

import pytest

from ankimorphs import ankimorphs_config
from ankimorphs import ankimorphs_globals as am_globals
from ankimorphs import text_preprocessing
from ankimorphs.ankimorphs_config import (
    AnkiMorphsConfigFilter,
    RawConfigFilterKeys,
    RawConfigKeys,
)
from ankimorphs.ankimorphs_db import AnkiMorphsDB
from ankimorphs.morph_priority_utils import get_morph_priority

from anki.cards import Card, CardId  # isort:skip  pylint:disable=wrong-import-order
from anki.collection import Collection  # isort:skip  pylint:disable=wrong-import-order
from anki.consts import (  # isort:skip  pylint:disable=wrong-import-order
    CARD_TYPE_NEW,
    CARD_TYPE_REV,
    QUEUE_TYPE_NEW,
    QUEUE_TYPE_REV,
    QUEUE_TYPE_SIBLING_BURIED,
)

_COLLECTION = FakeEnvironmentParams(
    initial_col="card_handling_collection",
    config=copy.deepcopy(default_config_dict),
)


def _forget_what_recalc_wrote() -> None:
    am_db = AnkiMorphsDB()
    am_db.create_recalc_fingerprint_table()
    am_db.replace_recalc_fingerprints([])
    am_db.con.close()


def _nothing_changed(
    _collection: Collection, _config_filter: AnkiMorphsConfigFilter
) -> None:
    pass


def _user_broke_an_extra_field(
    collection: Collection, _config_filter: AnkiMorphsConfigFilter
) -> None:
    note = collection.get_note(sorted(collection.find_notes(""))[0])
    note[am_globals.EXTRA_FIELD_HIGHLIGHTED] = "the user wiped this by hand"
    collection.update_note(note)


def _user_removed_a_tag(
    collection: Collection, _config_filter: AnkiMorphsConfigFilter
) -> None:
    tagged_note_ids = [
        note_id
        for note_id in collection.find_notes("")
        if collection.get_note(note_id).tags
    ]
    assert len(tagged_note_ids) > 0

    note = collection.get_note(tagged_note_ids[0])
    note.tags.clear()
    collection.update_note(note)


def _user_repositioned_a_card(
    collection: Collection, _config_filter: AnkiMorphsConfigFilter
) -> None:
    card = collection.get_card(sorted(collection.find_cards(""))[0])
    card.due = 123456
    collection.update_card(card)


def _card_was_studied(
    collection: Collection, _config_filter: AnkiMorphsConfigFilter
) -> None:
    card = collection.get_card(sorted(collection.find_cards(""))[0])
    card.type = CARD_TYPE_REV
    card.queue = QUEUE_TYPE_REV
    card.ivl = 500
    collection.update_card(card)


def _card_was_forgotten(
    collection: Collection, _config_filter: AnkiMorphsConfigFilter
) -> None:
    for card_id in sorted(collection.find_cards("")):
        card = collection.get_card(card_id)
        if card.type != CARD_TYPE_NEW:
            card.type = CARD_TYPE_NEW
            card.queue = QUEUE_TYPE_NEW
            card.ivl = 0
            collection.update_card(card)
            return
    pytest.skip("collection has no studied card to forget")


def _card_was_buried(
    collection: Collection, _config_filter: AnkiMorphsConfigFilter
) -> None:
    card = collection.get_card(sorted(collection.find_cards(""))[0])
    card.queue = QUEUE_TYPE_SIBLING_BURIED
    collection.update_card(card)


def _expression_changed(
    collection: Collection, config_filter: AnkiMorphsConfigFilter
) -> None:
    note = collection.get_note(sorted(collection.find_notes(""))[0])
    note[config_filter.field] = "a sentence with completely different words"
    collection.update_note(note)


mutations: list[Any] = [
    pytest.param(_nothing_changed, id="nothing_changed"),
    pytest.param(_user_broke_an_extra_field, id="user_broke_an_extra_field"),
    pytest.param(_user_removed_a_tag, id="user_removed_a_tag"),
    pytest.param(_user_repositioned_a_card, id="user_repositioned_a_card"),
    pytest.param(_card_was_studied, id="card_was_studied"),
    pytest.param(_card_was_forgotten, id="card_was_forgotten"),
    pytest.param(_card_was_buried, id="card_was_buried"),
    pytest.param(_expression_changed, id="expression_changed"),
]


def _assert_skipping_matches_a_full_recalc(
    collection: Collection,
    mutate: Callable[[Collection, AnkiMorphsConfigFilter], None],
) -> None:
    config_filter = ankimorphs_config.get_read_enabled_filters()[0]

    recalc()
    mutate(collection, config_filter)
    recalc()
    after_skipping = dump_collection(collection)

    _forget_what_recalc_wrote()
    recalc()

    assert len(after_skipping) > 0
    assert after_skipping == dump_collection(collection)


@pytest.mark.parametrize("fake_environment_fixture", [_COLLECTION], indirect=True)
@pytest.mark.parametrize("mutate", mutations)
def test_skipping_leaves_the_collection_as_a_full_recalc_would(
    fake_environment_fixture: FakeEnvironment | None,
    mutate: Callable[[Collection, AnkiMorphsConfigFilter], None],
) -> None:
    assert fake_environment_fixture is not None
    text_preprocessing.update_translation_table()

    _assert_skipping_matches_a_full_recalc(fake_environment_fixture.mock_mw.col, mutate)


@pytest.mark.parametrize(
    "fake_environment_fixture",
    [
        FakeEnvironmentParams(
            initial_col="offset_new_cards_inflection_collection",
            config=copy.deepcopy(default_config_dict)
            | {RawConfigKeys.RECALC_OFFSET_NEW_CARDS: True},
        )
    ],
    indirect=True,
)
@pytest.mark.parametrize("mutate", mutations)
def test_offset_cards_are_never_skipped(
    fake_environment_fixture: FakeEnvironment | None,
    mutate: Callable[[Collection, AnkiMorphsConfigFilter], None],
) -> None:
    assert fake_environment_fixture is not None
    text_preprocessing.update_translation_table()

    _assert_skipping_matches_a_full_recalc(fake_environment_fixture.mock_mw.col, mutate)


@pytest.mark.parametrize("fake_environment_fixture", [_COLLECTION], indirect=True)
def test_changing_the_settings_makes_every_card_stale(
    fake_environment_fixture: FakeEnvironment | None,
) -> None:
    assert fake_environment_fixture is not None
    text_preprocessing.update_translation_table()

    collection = fake_environment_fixture.mock_mw.col
    recalc()

    config = copy.deepcopy(fake_environment_fixture.config)
    config[RawConfigKeys.ALGORITHM_TOTAL_PRIORITY_ALL_MORPHS_WEIGHT] = 42
    config[RawConfigKeys.FILTERS][0][RawConfigFilterKeys.EXTRA_SCORE_TERMS] = False
    fake_environment_fixture.mock_mw.addonManager.getConfig.return_value = config

    recalc()
    with_new_settings = dump_collection(collection)

    _forget_what_recalc_wrote()
    recalc()

    assert with_new_settings == dump_collection(collection)


@pytest.mark.parametrize("fake_environment_fixture", [_COLLECTION], indirect=True)
def test_unchanged_cards_are_not_read_from_the_collection(
    fake_environment_fixture: FakeEnvironment | None,
) -> None:
    assert fake_environment_fixture is not None
    text_preprocessing.update_translation_table()

    collection = fake_environment_fixture.mock_mw.col
    config_filter = ankimorphs_config.get_read_enabled_filters()[0]
    card_amount = len(collection.find_cards(""))
    cards_read: list[CardId] = []

    original_get_card = Collection.get_card

    def spy(self: Collection, card_id: CardId) -> Card:
        cards_read.append(card_id)
        return original_get_card(self, card_id)

    with mock.patch.object(Collection, "get_card", spy):
        recalc()
        assert len(cards_read) == card_amount

        cards_read.clear()
        recalc()
        assert not cards_read

        edited_note_id = sorted(collection.find_notes(""))[0]
        _expression_changed(collection, config_filter)
        recalc()

    assert set(cards_read) >= set(collection.card_ids_of_note(edited_note_id))


@pytest.mark.parametrize("fake_environment_fixture", [_COLLECTION], indirect=True)
def test_a_longer_priority_list_makes_every_card_stale(
    fake_environment_fixture: FakeEnvironment | None,
) -> None:
    assert fake_environment_fixture is not None
    text_preprocessing.update_translation_table()

    collection = fake_environment_fixture.mock_mw.col
    target = "ankimorphs.recalc.recalc_main.get_morph_priority"

    def only_one_morph_listed(extra_entries: int) -> Callable[..., Any]:
        def _get_morph_priority(**kwargs: Any) -> dict[tuple[str, str], int]:
            listed = dict(list(get_morph_priority(**kwargs).items())[:1])
            for index in range(extra_entries):
                listed[(f"absent{index}", f"absent{index}")] = len(listed)
            return listed

        return _get_morph_priority

    with mock.patch(target, only_one_morph_listed(0)):
        recalc()

    with mock.patch(target, only_one_morph_listed(500)):
        recalc()
        with_longer_list = dump_collection(collection)

        _forget_what_recalc_wrote()
        recalc()

    assert with_longer_list == dump_collection(collection)


@pytest.mark.parametrize("fake_environment_fixture", [_COLLECTION], indirect=True)
def test_an_interval_that_keeps_its_learning_status_is_not_a_change(
    fake_environment_fixture: FakeEnvironment | None,
) -> None:
    assert fake_environment_fixture is not None
    text_preprocessing.update_translation_table()

    collection = fake_environment_fixture.mock_mw.col
    interval_for_known = ankimorphs_config.AnkiMorphsConfig().interval_for_known_morphs

    for card_id in collection.find_cards(""):
        card = collection.get_card(card_id)
        card.type = CARD_TYPE_REV
        card.queue = QUEUE_TYPE_REV
        card.ivl = interval_for_known * 2
        collection.update_card(card)

    recalc()
    recalc()
    before = dump_collection(collection)

    for card_id in collection.find_cards(""):
        card = collection.get_card(card_id)
        card.ivl = interval_for_known * 3  # still comfortably 'known'
        collection.update_card(card)

    cards_read: list[CardId] = []
    original_get_card = Collection.get_card

    def spy(self: Collection, card_id: CardId) -> Card:
        cards_read.append(card_id)
        return original_get_card(self, card_id)

    with mock.patch.object(Collection, "get_card", spy):
        recalc()

    assert not cards_read
    assert before == dump_collection(collection)


def _assert_the_fixed_point_is_the_one_a_full_recalc_reaches(
    collection: Collection,
) -> None:
    with_skipping = recalc_until_the_collection_stops_changing(collection)

    _forget_what_recalc_wrote()
    recalc()

    assert len(with_skipping) > 0
    assert with_skipping == dump_collection(collection)


@pytest.mark.parametrize("fake_environment_fixture", [_COLLECTION], indirect=True)
def test_a_card_that_recalc_changed_keeps_no_fingerprint(
    fake_environment_fixture: FakeEnvironment | None,
) -> None:
    assert fake_environment_fixture is not None
    text_preprocessing.update_translation_table()

    collection = fake_environment_fixture.mock_mw.col
    config_filter = ankimorphs_config.get_read_enabled_filters()[0]
    recalc_until_the_collection_stops_changing(collection)

    written_per_recalc: list[set[int]] = []
    fingerprinted_per_recalc: list[set[int]] = []

    original_update_cards = Collection.update_cards
    original_update_notes = Collection.update_notes
    original_replace = AnkiMorphsDB.replace_recalc_fingerprints

    def update_cards_spy(self: Collection, cards: list[Card]) -> Any:
        written_per_recalc.append({card.id for card in cards})
        return original_update_cards(self, cards)

    def update_notes_spy(self: Collection, notes: list[Any]) -> Any:
        written_per_recalc[-1].update(
            card_id for note in notes for card_id in self.card_ids_of_note(note.id)
        )
        return original_update_notes(self, notes)

    def replace_spy(self: AnkiMorphsDB, fingerprints: list[tuple[int, int]]) -> None:
        fingerprinted_per_recalc.append({card_id for card_id, _ in fingerprints})
        original_replace(self, fingerprints)

    _expression_changed(collection, config_filter)

    with mock.patch.object(Collection, "update_cards", update_cards_spy):
        with mock.patch.object(Collection, "update_notes", update_notes_spy):
            with mock.patch.object(
                AnkiMorphsDB, "replace_recalc_fingerprints", replace_spy
            ):
                recalc()

    assert len(written_per_recalc) == len(fingerprinted_per_recalc)
    assert any(written_per_recalc)

    for written, fingerprinted in zip(written_per_recalc, fingerprinted_per_recalc):
        assert not written & fingerprinted


@pytest.mark.parametrize("fake_environment_fixture", [_COLLECTION], indirect=True)
def test_a_note_holding_both_ready_tags_settles_where_a_full_recalc_settles(
    fake_environment_fixture: FakeEnvironment | None,
) -> None:
    assert fake_environment_fixture is not None
    text_preprocessing.update_translation_table()

    collection = fake_environment_fixture.mock_mw.col
    am_config = ankimorphs_config.AnkiMorphsConfig()
    card = collection.get_card(sorted(collection.find_cards(""))[0])
    card.type = CARD_TYPE_REV
    card.queue = QUEUE_TYPE_REV
    card.ivl = 250
    collection.update_card(card)

    note = card.note()
    note.tags = [am_config.tag_ready, am_config.tag_not_ready]
    collection.update_note(note)

    _assert_the_fixed_point_is_the_one_a_full_recalc_reaches(collection)
