from __future__ import annotations

import hashlib
import json
from typing import Any

from anki.cards import Card, CardId
from anki.models import FieldDict, NotetypeId
from anki.notes import Note
from aqt import mw

from .. import ankimorphs_globals as am_globals
from ..ankimorphs_config import (
    AnkiMorphsConfig,
    AnkiMorphsConfigFilter,
    get_config_dict,
)
from ..ankimorphs_db import AnkiMorphsDB
from ..morpheme import Morpheme

_DIGEST_SIZE = 8


def _digest(*parts: Any) -> bytes:
    return hashlib.blake2b(
        repr(parts).encode("utf-8"), digest_size=_DIGEST_SIZE
    ).digest()


def _get_card_states(note_type_id: NotetypeId) -> list[tuple[int, int, bytes]]:
    """
    Returns (card id, note id, digest) for every card of this note type, where
    the digest covers everything on the card and its note that recalc writes.

    Returns: list of (card_id, note_id, card_state)
    """
    assert mw is not None
    assert mw.col.db is not None

    return [
        (
            row[0],
            row[1],
            hashlib.blake2b(
                f"{row[2]}\x1f{row[3]}\x1f{row[4]}\x1f{row[5]}\x1f{row[6]}".encode(),
                digest_size=_DIGEST_SIZE,
            ).digest(),
        )
        for row in mw.col.db.all(
            "SELECT cards.id, cards.nid, cards.due, cards.queue, cards.type,"
            " notes.flds, notes.tags"
            " FROM cards"
            " INNER JOIN notes ON cards.nid = notes.id"
            " WHERE notes.mid = ?",
            note_type_id,
        )
    ]


class SkipUnchangedCards:  # pylint:disable=too-many-instance-attributes
    """
    Lets recalc leave a card alone when it already holds what this run would
    write to it.

    A card's fingerprint combines what recalc would produce (its morphs with
    their learning status and priority, plus the settings that turn those into
    a score, tags and extra fields) with what the card currently holds (due,
    queue, type, fields, tags). Both still matching means recalc would write
    what is already there.

    The invariant that makes this safe is in save(): only cards this recalc left
    untouched get a fingerprint. Recalc is not guaranteed to reach its result in
    a single pass -- two cards of the same note each hold their own Note object,
    so the later one can discard what the earlier one wrote, and a note holding
    both 'ready' tags loses one per run. Fingerprinting a card recalc just
    changed would freeze it half-way there forever; fingerprinting only the
    settled ones cannot.
    """

    def __init__(self, am_config: AnkiMorphsConfig) -> None:
        self.enabled = not am_config.recalc_offset_new_cards
        self._am_config = am_config
        self._previous_fingerprints: dict[int, int] = {}
        self._content_fingerprints: dict[int, bytes] = {}
        self._card_states: dict[int, bytes] = {}
        self._note_id_of_card: dict[int, int] = {}
        self._settings_digest = b""
        self._morph_priorities: dict[tuple[str, str], int] = {}
        self._morph_digests: dict[Morpheme, bytes] = {}

        if self.enabled:
            with AnkiMorphsDB() as am_db:
                am_db.create_recalc_fingerprint_table()
                self._previous_fingerprints = am_db.get_recalc_fingerprints()

    def start_note_filter(
        self,
        config_filter: AnkiMorphsConfigFilter,
        field_name_dict: dict[str, tuple[int, FieldDict]],
        note_type_id: NotetypeId | None,
        morph_priorities: dict[tuple[str, str], int],
    ) -> None:
        if not self.enabled:
            return

        assert note_type_id is not None
        field_names = sorted(field_name_dict)
        self._settings_digest = _digest(
            am_globals.__version__,
            json.dumps(get_config_dict(), sort_keys=True),
            config_filter.note_type,
            config_filter.field,
            field_names,
            [field_name_dict[field_name][0] for field_name in field_names],
            len(morph_priorities),
        )
        self._morph_priorities = morph_priorities
        self._morph_digests = {}

        for card_id, note_id, card_state in _get_card_states(note_type_id):
            self._card_states[card_id] = card_state
            self._note_id_of_card[card_id] = note_id

    def can_skip(self, card_id: CardId, card_morphs: list[Morpheme] | None) -> bool:
        if not self.enabled:
            return False

        content_fingerprint = self._get_content_fingerprint(card_morphs)
        self._content_fingerprints[card_id] = content_fingerprint
        card_state = self._card_states.get(card_id)

        if card_state is None:
            return False

        return self._previous_fingerprints.get(card_id) == _combine(
            content_fingerprint, card_state
        )

    def save(
        self, modified_cards: dict[CardId, Card], modified_notes: list[Note]
    ) -> None:
        if not self.enabled:
            return

        modified_note_ids = {note.id for note in modified_notes}
        fingerprints: list[tuple[int, int]] = []

        for card_id, content_fingerprint in self._content_fingerprints.items():
            if card_id in modified_cards:
                continue
            if self._note_id_of_card.get(card_id) in modified_note_ids:
                continue

            card_state = self._card_states.get(card_id)
            if card_state is not None:
                fingerprints.append(
                    (card_id, _combine(content_fingerprint, card_state))
                )

        with AnkiMorphsDB() as am_db:
            am_db.replace_recalc_fingerprints(fingerprints)

    def _get_content_fingerprint(self, card_morphs: list[Morpheme] | None) -> bytes:
        digests: list[bytes] = [self._settings_digest]

        evaluate_inflection = self._am_config.evaluate_morph_inflection

        for morph in card_morphs or []:
            digest = self._morph_digests.get(morph)

            if digest is None:
                sub_key = morph.inflection if evaluate_inflection else morph.lemma

                digest = _digest(
                    morph.lemma,
                    morph.inflection,
                    morph.get_learning_status(
                        evaluate_inflection, self._am_config.interval_for_known_morphs
                    ),
                    self._morph_priorities.get((morph.lemma, sub_key)),
                )
                self._morph_digests[morph] = digest

            digests.append(digest)

        return hashlib.blake2b(b"".join(digests), digest_size=_DIGEST_SIZE).digest()


def _combine(content_fingerprint: bytes, card_state: bytes) -> int:
    return int.from_bytes(
        hashlib.blake2b(
            content_fingerprint + card_state, digest_size=_DIGEST_SIZE
        ).digest(),
        byteorder="big",
        signed=True,
    )
