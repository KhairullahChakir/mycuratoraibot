"""Credential-safe environment parsing, independent of configuration loading."""
from sqlalchemy.engine import make_url


def parse_admin_ids(raw):
    result = set()
    for value in raw.split(","):
        value = value.strip()
        if not value:
            continue
        if not value.isascii() or not value.isdecimal() or len(value) > 19:
            raise ValueError("ADMIN_IDS must contain comma-separated positive Telegram IDs.")
        number = int(value)
        if not 0 < number <= 9223372036854775807:
            raise ValueError("ADMIN_IDS contains an invalid Telegram ID.")
        result.add(number)
    return result


def async_database_url(raw):
    if not raw:
        raise ValueError("DATABASE_URL is required in the environment.")
    try:
        url = make_url(raw.strip())
        if url.drivername not in ("postgres", "postgresql", "postgresql+asyncpg"):
            raise ValueError
        if not url.host or not url.database:
            raise ValueError
        query = dict(url.query)
        if "sslmode" in query:
            if "ssl" in query and query["ssl"] != query["sslmode"]:
                raise ValueError
            query["ssl"] = query.pop("sslmode")
        if "ssl" in query and query["ssl"] not in (
            "disable", "allow", "prefer", "require", "verify-ca", "verify-full",
        ):
            raise ValueError
        return url.set(drivername="postgresql+asyncpg", query=query)
    except Exception:
        # Parser exceptions can contain the original URL and its password.
        raise ValueError("DATABASE_URL must be a valid PostgreSQL URL with a supported SSL mode.") from None
