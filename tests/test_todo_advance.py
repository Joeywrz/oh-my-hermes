"""Contracts for `omh_todo` action=advance, the single-item write.

What the action is for. `action=set` was the only write, so ticking one item
re-sent the whole list -- text, state and phase for every item -- at about
1.5 KB of arguments per call, 16 to 24 KB of bookkeeping for a ten-step plan,
and one chance per advance to drop or reword an item that was never meant to
change.

What these pin is that it is the SAME write reached by a narrower route, not
a second way to write a record. The acceptance that says so is byte-equality:
a plan advanced only through `advance` produces a record identical, byte for
byte, to the same plan advanced through `set` under a fixed clock. Everything
else here is a refusal, an invariant, or the concurrency the read-modify-write
introduced and the record lock removes.

The item reference is an index plus a guard, not an id. The record has no item
id and needs none for anything else -- adding one would move the on-disk
schema, the digest a deferral lapses on, and every surface that projects an
item, to carry a handle whose only reader would be this action. What a bare
index cannot do is refuse when the list moved underneath it, so `item_text`
is required and compared against the text stored at that position.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh import todo_store
from omh.plugin_bundle.omh.todo_store import (
    MAX_TODO_BLOCKED_REASON_CHARS,
    TodoContendedError,
    TodoStoreError,
    TodoValidationError,
    advance_todo_item,
    build_todo_record,
    todo_path,
    write_todo,
)
from omh.plugin_bundle.omh.tools.todo_tool import OMH_TODO_SCHEMA, omh_todo_handler

SESSION = "tui-session"
FIXED_CLOCK = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """`datetime.now` pinned, so two paths that stamp a record agree on when.

    A subclass rather than a Mock because `build_todo_record` calls
    `datetime.now(timezone.utc).isoformat()` and the result has to be a real
    datetime for the rest of that expression to work.
    """

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - stdlib signature
        return FIXED_CLOCK if tz is None else FIXED_CLOCK.astimezone(tz)


class _TodoHomeTest(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "omh"

    def write_plan(self, items, *, session_ref=SESSION, deferred_reason="", title="plan"):
        record = build_todo_record(
            title, items, source="omh_todo", session_ref=session_ref,
            deferred_reason=deferred_reason,
        )
        _ = write_todo(self.home, record)
        return record

    def stored(self, *, session_ref=SESSION) -> dict:
        return json.loads(todo_path(self.home, session_ref).read_text(encoding="utf-8"))

    def raw_bytes(self, *, session_ref=SESSION) -> bytes:
        return todo_path(self.home, session_ref).read_bytes()

    def advance(self, **kwargs):
        payload = {
            "source": "omh_todo",
            "session_ref": SESSION,
        }
        payload.update(kwargs)
        return advance_todo_item(self.home, **payload)


class ByteEqualityTest(_TodoHomeTest):
    """The acceptance: the two routes reach one record.

    Driven through the store rather than the tool so the comparison is of the
    bytes on disk, and under a frozen clock because the only field that could
    differ between two writes a few microseconds apart is the stamp.
    """

    ITEMS = [
        {"text": "land the fix", "state": "pending", "phase": "I. Delivery"},
        {"text": "open the PR", "state": "pending", "phase": "I. Delivery"},
        {"text": "watch CI", "state": "pending", "phase": "II. Evidence", "depth": 1},
    ]

    def _plan_advanced_through_set(self) -> bytes:
        home = Path(self._tmp.name) / "through-set"
        record = build_todo_record("plan", self.ITEMS, source="omh_todo", session_ref=SESSION)
        _ = write_todo(home, record)
        # Three whole-list writes, each one re-sending every item, which is
        # what a session without this action has to do to walk the plan.
        for index in range(3):
            items = [dict(item) for item in self.ITEMS]
            for done in range(index + 1):
                items[done]["state"] = "done"
            if index + 1 < len(items):
                items[index + 1]["state"] = "active"
            _ = write_todo(
                home,
                build_todo_record("plan", items, source="omh_todo", session_ref=SESSION),
            )
        return todo_path(home, SESSION).read_bytes()

    def _plan_advanced_through_advance(self) -> bytes:
        home = Path(self._tmp.name) / "through-advance"
        _ = write_todo(
            home,
            build_todo_record("plan", self.ITEMS, source="omh_todo", session_ref=SESSION),
        )
        for index, item in enumerate(self.ITEMS, start=1):
            _ = advance_todo_item(
                home,
                item=index,
                item_text=item["text"],
                state="done",
                source="omh_todo",
                session_ref=SESSION,
            )
            if index < len(self.ITEMS):
                _ = advance_todo_item(
                    home,
                    item=index + 1,
                    item_text=self.ITEMS[index]["text"],
                    state="active",
                    source="omh_todo",
                    session_ref=SESSION,
                )
        return todo_path(home, SESSION).read_bytes()

    def test_a_plan_walked_through_advance_is_byte_equal_to_one_walked_through_set(self):
        with patch.object(todo_store, "datetime", _FrozenDatetime):
            through_set = self._plan_advanced_through_set()
            through_advance = self._plan_advanced_through_advance()

        self.assertEqual(through_advance, through_set)
        # And the record really is the finished plan, so the equality is not
        # two identical failures.
        record = json.loads(through_advance.decode("utf-8"))
        self.assertEqual([item["state"] for item in record["items"]], ["done", "done", "done"])
        self.assertEqual(record["source"], "omh_todo")
        self.assertEqual(record["session_ref"], SESSION)

    def test_the_fields_advance_does_not_touch_come_back_unchanged(self):
        # Phase and depth are the two an item carries that a state change has
        # no business editing, and re-sending them by hand under `set` is the
        # exact place they get lost.
        _ = self.write_plan(self.ITEMS)

        _ = self.advance(item=3, item_text="watch CI", state="active")

        third = self.stored()["items"][2]
        self.assertEqual(third["phase"], "II. Evidence")
        self.assertEqual(third["depth"], 1)
        self.assertEqual(third["text"], "watch CI")
        self.assertEqual(third["state"], "active")


class RefusalTest(_TodoHomeTest):
    """Every way a call is refused, and the field each refusal names."""

    def setUp(self) -> None:
        super().setUp()
        _ = self.write_plan(
            [
                {"text": "land the fix", "state": "done"},
                {"text": "open the PR", "state": "active"},
            ]
        )

    def _refusal(self, **kwargs) -> str:
        with self.assertRaises(TodoValidationError) as caught:
            _ = self.advance(**kwargs)
        return str(caught.exception)

    def test_an_out_of_range_reference_names_the_field_and_the_count(self):
        for reference in (0, 3, 99, -1):
            with self.subTest(item=reference):
                message = self._refusal(item=reference, item_text="open the PR", state="done")
                self.assertIn("item", message)
                self.assertIn("2 items", message)

    def test_a_non_integer_reference_names_the_field(self):
        # `True` is `1` to Python and would otherwise tick the first item.
        for reference in (None, "2", 2.0, True, [2]):
            with self.subTest(item=reference):
                message = self._refusal(item=reference, item_text="open the PR", state="done")
                self.assertIn("item must be an integer", message)

    def test_a_missing_guard_names_the_field_and_says_what_it_is_for(self):
        message = self._refusal(item=2, item_text="", state="done")

        self.assertIn("item_text", message)
        self.assertIn("stale index", message)

    def test_a_guard_that_no_longer_matches_names_the_field_and_the_item(self):
        message = self._refusal(item=2, item_text="land the fix", state="done")

        self.assertIn("item_text", message)
        self.assertIn("open the PR", message)
        self.assertIn("action=show", message)

    def test_a_state_outside_the_enum_names_the_field(self):
        for state in ("", "blocked", "DONE", None, 3):
            with self.subTest(state=state):
                message = self._refusal(item=2, item_text="open the PR", state=state)
                self.assertIn("state must be one of", message)

    def test_a_finished_plan_is_refused_and_names_the_action_that_reopens_it(self):
        _ = self.write_plan(
            [{"text": "land the fix", "state": "done"}, {"text": "open the PR", "state": "done"}]
        )

        message = self._refusal(item=1, item_text="land the fix", state="active")

        self.assertIn("finished", message)
        self.assertIn("action=set", message)

    def test_an_absent_record_is_refused_rather_than_declaring_one(self):
        message = self._refusal(
            item=1, item_text="land the fix", state="done", session_ref="no-such-session"
        )

        self.assertIn("no todo record", message)
        self.assertIn("action=set", message)
        self.assertFalse(todo_path(self.home, "no-such-session").exists())

    def test_a_home_with_no_session_directory_reports_an_absent_record(self):
        # Not "the home is not writable", which is what taking a lock inside
        # a directory that does not exist reports. Two different problems and
        # only one of them is the caller's.
        empty = Path(self._tmp.name) / "never-used"

        with self.assertRaises(TodoValidationError) as caught:
            _ = advance_todo_item(
                empty,
                item=1,
                item_text="land the fix",
                state="done",
                source="omh_todo",
                session_ref=SESSION,
            )

        self.assertIn("no todo record", str(caught.exception))
        self.assertFalse(empty.exists())

    def test_an_over_length_blocked_reason_is_refused_by_the_shared_validator(self):
        message = self._refusal(
            item=2,
            item_text="open the PR",
            state="active",
            blocked_reason="x" * (MAX_TODO_BLOCKED_REASON_CHARS + 1),
        )

        self.assertIn("blocked_reason", message)
        self.assertIn(str(MAX_TODO_BLOCKED_REASON_CHARS), message)

    def test_a_refused_call_leaves_the_record_exactly_as_it_was(self):
        before = self.raw_bytes()

        for kwargs in (
            {"item": 9, "item_text": "open the PR", "state": "done"},
            {"item": 2, "item_text": "land the fix", "state": "done"},
            {"item": 2, "item_text": "open the PR", "state": "nope"},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(TodoValidationError):
                    _ = self.advance(**kwargs)

        self.assertEqual(self.raw_bytes(), before)

    def test_a_symlinked_record_path_is_refused_the_way_a_whole_list_write_is(self):
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        session_dir = self.home / "runtime" / "todos"
        for entry in session_dir.iterdir():
            entry.unlink()
        session_dir.rmdir()
        session_dir.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(TodoStoreError):
            _ = self.advance(item=1, item_text="land the fix", state="done")


class SharedValidatorTest(_TodoHomeTest):
    """The single-item path enforces what it enforces by REUSING the validator.

    There is no second copy of the item rules here, and these say so by
    reaching the store's own refusals through the narrow route.
    """

    def test_the_guard_and_the_state_strip_control_characters_the_same_way(self):
        # `strip_control_characters` is what makes a stored text comparable to
        # a supplied one, and a guard carrying an escape must not miss.
        _ = self.write_plan([{"text": "land the fix", "state": "active"}])

        _ = self.advance(item=1, item_text="land\x1b the fix", state="done")

        self.assertEqual(self.stored()["items"][0]["state"], "done")

    def test_the_record_it_writes_passes_the_same_item_rules_as_a_set(self):
        # A hand-written record with a field past its cap is repaired on the
        # way through, because the write goes through `validate_todo_items`
        # rather than around it.
        _ = self.write_plan([{"text": "land the fix", "state": "active"}])
        path = todo_path(self.home, SESSION)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["items"][0]["text"] = "x" * 400
        path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

        with self.assertRaises(TodoValidationError) as caught:
            _ = self.advance(item=1, item_text="x" * 20, state="done")

        self.assertIn("text is capped", str(caught.exception))

    def test_the_stamp_moves_on_every_advance_so_readers_see_the_plan_change(self):
        # Everything keyed on "the plan changed" reads `updated_at`: the
        # reconciliation turn budget, the HUD's unchanged duration, the
        # turn-end nudge budget. A single-item write is a write.
        _ = self.write_plan(
            [{"text": "land the fix", "state": "active"}, {"text": "open the PR", "state": "pending"}]
        )
        before = self.stored()["updated_at"]

        _ = self.advance(item=1, item_text="land the fix", state="done")

        self.assertNotEqual(self.stored()["updated_at"], before)
        self.assertGreater(self.stored()["updated_at"], before)


class BlockedReasonTest(_TodoHomeTest):
    """The item-level reason, set and cleared by the same route `set` uses."""

    def setUp(self) -> None:
        super().setUp()
        _ = self.write_plan(
            [
                {"text": "land the fix", "state": "done"},
                {"text": "open the PR", "state": "active"},
            ]
        )

    def test_a_reason_sent_with_the_state_is_recorded_on_that_item(self):
        _ = self.advance(
            item=2, item_text="open the PR", state="active", blocked_reason="waiting on review"
        )

        self.assertEqual(self.stored()["items"][1]["blocked_reason"], "waiting on review")

    def test_omitting_the_reason_clears_one_the_way_leaving_it_out_of_a_set_does(self):
        _ = self.advance(
            item=2, item_text="open the PR", state="active", blocked_reason="waiting on review"
        )

        _ = self.advance(item=2, item_text="open the PR", state="active")

        self.assertNotIn("blocked_reason", self.stored()["items"][1])

    def test_a_whitespace_reason_is_absence_rather_than_a_declaration(self):
        _ = self.advance(item=2, item_text="open the PR", state="active", blocked_reason="  \t ")

        self.assertNotIn("blocked_reason", self.stored()["items"][1])


class DeferralTest(_TodoHomeTest):
    """The plan-level field behaves identically on both routes.

    It has to, or there is a record state reachable by `set` and not by this,
    and the byte-equality above would be a claim about one path only.
    """

    def setUp(self) -> None:
        super().setUp()
        _ = self.write_plan(
            [
                {"text": "land the fix", "state": "done"},
                {"text": "open the PR", "state": "active"},
            ],
            deferred_reason="the person asked for the release notes first",
        )

    def test_an_advance_that_does_not_re_send_the_reason_lapses_the_deferral(self):
        _ = self.advance(item=2, item_text="open the PR", state="done")

        record = self.stored()
        self.assertNotIn("deferred_reason", record)
        self.assertNotIn("deferred_items_digest", record)

    def test_re_sending_the_reason_declares_it_against_the_new_item_list(self):
        _ = self.advance(
            item=2,
            item_text="open the PR",
            state="done",
            deferred_reason="the person asked for the release notes first",
        )

        record = self.stored()
        self.assertEqual(
            record["deferred_reason"], "the person asked for the release notes first"
        )
        self.assertEqual(
            record["deferred_items_digest"], todo_store.todo_items_digest(record["items"])
        )


class LostUpdateTest(_TodoHomeTest):
    """The read-modify-write is serialized against every other write.

    This is the one hazard the action introduces. `write_todo` was already
    atomic against a reader through `os.replace`, and nothing read a record
    in order to write one, so no lock was needed until now.

    Both tests below fail with the lock removed from `todo_store`, which is
    what makes them a proof rather than a description.
    """

    def test_concurrent_advances_on_one_record_all_land(self):
        count = 6
        _ = self.write_plan([{"text": f"task {index}", "state": "pending"} for index in range(count)])
        barrier = threading.Barrier(count)
        errors: list[BaseException] = []

        def tick(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                _ = self.advance(item=index + 1, item_text=f"task {index}", state="done")
            except BaseException as error:  # noqa: BLE001 - reported, not swallowed
                errors.append(error)

        threads = [threading.Thread(target=tick, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(
            [item["state"] for item in self.stored()["items"]], ["done"] * count
        )

    def test_a_whole_list_write_is_never_overwritten_by_a_list_read_before_it(self):
        # The interleave that loses a write without a lock: advance reads the
        # five-item list, `set` writes a six-item one, advance writes the five
        # it read. Whichever order the two actually run in, the record that
        # survives has six items -- the only way to see five is the lost
        # update.
        five = [{"text": f"task {index}", "state": "pending"} for index in range(5)]
        six = five + [{"text": "task 5", "state": "pending"}]
        for attempt in range(40):
            with self.subTest(attempt=attempt):
                _ = self.write_plan(five)
                barrier = threading.Barrier(2)
                errors: list[BaseException] = []

                def whole_list() -> None:
                    try:
                        barrier.wait(timeout=10)
                        _ = self.write_plan(six)
                    except BaseException as error:  # noqa: BLE001 - reported
                        errors.append(error)

                def one_item() -> None:
                    try:
                        barrier.wait(timeout=10)
                        _ = self.advance(item=1, item_text="task 0", state="done")
                    except BaseException as error:  # noqa: BLE001 - reported
                        errors.append(error)

                threads = [threading.Thread(target=whole_list), threading.Thread(target=one_item)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)

                self.assertEqual(errors, [])
                self.assertEqual(len(self.stored()["items"]), 6)


class ContentionRefusalTest(_TodoHomeTest):
    """What a writer is told when the record is busy, and that nothing changed.

    New for `set`, which could not fail this way before: it wrote through
    `os.replace` and took no lock. The refusal has to say which kind of
    failure it is, because the two ask for opposite responses -- a payload
    that was invalid wants different arguments, and a record that was busy
    wants the identical call again. A writer told `invalid_todo` about a lock
    would rewrite the list it just sent, which is the whole-list rewrite this
    change exists to stop.
    """

    def setUp(self) -> None:
        super().setUp()
        self._env_patch = patch.dict(os.environ, {"OMH_HOME": str(self.home)})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        _ = self.write_plan(
            [
                {"text": "land the fix", "state": "active"},
                {"text": "open the PR", "state": "pending"},
            ]
        )

    def _held(self):
        """Hold the record's own lock, the way another process would."""
        return todo_store._todo_record_lock(
            todo_store.todo_path(self.home, SESSION), root=self.home
        )

    def call(self, args: dict) -> dict:
        return json.loads(omh_todo_handler(args, session_id=SESSION))

    def test_a_contended_set_says_so_and_is_not_called_invalid(self):
        with patch.object(todo_store, "_LOCK_TIMEOUT_SECONDS", 0.05), self._held():
            result = self.call(
                {"action": "set", "title": "plan", "items": [{"text": "rewritten"}]}
            )

        self.assertEqual(result["status"], "contended")
        self.assertIn("was not written", result["error"])
        self.assertIn("send the same call again", result["error"])
        # And the record is exactly what it was: the refusal is not a partial
        # write that happened to report a failure.
        self.assertEqual(
            [item["text"] for item in self.stored()["items"]],
            ["land the fix", "open the PR"],
        )

    def test_a_contended_advance_reports_the_same_way(self):
        before = self.raw_bytes()

        with patch.object(todo_store, "_LOCK_TIMEOUT_SECONDS", 0.05), self._held():
            result = self.call(
                {"action": "advance", "item": 1, "item_text": "land the fix", "state": "done"}
            )

        self.assertEqual(result["status"], "contended")
        self.assertEqual(self.raw_bytes(), before)

    def test_the_contended_error_is_a_store_error_for_every_older_caller(self):
        # A subclass, so nothing written before the lock existed has to learn
        # a new failure to keep catching this one.
        self.assertTrue(issubclass(TodoContendedError, TodoStoreError))

        with patch.object(todo_store, "_LOCK_TIMEOUT_SECONDS", 0.05), self._held():
            with self.assertRaises(TodoStoreError):
                _ = self.advance(item=1, item_text="land the fix", state="done")

    def test_an_uncontended_write_never_reports_contention(self):
        # The other half: the status exists for a real condition, not as a
        # branch that fires whenever the lock is taken at all.
        result = self.call(
            {"action": "advance", "item": 1, "item_text": "land the fix", "state": "done"}
        )

        self.assertEqual(result["status"], "written")


class SharedLockTest(_TodoHomeTest):
    """The store takes the bundle's one lock rather than carrying a copy.

    The policy gate in `tests/test_journal_lock_portability.py` says no module
    outside a named list may call `fcntl.flock` itself, and the reason is the
    half a copy always loses: `awareness_delivery` grew its msvcrt branch
    after a Windows host was found taking no lock at all while reading as
    though it had one. A second implementation here would be a second chance
    to make that mistake, so these pin that there is only one.
    """

    def test_the_store_calls_no_lock_backend_of_its_own(self):
        source = Path(todo_store.__file__).read_text(encoding="utf-8")

        self.assertNotIn("fcntl.flock", source)
        self.assertNotIn("msvcrt.locking", source)
        self.assertIn("from .awareness_delivery import _awareness_delivery_lock", source)

    def test_the_shared_lock_file_is_the_one_the_prune_knows(self):
        # Two derivations of the same name -- the shared helper's, and this
        # module's `_lock_file_for` plus the `_LOCK_NAME` the prune matches on
        # -- so the name is measured against the file the helper really
        # writes rather than assumed to agree with it. If they ever part,
        # lock files accumulate in the session directory forever and nothing
        # else says so.
        destination = todo_store.todo_path(self.home, SESSION)
        _ = self.write_plan([{"text": "land the fix", "state": "active"}])

        with todo_store._todo_record_lock(destination, root=self.home):
            pass

        expected = todo_store._lock_file_for(destination)
        self.assertTrue(expected.exists())
        self.assertTrue(todo_store._LOCK_NAME.fullmatch(expected.name))
        self.assertEqual(
            sorted(p.name for p in destination.parent.iterdir()),
            sorted([destination.name, expected.name]),
        )

    def test_a_failure_inside_the_lock_is_not_relabelled_as_a_store_error(self):
        # The two handlers around the `with` see the caller's body as well as
        # the acquisition, and `TimeoutError` is an `OSError`, so the pair is
        # easy to get wrong in the direction that hides a real fault behind
        # "the destination is not writable".
        destination = todo_store.todo_path(self.home, SESSION)
        _ = self.write_plan([{"text": "land the fix", "state": "active"}])

        for raised in (OSError(13, "permission denied"), TimeoutError("something else")):
            with self.subTest(raised=type(raised).__name__):
                with self.assertRaises(type(raised)) as caught:
                    with todo_store._todo_record_lock(destination, root=self.home):
                        raise raised
                self.assertIs(caught.exception, raised)
                self.assertNotIsInstance(caught.exception, TodoStoreError)

    def test_the_deadline_this_store_passes_is_its_own_not_the_telemetry_one(self):
        # The shared default is sized for a counter that may be dropped. A
        # plan write is the opposite trade, and passing the deadline is what
        # let the lock be shared instead of forked.
        from omh.plugin_bundle.omh import awareness_delivery

        self.assertEqual(awareness_delivery._LOCK_TIMEOUT_SECONDS, 0.1)
        self.assertEqual(todo_store._LOCK_TIMEOUT_SECONDS, 2.0)

        seen = {}
        real = awareness_delivery._acquire_delivery_lock

        def record(handle, lock_path, timeout_seconds=awareness_delivery._LOCK_TIMEOUT_SECONDS):
            seen["timeout"] = timeout_seconds
            return real(handle, lock_path, timeout_seconds)

        _ = self.write_plan([{"text": "land the fix", "state": "active"}])
        with patch.object(awareness_delivery, "_acquire_delivery_lock", record):
            _ = self.advance(item=1, item_text="land the fix", state="done")

        self.assertEqual(seen["timeout"], 2.0)

    def test_a_host_with_no_lock_backend_still_keeps_a_plan(self):
        # The shared helper yields "none" and takes no lock. Refusing there
        # would make a platform without either backend unable to hold a plan
        # at all, which is worse than the race it would avoid, and it is the
        # behaviour every writer here had before the lock existed.
        from omh.plugin_bundle.omh import awareness_delivery

        _ = self.write_plan([{"text": "land the fix", "state": "active"}])
        with patch.object(awareness_delivery, "fcntl", None), patch.object(
            awareness_delivery, "msvcrt", None
        ):
            _ = self.advance(item=1, item_text="land the fix", state="done")

        self.assertEqual(self.stored()["items"][0]["state"], "done")


class ToolSurfaceTest(_TodoHomeTest):
    """The action as the model reaches it, through `omh_todo`."""

    def setUp(self) -> None:
        super().setUp()
        # The env rather than the function, because the handler writes through
        # `default_omh_home()` and reads back through
        # `runtime_paths.plugin_home()`; patching one of them would leave the
        # projection in the test process's shared temp home and the assertion
        # about it meaningless.
        self._env_patch = patch.dict(os.environ, {"OMH_HOME": str(self.home)})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def call(self, args: dict, **kwargs) -> dict:
        payload = {"session_id": SESSION}
        payload.update(kwargs)
        return json.loads(omh_todo_handler(args, **payload))

    def declare(self, session_id: str = SESSION) -> dict:
        return self.call(
            {
                "action": "set",
                "title": "plan",
                "items": [
                    {"text": "land the fix", "state": "active"},
                    {"text": "open the PR", "state": "pending"},
                ],
            },
            session_id=session_id,
        )

    def test_the_action_is_in_the_schema_next_to_the_ones_it_joins(self):
        action = OMH_TODO_SCHEMA["parameters"]["properties"]["action"]

        self.assertEqual(action["enum"], ["set", "advance", "clear", "show", "checkpoint", "record", "recall"])
        # The steering: a model reading this must reach for the cheap write.
        self.assertIn("Change a state with advance, not set", action["description"])
        for field in ("item", "item_text", "state"):
            self.assertIn(field, OMH_TODO_SCHEMA["parameters"]["properties"])
        self.assertEqual(
            OMH_TODO_SCHEMA["parameters"]["properties"]["state"]["enum"],
            ["pending", "active", "done"],
        )

    def test_a_tick_through_the_tool_writes_and_reports_the_new_projection(self):
        _ = self.declare()

        result = self.call(
            {"action": "advance", "item": 1, "item_text": "land the fix", "state": "done"}
        )

        self.assertEqual(result["status"], "written")
        self.assertEqual(result["todo"]["counts"]["done"], 1)
        self.assertEqual(self.stored()["items"][0]["state"], "done")

    def test_a_refusal_comes_back_as_invalid_todo_with_the_field_named(self):
        _ = self.declare()

        result = self.call(
            {"action": "advance", "item": 9, "item_text": "land the fix", "state": "done"}
        )

        self.assertEqual(result["status"], "invalid_todo")
        self.assertIn("out of range", result["error"])
        # The projection still comes back, so the model sees what is there.
        self.assertEqual(result["todo"]["counts"]["done"], 0)

    def test_an_operator_home_override_is_refused_on_this_write_too(self):
        # `set` and `clear` already refuse it: a caller-chosen path would turn
        # a metadata tool into an arbitrary-location file primitive, and a
        # single-item write is a write.
        _ = self.declare()

        result = self.call(
            {
                "action": "advance",
                "item": 1,
                "item_text": "land the fix",
                "state": "done",
                "omh_home": str(self.home),
            }
        )

        self.assertEqual(result["status"], "invalid_todo")
        self.assertIn("read-only", result["error"])
        self.assertEqual(self.stored()["items"][0]["state"], "active")

    def test_an_unknown_action_names_every_action_there_is(self):
        result = self.call({"action": "tick"})

        self.assertEqual(result["status"], "invalid_action")
        self.assertEqual(result["error"], "action must be set, advance, clear, show, checkpoint, record, or recall")

    def test_a_delegated_child_advances_its_own_record_and_not_its_parents(self):
        # The rule `set` follows and this inherits: the record is keyed by the
        # host session id passed as a KEYWORD, never by anything in args, so a
        # child running under its own session id has its own plan. A child
        # that never declared one is refused rather than reaching the parent's.
        _ = self.declare()
        child = "child-session"

        refused = self.call(
            {"action": "advance", "item": 1, "item_text": "land the fix", "state": "done"},
            session_id=child,
        )

        self.assertEqual(refused["status"], "invalid_todo")
        self.assertIn("no todo record", refused["error"])
        self.assertEqual(self.stored()["items"][0]["state"], "active")

        _ = self.declare(session_id=child)
        written = self.call(
            {"action": "advance", "item": 1, "item_text": "land the fix", "state": "done"},
            session_id=child,
        )

        self.assertEqual(written["status"], "written")
        self.assertEqual(self.stored(session_ref=child)["items"][0]["state"], "done")
        self.assertEqual(self.stored()["items"][0]["state"], "active")


class ReadersSeeItTest(_TodoHomeTest):
    """Everything that reads "the plan changed" sees a single-item write.

    The budget from #1739, the HUD projection and the turn-end nudge all key
    on the record's own `updated_at`, so the question is answered once, here,
    by driving the real hook rather than by reasoning about the field.
    """

    def test_the_reconciliation_budget_restarts_on_a_single_item_write(self):
        from omh.plugin_bundle.omh.hooks import nudge_budget
        from omh.plugin_bundle.omh.hooks.llm_hooks import pre_llm_call
        from omh.plugin_bundle.omh.todo_reconciliation import (
            TODO_RECONCILIATION_FULL_TURNS,
            TODO_RECONCILIATION_RULE,
        )

        nudge_budget.reset_nudge_budget()
        self.addCleanup(nudge_budget.reset_nudge_budget)
        hermes = Path(self._tmp.name) / "hermes"
        hermes.mkdir(parents=True, exist_ok=True)
        _ = self.write_plan(
            [
                {"text": "land the fix", "state": "active"},
                {"text": "open the PR", "state": "pending"},
            ]
        )

        def context() -> str:
            payload = pre_llm_call(
                omh_home=str(self.home),
                hermes_home=str(hermes),
                session_id=SESSION,
                user_message="",
            )
            return str((payload or {}).get("context", ""))

        for _ in range(TODO_RECONCILIATION_FULL_TURNS + 1):
            _ = context()
        self.assertNotIn(TODO_RECONCILIATION_RULE, context())

        _ = self.advance(item=1, item_text="land the fix", state="done")

        self.assertIn(TODO_RECONCILIATION_RULE, context())

    def test_the_hud_projection_reads_the_advanced_record(self):
        from omh.plugin_bundle.omh.runtime_reader import read_omh_todo

        _ = self.write_plan(
            [
                {"text": "land the fix", "state": "active"},
                {"text": "open the PR", "state": "pending"},
            ]
        )

        _ = self.advance(item=1, item_text="land the fix", state="done")
        _ = self.advance(item=2, item_text="open the PR", state="active")

        todo = read_omh_todo(str(self.home), session_ref=SESSION)
        self.assertEqual(todo["counts"]["done"], 1)
        self.assertEqual(todo["counts"]["active"], 1)
        self.assertEqual(todo["status"], "established")


if __name__ == "__main__":  # pragma: no cover - module entry point
    unittest.main()
