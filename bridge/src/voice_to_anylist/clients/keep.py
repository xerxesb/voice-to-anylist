"""Google Keep side of the mirror.

This is where "Hey Google, add strawberries to the shopping list" lands.  The
speaker writes a plain line of text into a checklist note; everything else --
splitting the quantity out, matching it against what AnyList already holds --
happens above this layer.

Writes are staged locally by gkeepapi and only reach Google on ``commit``,
which is why the engine calls it once at the end of a cycle rather than after
every mutation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import gkeepapi
from gkeepapi.exception import LoginException, ResyncRequiredException, SyncException
from gkeepapi.node import List as KeepList
from gkeepapi.node import ListItem as KeepListItem

from ..normalise import parse_quantity, render
from .base import AuthenticationError, ListClientError, ListItem

log = logging.getLogger(__name__)


class KeepNoteNotFound(ListClientError):
    """The configured note is missing, or is not a checklist.

    Deliberately fatal rather than self-healing: silently creating a second
    "Shopping list" would leave Assistant writing into one note while the
    bridge watched another, and nothing would ever appear in AnyList.
    """


class KeepClient:
    name = "keep"

    def __init__(
        self,
        email: str,
        master_token: str,
        note_title: str = "Shopping list",
        state_path: str | Path | None = None,
    ):
        self.email = email
        self.master_token = master_token
        self.note_title = note_title
        self.state_path = Path(state_path) if state_path else None
        self._keep: gkeepapi.Keep | None = None
        self._note: KeepList | None = None

    # -- session ------------------------------------------------------------

    def _load_state(self) -> dict | None:
        """Restore the cached sync state so a restart is not a full resync."""
        if not self.state_path or not self.state_path.exists():
            return None
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            log.warning("discarding unreadable Keep state (%s); will resync", error)
            return None

    def _save_state(self) -> None:
        if not self.state_path or self._keep is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self._keep.dump()))
        except OSError as error:
            log.warning("could not persist Keep state (%s); continuing", error)

    def _connect(self) -> gkeepapi.Keep:
        if self._keep is not None:
            return self._keep
        keep = gkeepapi.Keep()
        try:
            keep.authenticate(self.email, self.master_token, state=self._load_state(), sync=True)
        except LoginException as error:
            raise AuthenticationError(
                "Google rejected the master token. It is revoked whenever the account "
                "password changes or the device is removed from the account. "
                "Re-run `voice-to-anylist bootstrap`."
            ) from error
        except (OSError, SyncException) as error:
            raise ListClientError(f"Could not reach Google Keep: {error}") from error
        self._keep = keep
        return keep

    def _resolve_note(self, keep: gkeepapi.Keep) -> KeepList:
        wanted = self.note_title.strip().lower()
        matches = [
            node
            for node in keep.find(func=lambda n: not n.trashed and not n.deleted)
            if isinstance(node, KeepList) and (node.title or "").strip().lower() == wanted
        ]
        if not matches:
            plain = [
                node
                for node in keep.find(func=lambda n: not n.trashed and not n.deleted)
                if (getattr(node, "title", "") or "").strip().lower() == wanted
            ]
            if plain:
                raise KeepNoteNotFound(
                    f'The Keep note "{self.note_title}" is not a checklist. '
                    'Open it in Keep and turn on "Show checkboxes".'
                )
            raise KeepNoteNotFound(
                f'No Keep note titled "{self.note_title}". Create it, turn on '
                '"Show checkboxes", and share it with the household.'
            )
        if len(matches) > 1:
            # Stable choice, so the bridge does not oscillate between notes.
            matches.sort(key=lambda n: n.id)
            log.warning(
                'Found %d Keep notes titled "%s"; using %s',
                len(matches),
                self.note_title,
                matches[0].id,
            )
        return matches[0]

    def _items_by_id(self) -> dict[str, KeepListItem]:
        assert self._note is not None
        return {item.id: item for item in self._note.items}

    # -- ListClient ---------------------------------------------------------

    def fetch(self) -> list[ListItem]:
        keep = self._connect()
        try:
            keep.sync()
        except ResyncRequiredException:
            log.warning("Google asked for a full resync; discarding cached state")
            keep.sync(resync=True)
        except (OSError, SyncException) as error:
            raise ListClientError(f"Keep sync failed: {error}") from error

        self._note = self._resolve_note(keep)
        self._save_state()

        items: list[ListItem] = []
        for raw in self._note.items:
            parsed = parse_quantity(raw.text)
            if not parsed.name:
                continue
            items.append(
                ListItem(
                    id=raw.id,
                    name=parsed.name,
                    quantity=parsed.quantity,
                    checked=bool(raw.checked),
                )
            )
        return items

    def _require_note(self) -> KeepList:
        if self._note is None:
            raise ListClientError("Keep note not loaded; fetch() must run before writing")
        return self._note

    def add(self, name: str, quantity: str | None, checked: bool = False) -> str:
        note = self._require_note()
        return note.add(render(name, quantity), checked).id

    def set_quantity(self, item_id: str, quantity: str | None) -> None:
        """Rewrite the line, preserving whatever wording is already there.

        Keep has no quantity field, so the name is re-derived from the existing
        text rather than passed in -- that keeps "Strawberries" spelled the way
        the household wrote it.
        """
        self._require_note()
        item = self._items_by_id().get(item_id)
        if item is None:
            log.warning("Keep item %s vanished before its quantity could be set", item_id)
            return
        item.text = render(parse_quantity(item.text).name, quantity)

    def set_checked(self, item_id: str, checked: bool) -> None:
        self._require_note()
        item = self._items_by_id().get(item_id)
        if item is None:
            log.warning("Keep item %s vanished before it could be ticked", item_id)
            return
        item.checked = checked

    def remove(self, item_id: str) -> None:
        self._require_note()
        item = self._items_by_id().get(item_id)
        if item is None:
            return
        item.delete()

    def commit(self) -> None:
        """Push the staged edits. gkeepapi batches until told to sync."""
        if self._keep is None:
            return
        try:
            self._keep.sync()
        except (OSError, SyncException) as error:
            raise ListClientError(f"Keep sync failed while saving: {error}") from error
        self._save_state()
