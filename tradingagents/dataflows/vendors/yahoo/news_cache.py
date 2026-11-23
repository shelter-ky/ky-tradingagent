"""Bounded exact-request news cache; cached news is never fresh outage coverage."""
import hashlib
import inspect
import json
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.files import replace_file

VERSION = "yahoo-news-v1"


def _now():
    return datetime.now(UTC)


def cached_news(func):
    signature = inspect.signature(func)

    @wraps(func)
    def wrapped(*args, **kwargs):
        cfg, now = get_config(), _now()
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        end = values.get("end_date", values.get("as_of_date"))
        # Historical requests retain the original provider behaviour. Never
        # substitute a capture from after the date of a historical analysis.
        if not cfg.get("data_cache_dir") or str(end) != now.date().isoformat():
            return func(*args, **kwargs)
        identity = {"tool": func.__name__, "arguments": dict(values),
                    "config": {k: cfg.get(k) for k in ("news_article_limit", "global_news_queries",
                                "global_news_article_limit", "global_news_lookback_days")}}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        path = Path(cfg["data_cache_dir"]) / VERSION / f"{digest}.json"
        cached = None
        try:
            if path.stat().st_size <= 5_000_000:
                obj = json.loads(path.read_text(encoding="utf-8"))
                at = datetime.fromisoformat(obj["observed_at"])
                if (obj.get("identity") == identity and at.tzinfo is not None and at <= now
                        and now - at <= timedelta(hours=24)
                        and isinstance(obj.get("text"), str) and obj["text"].startswith("## ")
                        and "\n### " in obj["text"]):
                    cached = (at, obj["text"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

        def render(label):
            at, text = cached
            return (f"News data source: {label}; original observation UTC: {at.isoformat()}. "
                    "Coverage is a sample, not proof that no other news exists.\n\n" + text)

        if cached and now - cached[0] <= timedelta(minutes=5):
            return render("Yahoo Finance local cache")
        try:
            text = func(*args, **kwargs)
        except VendorUnavailableError:
            if cached:
                return render("ARCHIVED NEWS REFERENCE ONLY — live refresh failed; news after the observation is unverified")
            raise
        if text.startswith("## ") and "\n### " in text:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                observed = _now()
                payload = {"identity": identity, "observed_at": observed.isoformat(), "text": text}
                replace_file(path, lambda temp: Path(temp).write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"))
                return (f"News data source: Yahoo Finance live response; observation UTC: {observed.isoformat()}.\n\n" + text)
            except (OSError, ValueError):
                return "News cache could not be saved; no future fallback established.\n\n" + text
        return text

    return wrapped
