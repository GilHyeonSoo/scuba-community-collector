from .forums import DcinsideCollector, ScubaBoardCollector
from .naver import NaverCollector
from .reddit import RedditCollector
from .stores import AppStoreCollector, GooglePlayCollector

COLLECTORS = {
    "reddit": RedditCollector,
    "google_play": GooglePlayCollector,
    "app_store": AppStoreCollector,
    "dcinside": DcinsideCollector,
    "naver_cafe": NaverCollector,
    "scubaboard": ScubaBoardCollector,
}
