"""presets.toml → parameter objects (pure apart from reading the file)."""

import dataclasses
import os
import tomllib
from dataclasses import dataclass

from .absolute import AbsoluteParams
from .direction import DirectionParams
from .stick import StickParams

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets.toml")


@dataclass(frozen=True)
class RelativePreset:
    name: str
    label: str
    direction: DirectionParams
    stick: StickParams
    pointer: str = "ziel"


@dataclass(frozen=True)
class AbsolutePreset:
    name: str
    label: str
    params: AbsoluteParams


@dataclass(frozen=True)
class Presets:
    min_size: float
    show_all: bool
    relative: tuple     # RelativePreset, in file order
    absolute: tuple     # AbsolutePreset

    def find(self, kind, name):
        for p in getattr(self, kind):
            if p.name == name:
                return p
        return None


def _pick(cls, table):
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in table.items() if k in names})


def parse_presets(data):
    general = data.get("general", {})
    relative = tuple(
        RelativePreset(name, t.get("label", name), _pick(DirectionParams, t), _pick(StickParams, t),
                       t.get("pointer", "ziel"))
        for name, t in data.get("relative", {}).items())
    absolute = tuple(
        AbsolutePreset(name, t.get("label", name), _pick(AbsoluteParams, t))
        for name, t in data.get("absolute", {}).items())
    if not relative or not absolute:
        raise ValueError("presets need at least one [relative.*] and one [absolute.*]")
    return Presets(float(general.get("min_size", 0)), bool(general.get("show_all", False)),
                   relative, absolute)


def load_presets(path=DEFAULT_PATH):
    with open(path, "rb") as f:
        return parse_presets(tomllib.load(f))
