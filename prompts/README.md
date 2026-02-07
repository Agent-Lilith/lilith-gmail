# Prompts

Classification prompts are stored here as **Markdown** (`.md`) so they’re easy to edit and version. The model receives the rendered text (markdown is preserved).

## Templating

Prompts use **Python `str.format()`** placeholders: `{name}`. Available variables:

| Placeholder     | Used in   | Description                          |
|-----------------|-----------|--------------------------------------|
| `{sender}`      | user      | Email From header                     |
| `{subject}`     | user      | Email subject                         |
| `{body_preview}`| user      | Body text (possibly truncated)        |
| `{output_labels}`| system   | Default: `SENSITIVE, PERSONAL, or PUBLIC` |

You can add more placeholders in the templates; the code must then pass them when formatting (see `PrivacyManager._template_vars` in `src/transform/privacy.py`).

## Files

- **`classification_system.md`** — System message (rules and output format). Can use `{output_labels}` and any other vars passed at format time.
- **`classification_user.md`** — User message template. Must include `{sender}`, `{subject}`, `{body_preview}`.

Override the prompts directory with the env var **`LILITH_PROMPTS_DIR`** (absolute path). Default is the project root `prompts/` directory.
