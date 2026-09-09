#!/usr/bin/env python3
"""Refresh the site's cached publication data from structured APIs.

The updater deliberately uses only the Python standard library.  The live site
must read ``data/publications.json`` and never call an external API at build or
request time.  A refresh is assembled and validated in memory, then installed
with ``os.replace``; a failed API request or validation therefore leaves the
last known-good cache untouched.

OpenAlex discovery searches exact, work-level raw bylines with this expression::

    raw_author_name.search:"Paul Caillon" OR "Caillon Paul"

Results are gated a second time against those aliases locally.  This avoids
depending on OpenAlex author-profile disambiguation and rejects works where the
name tokens belong to different bylines.

Optional overrides live in ``data/publication-overrides.json``::

    {
      "exclude": [
        "doi:10.1234/example",
        "title:a normalized title"
      ],
      "overrides": {
        "doi:10.1234/example": {
          "venue": "Correct venue",
          "published": true,
          "selected": true,
          "code": "https://github.com/example/project"
        },
        "title:a normalized title": {
          "pdf": "https://example.org/paper.pdf"
        }
      },
      "additions": [
        {
          "title": "A manually indexed work",
          "authors": ["Paul Caillon"],
          "year": 2026,
          "venue": "Venue",
          "doi": "10.1234/manual"
        }
      ]
    }

``exclusions`` is accepted as an alias for ``exclude``.  Match keys are always
``doi:<normalized-doi>`` or ``title:<normalized-title>``; DOI URLs and title
punctuation/case are normalized automatically.  An override may set an
optional field to ``null`` to remove it.  Supported publication fields are:
``title``, ``authors``, ``year``, ISO ``date``, ``venue``, ``venue_url``,
``doi``, ``arxiv``, ``pdf``, ``code``, ``project``, ``description``, and
``published``, and ``selected``.  The homepage only renders records explicitly
marked ``published: true``; API-discovered records without that flag remain in
the cache but do not appear as publications until they are reviewed.

Refreshes deduplicate by normalized DOI first and normalized title second.
When an arXiv record and a published record share a title, the merged entry
keeps the arXiv/PDF links while preferring the non-arXiv DOI and richer venue.
Crossref enrichment is opt-in with ``--crossref`` and only fills metadata that
OpenAlex lacks.  If enabled, a Crossref failure is fatal for that refresh.

Use ``--check`` in CI to validate the cached JSON entirely offline.
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_PATH = REPOSITORY_ROOT / "data" / "publications.json"
DEFAULT_OVERRIDES_PATH = REPOSITORY_ROOT / "data" / "publication-overrides.json"

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
CROSSREF_WORK_URL = "https://api.crossref.org/works/{doi}"
AUTHOR_ALIASES = ("Paul Caillon", "Caillon Paul")
OPENALEX_AUTHOR_QUERY = " OR ".join(f'"{name}"' for name in AUTHOR_ALIASES)
OPENALEX_FILTER = f"raw_author_name.search:{OPENALEX_AUTHOR_QUERY},type:!paratext"

USER_AGENT = "pcaillon-publication-updater/1.0"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_OPENALEX_PAGES = 100

FIELD_ORDER = (
    "title",
    "authors",
    "year",
    "date",
    "venue",
    "venue_url",
    "doi",
    "arxiv",
    "pdf",
    "code",
    "project",
    "description",
    "published",
    "selected",
)
ALLOWED_FIELDS = frozenset(FIELD_ORDER)
LINK_FIELDS = ("venue_url", "pdf", "code", "project")

DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
DOI_PREFIX_RE = re.compile(
    r"^(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE
)
ARXIV_URL_RE = re.compile(
    r"https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/([^?#]+)", re.IGNORECASE
)
WHITESPACE_RE = re.compile(r"\s+")


class PublicationError(RuntimeError):
    """Base class for expected updater failures."""


class FetchError(PublicationError):
    """An external API could not provide a valid response."""


class ValidationError(PublicationError):
    """Publication or override data failed schema validation."""


def log(message: str, *, verbose: bool = True) -> None:
    """Print a progress message when requested."""

    if verbose:
        print(message)


def clean_text(value: Any) -> str:
    """Return a trimmed, whitespace-normalized string or an empty string."""

    if not isinstance(value, str):
        return ""
    return WHITESPACE_RE.sub(" ", html.unescape(value)).strip()


def normalize_title(value: Any) -> str:
    """Normalize a title for identity matching, not for display."""

    text = unicodedata.normalize("NFKD", clean_text(value)).casefold()
    characters: list[str] = []
    for character in text:
        if unicodedata.combining(character):
            continue
        characters.append(character if character.isalnum() else " ")
    return WHITESPACE_RE.sub(" ", "".join(characters)).strip()


def normalize_person_name(value: Any) -> str:
    """Normalize a raw byline while retaining token order."""

    return normalize_title(value)


AUTHOR_ALIAS_KEYS = frozenset(normalize_person_name(name) for name in AUTHOR_ALIASES)


def normalize_doi(value: Any) -> str:
    """Return a lowercase bare DOI, accepting common DOI URL forms."""

    text = clean_text(value)
    if not text:
        return ""
    text = urllib.parse.unquote(text).strip().strip("<>")
    previous = None
    while previous != text:
        previous = text
        text = DOI_PREFIX_RE.sub("", text).strip()
    return text.casefold()


def canonical_doi_url(value: Any) -> str:
    """Return a canonical HTTPS DOI URL or an empty string."""

    doi = normalize_doi(value)
    if not doi:
        return ""
    if not DOI_RE.fullmatch(doi):
        raise ValidationError(f"invalid DOI: {value!r}")
    return f"https://doi.org/{doi}"


def is_arxiv_doi(value: Any) -> bool:
    return normalize_doi(value).startswith("10.48550/arxiv.")


def normalize_arxiv_id(value: Any) -> str:
    """Return a bare arXiv identifier from an identifier or arXiv URL."""

    text = clean_text(value)
    if not text:
        return ""
    match = ARXIV_URL_RE.search(text)
    if match:
        text = match.group(1)
    text = text.removeprefix("arXiv:").removeprefix("arxiv:")
    text = text.strip().strip("/")
    if text.casefold().endswith(".pdf"):
        text = text[:-4]
    text = re.sub(r"v\d+$", "", text, flags=re.IGNORECASE)
    if not text or any(character.isspace() for character in text):
        raise ValidationError(f"invalid arXiv identifier: {value!r}")
    return text


def canonical_arxiv_url(value: Any) -> str:
    arxiv_id = normalize_arxiv_id(value)
    return f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""


def arxiv_id_from_doi(value: Any) -> str:
    doi = normalize_doi(value)
    prefix = "10.48550/arxiv."
    return doi[len(prefix) :] if doi.startswith(prefix) else ""


def arxiv_id_from_url(value: Any) -> str:
    text = clean_text(value)
    match = ARXIV_URL_RE.search(text)
    return normalize_arxiv_id(match.group(1)) if match else ""


def is_arxiv_venue(value: Any) -> bool:
    venue = normalize_title(value)
    return venue in {"arxiv", "arxiv preprint"} or venue.startswith("arxiv ")


def ensure_url(value: Any, *, field: str, allow_relative: bool = True) -> str:
    """Validate a publication link and return its trimmed representation."""

    text = clean_text(value)
    if not text:
        return ""
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return text
    if allow_relative and text.startswith("/") and not text.startswith("//"):
        return text
    raise ValidationError(f"{field} must be an HTTP(S) or site-relative URL: {value!r}")


def parse_year(value: Any) -> int:
    if isinstance(value, bool):
        raise ValidationError("year must be an integer")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or not 1000 <= value <= 3000:
        raise ValidationError(f"invalid publication year: {value!r}")
    return value


def normalize_date(value: Any) -> str:
    """Validate an optional ISO calendar date."""

    text = clean_text(value)
    if not text:
        return ""
    try:
        parsed = datetime.date.fromisoformat(text)
    except ValueError as error:
        raise ValidationError(f"invalid ISO publication date: {value!r}") from error
    return parsed.isoformat()


def normalize_authors(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError("authors must be a JSON array of names")
    authors = [clean_text(author) for author in value]
    if not authors or any(not author for author in authors):
        raise ValidationError("authors must contain at least one non-empty name")
    return authors


def normalize_publication(raw: Mapping[str, Any], *, context: str) -> dict[str, Any]:
    """Validate and canonicalize one site publication record."""

    if not isinstance(raw, Mapping):
        raise ValidationError(f"{context} must be a JSON object")
    unknown = set(raw) - ALLOWED_FIELDS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValidationError(f"{context} has unsupported field(s): {names}")

    title = clean_text(raw.get("title"))
    if not title:
        raise ValidationError(f"{context}.title must be a non-empty string")

    publication_date = normalize_date(raw.get("date"))
    year_value = raw.get("year")
    if year_value is None and publication_date:
        year_value = int(publication_date[:4])
    year = parse_year(year_value)
    if publication_date and int(publication_date[:4]) != year:
        raise ValidationError(f"{context}.date and {context}.year disagree")

    publication: dict[str, Any] = {
        "title": title,
        "authors": normalize_authors(raw.get("authors")),
        "year": year,
    }

    if publication_date:
        publication["date"] = publication_date

    venue = clean_text(raw.get("venue"))
    if venue:
        publication["venue"] = venue

    doi = canonical_doi_url(raw.get("doi"))
    arxiv_doi = arxiv_id_from_doi(doi)
    if doi and not arxiv_doi:
        publication["doi"] = doi

    arxiv_value = raw.get("arxiv")
    if not arxiv_value and arxiv_doi:
        arxiv_value = arxiv_doi
    if not arxiv_value:
        arxiv_value = arxiv_id_from_url(raw.get("pdf"))
    arxiv = canonical_arxiv_url(arxiv_value)
    if arxiv:
        publication["arxiv"] = arxiv

    for field in LINK_FIELDS:
        link = ensure_url(raw.get(field), field=f"{context}.{field}")
        if link:
            publication[field] = link

    description = clean_text(raw.get("description"))
    if description:
        publication["description"] = description

    for field in ("published", "selected"):
        if field in raw and raw[field] is not None:
            if not isinstance(raw[field], bool):
                raise ValidationError(f"{context}.{field} must be true or false")
            publication[field] = raw[field]

    return publication


def ordered_publication(publication: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stable field order for human-readable diffs."""

    return {field: publication[field] for field in FIELD_ORDER if field in publication}


def publication_identity_keys(publication: Mapping[str, Any]) -> set[str]:
    keys = {f"title:{normalize_title(publication.get('title'))}"}
    doi = normalize_doi(publication.get("doi"))
    if doi:
        keys.add(f"doi:{doi}")
    return keys


def canonical_match_key(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or ":" not in value:
        raise ValidationError(
            f"{context} must use 'doi:<value>' or 'title:<value>'"
        )
    kind, raw_value = value.split(":", 1)
    kind = kind.strip().casefold()
    if kind == "doi":
        normalized = normalize_doi(raw_value)
        if not normalized or not DOI_RE.fullmatch(normalized):
            raise ValidationError(f"{context} contains an invalid DOI key")
    elif kind == "title":
        normalized = normalize_title(raw_value)
        if not normalized:
            raise ValidationError(f"{context} contains an empty title key")
    else:
        raise ValidationError(f"{context} has unsupported key prefix {kind!r}")
    return f"{kind}:{normalized}"


def reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def load_json(path: Path, *, required: bool) -> Any:
    if not path.exists():
        if required:
            raise ValidationError(f"missing JSON file: {path}")
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=reject_duplicate_json_keys)
    except OSError as error:
        raise ValidationError(f"could not read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ValidationError(
            f"invalid JSON in {path} at line {error.lineno}, column {error.colno}: "
            f"{error.msg}"
        ) from error


def validate_override_patch(raw: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError(f"{context} must be a JSON object")
    unknown = set(raw) - ALLOWED_FIELDS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValidationError(f"{context} has unsupported field(s): {names}")
    if any(field in raw and raw[field] is None for field in ("title", "authors", "year")):
        raise ValidationError(f"{context} cannot remove title, authors, or year")
    return dict(raw)


def load_overrides(path: Path) -> dict[str, Any]:
    """Load and normalize the manual override document."""

    raw = load_json(path, required=False)
    if raw is None:
        return {"exclude": set(), "overrides": {}, "additions": []}
    if not isinstance(raw, dict):
        raise ValidationError(f"{path} must contain a JSON object")
    unknown = set(raw) - {"exclude", "exclusions", "overrides", "additions"}
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValidationError(f"{path} has unsupported top-level field(s): {names}")

    excluded_values: list[Any] = []
    for field in ("exclude", "exclusions"):
        values = raw.get(field, [])
        if not isinstance(values, list):
            raise ValidationError(f"{path}:{field} must be a JSON array")
        excluded_values.extend(values)
    exclusions = {
        canonical_match_key(value, context=f"{path}:exclusion")
        for value in excluded_values
    }

    raw_overrides = raw.get("overrides", {})
    if not isinstance(raw_overrides, dict):
        raise ValidationError(f"{path}:overrides must be a JSON object")
    overrides: dict[str, dict[str, Any]] = {}
    for raw_key, patch in raw_overrides.items():
        key = canonical_match_key(raw_key, context=f"{path}:override key")
        if key in overrides:
            raise ValidationError(
                f"{path}: multiple override keys normalize to {key!r}"
            )
        overrides[key] = validate_override_patch(
            patch, context=f"{path}:overrides[{raw_key!r}]"
        )

    raw_additions = raw.get("additions", [])
    if not isinstance(raw_additions, list):
        raise ValidationError(f"{path}:additions must be a JSON array")
    additions = [
        normalize_publication(item, context=f"{path}:additions[{index}]")
        for index, item in enumerate(raw_additions)
    ]

    return {
        "exclude": exclusions,
        "overrides": overrides,
        "additions": additions,
    }


def retry_delay(error: Exception, attempt: int) -> float:
    if isinstance(error, urllib.error.HTTPError):
        retry_after = error.headers.get("Retry-After") if error.headers else None
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), 30.0)
            except ValueError:
                pass
    return min(2.0**attempt, 8.0)


def fetch_json(
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    retries: int,
) -> Any:
    """Fetch one bounded JSON response, retrying transient failures."""

    request = urllib.request.Request(url, headers=dict(headers))
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise FetchError(f"API response exceeded {MAX_RESPONSE_BYTES} bytes")
                charset = response.headers.get_content_charset() or "utf-8"
            try:
                return json.loads(payload.decode(charset))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise FetchError(f"API returned invalid JSON from {url}: {error}") from error
        except FetchError:
            raise
        except urllib.error.HTTPError as error:
            last_error = error
            retryable = error.code == 429 or 500 <= error.code < 600
            if not retryable or attempt >= retries:
                detail = ""
                try:
                    detail = clean_text(error.read(512).decode("utf-8", "replace"))
                except OSError:
                    pass
                suffix = f": {detail}" if detail else ""
                raise FetchError(f"HTTP {error.code} fetching {url}{suffix}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt >= retries:
                raise FetchError(f"could not fetch {url}: {error}") from error
        time.sleep(retry_delay(last_error, attempt))
    raise FetchError(f"could not fetch {url}: {last_error}")


def api_headers(*, email: str, api_key: str = "") -> dict[str, str]:
    agent = USER_AGENT
    if email:
        agent += f" (mailto:{email})"
    headers = {"Accept": "application/json", "User-Agent": agent}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def openalex_url(cursor: str) -> str:
    parameters = {
        "filter": OPENALEX_FILTER,
        "corpus": "all",
        "per_page": "100",
        "cursor": cursor,
    }
    return f"{OPENALEX_WORKS_URL}?{urllib.parse.urlencode(parameters)}"


def has_exact_target_byline(work: Mapping[str, Any]) -> bool:
    authorships = work.get("authorships")
    if not isinstance(authorships, list):
        return False
    return any(
        isinstance(authorship, dict)
        and normalize_person_name(authorship.get("raw_author_name")) in AUTHOR_ALIAS_KEYS
        for authorship in authorships
    )


def openalex_authors(work: Mapping[str, Any]) -> list[str]:
    authorships = work.get("authorships")
    if not isinstance(authorships, list):
        return []
    authors: list[str] = []
    for authorship in authorships:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author")
        name = ""
        if isinstance(author, dict):
            name = clean_text(author.get("display_name"))
        if not name:
            name = clean_text(authorship.get("raw_author_name"))
        if name:
            authors.append(name)
    return authors


def location_source_name(location: Any) -> str:
    if not isinstance(location, dict):
        return ""
    source = location.get("source")
    return clean_text(source.get("display_name")) if isinstance(source, dict) else ""


def openalex_locations(work: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    candidates: list[Any] = [work.get("primary_location"), work.get("best_oa_location")]
    locations = work.get("locations")
    if isinstance(locations, list):
        candidates.extend(locations)
    for location in candidates:
        if not isinstance(location, dict):
            continue
        marker = json.dumps(location, ensure_ascii=False, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            result.append(location)
    return result


def publication_from_openalex(work: Mapping[str, Any], *, context: str) -> dict[str, Any]:
    title = clean_text(work.get("display_name") or work.get("title"))
    authors = openalex_authors(work)
    year = work.get("publication_year")
    locations = openalex_locations(work)

    primary = work.get("primary_location")
    venue = location_source_name(primary)
    if not venue and isinstance(primary, dict):
        venue = clean_text(primary.get("raw_source_name"))
    if not venue:
        venue = next(
            (
                location_source_name(item) or clean_text(item.get("raw_source_name"))
                for item in locations
                if location_source_name(item) or clean_text(item.get("raw_source_name"))
            ),
            "",
        )
    venue_url = clean_text(primary.get("landing_page_url")) if isinstance(primary, dict) else ""

    doi: Any = work.get("doi")
    identifiers = work.get("ids")
    if not doi and isinstance(identifiers, dict):
        doi = identifiers.get("doi")

    arxiv_id = arxiv_id_from_doi(doi)
    arxiv_pdf = ""
    fallback_pdf = ""
    for location in locations:
        landing_page = clean_text(location.get("landing_page_url"))
        pdf_url = clean_text(location.get("pdf_url"))
        location_arxiv_id = arxiv_id_from_url(landing_page) or arxiv_id_from_url(pdf_url)
        if location_arxiv_id and not arxiv_id:
            arxiv_id = location_arxiv_id
        if location_arxiv_id and pdf_url and not arxiv_pdf:
            arxiv_pdf = pdf_url
        if pdf_url and not fallback_pdf:
            fallback_pdf = pdf_url
    if arxiv_id and not arxiv_pdf:
        arxiv_pdf = f"https://arxiv.org/pdf/{arxiv_id}"

    raw = {
        "title": title,
        "authors": authors,
        "year": year,
        "date": work.get("publication_date"),
        "venue": venue,
        "venue_url": venue_url,
        "doi": doi,
        "arxiv": arxiv_id,
        "pdf": arxiv_pdf or fallback_pdf,
    }
    return normalize_publication(raw, context=context)


def fetch_openalex_works(
    *, timeout: float, retries: int, email: str, api_key: str, verbose: bool
) -> tuple[list[dict[str, Any]], int]:
    """Fetch every cursor page and retain only exact target bylines."""

    headers = api_headers(email=email, api_key=api_key)
    cursor = "*"
    seen_cursors: set[str] = set()
    works_by_id: dict[str, Mapping[str, Any]] = {}
    fetched_count = 0

    for page_number in range(1, MAX_OPENALEX_PAGES + 1):
        if cursor in seen_cursors:
            raise FetchError("OpenAlex returned a repeated pagination cursor")
        seen_cursors.add(cursor)
        payload = fetch_json(
            openalex_url(cursor), headers=headers, timeout=timeout, retries=retries
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise FetchError("OpenAlex response is missing a results array")
        results = payload["results"]
        fetched_count += len(results)
        for index, work in enumerate(results):
            if not isinstance(work, dict):
                raise FetchError(
                    f"OpenAlex page {page_number} result {index} is not an object"
                )
            if not has_exact_target_byline(work):
                continue
            work_id = clean_text(work.get("id"))
            if not work_id:
                work_id = json.dumps(work, ensure_ascii=False, sort_keys=True)
            works_by_id[work_id] = work

        meta = payload.get("meta")
        if not isinstance(meta, dict):
            raise FetchError("OpenAlex response is missing pagination metadata")
        next_cursor = meta.get("next_cursor")
        log(
            f"OpenAlex page {page_number}: {len(results)} candidates, "
            f"{len(works_by_id)} exact works retained",
            verbose=verbose,
        )
        if not next_cursor:
            break
        if not isinstance(next_cursor, str):
            raise FetchError("OpenAlex returned an invalid next_cursor")
        cursor = next_cursor
    else:
        raise FetchError(f"OpenAlex exceeded {MAX_OPENALEX_PAGES} cursor pages")

    if not works_by_id:
        raise FetchError(
            "OpenAlex returned no works with the exact raw-author aliases; refusing "
            "to replace the cache"
        )

    publications: list[dict[str, Any]] = []
    skipped = 0
    for work_id, work in sorted(works_by_id.items()):
        try:
            publications.append(
                publication_from_openalex(work, context=f"OpenAlex work {work_id}")
            )
        except ValidationError as error:
            skipped += 1
            print(f"warning: skipped {work_id}: {error}", file=sys.stderr)
    if not publications:
        raise ValidationError("no complete OpenAlex publications remained after validation")
    if skipped:
        log(f"Skipped {skipped} incomplete OpenAlex work(s)", verbose=verbose)
    return publications, fetched_count


def first_text(value: Any) -> str:
    if isinstance(value, list):
        return next((clean_text(item) for item in value if clean_text(item)), "")
    return clean_text(value)


def crossref_date(message: Mapping[str, Any]) -> str:
    for field in ("published-print", "published-online", "published", "issued", "created"):
        value = message.get(field)
        if not isinstance(value, dict):
            continue
        date_parts = value.get("date-parts")
        if (
            isinstance(date_parts, list)
            and date_parts
            and isinstance(date_parts[0], list)
            and date_parts[0]
            and isinstance(date_parts[0][0], int)
        ):
            parts = date_parts[0]
            year = parts[0]
            month = parts[1] if len(parts) > 1 and isinstance(parts[1], int) else 1
            day = parts[2] if len(parts) > 2 and isinstance(parts[2], int) else 1
            try:
                return datetime.date(year, month, day).isoformat()
            except ValueError:
                continue
    return ""


def crossref_authors(message: Mapping[str, Any]) -> list[str]:
    raw_authors = message.get("author")
    if not isinstance(raw_authors, list):
        return []
    result: list[str] = []
    for author in raw_authors:
        if not isinstance(author, dict):
            continue
        literal = clean_text(author.get("name"))
        if literal:
            result.append(literal)
            continue
        name = clean_text(
            " ".join(
                part
                for part in (clean_text(author.get("given")), clean_text(author.get("family")))
                if part
            )
        )
        if name:
            result.append(name)
    return result


def publication_from_crossref(message: Mapping[str, Any], doi: str) -> dict[str, Any]:
    venue = first_text(message.get("container-title"))
    if not venue:
        event = message.get("event")
        if isinstance(event, dict):
            venue = clean_text(event.get("name"))
    if not venue:
        venue = clean_text(message.get("publisher"))

    pdf = ""
    links = message.get("link")
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue
            candidate = clean_text(link.get("URL"))
            content_type = clean_text(link.get("content-type")).casefold()
            if candidate and ("pdf" in content_type or candidate.casefold().endswith(".pdf")):
                pdf = candidate
                break

    publication_date = crossref_date(message)
    raw: dict[str, Any] = {
        "title": first_text(message.get("title")),
        "authors": crossref_authors(message),
        "year": int(publication_date[:4]) if publication_date else None,
        "date": publication_date,
        "venue": venue,
        "venue_url": message.get("URL"),
        "doi": message.get("DOI") or doi,
        "pdf": pdf,
    }
    return normalize_publication(raw, context=f"Crossref DOI {doi}")


def needs_crossref(publication: Mapping[str, Any]) -> bool:
    doi = normalize_doi(publication.get("doi"))
    if not doi or is_arxiv_doi(doi):
        return False
    return not publication.get("venue") or is_arxiv_venue(publication.get("venue"))


def enrich_from_crossref(
    publications: list[dict[str, Any]],
    *,
    timeout: float,
    retries: int,
    email: str,
    verbose: bool,
) -> int:
    headers = api_headers(email=email)
    enriched = 0
    for publication in publications:
        if not needs_crossref(publication):
            continue
        doi = normalize_doi(publication.get("doi"))
        url = CROSSREF_WORK_URL.format(doi=urllib.parse.quote(doi, safe=""))
        payload = fetch_json(url, headers=headers, timeout=timeout, retries=retries)
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            raise FetchError(f"Crossref response for {doi} is missing message metadata")
        candidate = publication_from_crossref(message, doi)
        before = dict(publication)
        for field in ("title", "authors", "year", "date", "venue", "venue_url", "pdf"):
            if field not in publication and field in candidate:
                publication[field] = candidate[field]
        if is_arxiv_venue(publication.get("venue")) and not is_arxiv_venue(
            candidate.get("venue")
        ):
            publication["venue"] = candidate["venue"]
        if publication != before:
            enriched += 1
            log(f"Crossref enriched {doi}", verbose=verbose)
    return enriched


def publication_priority(publication: Mapping[str, Any]) -> tuple[int, ...]:
    doi = publication.get("doi")
    venue = publication.get("venue")
    return (
        int(bool(doi) and not is_arxiv_doi(doi)),
        int(bool(venue) and not is_arxiv_venue(venue)),
        publication_date_ordinal(publication),
        int(bool(publication.get("arxiv"))),
        int(bool(publication.get("pdf"))),
        len(publication.get("authors", [])),
        sum(field in publication for field in FIELD_ORDER),
        len(clean_text(publication.get("title"))),
    )


def merge_publication_group(group: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge equivalent records with stable, publication-aware precedence."""

    if not group:
        raise ValidationError("cannot merge an empty publication group")
    ranked = sorted(
        (dict(item) for item in group),
        key=lambda item: (
            publication_priority(item),
            json.dumps(item, ensure_ascii=False, sort_keys=True),
        ),
        reverse=True,
    )
    merged = dict(ranked[0])
    for candidate in ranked[1:]:
        candidate_doi = candidate.get("doi")
        if candidate_doi and (
            not merged.get("doi")
            or (is_arxiv_doi(merged.get("doi")) and not is_arxiv_doi(candidate_doi))
        ):
            merged["doi"] = candidate_doi

        candidate_venue = candidate.get("venue")
        if candidate_venue and (
            not merged.get("venue")
            or (
                is_arxiv_venue(merged.get("venue"))
                and not is_arxiv_venue(candidate_venue)
            )
        ):
            merged["venue"] = candidate_venue
            if candidate.get("venue_url"):
                merged["venue_url"] = candidate["venue_url"]

        if (
            not merged.get("venue_url")
            and candidate.get("venue_url")
            and normalize_title(candidate.get("venue")) == normalize_title(merged.get("venue"))
        ):
            merged["venue_url"] = candidate["venue_url"]

        if (
            not merged.get("date")
            and candidate.get("date")
            and candidate.get("year") == merged.get("year")
        ):
            merged["date"] = candidate["date"]

        if len(candidate.get("authors", [])) > len(merged.get("authors", [])):
            merged["authors"] = candidate["authors"]

        candidate_pdf = clean_text(candidate.get("pdf"))
        current_pdf = clean_text(merged.get("pdf"))
        if candidate_pdf and (
            not current_pdf
            or (arxiv_id_from_url(candidate_pdf) and not arxiv_id_from_url(current_pdf))
        ):
            merged["pdf"] = candidate_pdf

        for field in (
            "arxiv",
            "code",
            "project",
            "description",
            "published",
            "selected",
        ):
            if field not in merged and field in candidate:
                merged[field] = candidate[field]

    return ordered_publication(merged)


def deduplicate_by(
    publications: Iterable[Mapping[str, Any]], *, identity: str
) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    unkeyed: list[Mapping[str, Any]] = []
    for publication in publications:
        if identity == "doi":
            key = normalize_doi(publication.get("doi"))
        elif identity == "title":
            key = normalize_title(publication.get("title"))
        else:
            raise ValueError(f"unsupported deduplication identity: {identity}")
        if key:
            groups.setdefault(key, []).append(publication)
        else:
            unkeyed.append(publication)
    merged = [merge_publication_group(group) for _, group in sorted(groups.items())]
    merged.extend(dict(item) for item in unkeyed)
    return merged


def apply_manual_rules(
    publications: Iterable[Mapping[str, Any]], rules: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], int, set[str]]:
    exclusions: set[str] = rules["exclude"]
    overrides: dict[str, dict[str, Any]] = rules["overrides"]
    result: list[dict[str, Any]] = []
    excluded_count = 0
    matched_overrides: set[str] = set()

    for index, publication in enumerate(publications):
        original_keys = publication_identity_keys(publication)
        if original_keys & exclusions:
            excluded_count += 1
            continue

        updated = dict(publication)
        title_key = next((key for key in original_keys if key.startswith("title:")), "")
        doi_key = next((key for key in original_keys if key.startswith("doi:")), "")
        # A DOI override is more precise and therefore wins over a title override.
        for key in (title_key, doi_key):
            if key and key in overrides:
                updated.update(overrides[key])
                matched_overrides.add(key)
        updated = normalize_publication(updated, context=f"publication candidate {index}")

        if publication_identity_keys(updated) & exclusions:
            excluded_count += 1
            continue
        result.append(updated)

    return result, excluded_count, matched_overrides


def publication_sort_key(publication: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        -publication_date_ordinal(publication),
        normalize_title(publication["title"]),
        normalize_doi(publication.get("doi")),
    )


def publication_date_ordinal(publication: Mapping[str, Any]) -> int:
    value = clean_text(publication.get("date"))
    if not value:
        value = f"{int(publication['year']):04d}-01-01"
    return datetime.date.fromisoformat(value).toordinal()


def validate_collection(
    raw: Any, *, context: str, require_sorted: bool
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValidationError(f"{context} must contain a JSON array")
    if not raw:
        raise ValidationError(f"{context} must contain at least one publication")
    normalized = [
        ordered_publication(normalize_publication(item, context=f"{context}[{index}]"))
        for index, item in enumerate(raw)
    ]

    doi_owners: dict[str, int] = {}
    title_owners: dict[str, int] = {}
    for index, publication in enumerate(normalized):
        doi = normalize_doi(publication.get("doi"))
        title = normalize_title(publication["title"])
        if doi:
            if doi in doi_owners:
                raise ValidationError(
                    f"{context} has duplicate DOI in entries {doi_owners[doi]} and {index}: {doi}"
                )
            doi_owners[doi] = index
        if title in title_owners:
            raise ValidationError(
                f"{context} has duplicate normalized title in entries "
                f"{title_owners[title]} and {index}: {title}"
            )
        title_owners[title] = index

    if require_sorted and normalized != sorted(normalized, key=publication_sort_key):
        raise ValidationError(
            f"{context} is not sorted by descending date and normalized title"
        )
    return normalized


def render_json(publications: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(
        [ordered_publication(item) for item in publications],
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def atomic_write(path: Path, content: str) -> bool:
    """Atomically replace path; return False when its content is unchanged."""

    try:
        current = path.read_text(encoding="utf-8") if path.exists() else None
    except OSError as error:
        raise PublicationError(f"could not read existing cache {path}: {error}") from error
    if current == content:
        return False
    if not path.parent.is_dir():
        raise PublicationError(f"output directory does not exist: {path.parent}")

    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temporary_name, path.stat().st_mode & 0o777)
        os.replace(temporary_name, path)
        temporary_name = ""
        return True
    except OSError as error:
        raise PublicationError(f"could not atomically write {path}: {error}") from error
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def check_cache(data_path: Path, overrides_path: Path) -> int:
    raw = load_json(data_path, required=True)
    publications = validate_collection(
        raw, context=str(data_path), require_sorted=True
    )
    rules = load_overrides(overrides_path)
    published = sum(item.get("published") is True for item in publications)
    selected = sum(item.get("selected") is True for item in publications)
    print(
        f"OK: {data_path} contains {len(publications)} valid, unique, sorted "
        f"record(s) ({published} published, {selected} selected)"
    )
    if overrides_path.exists():
        print(
            f"OK: {overrides_path} contains {len(rules['exclude'])} exclusion(s), "
            f"{len(rules['overrides'])} override(s), and "
            f"{len(rules['additions'])} addition(s)"
        )
    else:
        print(f"Note: {overrides_path} is absent; using empty manual overrides")
    return 0


def refresh(args: argparse.Namespace) -> int:
    rules = load_overrides(args.overrides)
    log(
        f"Fetching OpenAlex works for exact bylines: {OPENALEX_AUTHOR_QUERY}",
        verbose=not args.quiet,
    )
    fetched, candidate_count = fetch_openalex_works(
        timeout=args.timeout,
        retries=args.retries,
        email=args.email,
        api_key=args.api_key,
        verbose=not args.quiet,
    )
    log(
        f"OpenAlex returned {candidate_count} candidate result(s); "
        f"{len(fetched)} exact, complete publication(s) retained",
        verbose=not args.quiet,
    )

    candidates = fetched + list(rules["additions"])
    candidates, excluded_count, matched_overrides = apply_manual_rules(candidates, rules)
    publications = deduplicate_by(candidates, identity="doi")
    after_doi = len(publications)
    publications = deduplicate_by(publications, identity="title")
    after_title = len(publications)

    enriched_count = 0
    if args.crossref:
        enriched_count = enrich_from_crossref(
            publications,
            timeout=args.timeout,
            retries=args.retries,
            email=args.email,
            verbose=not args.quiet,
        )

    publications = [
        ordered_publication(
            normalize_publication(item, context=f"final publication {index}")
        )
        for index, item in enumerate(publications)
    ]
    publications.sort(key=publication_sort_key)
    publications = validate_collection(
        publications, context="generated publication data", require_sorted=True
    )
    content = render_json(publications)

    unmatched = sorted(set(rules["overrides"]) - matched_overrides)
    for key in unmatched:
        print(f"warning: override did not match a fetched/additional work: {key}", file=sys.stderr)

    summary = (
        f"Prepared {len(publications)} publication(s): {excluded_count} excluded; "
        f"{len(candidates) - after_doi} DOI duplicate(s) merged; "
        f"{after_doi - after_title} title duplicate(s) merged; "
        f"{enriched_count} Crossref enrichment(s)"
    )
    log(summary, verbose=not args.quiet)
    if args.dry_run:
        print("Dry run complete; cached JSON was not changed")
        return 0

    changed = atomic_write(args.data, content)
    print(
        f"{'Updated' if changed else 'Unchanged'}: {args.data} "
        f"({len(publications)} publication(s))"
    )
    return 0


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh deterministic local publication data from exact OpenAlex "
            "raw-author matches, or validate the cached data offline."
        )
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate cached data and overrides without making network requests",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch, merge, and validate but do not replace the cached JSON",
    )
    parser.add_argument(
        "--crossref",
        action="store_true",
        help="enrich missing published metadata from Crossref (failure is fatal)",
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=DEFAULT_DATA_PATH,
        help=f"cached publication JSON (default: {DEFAULT_DATA_PATH})",
    )
    parser.add_argument(
        "--overrides",
        type=Path,
        default=DEFAULT_OVERRIDES_PATH,
        help=f"manual override JSON (default: {DEFAULT_OVERRIDES_PATH})",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENALEX_API_KEY", ""),
        help="OpenAlex API key (default: OPENALEX_API_KEY environment variable)",
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("PUBLICATIONS_API_EMAIL", ""),
        help="contact email sent in API User-Agent (default: PUBLICATIONS_API_EMAIL)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=30.0,
        help="per-request timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--retries",
        type=nonnegative_int,
        default=3,
        help="transient API retries after the first request (default: 3)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress progress messages (errors and final status remain visible)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.check and args.dry_run:
        parser.error("--check and --dry-run are mutually exclusive")
    if args.check and args.crossref:
        parser.error("--check is offline and cannot be combined with --crossref")
    try:
        if args.check:
            return check_cache(args.data, args.overrides)
        return refresh(args)
    except KeyboardInterrupt:
        print("error: interrupted; cached publication data was not changed", file=sys.stderr)
        return 130
    except PublicationError as error:
        print(f"error: {error}", file=sys.stderr)
        print("Cached publication data was not changed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
