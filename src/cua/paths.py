from urllib.parse import urlsplit


def path_matches(pattern: str, path: str) -> bool:
    """Route pattern match: ``*`` is one segment, a trailing ``**`` is any remainder (including none)."""
    want = pattern.strip("/").split("/")
    have = urlsplit(path).path.strip("/").split("/")
    for i, segment in enumerate(want):
        if segment == "**":
            return True
        if i >= len(have) or (segment != "*" and segment != have[i]):
            return False
    return len(want) == len(have)


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
