import difflib
import os
import re

from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

# Load variables from a local .env file when present. On hosting platforms
# (Render, Vercel, etc.) the variables come from the dashboard's environment
# settings instead, and load_dotenv() simply does nothing there.
load_dotenv()

# Ordinary question words that should never be treated as (misspelled) names.
COMMON_WORDS = {
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    "did", "does", "do", "has", "have", "had", "was", "were", "are", "is",
    "the", "and", "for", "from", "with", "about", "into", "that", "this",
    "purchase", "purchased", "purchases", "buy", "bought", "order", "orders",
    "ordered", "price", "prices", "cost", "costs", "product", "products",
    "item", "items", "money", "much", "many", "total", "amount", "spend",
    "spent", "get", "got", "show", "list", "all", "any", "name", "names",
    "customer", "customers", "user", "users", "quantity", "number",
    # short connectives: without these a phrase can start with one and the
    # typed text echoed back reads "of iphon 15" instead of "iphon 15"
    "of", "in", "on", "at", "to", "by", "as", "an", "or", "but", "not",
    "me", "my", "you", "your", "it", "its", "give", "tell", "find", "want",
    "need", "please", "top", "most", "least", "best", "each", "every",
}

# ===========================
# Database Connection (MongoDB)
# ===========================

MONGO_URL = (
    os.getenv("mongoDB_URL")
    or os.getenv("MONGODB_URL")
    or os.getenv("MONGO_URL")
)
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "shopping_ai")

# The collections this app knows about.
COLLECTIONS = ["users", "products", "orders", "order_items"]

# Connect lazily (on first query) rather than at import time, so a bad/unset
# URL doesn't crash the whole app on startup.
_client = None


def get_db():
    """Return the MongoDB database handle, connecting on first use."""

    global _client

    if _client is None:
        if not MONGO_URL:
            raise RuntimeError(
                "MongoDB URL is not set. Add mongoDB_URL to the environment "
                "(the Atlas connection string)."
            )
        _client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=10000)

    return _client[MONGO_DB_NAME]


def _clean(docs):
    """Drop the noisy ObjectId `_id` from result docs (keep meaningful
    $group _id values, which are not ObjectIds)."""

    for d in docs:
        if isinstance(d.get("_id"), ObjectId):
            d.pop("_id", None)
    return docs


# ===========================
# Describe the data (for the LLM)
# ===========================

def get_schema():
    """Describe each collection and its fields from a sample document,
    so the LLM knows what it can query."""

    db = get_db()
    schema = ""

    for coll in COLLECTIONS:
        doc = db[coll].find_one()
        schema += f"\nCollection: {coll}\n"
        if doc:
            for key, value in doc.items():
                if key == "_id":
                    continue
                schema += f"- {key} ({type(value).__name__})\n"

    return schema


# ===========================
# Run a MongoDB aggregation query
# ===========================

def run_query(collection, pipeline):
    """Execute an aggregation pipeline against a collection and return the
    result documents (as plain dicts)."""

    if collection not in COLLECTIONS:
        raise ValueError(f"Unknown collection: {collection}")

    db = get_db()
    docs = list(db[collection].aggregate(pipeline or []))
    return _clean(docs)


def find_all(collection):
    """Return every document in a collection (used by the /data viewer)."""

    if collection not in COLLECTIONS:
        raise ValueError(f"Unknown collection: {collection}")

    db = get_db()
    return list(db[collection].find({}, {"_id": 0}))


def get_profile():
    """Return the stored developer profile text (or '' if none)."""

    db = get_db()
    doc = db.profile.find_one({}, {"_id": 0})
    return doc.get("content", "") if doc else ""


# ===========================
# Name lists (for "did you mean?")
# ===========================

def get_customer_names():
    db = get_db()
    return [d["name"] for d in db.users.find({}, {"name": 1, "_id": 0}) if d.get("name")]


def get_product_names():
    db = get_db()
    return [
        d["product_name"]
        for d in db.products.find({}, {"product_name": 1, "_id": 0})
        if d.get("product_name")
    ]


# ===========================
# Empty-result check
# ===========================

def is_empty_result(result):
    """
    True if the query returned nothing meaningful.

    An aggregate like $sum/$count still returns ONE doc even when no rows
    match (e.g. [{'total': None}]), so a plain `not result` check misses it.
    Treat a result as empty when every value is None / 0 / "".
    """

    if not result:
        return True

    for row in result:
        for value in row.values():
            if value not in (None, 0, 0.0, ""):
                return False

    return True


# ===========================
# Fuzzy "Did you mean?" Suggestions
# ===========================

# Longest phrase compared against a name. Real names here run 1-3 words
# ("Ravi", "iPhone SE", "Samsung Galaxy S23"); scanning wider just invites
# question words into the match.
MAX_NAME_WORDS = 3


def _is_name_like(words):
    """
    True if `words` (already lowercased) could plausibly be a name.

    A name never begins with a question word or a bare number, and never ends
    with a question word — trailing digits are fine, since "iPhone 13" is a
    perfectly good name. Without those edge rules the scan below would test
    phrases like "cost iphone 13", which fuzzy-matches the junk "iPhone 193".
    """

    if not words:
        return False

    first, last = words[0], words[-1]

    if first in COMMON_WORDS or first.isdigit() or last in COMMON_WORDS:
        return False

    # at least one real word, so a lone number never matches a product
    return any(len(w) >= 3 and not w.isdigit() for w in words)


def _numbers(text):
    """The distinct numbers in a string ("iPhone 13" -> {"13"})."""

    return set(re.findall(r"\d+", text))


def _best_name(phrase, lower_names):
    """
    Closest known name to `phrase`, or None if nothing is close enough.

    A model number is an identity, not a spelling, so it must match exactly.
    Fuzzily it does not: "iPhone 13" scores 0.95 against the catalogue's
    "iPhone 193" (the digits "13" sit inside "193") — a better score than the
    real "iPhone 15" — so a plain ratio suggests obvious nonsense. Requiring
    equal numbers means a missing model is honestly reported as not found,
    while a genuine typo ("iphon 15" -> "iPhone 15") is still caught.
    """

    phrase_numbers = _numbers(phrase)

    scored = []

    for name in lower_names:

        if _numbers(name) != phrase_numbers:
            continue

        # 0.75 cutoff: real typos (sneh->sneha, ravii->ravi) score ~0.88,
        # while unrelated words (price->priya ~0.6) stay below the bar.
        ratio = difflib.SequenceMatcher(None, phrase, name).ratio()

        if ratio >= 0.75:
            # Tie-break on the shortest, then alphabetically first name, so
            # equally-close candidates resolve stably instead of by difflib's
            # arbitrary ordering.
            scored.append((-ratio, len(name), name))

    if not scored:
        return None

    return min(scored)[2]


def _mask_known_names(question, lower_names):
    """
    Blank out any known name that already appears in the question as a whole
    phrase, and return what's left.

    Without this, a multi-word name can never be seen as already-correct: the
    word-by-word scan below would read the "iPhone" of an already-correct
    "iPhone SE" as a typo *of* "iPhone SE" and suggest it again, so answering
    "yes" re-asks the same question forever.

    Longest names first, so "iPhone SE" is consumed before a bare "iPhone".
    """

    masked = question

    for name in sorted(lower_names, key=len, reverse=True):
        # (?<![A-Za-z]) / (?![A-Za-z]) = phrase boundaries that, unlike \b,
        # still hold for names containing spaces or punctuation.
        pattern = r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])"
        masked = re.sub(pattern, " ", masked, flags=re.IGNORECASE)

    return masked


def find_similar_names(question):
    """
    Look at each word in the user's question and, if it looks like a
    misspelled customer or product name, return close real matches.

    Returns a list of (typed_word, suggested_name) tuples.
    """

    known = get_customer_names() + get_product_names()

    # map lowercase -> original spelling so matching is case-insensitive
    lookup = {name.lower(): name for name in known}
    lower_names = list(lookup.keys())

    # Ignore names that are already spelled correctly, so a correct name is
    # never "corrected" into a different one.
    question = _mask_known_names(question, lower_names)

    # Tokens keep digits: product names are often "<word> <number>"
    # ("iPhone 13"), and a letters-only scan would drop the number and match
    # the bare word to an arbitrary model.
    tokens = list(re.finditer(r"[A-Za-z0-9]{2,}", question))

    suggestions = []
    seen = set()

    i = 0
    while i < len(tokens):

        # Longest phrase first: "iphone 13" should beat a bare "iphone", which
        # on its own is equally close to every iPhone in the catalogue.
        for size in range(min(MAX_NAME_WORDS, len(tokens) - i), 0, -1):

            span = tokens[i:i + size]
            phrase = question[span[0].start():span[-1].end()]

            if not _is_name_like([t.group().lower() for t in span]):
                continue

            # A number right after the phrase belongs to it: "iphone" out of
            # "iphone 13" must not be tested alone, or — the model number
            # dropped — it matches whichever iPhone happens to be closest.
            following = tokens[i + size] if i + size < len(tokens) else None
            if following is not None and following.group().isdigit():
                continue

            match = _best_name(phrase.lower(), lower_names)

            if match:
                if match not in seen:
                    suggestions.append((phrase, lookup[match]))
                    seen.add(match)
                i += size  # consume the whole phrase
                break
        else:
            i += 1

    return suggestions
