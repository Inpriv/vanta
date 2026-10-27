# Design system

Vanta's identity is *deep black, one luminous accent, lots of quiet space*.
The same tokens drive the launcher (`crates/vanta-app/ui/theme.slint`) and the
website (`web/assets/tokens.css`). Change both together.

## Logo

A faceted "V": two planes of an inverted gem split along the centre, with a
notch in the top edge. The left plane is lighter, the right one darker, which
reads as a single light source from the top left. It is drawn on a 24 px grid
so it stays sharp at 16 px.

- Mark: `packaging/icons/vanta-mark.svg`
- App icon (mark on a dark rounded tile): `packaging/icons/vanta.svg`, PNGs 16–512 px, `vanta.ico`

## Color

| Token | Dark | Light | Use |
| --- | --- | --- | --- |
| `bg` | `#0b0c0e` | `#f5f5f3` | Window background |
| `surface-1` | `#111215` | `#ffffff` | Sidebar, cards |
| `surface-2` | `#17191d` | `#efefec` | Hover, inputs |
| `surface-3` | `#1e2025` | `#e6e6e2` | Pressed, tracks, secondary buttons |
| `border` | `#ffffff0d` | `#0000000f` | Hairlines |
| `border-strong` | `#ffffff1c` | `#0000001f` | Hovered cards, dialogs |
| `text` | `#ecedf0` | `#141518` | Primary text |
| `text-muted` | `#9a9da6` | `#5c5f67` | Secondary text |
| `text-faint` | `#686b74` | `#8d9097` | Captions, placeholders |
| `danger` | `#ff6b6b` | `#d93636` | Destructive actions, errors |
| `warning` | `#f5b94a` | `#b7790b` | Warnings in logs |

### Accents

One accent at a time. Text on the accent uses `on-accent` (`bg` in dark mode, white in light mode).

| Name | Dark | Light |
| --- | --- | --- |
| Signal (default) | `#74e8bc` | `#16a46e` |
| Iris | `#a898ff` | `#6b55f0` |
| Ember | `#ff9a62` | `#e2622a` |
| Azure | `#72b8ff` | `#2a7bd8` |
| Rose | `#ff85ba` | `#d83f82` |
| Sun | `#f4d265` | `#b58a0b` |

`accent-soft` is the accent at 14 % (12 % in light mode) for active navigation and selections.

## Typography

Inter (SIL OFL 1.1), bundled in Regular, Medium, SemiBold and Bold.

| Token | Size | Typical weight |
| --- | --- | --- |
| `caption` | 12 px | 500–600 |
| `small` | 13 px | 400–500 |
| `body` | 14 px | 400–500 |
| `subtitle` | 16 px | 600 |
| `title` | 20 px | 600 |
| `display` | 28 px | 700, −0.4 px tracking |
| `hero` | 40 px | 700, −0.8 px tracking |

## Spacing, radius, depth

- Spacing follows a 4 px grid: `s1` 4 · `s2` 8 · `s3` 12 · `s4` 16 · `s5` 20 · `s6` 24 · `s8` 32 · `s10` 40 · `s12` 48.
- Radius: `sm` 6 · `md` 10 · `lg` 14 · `xl` 20 px. Pills use half their height.
- Depth comes from surface steps, not borders. Shadows appear only on things
  that float: hovered cards (24 px blur, 8 px offset), popups and dialogs
  (24–48 px blur).

## Motion

| Token | Duration | Use |
| --- | --- | --- |
| `fast` | 120 ms | Hover, press, color changes |
| `base` | 180 ms | Cards lifting, segmented indicator, dialogs |
| `slow` | 260 ms | Progress bars, toggles, console sheet |
| `page` | 220 ms | Page fade and slide |

- `out`: `cubic-bezier(0.22, 1, 0.36, 1)` — the default; fast start, soft landing.
- `spring`: `cubic-bezier(0.34, 1.4, 0.64, 1)` — a small overshoot for things that "settle" (toggle knobs, active markers).

With **Reduce motion** on (or `prefers-reduced-motion` on the website), every
duration becomes 0 and looping animations (spinners, skeleton pulses,
indeterminate bars) stop.

## Components

Every interactive element has hover, pressed, keyboard focus (2 px ring in
`focus-ring`, the accent at 55 %) and disabled (55 % opacity, no pointer)
states, and an accessible role and label.

- **Button**: primary (accent), secondary (surface-3 + hairline), ghost, danger; regular 36 px, large 48 px.
- **IconButton**: 34 px square with a tooltip after hover.
- **TextField**, **Select** (popup list), **Segmented** (sliding indicator), **Toggle**, **Checkbox**, **Slider** (keyboard arrows step).
- **Card**: surface-1, 14 px radius; interactive cards lift 2–3 px on hover.
- **InstanceIcon**: gradient tile (10 palettes) with the instance's initial.
- **ProgressBar** with real numbers next to it (MB done/total, MB/s, files left), **Skeleton** placeholders, **Spinner**.
- **Modal** (fade + 16 px rise), **Console** (bottom sheet), **Toast** (bottom pill, 3.2 s).

## Layout

- 72 px icon rail on the left: Home, Library, Browse; Console and Settings at
  the bottom; account avatar last.
- 44 px custom title bar with native-feeling window controls (close turns red on hover).
- Pages use 40 px side padding and a 4-column grid that reflows by width.
