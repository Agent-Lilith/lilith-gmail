import re
from datetime import datetime
from email.utils import parsedate_to_datetime


def parse_email_address(header_value: str) -> tuple[str, str]:
    if not header_value or not header_value.strip():
        return ("", "")
    header_value = header_value.strip()
    match = re.search(r"<([^>]+)>", header_value)
    if match:
        email = match.group(1).strip()
        name = re.sub(r"<[^>]+>", "", header_value).strip().strip("\"'")
        return (email, name or "")
    if "@" in header_value:
        return (header_value.strip(), "")
    return ("", header_value)


def parse_email_list(header_value: str) -> list[str]:
    if not header_value or not header_value.strip():
        return []
    # Split on comma only when not inside quoted string
    result = []
    for part in re.split(
        r",\s*(?=(?:[^\"']*[\"'][^\"']*[\"'])*[^\"']*$)", header_value
    ):
        part = part.strip()
        match = re.search(r"<([^>]+)>", part)
        if match:
            result.append(match.group(1).strip())
        elif "@" in part:
            result.append(part)
    return result


def parse_date(header_value: str) -> datetime | None:
    if not header_value or not header_value.strip():
        return None
    try:
        return parsedate_to_datetime(header_value.strip())
    except (ValueError, TypeError):
        return None
