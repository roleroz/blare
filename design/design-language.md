# Blare — Design Language

Blare analyzes a codebase, documents its failure modes, and recommends the metrics and alerts needed to catch them. **The alert arriving is the point — not the failure.** Every design decision should feel like a system that is watching and will tell you in time.

Tone: precise and engineered, with a little wit. Never alarmist, never cute.

---

## 1. Logo

The mark is a rising bar chart crossing a dashed threshold line, with the tallest bar breaking through and firing a ping.

Assets live in `brand/`:

| File | Use |
|---|---|
| `blare-mark.svg` | Mark on light backgrounds |
| `blare-mark-dark.svg` | Mark on dark backgrounds (dimmed baseline bars) |
| `blare-mark-mono.svg` | Single-color, uses `currentColor` — CLI, stencil, embossing |
| `blare-lockup-light.svg` | Mark + wordmark, dark text |
| `blare-lockup-dark.svg` | Mark + wordmark, light text |
| `blare-favicon-32.svg` | 3-bar simplification, rounded dark tile |
| `blare-favicon-16.svg` | 3-bar, no threshold line, oversized ping |

### Rules

- **Never redraw the mark.** Bar geometry, the 52/120 threshold line, and the ping position are fixed.
- **Drop detail as it shrinks.** ≥32px: 5 bars + threshold. 24–32px: 3 bars + threshold. ≤16px: 3 bars, no threshold line, enlarged ping.
- **Clear space** = the width of one bar (13/120 of the mark height) on all sides.
- **Lockup gap** = 26/120 of the mark height between mark and wordmark, baselines optically aligned.
- The wordmark in shipped SVGs must be **outlined**, not live text.
- The gradient always runs bottom→top (`#FF5A1F` → `#FFB020`). Never rotate, never recolor it.
- Never put the mark on a busy photo, tilt it, add a drop shadow, or outline it.

---

## 2. Color

| Token | Hex | Role |
|---|---|---|
| `--blare-alert` | `#FF5A1F` | Firing alerts, breach state, primary CTA, the ping |
| `--blare-alert-hi` | `#FFB020` | Gradient top; warning / degraded state |
| `--blare-baseline` | `#7A8699` | Healthy metrics, secondary text, chart baselines |
| `--blare-baseline-dim` | `#5B6675` | Receding bars on dark, disabled state |
| `--blare-ink` | `#0E1116` | Dark surface, primary text on light |
| `--blare-ink-soft` | `#1A1E26` | Wordmark, headings on light |
| `--blare-paper` | `#FAFBFC` | Light surface |
| `--blare-mist` | `#F4F6F8` | Text on dark, subtle light fills |
| `--blare-line` | `#E3E7EC` | Borders and dividers on light |
| `--blare-line-dark` | `#242A33` | Borders and dividers on dark |

**Semantic mapping** — Blare is a status product, so color is functional first:

- **Firing / breach** → `#FF5A1F`
- **Warning / degraded** → `#FFB020`
- **Healthy / nominal** → `#7A8699` (deliberately *not* green — silence is grey, quiet, unremarkable)
- **Unknown / no data** → `#5B6675` at 60% opacity

Discipline: **orange is a signal, not a decoration.** If more than ~10% of a screen is orange, the signal has stopped meaning anything. Never use it for headers, nav chrome, or section backgrounds.

Dark surface is the product's home (dashboards, CLI, log views). Light surface is for docs, marketing, and settings.

---

## 3. Typography

| Role | Face | Weight | Notes |
|---|---|---|---|
| Wordmark & headings | **Space Grotesk** | 700 | Tracking `-0.035em` at display sizes |
| UI / body | **Space Grotesk** | 400/500 | Tracking `-0.01em`, line-height 1.55 |
| Code, metrics, IDs, CLI | **JetBrains Mono** | 400/500/700 | Tracking `0` in code, `0.18em` uppercase for eyebrows |

Scale (px): 11 · 12 · 13 · 15 · 19 · 24 · 34 · 48 · 58

- Anything a machine produced — metric names, thresholds, durations, commit SHAs, service names, file paths — is set in **JetBrains Mono**, even inline in prose.
- Small-caps eyebrows: JetBrains Mono 11px, `letter-spacing: 0.18em`, uppercase, `--blare-baseline`.
- Never use Inter, Roboto, or system-ui as a brand face.

---

## 4. Layout & surfaces

- **Square corners by default.** Radius `0` for panels, tables, and inputs; `3px` for small chips and badges; `26/120` only on the app-icon tile.
- **1px hairline borders** define structure — not shadows. Shadows only for genuinely floating layers (menus, toasts, modals).
- Spacing scale: 4 · 8 · 14 · 18 · 22 · 28 · 40 · 56.
- Layouts are **dense and tabular**. This is an operator tool; whitespace is for reading, not for grandeur.
- Use flex/grid with `gap`, never margin chains.

---

## 5. Data & status display

- Bar charts inherit the logo: rounded caps (`rx` = half width), baseline bars in `--blare-baseline`, breaching bars in the alert gradient.
- **Thresholds are always dashed** (`stroke-dasharray: 5 6`) — dashed means "the line you must not cross." Never dash anything else.
- The **ping dot** (filled circle + faint concentric ring) means "an alert fired here." Reserve it for exactly that; don't use it as a generic bullet.
- Counts of problems are stated plainly: `2 gaps`, `5 unalerted`, `14 failure modes · 9 covered`. No progress rings, no gauges, no letter grades.
- Numbers precede nouns; the noun stays lowercase: `3 alerts`, not `Alerts (3)`.

---

## 6. Voice & copy

- Lowercase product name in running text: **blare**. Sentence case for UI labels.
- Terse and factual. `5 failure modes have no alert` — not `Uh oh! We found some issues.`
- Verbs are operator verbs: analyze, detect, document, recommend, apply, watch, diff.
- Never apologize on the system's behalf; state what is true and what the user can do.
- CLI output uses `→` for results, `$` for the prompt, and color only for the severity word.
- Vocabulary to keep consistent: **failure mode**, **coverage** (not "health"), **gap** (a failure mode with no alert), **breach**, **diff**, **recommendation**.

---

## 7. Motion

Restrained. The only signature motion is the **ping**: the ring scales `0.6 → 1.6` while fading to 0 over 900ms, used once when an alert transitions to firing. Everything else is a 120–180ms ease for state changes. Nothing loops, nothing pulses idly — a UI that is always animating cannot signal urgency.

---

## 8. Quick reference for implementation

```css
:root {
  --blare-alert: #FF5A1F;
  --blare-alert-hi: #FFB020;
  --blare-baseline: #7A8699;
  --blare-baseline-dim: #5B6675;
  --blare-ink: #0E1116;
  --blare-ink-soft: #1A1E26;
  --blare-paper: #FAFBFC;
  --blare-mist: #F4F6F8;
  --blare-line: #E3E7EC;
  --blare-line-dark: #242A33;
  --blare-gradient: linear-gradient(0deg, #FF5A1F, #FFB020);
  --blare-font-display: 'Space Grotesk', system-ui, sans-serif;
  --blare-font-mono: 'JetBrains Mono', ui-monospace, monospace;
}
```

**Checklist before shipping any Blare surface**

1. Is orange used *only* for something that is actually wrong or actionable?
2. Are all machine-generated values in mono?
3. Are corners square and borders hairline?
4. Does the smallest instance of the mark use the right level of detail?
5. Does the copy state a fact and an action, in that order?
