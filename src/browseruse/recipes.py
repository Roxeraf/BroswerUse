"""Procedures the assistant has learned, and can repeat.

A one-off agent re-derives everything from scratch each time. The difference
between that and an assistant is that an assistant *remembers how you do
things* -- and repeating a known procedure should not cost a model call per
step.

The hard part is that element indices are meaningless across runs: [12] is the
notifications link today and a cookie button tomorrow. So a recipe does not
store indices. It stores **intent** -- the description Claude already writes
for every action -- and replay resolves intent back to an element with Jev's
Choice primitive, at roughly a five-thousandth of the cost of a Claude turn.

What a recipe deliberately cannot do is launder an approval. A step that needed
your yes when it was recorded asks again on every replay, forever. "I approved
it once" must never quietly become "it does this unattended".
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Arguments that are never replayed: they point at a page that no longer exists.
_VOLATILE_ARGS = frozenset({"index"})

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    return _SLUG.sub("-", name.strip().lower()).strip("-") or "recipe"


@dataclass
class RecipeStep:
    """One remembered action, described by what it was for."""

    action: str
    args: dict[str, Any] = field(default_factory=dict)
    #: How Claude described the target. This is what makes replay possible.
    target_description: str | None = None
    #: Where the page was when this ran, as a sanity check on replay.
    url_before: str = ""
    #: True if this step needed the user's approval. Always re-asked.
    needed_approval: bool = False

    @property
    def needs_element(self) -> bool:
        return self.action in {"click", "type_text", "select_option"}

    def replay_args(self, parameters: dict[str, str]) -> dict[str, Any]:
        """The stored arguments with placeholders filled and indices dropped."""
        out: dict[str, Any] = {}
        for key, value in self.args.items():
            if key in _VOLATILE_ARGS:
                continue
            if isinstance(value, str):
                for name, replacement in parameters.items():
                    value = value.replace(f"{{{{{name}}}}}", replacement)
            out[key] = value
        return out

    def placeholders(self) -> set[str]:
        found: set[str] = set()
        for value in self.args.values():
            if isinstance(value, str):
                found.update(re.findall(r"\{\{(\w+)\}\}", value))
        return found


@dataclass
class Recipe:
    """A named procedure, learned from a run that worked."""

    name: str
    goal: str
    steps: list[RecipeStep] = field(default_factory=list)
    created: str = ""
    last_run: str = ""
    runs: int = 0
    successes: int = 0

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def reliability(self) -> float | None:
        return self.successes / self.runs if self.runs else None

    def placeholders(self) -> set[str]:
        """Every ``{{name}}`` across the recipe, for the run command to fill."""
        found: set[str] = set()
        for step in self.steps:
            found |= step.placeholders()
        return found

    def summary(self) -> str:
        rate = (
            f"{self.reliability:.0%} of {self.runs}" if self.runs else "never run"
        )
        params = f"  params: {', '.join(sorted(self.placeholders()))}" if self.placeholders() else ""
        return f"{self.name:<28} {len(self.steps):>2} steps   {rate:<16}{params}"

    # -- storage ---------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "Recipe":
        data = json.loads(raw)
        steps = [RecipeStep(**step) for step in data.pop("steps", [])]
        return cls(steps=steps, **data)


class RecipeBook:
    """The recipes on disk, one JSON file each so you can read and edit them."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, name: str) -> Path:
        return self.directory / f"{slugify(name)}.json"

    def save(self, recipe: Recipe) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        if not recipe.created:
            recipe.created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path = self._path(recipe.name)
        path.write_text(recipe.to_json())
        return path

    def load(self, name: str) -> Recipe | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            return Recipe.from_json(path.read_text())
        except (json.JSONDecodeError, TypeError):
            return None

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if not path.exists():
            return False
        path.unlink()
        return True

    def all(self) -> list[Recipe]:
        if not self.directory.exists():
            return []
        found = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                found.append(Recipe.from_json(path.read_text()))
            except (json.JSONDecodeError, TypeError):
                continue  # a hand-edited file that no longer parses
        return found

    def record_run(self, recipe: Recipe, *, succeeded: bool) -> None:
        recipe.runs += 1
        recipe.successes += int(succeeded)
        recipe.last_run = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.save(recipe)


#: Actions worth remembering. `done` is the agent finishing, not a step, and
#: reads are re-derived on replay anyway.
_SKIP_WHEN_LEARNING = frozenset({"done", "list_tabs", "wait", "extract_text"})


def learn(name: str, goal: str, steps: list[Any]) -> Recipe:
    """Turn the steps of a successful run into a repeatable procedure.

    A step with no description is kept only if it needs no element -- a
    scroll or a navigate replays fine without one, a click does not.
    """
    remembered: list[RecipeStep] = []
    for record in steps:
        if record.action in _SKIP_WHEN_LEARNING:
            continue
        if record.approved is False:
            continue  # declined or blocked -- never learn it
        description = record.args.get("target_description")
        step = RecipeStep(
            action=record.action,
            args=dict(record.args),
            target_description=description,
            needed_approval=record.approved is True,
        )
        if step.needs_element and not description:
            continue
        remembered.append(step)
    return Recipe(name=name, goal=goal, steps=remembered)
