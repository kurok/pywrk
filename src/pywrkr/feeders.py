"""Data feeders: CSV/JSON row sources that make each request different.

A scenario declares named data sets; each virtual user draws one row per data
set at the start of every iteration and references its columns as
``${dataset.column}``. That is what turns "1000 loops of one identical request"
into "1000 different users logging in".

Rows are read into memory once at startup — see :data:`FEEDER_ROW_WARN_LIMIT`
for the practical bound. Streaming very large files is deliberately out of
scope.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import random
from dataclasses import dataclass, field

from pywrkr.templating import TemplateFunctions, stringify

logger = logging.getLogger(__name__)

__all__ = [
    "FEEDER_STRATEGIES",
    "DataRuntime",
    "Feeder",
    "FeederCursor",
    "load_feeder",
    "shard_rows",
    "validate_unique_capacity",
]

#: How rows are handed out. ``loop`` wraps around forever; ``sequential`` and
#: ``unique`` consume each row at most once and stop the user when spent;
#: ``random`` picks uniformly with replacement.
FEEDER_STRATEGIES = ("loop", "sequential", "random", "unique")

#: Strategies under which a row is used at most once for the whole run. These
#: are the ones whose cursor can run dry, and the ones that have to be sharded
#: across nodes in distributed mode to stay globally unique.
CONSUMING_STRATEGIES = ("sequential", "unique")

#: Rows beyond this count draw a warning: everything is held in memory, so a
#: multi-million-row feeder is a footgun rather than a feature.
FEEDER_ROW_WARN_LIMIT = 1_000_000


@dataclass(frozen=True)
class Feeder:
    """A named data set: the rows themselves plus how they are handed out."""

    name: str
    strategy: str
    rows: tuple[dict[str, str], ...]
    source: str = ""  # file path, kept for error messages

    @property
    def fields(self) -> tuple[str, ...]:
        """Column names, taken from the first row."""
        return tuple(self.rows[0]) if self.rows else ()

    @property
    def consumes_rows(self) -> bool:
        """True when each row is used at most once for the whole run."""
        return self.strategy in CONSUMING_STRATEGIES


class FeederCursor:
    """Hands out rows for one data set according to its strategy.

    One cursor per data set per process, shared by every virtual user — that
    sharing is what makes ``unique`` actually unique rather than unique-per-user.
    """

    __slots__ = ("feeder", "_index", "_exhausted")

    def __init__(self, feeder: Feeder) -> None:
        self.feeder = feeder
        self._index = 0
        self._exhausted = False

    @property
    def exhausted(self) -> bool:
        """True once a consuming strategy has handed out its last row."""
        return self._exhausted

    def next_row(self) -> "dict[str, str] | None":
        """Return the next row, or None when a consuming strategy is spent."""
        rows = self.feeder.rows
        if not rows:
            self._exhausted = True
            return None
        if self.feeder.strategy == "random":
            return random.choice(rows)
        if self.feeder.strategy == "loop":
            row = rows[self._index % len(rows)]
            self._index += 1
            return row
        # sequential / unique: never reuse a row.
        if self._index >= len(rows):
            self._exhausted = True
            return None
        row = rows[self._index]
        self._index += 1
        return row


@dataclass
class DataRuntime:
    """The run's data-driven state, shared by every virtual user.

    Holds one cursor per data set plus the generator functions, so
    ``${dataset.field}`` draws from a shared pool and ``counter()`` is monotonic
    across the whole run.
    """

    cursors: dict[str, FeederCursor] = field(default_factory=dict)
    functions: TemplateFunctions = field(default_factory=TemplateFunctions)

    @classmethod
    def for_feeders(cls, feeders: "dict[str, Feeder] | None") -> "DataRuntime":
        """Build a runtime with a fresh cursor per feeder."""
        return cls(cursors={name: FeederCursor(f) for name, f in (feeders or {}).items()})

    def next_rows(self) -> "dict[str, dict[str, str]] | None":
        """Draw one row per data set for a user's next iteration.

        Returns None when a consuming data set has run dry, which is the signal
        for the calling user to stop rather than replay stale data.
        """
        rows: dict[str, dict[str, str]] = {}
        for name, cursor in self.cursors.items():
            row = cursor.next_row()
            if row is None:
                return None
            rows[name] = row
        return rows

    @property
    def exhausted_feeders(self) -> tuple[str, ...]:
        """Names of data sets that ran out of rows during the run."""
        return tuple(name for name, cursor in self.cursors.items() if cursor.exhausted)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _row_from_mapping(raw: object, source: str, index: int) -> dict[str, str]:
    """Coerce one parsed record into a flat row of strings."""
    if not isinstance(raw, dict):
        raise ValueError(
            f"{source}: record {index} must be an object, got {type(raw).__name__}; "
            f"a JSON data file must be a list of flat objects"
        )
    row: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{source}: record {index} has an empty field name")
        if isinstance(value, (dict, list)):
            raise ValueError(
                f"{source}: record {index} field {key!r} is {type(value).__name__}; "
                f"data rows must be flat -- nested values are not addressable as "
                f"${{name.field}}"
            )
        row[key] = stringify(value)
    if not row:
        raise ValueError(f"{source}: record {index} is empty")
    return row


def _load_json_rows(text: str, source: str) -> list[dict[str, str]]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{source}: not valid JSON: {exc}") from None
    if not isinstance(data, list):
        raise ValueError(
            f"{source}: must contain a JSON array of objects, got {type(data).__name__}"
        )
    return [_row_from_mapping(raw, source, i) for i, raw in enumerate(data)]


def _load_csv_rows(text: str, source: str) -> list[dict[str, str]]:
    reader = csv.reader(text.splitlines())
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError(f"{source}: file is empty; a CSV data file needs a header row") from None

    header = [name.strip() for name in header]
    if not any(header):
        raise ValueError(f"{source}: header row is blank")
    blank = [i for i, name in enumerate(header) if not name]
    if blank:
        raise ValueError(
            f"{source}: header column(s) {', '.join(str(i + 1) for i in blank)} have no name"
        )
    duplicates = sorted({name for name in header if header.count(name) > 1})
    if duplicates:
        raise ValueError(f"{source}: duplicate header column(s) {', '.join(duplicates)}")

    rows: list[dict[str, str]] = []
    for line_no, values in enumerate(reader, start=2):
        if not values or all(not v.strip() for v in values):
            continue  # tolerate blank lines, including a trailing newline
        if len(values) != len(header):
            raise ValueError(
                f"{source}: line {line_no} has {len(values)} value(s) but the header "
                f"declares {len(header)}"
            )
        rows.append(dict(zip(header, values)))
    return rows


def load_feeder(name: str, path: str, strategy: str = FEEDER_STRATEGIES[0]) -> Feeder:
    """Read a CSV or JSON data file into a :class:`Feeder`.

    The format is chosen by extension; anything that is not ``.json`` is read as
    CSV, where the header row supplies the field names.

    Args:
        name: Data-set name, referenced as ``${name.field}``.
        path: Path to the ``.csv`` or ``.json`` file.
        strategy: One of :data:`FEEDER_STRATEGIES`.

    Raises:
        ValueError: Unknown strategy or name, or the file is missing, empty, or
            malformed. Everything is reported here, at startup, rather than
            mid-run.
    """
    from pywrkr.templating import is_valid_var_name

    if not is_valid_var_name(name):
        raise ValueError(
            f"data set name {name!r} is not a valid ${{name.field}} identifier "
            f"(letters, digits and underscore; must not start with a digit)"
        )
    if strategy not in FEEDER_STRATEGIES:
        raise ValueError(
            f"data set {name!r}: unknown strategy {strategy!r}; "
            f"expected one of {', '.join(FEEDER_STRATEGIES)}"
        )
    if not os.path.isfile(path):
        raise ValueError(f"data set {name!r}: file not found: {path}")

    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        text = handle.read()
    if not text.strip():
        raise ValueError(f"data set {name!r}: file is empty: {path}")

    source = f"data set {name!r} ({path})"
    if os.path.splitext(path)[1].lower() == ".json":
        rows = _load_json_rows(text, source)
    else:
        rows = _load_csv_rows(text, source)

    if not rows:
        raise ValueError(f"{source}: header row present but no data rows")

    fields = set(rows[0])
    for i, row in enumerate(rows[1:], start=1):
        if set(row) != fields:
            missing = ", ".join(sorted(fields - set(row))) or "none"
            extra = ", ".join(sorted(set(row) - fields)) or "none"
            raise ValueError(
                f"{source}: record {i} has different fields than the first "
                f"(missing: {missing}; unexpected: {extra})"
            )

    if len(rows) > FEEDER_ROW_WARN_LIMIT:
        logger.warning(
            "%s: %d rows held in memory (over the %d-row guideline); consider a smaller sample",
            source,
            len(rows),
            FEEDER_ROW_WARN_LIMIT,
        )

    logger.debug("Loaded %s: %d rows, strategy=%s", source, len(rows), strategy)
    return Feeder(name=name, strategy=strategy, rows=tuple(rows), source=path)


# ---------------------------------------------------------------------------
# Startup capacity check and distributed sharding
# ---------------------------------------------------------------------------


def validate_unique_capacity(
    feeders: "dict[str, Feeder] | None",
    users: "int | None",
    num_requests: "int | None",
    steps: int,
    nodes: int = 1,
) -> None:
    """Fail before the run starts if a ``unique`` data set is too small.

    ``unique`` promises every row is used at most once, so a run that needs more
    iterations than there are rows cannot deliver the load it was asked for.
    Catching that here beats discovering it as a mysteriously short run.

    The demand we can actually predict is checked: one row per virtual user for
    their first iteration, and — when ``-n`` fixes the request count — one row
    per iteration needed to reach it. A duration-based run has no predictable
    iteration count, so only the per-user floor applies.

    Args:
        feeders: The scenario's data sets.
        users: Virtual user count, if in user-simulation mode.
        num_requests: Total request budget, if in request-count mode.
        steps: Steps per scenario iteration (used to convert requests to iterations).
        nodes: Worker nodes the rows will be split across in distributed mode.

    Raises:
        ValueError: A ``unique`` data set has fewer rows than the run needs.
    """
    concurrent = max(1, users or 1) * max(1, nodes)
    needed = concurrent
    if num_requests is not None and steps > 0:
        iterations = -(-num_requests // steps)  # ceil
        needed = max(needed, iterations)

    for name, feeder in (feeders or {}).items():
        if feeder.strategy != "unique":
            continue
        if len(feeder.rows) < needed:
            detail = f"{concurrent} concurrent user(s)"
            if num_requests is not None and steps > 0:
                detail += f" and {needed} iteration(s) to reach -n {num_requests}"
            raise ValueError(
                f"data set {name!r} has {len(feeder.rows)} row(s) but strategy 'unique' "
                f"needs at least {needed} for {detail}. Add rows, lower the load, or "
                f"use strategy 'loop'."
            )


def shard_rows(rows: "tuple[dict[str, str], ...]", index: int, count: int) -> tuple:
    """Return node *index*'s contiguous slice of *rows* out of *count* nodes.

    Splitting the rows up front is what keeps ``unique`` globally unique in
    distributed mode: each node consumes a disjoint range, so no row can be
    handed out twice. Earlier nodes take the extra rows when the split is uneven.
    """
    if count <= 1:
        return rows
    total = len(rows)
    base, remainder = divmod(total, count)
    start = index * base + min(index, remainder)
    size = base + (1 if index < remainder else 0)
    return rows[start : start + size]
