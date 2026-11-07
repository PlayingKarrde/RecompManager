"""Recipes on disk: ``/config/recipes/<id>.yaml``, plus built-ins shipped with the server.

The YAML file is the source of truth for a game's definition; edit it by hand or
through the API. Built-in recipes are templates: adding a game whose repo has a
built-in recipe copies that recipe into ``/config/recipes``.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import yaml
from pydantic import ValidationError

from recompmanager.recipes.schema import Recipe

BUILTIN_DIR = Path(__file__).parent / "builtin"


class RecipeError(Exception):
    pass


def text_hash(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode()).hexdigest()


def parse(text: str, origin: str = "recipe") -> Recipe:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RecipeError(f"{origin}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise RecipeError(f"{origin}: expected a mapping at the top level")
    try:
        return Recipe.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
            for err in e.errors()
        )
        raise RecipeError(f"{origin}: {problems}") from e


def dump(recipe: Recipe) -> str:
    data = recipe.model_dump(mode="json", exclude_defaults=True)
    # Keep the identifying fields even when they equal defaults.
    data = {"id": recipe.id, "name": recipe.name, **data}
    data["source"] = recipe.source.model_dump(mode="json")
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


class RecipeStore:
    def __init__(self, recipes_dir: Path, builtin_dir: Path = BUILTIN_DIR) -> None:
        self.dir = recipes_dir
        self.builtin_dir = builtin_dir
        self._cache: dict[str, tuple[float, Recipe]] = {}
        self._builtins: dict[str, Recipe] | None = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- built-ins

    def builtins(self) -> dict[str, Recipe]:
        if self._builtins is None:
            out: dict[str, Recipe] = {}
            for p in sorted(self.builtin_dir.glob("*.yaml")):
                r = parse(p.read_text(encoding="utf-8"), p.name)
                out[r.id] = r
            self._builtins = out
        return self._builtins

    def builtin_for_repo(self, repo: str) -> Recipe | None:
        repo = repo.lower()
        for r in self.builtins().values():
            if r.source.repo.lower() == repo:
                return r
        return None

    # ---------------------------------------------------------------- library recipes

    def path(self, recipe_id: str) -> Path:
        return self.dir / f"{recipe_id}.yaml"

    def exists(self, recipe_id: str) -> bool:
        return self.path(recipe_id).is_file()

    def load(self, recipe_id: str) -> Recipe:
        p = self.path(recipe_id)
        try:
            mtime = p.stat().st_mtime
        except FileNotFoundError as e:
            raise RecipeError(f"{p.name} is missing") from e
        with self._lock:
            hit = self._cache.get(recipe_id)
            if hit and hit[0] == mtime:
                return hit[1]
        recipe = parse(p.read_text(encoding="utf-8"), p.name)
        if recipe.id != recipe_id:
            raise RecipeError(f"{p.name}: id is {recipe.id!r}, expected {recipe_id!r}")
        with self._lock:
            self._cache[recipe_id] = (mtime, recipe)
        return recipe

    def save(self, recipe: Recipe) -> str:
        """Write the recipe file; returns its sha256 (to notice hand edits later)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path(recipe.id)
        tmp = p.with_suffix(".yaml.tmp")
        text = dump(recipe)
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, p)
        with self._lock:
            self._cache.pop(recipe.id, None)
        return text_hash(text)

    def file_hash(self, recipe_id: str) -> str | None:
        try:
            return text_hash(self.path(recipe_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def builtin_as(self, builtin: Recipe, recipe_id: str) -> tuple[Recipe, str]:
        """A built-in recipe under a library id, and the hash of its file text."""
        r = builtin.model_copy(update={"id": recipe_id})
        return r, text_hash(dump(r))

    def delete(self, recipe_id: str) -> None:
        """Keeps a backup next to the recipes, in case it was hand-edited."""
        p = self.path(recipe_id)
        if p.exists():
            os.replace(p, p.with_suffix(".yaml.deleted"))
        with self._lock:
            self._cache.pop(recipe_id, None)
