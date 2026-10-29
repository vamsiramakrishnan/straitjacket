"""The skeleton roster names sixteen languages; every one must extract.

Before this, grammar wheels existed for five (python, javascript,
typescript, go, rust) and the other eleven fell to universal-ctags, absent
on most machines, and then to nothing — `ctx map` advertised symbols the
verbs could not resolve. One fixture per language below, parsed by the
grammar itself; the expected rows were read off the parse trees, not
remembered. A language whose wheel is not installed is skipped, never
faked (the `[code]` extra installs all sixteen).
"""

from __future__ import annotations

import importlib

import pytest

from ctx.skeleton import _TS_GRAMMAR_MODULES, _tree_sitter_extract, language_for

FIXTURES: dict[str, tuple[str, str, set[tuple[str, str, str | None]], list[str]]] = {
    # language: (file name, source, expected (kind, name, scope) rows, expected imports)
    "c": ("a.c",
          '#include <stdio.h>\nstruct Point { int x; int y; };\nenum Color { RED, GREEN };\n'
          'typedef struct Point Point_t;\nstatic int add(int a, int b) {\n  return a + b;\n}\n'
          'int main(void) { return add(1, 2); }\n',
          {("struct", "Point", None), ("enum", "Color", None), ("type", "Point_t", None),
           ("function", "add", None), ("function", "main", None)},
          ["stdio.h"]),
    "c++": ("a.cpp",
            '#include <vector>\nnamespace geo {\nclass Shape {\npublic:\n  Shape();\n'
            '  virtual double area() const { return 0; }\n  void scale(double f);\n  static int count;\n};\n'
            'struct Point { int x; };\ntemplate <typename T> T twice(T v) { return v * 2; }\n}\n'
            'void geo::Shape::scale(double f) {}\nint helper(int a) { return a; }\n',
            {("class", "Shape", "geo"), ("method", "Shape", "Shape"), ("method", "area", "Shape"),
             ("method", "scale", "Shape"), ("struct", "Point", "geo"), ("function", "twice", "geo"),
             ("function", "scale", None), ("function", "helper", None)},
            ["vector"]),
    "c#": ("a.cs",
           'using System;\nnamespace Demo {\n  public interface IShape { double Area(); }\n'
           '  public class Circle : IShape {\n    public Circle(double r) { R = r; }\n'
           '    public double R { get; }\n    public double Area() { return 3.14 * R * R; }\n  }\n'
           '  public enum Color { Red, Green }\n  public struct Vec { public int X; }\n'
           '  public record Pair(int A, int B);\n}\n',
           {("interface", "IShape", "Demo"), ("method", "Area", "IShape"), ("class", "Circle", "Demo"),
            ("method", "Circle", "Circle"), ("property", "R", "Circle"), ("method", "Area", "Circle"),
            ("enum", "Color", "Demo"), ("struct", "Vec", "Demo"), ("record", "Pair", "Demo")},
           ["System"]),
    "java": ("A.java",
             'import java.util.List;\npublic class Shape {\n  private int x;\n'
             '  public Shape(int x) { this.x = x; }\n  public int area() { return x * x; }\n}\n'
             'interface Drawable { void draw(); }\nenum Color { RED, GREEN }\nrecord Pair(int a, int b) {}\n',
             {("class", "Shape", None), ("method", "Shape", "Shape"), ("method", "area", "Shape"),
              ("interface", "Drawable", None), ("method", "draw", "Drawable"), ("enum", "Color", None),
              ("record", "Pair", None)},
             ["java.util.List"]),
    "kotlin": ("A.kt",
               'import kotlin.math.abs\nclass Shape(val x: Int) {\n  fun area(): Int {\n    return x * x\n  }\n}\n'
               'object Registry {\n  fun get(): Int {\n    return 1\n  }\n}\ninterface Drawable {\n  fun draw()\n}\n'
               'fun helper(a: Int): Int {\n  return abs(a)\n}\n',
               {("class", "Shape", None), ("method", "area", "Shape"), ("object", "Registry", None),
                ("method", "get", "Registry"), ("interface", "Drawable", None), ("method", "draw", "Drawable"),
                ("function", "helper", None)},
               ["kotlin.math.abs"]),
    "lua": ("a.lua",
            'local M = {}\nfunction M.area(x)\n  return x * x\nend\nlocal function helper(a)\n  return a\nend\n'
            'function Shape:scale(f)\n  self.x = self.x * f\nend\nreturn M\n',
            {("function", "M.area", None), ("function", "helper", None), ("function", "Shape.scale", None)},
            []),
    "php": ("a.php",
            '<?php\nnamespace App;\nuse Foo\\Bar;\ninterface Drawable { public function draw(); }\n'
            'class Shape implements Drawable {\n  public function __construct(private int $x) {}\n'
            '  public function area(): int { return $this->x * $this->x; }\n  public function draw() {}\n}\n'
            'trait Scales { public function scale($f) {} }\nenum Color { case Red; }\n'
            'function helper(int $a): int { return $a; }\n',
            {("interface", "Drawable", None), ("method", "draw", "Drawable"), ("class", "Shape", None),
             ("method", "__construct", "Shape"), ("method", "area", "Shape"), ("trait", "Scales", None),
             ("method", "scale", "Scales"), ("enum", "Color", None), ("function", "helper", None)},
            ["Foo\\Bar"]),
    "ruby": ("a.rb",
             'require "json"\nmodule Geo\n  class Shape\n    def initialize(x)\n      @x = x\n    end\n'
             '    def area\n      @x * @x\n    end\n    def self.unit\n      new(1)\n    end\n  end\nend\n'
             'def helper(a)\n  a\nend\n',
             {("module", "Geo", None), ("class", "Shape", "Geo"), ("method", "initialize", "Shape"),
              ("method", "area", "Shape"), ("method", "unit", "Shape"), ("function", "helper", None)},
             ["json"]),
    "scala": ("A.scala",
              'import scala.math.abs\nclass Shape(x: Int) {\n  def area: Int = x * x\n}\n'
              'object Registry { def get(): Int = 1 }\ntrait Drawable { def draw(): Unit }\n'
              'def helper(a: Int): Int = abs(a)\n',
              {("class", "Shape", None), ("method", "area", "Shape"), ("object", "Registry", None),
               ("method", "get", "Registry"), ("trait", "Drawable", None), ("method", "draw", "Drawable"),
               ("function", "helper", None)},
              ["scala.math.abs"]),
    "shell": ("a.sh",
              '#!/bin/bash\nsource lib.sh\nhelper() {\n  echo "$1"\n}\nfunction area {\n  echo $(( $1 * $1 ))\n}\n',
              {("function", "helper", None), ("function", "area", None)},
              ["lib.sh"]),
    "swift": ("A.swift",
              'import Foundation\nprotocol Drawable { func draw() }\nclass Shape: Drawable {\n  var x: Int\n'
              '  init(x: Int) { self.x = x }\n  func area() -> Int { return x * x }\n  func draw() {}\n}\n'
              'struct Point { var x: Int }\nenum Color { case red }\nfunc helper(_ a: Int) -> Int { return a }\n',
              {("protocol", "Drawable", None), ("class", "Shape", None), ("method", "init", "Shape"),
               ("method", "area", "Shape"), ("method", "draw", "Shape"), ("struct", "Point", None),
               ("enum", "Color", None), ("function", "helper", None)},
              ["Foundation"]),
}


def _grammar_available(language: str) -> bool:
    for mod in _TS_GRAMMAR_MODULES[language]:
        try:
            importlib.import_module(mod)
            return True
        except Exception:
            continue
    return False


@pytest.mark.parametrize("language", sorted(FIXTURES))
def test_tree_sitter_extracts_every_declared_language(language):
    if not _grammar_available(language):
        pytest.skip(f"{language}: grammar wheel not installed (pip install 'ctx-harness[code]')")
    name, source, expected, imports = FIXTURES[language]
    assert language_for(name) == language
    symbols, found_imports = _tree_sitter_extract(source, language)
    rows = {(s["kind"], s["name"], s["scope"]) for s in symbols}
    missing = expected - rows
    assert not missing, f"{language}: missing {sorted(missing)}; got {sorted(rows)}"
    for s in symbols:
        a, b = s["range"]
        assert 1 <= a <= b, s
    assert found_imports == imports


def test_every_roster_language_has_a_grammar_wheel_declared():
    from ctx.skeleton import _LANG_BY_EXT

    assert set(_LANG_BY_EXT.values()) <= set(_TS_GRAMMAR_MODULES)


def test_partial_parse_still_yields_the_symbols_around_the_error():
    if not _grammar_available("java"):
        pytest.skip("java grammar wheel not installed")
    src = 'class A {\n  int f() { return 1; }\n}\nclass B { int g( { }\nclass C {\n  int h() { return 3; }\n}\n'
    symbols, _ = _tree_sitter_extract(src, "java")
    names = {s["name"] for s in symbols}
    assert {"A", "f", "C", "h"} <= names
