"""
The grounding check (PHASE_6_PLAN.md §4.3 and step 4).

ground_answer(answer, tool_trace) decides whether every factual number in an answer can be traced
to a successful tool result of the same turn, and says which tool supplied it. It is pure and
deterministic: no model, no network, no database, no clock, no randomness.

What is evidence
    * Numbers (JSON numbers, never booleans) in the `data` of a successful tool result. Strings are
      never numeric evidence, so a workspace named "revenue is 999999" proves nothing.
    * The server-validated effective arguments of a successful call (a `limit` of 5) and numbers in
      `meta.filters`: parameters, reported as kind "argument".
    * Numbers written in `meta.caveats`: text the services author, reported as kind "caveat".
    * Values derived from the above by a fixed, small set of operations (see _derive), each
      reported with the operation and the numbers it came from.
  Everything else is not evidence: the user's and assistant's messages (so a number the user states
  or an earlier answer repeats is unsupported until a tool returns it), failed tool results (their
  error text included), results whose endpoint or service does not match their tool, and results
  that conflict with another result of the same endpoint and arguments.

What is not a claim (never a failure by itself)
    ISO dates and months, "Jan 5, 2025" style dates, list numbering ("1." at a line start), short
    labels such as Q3, H1 or P95, and a four-digit year that appears in the evidence's dates. Dates
    are still audited: dates not found in the evidence are listed in `dates_not_in_evidence` and
    noted in `caveats`, which does not reject the answer (a date such as "the week ending ..." can be
    computed from a returned one). Identifiers (exp_042, ws_17, v1.2) are claims: each must appear
    verbatim in the evidence text. Ordinals (3rd) must fit within a returned list.

How a number is matched
    A token is read with its displayed precision and unit ($, %, pp, x, k/M/B or thousand/million/
    billion, and spelled-out numbers). It is supported when a candidate lies within half a unit of
    its last displayed digit, so 0.639 supports 63.9% and 64%, and 61,870 supports $61.9k. Percent
    tokens match a candidate or the candidate times 100. Magnitudes are compared without sign, so
    "fell 3 points" matches a difference of -3. A whole number ending in zeros ("62,000") can
    round a nearby value only when an approximation word ("about", "~") precedes it.

How derived values are limited
    Derived values are the way a wrong number could slip through by coincidence, so each operation
    has a narrow scope: sums, differences, ratios, shares and percent changes (never between a rate and a non-rate) between sibling
    fields of one object; adjacent and first-to-last (and, for series of at most SMALL_SERIES values,
    any pair of) values of one field across rows; sum, mean and count of a whole series, not when
    the tool layer cut that list; and the same field in two different results, not when their data
    versions differ. A small whole number (below 10, no unit) is accepted only as a direct value, a
    parameter, a count or a rank, never as a derived value.

Failure behavior
    Fail closed: no tool result, only failed ones, empty data, conflicting results or a number that
    nothing supports leave that number unverified. run_grounded_chat() then withholds the answer
    (policy "reject", the default) or keeps it with the numbers listed (policy "flag", the plan §4.1
    response shape, for the route to use if it prefers to show a warning). Neither rewrites the
    answer or calls a model again. A report holds only numbers the model wrote, fixed tool and
    endpoint names and the effective arguments: no exception text, no prompt, no secret.
"""
import bisect
import math
import re
from dataclasses import dataclass, field, replace
from datetime import date

from api.analyst import engine as chat_engine
from api.analyst import tools
from pipeline.log import redact

GROUNDING_VERSION = 'grounding-v1'
POLICIES = ('reject', 'flag')
SMALL_SERIES = 12                     # any pair of values is derived only within series this short
MAX_SERIES_VALUES = 400               # longer series give direct values only
MAX_SIBLING_FIELDS = 12
MAX_DERIVED = 150_000                 # cap on derived candidates for one answer (fail closed beyond)
MAX_ANSWER_SCAN = 20_000
MAX_CLAIMS = 200
MAX_CLAIM_CHARS = 40
UNGROUNDED_MESSAGE = ('The answer was withheld because some numbers in it could not be matched to '
                      'the tool results.')

_REL = 1e-9
_SCALES = {'k': 1e3, 'thousand': 1e3, 'm': 1e6, 'million': 1e6, 'b': 1e9, 'billion': 1e9}
_APPROX = re.compile(r'(?:\b(?:about|approx(?:imately)?\.?|around|roughly|nearly|almost|circa)|~|≈)'
                     r'\s*$', re.I)

# --- reading numbers from an answer ----------------------------------------------------------------

_MONTHS = ('Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|'
           'Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?')
_ISO = re.compile(r'(?<![\w-])(\d{4})-(\d{2})(?:-(\d{2}))?(?![\w-])')
# Month names match capitalised only, so the verb in "may 5 users" is not read as a date.
_NAMED_DATE = re.compile(
    rf'\b({_MONTHS})\.?\s+(?:(\d{{1,2}})(?:st|nd|rd|th)?,?\s+)?(\d{{4}})\b'
    rf'|\b({_MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!\s*(?:%|percent))')
_MONTH_NUMBER = {m: i + 1 for i, m in enumerate(
    ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])}
_LIST_NUMBER = re.compile(r'^[ \t]*\(?\d{1,3}[.)]\s', re.M)

_ONES = {w: i for i, w in enumerate(
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen '
    'sixteen seventeen eighteen nineteen'.split())}
_TENS = {w: 10 * (i + 2) for i, w in enumerate(
    'twenty thirty forty fifty sixty seventy eighty ninety'.split())}
_WORD = '|'.join(sorted([*_ONES, *_TENS, 'hundred', 'thousand', 'million', 'billion'], key=len,
                        reverse=True))
_IDENT_TEXT = r'[A-Za-z_][A-Za-z0-9_]*\d[A-Za-z0-9_]*'
_IDENTIFIER = rf'(?<![\w.])(?:v\d+(?:\.\d+)+|{_IDENT_TEXT}|\d+[A-Za-z_][A-Za-z0-9_]*)(?!\w)'
_IDENT_IN_TEXT = re.compile(_IDENTIFIER)

# Alternatives are tried in this order at each position: a word with a digit inside it (an
# identifier), a number with its unit, a digit-first word (10kg, 3d), then spelled-out numbers.
_TOKEN = re.compile(
    rf'(?P<identifier>(?<![\w.])(?:v\d+(?:\.\d+)+|{_IDENT_TEXT})(?!\w))'
    r'|(?P<number>(?<![\w.,])(?P<sign>[-−–+])?(?:[$£€]|US\$)?'
    r'(?P<digits>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)'
    r'(?:(?P<tight>%|pp|pts?|[kKmMbB]n?|x|×|st|nd|rd|th)|'
    r'\s(?P<spaced>percent(?:age)?(?:\s+points?)?|thousand|million|billion))?(?!\w))'
    r'|(?P<digitfirst>(?<![\w.])\d+[A-Za-z_][A-Za-z0-9_]*(?!\w))'
    rf'|(?P<words>\b(?:{_WORD})(?:[-\s]+(?:and[-\s]+)?(?:{_WORD}))*\b'
    r'(?:\s+(?P<wordunit>percent|per\s+cent))?)',
    re.I)


@dataclass(frozen=True)
class Claim:
    text: str
    kind: str                      # 'number', 'ordinal' or 'identifier'
    value: float = 0.0             # magnitude as written, with k/M/B applied
    decimals: int = 0              # digits after the point as written
    scale: float = 1.0             # 1000 for "61.9k": the last written digit is worth 100
    zeros: int = 0                 # trailing zeros of a whole number as written ("62,000": 3)
    unit: str = ''                 # '', '%' or 'x'
    approximate: bool = False
    before: str = ''               # a few characters of the answer on either side (to read a label such as "week-4")
    after: str = ''


# A small whole number written as a label ("week-4", "14-day", "4-week", "30 days") is not a measurement.
_LABEL_BEFORE = re.compile(r'\b(week|day|month|year)s?[-_ ]?$', re.I)
_LABEL_AFTER = re.compile(r'^[-_ ]?(week|day|month|year)s?\b', re.I)
_LABEL_WORD_FIRST = re.compile(r'\b(week|day|month|year)s?[-_ ]?(\d{1,3})(?!\d)', re.I)
_LABEL_NUMBER_FIRST = re.compile(r'(?<!\d)(\d{1,3})[-_ ]?(week|day|month|year)s?\b', re.I)


def label_of(claim):
    """(word, number) when the claim is a small whole number attached to week, day, month or year."""
    if claim.kind != 'number' or claim.unit or claim.decimals or claim.scale != 1.0 or not 0 <= claim.value < 1000             or not float(claim.value).is_integer():
        return None
    word = _LABEL_BEFORE.search(claim.before) or _LABEL_AFTER.match(claim.after)
    return (word.group(1).lower(), int(claim.value)) if word else None


def label_pairs(text):
    """The (word, number) labels a piece of evidence text uses: "week4_retention_rate", "within 14 days"."""
    pairs = {(m.group(1).lower(), int(m.group(2))) for m in _LABEL_WORD_FIRST.finditer(text)}
    pairs.update((m.group(2).lower(), int(m.group(1))) for m in _LABEL_NUMBER_FIRST.finditer(text))
    return pairs


def _blank(text, pattern, handler):
    """Replace each match with spaces when handler(match) is true, so offsets stay stable."""
    return pattern.sub(lambda m: ' ' * len(m.group(0)) if handler(m) else m.group(0), text)


def _valid_date(y, m, d=None):
    try:
        date(int(y), int(m), 1 if d is None else int(d))
        return True
    except ValueError:
        return False


def _dates_in(answer):
    """(answer with dates blanked, the 'YYYY-MM[-DD]' dates found). Dates are not numeric claims."""
    found = []

    def iso(m):
        ok = _valid_date(m.group(1), m.group(2), m.group(3))
        if ok:
            found.append(m.group(0))
        return ok

    def named(m):
        if m.group(4):                                     # "Jan 5": no year, nothing to audit
            return 1 <= int(m.group(5)) <= 31
        month, day, year = _MONTH_NUMBER[m.group(1)[:3]], m.group(2), m.group(3)
        if not _valid_date(year, month, day or 1):
            return False
        found.append(f'{year}-{month:02d}-{int(day):02d}' if day else f'{year}-{month:02d}')
        return True

    return _blank(_blank(answer, _ISO, iso), _NAMED_DATE, named), found


def _words_value(text):
    """The integer a run of number words spells, or None."""
    total = current = 0
    seen = False
    for word in re.split(r'[-\s]+', text.lower()):
        if word in ('', 'and'):
            continue
        seen = True
        if word in _ONES:
            current += _ONES[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == 'hundred':
            current = max(current, 1) * 100
        else:
            total += max(current, 1) * int(_SCALES[word])
            current = 0
    return total + current if seen else None


def extract_claims(answer):
    """(numeric claims in order, ISO-like dates found). Dates, list numbering and prose such as
    "one of" are not claims."""
    text = answer[:MAX_ANSWER_SCAN] if isinstance(answer, str) else ''
    masked, dates = _dates_in(_blank(text, _LIST_NUMBER, lambda m: True))
    claims = []
    for match in _TOKEN.finditer(masked):
        if len(claims) >= MAX_CLAIMS:
            break
        raw = match.group(0).strip()
        approximate = bool(_APPROX.search(masked[max(0, match.start() - 24):match.start()]))
        if match.group('identifier') or match.group('digitfirst'):
            claims.append(Claim(raw, 'identifier'))
        elif match.group('number'):
            digits = match.group('digits')
            word = (match.group('tight') or match.group('spaced') or '').lower()
            value = float(digits.replace(',', ''))
            decimals = len(digits.split('.', 1)[1]) if '.' in digits else 0
            if word in ('st', 'nd', 'rd', 'th'):
                claims.append(Claim(raw, 'ordinal', value))
                continue
            unit, scale = '', 1.0
            if word.startswith('percent') or word in ('%', 'pp', 'pt', 'pts'):
                unit = '%'
            elif word in ('x', '×'):
                unit = 'x'
            elif word.rstrip('n') in _SCALES:
                scale = _SCALES[word.rstrip('n')]
            whole = digits.replace(',', '')
            zeros = len(whole) - len(whole.rstrip('0')) if decimals == 0 else 0
            claims.append(Claim(raw, 'number', value * scale, decimals, scale, zeros, unit,
                                approximate, masked[max(0, match.start() - 10):match.start()],
                                masked[match.end():match.end() + 10]))
        else:
            words, percent = match.group('words'), bool(match.group('wordunit'))
            core = re.sub(r'\s+(?:percent|per\s+cent)$', '', words, flags=re.I)
            value = _words_value(core)
            small = all(w in _ONES for w in re.split(r'[-\s]+', core.lower()) if w not in ('', 'and'))
            if value is None or (small and not percent):
                continue                                   # "one of", "two": prose, not a figure
            claims.append(Claim(raw, 'number', float(value), 0, 1.0, 0, '%' if percent else '',
                                approximate, masked[max(0, match.start() - 10):match.start()],
                                masked[match.end():match.end() + 10]))
    return claims, dates


# --- reading evidence from tool results -----------------------------------------------------------

@dataclass
class Source:
    """One consulted tool result, with the Step 2 metadata kept intact."""
    call_id: str
    tool: str
    endpoint: str
    service: str
    arguments: dict
    data_version: str | None
    as_of: str | None
    relations: list
    caveats: list
    truncated: bool
    truncated_lists: list = field(default_factory=list)
    conflicted: bool = False

    def to_dict(self):
        return {'call_id': self.call_id, 'tool': self.tool, 'endpoint': self.endpoint,
                'service': self.service, 'arguments': self.arguments,
                'data_version': self.data_version, 'as_of': self.as_of, 'relations': self.relations,
                'caveats': self.caveats, 'truncated': self.truncated,
                'truncated_lists': self.truncated_lists, 'conflicted': self.conflicted}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _walk_numbers(node, path=()):
    """Yield (path, value) for every numeric leaf under node, in document order."""
    if _number(node):
        yield path, float(node)
    elif isinstance(node, dict):
        for key, item in node.items():
            yield from _walk_numbers(item, path + (str(key),))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk_numbers(item, path + (index,))


def _walk_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for item in node.values():
            yield from _walk_strings(item)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_strings(item)


LABEL_TEXT_KEYS = {'definition', 'note', 'description', 'label', 'unit'}


def _descriptive_strings(node):
    """String values stored under the services' own descriptive keys (not names or free text)."""
    if isinstance(node, dict):
        for key, item in node.items():
            if isinstance(item, str) and key in LABEL_TEXT_KEYS:
                yield item
            else:
                yield from _descriptive_strings(item)
    elif isinstance(node, list):
        for item in node:
            yield from _descriptive_strings(item)


def _walk_keys(node):
    if isinstance(node, dict):
        for key, item in node.items():
            yield str(key)
            yield from _walk_keys(item)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item)


def _lists(node):
    if isinstance(node, list):
        yield node
        for item in node:
            yield from _lists(item)
    elif isinstance(node, dict):
        for item in node.values():
            yield from _lists(item)


def _wildcard(path):
    return tuple('*' if isinstance(p, int) else p for p in path)


def _trusted(entry):
    """The trace entry's result when it is a well-formed, successful result of an approved tool
    whose source matches that tool; otherwise None. The trace is server-built, but a result that
    does not match its tool is never evidence."""
    result = entry.get('result') if isinstance(entry, dict) else None
    if not isinstance(result, dict) or result.get('ok') is not True:
        return None
    tool = tools.TOOLS.get(result.get('tool')) if isinstance(result.get('tool'), str) else None
    source = result.get('source')
    if tool is None or entry.get('name') != tool.name or not isinstance(source, dict):
        return None
    if source.get('endpoint') != tool.endpoint or source.get('service') != tool.service:
        return None
    if not isinstance(source.get('arguments'), dict) or not isinstance(result.get('data'), (dict, list)):
        return None
    return result


@dataclass
class Evidence:
    """Everything an answer may be matched against, built once per answer."""
    sources: dict                                    # call_id -> Source, every successful result
    values: list                                     # (magnitude, kind, call ids, description)
    strings: set                                     # identifier-like words in the evidence text
    dates: set                                       # 'YYYY', 'YYYY-MM' and 'YYYY-MM-DD' strings
    max_list: int
    labels: dict                                     # (word, number) -> call ids that use the label
    conflicts: list
    mixed_versions: bool
    index: list = field(default_factory=list)        # `values` sorted by magnitude
    derived_capped: bool = False


_DATE_IN_TEXT = re.compile(r'(\d{4})-(\d{2})(?:-(\d{2}))?')


def _add_dates(text, out):
    for m in _DATE_IN_TEXT.finditer(text):
        if _valid_date(m.group(1), m.group(2), m.group(3)):
            out.add(m.group(1))
            out.add(f'{m.group(1)}-{m.group(2)}')
            if m.group(3):
                out.add(m.group(0))


def build_evidence(tool_trace):
    sources, trusted = {}, []
    for entry in tool_trace if isinstance(tool_trace, list) else []:
        result = _trusted(entry)
        if result is None:
            continue
        meta = result.get('meta') if isinstance(result.get('meta'), dict) else {}
        limits = result.get('limits') if isinstance(result.get('limits'), dict) else {}
        cut = [r for r in (limits.get('truncated_lists') or []) if isinstance(r, dict)]
        relations = meta.get('sources') if isinstance(meta.get('sources'), list) else []
        caveats = meta.get('caveats') if isinstance(meta.get('caveats'), list) else []
        call_id = str(entry.get('id'))
        sources[call_id] = Source(
            call_id, result['tool'], result['source']['endpoint'], result['source']['service'],
            dict(result['source']['arguments']), meta.get('data_version'), meta.get('as_of'),
            [str(r) for r in relations], [str(c) for c in caveats], bool(cut), cut)
        trusted.append((call_id, result))

    # conflicting results: the same endpoint and arguments but different data
    groups, conflicts = {}, []
    for call_id, result in trusted:
        key = (result['source']['endpoint'], repr(sorted(result['source']['arguments'].items())))
        groups.setdefault(key, []).append((call_id, result))
    for (endpoint, _), members in sorted(groups.items()):
        fingerprints = {repr((r.get('data'), (r.get('meta') or {}).get('data_version')))
                        for _, r in members}
        if len(fingerprints) > 1:
            ids = [c for c, _ in members]
            conflicts.append({'endpoint': endpoint, 'call_ids': ids})
            for call_id in ids:
                sources[call_id].conflicted = True
    usable = [(c, r) for c, r in trusted if not sources[c].conflicted]
    mixed = len({sources[c].data_version for c, _ in usable}) > 1

    values, strings, dates, max_list, labels = [], set(), set(), 0, {}
    for call_id, result in usable:
        meta = result.get('meta') or {}
        for _, value in _walk_numbers(result['source']['arguments']):
            values.append((abs(value), 'argument', (call_id,), 'argument'))
        for _, value in _walk_numbers(meta.get('filters')):
            values.append((abs(value), 'argument', (call_id,), 'filter'))
        for caveat in meta.get('caveats') or []:
            if isinstance(caveat, str):
                plain = _DATE_IN_TEXT.sub(' ', caveat)
                values.extend((abs(float(t.replace(',', ''))), 'caveat', (call_id,), 'caveat')
                              for t in re.findall(r'\d+(?:,\d{3})*(?:\.\d+)?', plain))
        for text in _walk_strings({'meta': meta, 'data': result['data'], 'args': result['source']}):
            strings.update(t.lower() for t in _IDENT_IN_TEXT.findall(text))
            _add_dates(text, dates)
        # Labels come from field names and the services' own descriptive text (definitions, notes, caveats),
        # never from free-text warehouse values such as names or hypotheses, which are untrusted.
        for text in (*_walk_keys({'meta': meta, 'data': result['data']}), *_walk_strings(meta),
                     *_descriptive_strings(result['data'])):
            for pair in label_pairs(text):
                labels.setdefault(pair, []).append(call_id)
        for _, value in _walk_numbers(result['data']):
            values.append((abs(value), 'data', (call_id,), 'value'))
        max_list = max([max_list, *(len(v) for v in _lists(result['data']))])
    evidence = Evidence(sources, values, strings, dates, max_list, labels, conflicts, mixed)
    _derive(evidence, usable)
    evidence.index = sorted(evidence.values, key=lambda v: v[0])
    return evidence


# --- derived values ------------------------------------------------------------------------------------

def _rate_like(x):
    return 0 < abs(x) < 1


def _pair_values(a, b):
    """The (operation, magnitude) pairs for two values. A rate (strictly between 0 and 1) is never
    combined with a value that is not one: 0.639 + 1,240 is not a figure anybody reports."""
    if _rate_like(a) != _rate_like(b):
        return []
    out = [('difference', abs(a - b)), ('sum', abs(a + b))]
    if b:
        out += [('ratio', abs(a / b)), ('percent_change', abs((a - b) / b))]
    if a + b:
        out.append(('share', abs(a / (a + b))))
    return out


def _split(label):
    return [] if not isinstance(label, str) or label == '(data)' else label.split('.')


def _cut_paths(result):
    """The lists the tool layer shortened, as tuples of text path parts."""
    records = (result.get('limits') or {}).get('truncated_lists') or []
    return {tuple(_split(r.get('path'))) for r in records if isinstance(r, dict)}


def _partial(list_path, cut):
    """True when the list at list_path, or a list that contains it, was shortened."""
    path = tuple(str(p) for p in list_path)
    return any(path[:len(c)] == c for c in cut)


def _derive(evidence, usable):
    """Append derived candidates (value, kind, call ids, description) to evidence.values."""
    derived = []

    def add(kind, detail, call_ids, value):
        if len(derived) >= MAX_DERIVED:
            evidence.derived_capped = True
        elif math.isfinite(value):
            derived.append((value, kind, call_ids, detail))

    by_path = {}                                    # full numeric path -> [(call id, value)]
    for call_id, result in usable:
        cut = _cut_paths(result)
        siblings, series = {}, {}
        for path, value in _walk_numbers(result['data']):
            by_path.setdefault(path, []).append((call_id, value))
            siblings.setdefault(path[:-1], []).append((path[-1], value))
            indexes = [i for i, p in enumerate(path) if isinstance(p, int)]
            if indexes:
                series.setdefault(_wildcard(path), []).append((path[:indexes[-1]], value))
        for parent, fields in sorted(siblings.items(), key=lambda kv: repr(kv[0])):
            named = [(k, v) for k, v in fields if isinstance(k, str)]
            if 2 <= len(named) <= MAX_SIBLING_FIELDS:
                label = '.'.join(map(str, _wildcard(parent))) or '(data)'
                for i in range(len(named)):
                    for j in range(i + 1, len(named)):
                        for op, value in _pair_values(named[i][1], named[j][1]):
                            add('sibling', f'{op} of {label}.{named[i][0]} and {label}.{named[j][0]}',
                                (call_id,), value)
        for wildcard, items in sorted(series.items(), key=lambda kv: repr(kv[0])):
            numbers = [v for _, v in items]
            label = '.'.join(map(str, wildcard))
            if not 2 <= len(numbers) <= MAX_SERIES_VALUES:
                continue
            everything = len(numbers) <= SMALL_SERIES
            for i in range(len(numbers) - 1):
                for j in (range(i + 1, len(numbers)) if everything else (i + 1,)):
                    for op, value in _pair_values(numbers[i], numbers[j]):
                        add('series', f'{op} of {label}[{i}] and {label}[{j}]', (call_id,), value)
            if len(numbers) > 2 and not everything:
                for op, value in _pair_values(numbers[0], numbers[-1]):
                    add('series', f'{op} of first and last of {label}', (call_id,), value)
            if not any(_partial(lp, cut) for lp, _ in items):     # a cut list proves no total
                add('aggregate', f'sum of {label}', (call_id,), abs(sum(numbers)))
                add('aggregate', f'mean of {label}', (call_id,), abs(sum(numbers) / len(numbers)))
                add('count', f'count of {label}', (call_id,), float(len(numbers)))
    if not evidence.mixed_versions:                 # results from one data version only
        for path, members in sorted(by_path.items(), key=lambda kv: repr(kv[0])):
            if 2 <= len(members) <= SMALL_SERIES and len({c for c, _ in members}) == len(members):
                label = '.'.join(map(str, path))
                for i in range(len(members)):
                    for j in range(i + 1, len(members)):
                        for op, value in _pair_values(members[i][1], members[j][1]):
                            add('cross_result', f'{op} of {label} in {members[i][0]} and '
                                                f'{members[j][0]}', (members[i][0], members[j][0]),
                                value)
    evidence.values.extend(derived)


# --- matching -------------------------------------------------------------------------------------------

def _tolerance(claim, target_scale=1.0):
    """Half a unit of the last written digit ("61.9k": 50), or of the last non-zero digit when an
    approximation word precedes a whole number ("about 62,000": 500), in the units compared."""
    unit = 10.0 ** -claim.decimals * claim.scale
    if claim.approximate and claim.zeros:
        unit = 10.0 ** claim.zeros * claim.scale
    return 0.5 * unit * target_scale * (1 + _REL) + _REL * max(1.0, claim.value * target_scale)


def _too_coarse(claim):
    """A small whole number with no unit: it would match some derived value by coincidence."""
    return claim.unit == '' and claim.value < 10 and float(claim.value).is_integer()


_DIRECT_RANK = {'data': 0, 'argument': 1, 'caveat': 1, 'count': 2}      # derived kinds rank 3


def match_claim(claim, evidence):
    """(kind, call ids, description) of the best support for a number claim, or None. A direct
    value outranks a parameter, a parameter a count and a count a derived value."""
    targets = [(claim.value, 1.0)]
    if claim.unit == '%':
        targets.append((claim.value / 100.0, 0.01))          # 0.639 supports 63.9%
    best, best_rank = None, 99
    index = evidence.index
    for target, scale in targets:
        tol = _tolerance(claim, scale)
        k = bisect.bisect_left(index, target - tol, key=lambda v: v[0])
        while k < len(index) and index[k][0] <= target + tol:
            _, kind, call_ids, detail = index[k]
            k += 1
            rank = _DIRECT_RANK.get(kind, 3)
            if rank == 3 and _too_coarse(claim):
                continue                                       # too coarse to trust as derived
            if rank < best_rank:
                best, best_rank = (kind, call_ids, detail), rank
    return best


# --- the report --------------------------------------------------------------------------------------------

@dataclass
class GroundingReport:
    accepted: bool
    claims: list                      # per claim: text, kind, status, and what supported it
    unverified_numbers: list          # the text of each number or identifier nothing supports
    sources: list                     # every consulted tool result (Source.to_dict())
    supporting_sources: list          # call ids that supported at least one claim
    dates_not_in_evidence: list
    caveats: list                     # notes about the evidence ("truncated-evidence", ...)
    conflicts: list
    reason: str | None = None
    version: str = GROUNDING_VERSION

    def to_dict(self):
        return {'accepted': self.accepted, 'claims': self.claims,
                'unverified_numbers': self.unverified_numbers, 'sources': self.sources,
                'supporting_sources': self.supporting_sources,
                'dates_not_in_evidence': self.dates_not_in_evidence, 'caveats': self.caveats,
                'conflicts': self.conflicts, 'reason': self.reason, 'version': self.version}


def _identifier_supported(claim, evidence):
    text = claim.text.lower()
    if re.fullmatch(r'[a-z]{1,2}\d{1,2}', text):
        return True                                   # Q3, H1, P95, D7: labels, not facts
    return text in evidence.strings


def _year_supported(claim, evidence):
    return (claim.unit == '' and claim.decimals == 0 and claim.scale == 1.0 and ',' not in claim.text
            and 1900 <= claim.value <= 2100 and f'{int(claim.value)}' in evidence.dates)


def ground_answer(answer, tool_trace):
    """Check `answer` against `tool_trace` (ChatResult.tool_trace). Returns a GroundingReport and
    never raises: any internal failure is reported as an unaccepted answer."""
    try:
        return _ground(answer, tool_trace)
    except Exception:                                  # fail closed, never leak the cause
        return GroundingReport(False, [], [], [], [], [], ['grounding-error'], [],
                               reason='the grounding check could not run')


def _ground(answer, tool_trace):
    evidence = build_evidence(tool_trace)
    claims, dates = extract_claims(redact(answer) if isinstance(answer, str) else answer)
    records, unverified, supporting = [], [], []
    for claim in claims:
        shown = redact(claim.text)[:MAX_CLAIM_CHARS]        # a secret typed into an answer stays masked
        status, support = 'unverified', None
        if claim.kind == 'identifier':
            if _identifier_supported(claim, evidence):
                status = 'verified'
        elif claim.kind == 'ordinal':
            if float(claim.value).is_integer() and 1 <= claim.value <= evidence.max_list:
                status, support = 'verified', ('rank', (), 'position within a returned list')
        elif _year_supported(claim, evidence):
            status = 'verified'
        elif label_of(claim) in evidence.labels:
            call_ids = tuple(dict.fromkeys(evidence.labels[label_of(claim)]))
            status, support = 'verified', ('label', call_ids, 'a label the tool results use')
        else:
            support = match_claim(claim, evidence)
            if support:
                status = 'verified' if support[0] in ('data', 'argument', 'caveat') else 'derived'
        if status == 'unverified':
            unverified.append(shown)
        record = {'text': shown, 'kind': claim.kind, 'status': status}
        if support:
            record.update(support_kind=support[0], source_ids=list(support[1]), detail=support[2])
            supporting.extend(c for c in support[1] if c not in supporting)
        records.append(record)
    ungrounded_dates = sorted({d for d in dates if d not in evidence.dates})
    notes = []
    if any(s.truncated for s in evidence.sources.values()):
        notes.append('truncated-evidence')
    if evidence.conflicts:
        notes.append('conflicting-evidence')
    if evidence.mixed_versions:
        notes.append('mixed-data-versions')
    if evidence.derived_capped:
        notes.append('derived-values-capped')
    if ungrounded_dates:
        notes.append('dates-not-in-evidence')
    reason = None
    if unverified:
        reason = ('no successful tool result supports the numbers' if not evidence.sources
                  else 'some numbers are not supported by the tool results')
    return GroundingReport(not unverified, records, unverified,
                           [s.to_dict() for s in evidence.sources.values()], supporting,
                           ungrounded_dates, notes, evidence.conflicts, reason)


# --- the grounded chat ----------------------------------------------------------------------------------------

@dataclass
class GroundedChat:
    """A ChatResult plus its grounding report (None when there was no answer to check). With policy
    "reject" an answer with an unverified number is withheld: status 'ungrounded', answer None."""
    result: object
    report: GroundingReport | None

    def to_dict(self):
        return {**self.result.to_dict(), 'grounding': self.report.to_dict() if self.report else None}


def apply_policy(result, policy='reject'):
    """Ground a finished ChatResult. A result that has no answer (rejected input, timeout, tool
    limit, ...) passes through unchanged with no report."""
    if policy not in POLICIES:
        raise ValueError(f'policy must be one of {POLICIES}')
    if result.status != 'answered' or not result.answer:
        return GroundedChat(result, None)
    report = ground_answer(result.answer, result.tool_trace)
    if report.accepted or policy == 'flag':
        return GroundedChat(result, report)
    withheld = replace(result, status='ungrounded', answer=None,
                       error={'code': 'ungrounded-answer', 'message': UNGROUNDED_MESSAGE})
    return GroundedChat(withheld, report)


def run_grounded_chat(messages, *, policy='reject', **options):
    """run_chat() followed by the grounding check. Takes run_chat's keyword arguments."""
    return apply_policy(chat_engine.run_chat(messages, **options), policy)
