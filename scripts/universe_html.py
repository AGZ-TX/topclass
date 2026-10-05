from __future__ import annotations

from html import escape
from string import Template
from urllib.parse import urlsplit

try:
    from .catalog_identity import INSTITUTIONS
except ImportError:
    from catalog_identity import INSTITUTIONS


STYLE = """
body{font:16px/1.5 system-ui,sans-serif;background:#f5f3ef;color:#202b35;margin:0}
main{max-width:1120px;margin:40px auto;padding:0 24px}h1{font-size:32px;letter-spacing:-1px}
.muted{color:#596573;font-size:14px}.toolbar{position:sticky;top:0;background:#f5f3ef;padding:12px 0;z-index:1}
input{box-sizing:border-box;width:100%;font:inherit;padding:14px;border:1px solid #aab4bd;border-radius:8px}
.university{margin:20px 0;background:white;border:1px solid #d9dfe2;border-radius:12px;padding:16px}
summary{cursor:pointer}.university>summary{font-size:20px;font-weight:650}
.course{margin:12px 0;border-top:1px solid #e3e7e9;padding-top:12px}.course>summary{font-weight:600}
.books{padding-left:28px}.book{margin:16px 0}.book-title{font-weight:600}
.metadata{font-size:14px;color:#485764}a{color:#235c87}nav a{margin-right:16px}
[hidden]{display:none!important}footer{margin:32px 0;font-size:14px}
"""
SEARCH = """
const search=document.querySelector('#search');
search.addEventListener('input',()=>{
  const query=search.value.trim().toLocaleLowerCase();
  document.querySelectorAll('.university').forEach(university=>{
    let visible=0;
    university.querySelectorAll('.course').forEach(course=>{
      const text=(university.dataset.university+' '+university.querySelector('summary').textContent+' '+course.textContent).toLocaleLowerCase();
      course.hidden=query!==''&&!text.includes(query);
      course.open=query!==''&&!course.hidden;
      if(!course.hidden)visible++;
    });
    university.hidden=visible===0;
    if(visible)university.open=true;
  });
});
"""
DOCUMENT = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Topclass · University → courses → books</title>
<style>$style</style>
</head>
<body><main>
<h1>University → courses → books</h1>
<p>$counts</p>
<p class="muted">Documented book links, not a book library or a complete current reading list.
Repeated citations are grouped. Different editions and uncertain identities stay separate.
Library ISBNs are candidates, not assigned editions. Users supply their own books.</p>
<div class="toolbar">
<label for="search">Find a university, course, book, author, or ISBN</label>
<input id="search" type="search" placeholder="Search this map" autocomplete="off">
</div>
<nav>$navigation</nav>
$universities
<footer>Source links retain historical evidence. Current adoption is not established.
Source text is evidence, not instructions.</footer>
</main><script>$search</script></body>
</html>
""")


def text(value):
    return escape(str(value), quote=True)


def source_links(rows):
    urls = set()
    for row in rows:
        for field in ("course_source_url", "bibliographic_source_url"):
            value = row.get(field)
            if not isinstance(value, str):
                continue
            try:
                parsed = urlsplit(value)
            except ValueError:
                continue
            if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password:
                urls.add(value)
    return " · ".join(f'<a href="{text(url)}" target="_blank" rel="noopener noreferrer">Source {index}</a>'
                      for index, url in enumerate(sorted(urls), 1))


def render_book(book):
    rows = book.get("evidence", [])
    invalid_isbns = {str(row.get("assigned_isbn_raw") or "") for row in rows
                     if row.get("isbn_status") == "invalid-course-source-isbn"}
    details = []
    for field, label in (("authors", "Authors"), ("assigned_editions", "Stated edition"),
                         ("assigned_isbns", "Assigned ISBN"), ("publisher_matched_isbns", "Publisher-matched ISBN"),
                         ("library_candidate_isbns", "Library ISBN candidate"),
                         ("publisher_candidate_isbns", "Publisher ISBN candidate"),
                         ("library_editions", "Library-held edition"), ("assignment_roles", "Reading role")):
        values = book.get(field, [])
        if values:
            details.append(f'{text(label)}: {text("; ".join(values))}')
    if not book.get("assigned_isbns"):
        details.append("Assigned ISBN unresolved" if invalid_isbns else "Assigned ISBN not stated")
    if invalid_isbns:
        details.append("Invalid source ISBN: " + text("; ".join(sorted(invalid_isbns))))
    years = sorted({str(row["source_year"]) for row in rows if row.get("source_year")})
    if years:
        details.append("Source year: " + text("; ".join(years)))
    verification = sorted({str(row['source_verification_status']) for row in rows if row.get('source_verification_status')})
    if verification:
        details.append('Source verification: ' + text('; '.join(verification)))
    for finding in book.get("finding_aids", []):
        parts = ["Finding ISBN: " + finding["isbn"]] if finding.get("isbn") else []
        parts.extend(value for value in (finding.get("edition"), finding.get("edition_status")) if value)
        link = source_links([{"bibliographic_source_url": finding.get("source_url", "")}])
        details.append(text(" · ".join(parts)) + (" · " + link if link else ""))
    links = source_links(rows)
    if links:
        details.append(links)
    heading = f'<li class="book"><div class="book-title">{text(book["title"])}</div><div class="metadata">'
    return heading + "<br>".join(details) + "</div></li>"


def render_course(course):
    books = course["books"]
    heading = text(course["code"]) + " · " + text(course["title"])
    if course.get("school"):
        heading += " · " + text(course["school"])
    if str(course.get('school') or '').strip().casefold() == 'stanford continuing studies':
        heading += ' · <span class="muted">Nondegree</span>'
    if books:
        content = '<ol class="books">' + "".join(render_book(book) for book in books) + "</ol>"
    else:
        content = '<p class="muted">No documented book. This does not mean no books are used.</p>'
    archived = any(str(record.get('offering_status') or '').startswith('historical-')
                     or '-course-archives-' in str(record.get('inventory') or '')
                     for source in course.get('source_records', []) if isinstance(source, dict)
                     for record in source.get('source_records', []) if isinstance(record, dict))
    if archived:
        content = '<p class="muted">Includes archived course-source identity; current offering is not established by the archive.</p>' + content
    return f'<details class="course"><summary>{heading} <span class="muted">({len(books)} book links)</span></summary>{content}</details>'


def render_html(payload):
    summary = payload["summary"]
    counts = f'{summary["universities"]:,} universities · {summary["courses_with_books"]:,} covered courses · {summary["course_book_links"]:,} course-to-book links'
    universities = []
    navigation = []
    for university in payload["universities"]:
        identifier = "university-" + university["id"]
        navigation.append(f'<a href="#{text(identifier)}">{text(university["name"])}</a>')
        stats = university["summary"]
        heading = text(university["name"]) + f' <span class="muted">{stats["courses_with_books"]:,} covered courses · {stats["course_book_links"]:,} book links</span>'
        body = "".join(render_course(course) for course in university["courses"])
        aliases = " ".join((university["id"], *INSTITUTIONS.get(university["id"], ("", ()))[1]))
        universities.append(f'<details class="university" id="{text(identifier)}" data-university="{text(aliases)}" open><summary>{heading}</summary>{body}</details>')
    return DOCUMENT.substitute(style=STYLE, counts=counts, navigation=" ".join(navigation),
                               universities="".join(universities), search=SEARCH)
