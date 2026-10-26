"""Source-specific metadata and download adapters."""

from jiffle.features.imports.source_adapters.bluesky import BlueskySourceProvider
from jiffle.features.imports.source_adapters.danbooru import DanbooruSourceProvider
from jiffle.features.imports.source_adapters.e621 import E621SourceProvider
from jiffle.features.imports.source_adapters.fluffle import (
    FluffleReverseSearch,
    FluffleReverseSearchProvider,
)
from jiffle.features.imports.source_adapters.furaffinity import FurAffinitySourceProvider
from jiffle.features.imports.source_adapters.fuzzysearch import (
    FuzzySearchReverseSearch,
    FuzzySearchReverseSearchProvider,
)
from jiffle.features.imports.source_adapters.gelbooru import GelbooruSourceProvider
from jiffle.features.imports.source_adapters.iqdb import IqdbReverseSearch
from jiffle.features.imports.source_adapters.joyreactor import JoyReactorSourceProvider
from jiffle.features.imports.source_adapters.pawchive import PawchiveSourceProvider
from jiffle.features.imports.source_adapters.registry import (
    build_saucenao_provider,
    build_source_providers,
)
from jiffle.features.imports.source_adapters.rule34 import Rule34SourceProvider
from jiffle.features.imports.source_adapters.saucenao import SaucenaoReverseSearch
from jiffle.features.imports.source_adapters.tbib import TbibSourceProvider
from jiffle.features.imports.source_adapters.twitter import TwitterSourceProvider
from jiffle.features.imports.source_adapters.yandex import (
    YandexReverseSearch,
    YandexReverseSearchProvider,
)

__all__ = [
    "BlueskySourceProvider",
    "DanbooruSourceProvider",
    "E621SourceProvider",
    "FluffleReverseSearch",
    "FluffleReverseSearchProvider",
    "FurAffinitySourceProvider",
    "FuzzySearchReverseSearch",
    "FuzzySearchReverseSearchProvider",
    "GelbooruSourceProvider",
    "IqdbReverseSearch",
    "JoyReactorSourceProvider",
    "PawchiveSourceProvider",
    "Rule34SourceProvider",
    "SaucenaoReverseSearch",
    "TbibSourceProvider",
    "TwitterSourceProvider",
    "YandexReverseSearch",
    "YandexReverseSearchProvider",
    "build_saucenao_provider",
    "build_source_providers",
]
