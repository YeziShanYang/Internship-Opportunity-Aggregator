"""The artifact codec has an inverse, and these are the tests that keep it having one.

A codec nobody checks is worse than no codec: every stage boundary would be a place a
field could go missing, and a half-decoded artifact looks exactly like a morning on
which nothing changed -- which is the failure this whole project is built to make
impossible (spec 10.1).

Two gates, from the plan:

* `loads(dumps(x)) == x` for every artifact shape.
* `dumps(loads(dumps(x))) == dumps(x)`, i.e. the text form is a fixed point. This is
  the one that catches `tuple` coming back as `list`: the objects can compare equal in
  a language that ignores the difference and still serialise differently.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core import codec, models, paths
from persist import artifacts


@dataclasses.dataclass(frozen=True)
class _Tupled:
    """Stands in for the shapes that hold a tuple -- FilterReport.samples and friends.

    Present as a fixture rather than waiting for the real one, because `tuple` -> `list`
    is the specific bug the plan calls out in `dataclasses.asdict`, and a test for it
    should not depend on which step happens to have landed.
    """

    name: str = ""
    samples: tuple[str, ...] = ()
    count: int = 0


@dataclasses.dataclass
class _Nested:
    label: str = ""
    change: models.Change = dataclasses.field(
        default_factory=lambda: models.Change("s", "added", "k", "d"))
    tupled: _Tupled = dataclasses.field(default_factory=_Tupled)
    maybe: str | None = None
    table: dict[str, models.Change] = dataclasses.field(default_factory=dict)


def _sample_change(n: int = 1) -> models.Change:
    return models.Change(
        source_id=f"src-{n}",
        kind="added",
        # A homoglyph canary and a pipe, because both survive into the digest and both
        # have broken a renderer here before.
        key="ꓟachine ꓡearning Researcher @ New York | NY",
        detail="New row: Type=Intern; URL=https://x.test/1\tsecond=tab inside a value",
        url="https://x.test/1",
        program_name="Board A|Board B",
        is_discovery_candidate=True,
        rolling=True,
        posting_text="Expected graduation date: 2029 or later.",
    )


def _sample_result() -> models.SourceResult:
    return models.SourceResult(
        source_id="src-1",
        ok=True,
        changes=[_sample_change(1), _sample_change(2)],
        error="",
        content_length=1234,
        snapshot_text="# sections\nA\n# rows\nA\tk\tv\n",
        baseline=False,
        snapshot_ext="tsv",
        extra={"rows": 51, "ratio": 0.0129, "redirected": "", "collapsed": 0},
        quarantined=False,
    )


CASES = (
    ("change", models.Change, _sample_change()),
    ("result", models.SourceResult, _sample_result()),
    ("results", list[models.SourceResult], [_sample_result(), _sample_result()]),
    ("tupled", _Tupled, _Tupled(name="x", samples=("a", "b"), count=2)),
    ("nested", _Nested, _Nested(label="l", maybe="m", table={"a": _sample_change()})),
    ("nested-none", _Nested, _Nested()),
    ("table", dict[str, models.Change], {"a": _sample_change(), "b": _sample_change(2)}),
    ("empty-list", list[models.Change], []),
    ("empty-table", dict[str, models.Change], {}),
)


class CodecRoundTripTests(unittest.TestCase):
    def test_loads_of_dumps_is_the_identity(self):
        for name, hint, obj in CASES:
            with self.subTest(name):
                self.assertEqual(codec.loads(name, hint, codec.dumps(name, obj)), obj)

    def test_the_text_form_is_a_fixed_point(self):
        """The tuple test. Two objects can compare equal and serialise differently, so
        equality alone would not catch `samples` coming back as a list."""
        for name, hint, obj in CASES:
            with self.subTest(name):
                once = codec.dumps(name, obj)
                self.assertEqual(codec.dumps(name, codec.loads(name, hint, once)), once)

    def test_a_tuple_field_comes_back_as_a_tuple(self):
        """Stated directly, because it is the reason this codec exists instead of
        asdict, and a fixed-point failure would not say which field did it."""
        obj = _Tupled(name="x", samples=("a", "b"))
        back = codec.loads("tupled", _Tupled, codec.dumps("tupled", obj))
        self.assertIsInstance(back.samples, tuple)
        self.assertEqual(hash(back), hash(obj), "frozen equality depends on it")

    def test_output_is_key_sorted_so_field_order_cannot_diff(self):
        text = codec.dumps("change", _sample_change())
        keys = list(json.loads(text)["payload"])
        self.assertEqual(keys, sorted(keys))
        self.assertTrue(text.endswith("\n"), "every file this repo writes ends in one")

    def test_non_ascii_stays_readable_in_the_file(self):
        """The homoglyph canaries and the emoji headings are the things a human reads
        an artifact to check. `\\uA4DF` escapes would defeat that."""
        self.assertIn("ꓟachine", codec.dumps("change", _sample_change()))


class CodecRefusalTests(unittest.TestCase):
    """Every way a stored artifact can be untrustworthy is an error, not a partial."""

    def test_a_version_mismatch_is_refused(self):
        text = codec.dumps("change", _sample_change()).replace(
            f'"version": {codec.VERSION}', '"version": 99')
        with self.assertRaises(codec.ArtifactError) as caught:
            codec.loads("change", models.Change, text)
        self.assertIn("version", str(caught.exception))

    def test_the_wrong_kind_is_refused(self):
        text = codec.dumps("enriched", _sample_change())
        with self.assertRaises(codec.ArtifactError):
            codec.loads("changes", models.Change, text)

    def test_a_missing_field_is_refused_rather_than_defaulted(self):
        """Filling in a default here would turn a shape change into a silently
        different answer, which is the whole reason the envelope carries a version."""
        payload = json.loads(codec.dumps("change", _sample_change()))
        del payload["payload"]["rolling"]
        with self.assertRaises(codec.ArtifactError) as caught:
            codec.loads("change", models.Change, json.dumps(payload))
        self.assertIn("rolling", str(caught.exception))

    def test_malformed_json_is_refused(self):
        with self.assertRaises(codec.ArtifactError):
            codec.loads("change", models.Change, "{not json")

    def test_a_value_with_no_json_form_is_refused_at_write_time(self):
        with self.assertRaises(codec.ArtifactError):
            codec.dumps("change", {"client": object()})


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(setattr, paths, "RUN_DIR", paths.RUN_DIR)
        paths.RUN_DIR = pathlib.Path(tmp.name) / ".run"

    def test_write_then_read_survives_a_real_file(self):
        artifacts.write(artifacts.CHANGES, "changes", [_sample_change()])
        self.assertEqual(
            artifacts.read(artifacts.CHANGES, "changes", list[models.Change]),
            [_sample_change()])

    def test_reading_an_artifact_that_was_never_written_says_which_stage_to_run(self):
        with self.assertRaises(codec.ArtifactError) as caught:
            artifacts.read(artifacts.JUDGED, "judged", list[models.Change])
        self.assertIn("run.py", str(caught.exception))

    def test_a_raw_body_round_trips_as_bytes_without_a_decode(self):
        """Bytes, not text: the encoding decision belongs to the parser in `process`,
        not to the fetch boundary, and a mis-decoded page is indistinguishable from a
        page that changed."""
        raw = b"\xff\xfe<html>caf\xc3\xa9</html>"
        artifacts.write_body("src-1", raw)
        self.assertEqual(artifacts.read_body("src-1"), raw)

    def test_a_body_that_was_never_fetched_reads_as_none_not_empty(self):
        self.assertIsNone(artifacts.read_body("never-fetched"))

    def test_reset_clears_a_previous_run(self):
        """Half of today's artifacts and half of yesterday's is the one state no stage
        can detect: the codec version would match and the content would be wrong."""
        artifacts.write_body("stale", b"x")
        artifacts.reset()
        self.assertIsNone(artifacts.read_body("stale"))
        self.assertTrue(paths.RUN_DIR.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
