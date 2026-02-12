from typing import Any


def summarize_emails(
    emails: list[dict[str, Any]],
    max_length: int = 500,
) -> str:
    if not emails:
        return "No emails to summarize."

    parts: list[str] = []
    for i, e in enumerate(emails[:10], 1):
        subject = e.get("subject") or "(no subject)"
        from_ = e.get("from", "unknown")
        date = e.get("date", "")
        snippet = (e.get("snippet") or "")[:200]
        parts.append(f"{i}. [{date}] {from_}: {subject}\n   {snippet}")

    summary = "\n".join(parts)
    if len(summary) > max_length:
        summary = summary[: max_length - 3] + "..."
    return summary
