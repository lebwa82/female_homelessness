"""Compile customer-facing HTML copy; links remain data, never executable actions."""

from __future__ import annotations

import argparse
import difflib
import json
from html.parser import HTMLParser
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "docs/nevidimy-bot-scenario-final.html"
TARGET = SOURCE.parents[1] / "app/scenario_copy.json"


class ScenarioParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.screens = {}
        self.screen = None
        self.depth = 0
        self.capture = None
        self.parts = []
        self.capture_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in {"br", "hr", "img", "meta", "link", "input"}:
            self.depth += 1
        if tag == "section" and "screen" in attrs.get("class", "").split():
            self.screen = {"texts": [], "buttons": []}
            screen_id = attrs["id"]
            if screen_id in self.screens:
                raise ValueError("Duplicate screen ID")
            self.screens[screen_id] = self.screen
        if self.screen is None:
            return
        if tag == "div" and "bubble" in attrs.get("class", "").split():
            self.capture, self.parts, self.capture_depth = "text", [], self.depth
        elif tag == "a" and "key" in attrs.get("class", "").split():
            self.capture, self.parts, self.capture_depth = "button", [], self.depth
            self.target = attrs["href"].removeprefix("#")
        elif self.capture == "text" and tag in {"p", "li", "br"}:
            self.parts.append("\n\n" if tag == "p" else "\n")
            if tag == "li":
                self.parts.append("• ")

    def handle_endtag(self, tag):
        if self.capture and self.depth == self.capture_depth:
            value = "\n".join(" ".join(line.split()) for line in "".join(self.parts).split("\n"))
            value = value.strip()
            if self.capture == "text":
                self.screen["texts"].append(value)
            else:
                self.screen["buttons"].append({"target": self.target, "label": value})
            self.capture = None
        if tag == "section":
            self.screen = None
        self.depth -= 1

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)


def compile_copy(source: str) -> str:
    parser = ScenarioParser()
    parser.feed(source)
    for screen in parser.screens.values():
        if any(b["target"] not in parser.screens for b in screen["buttons"]):
            raise ValueError("Unknown screen link")
    return json.dumps(parser.screens, ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--patch", action="store_true")
    options = args.parse_args()
    compiled = compile_copy(SOURCE.read_text())
    old = TARGET.read_text() if TARGET.exists() else ""
    if options.patch:
        print("*** Begin Patch")
        if TARGET.exists():
            print(f"*** Update File: {TARGET}")
            lines = list(difflib.unified_diff(old.splitlines(), compiled.splitlines(), lineterm=""))[2:]
            print("\n".join("@@" if line.startswith("@@") else line for line in lines))
        else:
            print(f"*** Add File: {TARGET}")
            print("\n".join("+" + line for line in compiled.splitlines()))
        print("*** End Patch")
    else:
        print(json.dumps({"screens": len(json.loads(compiled)), "up_to_date": old == compiled}))
        raise SystemExit(0 if old == compiled else 1)
