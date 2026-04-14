---
name: plex-config-and-connections
description: 'Handle CleanMyPlex configuration, Plex token or username/password auth, settings form changes, config.json.example alignment, and connection guard behavior. Use when changing settings, auth validation, test_token, test_login, or ensure_required_connections.'
argument-hint: 'Describe the config or auth change to make in CleanMyPlex'
user-invocable: true
---

# Plex Config And Connections

## When To Use
- Updating the settings page
- Changing Plex authentication rules
- Fixing config loading or persistence
- Debugging token versus username/password behavior
- Adjusting route guards related to Plex or account access

## Project Facts
- Settings are handled in cleanmyplex.py and templates/settings.html.
- Config values are loaded through load_config() and persisted to config.json.
- Plex server access uses connect_to_plex().
- Plex account access uses connect_to_account().
- Route guards use ensure_required_connections().
- The current auth rule is: a user may configure either PLEX_TOKEN, or PLEX_USERNAME with PLEX_PASSWORD.

## Procedure
1. Inspect cleanmyplex.py helpers first: load_config(), connect_to_plex(), connect_to_account(), refresh_connections(), ensure_required_connections(), and the settings route.
2. Inspect templates/settings.html for required fields, client-side validation, test buttons, and form field names.
3. Keep the validation consistent across browser-side behavior, POST handling, and flash messages.
4. If config keys change, keep config.json.example aligned.
5. Validate changed Python and template files for diagnostics.

## Guardrails
- Do not require both auth modes unless explicitly requested.
- Do not log secrets.
- Do not break routes that only require server access versus routes that require account access.