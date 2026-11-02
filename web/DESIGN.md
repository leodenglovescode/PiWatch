# Interface design

PiWatch uses administration patterns from [Cockpit](https://cockpit-project.org/)
and compact metric panels from [Grafana](https://grafana.com/docs/grafana/latest/visualizations/dashboards/use-dashboards/).
The application shares one header, navigation rail, control style, and set of
status colours across overview, settings, account, and sign-in pages.

Use neutral surfaces, clear dividers, and ordinary form controls. Colour identifies
state and chart series. Keep the overview compact enough for the current seven
targets and six system/network readings at a desktop viewport of 1280×720.
Additional targets may scroll; data must not be clipped to force a screen fit.
Charts contain the latest samples collected while the page is open.

## Forms and navigation

- Follow [W3C's form guidance](https://www.w3.org/WAI/tutorials/forms/) for labels,
  instructions, and related groups of controls.
- Use the [GOV.UK error-summary pattern](https://design-system.service.gov.uk/components/error-summary/):
  a focused summary, links to affected fields, inline messages, and
  `aria-invalid`/`aria-describedby` relationships.
- Error links and settings search must open collapsed service editors before
  focusing their fields. Disabled controls are excluded from search results.
- Keep section navigation, a skip link, visible keyboard focus, and a persistent
  save control. Warn before leaving unsaved settings. Do not hide form sections
  to implement search: their validation and keyboard access must keep working.
- Never place stored credentials in the page. Empty secret inputs preserve their
  values; clearing is an explicit action with visible instructions.
- Respect reduced motion and a saved theme preference. New browsers start in the
  light theme; dark and system themes remain selectable.

Use the isolated preview with synthetic accounts/settings for browser checks.
Check all pages in both themes, mobile overflow, keyboard focus, invalid fields
inside collapsed editors, settings save, and account verification setup/cancel.
Automated axe checks complement these interaction checks.
