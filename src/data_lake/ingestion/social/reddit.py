"""Reddit connector (PRAW) — r/wallstreetbets, r/investing, r/stocks, r/CanadianInvestor.

Setup: create a "script" app at https://www.reddit.com/prefs/apps, put the id/secret in .env.
Privacy: authors are stored as `stable_hash(username)` only — the plaintext username never
touches the database (Québec social-media-privacy constraint). Respect Reddit Data API terms
for retention/redistribution.

Disk note: the ``raw`` JSON column stores a *trimmed* dict (a handful of fields we might use in
signals), never the full submission object — full PRAW payloads are large and this box has little
free disk. Re-polling a submission refreshes the volatile fields (score/num_comments/body).
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from data_lake.db.models import SocialPost
from data_lake.ingestion.base import Connector, stable_hash


def _reddit_client(settings: Any):
    """Build a read-only PRAW client. Isolated so tests monkeypatch this, never import praw."""
    import praw  # imported lazily: tests replace this factory and never touch the network

    return praw.Reddit(
        client_id=settings.reddit_client_id,
        client_secret=settings.reddit_client_secret,
        user_agent=settings.reddit_user_agent,
        check_for_updates=False,
    )


def _author_hash(author: Any) -> str | None:
    """Hash the username, or None for deleted/removed authors. Plaintext is never stored."""
    if author is None:
        return None
    name = getattr(author, "name", None) or str(author)
    return stable_hash(name)


def _trimmed_raw(submission: Any) -> dict[str, Any]:
    """A small, fixed set of fields — never ``vars(submission)`` (huge)."""
    return {
        "permalink": getattr(submission, "permalink", None),
        "url": getattr(submission, "url", None),
        "upvote_ratio": getattr(submission, "upvote_ratio", None),
        "is_self": getattr(submission, "is_self", None),
        "link_flair_text": getattr(submission, "link_flair_text", None),
    }


class RedditConnector(Connector):
    name = "reddit"

    def fetch(self, subreddits: list[str] | None = None, limit: int = 100, **kwargs) -> int:
        settings = self.settings
        if not (settings.reddit_client_id and settings.reddit_client_secret):
            raise RuntimeError("REDDIT_CLIENT_ID/SECRET not set (see .env.example)")
        subreddits = subreddits or settings.subreddits

        reddit = _reddit_client(settings)
        fetched_at = datetime.now(UTC)
        count = 0
        with self.session() as session:
            for sub in subreddits:
                for submission in reddit.subreddit(sub).new(limit=limit):
                    created_at = datetime.fromtimestamp(float(submission.created_utc), tz=UTC)
                    existing = session.scalar(
                        select(SocialPost).where(
                            SocialPost.platform == self.name,
                            SocialPost.external_id == str(submission.id),
                        )
                    )
                    if existing:
                        # Re-poll: refresh the volatile fields, keep first-seen created_at.
                        existing.channel = sub
                        existing.title = submission.title
                        existing.body = submission.selftext or None
                        existing.score = int(submission.score)
                        existing.num_comments = int(submission.num_comments)
                        existing.raw = _trimmed_raw(submission)
                        existing.fetched_at = fetched_at
                    else:
                        session.add(
                            SocialPost(
                                platform=self.name,
                                channel=sub,
                                external_id=str(submission.id),
                                created_at=created_at,
                                author_hash=_author_hash(submission.author),
                                title=submission.title,
                                body=submission.selftext or None,
                                score=int(submission.score),
                                num_comments=int(submission.num_comments),
                                symbols=None,  # ticker extraction happens in signals/
                                sentiment=None,  # scored later
                                raw=_trimmed_raw(submission),
                                fetched_at=fetched_at,
                            )
                        )
                    count += 1
        return count
