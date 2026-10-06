# Design

Applies to the **public pages only**: flyer, evaluation form, credits page, verification
page, certificate PDF. The Django admin stays unstyled — don't fight its CSS.

Not needed until step 4 of the build order. Recorded now so it isn't improvised later.

## Direction

Institutional and plain. Deep navy, white space, one warm accent used sparingly. The
reader is a clinician checking something quickly between other tasks, not browsing.

Reference feel: a navy masthead, a white body, blue link-coloured titles, a single warm
accent reserved for the one action on the page. No gradients, no shadows, no illustration.

## Tokens

Define these as CSS custom properties on `:root`. Nothing hardcodes a hex value anywhere
else.

```css
:root {
  --navy-900: #0B1C3F;   /* masthead, footer */
  --navy-700: #1F3A6E;   /* headings */
  --link:     #1D4ED8;   /* link and card titles */
  --accent:   #D4491F;   /* one primary action per page. Nothing else. */
  --ink:      #1A1A1A;   /* body text */
  --ink-mute: #5A5F6A;   /* secondary text, timestamps, help */
  --rule:     #D8DCE3;   /* hairline borders */
  --bg:       #FFFFFF;
  --bg-soft:  #F5F7FA;   /* table stripes, callouts */
}
```

Use your own values rather than sampling another provider's site. These are a starting
point, not a match to anything.

## Type

```css
font-family: Lato, "Source Sans 3", "Segoe UI", system-ui, sans-serif;
```

Self-host the webfont rather than calling Google Fonts, so the pages work without an
external request and there's no third-party call to explain in a privacy notice.

- Body 16px / 1.6, max line length ~70 characters
- Headings 600 weight, `--navy-700`, no letterspacing tricks
- Never below 14px. The audience is reading on hospital monitors.

## Rules

- **One accent action per page.** Submit the evaluation. Download the certificate. If a
  page has two orange buttons, one of them is wrong.
- **Left-aligned everything.** No centred body text.
- **No colour-only meaning.** A flagged discrepancy needs a word, not just a red dot.
- **Mobile matters.** People fill the evaluation form on a phone during the session.
  Single column under 640px, tap targets 44px minimum.
- **Contrast at 4.5:1 minimum.** Check `--accent` on white; darken it if it fails.

## Certificate PDF

Restrained, not branded. Navy rule lines and navy text on white. **No orange** — a warm
accent on an accreditation document reads as marketing. The verification code goes in the
footer in a monospace face, large enough to type off a printed page.

Check McGill's visual identity guidelines before finalizing anything on the certificate.
An accredited document may be required to carry specific marks, and that constraint
outranks this file.
