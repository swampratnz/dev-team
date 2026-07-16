"""A greenfield visual baseline for web-frontend deliveries.

The delivery pipeline gates on *correctness* (tests pass) and *safety*
(security review), but nothing in the loop ever looks at a rendered page — so a
from-scratch web app comes out functional and visually spartan. This module
raises the floor with **static guidance**: when a delivery renders a UI, a
curated design baseline is folded into the house-conventions text that already
reaches the engineer (which it builds to) and the reviewer (which flags
deviations, at minor severity — a nudge, never a gate).

It is deliberately blind: it cannot see the result, only encourage sane
defaults. Judging the rendered page needs a browser + vision loop, which is out
of scope here (see ``docs/ROADMAP.md``).
"""

from __future__ import annotations

from .models import Design, FeatureRequest

#: Distinctive, high-precision substrings that mark a web-frontend delivery.
#: Chosen to avoid false matches inside unrelated words (e.g. no bare "ui" /
#: "page" / "react", which hide in "build" / "package" / "reactions").
_FRONTEND_SIGNALS = (
    "html",
    "css",
    "stylesheet",
    "jinja",
    "template",
    "frontend",
    "front-end",
    "server-rendered",
    "server rendered",
    "responsive",
    "tailwind",
    "bootstrap",
    "svelte",
    "user interface",
    "web app",
    "web page",
    "webpage",
    "viewport",
)

#: The design baseline. Framework-agnostic tokens + layout + component and
#: accessibility defaults, written as prompt guidance the engineer applies and
#: the reviewer checks. Bounded, like the other prompt sections.
FRONTEND_GUIDANCE = """\
When this delivery renders a web UI, meet a baseline of visual craft rather than
shipping unstyled browser defaults. Define these shared design tokens once and
reference them everywhere (as CSS custom properties in a single stylesheet for a
no-build/vanilla project, or as the framework's theme when one is in use) —
never ad-hoc per-element values:

- Type: a system font stack (system-ui, -apple-system, "Segoe UI", Roboto,
  sans-serif); 16px/1.5 body; a small modular scale for headings; cap running
  text at ~65ch for readability.
- Spacing: one rhythm (a 4px or 8px step) reused for every margin, padding, and
  gap — never arbitrary one-off pixel values.
- Color: a small palette — one neutral ramp (background / surface / border /
  text) plus one accent — defined once and referenced by name. Body text on its
  background must meet WCAG AA (contrast >= 4.5:1).
- Shape: one consistent border-radius and a single subtle shadow for raised
  surfaces.

Layout:
- Mobile-first and responsive: fluid widths, a centered max-width container
  (~64rem) with consistent page padding; never fixed pixel widths that overflow
  a phone. Use flexbox/grid, not floats.

Components (style these; do not ship browser defaults):
- Buttons and action links: comfortable padding, a pointer cursor, and BOTH a
  :hover and a visible :focus-visible state.
- Forms: every input has an associated <label for>; a visible focus ring;
  sensible field sizing and spacing.
- Nav, tables, lists, and cards: consistent spacing and alignment from the scale.

Semantics and accessibility:
- Use semantic HTML5 landmarks (<header> <nav> <main> <footer>), heading levels
  in order, alt text on images, and never convey meaning by color alone.

Create the shared stylesheet/theme early (in the scaffold) and extend it as
pages are added, so the site reads as one designed system rather than a set of
separately-styled pages."""


def looks_like_frontend(request: FeatureRequest, design: Design) -> bool:
    """Whether ``request``/``design`` describe a web UI worth styling.

    Deterministic substring scan over the request and design text; matching is
    high-precision (see :data:`_FRONTEND_SIGNALS`) so a backend/CLI/library
    delivery does not trip it.
    """

    haystack = " ".join(
        [
            request.title,
            request.description,
            design.overview,
            design.rationale,
            " ".join(design.tech_stack),
            " ".join(f"{c.name} {c.responsibility}" for c in design.components),
        ]
    ).lower()
    return any(signal in haystack for signal in _FRONTEND_SIGNALS)


def merge_conventions(existing: str | None, block: str) -> str:
    """Append the frontend ``block`` to any existing house conventions.

    Preserves learned conventions (from an assessment) rather than replacing
    them; the baseline lands under its own labelled section so both the
    engineer and reviewer parse it clearly.
    """

    section = f"Frontend design baseline:\n{block}"
    if existing:
        return f"{existing}\n\n{section}"
    return section
