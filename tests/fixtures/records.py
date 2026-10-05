"""Realistic test records, shared across the suite.

Every record here is shaped like something a real source produces — Apollo's
nested organization object, a vendor CSV export, a list pasted out of a
spreadsheet — rather than like a minimal value that happens to satisfy the model.
That matters because the interesting failures live in realistic data: a
diacritic that decomposes differently on a Mac than on Linux, a "website" column
containing four words and a comma, an email address that is only *almost* one.

Two rules keep this usable as shared state:

* Records are built by **function**, never handed out as a module-level instance.
  Pydantic models are mutable, and a test that edits a shared fixture would leak
  into every later test that happened to run after it.
* The data tables (:data:`MALFORMED_DOMAINS`, :data:`INVALID_EMAILS`,
  :data:`INTERNATIONAL_NAMES`) are immutable inputs for parametrized tests, so
  every table is accompanied by what the expected outcome is — a table of inputs
  alone would push the expectation into the test body and lose it on the next
  edit.
"""

from __future__ import annotations

import unicodedata
from typing import Literal

from src.models.lead import RawLead

#: The two Unicode normalization forms a source realistically emits: composed
#: (APIs, most CSVs) and decomposed (macOS filenames, some Excel exports).
UnicodeForm = Literal["NFC", "NFD"]

# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #
#: Vietnamese personal names, in composed form. Vietnam is the target market for
#: the shipped search profiles, and Vietnamese is the script that exercises the
#: normalization path hardest: almost every syllable carries a tone mark *and*
#: often a vowel-quality mark, so a name has several combining characters per
#: word. ``Đ``/``đ`` is special — it is a distinct letter rather than a marked
#: ``D``, and it does not decompose at all under NFD.
VIETNAMESE_NAMES: tuple[str, ...] = (
    "Nguyễn Văn An",
    "Trần Thị Bích",
    "Lê Quốc Bảo",
    "Phạm Minh Đức",
    "Võ Thị Hồng",
    "Đặng Thu Hà",
    "Hoàng Ngọc Mai",
    "Bùi Thanh Tùng",
)

#: The same names decomposed, as macOS's filesystem and some Excel exports emit
#: them. Built rather than written out so the two forms cannot drift apart.
VIETNAMESE_NAMES_NFD: tuple[str, ...] = tuple(
    unicodedata.normalize("NFD", name) for name in VIETNAMESE_NAMES
)

#: Scripts beyond Latin, with one representative each. A pipeline that only ever
#: sees ASCII can pass every test and still mangle a name on first contact with
#: a real export, so each script is carried through normalization and export.
INTERNATIONAL_NAMES: tuple[tuple[str, str], ...] = (
    ("José Álvarez", "latin-accented"),
    ("Nguyễn Văn An", "vietnamese"),
    ("Ольга Иванова", "cyrillic"),
    ("Μαρία Παπαδοπούλου", "greek"),
    ("محمد الأحمد", "arabic"),
    ("김민준", "korean"),
    ("山田太郎", "japanese"),
    ("สมชาย ใจดี", "thai"),
)

#: Company names in the same scripts, for the company half of a lead.
INTERNATIONAL_COMPANIES: tuple[tuple[str, str], ...] = (
    ("Công ty TNHH Giải pháp Số", "vietnamese"),
    ("ООО Технологии", "cyrillic"),
    ("東京テクノロジー株式会社", "japanese"),
    ("شركة الحلول الرقمية", "arabic"),
    ("Acme & Sons, Inc.", "punctuation"),
)

#: Vietnamese addresses and cities, which reach a lead through ``company_city``
#: and ``company_country``. Cities arrive with their diacritics in a Vietnamese
#: export and without them in an English one — both are legitimate.
VIETNAMESE_CITIES: tuple[str, ...] = ("Hà Nội", "Hồ Chí Minh", "Đà Nẵng", "Cần Thơ")

# --------------------------------------------------------------------------- #
# Malformed URLs
# --------------------------------------------------------------------------- #
# Both URL helpers are exercised, and they answer different questions, so each
# gets its own table rather than one conflated list:
#
# * :func:`~src.utils.urls.normalize_domain` extracts a *host* from anything that
#   might contain one — a URL, an email address, a bare domain. An IP address is
#   a host, so it is returned; this function is not a fetcher and has no business
#   refusing one.
# * :func:`~src.utils.urls.normalize_page_url` decides whether something is a
#   *page that may be fetched*, which is why it is the one that refuses
#   ``javascript:`` and ``localhost``.
#
#: Input -> host a domain extractor must find, or ``None`` when there is none.
MALFORMED_DOMAINS: tuple[tuple[str, str | None], ...] = (
    # Nothing to extract.
    ("", None),
    ("   ", None),
    ("n/a", None),
    ("-", None),
    ("see our website, it is great", None),
    ("Acme Corp, Inc.", None),
    ("/just/a/path", None),
    ("www.", None),
    ("http://", None),
    ("https://", None),
    ("not a domain", None),
    # Recoverable: a host is in there, with noise around it.
    ("acme.com", "acme.com"),
    ("ACME.COM", "acme.com"),
    ("  acme.com  ", "acme.com"),
    ("www.acme.com", "acme.com"),
    ("https://www.acme.com/path?q=1#frag", "acme.com"),
    ("acme.com:8080", "acme.com"),  # a port is not part of the host
    ("ada@acme.com", "acme.com"),  # an address carries its host
    # A display-name wrapper is *not* unwrapped here, unlike in the email
    # normalizer — this function reads a string that should be a host, and one
    # that starts with a person's name is not one. Kept as a row because the
    # asymmetry between the two helpers is easy to assume away.
    ("Ada Lovelace <ada@acme.com>", None),
    ("careers.acme.com", "careers.acme.com"),  # subdomains are preserved
    ("acme.com.", "acme.com"),  # trailing root dot
    ("mailto:ada@acme.com", "acme.com"),
    # A host, but one no public crawler should ever fetch. Accepted here on
    # purpose — see the note above; the page-URL table is where they are refused.
    ("http://127.0.0.1/", "127.0.0.1"),
    ("http://192.168.0.1/", "192.168.0.1"),
)

#: Input -> the URL that may be fetched, or ``None`` when it may not. The
#: refusals are the security half: a scheme that executes code, or a host that is
#: this machine, must never come back as something to request.
MALFORMED_PAGE_URLS: tuple[tuple[str, str | None], ...] = (
    ("", None),
    ("   ", None),
    ("http://", None),
    ("see our website, it is great", None),
    ("/just/a/path", None),
    ("//acme.com/protocol-relative", None),
    ("http://localhost:8080/about", None),
    # Not pages. Following any of these is a bug, not a normalization nicety.
    ("javascript:alert(1)", None),
    ("javascript:alert('xss')", None),
    ("vbscript:msgbox(1)", None),
    ("mailto:ada@acme.com", None),
    ("tel:+14155550142", None),
    ("data:text/html,<h1>x</h1>", None),
    ("file:///etc/passwd", None),
    ("ftp://acme.com/pub", None),
    # Recoverable: a bare host becomes a page.
    ("acme.com", "https://acme.com"),
    ("www.acme.com", "https://acme.com"),
    ("  acme.com  ", "https://acme.com"),
    ("HTTPS://ACME.COM/About", "https://acme.com/About"),
    ("https://www.acme.com/a/b?q=1#frag", "https://acme.com/a/b?q=1"),
)

#: Schemes and shapes that must never come back as a fetchable page, as a flat
#: list so the set can be asserted in one pass. Every entry is a plausible value
#: for a spreadsheet's "website" column.
NON_FETCHABLE_URLS: tuple[str, ...] = (
    "javascript:alert(1)",
    "javascript:void(0)",
    "vbscript:msgbox(1)",
    "data:text/html,<h1>x</h1>",
    "data:text/html;base64,PHNjcmlwdD4=",
    "file:///etc/passwd",
    "ftp://acme.com/pub",
    "mailto:ada@acme.com",
    "tel:+14155550142",
    "sms:+14155550142",
    "about:blank",
    "chrome://settings",
    "http://localhost/",
    "http://localhost:3000/",
)

#: Hosts written in a non-ASCII script, and their punycode equivalents.
#:
#: This is a **limitation, pinned rather than wished away**: the domain grammar
#: is ASCII-only, so an IDN written in its own script is rejected while the same
#: host in punycode is accepted. A company whose site is published only as
#: ``münchen.de`` therefore loses its domain — and with it the ``name_domain``
#: deduplication key and any chance of website enrichment.
#:
#: Pinning it means the day someone adds IDNA support, this test fails and says
#: what changed, instead of the improvement arriving unnoticed.
UNICODE_HOSTS: tuple[tuple[str, str | None], ...] = (
    ("https://münchen.de/", None),
    ("https://例え.jp/", None),
    ("https://acme.vn/", "acme.vn"),
    ("https://xn--mnchen-3ya.de/", "xn--mnchen-3ya.de"),
    ("https://xn--r8jz45g.jp/", "xn--r8jz45g.jp"),
)

#: Inputs built to be hostile rather than merely wrong. The expectation is not a
#: particular host but that nothing raises, and that a newline never survives
#: into a URL — a CRLF that reaches a request line is header injection.
HOSTILE_URLS: tuple[str, ...] = (
    "http://acme.com/\x00null",
    "http://acme.com/\x1bescape",
    "http://acme.com/\r\nHeader: injected",
    "http://acme.com/\n\nGET /admin HTTP/1.1",
    "https://user:password@acme.com/secret",
    "http://" + "a" * 2000 + ".com/",
    "http://" + ".".join(["sub"] * 200) + ".acme.com/",
    "http://acme.com/" + "p" * 5000,
    "https://acme.com/?q=" + "x" * 5000,
)

# --------------------------------------------------------------------------- #
# Invalid and awkward email addresses
# --------------------------------------------------------------------------- #
#: Input -> normalized address, or ``None`` when it is not one.
#:
#: The ``None`` entries are the point: an address that cannot be parsed must
#: become *absent* rather than a corrupted string, because the normalized value
#: is a deduplication key. A half-parsed address is a key for a person who does
#: not exist, and two such records would collapse into one lead.
INVALID_EMAILS: tuple[tuple[str, str | None], ...] = (
    # Known "no value" tokens, which upstream exports use for empty cells.
    ("", None),
    ("   ", None),
    ("n/a", None),
    ("N/A", None),
    ("-", None),
    ("--", None),
    ("unknown", None),
    ("null", None),
    ("none", None),
    # Not addresses.
    ("not-an-email", None),
    ("ada@", None),
    ("@acme.com", None),
    ("ada@acme", None),  # no dot in the domain
    ("ada@@acme.com", None),
    ("ada@acme..com", None),
    ("ada acme@acme.com", None),
    ("ada@ac me.com", None),
    ("ada@acme.com,grace@navy.com", None),  # two addresses in one cell
    ("ada@acme.com grace@navy.com", None),
    # Recoverable forms that real exports contain.
    ("ada@acme.com\n", "ada@acme.com"),
    ("  ADA@ACME.COM  ", "ada@acme.com"),
    ("Ada.Lovelace@Acme.COM", "ada.lovelace@acme.com"),
    ("mailto:ada@acme.com", "ada@acme.com"),
    ("<ada@acme.com>", "ada@acme.com"),  # angle brackets are stripped
    ("Ada Lovelace <ada@acme.com>", "ada@acme.com"),  # display-name wrapper
)


# --------------------------------------------------------------------------- #
# Realistic records
# --------------------------------------------------------------------------- #
def complete_lead() -> RawLead:
    """Every field a source can supply, populated.

    The reference record for "nothing is missing": validation passes,
    completeness is 1.0, and no filter rule has grounds to reject it. Every field
    of :class:`~src.models.lead.RawLead` is set, so an export of this record has
    no blank cell in any column the model declares — which is what makes it
    useful as the fixture that catches a dropped field.

    The job title carries an ``at <company>`` suffix on purpose. The normalizer
    keeps the cleaned title in ``job_title`` and the original in
    ``job_title_raw``, and it only records the latter when it actually stripped
    something — so a title without a suffix would leave that column empty and
    the "no blank cells" property would be untestable.
    """
    return RawLead(
        provider="apollo",
        external_id="66f1a2b3c4d5e6f7a8b9c0d1",
        source_url="https://app.apollo.io/#/contacts/66f1a2b3c4d5e6f7a8b9c0d1",
        first_name="Ada",
        last_name="Lovelace",
        full_name="Ada Lovelace",
        job_title="VP of Engineering at Acme Corp",
        seniority="vp",
        email="ada.lovelace@acme.com",
        phone="+14155550142",
        linkedin_url="https://www.linkedin.com/in/ada-lovelace",
        company_name="Acme Corp",
        company_domain="acme.com",
        company_website="https://www.acme.com",
        company_industry="Software",
        company_employee_count=250,
        company_country="United States",
        company_city="Austin",
        company_linkedin_url="https://www.linkedin.com/company/acme",
        company_description="Acme Corp builds analytical engines for enterprise customers.",
        company_contact_url="https://www.acme.com/contact",
        company_social_links={
            "linkedin": "https://www.linkedin.com/company/acme",
            "github": "https://github.com/acme",
        },
    )


def missing_email() -> RawLead:
    """A perfectly good lead with no way to reach its person.

    The normal case for Apollo people-search, which returns contact records but
    not addresses, so this is the record that decides whether a run survives
    ``--require-email``.
    """
    lead = complete_lead()
    return lead.model_copy(
        update={"email": None, "provider": "apollo", "external_id": "66f1a2b3c4d5e6f7a8b9c0d2"}
    )


def missing_company() -> RawLead:
    """A person with no employer recorded — a freelance or stealth-mode contact.

    Identity still holds through the email, so this must validate; it simply has
    fewer ways to be matched later.
    """
    lead = complete_lead()
    return lead.model_copy(
        update={
            "company_name": None,
            "company_domain": None,
            "company_website": None,
            "company_industry": None,
            "company_employee_count": None,
            "company_country": None,
            "company_city": None,
            "company_linkedin_url": None,
        }
    )


def sparse_lead() -> RawLead:
    """Name plus employer and nothing else — the shape a purchased list has."""
    return RawLead(
        provider="csv",
        first_name="Grace",
        last_name="Hopper",
        full_name="Grace Hopper",
        company_name="Navy Systems",
        company_domain="navy.example.com",
    )


def vietnamese_lead(form: UnicodeForm = "NFC") -> RawLead:
    """A Vietnamese lead, in the Unicode form the caller asks for.

    Args:
        form: ``"NFC"`` (composed, what most APIs emit) or ``"NFD"`` (decomposed,
        what macOS and some Excel exports emit). The two describe the same
        person, which is exactly the property the deduplication tests pin.
    """
    return RawLead(
        provider="csv",
        full_name=unicodedata.normalize(form, "Nguyễn Văn An"),
        job_title="Giám đốc Kỹ thuật",
        email="an.nguyen@congty.vn",
        company_name=unicodedata.normalize(form, "Công ty TNHH Giải pháp Số"),
        company_domain="congty.vn",
        company_city=unicodedata.normalize(form, "Hà Nội"),
        company_country="Việt Nam",
    )


#: A CSV as a Vietnamese vendor would export it: a BOM from Excel, diacritics
#: throughout, a city, a company name with a comma-free legal suffix, and one
#: row whose website column holds a whole sentence instead of a domain.
VIETNAMESE_CSV = (
    "Họ và tên,Chức danh,Email,Công ty,Website,Thành phố,Quốc gia\n"
    "Nguyễn Văn An,Giám đốc Kỹ thuật,an.nguyen@congty.vn,"
    "Công ty TNHH Giải pháp Số,congty.vn,Hà Nội,Việt Nam\n"
    "Trần Thị Bích,Trưởng phòng Nhân sự,bich.tran@congty.vn,"
    "Công ty TNHH Giải pháp Số,congty.vn,Hồ Chí Minh,Việt Nam\n"
    "Phạm Minh Đức,Kỹ sư phần mềm,duc.pham@khac.vn,"
    "Công ty Cổ phần Khác,see our website it is great,Đà Nẵng,Việt Nam\n"
)
