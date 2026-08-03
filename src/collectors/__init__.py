"""데이터 수집기 모음.

arXiv 는 수집기가 아니다 — Reddit/X 에서 링크된 논문만 조회하는
보강 단계(src/enrichment.py)로 옮겼다.
"""

from .base import Collector
from .reddit import RedditCollector
from .x import XCollector

__all__ = ["Collector", "RedditCollector", "XCollector"]
