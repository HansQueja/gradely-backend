"""
Answer keys are stored on Quiz.answer_key as {"1": "A", "2": "C", ...}.

Teachers upload them as CSV/Excel (two columns: item number, answer; header
optional) or send them as JSON. Keys must cover every item of the quiz.
"""
import io

import pandas as pd

CHOICES = "ABCD"      # must match the printed answer sheet (scanner omr/layout.py)
MAX_ITEMS = 100


class AnswerKeyError(ValueError):
    pass


def normalize(raw, num_items: int) -> dict:
    """Validates a {item: answer} mapping against the quiz length; returns {"1": "A", ...} in item order."""
    if not isinstance(raw, dict) or not raw:
        raise AnswerKeyError("The answer key is empty.")

    key = {}
    for item, answer in raw.items():
        try:
            number = int(item)
        except (TypeError, ValueError):
            raise AnswerKeyError(f"Invalid item number: {item!r}.")
        letter = str(answer).strip().upper()
        if letter not in CHOICES:
            raise AnswerKeyError(f"Item {number}: answer must be one of {', '.join(CHOICES)} (got {answer!r}).")
        key[number] = letter

    extra = sorted(n for n in key if not 1 <= n <= num_items)
    if extra:
        raise AnswerKeyError(f"This quiz has {num_items} items, but the key includes item(s) {_ranges(extra)}.")
    missing = [n for n in range(1, num_items + 1) if n not in key]
    if missing:
        raise AnswerKeyError(f"The key is missing item(s) {_ranges(missing)}.")

    return {str(n): key[n] for n in sorted(key)}


def parse_file(uploaded_file, num_items: int) -> dict:
    name = uploaded_file.name.lower()
    content = io.BytesIO(uploaded_file.read())
    try:
        if name.endswith((".xls", ".xlsx")):
            df = pd.read_excel(content, header=None)
        elif name.endswith(".csv"):
            # utf-8-sig strips Excel's BOM; sep=None sniffs "," or ";"
            df = pd.read_csv(content, header=None, encoding="utf-8-sig", sep=None, engine="python")
        else:
            raise AnswerKeyError("Upload the answer key as .csv or .xlsx.")
    except AnswerKeyError:
        raise
    except Exception as e:
        raise AnswerKeyError(f"Could not read the file: {e}")

    if df.shape[1] < 2:
        raise AnswerKeyError("The file needs two columns: item number and answer.")

    raw = {}
    for _, row in df.iterrows():
        item, answer = row.iloc[0], row.iloc[1]
        try:
            item = int(float(item))
        except (TypeError, ValueError):
            continue  # header or blank row
        if pd.isna(answer):
            continue
        raw[item] = answer
    return normalize(raw, num_items)


def _ranges(numbers):
    """[1, 2, 3, 7] -> '1-3, 7'"""
    parts, start, prev = [], None, None
    for n in numbers:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            parts.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = n
    if start is not None:
        parts.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(parts)
