---
name: Molecular Discovery Platform
colors:
  surface: '#faf8ff'
  surface-dim: '#d2d9f4'
  surface-bright: '#faf8ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f2f3ff'
  surface-container: '#eaedff'
  surface-container-high: '#e2e7ff'
  surface-container-highest: '#dae2fd'
  on-surface: '#131b2e'
  on-surface-variant: '#3d4947'
  inverse-surface: '#283044'
  inverse-on-surface: '#eef0ff'
  outline: '#6d7a77'
  outline-variant: '#bcc9c6'
  surface-tint: '#006a61'
  primary: '#00685f'
  on-primary: '#ffffff'
  primary-container: '#008378'
  on-primary-container: '#f4fffc'
  inverse-primary: '#6bd8cb'
  secondary: '#006c4a'
  on-secondary: '#ffffff'
  secondary-container: '#82f5c1'
  on-secondary-container: '#00714e'
  tertiary: '#006860'
  on-tertiary: '#ffffff'
  tertiary-container: '#248279'
  on-tertiary-container: '#f3fffc'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#89f5e7'
  primary-fixed-dim: '#6bd8cb'
  on-primary-fixed: '#00201d'
  on-primary-fixed-variant: '#005049'
  secondary-fixed: '#85f8c4'
  secondary-fixed-dim: '#68dba9'
  on-secondary-fixed: '#002114'
  on-secondary-fixed-variant: '#005137'
  tertiary-fixed: '#9cf2e8'
  tertiary-fixed-dim: '#80d5cb'
  on-tertiary-fixed: '#00201d'
  on-tertiary-fixed-variant: '#00504a'
  background: '#faf8ff'
  on-background: '#131b2e'
  surface-variant: '#dae2fd'
typography:
  display:
    fontFamily: Space Grotesk
    fontSize: 32px
    fontWeight: '600'
    lineHeight: 40px
    letterSpacing: -0.02em
  headline-lg:
    fontFamily: Space Grotesk
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
    letterSpacing: -0.015em
  headline-md:
    fontFamily: Space Grotesk
    fontSize: 18px
    fontWeight: '600'
    lineHeight: 24px
    letterSpacing: -0.01em
  headline-sm:
    fontFamily: Space Grotesk
    fontSize: 15px
    fontWeight: '600'
    lineHeight: 20px
    letterSpacing: -0.005em
  body-lg:
    fontFamily: Geist
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  body-default:
    fontFamily: Geist
    fontSize: 13px
    fontWeight: '400'
    lineHeight: 18px
  body-sm:
    fontFamily: Geist
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 16px
  mono-data:
    fontFamily: JetBrains Mono
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
    letterSpacing: -0.01em
  mono-sm:
    fontFamily: JetBrains Mono
    fontSize: 11px
    fontWeight: '400'
    lineHeight: 14px
  label-caps:
    fontFamily: JetBrains Mono
    fontSize: 10px
    fontWeight: '600'
    lineHeight: 12px
    letterSpacing: 0.06em
rounded:
  sm: 0.125rem
  DEFAULT: 0.25rem
  md: 0.375rem
  lg: 0.5rem
  xl: 0.75rem
  full: 9999px
spacing:
  gutter: 0.75rem
  gutter-dense: 0.375rem
  margin: 1rem
  space-2xs: 0.125rem
  space-xs: 0.25rem
  space-sm: 0.5rem
  space-md: 0.75rem
  space-lg: 1rem
  space-xl: 1.5rem
---

## Brand & Style

The design system establishes a high-precision digital instrument tailored for computational chemists, structural biologists, and pharmacology researchers. Its visual philosophy centers on **Scientific Minimalism**: disciplined, utilitarian, dense with semantic value, yet visually calm during prolonged multi-hour research workflows.

Every visual decision favors data legibility, low cognitive friction, and structural fidelity over ornamentation:
- **Tone:** Authoritative, clinical, meticulously ordered, and technologically sophisticated.
- **Interface Posture:** Digital laboratory bench rather than consumer SaaS. The UI remains neutral to let high-dimensional biomolecular data, complex 3D macromolecular structures, and kinetic plots hold primary focus.
- **Graphic Restraint:** No whimsical iconography, gradient badges, or decorative card wrappers. Visual affordances depend strictly on structural grid divisions, precise 1px geometric borders, and measured color accents that denote state transitions, binding affinity bands, and compute progress.

## Colors

The palette is engineered for prolonged analytical observation under clinical laboratory lighting conditions. It uses high-contrast typography resting upon muted, glare-reducing off-white and pale slate strata.

### Background and Surface Tokens
- **Canvas Base (`#F8F9FA`):** Ambient work surface framing multi-panel layouts.
- **Surface Elevation 0 (`#FFFFFF`):** High-density functional data tables, sequence view blocks, and viewport viewports.
- **Surface Muted (`#F1F5F9`):** Inactive tabs, column headers, tool trays, and docked diagnostic panels.
- **Surface Hover (`#E2E8F0`):** Subtle hit feedback on interactive rows and structural nodes.

### Boundary and Structural Tokens
- **Border Subtle (`#E2E8F0`):** 1px structural division lines across grid splitters, docked tool panels, and tabular data.
- **Border Strong (`#CBD5E1`):** Form field inputs, active view toggles, and modal interfaces.

### Typographic Hierarchy
- **Text Ink Primary (`#0F172A`):** Critical data points, chemical nomenclature, primary values, and primary headings.
- **Text Ink Secondary (`#334155`):** Table values, parameter names, and property descriptors.
- **Text Ink Muted (`#64748B`):** Units, disabled attributes, timestamps, and secondary structural metadata.

### Computational Accent System
- **Teal Primary (`#0D9488`):** Active target selection, active chemical sub-structure highlights, primary batch triggers, and primary progress bars.
- **Teal Focus/Border (`#14B8A6`):** Viewport crosshairs, focused input rings, and active canvas edge-highlights.
- **Emerald Functional (`#059669`):** Favorable binding affinities ($pKd \ge 8$), validated assay matches, and converged simulations.
- **Deep Marine (`#0F766E`):** Selected table rows, segmented control pills, and active filter flags.
- **Alert Rose (`#BE123C`):** Steric clashes, unfavorable torsion strain, and pipeline run errors.
- **Amber Warning (`#B45309`):** Low confidence scores (pLDDT < 70), boundary constraint violations, and chemical liabilities.

## Typography

The typographic strategy balances administrative hierarchy, readable long-form research text, and unambiguous machine-readable technical notation.

- **Display & Section Headers (`Space Grotesk`):** Delivers a technical, engineered presence. Used selectively on workspace panel titles, dock headers, modal headings, and compound ID banners.
- **Interface & Analytical Copy (`Geist`):** Delivers neutral, clear legibility at 12–14px sizes in high-density configuration panes, parameter forms, and structural annotation sidebars.
- **Analytical & Machine Data (`JetBrains Mono`):** Applied strictly to canonical biomedical formats:
  - Protein Data Bank codes (`PDB: 7L10`)
  - Canonical & Isomeric SMILES strings (`CC(=O)Oc1ccccc1C(=O)O`)
  - Thermodynamic & kinetic metrics ($pK_d$, $IC_{50}$, $k_{off}$, $\Delta G$)
  - In silico compute telemetry (GPU time, token counts, convergence delta)
  - Coordinate axes and sequence alignment positions
- Text alignment within data tables must consistently right-align `JetBrains Mono` numerical metrics to maintain strict vertical decimal scanning.

## Layout & Spacing

Workspaces run edge-to-edge on desktop displays (1440px to 4K multi-monitor workstations), optimizing spatial economy for multi-panel scientific coordination:

### Workspace Shell Architecture
- **Header Dock (48px Fixed Height):** Global context, project repository, compute cluster state indicator, and user access.
- **Tiling Pane Matrix:** A grid with dynamic horizontal and vertical splitters using `gutter` (12px) separation. 
  - *Left Pane:* Target/Ligand inventory tree and query filters.
  - *Center Viewport:* WebGL 3D molecular viewer and surface potential renderer.
  - *Right Pane:* Property inspection, torsion distributions, and kinetic readouts.
  - *Bottom Drawer:* Docked virtual screening results and parallel workflow logs.
- **Breakpoints:**
  - `Desktop Extended` ($\ge$ 1680px): 3-column split view (280px left rail, flexible 3D stage, 380px diagnostic inspector).
  - `Desktop Standard` (1280px – 1679px): Collapsible property inspector into an absolute overlay or tabbed panel.
  - `Tablet / Field Audit` (< 1280px): Sequential multi-step view; tabs replace side-by-side dock panes.

## Elevation & Depth

Visual hierarchy uses **Tonal Layering and Razor Borders** rather than standard diffuse drop shadows. This preserves visual clarity during detailed spatial examination:

- **Structural Borders:** All panel boundaries, section dividers, and table splits use exact 1px solid borders (`#E2E8F0`).
- **Surface Stacking:** Depth is achieved by placing lighter working tiles (`#FFFFFF`) on top of the neutral staging canvas (`#F8F9FA`).
- **Interactive Affordance:**
  - *Resting Element:* Flat plane with 1px border. No drop shadow.
  - *Hovered Interactive Element:* `#F1F5F9` background with `#CBD5E1` border stroke.
  - *Floating Inspectors & Context Menus:* 1px solid border (`#CBD5E1`) combined with an ultra-compact, cold-tinted ambient edge: `0 4px 12px -2px rgba(15, 23, 42, 0.08)`.
  - *Active 3D Viewport Focus:* Active border turns to `#0D9488` with no glow or blur, signaling absolute keyboard input focus.

## Shapes

The interface uses a **Soft Architectural (`1`)** shape model:
- **Base Geometry:** Canonical UI controls (buttons, inputs, status badges, and tabs) use `0.25rem` (4px) corner radii. This maintains geometric alignment with tabular data columns and 1px grid lines.
- **Panels and Viewport Containers:** Strictly `0.25rem` (4px) external corners, with inner content boxes using `0.125rem` (2px) to prevent corner nested-distortion.
- **Pill Exceptions:** Rounded pill profiles are strictly forbidden, even for status pills or chips. Everything preserves an intentional, machined edge profile.

## Components

### Buttons
- **Primary:** Deep Teal background (`#0D9488`), White text (`#FFFFFF`), 4px border radius. Hover: `#0F766E`. Active: `#115E59`.
- **Secondary / Neutral:** Pure white background (`#FFFFFF`), Slate border (`#CBD5E1`), Navy text (`#0F172A`). Hover: `#F8F9FA` with border `#94A3B8`.
- **Destructive:** White background, Rose border (`#FDA4AF`), Rose text (`#BE123C`). Hover: `#FFF1F2`.
- **Size Formats:** Ultra-compact 28px height (tool bars) and 32px standard height (forms). Padding: 8px horizontal (`space-sm`) for compact, 12px horizontal (`space-md`) for standard.

### Data Inputs & Steppers
- **Text & Numeric Inputs:** Flat `#FFFFFF` fill, 1px `#CBD5E1` border. Active focus: 1px `#0D9488` border with a crisp 1px ring (`#0D9488`).
- **Monospace Textarea (SMILES/PDB):** JetBrains Mono font (`12px`), tab-spacing locked, syntax highlighted via emerald/slate inline spans.

### Data Tables (Workstation Grid)
- **Header:** Height 28px, background `#F1F5F9`, border-bottom 1px `#CBD5E1`. Typography: `label-caps` in `#475569`.
- **Row:** Height 32px (dense view) or 40px (standard view). Alternating row fills are avoided; separation uses 1px `#F1F5F9` bottom borders.
- **Cell Hover / Selected:** Selected rows feature a 2px left border strip of `#0D9488` and a background tint of `#F0FDFA`.

### Scientific Metrics & Badges
- **Affinity Indicator:** Rectangular chip (radius 2px), padding `1px 6px`. Font: `JetBrains Mono` 11px. 
  - *High Affinity ($pK_d \ge 8$):* `#ECFDF5` background, `#047857` text, 1px `#A7F3D0` border.
  - *Low Affinity ($pK_d < 6$):* `#FFFBEB` background, `#B45309` text, 1px `#FDE68A` border.
- **Structure Tags (PDB IDs):** Monospaced pill-less tag, `#F1F5F9` background, `#1E293B` text, 1px `#E2E8F0` border.

### Visual Canvas / Docked Panels
- **Inspectors & Tool Shelves:** Encased in 1px `#E2E8F0` solid outlines. Headers are separated by a 1px border with zero vertical gap, forming an integrated single-frame workstation layout.