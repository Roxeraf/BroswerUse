"""Learning a procedure, and repeating it without a model call per step."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from browseruse.agent.loop import StepRecord
from browseruse.recipes import Recipe, RecipeBook, RecipeStep, learn, slugify


def record(action, args, approved=None):
    return StepRecord(action=action, args=args, result="ok", approved=approved)


def test_a_finished_run_becomes_a_procedure():
    steps = [
        record("navigate", {"url": "https://github.com/notifications"}),
        record("click", {"index": 12, "target_description": "the Notifications link"}),
        record("done", {"summary": "listed them"}),
    ]
    recipe = learn("Check GitHub", "open my notifications", steps)

    assert [s.action for s in recipe.steps] == ["navigate", "click"], "done is not a step"
    assert recipe.steps[1].target_description == "the Notifications link"


def test_indices_are_never_stored():
    """[12] is the notifications link today and a cookie button tomorrow."""
    recipe = learn("x", "g", [record("click", {"index": 12, "target_description": "Search"})])
    assert "index" not in recipe.steps[0].replay_args({})


def test_a_click_with_no_description_is_not_learned():
    """Without an intent there is nothing to match against on replay."""
    recipe = learn("x", "g", [record("click", {"index": 3})])
    assert recipe.steps == []


def test_a_declined_step_is_never_learned():
    steps = [
        record("click", {"index": 1, "target_description": "Pay now"}, approved=False),
        record("scroll", {"direction": "down"}),
    ]
    recipe = learn("x", "g", steps)
    assert [s.action for s in recipe.steps] == ["scroll"]


def test_a_step_that_needed_approval_is_marked_as_such():
    recipe = learn("x", "g", [
        record("click", {"index": 1, "target_description": "Submit order"}, approved=True)
    ])
    assert recipe.steps[0].needed_approval is True


def test_reads_are_not_remembered():
    steps = [record("extract_text", {}), record("wait", {"seconds": 2}),
             record("scroll", {"direction": "down"})]
    assert [s.action for s in learn("x", "g", steps).steps] == ["scroll"]


# -- parameters ----------------------------------------------------------

def test_placeholders_are_filled_at_run_time():
    step = RecipeStep("type_text", {"text": "flights to {{city}}", "press_enter": True})
    assert step.replay_args({"city": "Lisbon"})["text"] == "flights to Lisbon"


def test_a_recipe_advertises_what_it_needs():
    recipe = Recipe("trip", "book a trip", [
        RecipeStep("type_text", {"text": "{{origin}} to {{destination}}"}),
        RecipeStep("type_text", {"text": "{{date}}"}),
    ])
    assert recipe.placeholders() == {"origin", "destination", "date"}


def test_non_string_arguments_survive_substitution():
    step = RecipeStep("type_text", {"text": "{{q}}", "press_enter": True, "clear_first": False})
    args = step.replay_args({"q": "shoes"})
    assert args["press_enter"] is True and args["clear_first"] is False


# -- storage -------------------------------------------------------------

def test_recipes_round_trip_through_disk(tmp_path):
    book = RecipeBook(tmp_path)
    original = learn("Check GitHub", "open my notifications", [
        record("navigate", {"url": "https://github.com"}),
        record("click", {"index": 1, "target_description": "Notifications"}),
    ])
    book.save(original)

    loaded = book.load("check-github")
    assert loaded is not None
    assert loaded.goal == original.goal
    assert [s.target_description for s in loaded.steps] == [None, "Notifications"]


def test_names_normalise_so_you_can_type_them_loosely(tmp_path):
    book = RecipeBook(tmp_path)
    book.save(Recipe("Check My GitHub!", "g"))

    assert book.load("check my github") is not None
    assert book.load("Check-My-GitHub") is not None
    assert slugify("Check My GitHub!") == "check-my-github"


def test_run_statistics_accumulate(tmp_path):
    book = RecipeBook(tmp_path)
    recipe = Recipe("r", "g")
    book.save(recipe)

    book.record_run(recipe, succeeded=True)
    book.record_run(recipe, succeeded=False)

    assert book.load("r").runs == 2
    assert book.load("r").successes == 1
    assert recipe.reliability == pytest.approx(0.5)


def test_a_hand_edited_file_that_no_longer_parses_is_skipped(tmp_path):
    book = RecipeBook(tmp_path)
    book.save(Recipe("good", "g"))
    (tmp_path / "broken.json").write_text("{not json")

    assert [r.name for r in book.all()] == ["good"]
    assert book.load("broken") is None


def test_deleting_a_recipe(tmp_path):
    book = RecipeBook(tmp_path)
    book.save(Recipe("gone", "g"))

    assert book.delete("gone") is True
    assert book.delete("gone") is False
    assert book.all() == []
